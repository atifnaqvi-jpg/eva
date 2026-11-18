# Backup, restore and rollback runbook

Covers **OPS-09**: *"Daily off-box `pg_dump` backup (plus files) + timed restore drill +
written runbook and rollback procedure"* (ARCHITECTURE §19.1, §20.4, §27).

The script is `scripts/ops09_backup.py` and every claim below is a command it runs.
The drill's measured result on this stack is in PLAN §8.34; if the numbers there and
here disagree, the PLAN section is the newer measurement.

```
python scripts/ops09_backup.py backup    # dump + files + manifest
python scripts/ops09_backup.py verify    # checksum, size and archive table of contents
python scripts/ops09_backup.py drill     # timed restore into a throwaway database
```

## What a backup contains

| Part | Where it comes from | Notes |
|---|---|---|
| `eva-<stamp>.dump.age` | `pg_dump --format=custom` run in the `postgres` container | All 19 public tables, RLS policies, grants, and `document_versions.payload` (uploaded document bytes live in the database) |
| `eva-<stamp>-files.tar.gz` | `SECRET_VAULT_HOST_DIR` (default `./.secrets`) | BYOK ciphertext only. **The age identity is excluded, always** |
| `eva-<stamp>.json` | this script | Row counts per table, the alembic revision, the archive checksum and the off-box measurement |

Two facts worth stating because they are easy to get wrong:

* **There are no other files.** Visitor upload bytes are discarded — `visitor_files`
  holds metadata only — and crawled/uploaded document text and bytes are columns, so
  `pg_dump` already covers them. A restore that only needs the database plus the vault
  directory is complete.
* **The identity is not backed up.** `app/security/vault.py` keeps the age identity
  apart from the ciphertext on purpose: *"the identity is on the host so that a backup
  of one is not a backup of the other."* Copying both into one archive would turn the
  archive into a plaintext key store. Restoring BYOK therefore needs the identity from
  wherever the operator keeps it — a second, separate copy, and losing it loses every
  tenant's provider key by design.

## Daily schedule

`ops/systemd/eva-backup.{service,timer}` run the backup at **02:45 UTC**, before the
03:15 retention purge, so the copy contains what the purge is about to modify.
`Persistent=true` runs a missed backup after a reboot.

Install, once, as root:

```sh
sudo install -d /etc/eva
sudo tee /etc/eva/backup.env >/dev/null <<'EOF'
OPS09_BACKUP_DIR=/mnt/backup/eva
OPS09_AGE_RECIPIENT=age1...            # `age-keygen -y <identity>` prints this
OPS09_AGE_IDENTITY=/root/.config/eva/backup-identity
OPS09_KEEP=14
EOF
sudo chmod 0600 /etc/eva/backup.env

sudo install -m 0644 ops/systemd/eva-backup.service /etc/systemd/system/
sudo install -m 0644 ops/systemd/eva-backup.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now eva-backup.timer
systemctl list-timers eva-backup.timer
```

`OPS09_BACKUP_DIR` must be a mount of another machine's disk or a synced directory.
The script **refuses** a destination inside the repository and **warns** when the
destination shares a filesystem with the database volume — a copy on the same disk as
the original is not an off-box backup, and `off_box_proven` in the manifest stays
`false` until the operator provides one. That is the honest state of a single-VPS
deployment, not a bug in the script.

`OPS09_KEEP` is opt-in: pruning is off unless it is set, because the retention number is
a policy value that the DPA still carries as `[[FILL: backup_retention]]`. Pick it, write
it into the DPA, then set it here.

## Restore to a fresh box

Order matters and each step is one command.

1. **Provision.** Docker + compose, the repository at the same commit as the backup's
   `alembic_revision` is compatible with, and `.env` with the *same*
   `POSTGRES_PASSWORD`, `EVA_APP_DB_PASSWORD` and `WIDGET_TOKEN_SECRET`. A new
   `WIDGET_TOKEN_SECRET` is safe (it invalidates widget sessions, nothing else); a new
   `EVA_APP_DB_PASSWORD` must be changed in the restored roles too, see step 4.
2. **Bring up only postgres.** `sudo docker compose up -d postgres`. Do not start the
   API yet: it would run migrations against an empty database.
3. **Create the owner role and the empty database.** `pg_dump` does not carry roles.
   ```sh
   sudo docker compose exec -T postgres psql -U eva -d postgres \
     -c "CREATE DATABASE eva"
   ```
   If the cluster is brand new, create the `eva` superuser first — the image's
   `POSTGRES_USER` does this on first start.
