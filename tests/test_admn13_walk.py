"""ADMN-13: the multi-document walk's verdict has to be able to say no.

The walk itself is a browser running against a live stack (``scripts/admn13_walk.py``
drives ``scripts/admn13_driver.mjs``); that cannot run in CI, which has no stack. What
*can* run here is the half that decides the verdict: ``clauses()`` is a pure function
over the driver's report, so every clause is exercised by handing it a report and
mutating exactly one field.

That is the point of this file. §8.25 and §8.32 both record a gate that passed because
nothing it inspected could fail, and a verification row is the easiest place in this
repository to write one: a clause like "both documents answer" also holds when the
preview ignores its scope and searches everything. The mutations below are the ones
that would make this row's evidence vacuous if they stayed green.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "admn13_walk.py"
DRIVER = ROOT / "scripts" / "admn13_driver.mjs"

DRAFT_ONLY_LIVE = "Nothing is published yet, so visitors are answered from the greeting only."


def _load():
    name = "eva_admn13_walk"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


walk = _load()

A_Q, A_A = walk.QUESTION_A, walk.ANSWER_A
B_Q, B_A = walk.QUESTION_B, walk.ANSWER_B


def _column(*answers: str) -> str:
    # The shape the screen renders: the heading, then one "<score> Q: ... A: ..." per hit.
    return "Draft (everything indexed)\n" if not answers else (
        "hits\n" + "\n".join(f"0.7000 Q: x A: {a} — title" for a in answers)
    )


def report() -> dict:
    """A report shaped exactly like the one the driver printed on a passing walk."""
    both = _column(A_A, B_A)
    only_a = _column(A_A)
    only_b = _column(B_A)
    return {
        "steps": [{"name": "sign up", "detail": "landed on /conversations"}],
        "errors": [],
        "rows_before_publish": [
            {"title": A_Q, "served": "Make live"},
            {"title": B_Q, "served": "Make live"},
        ],
        "probe_draft_only": {"question": B_Q, "draft": both, "live": DRAFT_ONLY_LIVE, "responses": 2},
        "publish_1": {"notice": f"“{A_Q}” is live. Visitors can be answered from it now.", "rows": []},
        "probe_a_only": {"question": A_Q, "draft": both, "live": only_a, "responses": 2},
        "probe_b_after_a": {"question": B_Q, "draft": both, "live": only_a, "responses": 2},
        "publish_2": {
            "notice": f"“{B_Q}” is live. The bot now answers from 2 documents.",
            "rows": [],
        },
        "rows_after_publish_2": [
            {"title": A_Q, "served": "live"},
            {"title": B_Q, "served": "live"},
        ],
        "probe_a_after_b": {"question": A_Q, "draft": both, "live": both, "responses": 2},
        "probe_b_after_b": {"question": B_Q, "draft": both, "live": both, "responses": 2},
        "rollback": {"status": 200, "body": "{}"},
        "rows_after_rollback": [
            {"title": A_Q, "served": "live"},
            {"title": B_Q, "served": "Make live"},
        ],
        "probe_a_after_rollback": {"question": A_Q, "draft": both, "live": only_a, "responses": 2},
        "probe_b_after_rollback": {"question": B_Q, "draft": both, "live": only_a, "responses": 2},
        "served_counts": {"before_publish": 0, "after_publish_2": 2, "after_rollback": 1},
    }


def verdict(data: dict) -> dict[str, bool]:
    return dict(walk.clauses(data))


# --- the passing report ----------------------------------------------------


def test_the_observed_walk_satisfies_every_clause() -> None:
    results = verdict(report())
    assert len(results) == 7, "a clause was added or removed without a mutation below"
    assert all(results.values()), [name for name, ok in results.items() if not ok]


def test_every_clause_has_a_mutation_that_makes_it_fail() -> None:
    """The floor: no clause may be unreachable.

    One mutation per clause, each applied to a fresh copy. If a clause cannot be made
    to fail it is documentation, not evidence.
    """

    mutations = {
        "two documents were added and both are listed": lambda r: r.__setitem__(
            "rows_before_publish", r["rows_before_publish"][:1]
        ),
        "nothing is published at the start, and the screen says so": lambda r: r[
            "probe_draft_only"
        ].__setitem__("live", _column(A_A, B_A)),
        "publishing the first document serves it and not the other": lambda r: r[
            "probe_b_after_a"
        ].__setitem__("live", _column(A_A, B_A)),
        "publishing a second document adds it to the served set": lambda r: r[
            "probe_b_after_b"
        ].__setitem__("live", _column(A_A)),
        "both documents are marked live in the table after a reload": lambda r: r[
            "served_counts"
        ].__setitem__("after_publish_2", 1),
        "the publish notice reports how many documents are served": lambda r: r[
            "publish_2"
        ].__setitem__("notice", f"“{B_Q}” is live. Visitors can be answered from it now."),
        "rollback re-points the served set to one document": lambda r: r[
            "served_counts"
        ].__setitem__("after_rollback", 2),
    }
    names = set(verdict(report()))
    assert set(mutations) == names, "a clause has no mutation, or a mutation names no clause"

    for clause, mutate in mutations.items():
        data = copy.deepcopy(report())
        mutate(data)
        results = verdict(data)
        assert results[clause] is False, f"mutation did not falsify: {clause}"
        failed = [name for name, ok in results.items() if not ok]
        assert failed == [clause], f"{clause} mutation also moved {failed}"


# --- the traps this row could have walked into -----------------------------


def test_a_scope_blind_live_column_fails_the_walk() -> None:
    """The failure this row is exposed to: "live" that is really "everything".

    Both preview columns returning every indexed chunk passes "both documents answer",
    so that clause alone proves nothing. With one document published the draft knows
    both and the live column must not — this mutation is what makes the difference
    between "the set model works" and "the preview ignores its scope".
    """
    data = report()
    data["probe_a_only"]["live"] = _column(A_A, B_A)
    data["probe_b_after_a"]["live"] = _column(A_A, B_A)
    results = verdict(data)
    assert results["publishing the first document serves it and not the other"] is False
    # and the "both live" clause is exactly the one that still passes, which is why
    # it could not carry the row on its own
    assert results["publishing a second document adds it to the served set"] is True


def test_publishing_b_that_retires_a_is_red() -> None:
    """The single-pointer defect (PLAN §8.22) reappearing, in the one place that saw it.

    Under the pre-RAG-11 model, publishing B made A unreachable. A walk that reports
    green on that is the row's whole purpose defeated.
    """
    data = report()
    data["probe_a_after_b"]["live"] = _column(B_A)
    results = verdict(data)
    assert results["publishing a second document adds it to the served set"] is False


def test_the_served_column_alone_is_not_enough_without_the_live_column() -> None:
    """Two badges and no retrieval would pass a table-only check, and prove nothing.

    The badges come from `active_knowledge_version_ids`; answering is what the set is
    for. Dropping the preview answers while keeping both `live` badges must be red.
    """
    data = report()
    data["probe_a_after_b"]["live"] = ""
    data["probe_b_after_b"]["live"] = ""
    results = verdict(data)
    assert results["publishing a second document adds it to the served set"] is False
    assert results["both documents are marked live in the table after a reload"] is True


def test_a_rollback_that_changed_nothing_is_red() -> None:
    data = report()
    data["rollback"] = {"status": 500, "body": "boom"}
    assert verdict(data)["rollback re-points the served set to one document"] is False


def test_the_gate_fails_loudly_on_a_fatal_driver_report(monkeypatch, capsys) -> None:
    """A browser that stopped early must not be reported as clauses that "failed": the
    operator needs to know the walk did not finish."""
    monkeypatch.setattr(walk, "drive", lambda: {"fatal": "TimeoutError: waiting for locator"})
    assert walk.main() == 1
    assert "the browser stopped at" in capsys.readouterr().out


def test_uncaught_js_errors_fail_the_gate(monkeypatch, capsys) -> None:
    """A page error is a regression even when every clause happens to hold — the
    screens are part of what this row verifies."""
    data = report()
    data["errors"] = ["TypeError: cannot read properties of null"]
    monkeypatch.setattr(walk, "drive", lambda: data)
    assert walk.main() == 1
    assert "uncaught JS errors" in capsys.readouterr().out


def test_the_passing_report_exits_zero(monkeypatch, capsys) -> None:
    monkeypatch.setattr(walk, "drive", report)
    assert walk.main() == 0
    out = capsys.readouterr().out
    assert "ADMN-13: every clause observed" in out
    assert "[FAIL]" not in out


# --- the driver must walk the screen, not the API --------------------------


def test_publish_is_driven_through_the_screen() -> None:
    """The row is a *UI* verification, so publish must be a click on "Make live".

    A rollback POST is expected and stated (the screen has no such control), but if
    publishing went through the API the walk would prove the endpoint the in-process
    suite already covers and nothing about the screen.
    """
    source = DRIVER.read_text()
    assert 'name: "Make live"' in source, "publish must click the screen's own control"
    assert "/api/v1/knowledge/publish" not in source, "publishing through the API would not be a UI walk"
    assert source.count("/api/v1/knowledge/rollback") == 1, (
        "rollback is the one API call, and it is the documented gap"
    )


def test_the_walk_reads_the_served_column_from_the_rendered_table() -> None:
    """The `Served` column is read from `table tbody tr` cells, so it reflects the API
    after a reload rather than the click's local state."""
    source = DRIVER.read_text()
    assert "table tbody tr" in source
    assert source.count('page.reload({ waitUntil: "networkidle" })') >= 2, (
        "the served column must be re-read after a reload"
    )


def test_the_gate_documents_the_rollback_gap_it_leans_on() -> None:
    """A limitation stated in the script and in the registry, or it is a hidden one."""
    assert "no screen control" in SCRIPT.read_text().lower()
