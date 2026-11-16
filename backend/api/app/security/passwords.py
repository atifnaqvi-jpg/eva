"""Password hashing: Argon2id with the standard PHC string encoding.

Argon2id only — never a fallback to a weaker hash (ARCHITECTURE 19.1). Hashes are
stored as PHC strings, so the backend can change without invalidating stored
passwords or touching callers.

Backends
--------
``argon2-cffi`` (production, default) — the reference C implementation via cffi,
run at OWASP parameters (64 MiB, t=3, p=4). Constant-time verification is done by
the library on the encoded string.

``pure-python`` (fallback, never for production) — ``app.security.argon2``, kept
because it is independently verified against the RFC 9106 §5 vectors. It is
selected only when ``argon2-cffi`` is not installed, which is the case for some
test environments. It is correct but ~2.4 s/MiB, so it cannot serve logins at
realistic parameters; HALT-01 (PLAN 8.1) ruled it out for the production path.
"""

import base64
import hashlib
import hmac
import secrets

import threading

from app.config import (
    ARGON2_MAX_CONCURRENT,
    ARGON2_MEMORY_KIB,
    ARGON2_PARALLELISM,
    ARGON2_TIME_COST,
)

ALGORITHM = "argon2id"
SALT_BYTES = 16
HASH_LEN = 32

# Bounds peak memory under a login burst (see ARGON2_MAX_CONCURRENT).
_HASH_SLOTS = threading.BoundedSemaphore(ARGON2_MAX_CONCURRENT)

try:  # pragma: no cover - exercised by whichever backend is installed
    from argon2.low_level import Type, hash_secret_raw, verify_secret

    BACKEND = "argon2-cffi"
except ImportError:  # pragma: no cover
    BACKEND = "pure-python"


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text + "=" * (-len(text) % 4))


def _derive(password: str, salt: bytes) -> bytes:
    if BACKEND == "argon2-cffi":
        return hash_secret_raw(
            secret=password.encode("utf-8"),
            salt=salt,
            time_cost=ARGON2_TIME_COST,
            memory_cost=ARGON2_MEMORY_KIB,
            parallelism=ARGON2_PARALLELISM,
            hash_len=HASH_LEN,
            type=Type.ID,
            version=19,
        )

    from app.security.argon2 import ARGON2ID, argon2

    return argon2(
        password.encode("utf-8"),
        salt,
        time_cost=ARGON2_TIME_COST,
        memory_kib=ARGON2_MEMORY_KIB,
        parallelism=ARGON2_PARALLELISM,
        hash_len=HASH_LEN,
        kind=ARGON2ID,
    )


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(SALT_BYTES)
    with _HASH_SLOTS:
        digest = _derive(password, salt)
    return (
        f"${ALGORITHM}$v=19$m={ARGON2_MEMORY_KIB},t={ARGON2_TIME_COST},"
        f"p={ARGON2_PARALLELISM}${_b64(salt)}${_b64(digest)}"
    )


def parse_encoded(encoded: str) -> dict[str, object]:
    parts = encoded.split("$")
    if len(parts) != 6 or parts[1] != ALGORITHM:
        raise ValueError("not an argon2id PHC string")
    _, _, version, params, salt, digest = parts
    if version != "v=19":
        raise ValueError(f"unsupported argon2 version {version}")
    parsed = dict(item.split("=", 1) for item in params.split(","))
    return {
        "memory_kib": int(parsed["m"]),
        "time_cost": int(parsed["t"]),
        "parallelism": int(parsed["p"]),
        "salt": _unb64(salt),
        "digest": _unb64(digest),
    }


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time verification. Unknown or malformed encodings return False.

    The parameters are read from the stored string, not from current config, so
    raising the cost later does not lock existing users out.
    """
    if BACKEND == "argon2-cffi":
        try:
            with _HASH_SLOTS:
                # verify_secret re-derives with the stored parameters and compares
                # in constant time; it raises rather than returning a bool.
                verify_secret(
                    encoded.encode("ascii"), password.encode("utf-8"), Type.ID
                )
        except Exception:
            return False
        return True

    try:
        parsed = parse_encoded(encoded)
        from app.security.argon2 import ARGON2ID, argon2

        with _HASH_SLOTS:
            candidate = argon2(
                password.encode("utf-8"),
                parsed["salt"],  # type: ignore[arg-type]
                time_cost=parsed["time_cost"],  # type: ignore[arg-type]
                memory_kib=parsed["memory_kib"],  # type: ignore[arg-type]
                parallelism=parsed["parallelism"],  # type: ignore[arg-type]
                hash_len=len(parsed["digest"]),  # type: ignore[arg-type]
                kind=ARGON2ID,
            )
    except (ValueError, KeyError):
        return False
    return hmac.compare_digest(candidate, parsed["digest"])  # type: ignore[arg-type]


def hash_token(token: str) -> str:
    """Sessions store only this digest, never the cookie value itself."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
