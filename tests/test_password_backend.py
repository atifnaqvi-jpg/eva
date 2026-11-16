"""The production password path must use the real Argon2id implementation.

HALT-01 (PLAN 8.1) ruled that the pure-Python implementation is not defensible in
production. These tests pin the production backend and prove it against the RFC
9106 §5 vectors, bounded memory, and a sane cost at OWASP parameters.
"""

import os
import time

import pytest

from app import config

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PASSWORD = b"\x01" * 32
SALT = b"\x02" * 16
SECRET = b"\x03" * 8
ASSOCIATED = b"\x04" * 12
# RFC 9106 §5: m=32 KiB, t=3, p=4, T=32, version 19.
RFC_ARGS = dict(time_cost=3, memory_kib=32, parallelism=4, hash_len=32,
                secret=SECRET, associated=ASSOCIATED)

VECTORS = {
    0: "512b391b6f1162975371d30919734294f868e3be3984f3c1a13a4db9fabe4acb",  # Argon2d
    1: "c814d9d1dc7f37aa13f0d77f2494bda1c8de6b016dd388d29952a4c4672b6ce8",  # Argon2i
    2: "0d640df58d78766c08c037a34a8b53c9d01ef0452d75b65eb52520e96b01e659",  # Argon2id
}


def test_production_backend_is_argon2_cffi() -> None:
    from app.security.passwords import BACKEND

    assert BACKEND == "argon2-cffi", (
        "production must use the C implementation; the pure-Python fallback is "
        "~2.4 s/MiB and was ruled out by HALT-01"
    )


def test_owasp_parameters_are_the_defaults() -> None:
    """The suite lowers the cost via env for speed, so the real defaults are read
    in a subprocess with those variables removed."""
    import subprocess
    import sys

    env = {k: v for k, v in os.environ.items() if not k.startswith("ARGON2_")}
    out = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, 'backend/api');"
         "from app.config import ARGON2_MEMORY_KIB as m, ARGON2_TIME_COST as t,"
         " ARGON2_PARALLELISM as p; print(m, t, p)"],
        capture_output=True, text=True, env=env, cwd=BACKEND_ROOT,
    )
    assert out.returncode == 0, out.stderr
    memory_kib, time_cost, parallelism = (int(x) for x in out.stdout.split())
    assert memory_kib >= 65536, "OWASP: at least 64 MiB for Argon2id"
    assert time_cost >= 3
    assert parallelism >= 1


@pytest.mark.parametrize("kind", sorted(VECTORS), ids=["argon2d", "argon2i", "argon2id"])
def test_real_backend_matches_rfc9106_vectors(kind: int) -> None:
    """Exercises argon2_ctx directly: the high-level helper cannot express the
    RFC's secret and associated-data inputs, and skipping them would not prove
    the library is really computing Argon2 correctly."""
    from argon2.low_level import ffi, lib

    out = ffi.new("uint8_t[]", RFC_ARGS["hash_len"])
    pwd = ffi.new("uint8_t[]", PASSWORD)
    salt = ffi.new("uint8_t[]", SALT)
    secret = ffi.new("uint8_t[]", SECRET)
    ad = ffi.new("uint8_t[]", ASSOCIATED)

    ctx = ffi.new("argon2_context *")
    ctx.out = out
    ctx.outlen = RFC_ARGS["hash_len"]
    ctx.pwd = pwd
    ctx.pwdlen = len(PASSWORD)
    ctx.salt = salt
    ctx.saltlen = len(SALT)
    ctx.secret = secret
    ctx.secretlen = len(SECRET)
    ctx.ad = ad
    ctx.adlen = len(ASSOCIATED)
    ctx.t_cost = RFC_ARGS["time_cost"]
    ctx.m_cost = RFC_ARGS["memory_kib"]
    ctx.lanes = RFC_ARGS["parallelism"]
    ctx.threads = RFC_ARGS["parallelism"]
    ctx.version = 19
    ctx.allocate_cbk = ffi.NULL
    ctx.free_cbk = ffi.NULL
    ctx.flags = 0

    assert lib.argon2_ctx(ctx, kind) == lib.ARGON2_OK
    assert bytes(ffi.buffer(out, RFC_ARGS["hash_len"])).hex() == VECTORS[kind]


