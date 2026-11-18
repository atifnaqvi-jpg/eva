"""FUT-04: the same code produces the same schema on a dedicated database.

ARCHITECTURE §16.3: *"Prototype = shared schema; single-tenant later = same schema,
dedicated database (R13)."* R13 defers the *commercial* single-tenant offering, so this
row is readiness only: it has to be true that deploying one tenant onto its own database
needs no code fork, only a different ``POSTGRES_DB``.

Prose cannot establish that, and a one-off manual migration cannot either — the claim is
that the result is **reproducible** and **complete**. So this command builds the thing
twice, from the same migration code, into two databases that share nothing, and compares
what landed:

    python scripts/fut04_single_tenant.py check

Four things are compared, and the second is the one a tables-exist check misses:

* **Schema identity** — every table, column with its type and nullability, index,
  constraint, extension, trigger, function, policy *including its predicate*, the RLS
  enable/force flags, and the per-table ``eva_app`` grants. A database that has the
  tables but lost its policies is the failure mode OPS-09's drill was built around
  (§8.34): counts match and every request still 500s or over-returns.
* **Reproducibility** — two independent migrations, not one compared against itself.
  Comparing a fresh database against the *live* one would prove the opposite of the
  claim: the live database carries whatever drift its own history left behind.
* **Deployment configuration** — every service's DSN in ``compose.yaml`` must name
  ``${POSTGRES_DB}``. This is not decoration: the ``worker`` used to hard-code ``/eva``
  while the ``api`` followed ``POSTGRES_DB``, so a dedicated deployment silently had its
  API talking to one database and its ingestion worker to another, which is precisely
  the fork this row forbids (measured; the fix is in the same commit).
* **Floor counts** — a comparison of two empty databases is the same green as a correct
  one (§8.25's lesson, twice learned here). Zero objects found is a failure, not a pass.

**A guard derived from a measured accident.** Migration ``0001`` runs ``ALTER ROLE
eva_app ... PASSWORD`` on every upgrade, and a role is *cluster*-wide while a database is
not. So on a shared cluster, migrating a second database with a different
``EVA_APP_DB_PASSWORD`` rewrites the credential the first deployment is using — observed
here: the running stack's ``eva_app`` login changed out from under it. This script
therefore refuses to migrate (exit 2) when the role already exists and cannot
authenticate with the configured password, because in that state it would silently
rotate a shared credential rather than provision a dedicated one. On a genuinely
dedicated cluster (the enterprise path) the role is created with the new password and
that is correct, so a missing role is allowed rather than refused.

Exit codes match ``scripts/security_scan.py``: 0 identical, 1 differences, 2 could not
complete. "Could not complete" is never reported as a pass — a comparison that ran against
nothing is the failure mode a readiness gate has to be able to avoid.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

import psycopg
from psycopg.sql import SQL, Composable, Identifier

REPO = Path(__file__).resolve().parents[1]
API_DIR = REPO / "backend" / "api"
COMPOSE = REPO / "compose.yaml"

# Every scratch database this script creates is named with this prefix, and nothing else
# is ever dropped. A cleanup routine that drops "whatever it was asked to drop" is one
# typo away from the live database, and this script runs against a live cluster.
SCRATCH_PREFIX = "eva_fut04_"

# The runtime role migration 0001 creates and grants (§16.3). A module constant so the
# guard can be exercised against a role name that cannot exist.
APP_ROLE = "eva_app"

# Floors. A comparison of two empty databases passes every equality test, so the counts
# are asserted against the known shape as well as against each other. Deliberately
# slightly below today's numbers (19 tables, 17 policies, ...) so a future migration that
# adds objects does not fail the gate for the wrong reason.
FLOORS = {
    "tables": 19,
    "columns": 150,
    "policies": 17,
    "grants": 19,
    "indexes": 40,
    "constraints": 55,
    "functions": 20,
    "extensions": 3,
    "triggers": 1,
    "rls": 19,
}


class ReadinessError(RuntimeError):
    """A condition that makes the check meaningless rather than merely failing."""


def owner_dsn(dbname: str) -> str:
    """The migration/owner connection, from the same variables Alembic uses."""
    host = os.environ.get("TEST_PG_HOST", "127.0.0.1")
    port = os.environ.get("TEST_PG_PORT", "5432")
    user = os.environ.get("TEST_PG_USER", "eva")
    password = os.environ.get("TEST_PG_PASSWORD", "changeme")
    return f"host={host} port={port} user={user} password={password} dbname={dbname}"


def app_credentials() -> tuple[str, str]:
    """The runtime role's name and password, as the migration will set them."""
    password = os.environ.get("EVA_APP_DB_PASSWORD", "")
    if not password:
        raise ReadinessError(
            "EVA_APP_DB_PASSWORD must be set: migration 0001 requires it, and this "
            "script needs it for the shared-role guard"
        )
    return APP_ROLE, password