4. **Restore the archive into the empty database.** Restore the *whole* archive, not
   `--data-only`; see the trap below.
   ```sh
   age -d -i /root/.config/eva/backup-identity -o /tmp/restore.dump eva-<stamp>.dump.age
   sudo docker cp /tmp/restore.dump eva-postgres-1:/tmp/restore.dump
   sudo docker compose exec -T postgres \
     pg_restore --no-owner -U eva -d eva /tmp/restore.dump
   rm -f /tmp/restore.dump
   ```
   `--no-owner` because the dump names `eva` as owner and that is the role you just
   created. **`--no-privileges` must not be added**: it also drops the `eva_app`
   grants, and the restored database then answers the owner while the application gets
   `permission denied for table conversations`. Measured, see the traps.
5. **Restore the vault.** Untar `eva-<stamp>-files.tar.gz` into `SECRET_VAULT_HOST_DIR`
   (`./.secrets` by default) and put the age identity back where `SECRET_VAULT_IDENTITY`
   points. Preserve the `0600 root` ownership the api container expects.
6. **Reconcile the roles.** If `EVA_APP_DB_PASSWORD` changed, set it:
   `psql -c "ALTER ROLE eva_app PASSWORD '...'"`. The RLS policies came from the dump;
   verify them with `select count(*) from pg_policies` (17 today) and a scoped read as
   `eva_app`.
7. **Start the rest and verify.** `sudo docker compose up -d`, then
   ```sh
   curl -s http://localhost/health
   python scripts/ops09_backup.py drill --dest <backup dir>   # the restored box can drill itself
   ```

## Rollback procedure

Two halves, because a bad release can be either.

**Image.** The unit of rollback is a tagged image, so tag before deploying:

```sh
sudo docker tag eva-api:latest eva-api:pre-<change>
sudo docker compose up -d --build
# to roll back:
sudo docker tag eva-api:pre-<change> eva-api:latest && sudo docker compose up -d api
```

**Migration.** `drill` tests this on every run, against the restored copy, and reports
`migration_rollback.seconds`:

```
alembic downgrade -1     # one revision back
alembic upgrade head     # forward again
```

Both are run inside the `api` container with `DATABASE_URL` pointed at the scratch
database. What the drill proves is narrower than "every migration is reversible": it
proves the **head** revision downgrades and re-upgrades, which is the rollback a bad
deploy actually needs. Downgrading further is not exercised and `0001`'s `downgrade`
deliberately only revokes this database's privileges, because dropping `eva_app` would
break a sibling test database.

If a rollback needs data restored, it is the restore procedure above, not a downgrade:
a downgrade drops columns and does not bring their contents back.

## Traps this runbook exists to prevent

* **`--data-only` into a migrated database fails.** `pg_restore` replays `COPY` in
  archive order while the schema's foreign keys are already live, so the first
  dependent table aborts: `insert or update on table "usage_records" violates foreign
  key constraint`. Measured. Restore the whole archive into an **empty** database.
* **`pg_restore --list` needs a seekable file.** Piping the dump into the container
  gives `did not find magic string in file header`, because the custom format's header
  cannot be read from a pipe. The script copies the archive in and lists it there.
* **`--no-privileges` silently breaks the application role.** Row counts taken as the
  owner still match, so a drill that only compares counts passes while every request
  would 500. This is why `drill` reads one tenant's conversations **as `eva_app` inside
  a tenant scope**: it needs the grants *and* the policies to have survived.
* **`age` is not in the postgres image, and `pg_dump` is not in the api image.** The
  dump and restore run where `pg_dump` is; decryption runs on the host where `age` is.
  Adding either to a product image for a cron job would be a new dependency.
* **The vault ciphertext is root-owned** (`0600 root:root`), because the api container
  writes it as root. A host backup run by an unprivileged account cannot read it; the
  script retries through `sudo -n tar` and records `read_with_privilege` in the
  manifest rather than failing obscurely.
* **A backup inside the repository is refused outright.** It would be one `git add -A`
  from being committed, and it contains every tenant's data.

## What is still the operator's

| Item | Why it cannot be automated here |
|---|---|
| The off-box destination | Only the operator can mount or sync another machine's disk |
| `OPS09_AGE_RECIPIENT` / identity | Key material; the script fails closed without it |
| `OPS09_KEEP` | A retention policy the DPA still has as `[[FILL: backup_retention]]` |
| The **backup-failure alert** | OPS-04 (HALT-09): this row creates the producer, the alert that watches it is OPS-04's |
| Verifying the *fresh-VPS* RTO | A drill here restores into a scratch database on the same box; a genuine fresh-VPS restore is an operator exercise |
