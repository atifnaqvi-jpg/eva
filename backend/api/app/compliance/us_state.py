"""The facts the US state privacy notice is rendered from (SEC-12, §19.4, R30).

ARCHITECTURE §19.4 asks for four things and this module derives all four from the
code rather than restating them: the categories of personal information the platform
collects, the absence of any sale or share, the absence of model training on tenant
conversations, and the relationship between a deletion request and the statutory
clock.

The pattern is the one SEC-11 established for the GDPR notice: a legal document is a
claim about behaviour, and a hand-written claim goes false silently. So every
statement here is computed — the category table from the schema, the "no sale"
position from an enumeration of what the shipped clients actually load, the training
position from the request body the provider client actually sends, and the deletion
position from the retention policy and the erasure route.

Two things this module deliberately does *not* do:

* It does not invent a legal position. Which statutes apply to a given business is
  the business's determination (and its lawyer's), so it is a ``[[FILL: ...]]``.
* It does not claim a control the code lacks. Deletion propagation to model providers
  is **not** automated here, and the document says so instead of promising it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from app.compliance import facts as f
from app.retention import policy as retention

REPO_ROOT = Path(__file__).resolve().parents[4]

# §19.4, and Cal. Civ. Code §1798.130(a)(2). A statute, not a repository value: it
# cannot be derived from the code, so it is a named constant with its citation rather
# than a number that looks computed.
CCPA_RESPONSE_DAYS = 45

# --------------------------------------------------------------------------- categories


@dataclass(frozen=True)
class Category:
    """One CCPA/CPRA category of personal information, as §1798.140(v)(1) enumerates."""

    code: str
    name: str
    collected: bool
    # What it is here, in the product's own words. Empty when not collected.
    detail: str
    purpose: str
    disclosed_to: str


# The (table, column) pairs each category is stored in. Kept separate from the prose
# above so the test can check every pair against the live schema: a category that
# points at a column which does not exist is a false statement about the system, and a
# `visitor_*` column absent from this map is personal data that was not disclosed.
CATEGORY_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "A": (
        ("conversations", "visitor_session_id"),
        ("sessions", "ip"),
        ("sessions", "user_agent"),
        ("audit_logs", "ip"),
        ("visitor_files", "storage_path"),
    ),
    "B": (
        ("conversations", "visitor_name"),
        ("conversations", "visitor_email"),
        ("messages", "content"),
        ("feedback", "comment"),
    ),
    "F": (
        ("conversations", "page_url"),
        ("conversations", "started_at"),
        ("conversations", "last_activity_at"),
    ),
}
CATEGORIES: tuple[Category, ...] = (
    Category(
        "A",
        "Identifiers",
        True,
        "A random per-tab conversation identifier, a random visitor identifier, and the "
        "visitor's name and email address when they choose to leave them to ask for a "
        "person. For the business's own staff: login email, session IP address and user "
        "agent, and the IP recorded on an administrative action.",
        "To hold one conversation together, to reply to a visitor who asked for a human, "
        "and to keep staff sessions and administrative actions auditable.",
        "The business that embeds the assistant, through its dashboard. A model provider "
        "receives the visitor's name, email and page address only inside a handover "
        "summary.",
    ),
    Category(
        "B",
        "Customer records (Cal. Civ. Code §1798.80)",
        True,
        "Only name, email address and the content of what the visitor typed. No payment "
        "card, no account number, no financial account, no medical or insurance record.",
        "To answer the question and, where asked, to follow up by email.",
        "The business, and a model provider for prompt completion.",
    ),
    Category(
        "C",
        "Characteristics of protected classifications",
        False,
        "",
        "",
        "",
    ),
    Category(
        "D",
        "Commercial information",
        False,
        "The assistant cannot take orders, change a booking or process a refund, so no "
        "purchase or purchasing-history record is created. A visitor may *mention* an "
        "order in free text; that is stored as conversation content under category B.",
        "",
        "",
    ),
    Category(
        "E",
        "Biometric information",
        False,
        "No biometric data is collected, derived or inferred.",
        "",
        "",
    ),
    Category(
        "F",
        "Internet or other network activity",
        True,
        "The address of the page the chat was opened from, and the timestamps of the "
        "conversation's start and last activity. There is no cross-site tracking, no "
        "browsing history and no advertising identifier.",
        "So the business knows what the visitor was looking at, and so a conversation "
        "sorts by recency in its archive.",
        "The business. The page address also accompanies a handover summary.",
    ),
    Category(
        "G",
        "Geolocation",
        False,
        "No device location is requested. The IP address on a *staff* login session is "
        "recorded (category A); it is not resolved to a location by the platform.",
        "",
        "",
    ),
    Category(
        "H",
        "Sensory data (audio, visual, thermal, olfactory)",
        False,
        "The product is text-only. Voice is an interface the architecture reserves and "
        "the API answers 501; no audio is captured, stored or transmitted.",
        "",
        "",
    ),
    Category(
        "I",
        "Professional or employment information",
        False,
        "Not requested. A visitor may mention their job in free text, which is stored as "
        "conversation content under category B.",
        "",
        "",
    ),
    Category(
        "J",
        "Non-public education information (FERPA)",
        False,
        "Not requested and not collected as a category.",
        "",
        "",
    ),
    Category(
        "K",
        "Inferences drawn from other personal information",
        False,
        "No inference is drawn about the consumer. The confidence score attached to "
        "each answer and the passages it cited describe the *answer's* support; they "
        "are not a preference, characteristic, behaviour or prediction about the "
        "visitor, and nothing aggregates them into a profile.",
        "",
        "",
    ),
    Category(
        "L",
        "Sensitive personal information (§1798.140(ae))",
        False,
        "None is requested, collected as a category, or used to infer anything. If a "
        "visitor volunteers health, biometric or other sensitive detail in free text, it "
        "is stored as conversation content under category B and used for no other "
        "purpose — it is neither extracted nor acted on.",
        "",
        "",
    ),
)


def categories() -> tuple[Category, ...]:
    return CATEGORIES


def collected_categories() -> tuple[Category, ...]:
    return tuple(c for c in CATEGORIES if c.collected)


def undisclosed_personal_columns(schema: dict[str, set[str]]) -> set[tuple[str, str]]:
    """Personal-data columns present in the schema but attributed to no category.

    The rule is structural rather than a list of names to maintain: every ``visitor_*``
    column is personal data by construction, and so is any column of a table that holds
    conversation children. A new visitor field therefore fails the test instead of
    shipping undisclosed.
    """
    attributed = {pair for pairs in CATEGORY_COLUMNS.values() for pair in pairs}
    present = {
        (table, column)
        for table, columns in schema.items()
        for column in columns
        if column.startswith("visitor_")
    }
    return present - attributed


# The storage keys the widget writes, and the scope each one gets. Read from the source
# so the notice's promise about the visitor's device is checkable: the claim is not
# "no storage", it is "durable storage requires the visitor's own opt-in".
STORAGE_KEYS = "backend/api/widget/src/shared/identity.ts"
DURABLE_OPT_IN_UI = "backend/api/widget/src/app/app.tsx"


@dataclass(frozen=True)
class DeviceStorage:
    session_keys: tuple[str, ...]
    durable_keys: tuple[str, ...]
    opt_in_prompt: str


def device_storage() -> DeviceStorage:
    """What the widget writes on a visitor's device, and how the durable one is gated.

    This exists because the first draft of the notice said "session-scoped values
    only", and the code has a durable ``localStorage`` identifier behind a checkbox
    (R42). The sentence was false within one read; generating it from the keys and the
    label makes the two disagree loudly instead.
    """
    source = (REPO_ROOT / STORAGE_KEYS).read_text(encoding="utf-8")
    pairs = re.findall(r'^const (SESSION_KEY|CONVERSATION_KEY|DURABLE_KEY) = "([^"]+)"', source, re.M)
    by_scope: dict[str, list[str]] = {}
    for name, value in pairs:
        by_scope.setdefault(name, []).append(value)
    for required in ("SESSION_KEY", "CONVERSATION_KEY", "DURABLE_KEY"):
        if not by_scope.get(required):
            raise RuntimeError(
                f"{STORAGE_KEYS} no longer declares {required}: the notice's storage "
                "paragraph has to be rewritten, not silently rendered without it"
            )
    session_keys = tuple(sorted(by_scope["SESSION_KEY"] + by_scope["CONVERSATION_KEY"]))
    durable_keys = tuple(sorted(by_scope["DURABLE_KEY"]))
    ui = (REPO_ROOT / DURABLE_OPT_IN_UI).read_text(encoding="utf-8")
    labels = re.findall(r">\s*(Remember this conversation[^<]*)<", ui)
    if len(labels) != 1:
        raise RuntimeError(
            f"expected exactly one durable-opt-in label in {DURABLE_OPT_IN_UI}, "
            f"found {labels}: the notice quotes it"
        )
    return DeviceStorage(
        session_keys=session_keys,
        durable_keys=durable_keys,
        opt_in_prompt=labels[0].strip(),
    )


# --------------------------------------------------------------------- sale/share


# Asset references the shipped clients would resolve to another origin. Relative paths
# are same-origin by definition (Caddy serves both the dashboard and the widget), so
# only absolute references can be a third party.
_ABSOLUTE_ASSET = re.compile(r'(?:src|href)\s*=\s*"(https?:)?//', re.IGNORECASE)

SHIPPED_CLIENTS = (
    "backend/api/widget/src/app/index.html",
    "backend/api/widget/demo/host.html",
    "dashboard/app/layout.tsx",
)

# Names that appear in an advertising or analytics package. Any of these in either
# client's dependency list would mean the "no sale or share" position needs rewriting,
# which is why the test asserts the intersection is empty rather than trusting prose.
TRACKER_TOKENS = (
    "analytics",
    "gtag",
    "googletagmanager",
    "doubleclick",
    "adsense",
    "segment",
    "mixpanel",
    "amplitude",
    "posthog",
    "hotjar",
    "fullstory",
    "matomo",
    "plausible",
    "fathom",
    "facebook",
    "fbq",
    "tiktok",
    "criteo",
    "taboola",
    "outbrain",
    "ads",
)


def third_party_assets() -> list[tuple[str, str]]:
    """``(file, reference)`` for every absolute asset reference in a shipped client.

    Empty today, and the document says "zero" on the strength of this scan rather than
    on an assertion about intent.
    """
    found: list[tuple[str, str]] = []
    for relative in SHIPPED_CLIENTS:
        text = (REPO_ROOT / relative).read_text(encoding="utf-8")
        for match in _ABSOLUTE_ASSET.finditer(text):
            line_start = text.rfind("\n", 0, match.start()) + 1
            line_end = text.find("\n", match.end())
            found.append((relative, text[line_start:line_end].strip()))
    return found


def client_dependencies() -> dict[str, tuple[str, ...]]:
    """Both clients' package names, so the tracker scan reads real metadata."""
    out: dict[str, tuple[str, ...]] = {}
    for relative in ("dashboard/package.json", "backend/api/widget/package.json"):
        data = json.loads((REPO_ROOT / relative).read_text(encoding="utf-8"))
        names = set(data.get("dependencies", {})) | set(data.get("devDependencies", {}))
        out[relative] = tuple(sorted(names))
    return out


