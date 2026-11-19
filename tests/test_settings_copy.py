"""ADMN-14: the settings screen's retention and domain copy must match the code.

The row exists because the screen kept telling owners that retention and domain
verification were "not shipped, deliberately" after both had shipped (SEC-16's purge,
SEC-15's verification). A sentence about another component is the kind of claim that
goes stale silently: nothing fails, and the page keeps asserting something a tenant
would repeat to their own customers.

Two failure modes are guarded here, and the second is the one that survived the first
correction:

* **A claim that a shipped mechanism does not exist.** Asserted by forbidding the
  phrases on the rendered card, so re-introducing the old wording is red.
* **A number the screen never read.** The card hardcoded "90 days unless you change
  it" while the window is per-tenant (1–730) and the client's `TenantSettings` type
  dropped `retention` entirely, so the screen *could not* read it. The test asserts the
  card interpolates the API's value and that the client type still carries the field —
  a hardcoded number would pass a phrase check and still be wrong for every tenant that
  changed theirs.

The assertions read the source rather than a rendered DOM because these are copy
guards, not behaviour: the browser pass for this screen is ADMN-10's gate.
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
    """The JSX of the card whose ``<h2>`` is ``heading``.

    Slicing to the next ``</section>`` keeps an assertion bound to the card it names:
    a phrase that moved to a different card is a different claim, and a whole-file
    search would not notice.
    """
    start = source.index(f">{heading}</h2>")
    end = source.index("</section>", start)
    return source[start:end]


# --------------------------------------------------------------- retention: the number


def test_the_retention_card_states_the_window_it_read_from_the_api(
    page_source: str,
) -> None:
    """The window is per-tenant, so the card must interpolate it, not print a default."""
    body = card(page_source, "Data retention")
    assert "${retentionDays}" in body, (
        "the retention card must print the tenant's own window; a literal number here "
        "is wrong for every tenant that changed theirs"
    )
    # The value has to come from the settings response, not from a local constant.
    assert "setRetentionDays(tenant.retention.conversation_days)" in page_source


def test_the_retention_card_does_not_hardcode_the_default_window(page_source: str) -> None:
    """90 is the default, not the value: a tenant on 7 days must not read 90."""
    body = card(page_source, "Data retention")
    assert "90 days unless you change it" not in body
    assert not re.search(r"\b90 days?\b", body), (
        "a hardcoded 90 is the default only; state the value read from the API"
    )


def test_the_client_type_still_carries_the_retention_block(api_source: str) -> None:
    """Dropping the field from the type is what made the number unreadable."""
    assert "export type RetentionSettings = { conversation_days: number };" in api_source
    assert "retention: RetentionSettings;" in api_source


def test_the_card_says_which_conversations_are_eligible(page_source: str) -> None:
    """'Old conversations are deleted' overstates it: active ones are never purged.

    The purge reads ``ELIGIBLE_STATUSES = ("closed", "handed_over")`` and ages on
    ``last_activity_at``, so a card promising deletion by age alone would be false for
    a conversation someone left open.
    """
    body = card(page_source, "Data retention")
    lowered = body.lower()
    # "finished" specifically, not "finished or closed": the card uses the owner's word,
    # and an `or` lets a mutation that says only "old conversations" pass.
    assert "finished" in lowered
    assert "never" in lowered, "the card must say an open conversation is never purged"
    assert "last activity" in lowered, (
        "the clock runs from last_activity_at, not from creation"
    )


# ------------------------------------------------------------------ retention: shipped


def test_the_retention_card_no_longer_claims_nothing_deletes(page_source: str) -> None:
    """The exact sentence SEC-09 falsified, and its paraphrases."""
    # Lowercased: the first version of this test compared case-sensitively and let a
    # re-introduced "Not configurable yet." through with a capital N. A stale-phrase
    # guard that only catches one capitalisation does not guard anything.
    body = card(page_source, "Data retention").lower()
    for stale in (
        "nothing in the deployment deletes",
        "kept until that is built",
        "not configurable yet",
    ):
        assert stale not in body, f"stale claim survived: {stale!r}"


def test_the_card_names_the_purge_as_scheduled(page_source: str) -> None:
    """SEC-09's job exists; the card must not hide it behind a vague promise.

    "Daily job" specifically, not just the word "schedule": the sentence opens with
    "deleted on a schedule", so a check for "schedule" stayed green when the actual job
    — the thing that makes the promise true — was deleted from the second clause.
    """
    body = card(page_source, "Data retention").lower()
    assert "daily" in body and "job" in body
    assert "purge" in body


# --------------------------------------------------------------------- domains: shipped


def test_the_domains_card_describes_the_shipped_mechanism(page_source: str) -> None:
    """SEC-15 built add + verify; HALT-06's 'no mechanism chosen' is now false."""
    body = card(page_source, "Domains")
    assert "_eva-verify." in body, "the record name the API returns must be named"
    assert "HALT-06" not in body, (
        "a halt id is platform history, not something an owner can act on"
    )


