"""The facts the compliance templates are rendered from (SEC-11, §19.3).

The notice and the DPA say what the platform *does*. Every such statement is a claim
that decays the moment the code changes: add a provider to ``PROFILES`` and the DPA's
sub-processor table is wrong; change the retention floor and the notice promises a
period the purge does not honour. A hand-written document would be silently false
within one work package.

So the documents are rendered from this module, and
``tests/test_compliance_docs.py`` regenerates them and fails if the committed files
differ. Nothing here invents a legal position: contract-specific values are
``[[FILL: ...]]`` markers that the operator must replace, and the test asserts every
marker is declared here so a renderer change cannot quietly drop one.
"""

from __future__ import annotations

from dataclasses import dataclass
from app.config import SESSION_TTL_SECONDS, VISITOR_MAX_UPLOAD_BYTES
from app.conversation import prompt
from app.conversation.providers import PROFILES
from app.retention import policy as retention

# Legal facts are not derivable from the repository, and guessing them would be worse
# than leaving a visible gap. Every marker a document may contain is declared here;
# the test asserts this set and the set found in the rendered files are identical.
OPERATOR_FIELDS: dict[str, str] = {
    "business_legal_name": "the business's registered legal name",
    "business_address": "the business's registered address",
    "business_contact": "the business's privacy contact (email or address)",
    "platform_legal_name": "the platform operator's registered legal name",
    "platform_address": "the platform operator's registered address",
    "legal_basis_review": "confirmation of the lawful basis in the business's own "
    "jurisdiction and sector",
    "d3_position": "how to describe the processing chain if D3 is resolved the other way",
    "breach_notice_window": "how fast the processor notifies a breach, e.g. 24 hours",
    "database_at_rest": "how the database volume is encrypted at rest, or that it is not",
    "backup_retention": "how long backups are kept — a policy value no blueprint "
    "section specifies, and the window that bounds the one copy of a conversation an "
    "erasure cannot reach",
    "liability": "the liability clause agreed between the parties",
    "governing_law": "the law governing the DPA and its courts",
    "effective_date": "the date the notice and DPA take effect",
    "hosting_location": "the region the platform's own servers run in — a machine fact "
    "the repository cannot know, and one that must not be guessed in a privacy notice",
    "hosting_sub_processor": "the infrastructure host, its location and its terms "
    "(the platform's own servers are a sub-processor too — they hold the database)",
    "us_state_scope": "which US state privacy laws the business is subject to, and its "
    "revenue/volume basis for saying so — a legal determination the repository cannot make",
    "us_request_channel": "the address or form a US consumer uses to submit a request",
    "us_request_verification": "how the business verifies a request is genuine, e.g. by "
    "replying to the address the conversation was held under",
    "us_opt_in_mechanism": "how a tenant grants the explicit opt-in that would permit "
    "training on its conversations, if the business ever offers one",
}


def fill(name: str) -> str:
    return f"[[FILL: {name}]]"


# Provider names as the provider spells them. A legal document that names a
# sub-processor wrong names the wrong entity; the test asserts every runtime provider
# is covered here, so adding a provider to PROFILES forces this decision.
PROVIDER_DISPLAY_NAMES: dict[str, str] = {
    "ollama_cloud": "Ollama Cloud",
    "deepseek": "DeepSeek",
}


def provider_terms_field(key: str) -> str:
    return f"provider_terms_{key}"


def declared_markers() -> set[str]:
    """Every marker name a document may legitimately contain.

    Provider-terms markers are generated from ``PROFILES``, so adding a provider adds
    a required marker without a second edit here.
    """
    return set(OPERATOR_FIELDS) | {provider_terms_field(k) for k in PROFILES}


@dataclass(frozen=True)
class SubProcessor:
    """One third party that receives tenant or visitor data from the platform."""

    key: str
    role: str
    receives: str
    region: str

    @property
    def label(self) -> str:
        # Falls back rather than raising, so the gap surfaces as the explicit
        # "no display name" assertion instead of a stack trace at render time.
        return PROVIDER_DISPLAY_NAMES.get(self.key, self.key.replace("_", " ").title())


