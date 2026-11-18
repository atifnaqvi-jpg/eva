"""OPS-09: daily off-box ``pg_dump`` backup, verification, and a timed restore drill.

ARCHITECTURE §19.1 — *"Backups: daily ``pg_dump`` off-box; tested restore procedure."*
§20.4 adds the disaster-recovery shape (*"restore latest pg_dump + files to a fresh
VPS, RTO target: hours"*) and §27 asks for two checklist lines this row owns: *"Backup
restore drill completed and timed"* and *"Rollback procedure tested (image +
migration)"*. One command produces all of that evidence:

    python scripts/ops09_backup.py backup    # dump + files + manifest
    python scripts/ops09_backup.py verify    # is this artefact intact and complete?
    python scripts/ops09_backup.py drill     # timed restore into a scratch database

Three things this script refuses to pretend:

* **It cannot prove the artefact left the box.** It writes to ``OPS09_BACKUP_DIR`` and
  *measures* whether that sits on the same filesystem as the database volume, because
  a backup on the dying disk is a copy, not a backup. Making the destination a mount
  of another machine is the operator's step and the runbook says so.
* **It never copies the age identity.** ``vault.py``'s threat model is explicit: the
  identity is on the host *so that a backup of the ciphertext is not a backup of the
  keys*. Copying both into one archive would make the archive a plaintext key store.
  A restore therefore needs the identity from the operator's separate store.
* **It will not write an unencrypted dump by default.** A ``pg_dump`` carries session
  token hashes, password hashes and every tenant's data. ``--no-encrypt`` exists for a
  local drill and says so in the manifest.

Restore order is not obvious and cost a real debugging cycle to find: a
``--data-only`` restore into a freshly migrated database **fails on foreign keys**,
because ``pg_restore`` replays ``COPY`` in archive order and the schema's constraints
are already live. Restoring the whole archive into an *empty* database is the path that
works, and it also restores the RLS policies and grants — which is the half of a
restore that a test asserting row counts cannot see. See ``docs/ops/RUNBOOK.md``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FORMAT_VERSION = 1

# A restore that "succeeded" while restoring nothing is the failure this floor exists
# to catch: zero tables compared against zero tables is the same green as a clean
# restore (§8.25). The schema has 19 public tables today; 15 leaves room for a table
# being dropped on purpose without silently accepting an empty archive.
MIN_TABLES = 15

# The one table whose absence makes every tenant-scoped read return nothing, so a
# restore that loses it looks like an empty but healthy deployment.
REQUIRED_TABLES = ("tenants", "bots", "conversations", "messages", "alembic_version")

# Row counts are compared for every table in the manifest, but these are the ones a
# reviewer reads first: they are the tenant-visible data, and a restore that keeps
# tenants but loses their conversations is exactly the silent-partial case.
SALIENT_TABLES = ("tenants", "bots", "conversations", "messages", "chunks", "bot_domains")


class BackupError(RuntimeError):
    """A step that must stop the run rather than be reported as a warning."""


# --- configuration -------------------------------------------------------------


def repo_root() -> Path:
    return REPO


def backup_dir(explicit: str | None = None) -> Path:
    raw = explicit or os.environ.get("OPS09_BACKUP_DIR") or "/var/backups/eva"
    return Path(raw).expanduser().resolve()


def age_recipient() -> str:
    value = os.environ.get("OPS09_AGE_RECIPIENT", "").strip()
    if not value:
        raise BackupError(
            "OPS09_AGE_RECIPIENT is not set. A dump holds password hashes, session "
            "token hashes and every tenant's data, so an unencrypted one on disk is a "
            "confidentiality hazard; the script fails closed rather than defaulting. "
            "Generate a key pair once with `age-keygen -o <off-box path>` and put the "
            "public key here. Use --no-encrypt only for a local drill."
        )
    return value


def age_identity() -> Path:
    raw = os.environ.get("OPS09_AGE_IDENTITY") or os.environ.get("SECRET_VAULT_IDENTITY")
    if not raw:
        raise BackupError(
            "no age identity: set OPS09_AGE_IDENTITY (the key matching "
            "OPS09_AGE_RECIPIENT). It must come from the operator's off-box store, "
            "not from the vault directory being backed up."
        )
    path = Path(raw).expanduser()
    if not path.is_file():
        raise BackupError(f"age identity not found: {path}")
    return path


def vault_dir() -> Path | None:
    """The BYOK ciphertext directory, if this deployment has one.

    ``SECRET_VAULT_HOST_DIR`` is what compose bind-mounts and defaults to ``./.secrets``
    in the work tree, so that is the fallback rather than "no files": a backup that
    silently skipped the vault would need a second, separate restore step nobody wrote
    down. ``OPS09_FILES_DIR`` overrides for a deployment that moved it.
    """
    candidates = [os.environ.get("OPS09_FILES_DIR", "").strip(),
                  os.environ.get("SECRET_VAULT_HOST_DIR", "").strip()]
    for raw in candidates:
        if raw and Path(raw).expanduser().is_dir():
            return Path(raw).expanduser().resolve()
    fallback = repo_root() / ".secrets"
    return fallback.resolve() if fallback.is_dir() else None


def identity_paths() -> list[Path]:
    """Every path that must never enter a backup archive.

    The identity decrypts every tenant's provider key, so archiving it would turn the
    backup into a plaintext key store — ``vault.py`` deliberately keeps it apart so
    that one backup is not a backup of the other. Resolved independently of whether
    this run is encrypting: the plaintext drill path is exactly where a stray copy
    would be easiest to miss.
    """
    found: list[Path] = []
    for key in ("OPS09_AGE_IDENTITY", "SECRET_VAULT_IDENTITY"):
        raw = os.environ.get(key, "").strip()
        if raw:
            found.append(Path(raw).expanduser().resolve())
    directory = vault_dir()
    if directory is not None:
        # Matched by name as well, because an identity the operator put at the default
        # path is the likely case and must be excluded even when no env var names it.
        found.extend(p.resolve() for p in directory.glob("age-identity*"))
    return found


# --- docker plumbing -----------------------------------------------------------


def docker(*args: str, check: bool = True, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    """Run docker, adding sudo only when the socket needs it (as ``m4_gate.py`` does).

    Every call carries a timeout: a ``compose exec`` against a container that is
    restarting can block forever, and a drill that hangs is indistinguishable from one
    that is still running.
    """
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=False, timeout=timeout
    )
    if result.returncode != 0 and "permission denied" in (result.stderr or "").lower():
        result = subprocess.run(
            ["sudo", "-n", "docker", *args], capture_output=True, text=True,
            check=False, timeout=timeout,
        )
    if check and result.returncode != 0:
        raise BackupError(
            f"docker {' '.join(args)} failed ({result.returncode}): "
            f"{(result.stderr or result.stdout).strip()[:500]}"
        )
    return result


def psql(sql: str, *, database: str = "postgres") -> str:
    """Run one statement as the database owner and return stdout.

    ``database`` is the maintenance database by default: creating and dropping the
    scratch database cannot be done from inside the database being restored.
    """
    args = ["compose", "exec", "-T", "postgres", "psql", "-U", "eva", "-d", database,
            "-tAc", sql]
    return docker(*args).stdout.strip()


def postgres_container() -> str:
    """The running postgres container's name, so ``docker cp`` is not hard-coded."""
    names = docker("compose", "ps", "--format", "{{.Name}}", "postgres").stdout.split()
    if not names:
        raise BackupError("no running postgres container; start the stack first")
    return names[0]


