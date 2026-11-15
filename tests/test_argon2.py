"""RFC 9106 §5 test vectors — the correctness gate for our in-repo Argon2.

If these fail, the implementation is wrong and must not be used for passwords.
"""

import pytest

from app.security.argon2 import ARGON2D, ARGON2I, ARGON2ID, argon2

# RFC 9106 §5: m=32 KiB, t=3, p=4, T=32, version 19.
PASSWORD = b"\x01" * 32
SALT = b"\x02" * 16
SECRET = b"\x03" * 8
ASSOCIATED = b"\x04" * 12

COMMON = dict(
    time_cost=3, memory_kib=32, parallelism=4, hash_len=32,
    secret=SECRET, associated=ASSOCIATED,
)

VECTORS = {
    ARGON2D: bytes.fromhex(
        "512b391b6f116297" "5371d30919734294" "f868e3be3984f3c1" "a13a4db9fabe4acb"
    ),
    ARGON2I: bytes.fromhex(
        "c814d9d1dc7f37aa" "13f0d77f2494bda1" "c8de6b016dd388d2" "9952a4c4672b6ce8"
    ),
    ARGON2ID: bytes.fromhex(
        "0d640df58d78766c" "08c037a34a8b53c9" "d01ef0452d75b65e" "b52520e96b01e659"
    ),
}


@pytest.mark.parametrize("kind", sorted(VECTORS), ids=["argon2d", "argon2i", "argon2id"])
def test_rfc9106_test_vectors(kind: int) -> None:
    tag = argon2(PASSWORD, SALT, kind=kind, **COMMON)
    assert tag == VECTORS[kind], f"variant {kind} does not match RFC 9106"


def test_distinct_salts_give_distinct_tags() -> None:
    a = argon2(b"correct horse", b"saltsaltsaltsalt", kind=ARGON2ID, **{**COMMON, "secret": b"", "associated": b""})
    b = argon2(b"correct horse", b"differentsalt123", kind=ARGON2ID, **{**COMMON, "secret": b"", "associated": b""})
    assert a != b


def test_secret_and_associated_data_change_the_tag() -> None:
    """Confirms the KDF actually consumes secret/AD, as the RFC requires."""
    base = dict(time_cost=2, memory_kib=32, parallelism=1, hash_len=32)
    without = argon2(b"pw", SALT, kind=ARGON2ID, **base)
    with_secret = argon2(b"pw", SALT, kind=ARGON2ID, secret=b"k" * 8, **base)
    with_ad = argon2(b"pw", SALT, kind=ARGON2ID, associated=b"a" * 4, **base)
    assert len({without, with_secret, with_ad}) == 3


def test_parameter_validation() -> None:
    with pytest.raises(ValueError):
        argon2(b"pw", SALT, parallelism=0)
    with pytest.raises(ValueError):
        argon2(b"pw", SALT, hash_len=2)
    with pytest.raises(ValueError):
        argon2(b"pw", SALT, time_cost=0)
