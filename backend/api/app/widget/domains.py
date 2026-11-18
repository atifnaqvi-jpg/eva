"""Domain ownership verification by DNS TXT record (SEC-15, §16.1, §19.1).

ARCHITECTURE §16.1 fixes the storage — ``bot_domains.domain``, ``.verified`` and
``.verification_token`` — and the widget already refuses any origin whose row is not
verified (``widget/router._allowed_domains``). What was missing is the step that makes
``verified`` true, which is why every tenant registered since the schema was written
had zero verified domains and no working widget (HALT-06).

**Option 2 of the HALT-06 ruling: token-checked, by DNS TXT record.** Adding a domain
stores a token and leaves ``verified = false``. Verification is a separate, explicit
action that looks the token up at ``_eva-verify.<domain>`` and only then flips the flag.
Nothing here verifies on add, on a timer, or as a side effect of a read: an owner who
adds a domain they do not control must not be able to make the widget serve on it, and
"the record is there now" is only meaningful at the moment someone asks.

**Why TXT and not a ``/.well-known/`` file.** The alternative in the ruling fetches
``https://<domain>/.well-known/eva-verify.txt`` from inside our network. That is a
server-side request to a tenant-chosen host: a new HTTP surface for §21.2's SSRF class,
and ``handover/webhooks.py`` exists because that class is real here. A TXT lookup is one
UDP packet to the resolver this container is already configured with — no connection to
the tenant's host at all, so there is nothing to redirect and no IP to pin.

**Why a hand-written resolver and not ``dnspython``.** It is not installed in the api
image (measured: 53 distributions, no DNS client; the only mention of dnspython in the
closure is a ``psycopg`` *dev* extra) and adding a runtime dependency is a G3.1 halt.
Standard library only: ``socket``, ``struct``. The parser is small because it only has
to read one answer type, and it is tested against a real UDP server rather than a
mocked one (see ``tests/test_domain_verification.py``).

**"Could not ask" is not "the record is missing".** A truncated answer (TC), a SERVFAIL,
an unreachable resolver or a reply to a different query all raise ``lookup_failed`` —
they never come back as an empty record list. Telling an owner their DNS is wrong when
we simply could not read it is the OPS-03 §8.38 defect in a third place, and here it
would send them editing a record that was already correct. NXDOMAIN is different: the
name genuinely does not exist, and that is an honest "no record".

**One attempt, five seconds, no retry.** The ruling asked for an explicit verify action
with a 5 s no-retry policy. Retrying inside the request would multiply the outbound
queries a caller can trigger and hide a real outage behind a long hang; the owner's
button *is* the retry.

The guards below exist because the queried *name* is tenant-controlled and we ask the
public resolver about it. We never connect to anything, so this is not request
forgery; it is an internal-name oracle — a tenant could otherwise use our resolver to
test whether ``db.internal`` exists. Names that cannot be a public web origin are
refused before any packet is built.
"""

from __future__ import annotations

import ipaddress
import secrets
import socket
import struct
from urllib.parse import urlsplit

from app.widget.tokens import TokenError, normalise_origin

# Where the record lives, and what it must say. The marker is fixed and public so the
# error message can tell an owner exactly what to publish; only the token is unguessable.
RECORD_PREFIX = "_eva-verify"
TOKEN_MARKER = "eva-verify="

# The ruling's policy: one lookup, five seconds.
TIMEOUT_SECONDS = 5.0

# A DNS answer over UDP is at most 512 bytes without EDNS. Read a bit more so a
# resolver that appends authority data still parses, and treat the TC bit as failure
# rather than trying to read a partial answer.
MAX_RESPONSE_BYTES = 4096

_TYPE_TXT = 16

# Suffixes that name something only reachable inside a network, so asking the public
# resolver about them would turn this endpoint into an existence oracle for internal
# services. `.arpa` is the reverse zones (an address lookup); the rest are the special-use
# and single-label conventions a corporate network uses.
#
# The RFC 2606/6761 reserved names (`.test`, `.example`, `.invalid`) are deliberately NOT
# refused: they are guaranteed never to resolve to an internal service, so querying them
# reveals nothing, and allowing them lets the test suite drive the real UDP path with a
# real nameserver instead of replacing the code under test.
_REFUSED_SUFFIXES = (
    ".local",
    ".localhost",
    ".localdomain",
    ".internal",
    ".intranet",
    ".lan",
    ".home",
    ".corp",
    ".arpa",
    ".onion",
)


