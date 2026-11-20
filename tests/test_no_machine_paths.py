"""Committed evidence must not carry one machine's filesystem layout.

``scripts/ops13_checklist.py`` records command lines and output tails from a real run and
writes them into ``docs/ops/launch-checklist.json``, which is committed to a public
repository. On 2026-11-20 an audit found ``/home/ubuntu/work/eva/.venv/bin/python`` in
that file eight times: not a secret, but one developer's home directory published
permanently in a product repository.

The script now passes everything it records through ``redact()``. This test is the guard
that a future edit cannot bypass: it scans the committed artefacts for an absolute home
path, so forgetting the redaction step is a red test rather than a quiet leak.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Any absolute home directory: /home/<user>, /Users/<user>, C:\Users\<user>.
HOME_PATH = re.compile(r"(?:/home/|/Users/|[A-Za-z]:\\\\Users\\)[A-Za-z0-9_.-]+")

TRACKED_EVIDENCE = [
    "docs/ops/launch-checklist.json",
]


@pytest.mark.parametrize("rel", TRACKED_EVIDENCE)
def test_evidence_carries_no_absolute_home_path(rel: str) -> None:
    path = REPO_ROOT / rel
    if not path.is_file():
        pytest.skip(f"{rel} not present in this checkout")
    text = path.read_text(encoding="utf-8")
    hits = sorted(set(HOME_PATH.findall(text)))
    # Report the *prefixes* found, never the surrounding content.
    found = [m.group(0) for m in HOME_PATH.finditer(text)]
    assert not found, (
        f"{rel} contains {len(found)} absolute home path(s), e.g. {found[:3]}. "
        "Record evidence through scripts/ops13_checklist.py's redact() so the prefix "
        "becomes $REPO/$HOME before it is committed."
    )


def test_the_checklist_script_redacts_at_the_recording_boundary() -> None:
    """A retrofit cleans today's file; only redaction-at-source prevents tomorrow's."""
    src = (REPO_ROOT / "scripts" / "ops13_checklist.py").read_text(encoding="utf-8")
    assert "def redact(" in src, "the redaction helper was removed"
    assert "redact(" in src.replace("def redact(", "", 1), "nothing calls redact()"
    # The two fields that carried the leak must both be routed through it.
    assert '"cmd": redact(' in src
    assert '"stdout_tail": redact(' in src
