"""FUT-04: one codebase deploys onto a dedicated database (§16.3, R13).

§16.3 promises that single-tenant means *"same schema, dedicated database"*. The claim
is not "the migrations exist" — it is that running them somewhere the shared database
does not exist produces the same deployment, so an enterprise customer's install needs a
``POSTGRES_DB`` value and no code fork.

Four properties, each of which fails silently if untested:

* **The comparison can fail.** Two empty databases agree perfectly, so ``compare`` is
  tested against empty inventories (§8.25 / §8.30 / §8.32 all record a "test that cannot
  fail" costing a cycle). The floors are what make the equality assertions meaningful.
* **A policy is compared by its predicate, not its name.** Two databases can each hold 17
  policies and enforce different rules; comparing names alone would call that identical,
  and the lost-policy failure is a query that *succeeds and returns too much*.
* **The deployment configuration follows the database name.** This is the defect the row
  actually found: ``worker`` pinned ``/eva`` while ``api`` used ``${POSTGRES_DB}``, so a
  dedicated deployment had its API and its ingestion worker pointed at different
  databases. A test that only ran the migrations would have missed it entirely.
* **The shared-role guard refuses rather than rotates.** Migration ``0001`` sets
  ``eva_app``'s password on every upgrade, and a role is cluster-wide while a database is
  not. On a shared cluster that rewrites the credential another deployment is using —
  measured during this work, on this cluster. The guard is asserted against the real
  cluster in both directions.

One test runs the real thing end to end: two scratch databases, both migrated with the
repository's own ``alembic upgrade head``, compared and dropped. Everything else is a
decision test. No live database is touched, and every scratch database this module
creates is named ``eva_fut04_*`` and removed in the same test.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

os.environ.setdefault("ARGON2_MEMORY_KIB", "32")
os.environ.setdefault("ARGON2_TIME_COST", "1")
os.environ.setdefault("ARGON2_PARALLELISM", "1")

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "fut04_single_tenant.py"
COMPOSE = ROOT / "compose.yaml"


def _load():
    name = "eva_fut04_single_tenant"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


fut = _load()


def empty_inventory() -> dict:
    return {key: [] for key in fut.FLOORS}


def inventory_with(**overrides) -> dict:
    """A full-shape inventory so a single mutation is the only difference."""
    base = {
        "tables": [f"t{i}" for i in range(fut.FLOORS["tables"])],
        "columns": [(f"t{i}", "c", "text", "YES") for i in range(fut.FLOORS["columns"])],
        "policies": [(f"t{i}", f"p{i}", "tenant_id = 1", "tenant_id = 1")
                     for i in range(fut.FLOORS["policies"])],
        "grants": [(f"t{i}", True, True, True, True) for i in range(fut.FLOORS["grants"])],
        "indexes": [(f"t{i}", f"ix{i}") for i in range(fut.FLOORS["indexes"])],
        "constraints": [(f"t{i}", f"ck{i}") for i in range(fut.FLOORS["constraints"])],
        "functions": [f"f{i}" for i in range(fut.FLOORS["functions"])],
        "extensions": ["citext", "plpgsql", "vector"],
        "triggers": [("audit_logs_no_update", "audit_logs")],
        "rls": [(f"t{i}", True, True) for i in range(fut.FLOORS["rls"])],
    }
    base.update(overrides)
    return base


# --- the comparison must be able to fail ----------------------------------------


def test_two_empty_databases_are_not_reported_as_identical() -> None:
    """The floor is the whole reason an equality check is evidence."""
    result = fut.compare(empty_inventory(), empty_inventory())
    assert result["differences"], "empty == empty must not be a pass"
    kinds = {d["kind"] for d in result["differences"]}
    assert kinds == {"below_floor"}


def test_identical_full_inventories_compare_clean() -> None:
    """The other direction, or the floor test above proves only that it always fails."""
    assert fut.compare(inventory_with(), inventory_with())["differences"] == []


def test_a_missing_table_is_named() -> None:
    left = inventory_with()
    right = inventory_with(tables=left["tables"][:-1])
    differences = fut.compare(left, right)["differences"]
    assert any(d["kind"] == "missing" and d["fact"] == "tables" for d in differences), differences


def test_a_policy_with_the_same_name_and_a_different_predicate_is_a_difference() -> None:
    """The failure mode: 17 policies on each side, different rules, names identical."""
    left = inventory_with()
    weakened = [("t0", "p0", "true", "true")] + left["policies"][1:]
    differences = fut.compare(left, inventory_with(policies=weakened))["differences"]
    assert any(d["kind"] == "missing" and d["fact"] == "policies" for d in differences), differences
    assert any(d["kind"] == "extra" and d["fact"] == "policies" for d in differences), differences


def test_a_lost_grant_is_a_difference_though_every_row_count_matches() -> None:
    """OPS-09 §8.34's trap: `--no-privileges` keeps counts and removes the grants."""
    left = inventory_with()
    revoked = [(left["grants"][0][0], True, True, False, False)] + left["grants"][1:]
    differences = fut.compare(left, inventory_with(grants=revoked))["differences"]
    assert any(d["fact"] == "grants" for d in differences), differences