def test_hash_and_verify_roundtrip_uses_configured_parameters() -> None:
    from app.security.passwords import hash_password, verify_password

    encoded = hash_password("correct-horse-battery")
    # Read at call time: the values depend on which test module set the env first.
    assert encoded.startswith(
        f"$argon2id$v=19$m={config.ARGON2_MEMORY_KIB},t={config.ARGON2_TIME_COST},"
        f"p={config.ARGON2_PARALLELISM}$"
    ), "the encoded string must record the parameters actually used"
    assert verify_password("correct-horse-battery", encoded)
    assert not verify_password("wrong", encoded)


def test_login_cost_is_serviceable() -> None:
    """A login must cost milliseconds, not minutes (HALT-01 measured ~2.6 min)."""
    from app.security.passwords import hash_password, verify_password

    encoded = hash_password("correct-horse-battery")
    started = time.perf_counter()
    assert verify_password("correct-horse-battery", encoded)
    elapsed = time.perf_counter() - started
    assert elapsed < 1.0, f"verification took {elapsed:.2f}s at OWASP parameters"


def test_parameters_are_read_from_the_stored_hash() -> None:
    """Cost can be raised later without locking out existing users."""
    from app.security.passwords import verify_password

    # A hash produced under the old, cheap parameters must still verify.
    from argon2.low_level import Type, hash_secret_raw

    cheap = hash_secret_raw(
        secret=b"correct-horse-battery", salt=b"s" * 16, time_cost=1,
        memory_cost=32, parallelism=1, hash_len=32, type=Type.ID, version=19,
    )
    import base64

    encoded = (
        "$argon2id$v=19$m=32,t=1,p=1$"
        + base64.b64encode(b"s" * 16).decode().rstrip("=")
        + "$"
        + base64.b64encode(cheap).decode().rstrip("=")
    )
    assert verify_password("correct-horse-battery", encoded)


def test_verify_rejects_tampered_and_malformed_hashes() -> None:
    from app.security.passwords import hash_password, verify_password

    encoded = hash_password("correct-horse-battery")
    # Flip a character of the digest.
    tampered = encoded[:-1] + ("A" if encoded[-1] != "A" else "B")
    assert not verify_password("correct-horse-battery", tampered)
    assert not verify_password("correct-horse-battery", "")
    assert not verify_password("correct-horse-battery", "not-a-phc-string")
    assert not verify_password("correct-horse-battery", "$argon2id$v=19$bad")


def test_concurrent_hashing_is_bounded_in_memory() -> None:
    """The M0 gate found a login burst OOM-killing uvicorn.

    Argon2 is memory-hard: N concurrent hashes cost N x 64 MiB. Runs in a clean
    subprocess at the real OWASP parameters so the suite's lowered cost does not
    mask the problem.
    """
    import subprocess
    import sys

    env = {k: v for k, v in os.environ.items() if not k.startswith("ARGON2_")}
    script = """
import resource, sys, threading
sys.path.insert(0, 'backend/api')
from app.config import ARGON2_MEMORY_KIB
from app.security import passwords

threads = [threading.Thread(target=passwords.hash_password, args=(f'pw-{i}',))
           for i in range(16)]
for t in threads:
    t.start()
for t in threads:
    t.join()
peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
print(f'{peak:.0f} {ARGON2_MEMORY_KIB}')
"""
    out = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        env=env, cwd=BACKEND_ROOT, timeout=300,
    )
    assert out.returncode == 0, out.stderr
    peak_mib, memory_kib = (float(x) for x in out.stdout.split())
    # 16 concurrent hashes at 64 MiB would be >1 GiB unbounded.
    assert peak_mib < 3 * memory_kib / 1024, (
        f"16 concurrent hashes peaked at {peak_mib:.0f} MiB with "
        f"m={int(memory_kib)} KiB — hashing is not bounded"
    )
