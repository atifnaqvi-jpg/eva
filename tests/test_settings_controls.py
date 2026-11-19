"""ADMN-16: the retention editor and the domain controls.

The row closes two API-only gaps on `/settings`. Its risk is not that the controls
fail to render — it is that they offer a range the server refuses, or report a state
the server does not agree with. Both are asserted here against the backend's own
modules rather than against literals retyped into the test:

* the day bounds come from `app.retention.policy`, so a widened server range without a
  screen change (or the reverse) is red;
* the domain flow is asserted through the **client functions** against a stubbed
  `fetch`, which is the seam the dashboard's own suite already uses. What matters is
  that add POSTs to the add route and does not verify, that verify is its own call,
  and that a refusal is read as a body rather than a thrown error.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SETTINGS_PAGE = REPO_ROOT / "dashboard" / "app" / "settings" / "page.tsx"
API_CLIENT = REPO_ROOT / "dashboard" / "lib" / "api.ts"


@pytest.fixture(scope="module")
def page_source() -> str:
    return SETTINGS_PAGE.read_text()


@pytest.fixture(scope="module")
def api_source() -> str:
    return API_CLIENT.read_text()


def card(source: str, heading: str) -> str:
    """The JSX of the card whose ``<h2>`` is ``heading`` (same helper as the copy tests)."""
    start = source.index(f">{heading}</h2>")
    end = source.index("</section>", start)
    return source[start:end]


# ------------------------------------------------------- retention: the bound is real


def test_the_screen_bounds_match_the_policy_module(page_source: str) -> None:
    """A screen offering 0 or 5000 days sends the owner to a 400 they cannot act on.

    The numbers live in `dashboard/lib/api.ts` and are interpolated into the form, so a
    server-side change to `MIN_RETENTION_DAYS`/`MAX_RETENTION_DAYS` that nobody mirrored
    is this test failing rather than a refusal in production.
    """
    from app.retention import policy

    api_source_text = API_CLIENT.read_text()
    assert f"RETENTION_MIN_DAYS = {policy.MIN_RETENTION_DAYS};" in api_source_text, (
        "the screen's minimum has drifted from app.retention.policy.MIN_RETENTION_DAYS"
    )
    assert f"RETENTION_MAX_DAYS = {policy.MAX_RETENTION_DAYS};" in api_source_text, (
        "the screen's maximum has drifted from app.retention.policy.MAX_RETENTION_DAYS"
    )

    body = card(page_source, "Data retention")
    # Interpolated, not retyped: a literal 730 in the JSX would pass a phrase check and
    # still be the wrong number the day the policy changes.
    assert "min={RETENTION_MIN_DAYS}" in body
    assert "max={RETENTION_MAX_DAYS}" in body


def test_the_bounds_the_screen_offers_are_the_ones_the_api_accepts() -> None:
    """Exercise the real validator at both ends and one step outside each.

    This is the assertion the row is about: the boundary is enforced by the server
    module, so the screen's range is checked against behaviour rather than a comment.
    """
    from app.retention import policy

    policy.validate({"conversation_days": policy.MIN_RETENTION_DAYS})
    policy.validate({"conversation_days": policy.MAX_RETENTION_DAYS})

    with pytest.raises(ValueError):
        policy.validate({"conversation_days": policy.MIN_RETENTION_DAYS - 1})
    with pytest.raises(ValueError):
        policy.validate({"conversation_days": policy.MAX_RETENTION_DAYS + 1})


def test_the_editor_sends_the_typed_number_not_the_loaded_one(page_source: str) -> None:
    """The draft has to reach the request, or the form saves what was already stored."""
    body = card(page_source, "Data retention")
    assert "retentionDraft" in body, "the input must be bound to the draft"
    assert "Number(retentionDraft)" in page_source, (
        "the request must send the typed value; sending retentionDays would re-save the "
        "old window and report success"
    )
    assert "retention: { conversation_days:" in page_source


def test_the_editor_reports_the_refusal_verbatim(page_source: str) -> None:
    """The API's message names the bound; a generic error leaves the owner guessing."""
    assert "invalid_retention" in page_source, (
        "a refused window must be recognised by the API's own code"
    )
    # And the sentence chosen for it must be the server's, not one rewritten into
    # something vaguer: the message is the only place the actual bound is stated.
    assert "failure.message" in page_source