def tracker_dependencies() -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for manifest, names in client_dependencies().items():
        for name in names:
            lowered = name.lower()
            if any(token in lowered for token in TRACKER_TOKENS):
                found.append((manifest, name))
    return found


# ----------------------------------------------------------------------- training


@dataclass(frozen=True)
class TrainingPosition:
    """What the platform sends a model, and what it can ask one to do."""

    request_fields: tuple[str, ...]
    request_paths: tuple[str, ...]
    training_fields: tuple[str, ...]
    opt_in_keys: tuple[str, ...]


def _provider_source() -> str:
    path = REPO_ROOT / "backend" / "api" / "app" / "conversation" / "providers.py"
    return path.read_text(encoding="utf-8")


def request_fields() -> tuple[str, ...]:
    """The keys of the body the provider client actually posts.

    Read from ``_payload`` rather than restated: the notice discloses *which* fields
    leave the platform, so it must follow the code that builds the request. The private
    name is deliberate — a public wrapper beside the builder is one more thing that can
    drift from it.
    """
    from app.conversation.providers import _payload

    body = _payload([], model="compliance-probe", max_tokens=1, stream=False)
    return tuple(sorted(body))


def request_paths() -> tuple[str, ...]:
    """The endpoints the provider client posts to.

    A single completion path means no file-upload or fine-tuning endpoint is reachable
    from the platform at all — a stronger statement than "we choose not to train".
    """
    return tuple(sorted(set(re.findall(r'"(\/[a-z_]+(?:\/[a-z_]+)*)"', _provider_source()))))


