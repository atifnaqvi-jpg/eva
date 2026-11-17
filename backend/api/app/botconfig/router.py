"""Owner-facing bot configuration (ADMN-03, §17.2, §18.1).

The widget has served a normalised `GET /config` since WP-15, but nothing ever *wrote*
those fields — `bots` was populated with defaults at registration and never edited. So
the owner could not set their bot's name, greeting or colours from the product, which is
the first thing ADMN-11's critical path asks them to do.

Two properties shape this module:

* **Writes go through the same validators the read path uses.** `appearance.normalise`
  and `suggestions.sanitise` decide what reaches the widget, so applying them here too
  means the value the owner is shown after saving is the value the visitor will get. A
  second, more permissive validation on write would let an owner save something that is
  silently rewritten on delivery.
* **The patch is partial by construction.** Every field is optional and `None` means
  "leave alone", so a form that only edits the welcome message cannot blank the name by
  omitting it. Sending an explicit `null` for a nullable field clears it.

`instructions` is deliberately absent from the response. It is the bot's system prompt —
long, and not useful to echo back a screenful at a time — and §17.2 treats it as config
rather than content. It *is* writable, and its length is capped here.
"""

import json
import uuid

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.audit.service import Action, record
from app.auth.dependencies import RequireAdmin, ScopedSession
from app.conversation import suggestions
from app.security.client_ip import client_ip
from app.widget import appearance

router = APIRouter(prefix="/bots", tags=["bot-config"])

MAX_NAME = 80
MAX_WELCOME = 500
MAX_INSTRUCTIONS = 8000
MIN_THRESHOLD = 0.0
MAX_THRESHOLD = 1.0


class UpdateBotRequest(BaseModel):
    """A patch. Absent or null fields are left unchanged."""

    name: str | None = Field(default=None, max_length=MAX_NAME)
    welcome_message: str | None = Field(default=None, max_length=MAX_WELCOME)
    instructions: str | None = Field(default=None, max_length=MAX_INSTRUCTIONS)
    confidence_threshold: float | None = Field(
        default=None, ge=MIN_THRESHOLD, le=MAX_THRESHOLD
    )
    powered_by_visible: bool | None = None
    appearance: dict | None = None
    suggestions: list[str] | None = None
    dynamic_suggestions: bool | None = None


class BotConfigDetail(BaseModel):
    id: str
    public_id: str
    name: str
    welcome_message: str | None
    confidence_threshold: float
    powered_by_visible: bool
    appearance: dict[str, str]
    suggestions: list[str]
    dynamic_suggestions: bool
    # Returned so the dashboard can say what the widget will actually use, rather than
    # leaving the owner to guess why a colour they typed is not showing.
    appearance_defaults: dict[str, str]


def _error(code: str, message: str, http: int) -> HTTPException:
    return HTTPException(
        status_code=http, detail={"error": {"code": code, "message": message}}
    )


def _fetch(session, bot_id: str, tenant_id) -> dict:
    try:
        parsed = uuid.UUID(bot_id)
    except ValueError:
        raise _error("invalid_uuid", "bad bot_id", status.HTTP_400_BAD_REQUEST) from None

    row = (
        session.execute(
            text(
                "SELECT id, public_id, name, welcome_message, instructions, appearance,"
                " suggestion_settings, confidence_threshold, powered_by_visible"
                " FROM bots WHERE id = :bot_id AND tenant_id = :tenant_id"
            ),
            {"bot_id": str(parsed), "tenant_id": str(tenant_id)},
        )
        .mappings()
        .first()
    )
    if row is None:
        # 404, not 403: a foreign bot id must be indistinguishable from a missing one.
        raise _error("bot_not_found", "unknown bot", status.HTTP_404_NOT_FOUND)
    return dict(row)


def _detail(row: dict, tenant_id) -> BotConfigDetail:
    normalised = appearance.normalise(row["appearance"])
    settings = suggestions.settings(row["suggestion_settings"])
    return BotConfigDetail(
        id=str(row["id"]),
        public_id=row["public_id"],
        name=row["name"],
        welcome_message=row["welcome_message"],
        confidence_threshold=row["confidence_threshold"],
        powered_by_visible=row["powered_by_visible"],
        appearance=normalised,
        suggestions=settings["proactive"],
        dynamic_suggestions=settings["dynamic"],
        appearance_defaults={
            "primaryColor": appearance.DEFAULT_PRIMARY,
            "position": appearance.DEFAULT_POSITION,
        },
    )


@router.get("", response_model=list[BotConfigDetail])
def list_bots(user: RequireAdmin, session: ScopedSession) -> list[BotConfigDetail]:
    """The tenant's bots, so the dashboard can address one without guessing an id.

    There is exactly one per tenant today (registration creates it), but the screen
    must not hard-code that: the query returns what exists, and the screen renders what
    it is given. When multi-bot lands, the screen keeps working.
    """
    rows = (
        session.execute(
            text(
                "SELECT id, public_id, name, welcome_message, instructions, appearance,"
                " suggestion_settings, confidence_threshold, powered_by_visible"
                " FROM bots WHERE tenant_id = :tenant_id ORDER BY created_at, id"
            ),
            {"tenant_id": str(user.tenant_id)},
        )
        .mappings()
        .all()
    )
    return [_detail(dict(row), user.tenant_id) for row in rows]


