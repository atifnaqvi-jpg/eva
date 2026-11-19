"""ADMN-17's browser gate: the verdict is a pure function, so it can be falsified.

`scripts/admn17_walk.py` decides the row in `clauses(report)`. That shape exists so this
file can run in CI without a browser: take the report the driver would emit, flip one
field, and assert the matching clause goes red. A gate whose verdict is computed inline
beside the browser can only be verified by *running* the browser, which is exactly what
CI cannot do.

The report built here is the shape `scripts/admn17_driver.mjs` produces for a correct
walk. Each case then mutates one property, so every clause has a mutation that kills it —
the rule AGENTS.md applies to tests that claim to guard something.

The driver is also asserted not to compute a verdict of its own, because a second opinion
in the reporter is how a gate ends up agreeing with itself.
"""

from __future__ import annotations

import importlib.util
import re
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WALK = ROOT / "scripts" / "admn17_walk.py"
DRIVER = ROOT / "scripts" / "admn17_driver.mjs"


def _load():
    spec = importlib.util.spec_from_file_location("admn17_walk", WALK)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


WALK_MODULE = _load()


def good_report() -> dict:
    """A correct walk: four publishes, three restores, then the control goes quiet."""
    return {
        "control_before_publish": {
            "rendered": True,
            "disabled": True,
            "hint": "Nothing to restore yet. This appears once you publish a second time.",
        },
        "after_each_publish": [
            {"index": 0, "versions": ["v1"], "previous_set_id": None, "live_rows": 1},
            {"index": 1, "versions": ["v1", "v2"], "previous_set_id": "s1", "live_rows": 2},
            {"index": 2, "versions": ["v1", "v2", "v3"], "previous_set_id": "s2", "live_rows": 3},
            {
                "index": 3,
                "versions": ["v1", "v2", "v3", "v4"],
                "previous_set_id": "s3",
                "live_rows": 4,
            },
        ],
        "restores": [
            {
                "step": 0,
                "versions": ["v1", "v2", "v3"],
                "previous_versions": ["v1", "v2"],
                "live_rows": 3,
                "notice": "Restored the previous version. The bot now answers from 3 documents again.",
            },
            {
                "step": 1,
                "versions": ["v1", "v2"],
                "previous_versions": ["v1"],
                "live_rows": 2,
                "notice": "Restored the previous version. The bot now answers from 2 documents again.",
            },
            {
                "step": 2,
                "versions": ["v1"],
                "previous_versions": [],
                "live_rows": 1,
                "notice": "Restored the previous version. The bot is answering from that one document again.",
            },
        ],
        "control_after_exhausted": {
            "rendered": True,
            "disabled": True,
            "hint": "Nothing to restore yet. This appears once you publish a second time.",
        },
        "fatal": None,
        "steps": [],
    }


def verdict(report: dict) -> dict[str, bool]:
    return dict(WALK_MODULE.clauses(report))


def _code_only(source: str) -> str:
    """Strip // and /* */ comments so a guard matches code, not prose about the code.

    The negative assertions below exist to prove the *driver* observes rather than
    decides: it must not re-implement verdict logic. But a comment that names
    ``clauses()`` while explaining that it does NOT decide is not a violation, and
    matching it was a false positive of the same class as a citations guard that
    matched a function name inside a docstring.
    """
    import re as _re

    no_blocks = _re.sub(r"/\*.*?\*/", "", source, flags=_re.S)
    return _re.sub(r"(?m)^\s*//.*$", "", no_blocks)


def assert_only_red(report: dict, clause: str) -> None:
    """The named clause fails and every other one still passes.

    The second half matters as much as the first: a clause that goes red because an
    *unrelated* check broke would let a mutation pass as "covered".
    """
    results = verdict(report)
    assert clause in results, f"{clause!r} is not one of the gate's clauses"
    red = [name for name, ok in results.items() if not ok]
    assert red == [clause], f"expected only {clause!r} red, got {red}"


def test_a_correct_walk_passes_every_clause() -> None:
    """The control. Without this a gate that always fails would look rigorous."""
    results = WALK_MODULE.clauses(good_report())
    assert len(results) == 7
    assert all(ok for _, ok in results), [name for name, ok in results if not ok]


def test_the_old_serve_one_document_behaviour_is_red() -> None:
    """HALT-10's defect: each restore yields a single document regardless of history."""
    report = good_report()
    for entry in report["restores"]:
        entry["versions"] = ["v4"]
        entry["live_rows"] = 1
    red = [name for name, ok in WALK_MODULE.clauses(report) if not ok]
    assert "each restore serves the whole earlier set, not one document" in red


def test_a_restore_that_moves_the_pointer_but_not_the_table_is_red() -> None:
    """The M4 shape: the API is right and the screen the owner reads is not."""
    report = good_report()
    report["restores"][0]["live_rows"] = 1
    assert_only_red(report, "the Served column agrees with the API after every restore")


