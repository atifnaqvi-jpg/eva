"""SEC-12: the US state notice must be a statement about the code, not a template.

ARCHITECTURE §19.4 asks for four things — disclosed collection, no sale or share, no
training on tenant conversations without explicit opt-in, and deletion inside the
statutory window. Each is a claim about behaviour, and each fails silently in the same
way: the repository stays green while the published notice describes a product that
shipped last month.

So every one of them is tied to something the code can be asked about:

* **Categories** are checked against the live schema. A ``visitor_*`` column absent
  from the category map is personal data the notice does not disclose, and a pair that
  names a column which does not exist is a false statement about the system.
* **No sale or share** is checked against the shipped clients' asset references and
  their dependency manifests — the two ways a tracker actually arrives.
* **No training** is checked against the request body the provider client builds and
  the endpoints it calls, not against a sentence in the document.
* **Deletion** is checked against the erasure router and the retention policy, so the
  route named in the notice is the route that exists.

Deliberately not asserted: that any particular US statute applies to a given business.
That is the business's determination and its counsel's, which is why it is a
``[[FILL: ...]]`` marker rather than a generated claim.

Fixtures are synthetic throughout, per the confidentiality charter.
"""

import ast
import re
from pathlib import Path

import psycopg
import pytest

from app.compliance import facts, render, us_state

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS = REPO_ROOT / "docs" / "compliance"
NOTICE = "us-state-privacy-notice-template.md"

MARKER = re.compile(r"\[\[FILL: ([a-z0-9_]+)\]\]")


def _flat(text: str) -> str:
    """Collapse wrapping so a phrase assertion tests wording, not line width."""
    return " ".join(text.split())


def _committed(name: str = NOTICE) -> str:
    return (DOCS / name).read_text(encoding="utf-8")


# ------------------------------------------------------------------- the document


def test_the_us_notice_is_rendered_from_the_code() -> None:
    """A hand-edited notice is a claim nobody regenerates when the code moves."""
    assert (DOCS / NOTICE).read_text(encoding="utf-8") == render.render_us_state_notice()


def test_the_us_notice_uses_only_declared_markers() -> None:
    undeclared = set(MARKER.findall(_committed())) - facts.declared_markers()
    assert not undeclared, f"{NOTICE} uses undeclared markers: {sorted(undeclared)}"


def test_every_us_marker_is_declared_and_used() -> None:
    """A declared-but-unrendered marker means a required field silently vanished."""
    declared = {
        name
        for name in facts.declared_markers()
        if name.startswith("us_")
    }
    used = set(MARKER.findall(_committed()))
    assert declared <= used, f"declared US markers never rendered: {sorted(declared - used)}"


# --------------------------------------------------------------------- categories


def _schema_columns(migrated_db: str) -> dict[str, set[str]]:
    from conftest import _admin_dsn

    with psycopg.connect(_admin_dsn(migrated_db)) as conn:
        rows = conn.execute(
            "SELECT table_name, column_name FROM information_schema.columns"
            " WHERE table_schema = 'public'"
        ).fetchall()
    out: dict[str, set[str]] = {}
    for table, column in rows:
        out.setdefault(table, set()).add(column)
    return out


def test_every_attributed_column_exists(migrated_db: str) -> None:
    """A category pointing at a column the schema lacks is an invented fact.

    This is the direction that catches a rename: ``visitor_name`` moved or dropped would
    leave the notice describing a field that is not there.
    """
    schema = _schema_columns(migrated_db)
    for code, pairs in us_state.CATEGORY_COLUMNS.items():
        for table, column in pairs:
            assert table in schema, f"category {code} names a table that does not exist"
            assert column in schema[table], (
                f"category {code} names {table}.{column}, which the schema does not have"
            )


def test_every_visitor_column_is_attributed_to_a_category(migrated_db: str) -> None:
    """Personal data cannot be added without appearing in the disclosure.

    The rule is structural rather than a list of names: every ``visitor_*`` column is
    personal data by construction, so a new one fails here until it is disclosed. This is
    the direction that catches the omission a hand-written notice makes.
    """
    schema = _schema_columns(migrated_db)
    missing = us_state.undisclosed_personal_columns(schema)
    assert not missing, f"personal-data columns disclosed under no category: {sorted(missing)}"


