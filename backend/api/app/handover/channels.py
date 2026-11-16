"""Delivery channels for handover notifications (§13.4, HAND-03).

``HandoverChannel`` is the V1 expansion point named in the blueprint: the MVP ships
``EmailChannel``, and Zendesk/Freshdesk/agent-inbox channels become new classes
without touching the handover flow. To keep that promise honest, nothing outside
this module knows how a notification is delivered — the caller hands over a
``Notification`` and gets back a ``Delivery``.

**Stdlib only, on purpose.** ``smtplib`` and ``email.message`` cover SMTP with
STARTTLS, which is what an MVP needs; adding a mail library would be a dependency
grant for no capability. The consequence, stated plainly: no connection pooling and
no queueing here — a slow SMTP server would block its caller, so delivery is run off
the request path (see ``service.notify``).
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Notification:
    """One thing to tell the owner about.

    ``transcript`` is plain text (see ``transcript.compile_transcript``); a channel
    that cannot render it should say so rather than silently dropping it.
    """

    channel: str  # "email" | "webhook" — mirrors handovers.channel's check constraint
    to: str
    subject: str
    body: str
    reply_to: str | None = None
    # Included so a channel can attach the machine-readable form (a helpdesk ticket
    # wants JSON, a human wants the text body).
    conversation_id: str | None = None


@dataclass(frozen=True)
class Delivery:
    """Outcome of one delivery attempt.

    A failure is a value, not an exception: the visitor has already been told a human
    is coming, so an SMTP outage must be recorded on the handover row and logged, not
    turned into an error the visitor sees (§13 flow, HAND-03).
    """

    ok: bool
    detail: str = ""


class HandoverChannel(Protocol):
    """What a delivery mechanism has to provide."""

    name: str

    def send(self, notification: Notification) -> Delivery: ...


class EmailChannel:
    """SMTP delivery (MVP channel).

    Configured from the environment: a handover goes to the tenant's own address, so
    the *sender* is the platform's, not theirs.
    """

    name = "email"

    def __init__(
        self,
        *,
        host: str,
        port: int = 587,
        username: str | None = None,
        password: str | None = None,
        from_addr: str = "no-reply@eva.local",
        use_tls: bool = True,
        timeout: float = 15.0,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.from_addr = from_addr
        self.use_tls = use_tls
        self.timeout = timeout

    def send(self, notification: Notification) -> Delivery:
        message = EmailMessage()
        message["From"] = self.from_addr
        message["To"] = notification.to
        message["Subject"] = notification.subject
        message["Date"] = formatdate(localtime=True)
        message["Message-ID"] = make_msgid(domain="eva.local")
        if notification.reply_to:
            # Set explicitly: a reply must go to the visitor, not to no-reply, and
            # some clients hide Reply-To unless it is present.
            message["Reply-To"] = notification.reply_to
        message.set_content(notification.body)

        try:
            with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as smtp:
                if self.use_tls:
                    smtp.starttls(context=ssl.create_default_context())
                if self.username and self.password:
                    smtp.login(self.username, self.password)
                smtp.send_message(message)
        except (OSError, smtplib.SMTPException) as exc:
            logger.warning(
                "handover email failed (to=%s, conversation=%s): %s",
                notification.to,
                notification.conversation_id,
                exc,
            )
            return Delivery(ok=False, detail=f"{type(exc).__name__}: {exc}")
        return Delivery(ok=True, detail=f"sent to {notification.to}")


def build_channel() -> HandoverChannel | None:
    """The configured channel, or ``None`` when SMTP is not set up.

    Returning ``None`` rather than raising keeps an unconfigured deployment
    working: the handover row is still written and the transcript still stored, so
    the owner can see it in the dashboard once M5 lands. A deployment without a
    mail server is a degraded state, not a broken one.
    """
    from app.config import (
        SMTP_FROM,
        SMTP_HOST,
        SMTP_PASSWORD,
        SMTP_PORT,
        SMTP_TLS,
        SMTP_USER,
    )

    if not SMTP_HOST:
        return None
    return EmailChannel(
        host=SMTP_HOST,
        port=SMTP_PORT,
        username=SMTP_USER or None,
        password=SMTP_PASSWORD or None,
        from_addr=SMTP_FROM,
        use_tls=SMTP_TLS,
    )
