"""Encrypted secret storage behind a ``SecretVault`` (SEC-03, ARCHITECTURE §16.2).

Secrets live in **age-encrypted files on the host**. The database stores only a
``key_reference`` — a relative path — so a database dump contains no key material
and a filesystem dump without the age identity contains no plaintext. That is the
whole point of §16.2, and it is why this module never returns ciphertext to a
caller and never accepts a path from a request body.

Layout, under ``SECRET_VAULT_DIR``::

    <dir>/<tenant_id>/<provider>.age

**The identity file is outside the repository.** ``SECRET_VAULT_IDENTITY`` points
at an age identity (``AGE-SECRET-KEY-1...``); it is not in the repo, not in the
image and not in the database. Losing it loses every BYOK key, which is the
correct failure mode for a file that can decrypt them all.

Why ``age`` and not a KMS or ``cryptography``: STACK.md locks "age/sops-encrypted
files behind a ``SecretVault`` interface, Vault/KMS later". The interface is the
part that matters — swapping the backend later is a new class, not a new schema.

Threat model, stated so the limits are visible:

* An attacker with the database **and** the identity file has the keys. No design
  prevents that; the identity is on the host so that a *backup* of one is not a
  backup of the other.
* An attacker with the database alone learns which providers a tenant uses, which
  the dashboard already shows.
* Ciphertext is authenticated (age is X25519 + ChaCha20-Poly1305), so a modified
  file fails to decrypt rather than yielding attacker-chosen plaintext.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

DEFAULT_VAULT_DIR = "/var/lib/eva/secrets"

# Providers whose names become path components, so the set is closed rather than
# sanitised: a caller-supplied provider string in a path is a traversal bug waiting
# to happen, and an allowlist cannot be outgrown by a crafted value.
ALLOWED_PROVIDERS = frozenset({"ollama_cloud", "deepseek", "openai", "anthropic"})


class VaultError(RuntimeError):
    """Any failure that is not the caller's fault (missing binary, bad identity)."""


class SecretNotFound(VaultError):
    """The reference does not resolve to a stored secret."""


class InvalidProvider(ValueError):
    """Provider name is not in ``ALLOWED_PROVIDERS``."""


def _vault_dir() -> Path:
    return Path(os.environ.get("SECRET_VAULT_DIR", DEFAULT_VAULT_DIR))


def _identity_path() -> Path:
    value = os.environ.get("SECRET_VAULT_IDENTITY", "")
    if not value:
        raise VaultError(
            "SECRET_VAULT_IDENTITY is not set; BYOK cannot work without an age identity"
        )
    path = Path(value)
    if not path.is_file():
        raise VaultError(f"age identity file not found: {path}")
    return path


def _require_age() -> str:
    binary = shutil.which("age")
    if binary is None:
        raise VaultError("the `age` binary is not installed")
    return binary


def _require_age_keygen() -> str:
    # A separate binary: `age -y` is not "derive recipient from identity", it is the
    # passphrase-interactive mode. Only `age-keygen -y` prints the recipient.
    binary = shutil.which("age-keygen")
    if binary is None:
        raise VaultError("the `age-keygen` binary is not installed")
    return binary


def _validate_provider(provider: str) -> str:
    if provider not in ALLOWED_PROVIDERS:
        raise InvalidProvider(f"unknown provider: {provider}")
    return provider


def key_reference(tenant_id: uuid.UUID, provider: str) -> str:
    """The value stored in ``api_credentials.key_reference``.

    Relative on purpose. An absolute path in the database would bake the deployment
    layout into rows and break the moment the vault directory moves; it would also
    leak filesystem structure into any backup of the table.
    """
    return f"{tenant_id}/{_validate_provider(provider)}.age"


def _path_for(reference: str) -> Path:
    """Resolve a reference to a path, refusing anything that escapes the vault.

    References are composed by `key_reference()` and never sent by a client, but
    this is the function that turns a database string into a filesystem write, so
    it re-checks rather than trusting its caller.
    """
    root = _vault_dir().resolve()
    candidate = (root / reference).resolve()
    if root not in candidate.parents and candidate != root:
        raise VaultError("key reference escapes the vault directory")
    return candidate


def put(reference: str, secret: str) -> None:
    """Encrypt ``secret`` to ``reference``.

    Written to a temporary file in the *same directory* and renamed: a rename is
    atomic on a POSIX filesystem, so a crash mid-write leaves either the old
    ciphertext or the new one and never a truncated file that fails to decrypt.
    """
    if not secret:
        raise ValueError("refusing to store an empty secret")

    target = _path_for(reference)
    target.parent.mkdir(parents=True, exist_ok=True)

    identity = _identity_path()
    # `-R` reads recipients from a file; we derive the recipient from the identity
    # so there is one key to manage, not a key and a recipient that can disagree.
    recipient = _recipient_for(identity)

    fd, tmp_name = tempfile.mkstemp(dir=target.parent, suffix=".age.tmp")
    try:
        os.close(fd)
        completed = subprocess.run(
            [_require_age(), "-a", "-r", recipient, "-o", tmp_name],
            input=secret.encode(),
            capture_output=True,
            timeout=30,
            check=False,
        )
        if completed.returncode != 0:
            raise VaultError(
                f"age failed to encrypt: {completed.stderr.decode(errors='replace').strip()}"
            )
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, target)
    finally:
        Path(tmp_name).unlink(missing_ok=True)


def get(reference: str) -> str:
    """Decrypt ``reference``.

    The plaintext is returned to the caller and never logged. Callers that only
    need to know whether a key exists should use ``exists()``.
    """
    target = _path_for(reference)
    if not target.is_file():
        raise SecretNotFound(f"no secret stored at {reference}")

    identity = _identity_path()
    completed = subprocess.run(
        [_require_age(), "-d", "-i", str(identity), str(target)],
        capture_output=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        # Not "wrong key": age cannot tell a wrong identity from a tampered file,
        # and guessing in the message would be worse than saying nothing.
        raise VaultError("age could not decrypt this secret")
    return completed.stdout.decode()


def delete(reference: str) -> bool:
    """Remove the secret. Returns whether anything was there.

    The file holds ciphertext, so there is no plaintext to scrub; overwriting
    before unlinking would be theatre on any copy-on-write or SSD-backed volume.
    """
    target = _path_for(reference)
    try:
        target.unlink()
        return True
    except FileNotFoundError:
        return False


def exists(reference: str) -> bool:
    try:
        return _path_for(reference).is_file()
    except VaultError:
        return False


def _recipient_for(identity: Path) -> str:
    """Derive the public recipient from the age identity.

    ``age-keygen -y`` prints the recipient for an identity file. Deriving it keeps
    one source of truth: a separate ``RECIPIENT`` setting is a second value that
    can drift from the identity and silently encrypt to the wrong key.
    """
    completed = subprocess.run(
        [_require_age_keygen(), "-y", str(identity)],
        capture_output=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise VaultError("the age identity file is not a valid age identity")
    return completed.stdout.decode().strip()
