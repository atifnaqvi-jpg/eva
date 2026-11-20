"""OPS-13: the §27 checklist is a gate, and a gate that cannot fail is documentation.

The verdict must not live in the probing. ``clauses()`` is a pure function over the
report, so every item's tick can be flipped by mutating one field and re-deciding it
— which is what these tests do. A checklist whose items are "passed" because the
report happens to look plausible is exactly the failure mode this repository keeps
recording (PLAN §8.25, §8.28, §8.48), and a launch checklist is the worst place to
repeat it: a green table here is what a go-live decision would rest on.

No test here touches the live stack. The probes are not exercised; ``clauses`` is.
The live walk is ``docs/LAUNCH-CHECKLIST.md``'s evidence, not this file's.

Three properties are tested, because they fail differently:

1. **Every item can go red.** Each clause gets a report where only its evidence is
   wrong, and the item must be unticked. A clause that cannot fail is not a gate.
2. **A probe error is never a pass.** A section carrying ``probe_error`` must read
   unticked even when its remaining fields would have satisfied the clause — the
   timeout-against-a-saturated-API case is the one that actually happened here.
3. **The published document is the report's.** ``docs/LAUNCH-CHECKLIST.md`` is
   rendered from the recorded JSON; a hand-edit or a stale render is a red test.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "ops13_checklist.py"
REPORT = ROOT / "docs" / "ops" / "launch-checklist.json"
DOC = ROOT / "docs" / "LAUNCH-CHECKLIST.md"
ARCHITECTURE = ROOT / "docs" / "ARCHITECTURE.md"


def _load():
    spec = importlib.util.spec_from_file_location("ops13_checklist", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules["ops13_checklist"] = module
    spec.loader.exec_module(module)
    return module


ops13 = _load()


# --------------------------------------------------------------- a passing report


def passing_report() -> dict:
    """A report in which all fifteen items are genuinely satisfied.

    Written out rather than derived from the live walk: this is the *control*, and a
    control that depends on the stack would make every mutation test below as flaky
    as the stack. The field names are the probes' contract.
    """
    return {
        "started_at": "2026-11-19T00:00:00Z",
        "commit": "0" * 7,
        "host": "test",
        "tests": {
            "isolation": {"returncode": 0, "stdout_tail": "43 passed in 12.29s"},
            "eval": {"returncode": 0, "stdout_tail": "6 passed in 6.32s"},
            "injection": {"returncode": 0, "stdout_tail": "21 passed in 7.01s"},
        },
        "load": {
            "returncode": 0,
            "finals": 50,
            "hung": 0,
            "http_failed": 0,
            "http_total": 150,
            "http_failed_pct": 0.0,
            "scaling": [
                {"concurrency": 1, "median_seconds": 5.4},
                {"concurrency": 5, "median_seconds": 6.0},
                {"concurrency": 50, "median_seconds": 6.5},
            ],
        },
        "backup": {
            "backup_returncode": 0,
            "verify_returncode": 0,
            "drill_returncode": 0,
            "verify": {"verified": True, "tables": 22, "rows": 5338},
            "drill": {
                "ok": True,
                "seconds": 28.15,
                "tables": 22,
                "rows": 5338,
                "revision_restored": "0009_knowledge_set_history",
                "migration_ok": True,
            },
        },
        "purge": {
            "registered": True,
            "inserted_rows": 4,
            "job_returncode": 0,
            "survivors": [
                "ops13-active-old|active",
                "ops13-recent-closed|closed",
            ],
            "receipts": 1,
        },
        "storage": {"returncode": 0, "cases_passed": 2, "cases_failed": 0, "cases_skipped": 0},
        "domain": {
            "verified_origin_status": 200,
            "unverified_origin_status": 403,
            "refusal_code": "origin_not_allowed",
        },
        "limits": {
            "rate_limit": {"tripped": True, "codes": [200] * 10 + [429], "retry_after": "60"},
            "budget": {"status": 200, "refused": True},
        },
        "handover": {
            "handover_status": 201,
            "row": "notified|delivered",
            "mail": {"found": True, "has_transcript": True, "file": "msg-001.eml"},
        },
        "audit": {
            "recorded_actions": {
                "auth.login_succeeded": 1,
                "bot.config_updated": 1,
                "conversation.started": 1,
                "domain.added": 1,
                "knowledge.previewed": 1,
                "knowledge.published": 2,
                "knowledge.rolled_back": 1,
                "tenant.registered": 1,
                "tenant.settings_updated": 1,
                "webhook.created": 1,
            },
            "mutating_modules": {"backend/api/app/knowledge/router.py": True},
            "unrecorded_modules": [],
            "silent_routes": {},
            "required_actions": [
                "knowledge.published",
                "knowledge.rolled_back",
                "knowledge.previewed",
                "conversation.started",
            ],
            "missing_required_actions": [],
        },
        "legal": {
            "documents": {
                "privacy-notice.md": {"bytes": 100, "fill_markers": 0, "generated": True},
                "dpa-skeleton.md": {"bytes": 100, "fill_markers": 0, "generated": True},
                "us-state-privacy-notice-template.md": {
                    "bytes": 100,
                    "fill_markers": 0,
                    "generated": True,
                },
            },
            "disclosure_in_source": True,
            "disclosure_in_bundle": True,
            "runbook": True,
        },
        "alerts": {
            "sweep_returncode": 0,
            "sweep": {"worst": "ok", "sent": [], "readings": 4},
            "alert_email_configured": True,
        },
        "rollback": {
            "runbook_documents_image_rollback": True,
            "runbook_documents_migration_rollback": True,
            "eva_image_tags": ["eva-api:latest"],
        },
        "onboarding": {
            "register": 201,
            "login": 200,
            "bot_exists": True,
            "settings": 200,
            "faq": 201,
            "publish": 200,
            "preview_answered": True,
        },
    }


def test_every_item_ticked_on_a_passing_report():
    """The control: fifteen items, all ticked, so each mutation below is meaningful."""
    results = ops13.clauses(passing_report())
    assert set(results) == {item for item, _ in ops13.ITEMS}
    unticked = {k: v[1] for k, v in results.items() if not v[0]}
    assert unticked == {}


def test_there_are_exactly_fifteen_items_and_they_match_the_blueprint():
    """§27's count and wording, read from ARCHITECTURE.md rather than trusted."""
    spec = ops13.probe_spec()
    assert len(ops13.ITEMS) == 15
    assert spec["copies_found"] >= 1
    assert spec["item_count"] == 15
    assert spec["matches_table"], spec
    assert spec["missing_from_table"] == []
    assert spec["extra_in_table"] == []


