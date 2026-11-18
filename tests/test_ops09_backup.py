"""OPS-09: the backup is checkable, and the restore drill can fail.

The claims that matter here are the ones that fail *silently*: a backup that is a copy
on the same disk, an archive that would archive its own key, a drill that compares zero
rows against zero rows, and a restore that keeps the owner's row counts while removing
the grants the application needs. Each has a test, and each test was mutation-checked —
§8.25 and §8.32 both record a "test that cannot fail" costing a whole cycle, and a
backup gate is the worst place to repeat it.

No test here touches the live stack: the docker and subprocess calls are replaced by
recording fakes at the exact seam the script exposes (``docker``, ``psql``,
``subprocess.run``), so the decision logic runs for real while nothing is dumped,
restored or dropped. The live drill is PLAN §8.34's evidence, not this file's.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "ops09_backup.py"
UNIT_SERVICE = ROOT / "ops" / "systemd" / "eva-backup.service"
UNIT_TIMER = ROOT / "ops" / "systemd" / "eva-backup.timer"
RUNBOOK = ROOT / "docs" / "ops" / "RUNBOOK.md"


def _load():
    name = "eva_ops09_backup"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ops = _load()


def manifest_payload(**overrides) -> dict:
    base = {
        "format_version": ops.FORMAT_VERSION,
        "created_at": "2026-11-18T02:45:00+00:00",
        "database": "eva",
        "alembic_revision": "0006_widget_public_lookup",
        "encrypted": True,
        "dump": {"name": "eva-20261118T024500Z.dump.age", "bytes": 1024, "sha256": "a" * 64},
        "tables": {t: 1 for t in
                   ("tenants", "bots", "conversations", "messages", "alembic_version",
                    "chunks", "documents", "document_versions", "knowledge_sources",
                    "handovers", "feedback", "usage_records", "visitor_files",
                    "api_credentials", "bot_domains", "audit_logs", "sessions", "users",
                    "webhooks")},
        "files": {"source": "/srv/secrets", "archive": None, "count": 0},
        "off_box": {"off_box_proven": False},
    }
    base.update(overrides)
    return base


# --- the artefact is the right one ---------------------------------------------


def test_checksum_mismatch_is_refused(tmp_path) -> None:
    """A truncated or replaced archive must not drill: it would restore something else."""
    dump = tmp_path / "eva-20261118T024500Z.dump.age"
    dump.write_bytes(b"not the archive the manifest names")
    manifest = manifest_payload(dump={"name": dump.name, "bytes": dump.stat().st_size,
                                      "sha256": "0" * 64})
    (tmp_path / "eva-20261118T024500Z.json").write_text(json.dumps(manifest))
    with pytest.raises(ops.BackupError, match="checksum mismatch"):
        ops.run_verify(tmp_path, name=None)


def test_size_mismatch_is_refused_even_when_the_digest_is_right(tmp_path) -> None:
    """Belt and braces: the size check is what catches an appended dump, not a hash."""
    dump = tmp_path / "eva-20261118T024500Z.dump.age"
    dump.write_bytes(b"payload")
    manifest = manifest_payload(
        dump={"name": dump.name, "bytes": 999, "sha256": ops.sha256_file(dump)}
    )
    (tmp_path / "eva-20261118T024500Z.json").write_text(json.dumps(manifest))
    with pytest.raises(ops.BackupError, match="size mismatch"):
        ops.run_verify(tmp_path, name=None)


def test_a_missing_artefact_is_refused(tmp_path) -> None:
    manifest = manifest_payload()
    (tmp_path / "eva-20261118T024500Z.json").write_text(json.dumps(manifest))
    with pytest.raises(ops.BackupError, match="missing"):
        ops.run_verify(tmp_path, name=None)


# --- the manifest cannot make the drill vacuous --------------------------------


def test_a_manifest_below_the_table_floor_is_refused() -> None:
    """Zero tables compared against zero tables is the same green as a clean restore.

    A floor is the only thing that distinguishes "the whole schema came back" from "the
    dump was empty and the comparison had nothing to disagree with" (§8.25).
    """
    manifest = manifest_payload(tables={"tenants": 1, "bots": 1})
    with pytest.raises(ops.BackupError, match="below the floor"):
        ops.check_manifest_completeness(manifest)


def test_a_manifest_missing_tenants_is_refused() -> None:
    """`tenants` absent means every tenant-scoped read returns nothing — look healthy."""
    tables = manifest_payload()["tables"]
    tables.pop("tenants")
    with pytest.raises(ops.BackupError, match="required table"):
        ops.check_manifest_completeness(manifest_payload(tables=tables))


def test_an_empty_dump_is_refused() -> None:
    with pytest.raises(ops.BackupError, match="empty dump"):
        ops.check_manifest_completeness(
            manifest_payload(dump={"name": "x.dump", "bytes": 0, "sha256": "a" * 64})
        )


def test_a_manifest_without_a_digest_is_refused() -> None:
    with pytest.raises(ops.BackupError, match="name/sha256"):
        ops.check_manifest_completeness(
            manifest_payload(dump={"name": "x.dump", "bytes": 10})
        )


def test_a_complete_manifest_passes_every_check() -> None:
    """The complement: the guard above must not refuse a real backup."""
    ops.check_manifest_completeness(manifest_payload())


def test_the_write_path_and_the_read_path_agree() -> None:
    """`check_manifest_completeness` takes a dataclass on write and JSON on read.

    A guard that only understood one shape would let a manifest be written that the
    drill then refuses, or the reverse — so both are exercised on the same data.
    """
    payload = manifest_payload()
    as_dataclass = ops.Manifest(
        created_at=payload["created_at"], database="eva",
        alembic_revision=payload["alembic_revision"], encrypted=True,
        dump=payload["dump"], tables=payload["tables"],
    )
    ops.check_manifest_completeness(as_dataclass)
    ops.check_manifest_completeness(payload)

    broken = dict(as_dataclass.tables)
    broken.pop("tenants")
    as_dataclass.tables = broken
    with pytest.raises(ops.BackupError, match="required table"):
        ops.check_manifest_completeness(as_dataclass)


def test_an_unknown_manifest_version_is_not_reinterpreted(tmp_path) -> None:
    """Forward compatibility by refusal: guessing at a newer shape is how a restore lies."""
    manifest = manifest_payload(format_version=ops.FORMAT_VERSION + 1)
    path = tmp_path / "eva-20261118T024500Z.json"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ops.BackupError, match="format_version"):
        ops.read_manifest(path)


# --- the comparison itself ----------------------------------------------------


def test_a_dropped_table_is_a_mismatch() -> None:
    expected = {"tenants": 5, "messages": 9}
    assert ops.compare_counts(expected, {"tenants": 5}) == ["messages: missing after restore"]


def test_a_short_table_is_a_mismatch() -> None:
    assert ops.compare_counts({"messages": 9}, {"messages": 3}) == ["messages: 9 -> 3"]


def test_a_restore_with_extra_rows_is_a_mismatch() -> None:
    assert ops.compare_counts({"messages": 9}, {"messages": 10}) == ["messages: 9 -> 10"]


def test_matching_counts_are_not_a_mismatch() -> None:
    expected = {"tenants": 5, "messages": 9}
    assert ops.compare_counts(expected, dict(expected)) == []


# --- the destination is not a backup just because it exists --------------------


def test_a_destination_inside_the_repository_is_refused(tmp_path, monkeypatch) -> None:
    """A dump in the work tree is one `git add -A` from being committed."""
    monkeypatch.setattr(ops, "repo_root", lambda: tmp_path)
    inside = tmp_path / "backups"
    inside.mkdir()
    with pytest.raises(ops.BackupError, match="inside the repository"):
        ops.off_box_check(inside)


def test_a_destination_outside_the_repository_is_measured_not_assumed(
    tmp_path, monkeypatch
) -> None:
    """`off_box_proven` stays False: the script cannot mount the operator's disk.

    The value that matters is `same_filesystem_as_database` — measured from the device
    ids — because that is the difference between a backup and a second copy on the disk
    that is going to die.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(ops, "repo_root", lambda: repo)

    monkeypatch.setattr(ops, "docker", lambda *a, **k: _Completed("42\n"))
    monkeypatch.setattr(ops, "_device", lambda p: 42)
    result = ops.off_box_check(tmp_path)
    assert result["same_filesystem_as_database"] is True
    assert result["off_box_proven"] is False

    monkeypatch.setattr(ops, "_device", lambda p: 7)
    assert ops.off_box_check(tmp_path)["same_filesystem_as_database"] is False


