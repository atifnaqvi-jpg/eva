"""ADMN-17: the knowledge screen's restore control, and the contract it must not break.

The row's risk is not that the button fails to render. It is that the button *lies*:
the previous control a developer could have wired would have called an endpoint that
serves one document alone, so "restore previous version" would have shrunk a
five-document knowledge base to one — with a 200 and no warning. That is why ADMN-15 was
halted rather than shipped, and this file is the source-level half of the guard.

Four properties, each of which has failed somewhere in this project before:

* the control is **gated on the API's own field**, so it cannot be offered in a state
  where the endpoint refuses it (the ADMN-14 shape: a screen describing a number it
  could not read);
* the client sends **no version id**, because the API refuses one;
* a refusal is shown with the **server's code and sentence**, so "nothing published" and
  "no earlier set" do not read alike (SEC-15's `lookup_failed` lesson, in a fourth
  place);
* the copy says **what a restore does** — the whole earlier set comes back, nothing is
  un-published — because that is the exact misunderstanding that produced HALT-10.

The live behaviour is asserted in `tests/test_knowledge_rollback.py` (12 tests, real
Postgres, retrieval path included) and at the API in `tests/test_knowledge_api.py`. This
file never re-derives the server's rules; it checks the screen agrees with them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE_PAGE = REPO_ROOT / "dashboard" / "app" / "knowledge" / "page.tsx"
API_CLIENT = REPO_ROOT / "dashboard" / "lib" / "api.ts"


@pytest.fixture(scope="module")
def page_source() -> str:
    return KNOWLEDGE_PAGE.read_text()


@pytest.fixture(scope="module")
def api_source() -> str:
    return API_CLIENT.read_text()


def handler(source: str, name: str) -> str:
    """The body of one function, so an assertion cannot be satisfied elsewhere.

    Bounded deliberately: a phrase that appears in the card but in a *different*
    handler is what made three ADMN-14 guards pass against mutated code.
    """
    start = source.index(f"async function {name}")
    end = source.index("async function", start + 10)
    return source[start:end]


def test_the_client_calls_rollback_with_the_bot_id_only(api_source: str) -> None:
    """A `version_id` in the body is answered 422, so the client must not send one."""
    start = api_source.index("export function rollbackKnowledge")
    end = api_source.index("\n}", api_source.index("});", start))
    body = api_source[start:end]
    assert '"/api/v1/knowledge/rollback"' in body
    assert "JSON.stringify({ bot_id: botId })" in body, (
        "the ruling's operation takes a bot; a version id is the old serve-alone semantics"
    )
    # The request body, not the whole function: the response type legitimately carries
    # `active_knowledge_version_ids`, and a substring check on the function would fail on
    # its own return type rather than on a version id being sent.
    request_body = body[body.index("JSON.stringify") : body.index("})", body.index("JSON.stringify"))]
    assert "version_id" not in request_body


def test_the_control_is_gated_on_the_field_the_api_reports(page_source: str) -> None:
    """Offered only when an earlier set exists — otherwise the button 409s by design."""
    assert "previous_knowledge_set_id" in page_source
    body = handler(page_source, "restorePrevious")
    assert "bot.previous_knowledge_set_id" in body, (
        "the handler must re-check the state it was rendered from; a stale screen would "
        "otherwise send a request the server refuses"
    )
    # And the button itself is disabled in that state, so the refusal is not the only
    # signal the owner gets.
    start = page_source.index("Restore previous version")
    assert "disabled={" in page_source[start - 600 : start + 100]


def test_the_handler_uses_the_set_level_client_not_a_version_call(page_source: str) -> None:
    """`publishVersion` beside a restore label is the HALT-10 defect with a new name."""
    body = handler(page_source, "restorePrevious")
    assert "rollbackKnowledge(" in body
    assert "publishVersion(" not in body, (
        "re-publishing an older document is additive, not a restore (PLAN §8.24 RULINGS)"
    )


def test_a_refusal_is_shown_with_the_servers_own_code_and_sentence(page_source: str) -> None:
    """The three refusal codes send the owner to different places; none may be swallowed."""
    body = handler(page_source, "restorePrevious")
    assert "ApiFailure" in body
    assert "failure.code" in body and "failure.message" in body, (
        "a generic sentence would erase the distinction between 'nothing published' and "
        "'nothing to restore'"
    )
    assert "setError(" in body


def test_the_notice_states_that_the_whole_set_came_back(page_source: str) -> None:
    """The row's whole point, on screen. "Restored the previous version" alone is the
    sentence that let an owner believe four documents were still live."""
    body = handler(page_source, "restorePrevious")
    assert "documents again" in body or "document again" in body
    assert "active_knowledge_version_ids.length" in body, (
        "the count must come from the response, not from a local assumption"
    )


def test_the_card_says_a_restore_does_not_unpublish(page_source: str) -> None:
    """The copy that prevents the misunderstanding, and it must be the card's own text."""
    start = page_source.index("Restore previous version")
    # The hint sits in the same row as the button.
    row = page_source[start : start + 1200]
    assert "Nothing is un-published" in row
    assert "previous_knowledge_version_ids.length" in row, (
        "the hint must name how many documents a restore would serve, from the API"
    )


def test_the_client_type_carries_the_previous_set(api_source: str) -> None:
    """A field the type drops is a field the screen structurally cannot read.

    This is ADMN-14's actual defect: `TenantSettings` declared only `handover`, so the
    retention card could not read the number it described however correct the copy was.
    """
    start = api_source.index("export type BotConfig")
    end = api_source.index("};", start)
    body = api_source[start:end]
    assert "previous_knowledge_set_id: string | null;" in body
    assert "previous_knowledge_version_ids: string[];" in body