class DomainError(Exception):
    """A verification fact worth reporting, split into "your DNS" and "our lookup".

    ``code`` is what the API returns; the router maps a ``lookup_failed`` to 503 and
    everything else to 422, so a transient resolver problem can never be shown to an
    owner as "your record is missing".
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def new_token() -> str:
    """32 hex characters: no case, no separator, safe in any DNS control panel."""
    return secrets.token_hex(16)


def record_name(domain: str) -> str:
    """``https://Shop.Example.com/about`` -> ``_eva-verify.shop.example.com``.

    The stored entry may be a bare host (what an owner types) or an origin, and the
    widget's own matcher normalises both the same way, so this uses that function
    rather than a second, more permissive parser: a domain that the widget would never
    match must not be one we happily verify.
    """
    host = hostname(domain)
    return f"{RECORD_PREFIX}.{host}"


def hostname(domain: str) -> str:
    """The public DNS name inside a stored domain entry, or raise ``DomainError``."""
    try:
        origin = normalise_origin(domain if "://" in domain else f"https://{domain}")
    except TokenError as exc:
        raise DomainError("invalid_domain", str(exc)) from None

    host = urlsplit(origin).hostname or ""
    if not host:
        raise DomainError("invalid_domain", "the entry has no host")

    # An IP has no name to publish a record under, so "verify this origin" has no
    # answer for it. Refusing is honest; verifying it would be a claim nobody made.
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise DomainError(
            "invalid_domain",
            "an IP address has no DNS record to verify; use the site's hostname",
        )

    if host.endswith("."):
        host = host[:-1]
    if "." not in host:
        raise DomainError(
            "invalid_domain", "a single-label host has no public record to check"
        )
    for suffix in _REFUSED_SUFFIXES:
        if host == suffix.lstrip(".") or host.endswith(suffix):
            raise DomainError(
                "invalid_domain", f"{suffix} is not a public web domain"
            )
    return host


def matches(strings: list[str], token: str) -> bool:
    """Does any TXT string carry this token, in the documented form?

    The marker is compared too, so a provider's unrelated TXT value at the same name
    cannot verify by coincidence, and a token is never a substring of a longer value.
    Lowercased because hex is case-insensitive and some panels upper-case values.
    """
    expected = f"{TOKEN_MARKER}{token}".lower()
    return any(value.strip().lower() == expected for value in strings)


def verify_record(domain: str, token: str, *, server: str | None = None) -> None:
    """Look the token up in DNS, or raise ``DomainError`` explaining why not."""
    if not token:
        raise DomainError("no_token", "this domain has no verification token")
    name = record_name(domain)
    values = query_txt(name, server=server)
    if not values:
        raise DomainError(
            "no_txt_record",
            f"no TXT record found at {name}; publish "
            f"{TOKEN_MARKER}{token} and try again",
        )
    if not matches(values, token):
        raise DomainError(
            "token_mismatch",
            f"a TXT record exists at {name} but does not carry this domain's token",
        )


# --- the resolver ---------------------------------------------------------


def query_txt(name: str, *, server: str | None = None, timeout: float = TIMEOUT_SECONDS) -> list[str]:
    """The TXT strings at ``name``, or ``DomainError('lookup_failed')``.

    An empty list means the name exists with no TXT record (NODATA) or does not exist
    at all (NXDOMAIN). Every other outcome — timeout, SERVFAIL, truncation — raises,
    because a partial read reported as "no record" is the failure this module is most
    likely to hide.

    ``server`` is ``host`` or ``host:port``. Production passes a bare host (port 53); the
    port form exists so the test suite can drive this function end to end against a UDP
    server on an ephemeral port instead of replacing the code under test.
    """
    target, port = _split_resolver(server or _resolver())
    if not target:
        raise DomainError(
            "lookup_failed", "no DNS resolver is configured in this container"
        )

    query_id, packet = _build_query(name)
    family = socket.AF_INET6 if ":" in target else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.sendto(packet, (target, port))
            data, _ = sock.recvfrom(MAX_RESPONSE_BYTES)
    except TimeoutError:
        raise DomainError(
            "lookup_failed", f"the DNS resolver did not answer within {timeout:.0f} s"
        ) from None
    except OSError as exc:
        raise DomainError("lookup_failed", f"could not reach the DNS resolver: {exc}") from None

    return _parse_response(data, query_id)


def _split_resolver(value: str) -> tuple[str, int]:
    """``host`` -> ``(host, 53)``; ``host:5353`` -> ``(host, 5353)``.

    A bare IPv6 literal contains colons too, so only a trailing ``:digits`` counts as a
    port — otherwise ``2001:db8::1`` would be split into a host that does not exist.
    """
    value = value.strip()
    if not value:
        return "", 53
    host, _, maybe_port = value.rpartition(":")
    if host and maybe_port.isdigit():
        return host, int(maybe_port)
    return value, 53


def _resolver() -> str:
    """The first nameserver in `/etc/resolv.conf`, which is what every container has.

    Only the first, deliberately: falling through a list would mean an unknown number
    of packets per verify and a timeout that is no longer the 5 s the ruling names.
    """
    try:
        with open("/etc/resolv.conf", encoding="ascii", errors="replace") as handle:
            for line in handle:
                fields = line.split()
                if len(fields) >= 2 and fields[0] == "nameserver":
                    return fields[1]
    except OSError:
        return ""
    return ""


def _build_query(name: str) -> tuple[int, bytes]:
    """A single-question, recursion-desired TXT query."""
    try:
        encoded = name.encode("ascii")
    except UnicodeEncodeError:
        # We do not convert IDN to punycode: a name the owner typed in Unicode is not
        # the name that appears in DNS, so guessing the conversion would verify a
        # different host than the one they meant.
        raise DomainError(
            "invalid_domain", "publish the punycode (xn--) form of an internationalised domain"
        ) from None

    if not encoded or len(encoded) > 253:
        raise DomainError("invalid_domain", "the name is empty or too long")

    labels = encoded.split(b".")
    if any(not label or len(label) > 63 for label in labels):
        raise DomainError("invalid_domain", "a DNS label is empty or longer than 63 bytes")

    query_id = secrets.randbelow(65536)
    header = struct.pack("!HHHHHH", query_id, 0x0100, 1, 0, 0, 0)
    question = b"".join(bytes([len(label)]) + label for label in labels)
    return query_id, header + question + b"\x00" + struct.pack("!HH", _TYPE_TXT, 1)


def _parse_response(data: bytes, query_id: int) -> list[str]:
    if len(data) < 12:
        raise DomainError("lookup_failed", "the DNS answer was too short to be a reply")

    answer_id, flags, questions, answers, _authority, _additional = struct.unpack(
        "!HHHHHH", data[:12]
    )
    if answer_id != query_id:
        # A stray or spoofed packet. Reporting "no record" here would let anything on
        # the path decide that a verified domain is not.
        raise DomainError("lookup_failed", "the DNS answer did not match the query")

    if flags & 0x0200:
        raise DomainError(
            "lookup_failed",
            "the DNS answer was truncated; the record is too large for one lookup",
        )

    rcode = flags & 0x000F
    if rcode == 3:  # NXDOMAIN: the name genuinely does not exist.
        return []
    if rcode != 0:
        raise DomainError("lookup_failed", f"the DNS resolver answered with error {rcode}")

    offset = 12
    for _ in range(questions):
        _, offset = _read_name(data, offset)
        offset += 4  # qtype + qclass

    strings: list[str] = []
    for _ in range(answers):
        _, offset = _read_name(data, offset)
        if offset + 10 > len(data):
            raise DomainError("lookup_failed", "the DNS answer ended mid-record")
        rtype, _rclass, _ttl, rdlength = struct.unpack("!HHIH", data[offset : offset + 10])
        offset += 10
        if offset + rdlength > len(data):
            raise DomainError("lookup_failed", "the DNS answer ended mid-record")
        rdata = data[offset : offset + rdlength]
        offset += rdlength
        if rtype != _TYPE_TXT:
            continue
        strings.append(_read_txt(rdata))
    return strings


def _read_txt(rdata: bytes) -> str:
    """One TXT rdata is a sequence of length-prefixed strings, concatenated (RFC 1035)."""
    pieces: list[bytes] = []
    position = 0
    while position < len(rdata):
        length = rdata[position]
        position += 1
        if position + length > len(rdata):
            raise DomainError("lookup_failed", "a TXT value ended mid-string")
        pieces.append(rdata[position : position + length])
        position += length
    return b"".join(pieces).decode("utf-8", "replace")


def _read_name(data: bytes, offset: int) -> tuple[str, int]:
    """Read a possibly-compressed name. Pointers may only jump backwards."""
    labels: list[str] = []
    end = offset
    jumped = False
    seen = 0
    while True:
        if offset >= len(data):
            raise DomainError("lookup_failed", "a DNS name ran past the end of the answer")
        length = data[offset]
        if length == 0:
            offset += 1
            if not jumped:
                end = offset
            break
        if length & 0xC0 == 0xC0:
            if offset + 2 > len(data):
                raise DomainError("lookup_failed", "a DNS compression pointer was cut off")
            pointer = struct.unpack("!H", data[offset : offset + 2])[0] & 0x3FFF
            if not jumped:
                end = offset + 2
            # A forward jump lets an answer loop forever; refusing is the only safe read.
            if pointer >= offset:
                raise DomainError("lookup_failed", "a DNS compression pointer pointed forward")
            offset = pointer
            jumped = True
            seen += 1
            if seen > 64:
                raise DomainError("lookup_failed", "a DNS name compressed too deeply")
            continue
        offset += 1
        if offset + length > len(data):
            raise DomainError("lookup_failed", "a DNS label ran past the end of the answer")
        labels.append(data[offset : offset + length].decode("ascii", "replace"))
        offset += length
    return ".".join(labels), end