def test_the_duplicate_section_27_is_identical():
    """§27 appears twice in ARCHITECTURE.md; both copies must say the same thing.

    Recorded rather than resolved: the document is checksum-locked (G1), so a
    divergence would be a finding for the operator, not something to edit here.
    """
    spec = ops13.probe_spec()
    assert spec["copies_found"] == 2
    assert spec["copies_identical"]


# ------------------------------------------------- each item can independently fail


def _unticked_after(mutate) -> str:
    report = passing_report()
    mutate(report)
    results = ops13.clauses(report)
    reds = [k for k, (ok, _) in results.items() if not ok]
    assert len(reds) == 1, f"expected exactly one red, got {reds}"
    return reds[0]


def test_isolation_goes_red_on_a_failing_suite():
    item = _unticked_after(lambda r: r["tests"]["isolation"].update(returncode=1))
    assert item == "isolation"


def test_eval_goes_red_on_a_failing_suite():
    item = _unticked_after(lambda r: r["tests"]["eval"].update(returncode=1))
    assert item == "eval"


def test_injection_goes_red_on_a_failing_suite():
    item = _unticked_after(lambda r: r["tests"]["injection"].update(returncode=1))
    assert item == "injection"


def test_load_goes_red_when_a_stream_does_not_reach_final():
    item = _unticked_after(lambda r: r["load"].update(finals=16))
    assert item == "load"