def test_rls_disabled_on_one_table_is_a_difference() -> None:
    left = inventory_with()
    disabled = [(left["rls"][0][0], False, False)] + left["rls"][1:]
    differences = fut.compare(left, inventory_with(rls=disabled))["differences"]
    assert any(d["fact"] == "rls" for d in differences), differences


def test_the_inventory_reads_the_policy_predicate_not_just_its_name(
    migrated_db: str,
) -> None:
    """The query behind ``compare``, asserted on a real database.

    The test above proves ``compare`` treats predicates as significant; without this
    one, the SQL could still be selecting ``'' AS qual`` and every comparison would be
    a name comparison that passes. Mutation-checked: replacing the policy query's
    ``qual, with_check`` columns with empty literals leaves the rest of this file green.
    """
    facts = fut.inventory(migrated_db)
    predicates = {row[2] for row in facts["policies"]}
    assert predicates != {""}, "policies were read without their predicate"
    assert any("app.tenant_id" in (p or "") for p in predicates), predicates
    assert all(row[3] is not None for row in facts["policies"]), (
        "a policy with no WITH CHECK would let a row be written into the wrong tenant"
    )

    grants = {row[0]: row[1:] for row in facts["grants"]}
    assert grants["audit_logs"] == (True, True, False, False), (
        "audit_logs must stay append-only for eva_app (R29)"
    )
    assert grants["conversations"] == (True, True, True, True)


# --- the deployment configuration follows POSTGRES_DB ---------------------------


def test_the_shipped_compose_has_no_hardcoded_database_name() -> None:
    """The defect this row found: worker pinned /eva while api followed POSTGRES_DB."""
    assert fut.compose_dsn_problems(COMPOSE) == []


def test_a_hardcoded_database_name_is_caught(tmp_path: Path) -> None:
    """The exact historical line, reproduced, so the check above cannot be vacuous."""
    fixture = tmp_path / "compose.yaml"
    fixture.write_text(
        "services:\n"
        "  api:\n"
        "    environment:\n"
        "      APP_DATABASE_URL: postgresql+psycopg://eva_app:pw@postgres:5432/${POSTGRES_DB:-eva}\n"
        "      DATABASE_URL: postgresql+psycopg://eva:pw@postgres:5432/${POSTGRES_DB:-eva}\n"
        "  worker:\n"
        "    environment:\n"
        "      APP_DATABASE_URL: postgresql+psycopg://eva_app:pw@postgres:5432/eva\n"
    )
    problems = fut.compose_dsn_problems(fixture)
    assert [p["kind"] for p in problems] == ["hardcoded_database"], problems
    assert problems[0]["database"] == "eva"


def test_the_worker_dsn_is_the_one_that_was_wrong() -> None:
    """Pinned to the service, not just the file: this is where the bug lived."""
    text = COMPOSE.read_text()
    worker = text.split("  worker:", 1)[1]
    assert "${POSTGRES_DB" in worker.split("EMBEDDING_MODEL")[0], (
        "the worker's APP_DATABASE_URL must follow POSTGRES_DB, or a dedicated "
        "deployment ingests into a different database than the API reads"
    )


def test_a_compose_with_no_dsn_at_all_is_not_a_pass(tmp_path: Path) -> None:
    """Zero findings from zero input is the same green as a clean file."""
    fixture = tmp_path / "compose.yaml"
    fixture.write_text("services:\n  api:\n    environment:\n      REDIS_URL: x\n")
    problems = fut.compose_dsn_problems(fixture)
    assert [p["kind"] for p in problems] == ["below_floor"]


# --- the scratch-database guard -------------------------------------------------


def test_only_scratch_databases_may_be_dropped() -> None:
    """A cleanup routine that drops what it is told is one typo from the live database."""
    for name in ("eva", "postgres", "eva_test", "eva_fut04x"):
        with pytest.raises(fut.ReadinessError):
            fut.check_scratch_name(name)
    fut.check_scratch_name("eva_fut04_abcdef123456")


def test_scratch_names_are_unique() -> None:
    names = {fut.scratch_name() for _ in range(50)}
    assert len(names) == 50
    assert all(n.startswith(fut.SCRATCH_PREFIX) for n in names)