def scratch_name() -> str:
    return f"{SCRATCH_PREFIX}{uuid.uuid4().hex[:12]}"


def check_scratch_name(name: str) -> None:
    """Refuse any target this script did not create itself."""
    if not name.startswith(SCRATCH_PREFIX):
        raise ReadinessError(
            f"refusing to touch {name!r}: only {SCRATCH_PREFIX}* databases are mine to "
            "create or drop"
        )


# --- the shared-role guard -----------------------------------------------------


def role_guard() -> dict:
    """Refuse to migrate when doing so would rotate a shared credential.

    Returns what was measured so the report states the premise rather than assuming it.
    """
    role, password = app_credentials()
    with psycopg.connect(owner_dsn("postgres"), autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s", (APP_ROLE,)
        ).fetchone() is not None
    if not exists:
        # A dedicated cluster's first migration: CREATE ROLE is the correct outcome.
        return {"role": role, "preexisting": False, "authenticates": False}

    host = os.environ.get("TEST_PG_HOST", "127.0.0.1")
    port = os.environ.get("TEST_PG_PORT", "5432")
    try:
        psycopg.connect(
            f"host={host} port={port} user={role} password={password} dbname=postgres"
        ).close()
        authenticates = True
    except psycopg.OperationalError:
        authenticates = False

    if not authenticates:
        raise ReadinessError(
            f"role {role!r} already exists and does not accept the configured "
            "EVA_APP_DB_PASSWORD. Migration 0001 sets that password on every upgrade, "
            "and a role is cluster-wide while a database is not, so migrating here "
            "would rewrite the credential of every other database on this cluster "
            "(measured: it did). Either align EVA_APP_DB_PASSWORD with the role, or run "
            "this against a dedicated cluster, where the role is created fresh."
        )
    return {"role": role, "preexisting": True, "authenticates": True}


# --- lifecycle ------------------------------------------------------------------


def _maintenance(sql: Composable, *params: object) -> None:
    """Run one statement against the maintenance database.

    ``CREATE DATABASE`` and ``DROP DATABASE`` take an identifier, not a bound parameter,
    so the name travels through ``psycopg.sql.Identifier`` and is quoted by the driver
    rather than by an f-string here. ``check_scratch_name`` still runs first: the
    identifier quoting stops injection, but only the prefix check stops this script from
    being pointed at the live database.
    """
    with psycopg.connect(owner_dsn("postgres"), autocommit=True) as conn:
        conn.execute(sql, params or None)


def create_database(name: str) -> None:
    check_scratch_name(name)
    _maintenance(SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(Identifier(name)))
    _maintenance(SQL("CREATE DATABASE {}").format(Identifier(name)))


def drop_database(name: str) -> None:
    check_scratch_name(name)
    _maintenance(SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(Identifier(name)))