def test_load_goes_red_on_http_failures_even_with_every_final():
    """The measured failure: 16 finals and 34 failed requests.

    A run where every stream that *opened* finished but a third of the requests were
    refused is not "no error spike", so the clause must read the request metric too.
    """

    def mutate(report):
        report["load"].update(finals=50, http_failed=34, http_failed_pct=29.31)

    assert _unticked_after(mutate) == "load"


def test_load_goes_red_when_a_stream_hangs_without_final():
    assert _unticked_after(lambda r: r["load"].update(hung=1)) == "load"


def test_backup_goes_red_when_the_drill_fails():
    assert _unticked_after(lambda r: r["backup"]["drill"].update(ok=False)) == "backup"


def test_backup_goes_red_when_the_verify_fails():
    assert _unticked_after(lambda r: r["backup"]["verify"].update(verified=False)) == "backup"


def test_purge_goes_red_when_an_eligible_row_survives():
    """Aged and `closed` must be gone; leaving it is the purge not doing its job."""

    def mutate(report):
        report["purge"]["survivors"].append("ops13-eligible-closed|closed")

    assert _unticked_after(mutate) == "purge"


def test_purge_goes_red_when_it_deletes_a_row_it_should_keep():
    """The direction that harms a visitor rather than the operator.

    Deleting an `active` conversation makes the purge look *more* effective, so a
    clause that only asserted "something was deleted" would stay green.
    """

    def mutate(report):
        report["purge"]["survivors"] = ["ops13-recent-closed|closed"]

    assert _unticked_after(mutate) == "purge"


def test_storage_goes_red_on_a_skipped_case():
    """A skip is not evidence: 0 passed / 0 failed is how the first run read green."""
    assert _unticked_after(lambda r: r["storage"].update(cases_passed=0, cases_skipped=2)) == "storage"


def test_storage_goes_red_on_one_skip_even_though_both_cases_were_reported():
    """Two cases reported, one skipped: only one is evidence.

    Both fields stay in a range the *other* two guards accept, so this case fails
    only if the ``cases_skipped`` guard exists. The earlier versions each moved a
    field that another guard also caught, which is why both mutations survived.
    """
    assert _unticked_after(lambda r: r["storage"].update(cases_passed=2, cases_skipped=1)) == "storage"


def test_storage_goes_red_when_only_one_case_ran():
    """One passing case is half the audit; the second case may have silently vanished."""
    assert _unticked_after(lambda r: r["storage"].update(cases_passed=1, cases_skipped=0)) == "storage"


def test_domain_goes_red_when_an_unverified_origin_is_allowed():
    """Driven through the session endpoint, so the row cannot stand in for the filter."""
    assert _unticked_after(lambda r: r["domain"].update(unverified_origin_status=200)) == "domain"


def test_domain_goes_red_when_the_verified_origin_is_refused():
    """The other half: a filter so strict it refuses the owner's own site is also broken.

    Without this case the ``verified_origin_status == 200`` half of the clause could
    be deleted and every test stayed green (found by mutation).
    """
    assert _unticked_after(lambda r: r["domain"].update(verified_origin_status=403)) == "domain"


def test_limits_go_red_when_the_rate_limit_does_not_trip():
    assert _unticked_after(lambda r: r["limits"]["rate_limit"].update(tripped=False)) == "limits"


def test_limits_go_red_when_the_budget_is_not_enforced():
    assert _unticked_after(lambda r: r["limits"]["budget"].update(refused=False)) == "limits"


def test_handover_goes_red_when_the_mail_carries_no_transcript():
    """The row saying `notified` is not the claim; the transcript in the mail is."""
    assert (
        _unticked_after(lambda r: r["handover"]["mail"].update(has_transcript=False))
        == "handover"
    )


def test_handover_goes_red_when_no_mail_was_found():
    assert _unticked_after(lambda r: r["handover"]["mail"].update(found=False)) == "handover"