@router.get("/{bot_id}", response_model=BotConfigDetail)
def get_bot(
    bot_id: str, user: RequireAdmin, session: ScopedSession
) -> BotConfigDetail:
    row = _fetch(session, bot_id, user.tenant_id)
    return _detail(row, user.tenant_id)


@router.patch("/{bot_id}", response_model=BotConfigDetail)
def update_bot(
    bot_id: str,
    body: UpdateBotRequest,
    request: Request,
    user: RequireAdmin,
    session: ScopedSession,
) -> BotConfigDetail:
    row = _fetch(session, bot_id, user.tenant_id)

    assignments: list[str] = []
    params: dict[str, object] = {"bot_id": str(row["id"]), "tenant_id": str(user.tenant_id)}
    changed: list[str] = []

    if body.name is not None:
        name = body.name.strip()
        if not name:
            raise _error(
                "invalid_name", "name cannot be blank", status.HTTP_422_UNPROCESSABLE_CONTENT
            )
        assignments.append("name = :name")
        params["name"] = name
        changed.append("name")

    if body.welcome_message is not None:
        assignments.append("welcome_message = :welcome")
        params["welcome"] = body.welcome_message.strip() or None
        changed.append("welcome_message")

    if body.instructions is not None:
        assignments.append("instructions = :instructions")
        params["instructions"] = body.instructions.strip() or None
        changed.append("instructions")

    if body.confidence_threshold is not None:
        assignments.append("confidence_threshold = :threshold")
        params["threshold"] = body.confidence_threshold
        changed.append("confidence_threshold")

    if body.powered_by_visible is not None:
        assignments.append("powered_by_visible = :powered")
        params["powered"] = body.powered_by_visible
        changed.append("powered_by_visible")

    if body.appearance is not None:
        # Same allowlist the widget applies, so a saved value cannot be silently
        # dropped on delivery. Unknown keys are discarded rather than stored.
        assignments.append("appearance = CAST(:appearance AS jsonb)")
        params["appearance"] = _json(appearance.normalise(body.appearance))
        changed.append("appearance")

    if body.suggestions is not None:
        current = suggestions.settings(row["suggestion_settings"])
        cleaned = suggestions.sanitise(body.suggestions, question=None, minimum=0)
        merged = dict(current)
        merged["proactive"] = cleaned
        if body.dynamic_suggestions is not None:
            merged["dynamic"] = body.dynamic_suggestions
        assignments.append("suggestion_settings = CAST(:suggestions AS jsonb)")
        params["suggestions"] = _json(merged)
        changed.append("suggestions")
    elif body.dynamic_suggestions is not None:
        current = suggestions.settings(row["suggestion_settings"])
        current["dynamic"] = body.dynamic_suggestions
        assignments.append("suggestion_settings = CAST(:suggestions AS jsonb)")
        params["suggestions"] = _json(current)
        changed.append("dynamic_suggestions")

    if not assignments:
        # An empty patch is a no-op, not an error: a dashboard that saves an untouched
        # form should not have to special-case it.
        return _detail(_fetch(session, bot_id, user.tenant_id), user.tenant_id)

    session.execute(
        text(
            f"UPDATE bots SET {', '.join(assignments)}"
            " WHERE id = :bot_id AND tenant_id = :tenant_id"
        ),
        params,
    )
    record(
        session,
        tenant_id=user.tenant_id,
        actor_user_id=user.user_id,
        action=Action.BOT_CONFIG_UPDATED,
        entity_type="bot",
        entity_id=row["id"],
        # Field names only. The values include the system prompt and the greeting, and
        # an audit trail is read by more people than the config screen is.
        detail={"fields": changed},
        ip=client_ip(request),
    )
    # `tenant_session` closes without committing, so a route that forgets this discards
    # its writes and still returns 200.
    session.commit()

    return _detail(_fetch(session, bot_id, user.tenant_id), user.tenant_id)


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True)


class EmbedSnippet(BaseModel):
    """The copy-paste block for the tenant's own site (ADMN-12).

    Generated rather than stored: the public_id it embeds is already the unguessable
    handle the loader uses, so the snippet is a pure function of data the tenant can
    already read. Storing a copy would create a second thing to keep in sync with
    `bots.public_id`, and the failure mode of drift here is a snippet that silently
    points at someone else's bot.
    """

    public_id: str
    snippet: str
    script_url: str


@router.get("/{bot_id}/embed-snippet", response_model=EmbedSnippet)
def embed_snippet(
    bot_id: str, request: Request, user: RequireAdmin, session: ScopedSession
) -> EmbedSnippet:
    row = _fetch(session, bot_id, user.tenant_id)
    # Derived from the request rather than from configuration: the owner is looking at
    # this dashboard, so the origin that served it is the origin their visitors will
    # reach. A hard-coded production URL would produce a snippet that does not work on
    # the stack the owner is actually using.
    base = str(request.base_url).rstrip("/")
    script_url = f"{base}/widget/loader.js"
    snippet = f'<script src="{script_url}" data-bot="{row["public_id"]}" defer></script>'
    return EmbedSnippet(
        public_id=row["public_id"], snippet=snippet, script_url=script_url
    )
