"""ADMN-16: the settings walk's verdict has to be able to say no.

The walk is a browser against a live stack (``scripts/admn16_walk.py`` drives
``scripts/admn16_driver.mjs``), which CI cannot run. What can run here is the half that
decides the verdict: ``clauses()`` is a pure function over the driver's report, so every
clause is exercised by handing it a report and mutating exactly one field.

That is the point of this file, for the reason §8.25 and §8.32 keep recording: a UI row
is the easiest place in this repository to write a gate that cannot fail. "The control
saved" is exactly the assertion a form that posts nowhere also passes, which is why the
driver re-reads the server and why the mutations below include the click-does-nothing
case.

The clause that carries the row is the **boundary refusal**. The in-process suite checks
that `min`/`max` match `app.retention.policy`; only a browser shows that a bad value
produces a sentence a person can read instead of a tooltip that vanishes.
"""

from __future__ import annotations

import copy
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "admn16_walk.py"
DRIVER = ROOT / "scripts" / "admn16_driver.mjs"


def _load():
    name = "eva_admn16_walk"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


walk = _load()


def report() -> dict:
    """A report shaped exactly like the one the driver printed on a passing walk."""
    return {
        "retention_form": {"min": "1", "max": "730", "value": "90"},
        "retention_after_valid_save": {"api_days": 14, "card_mentions": True},
        "retention_after_rejected_save": {
            "api_days": 14,
            "refusal_visible": True,
            "message_names_bound": True,
        },
        "domain_after_add": {
            "verified": False,
            "record_name": "_eva-verify.shop.admn16.test",
            "record_value": "eva-verify=abc123",
            "record_on_screen": True,
        },
        "widget_status_after_add": 403,
        "after_remove": {"still_in_api": False, "still_on_screen": False},
        "fatal": None,
        "steps": [],
    }


def verdict(data: dict) -> dict[str, bool]:
    return dict(walk.clauses(data))


def test_the_observed_walk_satisfies_every_clause() -> None:
    results = verdict(report())
    assert len(results) == 6, "a clause was added or removed without a mutation below"
    assert all(results.values()), [name for name, ok in results.items() if not ok]


def test_every_clause_has_a_mutation_that_makes_it_fail() -> None:
    """The floor: no clause may be unreachable, and each mutation must isolate."""
    mutations = {
        "the retention editor renders with the policy's own bounds": lambda r: r[
            "retention_form"
        ].__setitem__("max", "3650"),
        "a valid window persists and the card re-reads the stored number": lambda r: r[
            "retention_after_valid_save"
        ].__setitem__("api_days", 90),
        "an out-of-range window is refused with the server's message and changes nothing": (
            lambda r: r["retention_after_rejected_save"].__setitem__("refusal_visible", False)
        ),
        "adding a domain stores it unverified and shows the record to publish": lambda r: r[
            "domain_after_add"
        ].__setitem__("record_on_screen", False),
        "the widget still refuses that origin after the add": lambda r: r.__setitem__(
            "widget_status_after_add", 200
        ),
        "removing the domain takes it off the screen and out of the API": lambda r: r[
            "after_remove"
        ].__setitem__("still_on_screen", True),
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


def test_a_control_that_posts_nothing_still_appears_to_save() -> None:
    """The defect a click-only assertion would bless.

    If the driver read the input's value instead of the server's, a form wired to
    nothing would show 14 and pass. The driver re-reads `GET /settings/tenant`, so the
    stale server value is what falsifies it — this mutation is exactly that report.
    """
    data = report()
    data["retention_after_valid_save"] = {"api_days": 90, "card_mentions": True}
    results = verdict(data)
    assert results["a valid window persists and the card re-reads the stored number"] is False


def test_an_add_that_also_verified_is_red() -> None:
    """SEC-15's rule: add grants nothing. A control that verified on add would serve a
    host nobody has proved they control, which is the defect this row must not reopen."""
    data = report()
    data["domain_after_add"]["verified"] = True
    data["widget_status_after_add"] = 200
    results = verdict(data)
    assert results["adding a domain stores it unverified and shows the record to publish"] is False
    assert results["the widget still refuses that origin after the add"] is False


def test_a_record_not_shown_is_red_even_when_the_row_exists() -> None:
    """The row is not the affordance: an owner who cannot read the value to publish has
    no usable control, which is the ADMN-10 defect in a table cell."""
    data = report()
    data["domain_after_add"]["record_on_screen"] = False
    assert verdict(data)["adding a domain stores it unverified and shows the record to publish"] is False


def test_a_rejected_save_that_still_stored_the_value_is_red() -> None:
    """Refusal and persistence are two halves; a 400 with the value written is worse
    than either alone, because the screen then disagrees with the purge."""
    data = report()
    data["retention_after_rejected_save"]["api_days"] = 5000
    results = verdict(data)
    assert (
        results[
            "an out-of-range window is refused with the server's message and changes nothing"
        ]
        is False
    )


def test_both_forms_of_refusal_are_accepted_but_silence_is_not() -> None:
    """The screen's durable message and the server's own refusal both name the bound.

    Measured live: with native validation left on, clicking Save at 5000 produced no
    message at all — the browser blocked submit and showed a tooltip that disappears on
    the next click. The clause requires a *visible* sentence, so silence is red whichever
    side produced it.
    """
    data = report()
    data["retention_after_rejected_save"] = {
        "api_days": 14,
        "refusal_visible": False,
        "message_names_bound": False,
    }
    assert (
        verdict(data)[
            "an out-of-range window is refused with the server's message and changes nothing"
        ]
        is False
    )


def test_the_gate_fails_loudly_on_a_fatal_driver_report(monkeypatch, capsys) -> None:
    """A browser that stopped halfway must not read as "every clause held"."""
    monkeypatch.setattr(walk, "drive", lambda: {**report(), "fatal": "login timed out"})
    assert walk.main() == 1
    assert "login timed out" in capsys.readouterr().out


def test_the_driver_exists_and_does_not_decide_the_verdict() -> None:
    """The verdict lives in `clauses()`; a driver that printed its own PASS could not be
    mutation-checked, which is why this file can exist at all."""
    source = DRIVER.read_text()
    assert "process.stdout.write" in source
    # Anchored on the printed markers, not the bare substrings: "PASS" also matches
    # "PASSWORD", and a guard that trips on an environment variable name would have to
    # be weakened rather than fixed.
    for marker in ('"[PASS]', "'[PASS]", '"[FAIL]', "'[FAIL]",
                   "ADMN-16: every clause", "ADMN-16: FAILED"):
        assert marker not in source, f"the driver is deciding the verdict: {marker}"