def test_the_domains_card_says_adding_alone_allows_nothing(page_source: str) -> None:
    """The ruling's whole point: add and verify are separate, and add grants nothing."""
    lowered = card(page_source, "Domains").lower()
    assert "verify" in lowered
    assert "adding a domain on its own never allows it" in lowered


def test_the_domains_card_names_the_real_api_path(page_source: str) -> None:
    """A pointer to a path that does not exist would send the owner nowhere.

    Asserted as the full ``/api/v1/bots/{id}/domains`` shape rather than as
    ``/api/v1/bots/`` plus ``/domains`` in two separate checks: the first version did
    the latter and a mutation to ``/api/v1/bots/domains`` — a path with no bot id, which
    is not a route — kept the suite green.
    """
    body = card(page_source, "Domains")
    assert "/api/v1/bots/&#123;id&#125;/domains" in body


def test_the_domains_card_does_not_call_verification_unbuilt(page_source: str) -> None:
    body = card(page_source, "Domains")
    for stale in (
        "no mechanism for verifying",
        "has not been chosen",
        "refuses every origin",
        "not available yet",
    ):
        assert stale not in body, f"stale claim survived: {stale!r}"


# ------------------------------------------------------------------------- the drift


def test_the_record_name_in_the_copy_matches_the_constant(page_source: str) -> None:
    """The screen quotes `_eva-verify.`; ``RECORD_PREFIX`` is the authority.

    A renamed prefix would leave the copy describing a record nobody can publish, and
    the domain would never verify — a support ticket rather than a test failure.
    """
    from app.widget import domains

    assert f"{domains.RECORD_PREFIX}." in card(page_source, "Domains")


def test_the_erasure_claim_says_erasure_needs_the_api(page_source: str) -> None:
    """§19.3's data-subject right exists, but not on this screen.

    The card's erasure sentence is the one a tenant would repeat to a data subject, so
    it must say where the action happens. A card that listed erasure among this page's
    features would be advertising a control the screen does not have — the ADMN-10
    defect (a BYOK list telling the owner to add a key with no way to add one) in a
    compliance sentence.
    """
    lowered = card(page_source, "Data retention").lower()
    assert "eras" in lowered, "the erasure route exists and the card must not omit it"
    # Bound to the erasure sentence itself, not to the card: the card's *closing*
    # sentence also says "through the API" (it explains the window), so a whole-card
    # check stayed green when the erasure half stopped saying it. A guard that can be
    # satisfied by a different sentence is not guarding this one.
    erasure = next(
        (sentence for sentence in lowered.split(".") if "eras" in sentence), ""
    )
    assert "through the api" in erasure, (
        "erasure is an API operation; the sentence describing it must say so"
    )


def test_the_copy_does_not_promise_a_control_the_screen_lacks(page_source: str) -> None:
    """Both cards must say the control is elsewhere, since neither has one.

    Asserted as "no control" rather than as "no control *or* through the API": the
    API pointer is in both cards already, so the ``or`` let a card that advertised a
    form pass on its second half. Saying where the control *is* does not stop a card
    from also implying it is here.
    """
    for heading, noun in (("Data retention", "window"), ("Domains", "domain")):
        lowered = card(page_source, heading).lower()
        assert "no control" in lowered, (
            f"{heading} advertises a control this screen does not have"
        )
        assert noun in lowered
