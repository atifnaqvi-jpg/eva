"""Password hashing: Argon2id with the standard PHC string encoding.

Only Argon2id is implemented — never a fallback to a weaker hash (ARCHITECTURE
19.1). The encoded format is the usual PHC string, so the backend can be swapped
(e.g. to ``argon2-cffi``) without touching stored hashes or callers.
"""

import base64
import hashlib
import hmac
import secrets

from app.config import ARGON2_MEMORY_KIB, ARGON2_PARALLELISM, ARGON2_TIME_COST
from app.security.argon2 import ARGON2ID, argon2

ALGORITHM = "argon2id"
SALT_BYTES = 16
_B64 = base64.b64encode


def _b64(raw: bytes) -> str:
    return _B64(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(SALT_BYTES)
    digest = argon2(
        password.encode("utf-8"),
        salt,
        time_cost=ARGON2_TIME_COST,
        memory_kib=ARGON2_MEMORY_KIB,
        parallelism=ARGON2_PARALLELISM,
        hash_len=32,
        kind=ARGON2ID,
    )
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
    """Constant-time verification. Unknown/garbage encodings return False."""
    try:
        parsed = parse_encoded(encoded)
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
