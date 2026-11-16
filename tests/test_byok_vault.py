"""SEC-03 / SEC-04: the age-backed vault and the write-only BYOK endpoint.

No mocks: every test shells out to the real `age` binary and, where a database row
is involved, the real migrated database. The vault is what stands between a
database dump and a tenant's provider key, so testing a stand-in for it would test
the stand-in.
"""

import os
import shutil
import subprocess
import uuid

import pytest

AGE_AVAILABLE = shutil.which("age") is not None and shutil.which("age-keygen") is not None

pytestmark = pytest.mark.skipif(not AGE_AVAILABLE, reason="age binary not installed")


@pytest.fixture
def vault_env(tmp_path, monkeypatch):
    """A fresh vault directory and age identity for one test."""
    identity = tmp_path / "age-identity"
    subprocess.run(
        ["age-keygen", "-o", str(identity)], capture_output=True, check=True
    )
    monkeypatch.setenv("SECRET_VAULT_DIR", str(tmp_path / "vault"))
    monkeypatch.setenv("SECRET_VAULT_IDENTITY", str(identity))
    return {"dir": tmp_path / "vault", "identity": identity}


def _vault():
    from app.security import vault

    return vault


# --- the vault itself (SEC-03) --------------------------------------------


def test_a_secret_round_trips(vault_env) -> None:
    vault = _vault()
    ref = vault.key_reference(uuid.uuid4(), "deepseek")
    vault.put(ref, "sk-synthetic-abcdef123456")
    assert vault.get(ref) == "sk-synthetic-abcdef123456"


def test_the_ciphertext_on_disk_does_not_contain_the_secret(vault_env) -> None:
    """The whole point: a filesystem dump alone must not yield the key."""
    vault = _vault()
    ref = vault.key_reference(uuid.uuid4(), "deepseek")
    vault.put(ref, "sk-synthetic-abcdef123456")

    on_disk = (vault_env["dir"] / ref).read_text()
    assert "sk-synthetic" not in on_disk
    assert on_disk.startswith("-----BEGIN AGE ENCRYPTED FILE-----")


def test_the_vault_file_is_owner_only(vault_env) -> None:
    vault = _vault()
    ref = vault.key_reference(uuid.uuid4(), "openai")
    vault.put(ref, "sk-synthetic-abcdef123456")
    mode = os.stat(vault_env["dir"] / ref).st_mode & 0o777
    assert mode == 0o600, oct(mode)


def test_rewriting_a_reference_replaces_it_atomically(vault_env) -> None:
    """A crash mid-write must leave old-or-new, never a truncated file."""
    vault = _vault()
    ref = vault.key_reference(uuid.uuid4(), "openai")
    vault.put(ref, "sk-synthetic-first-key")
    vault.put(ref, "sk-synthetic-second-key")
    assert vault.get(ref) == "sk-synthetic-second-key"
    # The temp file from the write must not survive next to the real one.
    leftovers = [p.name for p in (vault_env["dir"] / ref).parent.iterdir()
                 if p.name.endswith(".tmp")]
    assert leftovers == []


def test_a_missing_secret_is_distinguishable_from_a_vault_failure(vault_env) -> None:
    vault = _vault()
    with pytest.raises(vault.SecretNotFound):
        vault.get(vault.key_reference(uuid.uuid4(), "openai"))


def test_an_unknown_provider_is_refused(vault_env) -> None:
    """Provider names become path components, so the set is closed, not sanitised."""
    vault = _vault()
    with pytest.raises(vault.InvalidProvider):
        vault.key_reference(uuid.uuid4(), "../../etc/passwd")


def test_a_reference_escaping_the_vault_directory_is_refused(vault_env) -> None:
    vault = _vault()
    with pytest.raises(vault.VaultError):
        vault.put("../../etc/escape.age", "sk-synthetic-x")


def test_an_empty_secret_is_refused(vault_env) -> None:
    vault = _vault()
    with pytest.raises(ValueError):
        vault.put(vault.key_reference(uuid.uuid4(), "openai"), "")


