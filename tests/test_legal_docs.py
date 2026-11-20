"""The legal templates must match the code, and their markers must be registered.

Two checks, same shape as ``test_compliance_docs.py``:

1. **Drift.** Rendering ``render_legal.render_all()`` must reproduce the committed
   files byte for byte. A legal document that states a retention period the software no
   longer honours is worse than no document: it is a written promise that is false.

2. **Markers.** Every ``[[FILL: name]]`` a document contains must be declared in
   ``facts.OPERATOR_FIELDS`` (or generated from ``PROFILES``). An unregistered marker is
   a typo in a contract — ``[[FILL: support_channel]]`` misspelled renders as literal
   text a customer reads.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.compliance import facts
from app.compliance import render_legal

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPLIANCE = REPO_ROOT / "docs" / "compliance"

MARKER = re.compile(r"\[\[FILL: ([a-zA-Z0-9_]+)\]\]")


@pytest.mark.parametrize("name", sorted(render_legal.render_all()))
def test_committed_file_matches_the_renderer(name: str) -> None:
    rendered = render_legal.render_all()[name]
    committed = (COMPLIANCE / name).read_text(encoding="utf-8")
    assert committed == rendered, (
        f"{name} is stale: regenerate with "
        f"`cd backend/api && python -m app.compliance.render_legal`"
    )


@pytest.mark.parametrize("name", sorted(render_legal.render_all()))
def test_every_marker_is_declared(name: str) -> None:
    text = render_legal.render_all()[name]
    declared = facts.declared_markers()
    used = {m.group(1) for m in MARKER.finditer(text)}
    undeclared = used - declared
    assert not undeclared, (
        f"{name} uses markers no one declared: {sorted(undeclared)}. "
        "Add them to OPERATOR_FIELDS so a typo cannot reach a contract."
    )


def test_no_document_claims_a_training_use() -> None:
    """The US notice's no-training commitment must not be contradicted here.

    ``us-state-privacy-notice-template.md`` section 4 states that no model is trained on
    a tenant's conversations, and a separate test asserts that against the platform's
    behaviour. A Terms document that permitted a training use would make the two
    documents disagree — and the notice is the one a regulator reads first.
    """
    terms = render_legal.render_all()["terms-of-service-template.md"]
    assert "We do not train any model on your conversations" in terms


def test_prohibited_uses_cover_the_admt_boundary() -> None:
    """The ADMT restriction is the contractual half of the US notice's scope claim."""
    aup = render_legal.render_all()["acceptable-use-policy-template.md"]
    for phrase in ("employment", "finances", "healthcare", "housing", "education"):
        assert phrase in aup, f"the ADMT boundary must name {phrase}"
    assert "automated decision" in aup


def test_disclaimer_makes_the_ai_act_disclosure_explicit() -> None:
    """Art. 50(1) needs the person told they are interacting with an AI system."""
    disclaimer = render_legal.render_all()["ai-output-disclaimer-template.md"]
    assert "AI system" in disclaimer
    assert "Article 50" in disclaimer
    assert "You are talking to an AI" in disclaimer