def sub_processors() -> list[SubProcessor]:
    """The LLM providers reachable at runtime, in chain order (§11.1).

    Derived from ``PROFILES`` rather than a list in the document, because a provider
    the runtime can call is a sub-processor by definition. Note the asymmetry that
    matters legally: ``vault.ALLOWED_PROVIDERS`` also accepts ``openai`` and
    ``anthropic`` for *storage*, but neither has a runtime profile, so neither can
    process a conversation — they are not sub-processors, and listing them would
    overstate the chain while omitting one from ``PROFILES`` would understate it.
    """
    return [
        SubProcessor(
            key=key,
            role="Prompt completion and handover summarisation",
            receives=(
                "The visitor's messages, the conversation's recent history, retrieved "
                "knowledge-base passages, the business's bot instructions, and — for a "
                "handover summary — the compiled transcript including the visitor's "
                "name, email address and page URL when they supplied them."
            ),
            region="Per the provider's own terms; confirm and record the processing "
            "region before publishing.",
        )
        for key in PROFILES
    ]


@dataclass(frozen=True)
class TableFact:
    name: str
    purpose: str
    retention: str


# Every table the schema creates. The list is asserted against the migrations, so a
# new table cannot be added without this inventory being updated with it.
TABLES: tuple[TableFact, ...] = (
    TableFact(
        "tenants",
        "The business's account: name, plan, status and its settings blob (retention "
        "window, handover address, business hours).",
        "Life of the account.",
    ),
    TableFact(
        "users",
        "The business's own staff logins: email, Argon2id password hash, role.",
        "Life of the account; a user row is deleted with the tenant.",
    ),
    TableFact(
        "sessions",
        "Dashboard login sessions. Only a SHA-256 of the cookie value is stored, never "
        "the cookie itself; the client IP and user agent are recorded for the audit "
        f"trail. Sessions expire {SESSION_TTL_SECONDS // 3600} hours after login, are "
        "revoked on logout, and are deleted with the account.",
        "Expiry, revoke, or tenant deletion.",
    ),
    TableFact(
        "bots",
        "Bot configuration: name, instructions, welcome message, appearance, "
        "confidence threshold.",
        "Life of the account.",
    ),
    TableFact(
        "bot_domains",
        "The site origins the business has verified for its widget. Enforced: an "
        "unverified origin is refused.",
        "Life of the account.",
    ),
    TableFact(
        "knowledge_sources",
        "Where knowledge comes from: an uploaded file set, a crawled site or FAQs.",
        "Life of the account.",
    ),
    TableFact(
        "documents",
        "One indexed document, with its content hash for de-duplication.",
        "Life of the account.",
    ),
    TableFact(
        "document_versions",
        "A version of a document. Uploaded bytes live here, in the database, and are "
        "never served back to a browser.",
        "Life of the account.",
    ),
    TableFact(
        "chunks",
        "Retrieval chunks: text slices of a document version plus their embeddings.",
        "Life of the account.",
    ),
    TableFact(
        "knowledge_sets",
        "A published snapshot of a bot's knowledge: which document versions the bot "
        "answers from, and a label. Publishing creates a new set and points the bot at "
        "it, so the previous set stays readable and rollback is a re-point.",
        "Life of the account.",
    ),
    TableFact(
        "knowledge_set_members",
        "Which document versions belong to a knowledge set — one row per version. No "
        "personal data of its own; it exists so a snapshot is explicit rather than a "
        "diff of per-version statuses.",
        "Cascades with its set.",
    ),
    TableFact(
        "conversations",
        "One visitor conversation: the widget's session identifier, the page URL, the "
        "status, and the visitor's name and email address if they chose to leave them.",
        "The tenant's retention window, default 90 days.",
    ),
    TableFact(
        "messages",
        "Every message in the conversation — the visitor's questions and the "
        "assistant's answers, with citations, confidence and token counts.",
        "Cascades with its conversation.",
    ),
    TableFact(
        "feedback",
        "A thumbs-up or thumbs-down on an answer, with an optional free-text comment.",
        "Cascades with its conversation.",
    ),
    TableFact(
        "handovers",
        "A request for a human: the trigger, the channel, and the delivery outcome.",
        "Cascades with its conversation.",
    ),
    TableFact(
        "visitor_files",
        "Metadata for a file a visitor attached: the sniffed type, size and a "
        "server-chosen storage key. The bytes themselves are accepted, checked and "
        "then discarded — nothing reads or keeps an attachment.",
        "Cascades with its conversation.",
    ),
    TableFact(
        "api_credentials",
        "A business's own model-provider key, referenced by path only — the secret "
        "itself lives in an encrypted file outside the database and is never returned "
        "by any API.",
        "Until the business deletes it.",
    ),
    TableFact(
        "webhooks",
        "The business's signed webhook endpoints; the signing secret is vaulted and "
        "shown once at creation.",
        "Until the business deletes it.",
    ),
    TableFact(
        "usage_records",
        "Token and unit counts per conversation, for the business's own cost "
        "visibility. Erasing a conversation keeps the totals and nulls the link, so "
        "the aggregate holds no link back to a visitor.",
        "Life of the account.",
    ),
    TableFact(
        "provider_outcomes",
        "One row per answered turn recording whether the model provider succeeded, "
        "which provider and model served it, and — on failure — the exception's type "
        "name only. No question, no answer, no error text. Erasing a conversation "
        "deletes these rows with it.",
        "Cascades with its conversation.",
    ),
    TableFact(
        "audit_logs",
        "Operator actions: logins, role changes, credential and settings changes, "
        "conversation erasure. Append-only and never purged. A conversation erasure "
        "writes counts and the conversation id only — never the visitor's name, "
        "address, session id or page URL.",
        "Never purged.",
    ),
)