def dump_args(dbname: str) -> list[str]:
    """The exact argv of the dump. A function so a test can assert it rather than guess."""
    return [
        "compose", "exec", "-T", "postgres", "pg_dump",
        "--format=custom", "--compress=6", "-U", "eva", "-d", dbname,
    ]


def restore_args(dbname: str, archive: str) -> list[str]:
    """Restore argv for the drill.

    ``--no-owner`` because the dump records tables as owned by ``eva``, which exists on
    a fresh cluster only if the operator created it; ``--no-privileges`` is deliberately
    **absent**, because dropping it also drops the ``eva_app`` grants the API needs.
    Measured: with ``--no-privileges`` the restored database answers the owner but the
    app role gets ``permission denied for table conversations``.
    """
    return [
        "compose", "exec", "-T", "postgres", "pg_restore",
        "--no-owner", "-U", "eva", "-d", dbname, archive,
    ]


# --- table inventory -----------------------------------------------------------


COUNT_QUERY = (
    "SELECT string_agg(format('SELECT %L AS t, count(*) AS n FROM public.%I', relname, relname),"
    " ' UNION ALL ') FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
    " WHERE n.nspname = 'public' AND c.relkind = 'r'"
)


def table_counts(database: str) -> dict[str, int]:
    """Row counts for every public table, read from the database itself.

    Built from ``pg_class`` rather than a hard-coded list so a new table is covered the
    day its migration lands, and so the manifest can be compared table by table.
    """
    union = psql(COUNT_QUERY, database=database)
    if not union:
        raise BackupError(f"no public tables found in {database}")
    out: dict[str, int] = {}
    for line in psql(union, database=database).splitlines():
        name, _, count = line.partition("|")
        if name:
            out[name] = int(count)
    return out