def test_audit_goes_red_when_a_mutating_module_records_nothing():
    """The live finding: knowledge mutations write no audit row."""
    assert (
        _unticked_after(
            lambda r: r["audit"].update(unrecorded_modules=["backend/api/app/knowledge/router.py"])
        )
        == "audit"
    )


def test_audit_goes_red_when_the_walk_recorded_too_few_actions():
    assert _unticked_after(lambda r: r["audit"].update(recorded_actions={"tenant.registered": 1})) == "audit"


def test_audit_goes_red_when_one_required_action_was_never_observed():
    """The defect OPS-15 exists for, asserted as itself.

    A count threshold was satisfied by the auth and config rows while publish,
    rollback, preview and conversation recorded nothing — so the clause has to name
    the four, one at a time. Each case removes exactly one, which also proves the
    ``len(actions) >= 4`` floor is not what ticks the item.
    """
    for action in (
        "knowledge.published",
        "knowledge.rolled_back",
        "knowledge.previewed",
        "conversation.started",
    ):
        def mutate(report, action=action):
            report["audit"]["recorded_actions"].pop(action)

        assert _unticked_after(mutate) == "audit", action


def test_audit_evidence_discloses_a_silent_route_inside_a_covered_module():
    """Module-level coverage is not route-level coverage, and the item says "all".

    OPS-15's authorised unit is the module, and closing the routes still silent in
    ``knowledge/router.py`` and ``conversation/router.py`` needs action names the row
    does not grant. So the tick stands and the *evidence line* carries the debt — a
    green row that hides a known silent route would be the "gate that cannot see it"
    this checklist exists to avoid. Falsifiable: delete the ``silent_note`` and this
    fails, while every other audit case stays green.
    """
    report = passing_report()
    report["audit"]["silent_routes"] = {
        "backend/api/app/knowledge/router.py": ["create_faq", "upload"],
    }
    ok, reason = ops13.clauses(report)["audit"]
    assert ok is True
    assert "routes still silent" in reason
    assert "create_faq" in reason and "upload" in reason


def test_legal_goes_red_when_the_disclosure_is_missing_from_the_bundle():
    """In the source but not in the shipped bundle is not disclosed to a visitor."""
    assert _unticked_after(lambda r: r["legal"].update(disclosure_in_bundle=False)) == "legal"


def test_legal_goes_red_while_fill_markers_remain():
    """`[[FILL: ...]]` markers are the operator's; a template is not published until they go."""

    def mutate(report):
        report["legal"]["documents"]["privacy-notice.md"]["fill_markers"] = 19

    assert _unticked_after(mutate) == "legal"


def test_alerts_go_red_without_a_monitored_address():
    """The measured state: the sweep evaluates, `ALERT_EMAIL` is unset, nothing is sent."""
    assert _unticked_after(lambda r: r["alerts"].update(alert_email_configured=False)) == "alerts"


def test_alerts_go_red_when_the_sweep_did_not_run():
    assert _unticked_after(lambda r: r["alerts"].update(sweep=None)) == "alerts"


def test_rollback_goes_red_when_the_migration_half_was_not_tested():
    assert _unticked_after(lambda r: r["backup"]["drill"].update(migration_ok=False)) == "rollback"


def test_rollback_goes_red_when_the_image_half_is_not_documented():
    assert (
        _unticked_after(lambda r: r["rollback"].update(runbook_documents_image_rollback=False))
        == "rollback"
    )


def test_onboarding_goes_red_when_the_path_cannot_be_walked():
    assert _unticked_after(lambda r: r["onboarding"].update(publish=422)) == "onboarding"


def test_onboarding_goes_red_when_the_answer_is_not_grounded():
    assert _unticked_after(lambda r: r["onboarding"].update(preview_answered=False)) == "onboarding"


# -------------------------------------------------- an error is never a pass


