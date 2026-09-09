from fastapi.testclient import TestClient

from app import main


def test_readiness_requires_lifespan():
    with TestClient(main.app) as client:
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").json()["status"] == "ok"
    assert client.get("/health/ready").status_code == 503


def test_readiness_detects_database_failure(monkeypatch):
    with TestClient(main.app) as client:
        def fail():
            raise RuntimeError("sensitive database error")
        monkeypatch.setattr(main.db.engine, "connect", fail)
        response = client.get("/health/ready")
        assert response.status_code == 503
        assert "sensitive" not in response.text


def test_production_readiness_requires_running_bot(monkeypatch):
    with TestClient(main.app) as client:
        monkeypatch.setattr(main, "BOT_ENABLED", True)
        assert client.get("/health/ready").status_code == 503