def test_a_missing_identity_is_a_vault_error_not_a_crash(tmp_path, monkeypatch) -> None:
    """BYOK must fail as 503 with the rest of the product working, not as a 500."""
    vault = _vault()
    monkeypatch.setenv("SECRET_VAULT_DIR", str(tmp_path / "vault"))
    monkeypatch.setenv("SECRET_VAULT_IDENTITY", str(tmp_path / "nope"))
    with pytest.raises(vault.VaultError):
        vault.put(vault.key_reference(uuid.uuid4(), "openai"), "sk-synthetic-x")


def test_a_wrong_identity_cannot_decrypt(vault_env, tmp_path, monkeypatch) -> None:
    """Ciphertext is bound to the identity; a stolen file alone is not enough."""
    vault = _vault()
    ref = vault.key_reference(uuid.uuid4(), "openai")
    vault.put(ref, "sk-synthetic-abcdef123456")

    other = tmp_path / "other-identity"
    subprocess.run(["age-keygen", "-o", str(other)], capture_output=True, check=True)
    monkeypatch.setenv("SECRET_VAULT_IDENTITY", str(other))
    with pytest.raises(vault.VaultError):
        vault.get(ref)


def test_delete_reports_whether_anything_was_there(vault_env) -> None:
    vault = _vault()
    ref = vault.key_reference(uuid.uuid4(), "openai")
    vault.put(ref, "sk-synthetic-abcdef123456")
    assert vault.delete(ref) is True
    assert vault.delete(ref) is False


# --- the write-only endpoint (SEC-04) -------------------------------------


@pytest.fixture(scope="module")
def client(migrated_db: str):
    """Authenticated-capable client bound to the migrated test database."""
    from conftest import APP_PASSWORD, APP_USER, PG_HOST, PG_PORT

    os.environ["APP_DATABASE_URL"] = (
        f"postgresql+psycopg://{APP_USER}:{APP_PASSWORD}@{PG_HOST}:{PG_PORT}/{migrated_db}"
    )
    os.environ["COOKIE_NAME"] = "eva_session"

    import importlib

    # app/db has no __init__.py: it is a namespace package, so importing `db`
    # alone never binds `db.session` and `db.session` is an AttributeError. Import
    # the submodule itself, then reload *that module object*, which is the same
    # object the routers hold a reference to (`from app.db import session`).
    import app.db.session as db_session

    from app import config

    importlib.reload(config)
    importlib.reload(db_session)

    from app.auth import dependencies, service
    importlib.reload(service)
    importlib.reload(dependencies)
    from app.settings import router as settings_router
    importlib.reload(settings_router)
    from app import main as main_module

    importlib.reload(main_module)

    from fastapi.testclient import TestClient

    with TestClient(main_module.app, base_url="https://testserver") as test_client:
        yield test_client


@pytest.fixture
def owner(client, vault_env):
    """A registered owner, logged in. Registration returns the owner role."""
    email = f"byok-{uuid.uuid4().hex[:12]}@example.test"
    response = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "correct-horse-battery", "tenant_name": "Acme"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_setting_a_key_stores_it_encrypted_and_returns_no_secret(
    client, owner, vault_env
) -> None:
    response = client.post(
        "/api/v1/settings/credentials",
        json={"provider": "deepseek", "api_key": "sk-synthetic-abcdef123456"},
    )
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["provider"] == "deepseek"
    assert body["last4"] == "3456"
    # The response must not echo the key under any name.
    assert "sk-synthetic-abcdef123456" not in response.text

    # And it is genuinely in the vault, encrypted.
    from app.security import vault

    reference = vault.key_reference(uuid.UUID(owner["tenant_id"]), "deepseek")
    assert vault.get(reference) == "sk-synthetic-abcdef123456"


