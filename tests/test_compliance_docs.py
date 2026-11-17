"""SEC-11: the privacy notice and DPA skeleton must not drift from the code.

A compliance document is a claim about behaviour, and the failure mode is silent: the
repository stays green while the published notice names a sub-processor the runtime no
longer calls, or promises a retention floor the purge does not honour. These tests make
that drift red.

What is asserted, and why each one can fail:

* the committed files are exactly what the renderer produces — so editing a document by
  hand, or changing code without re-rendering, fails;
* every sub-processor named in the runtime profiles appears in both documents, and no
  provider that cannot process a conversation is named as one;
* every table the migrations create appears in the data inventory;
* the retention figures and the purge schedule in the documents are the code's values;
* every operator placeholder is declared, and every declared placeholder is used — a
  silently dropped `[[FILL: ...]]` would publish an incomplete legal document as if it
  were finished.

Deliberately not asserted: legal correctness. That is a lawyer's call, and a test that
claimed it would be the same false evidence as an unreachable assertion.
"""

import re
from pathlib import Path

import pytest

from app.compliance import facts, render

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS = REPO_ROOT / "docs" / "compliance"

MARKER = re.compile(r"\[\[FILL: ([a-z0-9_]+)\]\]")


def _flat(text: str) -> str:
    """Collapse wrapping so a phrase assertion tests wording, not line width."""
    return " ".join(text.split())


def _committed() -> dict[str, str]:
    return {
        name: (DOCS / name).read_text(encoding="utf-8")
        for name in render.render_all()
    }


def test_documents_are_rendered_from_the_code() -> None:
    """The files on disk are the renderer's output, not a hand-edit of it."""
    rendered = render.render_all()
    for name, expected in rendered.items():
        path = DOCS / name
        assert path.is_file(), f"{name} is missing; run `python -m app.compliance.render`"
        assert path.read_text(encoding="utf-8") == expected, (
            f"{name} differs from the renderer. Either re-render it, or change the "
            "code the document describes."
        )


@pytest.mark.parametrize("name", sorted(render.render_all()))
def test_every_operator_placeholder_is_declared(name: str) -> None:
    """A marker nobody declared is a typo that publishes as literal text."""
    found = set(MARKER.findall(render.render_all()[name]))
    undeclared = found - facts.declared_markers()
    assert not undeclared, f"{name} uses undeclared markers: {sorted(undeclared)}"


def test_every_declared_placeholder_is_used_somewhere() -> None:
    """A declared-but-unused marker means a required field silently vanished."""
    used: set[str] = set()
    for content in render.render_all().values():
        used |= set(MARKER.findall(content))
    unused = facts.declared_markers() - used
    assert not unused, f"declared but never rendered: {sorted(unused)}"


def test_sub_processors_follow_the_runtime_profiles() -> None:
    """The DPA's processor list *is* the set of providers the API can call.

    Mutation check performed when this was written: adding a fourth entry to
    ``PROFILES`` fails this test and both documents' text comparisons until the
    documents are re-rendered, so a provider cannot be reachable and undisclosed.
    """
    named = {sp.key for sp in facts.sub_processors()}
    assert named == set(facts.PROFILES)

    for name, content in _committed().items():
        for sp in facts.sub_processors():
            assert sp.label in content, f"{name} omits sub-processor {sp.label}"


def test_every_runtime_provider_has_a_declared_display_name() -> None:
    """A legal document that names a sub-processor wrong names the wrong entity.

    Mutation check: adding a provider to ``PROFILES`` without extending
    ``PROVIDER_DISPLAY_NAMES`` fails this test, so the naming decision is forced
    rather than defaulted to ``key.title()``.
    """
    undeclared = set(facts.PROFILES) - set(facts.PROVIDER_DISPLAY_NAMES)
    assert not undeclared, (
        f"providers without a declared display name: {sorted(undeclared)}"
    )


def test_stored_but_unusable_providers_are_not_named_as_sub_processors() -> None:
    """``vault.ALLOWED_PROVIDERS`` accepts keys for providers with no runtime profile.

    A key can be stored for OpenAI or Anthropic, but nothing can send them a
    conversation, so naming them would overstate the processing chain — the exact
    opposite of the error a careless template makes, and just as wrong.
    """
    from app.security import vault

    storable_only = set(vault.ALLOWED_PROVIDERS) - set(facts.PROFILES)
    assert storable_only, "premise of this test no longer holds; review the inventory"
    document = "\n".join(_committed().values())
    for key in storable_only:
        assert key.title() not in document, (
            f"{key} cannot process a conversation but is named in the documents"
        )


def test_no_third_party_is_named_for_embeddings_or_reranking() -> None:
    """Embeddings are self-hosted and there is no reranker in the approved stack."""
    document = "\n".join(_committed().values()).lower()
    for wrong in ("openai embeddings", "voyage", "cohere", "jina"):
        assert wrong not in document


def _migration_tables() -> set[str]:
    """Every table the migrations create, from both call styles used in this repo.

    ``op.create_table("name", ...)`` and a raw ``CREATE TABLE name``. The latter is how
    ``chunks`` and ``sessions`` are made, and a regex that only knew the first form
    would silently under-report the schema — then the inventory test would pass while
    missing tables.
    """
    names: set[str] = set()
    for path in (REPO_ROOT / "backend" / "api" / "migrations" / "versions").glob("*.py"):
        source = path.read_text(encoding="utf-8")
        names |= set(re.findall(r'op\.create_table\(\s*"([a-z_]+)"', source))
        names |= set(
            re.findall(r"CREATE TABLE (?:IF NOT EXISTS )?([a-z_]+)", source)
        )
    return names