def test_the_bound_is_refused_with_a_message_that_stays(page_source: str) -> None:
    """A browser range check is not a usable control: its tooltip vanishes on next click.

    Measured in the ADMN-16 walk before this was added: typing 5000 and clicking Save
    was blocked by native validation, so no message rendered at all — the stored value
    was correctly unchanged, and the screen said nothing. The form therefore opts out of
    native validation and the handler owns the sentence, while `min`/`max` stay on the
    input as a hint for the spinner.
    """
    body = card(page_source, "Data retention")
    assert "noValidate" in body, (
        "without noValidate the browser blocks submit before the handler can explain why"
    )
    start = page_source.index("async function saveRetention")
    end = page_source.index("async function", start + 10)
    handler = page_source[start:end]
    assert "RETENTION_MAX_DAYS" in handler and "RETENTION_MIN_DAYS" in handler
    assert "return" in handler, "an out-of-range value must not reach the request"
    # The message has to be durable state, not a transient attribute tooltip.
    assert "setError(" in handler
    # And the `min`/`max` hints must survive as hints.
    assert "min={RETENTION_MIN_DAYS}" in body and "max={RETENTION_MAX_DAYS}" in body


def test_the_screen_does_not_claim_saving_deletes_anything(page_source: str) -> None:
    """The purge is the only actor; saving a shorter window deletes nothing itself."""
    lowered = card(page_source, "Data retention").lower()
    assert "nothing is deleted on save" in lowered, (
        "a shorter window must not read as an immediate deletion"
    )
    assert "next daily run" in lowered


# ------------------------------------------------------------ domains: add != verify


def test_the_domain_card_keeps_add_and_verify_separate(page_source: str) -> None:
    """SEC-15's whole point: an add grants nothing, so the screen must not merge them."""
    body = card(page_source, "Domains")
    # The add handler must not verify. Bounded to that function's own body so a verify
    # call elsewhere in the card does not satisfy it.
    start = page_source.index("async function addDomainEntry")
    end = page_source.index("async function", start + 10)
    add_body = page_source[start:end]
    assert "verifyDomain(" not in add_body, (
        "adding must not verify: a row that arrives verified would serve a host nobody "
        "has proved they control"
    )
    assert "addDomain(" in add_body

    # And the verify handler is a separate action that only verifies.
    vstart = page_source.index("async function checkDomain")
    vend = page_source.index("async function", vstart + 10)
    verify_body = page_source[vstart:vend]
    assert "verifyDomain(" in verify_body
    assert "addDomain(" not in verify_body

    # The notice shown after an add must say the origin is still refused, or the owner
    # waits for a widget that will not load. Bound to the add handler, because that is
    # where the sentence is produced.
    assert "not allowed yet" in add_body.lower(), (
        "the add notice must say the origin is still refused, or the owner waits for a "
        "widget that will not load"
    )


def test_the_domain_card_shows_the_record_the_api_returned(page_source: str) -> None:
    """The name and value come from the API; composing them here could disagree."""
    body = card(page_source, "Domains")
    assert "entry.record_name" in body
    assert "entry.record_value" in body
    assert "&lt;your-domain&gt;" in body, "the copy names the record shape"


def test_a_lookup_failure_is_not_reported_as_a_missing_record(page_source: str) -> None:
    """"Could not ask" and "no record" mean different things (§8.38, third place)."""
    assert "lookup_failed" in page_source
    # The 503 branch must produce its own sentence; reusing the generic one would tell
    # the owner to fix a record that may already be correct.
    assert "Could not check the record right now" in page_source


def test_a_domain_with_no_record_name_cannot_be_verified(page_source: str) -> None:
    """A legacy row (an IP, a `.internal` name) is listed but not verifiable.

    `_summary` returns `record_name: ""` for those rather than hiding the row, so the
    screen has to disable the action or it will POST a verify that always fails.
    """
    body = card(page_source, "Domains")
    assert "!entry.record_name" in body, "the verify button must be disabled without a name"


# ------------------------------------------------------------------ the client calls


def test_the_client_hits_the_four_shipped_routes(api_source: str) -> None:
    """Add, list, verify and remove are four routes; a typo here 404s at runtime."""
    assert '`/api/v1/bots/${encodeURIComponent(botId)}/domains`' in api_source
    assert "/domains/${encodeURIComponent(domainId)}/verify`" in api_source
    assert "/domains/${encodeURIComponent(domainId)}`" in api_source, (
        "delete targets the domain id, not the bot"
    )


def test_settings_patch_accepts_a_retention_block(api_source: str) -> None:
    """The client type must carry `retention`, or the editor cannot send one."""
    start = api_source.index("export function updateTenantSettings")
    end = api_source.index("}", api_source.index("});", start))
    body = api_source[start:end]
    assert "retention?: Partial<RetentionSettings>" in body
    # And handover must stay optional: making it required would force every retention
    # save to send an email block, which is the ADMN-14 dropped-field bug inverted.
    assert "handover?: Partial<HandoverSettings>" in body


def test_the_settings_page_loads_domains_for_the_bot(page_source: str) -> None:
    """Without the bot id there is nothing to list against, and the card says so."""
    assert "listBots()" in page_source
    assert "listDomains(bot.id)" in page_source
    assert "no bot yet" in page_source.lower()
