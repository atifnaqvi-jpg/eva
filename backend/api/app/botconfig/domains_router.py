"""Allowed-domain management and verification (SEC-15, §17.2, §16.1).

ARCHITECTURE §17.2 names one route, ``POST /api/v1/bots/{id}/domains``, described as
"Add + verify allowed domain". The sentence hides two operations with different trust:
adding is the owner's claim, verifying is proof of control. They are separate endpoints
here because merging them is exactly what the HALT-06 ruling rejected — option 1
("self-assert on add") is the cheap version that makes ``verified`` decorative and lets
any tenant serve their bot on another company's domain.

The shape, all under the bot that serves it:

| Route | Meaning |
|---|---|
| `GET /bots/{id}/domains` | What is configured, verified or not, and the record to publish |
| `POST /bots/{id}/domains` | Add a domain: stores it **unverified** with a fresh token |
| `POST /bots/{id}/domains/{domain_id}/verify` | Look the token up in DNS; on match, flip the flag |
| `DELETE /bots/{id}/domains/{domain_id}` | Remove an entry |

Only the four routes above exist; ``verify`` adds a path under an existing resource with
no schema change, the same additive class as SEC-10's ``DELETE``, and it is written up in
the registry row rather than assumed free.

**Admin, not owner.** §17.2 lists domain management under bot configuration, which
ADMN-03 already exposes to `RequireAdmin`. Requiring owner here would be a stricter rule
than the rest of that surface and would let an admin edit the bot while being unable to
say where it may run — a split that produces a working widget in one screen and a 403 in
another. The privilege is not trivial (it decides where the bot is served) but it is the
same privilege as publishing the knowledge it answers from, so it follows that surface.

**The token is not a secret after it is published.** It goes in a public TXT record, so
returning it in the list is correct: the owner needs it to publish the record, and anyone
who can read their DNS can read it anyway. What must not happen is *guessing* it, which is
why it is 128 random bits and why verification compares the whole value.

**Re-verification stays possible and re-tokens on request.** A verified row keeps its
token, so an owner who changes DNS can verify again; and adding a domain that already
exists returns the existing row rather than minting a second token, because two live
tokens for one domain would mean an old record still verifies.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from app import config
from app.audit.service import Action, record
from app.auth.dependencies import RequireAdmin, ScopedSession
from app.security.client_ip import client_ip
from app.widget import domains

router = APIRouter(prefix="/bots", tags=["bot-domains"])

MAX_DOMAIN_LENGTH = 253


class DomainSummary(BaseModel):
    id: str
    domain: str
    verified: bool
    # Everything the owner needs to publish the record, so the screen never has to
    # compose the name itself and get it wrong.
    record_name: str
    record_value: str


class AddDomainRequest(BaseModel):
    domain: str = Field(min_length=1, max_length=MAX_DOMAIN_LENGTH)


class VerifyResult(BaseModel):
    """The verification outcome, on both success and a refused record.

    Returned as a body rather than only an error code because "no record found" is a
    normal step in setting this up, not a failure of the request: the owner needs the
    name and value to copy, and the reason theirs did not match.
    """

    domain: DomainSummary
    verified: bool
    detail: str = ""


def _error(code: str, message: str, http: int) -> HTTPException:
    return HTTPException(
        status_code=http, detail={"error": {"code": code, "message": message}}
    )


def _bot(session, bot_id: str, tenant_id) -> uuid.UUID:
    """Resolve a bot the caller may touch, or 404.

    404 rather than 403 for a foreign id: whether another tenant's bot exists is not
    something this endpoint should answer (same rule as ``botconfig._fetch``).
    """
    try:
        parsed = uuid.UUID(bot_id)
    except ValueError:
        raise _error("invalid_uuid", "bad bot_id", status.HTTP_400_BAD_REQUEST) from None

    found = session.execute(
        text("SELECT id FROM bots WHERE id = :id AND tenant_id = :tenant_id"),
        {"id": str(parsed), "tenant_id": str(tenant_id)},
    ).scalar()
    if found is None:
        raise _error("bot_not_found", "unknown bot", status.HTTP_404_NOT_FOUND)
    return found


def _summary(row: dict) -> DomainSummary:
    token = row["verification_token"] or ""
    try:
        name = domains.record_name(row["domain"])
    except domains.DomainError:
        # A row written before this route existed can hold an entry this module refuses
        # to build a record name for (an origin with an IP, a `*.internal` name). Naming
        # the record is a display concern: refusing to list the row would hide every
        # other domain on the bot behind one bad one.
        name = ""
    return DomainSummary(
        id=str(row["id"]),
        domain=row["domain"],
        verified=bool(row["verified"]),
        # An entry stored before this row existed may have no token; naming the record
        # it *would* need beats returning a blank the screen cannot explain.
        record_name=name,
        record_value=f"{domains.TOKEN_MARKER}{token}" if token else "",
    )


# §16.1 fixes these columns and `bot_domains` carries no timestamp, so the listing is
# ordered by domain — the only stable, tenant-meaningful column on the row.
_COLUMNS = "id, domain, verified, verification_token"


@router.get("/{bot_id}/domains", response_model=list[DomainSummary])
def list_domains(
    bot_id: str, user: RequireAdmin, session: ScopedSession
) -> list[DomainSummary]:
    bot = _bot(session, bot_id, user.tenant_id)
    rows = session.execute(
        text(
            f"SELECT {_COLUMNS} FROM bot_domains"
            " WHERE tenant_id = :tenant_id AND bot_id = :bot_id ORDER BY domain"
        ),
        {"tenant_id": str(user.tenant_id), "bot_id": str(bot)},
    ).mappings().all()
    return [_summary(dict(row)) for row in rows]


@router.post(
    "/{bot_id}/domains", response_model=DomainSummary, status_code=status.HTTP_201_CREATED
)
def add_domain(
    bot_id: str,
    body: AddDomainRequest,
    request: Request,
    user: RequireAdmin,
    session: ScopedSession,
) -> DomainSummary:
    """Register a domain **unverified**, with the token it must publish.

    Verification never happens here. A row that arrives verified would mean the widget
    serves a host nobody has proved they control, which is the whole defect SEC-15
    closes.
    """
    bot = _bot(session, bot_id, user.tenant_id)

    try:
        # Store the *host*, not the origin the owner pasted: `bot_domains.domain` is
        # matched by `tokens.origin_matches`, which normalises a bare entry as `https://`,
        # so a stored `http://host:8080` would be a row the widget never matches.
        host = domains.hostname(body.domain)
    except domains.DomainError as exc:
        raise _bad_domain(exc) from None

    existing = session.execute(
        text(
            f"SELECT {_COLUMNS} FROM bot_domains"
            " WHERE tenant_id = :tenant_id AND bot_id = :bot_id AND domain = :domain"
        ),
        {"tenant_id": str(user.tenant_id), "bot_id": str(bot), "domain": host},
    ).mappings().first()
    if existing is not None:
        # Idempotent add: an owner who submits the same form twice must not end up with
        # two tokens, one of which is in a record they already published.
        return _summary(dict(existing))

    row = session.execute(
        text(
            "INSERT INTO bot_domains (tenant_id, bot_id, domain, verified, verification_token)"
            " VALUES (:tenant_id, :bot_id, :domain, false, :token)"
            f" RETURNING {_COLUMNS}"
        ),
        {
            "tenant_id": str(user.tenant_id),
            "bot_id": str(bot),
            "domain": host,
            "token": domains.new_token(),
        },
    ).mappings().one()

    record(
        session,
        tenant_id=user.tenant_id,
        action=Action.DOMAIN_ADDED,
        entity_type="bot_domains",
        entity_id=row["id"],
        actor_user_id=user.user_id,
        # The domain is the tenant's own data; the token is not logged. It is public by
        # design, but a token in an append-only table is a value nobody can withdraw.
        detail={"bot_id": str(bot), "domain": host},
        ip=client_ip(request),
    )
    # `tenant_session` closes without committing, so a route that forgets this returns
    # 201 and persists nothing.
    session.commit()

    return _summary(dict(row))


@router.post("/{bot_id}/domains/{domain_id}/verify", response_model=VerifyResult)
def verify_domain(
    bot_id: str,
    domain_id: str,
    request: Request,
    user: RequireAdmin,
    session: ScopedSession,
) -> VerifyResult:
    """Look the token up at ``_eva-verify.<domain>`` and flip ``verified`` on a match.

    An explicit action: nothing verifies on a timer or as a side effect of a read, so an
    owner who has not yet published the record simply gets told what to publish.

    A transient resolver failure is **503**, not a 200 with ``verified: false``: the two
    mean different things to the owner and only one of them is their problem.
    """
    bot = _bot(session, bot_id, user.tenant_id)

    try:
        parsed = uuid.UUID(domain_id)
    except ValueError:
        raise _error("invalid_uuid", "bad domain_id", status.HTTP_400_BAD_REQUEST) from None

    row = session.execute(
        text(
            f"SELECT {_COLUMNS} FROM bot_domains"
            " WHERE tenant_id = :tenant_id AND bot_id = :bot_id AND id = :id"
        ),
        {"tenant_id": str(user.tenant_id), "bot_id": str(bot), "id": str(parsed)},
    ).mappings().first()
    if row is None:
        raise _error("domain_not_found", "no such domain", status.HTTP_404_NOT_FOUND)

    try:
        domains.verify_record(
            row["domain"],
            row["verification_token"] or "",
            server=config.DNS_RESOLVER or None,
        )
    except domains.DomainError as exc:
        if exc.code == "lookup_failed":
            # "We could not ask" is not "your record is missing" (§8.38's lesson).
            raise _error("lookup_failed", exc.message, status.HTTP_503_SERVICE_UNAVAILABLE) from None
        # A refused record is a normal step in setting the domain up, so it is a 200
        # with the reason attached: the screen shows it beside the values to copy.
        record(
            session,
            tenant_id=user.tenant_id,
            action=Action.DOMAIN_VERIFY_FAILED,
            entity_type="bot_domains",
            entity_id=row["id"],
            actor_user_id=user.user_id,
            detail={"bot_id": str(bot), "domain": row["domain"], "reason": exc.code},
            ip=client_ip(request),
        )
        session.commit()
        return VerifyResult(domain=_summary(dict(row)), verified=False, detail=exc.message)

    session.execute(
        text(
            "UPDATE bot_domains SET verified = true"
            " WHERE tenant_id = :tenant_id AND id = :id"
        ),
        {"tenant_id": str(user.tenant_id), "id": str(row["id"])},
    )
    record(
        session,
        tenant_id=user.tenant_id,
        action=Action.DOMAIN_VERIFIED,
        entity_type="bot_domains",
        entity_id=row["id"],
        actor_user_id=user.user_id,
        detail={"bot_id": str(bot), "domain": row["domain"]},
        ip=client_ip(request),
    )
    session.commit()

    updated = session.execute(
        text(f"SELECT {_COLUMNS} FROM bot_domains WHERE id = :id"),
        {"id": str(row["id"])},
    ).mappings().one()
    return VerifyResult(
        domain=_summary(dict(updated)),
        verified=True,
        detail="the record matched; this domain may now serve the widget",
    )


@router.delete(
    "/{bot_id}/domains/{domain_id}", status_code=status.HTTP_204_NO_CONTENT
)
def delete_domain(
    bot_id: str, domain_id: str, user: RequireAdmin, session: ScopedSession, request: Request
) -> None:
    """Remove an entry, which stops the widget serving that origin on the next request.

    There is no separate "unverify": the row is the permission, so deleting it is how a
    domain stops being allowed, and a disabled row would be a second concept to keep in
    step with the widget's filter.
    """
    bot = _bot(session, bot_id, user.tenant_id)
    try:
        parsed = uuid.UUID(domain_id)
    except ValueError:
        raise _error("invalid_uuid", "bad domain_id", status.HTTP_400_BAD_REQUEST) from None

    row = session.execute(
        text(
            "DELETE FROM bot_domains"
            " WHERE tenant_id = :tenant_id AND bot_id = :bot_id AND id = :id"
            " RETURNING domain"
        ),
        {"tenant_id": str(user.tenant_id), "bot_id": str(bot), "id": str(parsed)},
    ).mappings().first()
    if row is None:
        raise _error("domain_not_found", "no such domain", status.HTTP_404_NOT_FOUND)

    record(
        session,
        tenant_id=user.tenant_id,
        action=Action.DOMAIN_REMOVED,
        entity_type="bot_domains",
        entity_id=parsed,
        actor_user_id=user.user_id,
        detail={"bot_id": str(bot), "domain": row["domain"]},
        ip=client_ip(request),
    )
    session.commit()


def _bad_domain(exc: domains.DomainError) -> HTTPException:
    return _error(exc.code, exc.message, status.HTTP_422_UNPROCESSABLE_CONTENT)