def migrate(name: str) -> str:
    """Run the repository's own migration code against ``name``.

    Subprocess rather than an in-process Alembic call: the container and CI both invoke
    ``python -m alembic``, and a readiness check that used a different entry point would
    be measuring the check rather than the deployment.
    """
    check_scratch_name(name)
    host = os.environ.get("TEST_PG_HOST", "127.0.0.1")
    port = os.environ.get("TEST_PG_PORT", "5432")
    user = os.environ.get("TEST_PG_USER", "eva")
    password = os.environ.get("TEST_PG_PASSWORD", "changeme")
    env = os.environ.copy()
    env["DATABASE_URL"] = (
        f"postgresql+psycopg://{user}:{password}@{host}:{port}/{name}"
    )
    env.setdefault("EMBEDDING_DIM", "1024")
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=API_DIR, env=env, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise ReadinessError(
            f"alembic upgrade failed for {name}: "
            f"{(result.stderr or result.stdout).strip()[-800:]}"
        )
    return revision(name)


# --- what landed ----------------------------------------------------------------


def inventory(dbname: str) -> dict:
    """Every schema fact that decides whether the deployment works, not merely exists.

    Policies are compared *with their predicate*: two databases can each hold 17
    policies and enforce different rules, and the predicate is the rule.
    """
    with psycopg.connect(owner_dsn(dbname)) as conn:
        q = lambda sql: conn.execute(sql).fetchall()  # noqa: E731 - local shim
        return {
            # Carried so a caller can prove it read two different databases; every
            # equality test below is also satisfied by reading one database twice.
            "dbname": dbname,
            "tables": sorted(r[0] for r in q(
                "SELECT tablename FROM pg_tables WHERE schemaname='public'")),
            "columns": sorted((r[0], r[1], r[2], r[3]) for r in q(
                "SELECT table_name, column_name, data_type, is_nullable"
                " FROM information_schema.columns WHERE table_schema='public'")),
            "policies": sorted((r[0], r[1], r[2], r[3]) for r in q(
                "SELECT tablename, policyname, qual, with_check"
                " FROM pg_policies WHERE schemaname='public'")),
            "grants": sorted((r[0], r[1], r[2], r[3], r[4]) for r in q(
                "SELECT tablename,"
                " has_table_privilege('eva_app','public.'||quote_ident(tablename),'SELECT'),"
                " has_table_privilege('eva_app','public.'||quote_ident(tablename),'INSERT'),"
                " has_table_privilege('eva_app','public.'||quote_ident(tablename),'UPDATE'),"
                " has_table_privilege('eva_app','public.'||quote_ident(tablename),'DELETE')"
                " FROM pg_tables WHERE schemaname='public'")),
            "extensions": sorted(r[0] for r in q(
                "SELECT extname FROM pg_extension")),
            "triggers": sorted((r[0], r[1]) for r in q(
                "SELECT tgname, relname FROM pg_trigger t"
                " JOIN pg_class r ON r.oid = t.tgrelid WHERE NOT tgisinternal")),
            "functions": sorted(r[0] for r in q(
                "SELECT p.proname FROM pg_proc p JOIN pg_namespace n"
                " ON n.oid = p.pronamespace WHERE n.nspname='public'")),
            "indexes": sorted((r[0], r[1]) for r in q(
                "SELECT tablename, indexname FROM pg_indexes"
                " WHERE schemaname='public'")),
            "constraints": sorted((r[0], r[1]) for r in q(
                "SELECT conrelid::regclass::text, conname FROM pg_constraint"
                " WHERE connamespace='public'::regnamespace")),
            "rls": sorted((r[0], r[1], r[2]) for r in q(
                "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity"
                " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                " WHERE n.nspname='public' AND c.relkind='r'")),
        }


def revision(dbname: str) -> str:
    with psycopg.connect(owner_dsn(dbname)) as conn:
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    if row is None:
        raise ReadinessError(f"{dbname} has no alembic revision: the migration did not run")
    return row[0]


def compare(left: dict, right: dict) -> dict:
    """Differences and floor violations, in one structure a reader can act on."""
    differences = []
    for key, floor in FLOORS.items():
        a = {str(x) for x in left[key]}
        b = {str(x) for x in right[key]}
        if len(a) < floor:
            differences.append({
                "kind": "below_floor", "fact": key, "floor": floor,
                "found": len(a),
                "why": "a comparison of two near-empty databases is not evidence",
            })
        for item in sorted(a - b):
            differences.append({"kind": "missing", "fact": key, "item": item[:300]})
        for item in sorted(b - a):
            differences.append({"kind": "extra", "fact": key, "item": item[:300]})
    return {
        "differences": differences,
        "counts": {k: len(left[k]) for k in FLOORS},
        "compared": [left.get("dbname"), right.get("dbname")],
    }


# --- deployment configuration ---------------------------------------------------


def compose_dsn_problems(path: Path = COMPOSE) -> list[dict]:
    """Every service DSN must follow ``POSTGRES_DB``, or a deployment needs a fork.

    Parsed textually rather than with PyYAML: PyYAML is not a project dependency, and
    adding one to read a build file is a dependency decision this row does not get.
    """
    text = path.read_text()
    problems = []
    # `.+` rather than `\S+`: the variable's fail-closed form `:?VAR is required` puts
    # spaces inside the value, which a non-space pattern silently skips — the whole line
    # would go uncounted and the floor below would be the only thing to notice.
    pattern = re.compile(
        r"^\s+(APP_DATABASE_URL|DATABASE_URL):\s*(.+?)\s*$", re.MULTILINE
    )
    found = 0
    for match in pattern.finditer(text):
        variable, dsn = match.group(1), match.group(2)
        found += 1
        database = dsn.rsplit("/", 1)[-1]
        if "POSTGRES_DB" not in database:
            problems.append({
                "kind": "hardcoded_database", "variable": variable, "database": database,
                "why": "a dedicated deployment would need this line edited by hand",
            })
    if found < 3:
        problems.append({
            "kind": "below_floor", "variable": "DSN lines", "found": found, "floor": 3,
            "why": "zero findings from zero input is the same green as a clean file",
        })
    return problems


# --- command ----------------------------------------------------------------------


def run_check(*, keep: bool) -> int:
    report: dict = {"ok": False, "databases": {}, "reproducible": False, "compose": {}}
    created: list[str] = []
    try:
        report["role_guard"] = role_guard()

        first, second = scratch_name(), scratch_name()
        for name in (first, second):
            create_database(name)
            created.append(name)

        report["databases"] = {
            first: {"revision": migrate(first)},
            second: {"revision": migrate(second)},
        }
        report["revision"] = report["databases"][first]["revision"]
        report["reproducible"] = (
            report["databases"][first]["revision"]
            == report["databases"][second]["revision"]
        )

        left, right = inventory(first), inventory(second)
        result = compare(left, right)
        report["counts"] = result["counts"]
        report["compared"] = result["compared"]
        report["differences"] = result["differences"]

        problems = compose_dsn_problems()
        report["compose"] = {"problems": problems, "ok": not problems}

        report["ok"] = (
            report["reproducible"]
            and not result["differences"]
            and not problems
        )
        return 0 if report["ok"] else 1
    except ReadinessError as exc:
        report["error"] = str(exc)
        return 2
    finally:
        if not keep:
            for name in created:
                try:
                    drop_database(name)
                except (ReadinessError, psycopg.Error) as exc:
                    # Never mask the verdict with a cleanup problem, but say so: a
                    # leftover database on a shared cluster is an operator's problem.
                    report.setdefault("cleanup_warnings", []).append(f"{name}: {exc}")
        elif created:
            report["kept"] = created
        print(json.dumps(report, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=("check",))
    parser.add_argument(
        "--keep", action="store_true",
        help="leave the scratch databases in place for inspection",
    )
    args = parser.parse_args(argv)
    return run_check(keep=args.keep)


if __name__ == "__main__":
    sys.exit(main())