def test_data_inventory_covers_every_table_the_migrations_create() -> None:
    """A new table is new personal data; the DPA appendix must say so."""
    inventoried = {table.name for table in facts.TABLES}
    missing = _migration_tables() - inventoried
    assert not missing, f"tables absent from the DPA inventory: {sorted(missing)}"


def test_inventory_names_no_table_that_does_not_exist() -> None:
    """An invented table in a legal appendix is a false statement about the system."""
    invented = {table.name for table in facts.TABLES} - _migration_tables()
    assert not invented, f"inventory names tables that do not exist: {sorted(invented)}"


def test_retention_numbers_are_the_implemented_ones() -> None:
    """The notice promises a window and a schedule; both are read from the code."""
    facts_ = facts.retention_facts()
    document = _committed()["privacy-notice-template.md"]
    assert f"**{facts_.default_days} days**" in document
    assert (
        f"between {facts_.minimum_days} and {facts_.maximum_days}" in document
    ), "the configurable floor/ceiling is not stated"
    assert f"{facts_.purge_hour:02d}:{facts_.purge_minute:02d}" in document
    for status in facts_.eligible_statuses:
        assert f"`{status}`" in document


def test_the_purge_schedule_is_read_from_the_worker_not_restated() -> None:
    """Guards the link, not the value: changing the cron hour must change the notice."""
    from worker.settings import WorkerSettings

    purge = next(j for j in WorkerSettings.cron_jobs if "purge" in j.name)
    assert (purge.hour, purge.minute) == (
        facts.retention_facts().purge_hour,
        facts.retention_facts().purge_minute,
    )


def test_upload_terms_match_the_enforced_cap_and_types() -> None:
    """The notice states a size cap and an accepted-type list; both are enforced ones."""
    from app.config import VISITOR_MAX_UPLOAD_BYTES
    from app.widget.uploads import VISITOR_MIME_BY_SUFFIX

    document = _flat(_committed()["privacy-notice-template.md"])
    assert f"Up to {VISITOR_MAX_UPLOAD_BYTES // (1024 * 1024)} MB" in document
    for mime in VISITOR_MIME_BY_SUFFIX:
        assert mime in document, f"{mime} is accepted but not disclosed"


def test_notice_says_nothing_is_stored_before_the_chat_opens() -> None:
    """§19.3 and SEC-08 make this a release-blocking claim, so it is stated."""
    document = _committed()["privacy-notice-template.md"]
    assert "Before you open the chat: nothing." in _flat(document)


def test_notice_states_the_audit_log_exception() -> None:
    """The one retention limit the platform genuinely has must not be hidden.

    A notice that promises complete deletion while an append-only log retains the
    erasure event is exactly the sort of over-claim this work package exists to stop.
    """
    document = _committed()["privacy-notice-template.md"]
    assert "append-only audit log" in document
    assert "never your name, address or the pages you" in document


def test_dpa_does_not_claim_session_revocation_on_password_change() -> None:
    """There is no password-change endpoint, so sessions cannot be revoked by one.

    A DPA that promises a revocation it cannot perform is the kind of statement a
    template ships and an incident then disproves. If a change-password route is
    added later, delete this test deliberately rather than letting it rot.
    """
    from app.auth import router as auth_router

    routes = {getattr(r, "path", "") for r in auth_router.router.routes}
    assert not any("password" in p and "{" in p for p in routes), (
        "a password-change route now exists; the documents may state its effect"
    )
    document = _flat(_committed()["dpa-skeleton.md"])
    assert "password change" not in document


def test_dpa_does_not_call_erasure_self_service() -> None:
    """Erasure has an API, not a dashboard control — an operator performs it (§19.3).

    The export *is* self-service: it is a link in the dashboard. A clause that called
    both halves self-service would promise a control the Business does not have, which
    is the shape of error this file exists to catch. Mutation check: restoring
    "self-service" to §4 fails this test.
    """
    document = _flat(_committed()["dpa-skeleton.md"])
    assert "self-service" not in document
    assert "API operation" in document, (
        "the DPA must say *how* erasure happens, not merely drop the wrong word"
    )


def test_dpa_does_not_claim_a_backup_regime() -> None:
    """No restore drill exists yet, so the DPA marks backups as unfilled rather than
    inventing an RPO or a retention period."""
    document = _committed()["dpa-skeleton.md"]
    assert "[[FILL: backup_retention]]" in document
    assert "not yet implemented" in document


def test_dpa_does_not_claim_application_level_encryption_at_rest() -> None:
    """BYOK keys are encrypted; the database volume is the operator's problem."""
    document = _committed()["dpa-skeleton.md"]
    assert "not implemented in the application" in document


def test_ccpa_disclosure_is_not_claimed_here() -> None:
    """SEC-12 owns the CCPA/CPRA posture; SEC-11 must not pre-empt it."""
    document = _committed()["privacy-notice-template.md"]
    assert "are a separate template and are not claimed by this document" in _flat(document)
