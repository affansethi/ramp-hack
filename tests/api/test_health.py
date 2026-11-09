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


def test_health_storage_skipped_without_credential_call(monkeypatch) -> None:
    from app.adapters import storage

    for var in (
        "AZURE_STORAGE_ACCOUNT_URL",
        "STORAGE_ACCOUNT_URL",
        "WEBSITE_SITE_NAME",
    ):
        monkeypatch.delenv(var, raising=False)

    def boom(*a, **k):
        raise AssertionError("must not be called")

    monkeypatch.setattr(storage, "DefaultAzureCredential", boom)
    monkeypatch.setattr(storage, "BlobServiceClient", boom)
    response = TestClient(create_app()).get("/health")
    assert response.status_code == 200
    assert response.json()["storage"] == "skipped"


def test_health_storage_unavailable_when_check_raises(monkeypatch) -> None:
    from app.adapters import storage

    class Broken:
        def get_container_properties(self):
            raise RuntimeError("down")

    monkeypatch.setenv("AZURE_STORAGE_ACCOUNT_URL", "https://example.invalid")
    monkeypatch.setattr(storage, "_container", lambda: Broken())
    response = TestClient(create_app()).get("/health")
    assert response.status_code == 200
    assert response.json()["storage"] == "unavailable"
