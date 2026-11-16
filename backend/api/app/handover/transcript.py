"""Compile a conversation into a transcript a human can read (§13.2, HAND-04).

The output is plain text on purpose. It goes into an email body, where HTML would
be stripped or rendered inconsistently, and an owner may forward it to a helpdesk
that only accepts text. The compiled text is also what ``transcript_uri`` points at,
so it is written once and read by both the email and the dashboard.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

ROLE_LABELS = {"visitor": "Visitor", "assistant": "Assistant", "system": "Note"}


def compile_transcript(
    *,
    messages: Sequence[Mapping[str, Any]],
    bot_name: str,
    started_at: Any = None,
    page_url: str | None = None,
    reason: str | None = None,
    contact: Mapping[str, str] | None = None,
) -> str:
    """Render a conversation as plain text.

    Citations are rendered inline as bracketed sources rather than omitted: the
    owner's first question on a handover is "what did it tell them", and the answer
    is only useful with the document it came from.
    """
    lines = [f"Conversation with {bot_name}"]
    if started_at is not None:
        lines.append(f"Started: {_format_time(started_at)}")
    if page_url:
        lines.append(f"Page: {page_url}")
    if reason:
        lines.append(f"Handover reason: {reason}")
    if contact:
        who = ", ".join(f"{k}: {v}" for k, v in contact.items() if v)
        if who:
            lines.append(f"Contact: {who}")
    lines.append("")
    lines.append("-" * 60)
    lines.append("")

    if not messages:
        lines.append("(no messages)")

    for message in messages:
        label = ROLE_LABELS.get(str(message.get("role")), "Message")
        stamp = _format_time(message.get("created_at"))
        lines.append(f"[{stamp}] {label}:")
        for line in str(message.get("content") or "").splitlines() or [""]:
            lines.append(f"  {line}")
        sources = _citation_titles(message.get("citations"))
        if sources:
            lines.append(f"  Sources: {', '.join(sources)}")
        if message.get("confidence") is not None:
            lines.append(f"  Confidence: {message['confidence']:.2f}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def summary_prompt(transcript: str) -> list[dict[str, str]]:
    """The cheap-model prompt for the email summary (§13.2, ARCHITECTURE §11.6).

    Deliberately narrow: the owner needs to triage, not read a paraphrase of the
    transcript they were just sent. Bounded input because this runs on every
    handover and the model is billed per token.
    """
    return [
        {
            "role": "system",
            "content": (
                "Summarise this customer-support conversation for the business owner "
                "in at most three sentences. Say what the visitor wanted, what was "
                "answered, and whether anything is unresolved. Plain prose, no "
                "preamble, no markdown."
            ),
        },
        {"role": "user", "content": transcript[:6000]},
    ]


def fallback_summary(
    *, messages: Sequence[Mapping[str, Any]], reason: str | None
) -> str:
    """A summary that needs no model.

    Used when no provider is configured or the call fails. Better than an empty
    section: it still tells the owner how long the conversation was and why it was
    escalated, which is most of the triage value.
    """
    visitor_lines = [
        str(m.get("content") or "") for m in messages if m.get("role") == "visitor"
    ]
    first = visitor_lines[0].strip() if visitor_lines else ""
    if len(first) > 200:
        first = first[:197].rstrip() + "..."
    turns = len(visitor_lines)
    reason_text = f" Escalated: {reason}." if reason else ""
    if not first:
        return f"No visitor messages.{reason_text}"
    return f'Visitor opened with: "{first}" ({turns} message{"s" if turns != 1 else ""}).' + reason_text


def _citation_titles(citations: Any) -> list[str]:
    if not isinstance(citations, list):
        return []
    titles: list[str] = []
    for citation in citations:
        if isinstance(citation, Mapping):
            title = citation.get("title") or citation.get("document_title")
            if isinstance(title, str) and title.strip():
                titles.append(title.strip())
        elif isinstance(citation, str) and citation.strip():
            titles.append(citation.strip())
    return titles


def _format_time(value: Any) -> str:
    if value is None:
        return "?"
    try:
        return value.strftime("%Y-%m-%d %H:%M:%S UTC")
    except AttributeError:
        return str(value)
