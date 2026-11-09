"""Health endpoint: reports modes, never calls the model (architecture §4)."""

from fastapi.testclient import TestClient

from app.main import create_app


def test_health_reports_modes() -> None:
    client = TestClient(create_app())
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["data_mode"] == "synthetic"
    assert body["engine_mode"] == "mock_reviewed_ruleset"
    assert body["llm_mode"] == "replay"
    assert body["llm_replay"] is True


def test_health_also_served_under_api_v1() -> None:
    client = TestClient(create_app())
    assert client.get("/api/v1/health").status_code == 200
