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


def test_concurrent_hashing_serialises_and_bounds_its_own_memory() -> None:
    """The M0 gate found a login burst OOM-killing uvicorn.

    Argon2 is memory-hard: N concurrent hashes cost N x 64 MiB, so the backend
    serialises derivations behind ``ARGON2_MAX_CONCURRENT`` slots.

    Two properties, both measured on the *child's* own runtime:

    * **serialisation** — an instrumented overlap counter, which is deterministic
      and cannot be fooled by allocator behaviour. This is the property that
      actually protects the process.
    * **memory** — the child's own high-water mark, read from ``VmHWM``.

    This replaces an earlier version that asserted on ``ru_maxrss``. On Linux
    that value survives ``execve``: a child reports its *parent's* peak, not its
    own, so the assertion was really measuring how much memory **pytest** had
    used before reaching this test. It passed on a laptop (~90 MiB of suite) and
    failed on the smaller CI runner (194 MiB), with the hashing code identical
    in both. A threshold that tracks the harness rather than the subject is
    worse than no test, so the memory bound is now read from a metric the child
    owns.
    """
    import subprocess
    import sys

    env = {k: v for k, v in os.environ.items() if not k.startswith("ARGON2_")}
    script = """
import sys, threading
sys.path.insert(0, 'backend/api')
from app.config import ARGON2_MAX_CONCURRENT, ARGON2_MEMORY_KIB
from app.security import passwords

live = 0
peak = 0
lock = threading.Lock()
derive = passwords._derive

def counted(password, salt):
    global live, peak
    with lock:
        live += 1
        peak = max(peak, live)
    try:
        return derive(password, salt)
    finally:
        with lock:
            live -= 1

passwords._derive = counted

threads = [threading.Thread(target=passwords.hash_password, args=(f'pw-{i}',))
           for i in range(16)]
for t in threads:
    t.start()
for t in threads:
    t.join()

# VmHWM is this process's own high-water mark; ru_maxrss is not usable here
# because it is inherited across exec on Linux. Absent off Linux.
try:
    hwm_kib = next(int(line.split()[1]) for line in open('/proc/self/status')
                   if line.startswith('VmHWM'))
except OSError:
    hwm_kib = -1
print(ARGON2_MAX_CONCURRENT, ARGON2_MEMORY_KIB, peak, hwm_kib)
"""
    out = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        env=env, cwd=BACKEND_ROOT, timeout=300,
    )
    assert out.returncode == 0, out.stderr
    slots, memory_kib, peak_overlap, hwm_kib = (int(x) for x in out.stdout.split())

    assert slots >= 1
    # Serialisation is the property that protects the process. 16 callers must
    # never overlap more than the configured number of slots.
    assert peak_overlap <= slots, (
        f"{peak_overlap} hash derivations ran concurrently with only {slots} "
        f"slot(s) — the semaphore is not serialising"
    )

    # Bounded memory, measured on the child itself: the slots that may overlap,
    # the interpreter's own footprint, and headroom. Unbounded would be 16 x 64
    # MiB > 1 GiB.
    if hwm_kib < 0:
        pytest.skip("no VmHWM off Linux; serialisation above is the portable property")
    limit_kib = (slots + 1) * memory_kib + 128 * 1024
    assert hwm_kib < limit_kib, (
        f"16 concurrent hashes peaked at {hwm_kib / 1024:.0f} MiB with "
        f"m={memory_kib} KiB and {slots} slot(s) — hashing is not bounded"
    )


def test_the_overlap_counter_would_notice_lost_serialisation() -> None:
    """Proves the assertion above can actually fail.

    Bypassing the semaphore and calling the raw derivation from 16 threads must
    be observed as overlap. Runs with cheap parameters because the point is the
    counter, not the cost. If this passes trivially, the bound test above has
    gone blind.
    """
    import subprocess
    import sys

    env = {
        k: v for k, v in os.environ.items() if not k.startswith("ARGON2_")
    } | {"ARGON2_MEMORY_KIB": "32", "ARGON2_TIME_COST": "1",
         "ARGON2_PARALLELISM": "1", "ARGON2_MAX_CONCURRENT": "1"}
    script = """
import sys, threading
sys.path.insert(0, 'backend/api')
from app.config import ARGON2_MAX_CONCURRENT
from app.security import passwords

live = 0
peak = 0
lock = threading.Lock()
derive = passwords._derive

def counted(password, salt):
    global live, peak
    with lock:
        live += 1
        peak = max(peak, live)
    try:
        return derive(password, salt)
    finally:
        with lock:
            live -= 1

passwords._derive = counted
# Call _derive directly: hash_password would take the semaphore back.
threads = [threading.Thread(target=passwords._derive,
           args=(f'pw-{i}', b'sixteen-byte-salt'))
           for i in range(16)]
for t in threads:
    t.start()
for t in threads:
    t.join()
print(ARGON2_MAX_CONCURRENT, peak)
"""
    out = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        env=env, cwd=BACKEND_ROOT, timeout=120,
    )
    assert out.returncode == 0, out.stderr
    slots, peak = (int(x) for x in out.stdout.split())
    assert peak > slots, (
        f"the overlap counter saw {peak} concurrent derivation(s) with {slots} "
        "slot(s) even when the semaphore was bypassed, so it cannot detect "
        "lost serialisation"
    )