def test_no_endpoint_ever_returns_the_key(client, owner, vault_env) -> None:
    """The write-only guarantee, checked against the whole API surface.

    §16.2 says "never returned by any API". Asserting that on the one endpoint we
    remembered would be weak; this walks every credential-shaped response.
    """
    client.post(
        "/api/v1/settings/credentials",
        json={"provider": "openai", "api_key": "sk-synthetic-SECRETVALUE9999"},
    )
    for method, path in [
        ("get", "/api/v1/settings/credentials"),
        ("get", "/api/v1/auth/me"),
    ]:
        response = getattr(client, method)(path)
        assert "SECRETVALUE9999" not in response.text, f"{path} leaked the key"


def test_the_list_endpoint_shows_a_mask_not_the_key(client, owner, vault_env) -> None:
    client.post(
        "/api/v1/settings/credentials",
        json={"provider": "ollama_cloud", "api_key": "sk-synthetic-abcdef9999"},
    )
    body = client.get("/api/v1/settings/credentials").json()
    entry = next(e for e in body if e["provider"] == "ollama_cloud")
    assert entry["last4"] == "9999"
    assert "sk-synthetic" not in str(body)


def test_an_unknown_provider_is_a_400(client, owner, vault_env) -> None:
    response = client.post(
        "/api/v1/settings/credentials",
        json={"provider": "../../etc/passwd", "api_key": "sk-synthetic-abcdef1234"},
    )
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["error"]["code"] == "unknown_provider"


def test_setting_the_same_provider_twice_replaces_it(client, owner, vault_env) -> None:
    """Rotation must not leave two rows, and the new key must win."""
    for key in ("sk-synthetic-first1111", "sk-synthetic-second2222"):
        response = client.post(
            "/api/v1/settings/credentials",
            json={"provider": "deepseek", "api_key": key},
        )
        assert response.status_code == 201, response.text

    from app.security import vault

    reference = vault.key_reference(uuid.UUID(owner["tenant_id"]), "deepseek")
    assert vault.get(reference) == "sk-synthetic-second2222"

    listing = client.get("/api/v1/settings/credentials").json()
    assert len([e for e in listing if e["provider"] == "deepseek"]) == 1


def test_deleting_removes_both_the_row_and_the_ciphertext(client, owner, vault_env) -> None:
    client.post(
        "/api/v1/settings/credentials",
        json={"provider": "openai", "api_key": "sk-synthetic-abcdef1234"},
    )
    from app.security import vault

    reference = vault.key_reference(uuid.UUID(owner["tenant_id"]), "openai")
    assert vault.exists(reference)

    response = client.delete("/api/v1/settings/credentials/openai")
    assert response.status_code == 204, response.text
    assert not vault.exists(reference)
    assert client.get("/api/v1/settings/credentials").json() == []


def test_deleting_an_absent_credential_is_a_404(client, owner, vault_env) -> None:
    assert client.delete("/api/v1/settings/credentials/openai").status_code == 404


def test_a_vault_failure_is_503_and_writes_no_row(client, owner, monkeypatch) -> None:
    """A missing identity must not leave a reference to a key that was never written."""
    monkeypatch.setenv("SECRET_VAULT_IDENTITY", "/nonexistent/identity")
    response = client.post(
        "/api/v1/settings/credentials",
        json={"provider": "openai", "api_key": "sk-synthetic-abcdef1234"},
    )
    assert response.status_code == 503, response.text
    assert response.json()["detail"]["error"]["code"] == "vault_unavailable"
    # No row either: a reference to a key that was never written is worse than no
    # row, because the dashboard would show a configured provider that cannot work.
    assert client.get("/api/v1/settings/credentials").json() == []


def test_the_key_never_reaches_the_audit_trail(client, owner, vault_env) -> None:
    """The audit trail is append-only, so a key written there can never be rotated away."""
    client.post(
        "/api/v1/settings/credentials",
        json={"provider": "openai", "api_key": "sk-synthetic-AUDITTEST8888"},
    )
    from sqlalchemy import text

    from app.db.session import tenant_session

    with tenant_session(uuid.UUID(owner["tenant_id"])) as session:
        rows = session.execute(
            text("SELECT detail FROM audit_logs WHERE tenant_id = :t"),
            {"t": owner["tenant_id"]},
        ).all()
    assert rows
    assert "AUDITTEST8888" not in str(rows)