def test_the_category_table_renders_every_category_including_the_absent_ones() -> None:
    """Listing only the collected categories cannot distinguish 'no' from 'unmentioned'."""
    document = _flat(_committed())
    for category in us_state.categories():
        assert f"**{category.code}.** {category.name}" in document, category.code
        expected = "| Yes |" if category.collected else "| No |"
        row = f"**{category.code}.** {category.name} {expected}"
        assert row in document, f"category {category.code} is not marked {expected.strip()}"


def test_the_collected_categories_are_the_disclosed_ones() -> None:
    """The set in prose and the set in the document come from one list, so pin the set.

    Mutation check: flipping a category to ``collected=True`` without a purpose and a
    recipient fails here and in the render diff, so the disclosure cannot become a
    "Yes" row with nothing said about it.
    """
    collected = us_state.collected_categories()
    assert {"A", "B", "F"} <= {c.code for c in collected}
    assert "C" not in {c.code for c in collected}, "protected characteristics are not collected"
    assert "L" not in {c.code for c in collected}, "no sensitive personal information is collected"
    for category in collected:
        assert category.detail and category.purpose and category.disclosed_to, category.code


# ------------------------------------------------------------------- sale / share


def test_the_shipped_clients_load_nothing_from_another_origin() -> None:
    """'No share' rests on the pages pulling nothing third-party.

    Mutation check: add ``<script src="https://cdn.example/x.js">`` to the widget's
    index and the count in the notice changes, so the sentence and the scan are the
    same fact.
    """
    assert us_state.third_party_assets() == []


def test_no_client_depends_on_a_tracker_package() -> None:
    """A dependency is the other way a pixel arrives, and it is invisible in the HTML."""
    assert us_state.tracker_dependencies() == []


def test_the_notice_states_the_measured_counts_not_a_claim() -> None:
    document = _flat(_committed())
    assert f"load **{len(us_state.third_party_assets())}** asset from another origin" in document
    assert f"finds **{len(us_state.tracker_dependencies())}** match" in document


def test_the_notice_states_no_sale_and_no_sharing() -> None:
    document = _flat(_committed())
    assert "does not sell personal information" in document
    assert "cross-context behavioural advertising" in document


def test_the_device_storage_paragraph_matches_the_widget() -> None:
    """The first draft said "session-scoped only" and the widget has a durable opt-in.

    A claim about the visitor's device is the easiest one to get wrong, because the
    storage code and the notice live in different languages. The durable key and the
    checkbox label are read from the source, so adding storage without disclosing it
    fails here.
    """
    storage = us_state.device_storage()
    document = _committed()
    for key in storage.session_keys + storage.durable_keys:
        assert f"`{key}`" in document, f"storage key {key} is not disclosed"
    assert storage.opt_in_prompt in document
    assert "off by default" in _flat(document)


def test_the_durable_identifier_is_an_opt_in_not_a_default() -> None:
    """R42: the durable id exists only when the visitor ticked the box.

    Asserted on the widget source rather than on the prose, because the prose is what
    would be wrong: ``remembered()`` reads ``localStorage`` and ``id()`` falls back to
    it, so a bug that wrote the durable key on load would silently make every visitor
    persistent while the notice still promised an opt-in.
    """
    from app.compliance.us_state import DURABLE_OPT_IN_UI, REPO_ROOT, STORAGE_KEYS

    identity = (REPO_ROOT / STORAGE_KEYS).read_text(encoding="utf-8")
    # Every write of the durable key must be inside ``remember()``.
    body = identity.split("remember(enabled: boolean): void {", 1)
    assert len(body) == 2, "identity.ts no longer has a remember() method"
    writes_before = body[0].count("safeSet(win.localStorage, DURABLE_KEY")
    writes_inside = body[1].count("safeSet(win.localStorage, DURABLE_KEY")
    assert writes_before == 0, "the durable identifier is written outside remember()"
    assert writes_inside == 1, "remember() should write the durable key in one place"
    ui = (REPO_ROOT / DURABLE_OPT_IN_UI).read_text(encoding="utf-8")
    assert "useState(false)" in ui, "the remember checkbox must default to off"


def test_the_notice_names_the_model_providers_as_service_providers() -> None:
    """A provider that receives content is a disclosure, and must be named, not implied."""
    document = _flat(_committed())
    for label in us_state.service_providers():
        assert label in document, label
    assert "service providers" in document


# ----------------------------------------------------------------------- training


