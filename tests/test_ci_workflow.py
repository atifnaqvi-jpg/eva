"""The CI workflow must be structurally valid, not merely parseable.

A step with neither `run` nor `uses` is accepted by a YAML parser and rejected only by
GitHub, which reports it as a run with **zero jobs** -- no failing step to read, no log to
open. Run 44077227419 failed exactly that way: a line-range edit dropped
`run: python -m alembic upgrade head` from the migrate step.

These assertions are textual rather than YAML-based: PyYAML is not a project dependency,
and adding one to inspect a build file is not worth a dependency decision.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"

_STEP_HEAD = re.compile(r"^      - (.*)$")
_BLOCK_SCALAR = re.compile(r"^        \S")


def _steps() -> list[list[str]]:
    """Split the workflow into step blocks.

    A step begins at six-space `- ` and continues over its indented body. The `run: |`
    scalar's own lines sit at ten spaces and are kept by the indentation test.
    """
    steps: list[list[str]] = []
    current: list[str] | None = None
    for line in WORKFLOW.read_text().splitlines():
        if _STEP_HEAD.match(line):
            current = [line]
            steps.append(current)
        elif current is not None and (not line.strip() or line.startswith("        ")):
            current.append(line)
        else:
            current = None
    return steps


def _body(block: list[str]) -> str:
    return "\n".join(block)


def _step_name(block: list[str]) -> str:
    for line in block:
        match = re.match(r"^\s*-?\s*name:\s*(.+)$", line)
        if match:
            return match.group(1).strip()
        match = re.match(r"^\s*-?\s*uses:\s*(.+)$", line)
        if match:
            return f"uses: {match.group(1).strip()}"
    return block[0].strip()


def _named(name: str) -> str:
    for block in _steps():
        if _step_name(block) == name:
            return _body(block)
    raise AssertionError(f"no step named {name!r}")


def _browser_matrix() -> Path:
    return ROOT / "backend" / "api" / "widget" / "test" / "browser.test.mjs"


def test_every_step_has_exactly_one_of_run_or_uses() -> None:
    offenders = []
    for block in _steps():
        body = _body(block)
        has_run = re.search(r"^\s*(?:- )?run:", body, re.MULTILINE) is not None
        has_uses = re.search(r"^\s*(?:- )?uses:", body, re.MULTILINE) is not None
        if has_run == has_uses:
            offenders.append(_step_name(block))
    assert not offenders, (
        "a step with neither 'run' nor 'uses' makes GitHub reject the whole workflow "
        f"and report a run with no jobs: {offenders}"
    )


def test_the_widget_job_runs_server_seed_and_matrix_in_one_step() -> None:
    """The runner kills processes left behind by a finished step.

    A uvicorn started in an earlier step is dead before the matrix runs, and the matrix
    then fails on a connection refused that reads like a widget defect. Server, seed and
    matrix must share one shell.
    """
    script = _named("Browser matrix")
    assert "uvicorn" in script
    assert "seed_widget_bot.py" in script
    assert "browser.test.mjs" in script
    # The server is killed on exit so a stray process cannot outlive the job.
    assert "trap" in script


def test_the_matrix_step_fails_when_cases_skip() -> None:
    """`node --test` counts a skip as success.

    Without this guard the job went green for weeks having executed no assertions.
    """
    script = _named("Browser matrix")
    assert "skipped" in script
    assert "exit 1" in script
    # The floor is compared to the cases the file actually defines, not to a retyped
    # number: a literal here went stale the moment WIDG-17 added two cases, and a
    # floor below the real count would let a whole case disappear silently.
    cases = len(re.findall(r"^matrixTest\(", _browser_matrix().read_text(), re.MULTILINE))
    assert f'-lt {cases}' in script, f"the CI floor does not cover all {cases} matrix cases"
    assert f"at least {cases} browser cases" in script