# --- the shared-role guard ------------------------------------------------------


@pytest.fixture
def app_password() -> str:
    """The password the runtime role actually has on this cluster.

    ``conftest`` migrates a test database with ``TEST_APP_DB_PASSWORD`` (default
    ``appsecret``), and migration ``0001`` sets the cluster-wide role to that value, so
    that is the credential the guard is expected to accept.
    """
    return os.environ.get("TEST_APP_DB_PASSWORD", "appsecret")


def test_the_guard_allows_a_cluster_whose_role_already_matches(app_password: str) -> None:
    """The legitimate case: the role exists and the configured password is its own."""
    os.environ["EVA_APP_DB_PASSWORD"] = app_password
    result = fut.role_guard()
    assert result["role"] == "eva_app"
    assert result["preexisting"] is True
    assert result["authenticates"] is True


def test_the_guard_refuses_a_password_that_would_rotate_a_shared_role(
    app_password: str,
) -> None:
    """Measured on this cluster: migrating a second database rewrote eva_app's password
    and the running deployment's logins stopped working."""
    os.environ["EVA_APP_DB_PASSWORD"] = "not-the-roles-password"
    with pytest.raises(fut.ReadinessError) as caught:
        fut.role_guard()
    message = str(caught.value)
    assert "cluster-wide" in message, message
    assert "EVA_APP_DB_PASSWORD" in message, message


def test_the_guard_allows_a_cluster_where_the_role_does_not_exist_yet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The enterprise path: a fresh cluster, where CREATE ROLE is the correct outcome.

    Exercised against the real cluster by pointing the guard at a role name that cannot
    exist, rather than by creating and dropping a role.
    """
    monkeypatch.setattr(fut, "APP_ROLE", "eva_fut04_role_absent_probe")
    result = fut.role_guard()
    assert result["preexisting"] is False
    assert result["authenticates"] is False


def test_a_missing_app_password_is_a_refusal_not_a_default() -> None:
    """The migration requires it; a guard that invented one would hide the misconfig."""
    os.environ.pop("EVA_APP_DB_PASSWORD", None)
    with pytest.raises(fut.ReadinessError, match="EVA_APP_DB_PASSWORD"):
        fut.app_credentials()


# --- the real thing -------------------------------------------------------------


@pytest.fixture
def env_for_check(app_password: str) -> None:
    os.environ["EVA_APP_DB_PASSWORD"] = app_password
    os.environ.setdefault("TEST_PG_HOST", "127.0.0.1")
    os.environ.setdefault("TEST_PG_USER", "eva")
    os.environ.setdefault("TEST_PG_PASSWORD", "changeme")


def test_a_dedicated_database_is_built_identically_by_the_same_migrations(
    env_for_check: None, capsys: pytest.CaptureFixture[str],
) -> None:
    """End to end: two scratch databases, the repository's own migrations, compared.

    This is the row's claim executed rather than described. It is deliberately not
    mocked — a fake migration would prove the test, not the deployment.
    """
    import json

    exit_code = fut.run_check(keep=False)
    report = json.loads(capsys.readouterr().out)

    assert exit_code == 0, json.dumps(report, indent=2)[:4000]
    assert report["ok"] is True
    assert report["reproducible"] is True
    assert report["revision"] == "0006_widget_public_lookup"
    assert report["differences"] == []
    assert report["compose"]["ok"] is True
    assert len(report["databases"]) == 2, "one migration compared against itself proves nothing"

    # The two databases compared must be the two that were migrated, and distinct.
    # Mutation-checked: `inventory(first), inventory(first)` leaves every other
    # assertion here green.
    compared = report["compared"]
    assert len(set(compared)) == 2, compared
    assert set(compared) == set(report["databases"]), compared
    assert all(name.startswith(fut.SCRATCH_PREFIX) for name in compared), compared

    # The floors are met by the real schema, not only by the fixture above.
    for fact, floor in fut.FLOORS.items():
        assert report["counts"][fact] >= floor, (fact, report["counts"][fact], floor)


def test_the_check_never_leaves_a_scratch_database_behind(env_for_check: None) -> None:
    """A leftover on a shared cluster is an operator's problem, so cleanup is asserted."""
    import psycopg

    fut.run_check(keep=False)
    with psycopg.connect(fut.owner_dsn("postgres"), autocommit=True) as conn:
        left = conn.execute(
            "SELECT datname FROM pg_database WHERE datname LIKE %s",
            (f"{fut.SCRATCH_PREFIX}%",),
        ).fetchall()
    assert left == [], f"scratch databases left behind: {left}"
