"""Health endpoint: reports modes and storage, never calls the model."""

import pytest
from fastapi.testclient import TestClient

from app.adapters import storage as blob_storage
from app.main import create_app

pytestmark = pytest.mark.usefixtures("_clear_azure_credentials")


@pytest.fixture
def _clear_azure_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in blob_storage.AZURE_CREDENTIAL_ENV:
        monkeypatch.delenv(name, raising=False)


def _client() -> TestClient:
    return TestClient(create_app(start_worker=False))


def test_health_reports_modes() -> None:
    client = _client()
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["data_mode"] == "synthetic"
    assert body["engine_mode"] == "mock_reviewed_ruleset"
    assert body["llm_mode"] == "replay"
    assert body["llm_replay"] is True
    assert body["database"] == "ok"
    assert body["storage"] == "skipped"


def test_health_also_served_under_api_v1() -> None:
    client = _client()
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["storage"] == "skipped"
    assert response.json()["status"] == "ok"


def test_health_skips_storage_without_a_credential_or_model_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class FakeCredential:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            calls.append("credential")

    class FakeBlob:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            calls.append("blob")

    def fake_extract(*args: object, **kwargs: object) -> None:
        del args, kwargs
        calls.append("model")

    monkeypatch.setattr(blob_storage, "DefaultAzureCredential", FakeCredential)
    monkeypatch.setattr(blob_storage, "BlobServiceClient", FakeBlob)
    monkeypatch.setattr("app.adapters.extractor.extract_bounded", fake_extract)

    client = _client()
    for path in ("/health", "/api/v1/health"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
        assert response.json()["storage"] == "skipped"
    assert calls == []


@pytest.mark.parametrize(
    ("env", "configured"),
    [
        ({}, False),
        ({"AZURE_CLIENT_ID": "client"}, False),
        ({"IDENTITY_ENDPOINT": "http://127.0.0.1"}, False),
        (
            {
                "AZURE_CLIENT_ID": "client",
                "AZURE_TENANT_ID": "tenant",
                "AZURE_CLIENT_SECRET": "secret",
            },
            True,
        ),
        (
            {
                "AZURE_CLIENT_ID": "client",
                "AZURE_TENANT_ID": "tenant",
                "AZURE_CLIENT_CERTIFICATE_PATH": "/cert.pem",
            },
            True,
        ),
        (
            {
                "AZURE_CLIENT_ID": "client",
                "AZURE_USERNAME": "user",
                "AZURE_PASSWORD": "password",
            },
            True,
        ),
        (
            {
                "AZURE_CLIENT_ID": "client",
                "AZURE_TENANT_ID": "tenant",
                "AZURE_FEDERATED_TOKEN_FILE": "/token",
            },
            True,
        ),
        (
            {"IDENTITY_ENDPOINT": "http://127.0.0.1", "IDENTITY_HEADER": "header"},
            True,
        ),
        ({"MSI_ENDPOINT": "http://127.0.0.1"}, True),
    ],
)
def test_credentials_configured_reads_environment_only(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], configured: bool
) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert blob_storage.credentials_configured() is configured


def test_configured_storage_probe_reports_ok_or_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AZURE_CLIENT_ID", "client")
    monkeypatch.setenv("AZURE_TENANT_ID", "tenant")
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "secret")
    calls: list[str] = []

    class FakeCredential:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            calls.append("credential")

    class FakeContainer:
        def __init__(self, result: bool | Exception) -> None:
            self._result = result

        def exists(self) -> bool:
            if isinstance(self._result, Exception):
                raise self._result
            return self._result

    class FakeBlob:
        result: bool | Exception = True

        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            calls.append("blob")

        def get_container_client(self, name: str) -> FakeContainer:
            del name
            return FakeContainer(type(self).result)

    def fake_extract(*args: object, **kwargs: object) -> None:
        del args, kwargs
        calls.append("model")

    monkeypatch.setattr(blob_storage, "DefaultAzureCredential", FakeCredential)
    monkeypatch.setattr(blob_storage, "BlobServiceClient", FakeBlob)
    monkeypatch.setattr("app.adapters.extractor.extract_bounded", fake_extract)

    client = _client()
    FakeBlob.result = True
    ok = client.get("/health")
    assert ok.status_code == 200
    assert ok.json()["status"] == "ok"
    assert ok.json()["storage"] == "ok"
    assert calls == ["credential", "blob"]
    assert "model" not in calls

    calls.clear()
    FakeBlob.result = False
    missing = client.get("/api/v1/health")
    assert missing.status_code == 200
    assert missing.json()["storage"] == "unavailable"

    calls.clear()
    FakeBlob.result = RuntimeError("storage down")
    failed = client.get("/health")
    assert failed.status_code == 200
    assert failed.json()["status"] == "ok"
    assert failed.json()["storage"] == "unavailable"
    assert "model" not in calls