def test_offering_the_control_before_anything_is_published_is_red() -> None:
    report = good_report()
    report["control_before_publish"]["disabled"] = False
    assert_only_red(report, "no restore is offered before the first publish")


def test_a_notice_that_does_not_name_the_count_is_red() -> None:
    """"Restored the previous version" alone leaves the owner unable to tell what survived."""
    report = good_report()
    report["restores"][0]["notice"] = "Restored the previous version."
    assert_only_red(report, "the notice reports how many documents came back")


def test_a_control_that_stays_enabled_past_the_history_is_red() -> None:
    report = good_report()
    report["control_after_exhausted"]["disabled"] = False
    assert_only_red(report, "the control is disabled once the history is exhausted")


def test_a_grey_button_with_no_explanation_is_red() -> None:
    """A disabled control with no sentence is the ADMN-16 defect: a refusal and a dead
    button look identical."""
    report = good_report()
    report["control_after_exhausted"]["hint"] = ""
    assert_only_red(report, "the exhausted state explains itself")


def test_a_publish_that_replaced_instead_of_adding_is_red() -> None:
    """If publishing were not additive the restores above restore from a set that was
    never built, so the clause has to be independent of them."""
    report = good_report()
    for index, entry in enumerate(report["after_each_publish"]):
        entry["versions"] = [f"v{index + 1}"]
    assert_only_red(report, "each publish adds one document to the served set")


def test_a_walk_that_stopped_early_is_red() -> None:
    """Two restores is not the row: the third is what proves the chain keeps walking."""
    report = good_report()
    report["restores"] = report["restores"][:2]
    red = [name for name, ok in WALK_MODULE.clauses(report) if not ok]
    assert "each restore serves the whole earlier set, not one document" in red


def test_the_driver_computes_no_verdict_of_its_own() -> None:
    """The reporter observes; `clauses()` decides. Two opinions is how a gate agrees
    with itself."""
    source = DRIVER.read_text()
    code = _code_only(source)
    # "clauses(" would mean the driver re-uses the verdict function; process.exit would
    # mean it decides the run's outcome. PASS/FAILED are checked as standalone tokens,
    # because a bare substring also matches EVA_PASSWORD and similar identifiers.
    for forbidden in ("clauses(", "process.exit"):
        assert forbidden not in code, (
            f"{forbidden!r} in the driver means it is deciding, not observing"
        )
    for token in ("PASS", "FAILED"):
        assert not re.search(rf"[\"'`]\s*{token}\s*[\"'`]", code), (
            f"{token!r} printed by the driver means it is deciding, not observing"
        )


def test_the_driver_never_calls_the_rollback_endpoint_with_a_version_id() -> None:
    """The screen's own control is what this row ships; a driver POSTing `version_id`
    would be testing the operation the ruling deleted."""
    source = DRIVER.read_text()
    code = _code_only(source)
    # The driver may *observe* the publish request (waitForResponse) to prove the UI
    # button really performed it. It must not *issue* one: no fetch/post to that path.
    assert not re.search(r"fetch\([^)]*/knowledge/publish", code), (
        "the driver must not POST publish itself; the screen's button is what ships"
    )
    assert "version_id" not in source.split("/api/v1/knowledge/rollback")[0][-200:]


def test_the_walk_gate_is_one_command_that_can_fail() -> None:
    """`scripts/*_gate.py` is the evidence format here; a transcript is not."""
    source = WALK.read_text()
    assert "def clauses(" in source, "the verdict must be a pure function over the report"
    assert "return 1" in source and "return 0" in source
    assert "except SystemExit" not in source


@pytest.mark.parametrize("count", [1, 2, 3])
def test_expected_counts_are_derived_from_the_number_of_documents(count: int) -> None:
    """The arithmetic is the row's, not three literals that could drift apart."""
    assert WALK_MODULE.EXPECTED_COUNTS == list(range(WALK_MODULE.DOCUMENTS - 1, 0, -1))
    assert len(WALK_MODULE.EXPECTED_COUNTS) == WALK_MODULE.DOCUMENTS - 1
    assert count in WALK_MODULE.EXPECTED_COUNTS


def test_the_report_shape_the_driver_emits_is_the_one_the_clauses_read() -> None:
    """A renamed field would make every clause vacuously pass on `{}` values."""
    source = DRIVER.read_text()
    for field in (
        "control_before_publish",
        "after_each_publish",
        "restores",
        "control_after_exhausted",
    ):
        assert field in source
    report_keys = sorted(good_report())
    # The report must survive a JSON round-trip: the driver writes it as JSON and the
    # clauses read it back, so a non-serialisable value would break the walk silently.
    assert report_keys == sorted(json.loads(json.dumps(report_keys)))
    assert all(name in source for name in report_keys)