def test_the_provider_request_carries_no_training_field() -> None:
    """The claim is 'we cannot', so the check is on what is actually sent."""
    position = us_state.training_position()
    assert position.request_fields, "the payload probe returned nothing to inspect"
    assert position.training_fields == ()


def test_the_provider_client_calls_only_the_completion_endpoint() -> None:
    """A dataset or fine-tuning path would be a training route; none may exist."""
    paths = us_state.training_position().request_paths
    assert paths == ("/chat/completions",)


def test_no_training_setting_exists_in_tenant_settings() -> None:
    """An opt-in that no code reads would be theatre.

    The notice says the opt-in is not implemented. This asserts that, so adding a
    ``train_on_conversations`` setting without wiring it fails here rather than
    publishing a promise the pipeline ignores.
    """
    keys = us_state.training_position().opt_in_keys
    assert keys == (), f"a training opt-in key appeared: {sorted(keys)}"


def test_the_notice_states_the_training_position_and_its_absence_of_opt_in() -> None:
    document = _flat(_committed())
    assert "not used to train a model" in document
    assert "not implemented, and no such opt-in setting exists" in document


def test_the_dpa_carries_the_training_use_clause() -> None:
    """§19.4's vendor-contract requirement: retention, training-use default none."""
    dpa = _flat(_committed("dpa-skeleton.md"))
    assert "Training use of the Business's data" in dpa
    assert "does not use the Business's conversations" in dpa
    assert "`max_tokens`" in dpa, "the clause must cite the request fields it checked"


# ----------------------------------------------------------------------- deletion


def test_the_statutory_window_is_stated_with_its_extension() -> None:
    position = us_state.deletion_position()
    document = _flat(_committed())
    assert f"within {position.statutory_days} days" in document
    assert "extendable once by a further" in document


def test_the_erasure_route_named_is_the_route_that_exists() -> None:
    """The notice tells an operator how to delete; a stale path makes that instruction fail."""
    from app.conversation import erasure

    declared = {f"/api/v1{r.path}" for r in erasure.router.routes}
    assert us_state.deletion_position().erasure_route in declared
    assert f"`{us_state.deletion_position().erasure_route}`" in _committed()


def test_the_retention_numbers_are_the_implemented_ones() -> None:
    from app.retention import policy

    document = _flat(_committed())
    assert f"**{policy.DEFAULT_RETENTION_DAYS} days** by default" in document
    assert f"between {policy.MIN_RETENTION_DAYS} and {policy.MAX_RETENTION_DAYS}" in document


def test_the_notice_does_not_claim_automated_deletion_propagation() -> None:
    """The gap is real: no mechanism asks a provider to delete a prompt.

    §19.4 says the DPA must state 'working deletion propagation'. It cannot yet, so both
    documents state the limit. A future implementation must delete this test and rewrite
    the clauses deliberately, rather than leaving a green suite over a false promise.
    """
    sources = [
        REPO_ROOT / "backend" / "api" / "app" / "conversation" / "erasure.py",
        REPO_ROOT / "backend" / "api" / "app" / "conversation" / "providers.py",
        REPO_ROOT / "backend" / "api" / "app" / "handover" / "webhooks.py",
    ]
    for path in sources:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        assert not {"delete_file", "delete_dataset"} & names, (
            f"{path.name} appears to call a provider deletion API"
        )
    for name in (NOTICE, "dpa-skeleton.md"):
        document = _flat(_committed(name))
        assert "not propagated" in document or "not automated" in document, name


def test_the_notice_states_the_audit_log_exception() -> None:
    document = _flat(_committed())
    assert "append-only audit log" in document
    assert "never the consumer's name, address, session identifier or page address" in document


def test_the_notice_does_not_claim_backups() -> None:
    document = flat = _flat(_committed())
    assert "Backups are not claimed here" in flat
    assert "no restore drill exists yet" in document


# ------------------------------------------------------------------------ hygiene


@pytest.mark.parametrize("name", [NOTICE, "privacy-notice-template.md", "dpa-skeleton.md"])
def test_no_document_names_a_storable_but_unusable_provider(name: str) -> None:
    """Same asymmetry the GDPR notice asserts: storage is not processing."""
    from app.security import vault

    storable_only = set(vault.ALLOWED_PROVIDERS) - set(facts.PROFILES)
    document = _committed(name)
    for key in storable_only:
        assert key.title() not in document