@dataclass(frozen=True)
class RetentionFacts:
    default_days: int
    minimum_days: int
    maximum_days: int
    purge_hour: int
    purge_minute: int
    eligible_statuses: tuple[str, ...]
    aged_on: str


def _worker_module(name: str):
    """Import a module from the sibling ``worker`` package.

    The worker lives beside ``api``, so a bare ``python -m app.compliance.render``
    from ``backend/api`` cannot see it. ``tests/conftest.py`` puts it on the path for
    the suite; this mirrors that rather than restating the schedule in prose.
    """
    import importlib
    import sys
    from pathlib import Path

    try:
        return importlib.import_module(name)
    except ModuleNotFoundError:
        sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "worker"))
        return importlib.import_module(name)


def retention_facts() -> RetentionFacts:
    """The purge's actual behaviour, read from the code that implements it.

    The schedule is read from ``WorkerSettings`` rather than restated, because a
    document that says 03:15 while the job runs at another hour is worse than no
    document: it is a false statement about a control.
    """
    eligible = _worker_module("worker.purge_job").ELIGIBLE_STATUSES
    purge = next(
        job for job in _worker_module("worker.settings").WorkerSettings.cron_jobs
        if "purge" in job.name
    )
    return RetentionFacts(
        default_days=retention.DEFAULT_RETENTION_DAYS,
        minimum_days=retention.MIN_RETENTION_DAYS,
        maximum_days=retention.MAX_RETENTION_DAYS,
        purge_hour=purge.hour,
        purge_minute=purge.minute,
        eligible_statuses=tuple(eligible),
        aged_on="the conversation's last activity, so a live conversation is never "
        "deleted however old it is",
    )


@dataclass(frozen=True)
class EgressFacts:
    """What leaves the platform's own infrastructure, and under which setting."""

    history_messages: int
    max_chunk_chars: int
    upload_bytes: int
    upload_types: tuple[str, ...]


def egress_facts() -> EgressFacts:
    from app.widget.uploads import VISITOR_MIME_BY_SUFFIX

    return EgressFacts(
        history_messages=prompt.HISTORY_TURNS,
        max_chunk_chars=prompt.MAX_CHUNK_CHARS,
        upload_bytes=VISITOR_MAX_UPLOAD_BYTES,
        upload_types=tuple(sorted(VISITOR_MIME_BY_SUFFIX)),
    )