def alembic_revision(database: str) -> str | None:
    try:
        return psql("SELECT version_num FROM alembic_version", database=database) or None
    except BackupError:
        return None


# --- manifest ------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass
class Manifest:
    created_at: str
    database: str
    alembic_revision: str | None
    encrypted: bool
    dump: dict
    tables: dict[str, int]
    files: dict = field(default_factory=dict)
    off_box: dict = field(default_factory=dict)
    format_version: int = FORMAT_VERSION

    def to_json(self) -> str:
        payload = {
            "format_version": self.format_version,
            "created_at": self.created_at,
            "database": self.database,
            "alembic_revision": self.alembic_revision,
            "encrypted": self.encrypted,
            "dump": self.dump,
            "tables": self.tables,
            "files": self.files,
            "off_box": self.off_box,
        }
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def read_manifest(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise BackupError(f"unreadable manifest {path}: {exc}") from None
    if data.get("format_version") != FORMAT_VERSION:
        raise BackupError(
            f"manifest format_version is {data.get('format_version')!r}, this build reads "
            f"{FORMAT_VERSION}; an old manifest is not silently reinterpreted"
        )
    missing = {"created_at", "dump", "tables"} - set(data)
    if missing:
        raise BackupError(f"manifest is missing {sorted(missing)}")
    return data


def check_manifest_completeness(manifest: dict | Manifest) -> None:
    """Fail on a manifest that would make the drill pass vacuously.

    Every one of these is a way for a backup to look complete while being useless:
    no tables, no dump, or a table set missing the one the whole tenant model hangs off.
    Accepts either the dataclass (on the write path) or the parsed JSON (on the read
    path), so a manifest cannot satisfy one shape of this check and not the other.
    """
    data = manifest if isinstance(manifest, dict) else {
        "tables": manifest.tables, "dump": manifest.dump,
    }
    tables = data.get("tables") or {}
    if len(tables) < MIN_TABLES:
        raise BackupError(
            f"manifest lists {len(tables)} tables, below the floor of {MIN_TABLES}: "
            "a restore compared against this could not fail"
        )
    absent = [t for t in REQUIRED_TABLES if t not in tables]
    if absent:
        raise BackupError(f"manifest is missing required table(s): {', '.join(absent)}")
    dump = data.get("dump") or {}
    if not dump.get("name") or not dump.get("sha256"):
        raise BackupError("manifest has no dump name/sha256; nothing to verify")
    if int(dump.get("bytes") or 0) <= 0:
        raise BackupError("manifest records an empty dump")


def compare_counts(expected: dict[str, int], actual: dict[str, int]) -> list[str]:
    """Differences that mean the restore is not the backup, as readable strings."""
    problems: list[str] = []
    for table in sorted(expected):
        if table not in actual:
            problems.append(f"{table}: missing after restore")
        elif actual[table] != expected[table]:
            problems.append(f"{table}: {expected[table]} -> {actual[table]}")
    return problems


# --- backup --------------------------------------------------------------------


def _device(path: Path) -> int | None:
    try:
        return path.stat().st_dev
    except OSError:
        return None


def off_box_check(dest: Path) -> dict:
    """Measure whether ``dest`` is really somewhere else.

    Two facts, both measured rather than assumed: whether the destination is inside the
    repository (it must not be — the repo is the thing that could be lost, and a dump
    there would also be committable), and whether it shares a filesystem with the
    database volume. The second is reported, not enforced: only the operator can mount
    another machine's disk, but the runbook's claim "off-box" should be checkable.
    """
    repo = repo_root()
    inside_repo = dest == repo or repo in dest.parents
    if inside_repo:
        raise BackupError(
            f"backup destination {dest} is inside the repository. A dump of every "
            "tenant's data must not live in a work tree that gets committed, backed up "
            "and cloned; pass OPS09_BACKUP_DIR pointing outside it."
        )

    db_device = None
    try:
        raw = docker(
            "compose", "exec", "-T", "postgres",
            "stat", "-c", "%d", "/var/lib/postgresql/data",
        ).stdout.strip()
        db_device = int(raw) if raw else None
    except (BackupError, ValueError):
        db_device = None

    dest_device = _device(dest) if dest.exists() else None
    same = db_device is not None and dest_device is not None and db_device == dest_device
    return {
        "destination": str(dest),
        "database_device": db_device,
        "destination_device": dest_device,
        "same_filesystem_as_database": same,
        "off_box_proven": False,
    }


def run_backup(dest: Path, dbname: str, *, encrypt: bool, keep: int | None) -> int:
    dest.mkdir(parents=True, exist_ok=True)
    # Refuse before dumping: a dump written into the work tree is one `git add -A` away
    # from being committed, and it contains every tenant's data.
    off_box_check(dest)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = f"eva-{stamp}"
    plain = dest / f"{base}.dump"
    sealed = dest / f"{base}.dump.age"
    target = sealed if encrypt else plain

    recipient = age_recipient() if encrypt else None
    started = time.monotonic()

    # Dump straight through age: the plaintext never lands on disk when encrypting, so
    # there is no window where a crash leaves an unprotected copy behind.
    dump_cmd = docker_command(dump_args(dbname))
    if encrypt:
        producer = subprocess.Popen(dump_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        with target.open("wb") as out:
            sealed_proc = subprocess.Popen(
                ["age", "-r", recipient], stdin=producer.stdout, stdout=out,
                stderr=subprocess.PIPE,
            )
            producer.stdout.close()
            _, age_err = sealed_proc.communicate()
        _, dump_err = producer.communicate()
        if producer.returncode != 0:
            target.unlink(missing_ok=True)
            raise BackupError(f"pg_dump failed: {dump_err.decode(errors='replace')[:500]}")
        if sealed_proc.returncode != 0:
            target.unlink(missing_ok=True)
            raise BackupError(f"age failed: {age_err.decode(errors='replace')[:500]}")
    else:
        with target.open("wb") as out:
            result = subprocess.run(dump_cmd, stdout=out, stderr=subprocess.PIPE)
        if result.returncode != 0:
            target.unlink(missing_ok=True)
            raise BackupError(f"pg_dump failed: {result.stderr.decode(errors='replace')[:500]}")

    elapsed = time.monotonic() - started

    # The TOC is the only proof that the bytes are an archive rather than a truncated
    # pipe. Read it from the artefact that was just written.
    toc = archive_toc(target, encrypted=encrypt)
    missing = [t for t in REQUIRED_TABLES if f" TABLE public {t} " not in toc]
    if missing:
        target.unlink(missing_ok=True)
        raise BackupError(f"dump is missing table(s) {', '.join(missing)}; archive discarded")

    files = _copy_files(dest, base)
    manifest = Manifest(
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        database=dbname,
        alembic_revision=alembic_revision(dbname),
        encrypted=encrypt,
        dump={
            "name": target.name,
            "bytes": target.stat().st_size,
            "sha256": sha256_file(target),
            "toc_entries": sum(1 for line in toc.splitlines() if ";" not in line[:1]),
        },
        tables=table_counts(dbname),
        files=files,
        off_box=off_box_check(dest),
    )
    check_manifest_completeness(manifest)
    (dest / f"{base}.json").write_text(manifest.to_json())

    pruned = _prune(dest, keep) if keep else []
    print(json.dumps({
        "backup": str(target),
        "manifest": str(dest / f"{base}.json"),
        "bytes": manifest.dump["bytes"],
        "sha256": manifest.dump["sha256"],
        "tables": len(manifest.tables),
        "rows": sum(manifest.tables.values()),
        "encrypted": encrypt,
        "seconds": round(elapsed, 2),
        "files": files,
        "off_box": manifest.off_box,
        "pruned": pruned,
    }, indent=2))
    if manifest.off_box["same_filesystem_as_database"]:
        print(
            "WARNING: the backup directory is on the same filesystem as the database "
            "volume. That is a copy next to the original, not an off-box backup; mount "
            "another machine's disk or sync this directory off the box.",
            file=sys.stderr,
        )
    return 0


def docker_command(args: list[str]) -> list[str]:
    """``docker`` or ``sudo -n docker``, decided once by probing the socket."""
    probe = subprocess.run(["docker", "info"], capture_output=True, check=False)
    if probe.returncode == 0:
        return ["docker", *args]
    return ["sudo", "-n", "docker", *args]


def _copy_files(dest: Path, base: str) -> dict:
    """Copy the file half of the state — the vault ciphertext, never the identity.

    There is no other file-shaped state: uploaded visitor bytes are discarded
    (``visitor_files`` holds metadata only) and document payloads live in the database
    as ``document_versions.payload``, so ``pg_dump`` already covers them.
    """
    source = vault_dir()
    if source is None:
        return {"source": None, "archive": None, "count": 0}
    forbidden = set(identity_paths())
    archive = dest / f"{base}-files.tar.gz"
    members = [p for p in sorted(source.rglob("*")) if p.is_file()]
    excluded = [p for p in members if p.resolve() in forbidden]
    if excluded:
        # Not fatal, but never silent: an identity inside the backed-up directory means
        # the operator has one file that is both the archive's contents and its key.
        print(
            "WARNING: excluding key material from the files archive: "
            + ", ".join(str(p) for p in excluded)
            + ". It belongs in the operator's off-box store, not beside the ciphertext.",
            file=sys.stderr,
        )
    members = [p for p in members if p.resolve() not in forbidden]
    if not members:
        return {"source": str(source), "archive": None, "count": 0,
                "excluded_key_material": len(excluded)}
    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        # The vault is written by the api container, which runs as root, so the
        # ciphertext is root-owned with mode 600 and an unprivileged tar cannot read
        # it. Streaming to stdout rather than telling tar to write the output file
        # keeps the archive owned by whoever runs the backup.
        relative = [str(p.relative_to(source)) for p in members]
        result = subprocess.run(
            ["tar", "-czf", "-", "-C", str(source), *relative],
            capture_output=True, check=False,
        )
        privileged = False
        if result.returncode != 0:
            result = subprocess.run(
                ["sudo", "-n", "tar", "-czf", "-", "-C", str(source), *relative],
                capture_output=True, check=False,
            )
            privileged = result.returncode == 0
        if result.returncode != 0:
            raise BackupError(
                "cannot read the vault directory "
                f"{source}: {result.stderr.decode(errors='replace').strip()[:300]}. "
                "The ciphertext is root-owned (the api container writes it), so run "
                "this with an account that can read it or grant passwordless sudo tar."
            )
        tmp_path.write_bytes(result.stdout)
        shutil.move(str(tmp_path), archive)
    finally:
        tmp_path.unlink(missing_ok=True)
    return {
        "source": str(source),
        "archive": archive.name,
        "count": len(members),
        "sha256": sha256_file(archive),
        "bytes": archive.stat().st_size,
        "excluded_key_material": len(excluded),
        "read_with_privilege": privileged,
    }


def _prune(dest: Path, keep: int) -> list[str]:
    """Delete the oldest backup *sets*, only when the operator asks for a retention.

    A retention period for backups is a policy value: the DPA carries it as
    ``[[FILL: backup_retention]]`` because no number is specified anywhere. Defaulting
    to a number here would quietly become the retention promise, so pruning is opt-in
    and the count is the operator's.
    """
    if keep < 1:
        raise BackupError("--keep must be at least 1; use no --keep to keep every backup")
    stamps = sorted({_stem(p) for p in dest.glob("eva-*.json")
                     if not p.name.endswith("-drill.json")})
    doomed = stamps[: max(0, len(stamps) - keep)]
    for name in doomed:
        for suffix in (".dump", ".dump.age", "-files.tar.gz", ".json", "-drill.json"):
            (dest / f"{name}{suffix}").unlink(missing_ok=True)
    return doomed


def _stem(path: Path) -> str:
    """``eva-<stamp>`` from any file in a backup set.

    The suffixes are nested (``.dump.age``, ``-files.tar.gz``), so stripping one
    extension is wrong for two of the five names and the set would never prune together.
    """
    name = path.name
    for suffix in (".dump.age", "-files.tar.gz", "-drill.json", ".dump", ".json"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


# --- verify --------------------------------------------------------------------


def archive_toc(archive: Path, *, encrypted: bool) -> str:
    """The archive's table of contents, read from the artefact on disk.

    ``pg_restore --list`` needs a **seekable file**: the first version of this check
    piped the dump into the container and got ``did not find magic string in file
    header``, because the custom format's header cannot be read from a pipe. So the
    archive is copied into the postgres container (which is the one image with
    ``pg_restore``) and listed from there.
    """
    if encrypted:
        identity = age_identity()
        with tempfile.NamedTemporaryFile(suffix=".dump", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            with tmp_path.open("wb") as out:
                result = subprocess.run(
                    ["age", "-d", "-i", str(identity), str(archive)],
                    stdout=out, stderr=subprocess.PIPE,
                )
            if result.returncode != 0:
                raise BackupError(
                    f"cannot decrypt {archive.name}: "
                    f"{result.stderr.decode(errors='replace')[:300]}"
                )
            return _toc_of_file(tmp_path)
        finally:
            tmp_path.unlink(missing_ok=True)
    return _toc_of_file(archive)


def _toc_of_file(path: Path) -> str:
    container = postgres_container()
    staged = "/tmp/ops09-archive.dump"
    docker("cp", str(path), f"{container}:{staged}")
    try:
        return docker("compose", "exec", "-T", "postgres",
                      "pg_restore", "--list", staged).stdout
    finally:
        docker("compose", "exec", "-T", "postgres", "rm", "-f", staged, check=False)


def run_verify(dest: Path, *, name: str | None) -> int:
    manifest_path = _resolve_manifest(dest, name)
    manifest = read_manifest(manifest_path)
    check_manifest_completeness(manifest)

    artefact = dest / manifest["dump"]["name"]
    if not artefact.is_file():
        raise BackupError(f"dump named in the manifest is missing: {artefact}")
    actual_sha = sha256_file(artefact)
    if actual_sha != manifest["dump"]["sha256"]:
        raise BackupError(
            f"checksum mismatch on {artefact.name}: manifest "
            f"{manifest['dump']['sha256'][:12]}…, file {actual_sha[:12]}…"
        )
    actual_bytes = artefact.stat().st_size
    if actual_bytes != manifest["dump"]["bytes"]:
        raise BackupError(
            f"size mismatch on {artefact.name}: manifest {manifest['dump']['bytes']}, "
            f"file {actual_bytes}"
        )

    toc = archive_toc(artefact, encrypted=bool(manifest.get("encrypted")))
    missing = [t for t in REQUIRED_TABLES if f" TABLE public {t} " not in toc]
    if missing:
        raise BackupError(f"archive is unreadable or missing table(s): {', '.join(missing)}")

    files = manifest.get("files") or {}
    if files.get("archive"):
        archived = dest / files["archive"]
        if not archived.is_file():
            raise BackupError(f"files archive named in the manifest is missing: {archived}")
        if files.get("sha256") and sha256_file(archived) != files["sha256"]:
            raise BackupError(f"checksum mismatch on the files archive {archived.name}")

    print(json.dumps({
        "manifest": str(manifest_path),
        "dump": manifest["dump"]["name"],
        "tables": len(manifest["tables"]),
        "rows": sum(manifest["tables"].values()),
        "files": files.get("count", 0),
        "encrypted": bool(manifest.get("encrypted")),
        "verified": True,
    }, indent=2))
    return 0


def _resolve_manifest(dest: Path, name: str | None) -> Path:
    if name:
        candidate = dest / name
        if not candidate.is_file():
            raise BackupError(f"no such manifest: {candidate}")
        return candidate
    found = sorted(p for p in dest.glob("eva-*.json") if not p.name.endswith("-drill.json"))
    if not found:
        raise BackupError(f"no backup manifest in {dest}")
    return found[-1]


# --- drill ---------------------------------------------------------------------


def scratch_name(now: datetime | None = None) -> str:
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%S")
    return f"eva_restore_drill_{stamp}"


def assert_safe_target(target: str, live: str) -> None:
    """Refuse any target that could be the live database.

    The drill drops its scratch database when it finishes. A restore is the one
    operation here that can destroy data by being pointed at the wrong name, so the
    name is checked against the live one and must carry the drill prefix.
    """
    if target == live:
        raise BackupError(f"refusing to restore over the live database {live!r}")
    if not target.startswith("eva_restore_drill_"):
        raise BackupError(
            f"refusing to restore into {target!r}: a drill target must start with "
            "'eva_restore_drill_' so it cannot be a real database"
        )


def rls_check(target: str) -> dict:
    """Prove the restored database is usable by the role the application actually uses.

    Row counts taken as the owner prove nothing about isolation: the owner is
    ``BYPASSRLS``. This reads one tenant's conversations through ``eva_app`` inside a
    tenant scope, which requires both the grants *and* the policies to have survived —
    measured to fail when the restore drops privileges.

    A refusal is reported as ``ok: False`` with the reason, not raised: "the restored
    database denies the application role" is the finding the drill exists to produce,
    and turning it into a crash would lose the report along with the evidence.
    """
    tenant = psql(
        "SELECT tenant_id FROM conversations GROUP BY tenant_id"
        " ORDER BY count(*) DESC LIMIT 1",
        database=target,
    ) or ""
    if not tenant:
        return {"checked": False, "ok": False, "reason": "no conversations in the backup"}

    expected = int(psql(
        f"SELECT count(*) FROM conversations WHERE tenant_id = '{tenant}'", database=target
    ) or 0)
    scoped = docker(
        "compose", "exec", "-T", "postgres",
        "psql", "-U", "eva_app", "-d", target, "-tAc",
        f"SET app.tenant_id='{tenant}'; SELECT count(*) FROM conversations",
        check=False,
    )
    if scoped.returncode != 0:
        return {
            "checked": True,
            "tenant": tenant,
            "expected": expected,
            "ok": False,
            "error": (scoped.stderr or scoped.stdout).strip()[:300],
        }

    lines = scoped.stdout.strip().splitlines()
    visible = int(lines[-1]) if lines and lines[-1].strip().isdigit() else None
    unscoped = psql_unscoped(target)
    return {
        "checked": True,
        "tenant": tenant,
        "visible_to_app_role": visible,
        "expected": expected,
        "unscoped_visible_to_app_role": unscoped,
        "ok": visible == expected and expected > 0 and unscoped == 0,
    }


def psql_unscoped(target: str) -> int | None:
    """Rows the app role sees with no tenant set: must be zero, or RLS did not restore."""
    result = docker(
        "compose", "exec", "-T", "postgres",
        "psql", "-U", "eva_app", "-d", target, "-tAc", "SELECT count(*) FROM conversations",
        check=False,
    )
    out = result.stdout.strip()
    return int(out) if out.isdigit() else None


def migration_rollback_check(target: str) -> dict:
    """Time ``alembic downgrade -1`` then ``upgrade head`` against the restored copy.

    §27 asks for *"Rollback procedure tested (image + migration)"* and the migration half
    is testable here at no risk: the scratch database is a restored copy, so a downgrade
    that destroys data destroys the copy. A downgrade nobody has run is a procedure that
    may not exist — ``0001`` in particular only revokes this database's privileges
    because dropping the role would break a sibling test database, and whether that is
    reversible is exactly what this measures.
    """
    rev_before = alembic_revision(target)
    if not rev_before:
        return {"checked": False, "ok": False, "reason": "restored copy has no alembic_version"}

    started = time.monotonic()
    down = _alembic(target, "downgrade", "-1")
    down_seconds = round(time.monotonic() - started, 2)
    rev_after_down = alembic_revision(target)

    started = time.monotonic()
    up = _alembic(target, "upgrade", "head")
    up_seconds = round(time.monotonic() - started, 2)
    rev_after_up = alembic_revision(target)

    return {
        "checked": True,
        "revision_before": rev_before,
        "revision_after_downgrade": rev_after_down,
        "revision_after_upgrade": rev_after_up,
        "downgrade_seconds": down_seconds,
        "upgrade_seconds": up_seconds,
        "downgrade_output": down[-400:] if down else "",
        "upgrade_output": up[-400:] if up else "",
        # Moving one revision back and forward must land on the same revision; anything
        # else means the down migration and the up migration disagree about the schema.
        "ok": rev_before == rev_after_up and rev_after_down != rev_before,
    }


def _alembic(target: str, *args: str) -> str:
    """Run alembic in the api container against the scratch database.

    The container's ``DATABASE_URL`` points at the live database, so the target is
    passed explicitly. ``check=False`` on purpose: a failing downgrade is a finding to
    report, and raising here would lose the report.
    """
    result = docker(
        "compose", "exec", "-T", "api", "sh", "-c",
        'export DATABASE_URL="postgresql+psycopg://eva:$POSTGRES_PASSWORD@postgres:5432/'
        f'{target}"; alembic ' + " ".join(args),
        check=False, timeout=900,
    )
    return (result.stdout + result.stderr).strip()


def run_drill(dest: Path, *, name: str | None, live: str, keep_scratch: bool) -> int:
    manifest_path = _resolve_manifest(dest, name)
    manifest = read_manifest(manifest_path)
    check_manifest_completeness(manifest)

    artefact = dest / manifest["dump"]["name"]
    if not artefact.is_file():
        raise BackupError(f"dump named in the manifest is missing: {artefact}")
    if sha256_file(artefact) != manifest["dump"]["sha256"]:
        raise BackupError("checksum mismatch: the artefact is not the one the manifest records")

    target = scratch_name()
    assert_safe_target(target, live)

    # The archive is decrypted on the host (``age`` is not in the postgres image, and
    # putting it there would be a new dependency in a product container for a one-off
    # restore), then copied in and removed again. The plaintext lives only in a
    # temporary directory for the duration of the restore.
    work = f"/tmp/{target}"
    container = postgres_container()
    identity = age_identity() if manifest.get("encrypted") else None
    started = time.monotonic()
    phases: dict[str, float] = {}
    try:
        step = time.monotonic()
        psql(f'DROP DATABASE IF EXISTS "{target}" WITH (FORCE)')
        psql(f'CREATE DATABASE "{target}"')
        phases["create"] = round(time.monotonic() - step, 2)

        step = time.monotonic()
        staged = Path(tempfile.mkdtemp(prefix="ops09-drill-"))
        try:
            source = artefact
            if identity is not None:
                plain = staged / "archive.dump"
                decrypt = subprocess.run(
                    ["age", "-d", "-i", str(identity), "-o", str(plain), str(artefact)],
                    capture_output=True, check=False,
                )
                if decrypt.returncode != 0:
                    raise BackupError(
                        "cannot decrypt the archive: "
                        f"{decrypt.stderr.decode(errors='replace').strip()[:300]}"
                    )
                source = plain
            docker("cp", str(source), f"{container}:{work}.dump")
        finally:
            shutil.rmtree(staged, ignore_errors=True)
        phases["stage"] = round(time.monotonic() - step, 2)

        step = time.monotonic()
        restore = docker(
            "compose", "exec", "-T", "postgres",
            "sh", "-c",
            f"pg_restore --no-owner -U eva -d {target} {work}.dump",
            check=False,
        )
        phases["restore"] = round(time.monotonic() - step, 2)
        if restore.returncode != 0:
            # pg_restore reports non-fatal errors with a non-zero status, so the
            # decision is made on the comparison below, not on this code. It is
            # recorded either way rather than swallowed.
            phases["restore_exit"] = float(restore.returncode)

        step = time.monotonic()
        actual = table_counts(target)
        problems = compare_counts(manifest["tables"], actual)
        phases["compare"] = round(time.monotonic() - step, 2)

        step = time.monotonic()
        revision = alembic_revision(target)
        rls = rls_check(target)
        phases["integrity"] = round(time.monotonic() - step, 2)

        step = time.monotonic()
        rollback = migration_rollback_check(target) if not problems else {
            "checked": False, "ok": False, "reason": "skipped: row counts already mismatch"
        }
        phases["rollback"] = round(time.monotonic() - step, 2)

        elapsed = time.monotonic() - started
        report = {
            "manifest": str(manifest_path),
            "backup_created_at": manifest["created_at"],
            "scratch_database": target,
            "seconds": round(elapsed, 2),
            "phases_seconds": phases,
            "tables_compared": len(actual),
            "rows_restored": sum(actual.values()),
            "revision_expected": manifest.get("alembic_revision"),
            "revision_restored": revision,
            "rls": rls,
            "migration_rollback": rollback,
            "mismatches": problems,
            "salient": {t: actual.get(t) for t in SALIENT_TABLES},
            "ok": not problems and rls.get("ok", False)
            and rollback.get("ok", False)
            and revision == manifest.get("alembic_revision"),
        }
        (dest / f"{manifest_path.stem}-drill.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n"
        )
        print(json.dumps(report, indent=2))
        return 0 if report["ok"] else 1
    finally:
        if not keep_scratch:
            docker("compose", "exec", "-T", "postgres",
                   "sh", "-c",
                   f"rm -f {work}.dump {work}.plain {work}.key", check=False)
            try:
                psql(f'DROP DATABASE IF EXISTS "{target}" WITH (FORCE)')
            except BackupError as exc:  # never mask the drill's own verdict
                print(f"WARNING: could not drop {target}: {exc}", file=sys.stderr)


# --- cli -----------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=("backup", "verify", "drill"))
    parser.add_argument("--dest", default=None, help="backup directory (OPS09_BACKUP_DIR)")
    parser.add_argument("--dbname", default=os.environ.get("POSTGRES_DB", "eva"))
    parser.add_argument("--name", default=None, help="manifest name; default is the newest")
    parser.add_argument("--no-encrypt", action="store_true",
                        help="write a plaintext dump (local drill only)")
    parser.add_argument("--keep", type=int, default=None,
                        help="keep only the N newest backup sets (default: keep all)")
    parser.add_argument("--keep-scratch", action="store_true",
                        help="leave the restored database in place for inspection")
    parser.add_argument("--live", default=os.environ.get("POSTGRES_DB", "eva"),
                        help="name of the live database the drill must never touch")
    args = parser.parse_args(argv)

    dest = backup_dir(args.dest)
    try:
        if args.action == "backup":
            return run_backup(dest, args.dbname, encrypt=not args.no_encrypt, keep=args.keep)
        if args.action == "verify":
            return run_verify(dest, name=args.name)
        return run_drill(dest, name=args.name, live=args.live, keep_scratch=args.keep_scratch)
    except BackupError as exc:
        print(f"ops09_backup: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