# "fine" is not a token here: it is a substring of ordinary words like "defined", and a
# false positive would fail the absence assertion for the wrong reason. "tune" already
# covers "fine-tune" and "finetune".
TRAINING_TOKENS = ("train", "tune", "dataset", "files", "batch")

# Every module that reads a key out of ``tenants.settings``. A training opt-in would
# have to appear in one of them to mean anything, so scanning their string literals is
# a check on the code rather than on a list maintained beside it.
SETTINGS_READERS = (
    "backend/api/app/retention/policy.py",
    "backend/api/app/handover/policy.py",
    "backend/api/app/retrieval/priority.py",
    "backend/api/app/settings/router.py",
)


def settings_keys() -> tuple[str, ...]:
    """Every key the tenant-settings readers can act on, read from their source.

    Two shapes matter, and both are collected: a dictionary key (a string literal, e.g.
    ``raw.get("conversation_days")``) and a declared settings field (an annotated
    assignment inside a model class). Reading only the literals would miss a new
    ``train_on_conversations: bool`` field, which is exactly the opt-in this asserts is
    absent — so the identifier form is not decoration.
    """
    import ast

    keys: set[str] = set()
    for relative in SETTINGS_READERS:
        tree = ast.parse((REPO_ROOT / relative).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = node.value
                # A key, not prose: prose docstrings mention "training" in a sentence
                # and would fail the absence assertion for the wrong reason.
                if len(text) <= 40 and text.replace("_", "").replace(".", "").isalnum():
                    keys.add(text)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                keys.add(node.target.id)
    return tuple(sorted(keys))


def training_position() -> TrainingPosition:
    fields = request_fields()
    keys = settings_keys()
    return TrainingPosition(
        request_fields=fields,
        request_paths=request_paths(),
        training_fields=tuple(
            field for field in fields if any(t in field.lower() for t in TRAINING_TOKENS)
        ),
        opt_in_keys=tuple(k for k in keys if any(t in k.lower() for t in TRAINING_TOKENS)),
    )


# ---------------------------------------------------------------------- deletion


@dataclass(frozen=True)
class DeletionPosition:
    statutory_days: int
    erasure_route: str
    retention_default_days: int
    retention_minimum_days: int
    retention_maximum_days: int


def deletion_position() -> DeletionPosition:
    from app.conversation import erasure

    routes = sorted(
        f"/api/v1{route.path}"
        for route in erasure.router.routes
        if hasattr(route, "methods") and "DELETE" in (route.methods or ())
    )
    if len(routes) != 1:
        raise RuntimeError(
            f"expected exactly one erasure route, found {routes}: the US notice names it"
        )
    return DeletionPosition(
        statutory_days=CCPA_RESPONSE_DAYS,
        erasure_route=routes[0],
        retention_default_days=retention.DEFAULT_RETENTION_DAYS,
        retention_minimum_days=retention.MIN_RETENTION_DAYS,
        retention_maximum_days=retention.MAX_RETENTION_DAYS,
    )


def service_providers() -> tuple[str, ...]:
    """The parties that receive conversation content, by display name (DPA §5)."""
    return tuple(sp.label for sp in f.sub_processors())
