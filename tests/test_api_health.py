import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend" / "api"))

from app.main import app  # noqa: E402

client = TestClient(app)


def test_health_returns_ok() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_reports_stub_database() -> None:
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "stub"}