@pytest.mark.parametrize(
    "section, item",
    [
        ("tests", "isolation"),
        ("tests", "eval"),
        ("tests", "injection"),
        ("load", "load"),
        ("backup", "backup"),
        ("purge", "purge"),
        ("storage", "storage"),
        ("domain", "domain"),
        ("limits", "limits"),
        ("handover", "handover"),
        ("audit", "audit"),
        ("legal", "legal"),
        ("alerts", "alerts"),
        ("rollback", "rollback"),
        ("onboarding", "onboarding"),
    ],
)
def test_a_probe_error_is_never_a_pass(section, item):
    """The saturated-API case: the probe timed out, so the item has no evidence.

    Every section is checked, because the guard is easy to add to one clause and
    forget in the next — and the forgotten one is the item that would read green on
    no evidence at all.
    """
    report = passing_report()
    report[section] = {"probe_error": "ReadTimeout: timed out"}
    results = ops13.clauses(report)
    assert not results[item][0]
    assert "probe could not complete" in results[item][1]


def test_a_missing_section_is_unticked_not_ticked():
    """Absence of a probe is absence of evidence, never a default pass."""
    report = passing_report()
    del report["handover"]
    assert not ops13.clauses(report)["handover"][0]


def test_verdict_reports_the_counts_it_observed():
    report = passing_report()
    report["domain"]["unverified_origin_status"] = 200
    summary = ops13.verdict(ops13.clauses(report))
    assert summary["ticked"] == 14
    assert summary["unticked"] == 1
    assert summary["clean"] is False
    assert summary["unticked_ids"] == ["domain"]


# ------------------------------------------------------- the published document


def test_the_published_checklist_matches_its_recorded_report():
    """The Markdown is rendered from the JSON, so a stale render is a red test.

    This is SEC-11's rule for the compliance documents applied here: if a person can
    hand-edit the table, the table is not evidence.
    """
    if not REPORT.is_file() or not DOC.is_file():
        pytest.skip("the OPS-13 walk has not been recorded yet")
    report = json.loads(REPORT.read_text())
    assert DOC.read_text() == ops13.render_markdown(report)


def test_the_published_checklist_names_every_item():
    if not DOC.is_file():
        pytest.skip("the OPS-13 walk has not been recorded yet")
    text = DOC.read_text()
    for index, (_, item) in enumerate(ops13.ITEMS, start=1):
        assert item in text, f"item {index} missing from the published checklist"


def test_the_published_checklist_records_a_reason_for_every_unticked_item():
    """An unticked row without a reason is the row a reader cannot act on."""
    if not DOC.is_file():
        pytest.skip("the OPS-13 walk has not been recorded yet")
    report = json.loads(REPORT.read_text())
    results = ops13.clauses(report)
    for item_id, (ok, reason) in results.items():
        if not ok:
            assert reason.strip(), f"{item_id} is unticked with no reason"


def test_render_refuses_to_claim_a_pass_from_a_partial_report():
    """A report with one item's probe missing renders that row unticked."""
    report = passing_report()
    report["storage"] = {"probe_error": "not run"}
    markdown = ops13.render_markdown(report)
    assert "⬜" in markdown
    assert "could not be ticked" in markdown


def test_the_published_checklist_carries_no_machine_topology():
    """The report is committed, so it must not name the box it ran on.

    The confidentiality rule forbids machine-internal topology in commits. The walk
    records a commit sha and a timestamp, which is what identifies it for a reader;
    the hostname was the one field that violated this and it is asserted absent.
    """
    for path in (DOC, REPORT):
        if not path.is_file():
            pytest.skip(f"{path.name} has not been recorded yet")
        text = path.read_text()
        assert os.uname().nodename not in text
        assert '"host": "local"' in text or path is DOC


def test_architecture_and_stack_are_not_written_by_this_row():
    """G1: the locked docs are checksum-guarded by ``tests/test_locked_docs.py``.

    This row *reads* §27 out of ARCHITECTURE.md. A checklist that rewrote the spec
    it is checking would always agree with itself, so the script is asserted to
    contain no write call targeting either locked document.
    """
    assert (ROOT / "tests" / "test_locked_docs.py").is_file()
    script = SCRIPT.read_text()
    for locked in ("ARCHITECTURE.md", "STACK.md"):
        assert f'{locked}").write_text' not in script
        assert f"{locked}', 'w'" not in script