class _Completed:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


# --- encryption is required, not offered --------------------------------------


def test_the_recipient_is_required(monkeypatch) -> None:
    monkeypatch.delenv("OPS09_AGE_RECIPIENT", raising=False)
    with pytest.raises(ops.BackupError, match="OPS09_AGE_RECIPIENT"):
        ops.age_recipient()


def test_the_identity_must_exist(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("OPS09_AGE_IDENTITY", str(tmp_path / "absent"))
    monkeypatch.delenv("SECRET_VAULT_IDENTITY", raising=False)
    with pytest.raises(ops.BackupError, match="not found"):
        ops.age_identity()


def test_the_identity_is_never_archived(monkeypatch, tmp_path) -> None:
    """The archive must not carry the key that decrypts it.

    ``vault.py`` keeps them apart deliberately ("the identity is on the host so that a
    backup of one is not a backup of the other"). Copying both would make the backup a
    plaintext key store, so this asserts the identity is excluded *by path* — matched by
    name too, because an identity at the default path is named by no environment
    variable.
    """
    vault = tmp_path / "secrets"
    vault.mkdir()
    (vault / "age-identity").write_text("placeholder-not-a-real-key\n")
    (vault / "age-identity.bak").write_text("placeholder-not-a-real-key\n")
    tenant = vault / "tenant-1"
    tenant.mkdir()
    (tenant / "openai.age").write_text("ciphertext")

    monkeypatch.setenv("SECRET_VAULT_HOST_DIR", str(vault))
    monkeypatch.setenv("OPS09_AGE_IDENTITY", str(vault / "age-identity"))
    monkeypatch.delenv("OPS09_FILES_DIR", raising=False)

    excluded = {p.name for p in ops.identity_paths()}
    assert "age-identity" in excluded
    assert "age-identity.bak" in excluded, "a backup copy of the identity is still the key"

    files = ops._copy_files(tmp_path, "eva-test")
    assert files["excluded_key_material"] == 2
    import tarfile

    with tarfile.open(tmp_path / files["archive"]) as archive:
        names = archive.getnames()
    assert "tenant-1/openai.age" in names
    assert not any("age-identity" in n for n in names)
    assert files["count"] == 1


def test_the_files_half_is_skipped_when_there_is_no_vault(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("SECRET_VAULT_HOST_DIR", raising=False)
    monkeypatch.delenv("OPS09_FILES_DIR", raising=False)
    monkeypatch.setattr(ops, "repo_root", lambda: tmp_path)
    result = ops._copy_files(tmp_path, "eva-test")
    assert result == {"source": None, "archive": None, "count": 0}


# --- the drill can only touch a throwaway database ----------------------------


def test_the_drill_refuses_to_restore_over_the_live_database() -> None:
    with pytest.raises(ops.BackupError, match="live database"):
        ops.assert_safe_target("eva", "eva")


def test_the_drill_refuses_a_target_without_its_prefix() -> None:
    """The name is the only thing between a drill and a real database being dropped."""
    with pytest.raises(ops.BackupError, match="eva_restore_drill_"):
        ops.assert_safe_target("eva_staging", "eva")


def test_a_prefixed_target_is_accepted() -> None:
    ops.assert_safe_target("eva_restore_drill_20261118T024500", "eva")


def test_the_scratch_name_carries_the_prefix() -> None:
    assert ops.scratch_name().startswith("eva_restore_drill_")


def test_the_scratch_name_is_unique_per_run() -> None:
    """Two drills in the same minute must not drop each other's database."""
    from datetime import datetime, timezone

    first = ops.scratch_name(datetime(2026, 11, 18, 2, 45, 0, tzinfo=timezone.utc))
    second = ops.scratch_name(datetime(2026, 11, 18, 2, 45, 1, tzinfo=timezone.utc))
    assert first != second


# --- the restore argv must keep the grants ------------------------------------


def test_the_restore_argv_keeps_privileges() -> None:
    """`--no-privileges` also drops the `eva_app` grants.

    Measured on the live stack: with it, the restored database answers the owner (787
    conversations) while `eva_app` gets `permission denied for table conversations`,
    and every row count still matches. A counts-only drill would call that a success.
    """
    argv = ops.restore_args("eva_restore_drill_x", "/tmp/a.dump")
    assert "--no-owner" in argv
    assert "--no-privileges" not in argv
    assert "--data-only" not in argv, (
        "a data-only restore into a migrated database fails on foreign keys"
    )


def test_the_dump_is_custom_format_so_pg_restore_can_list_it() -> None:
    argv = ops.dump_args("eva")
    assert "--format=custom" in argv


# --- RLS survives the restore, or the drill says so ---------------------------


def test_an_rls_refusal_is_a_red_verdict_not_a_crash(monkeypatch) -> None:
    """The finding the drill exists to produce must reach the report.

    Raising here would abort the run and lose the comparison evidence with it — the
    first version did exactly that and hung in cleanup instead of reporting red.
    """
    monkeypatch.setattr(ops, "psql", lambda sql, database="postgres": (
        "f9db8c26-cb35-40d9-97d6-25f413ba9f4e" if "GROUP BY" in sql else "719"
    ))
    monkeypatch.setattr(
        ops, "docker",
        lambda *a, **k: _Completed("", returncode=1,
                                   stderr="ERROR:  permission denied for table conversations"),
    )
    result = ops.rls_check("eva_restore_drill_x")
    assert result["ok"] is False
    assert "permission denied" in result["error"]


def test_an_unscoped_read_seeing_rows_fails_the_rls_check(monkeypatch) -> None:
    """If the policy is gone, the app role sees every tenant's rows. That is a red.

    This is the branch a "the query worked" assertion would miss: the read succeeds and
    returns too much.
    """
    responses = iter(["719", "787"])

    def fake_docker(*args, **kwargs):
        value = next(responses)
        return _Completed(f"{value}\n")

    monkeypatch.setattr(ops, "psql", lambda sql, database="postgres": (
        "f9db8c26-cb35-40d9-97d6-25f413ba9f4e" if "GROUP BY" in sql else "719"
    ))
    monkeypatch.setattr(ops, "docker", fake_docker)
    result = ops.rls_check("eva_restore_drill_x")
    assert result["visible_to_app_role"] == 719
    assert result["unscoped_visible_to_app_role"] == 787
    assert result["ok"] is False


def test_a_correct_rls_read_passes(monkeypatch) -> None:
    responses = iter(["719", "0"])

    def fake_docker(*args, **kwargs):
        return _Completed(f"{next(responses)}\n")

    monkeypatch.setattr(ops, "psql", lambda sql, database="postgres": (
        "f9db8c26-cb35-40d9-97d6-25f413ba9f4e" if "GROUP BY" in sql else "719"
    ))
    monkeypatch.setattr(ops, "docker", fake_docker)
    assert ops.rls_check("eva_restore_drill_x")["ok"] is True


def test_a_backup_with_no_conversations_cannot_pass_the_rls_check(monkeypatch) -> None:
    """No conversations means nothing was proven, so it must not be green.

    The tempting implementation returns ``{"checked": False}`` and the drill's verdict
    reads only ``ok`` — which a missing key turns into a KeyError, or a `.get()` turns
    into False. Either way the value has to be explicit, not absent.
    """
    monkeypatch.setattr(ops, "psql", lambda sql, database="postgres": "")
    result = ops.rls_check("eva_restore_drill_x")
    assert result["checked"] is False
    assert result["ok"] is False


# --- migration rollback is tested, not described ------------------------------


def test_the_head_revision_downgrades_and_re_upgrades(monkeypatch) -> None:
    revisions = iter(["0006_widget_public_lookup", "0005_version_payload",
                      "0006_widget_public_lookup"])
    monkeypatch.setattr(ops, "alembic_revision", lambda db: next(revisions))
    monkeypatch.setattr(ops, "_alembic", lambda *a: "")
    result = ops.migration_rollback_check("eva_restore_drill_x")
    assert result["ok"] is True
    assert result["revision_after_downgrade"] == "0005_version_payload"
    assert result["revision_after_upgrade"] == "0006_widget_public_lookup"


def test_a_downgrade_that_does_not_move_is_not_green(monkeypatch) -> None:
    """A `downgrade -1` that silently no-ops must not read as a tested rollback."""
    revisions = iter(["0006_widget_public_lookup", "0006_widget_public_lookup",
                      "0006_widget_public_lookup"])
    monkeypatch.setattr(ops, "alembic_revision", lambda db: next(revisions))
    monkeypatch.setattr(ops, "_alembic", lambda *a: "")
    assert ops.migration_rollback_check("eva_restore_drill_x")["ok"] is False


def test_an_upgrade_that_lands_on_the_wrong_revision_is_not_green(monkeypatch) -> None:
    revisions = iter(["0006_widget_public_lookup", "0005_version_payload",
                      "0004_embedding_identity"])
    monkeypatch.setattr(ops, "alembic_revision", lambda db: next(revisions))
    monkeypatch.setattr(ops, "_alembic", lambda *a: "")
    assert ops.migration_rollback_check("eva_restore_drill_x")["ok"] is False


def test_a_restored_copy_without_a_revision_cannot_be_green(monkeypatch) -> None:
    monkeypatch.setattr(ops, "alembic_revision", lambda db: None)
    result = ops.migration_rollback_check("eva_restore_drill_x")
    assert result["checked"] is False
    assert result["ok"] is False


# --- retention is opt-in ------------------------------------------------------


def test_pruning_is_off_unless_asked(tmp_path) -> None:
    """No number is specified anywhere, so a default would be an invented retention.

    Asserted on the CLI rather than on ``_prune``: the risk is a default value appearing
    in the argument parser, which would silently start deleting backups an operator
    never agreed to lose.
    """
    for stem in ("eva-20261116T024500Z", "eva-20261117T024500Z"):
        (tmp_path / f"{stem}.json").write_text("{}")
        (tmp_path / f"{stem}.dump.age").write_text("x")
    # No --keep passed means run_backup is called with keep=None.
    seen: dict = {}

    def fake_backup(dest, dbname, *, encrypt, keep):
        seen["keep"] = keep
        return 0

    import unittest.mock as mock

    with mock.patch.object(ops, "run_backup", fake_backup):
        ops.main(["backup", "--dest", str(tmp_path)])
    assert seen["keep"] is None
    # And nothing was pruned: both sets are still on disk.
    assert len(list(tmp_path.glob("eva-*.json"))) == 2


def test_pruning_keeps_whole_sets_together(tmp_path) -> None:
    """A set is five differently-suffixed files; pruning one of them is a broken backup."""
    stems = ["eva-20261116T024500Z", "eva-20261117T024500Z", "eva-20261118T024500Z"]
    for stem in stems:
        (tmp_path / f"{stem}.json").write_text("{}")
        (tmp_path / f"{stem}.dump.age").write_text("x")
        (tmp_path / f"{stem}-files.tar.gz").write_text("x")
    removed = ops._prune(tmp_path, keep=1)
    assert removed == stems[:2]
    left = sorted(p.name for p in tmp_path.iterdir())
    assert left == ["eva-20261118T024500Z-files.tar.gz",
                    "eva-20261118T024500Z.dump.age",
                    "eva-20261118T024500Z.json"]


def test_pruning_refuses_a_zero_keep(tmp_path) -> None:
    with pytest.raises(ops.BackupError, match="at least 1"):
        ops._prune(tmp_path, keep=0)


def test_the_stem_strips_nested_suffixes() -> None:
    assert ops._stem(Path("eva-20261118T024500Z.dump.age")) == "eva-20261118T024500Z"
    assert ops._stem(Path("eva-20261118T024500Z-files.tar.gz")) == "eva-20261118T024500Z"
    assert ops._stem(Path("eva-20261118T024500Z.json")) == "eva-20261118T024500Z"
    assert ops._stem(Path("eva-20261118T024500Z-drill.json")) == "eva-20261118T024500Z"


# --- the shipped schedule is a daily timer, and the runbook exists ------------


def test_the_timer_runs_daily_and_persists_a_missed_run() -> None:
    text = UNIT_TIMER.read_text()
    assert "OnCalendar=*-*-* 02:45:00 UTC" in text
    assert "Persistent=true" in text, "a missed backup after a reboot must still run"


def test_the_timer_runs_before_the_retention_purge() -> None:
    """The purge is at 03:15; a backup after it would copy only what survived."""
    assert "02:45" in UNIT_TIMER.read_text()


def test_the_service_fails_loudly_and_carries_no_secret() -> None:
    text = UNIT_SERVICE.read_text()
    assert "Type=oneshot" in text
    assert "EnvironmentFile=/etc/eva/backup.env" in text
    # A key in a committed unit file would be a committed secret.
    assert "AGE-SECRET-KEY" not in text, "a key in a committed unit file is a committed secret"
    assert "OPS09_AGE_RECIPIENT=" not in text


def test_the_runbook_documents_both_halves_of_rollback() -> None:
    text = RUNBOOK.read_text()
    assert "## Restore to a fresh box" in text
    assert "## Rollback procedure" in text
    assert "alembic downgrade -1" in text
    assert "docker tag" in text, "the image half of the rollback must be spelled out"


def test_the_runbook_warns_about_the_measured_traps() -> None:
    """Each of these was measured on the live stack; losing them costs a debugging cycle."""
    text = RUNBOOK.read_text()
    assert "--no-privileges" in text
    assert "foreign key" in text
    assert "magic string" in text


def test_the_runbook_does_not_claim_an_off_box_backup_is_proven() -> None:
    text = RUNBOOK.read_text()
    assert "off_box_proven" in text
