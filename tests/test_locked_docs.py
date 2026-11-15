"""G1 gate: the frozen seed docs must not change without a user ruling.

A ruling arrives as a pulse that deliberately updates the recorded digests here.
"""

import hashlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

LOCKED_DOCS = {
    "docs/ARCHITECTURE.md": "44b89412badb12ad92e86913d0494622bf6f10ab7b35ca32a1d9c6142b49a1d8",
    "docs/STACK.md": "ea2a6576ac4dc37646573c6cc4f1a619f1498d53d3b0350a9f34cf74073cf3ab",
}


@pytest.mark.parametrize("relative_path,expected", sorted(LOCKED_DOCS.items()))
def test_locked_doc_checksum(relative_path: str, expected: str) -> None:
    path = REPO_ROOT / relative_path
    assert path.is_file(), f"{relative_path} is missing"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert digest == expected, (
        f"{relative_path} changed (G1). Actual sha256: {digest}. "
        "Editing this doc requires a user ruling that updates this test."
    )
