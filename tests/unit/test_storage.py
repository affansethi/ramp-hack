import hashlib

import pytest
from azure.core.exceptions import ResourceExistsError

from app.adapters import storage

ENV = ("AZURE_STORAGE_ACCOUNT_URL", "STORAGE_ACCOUNT_URL", "WEBSITE_SITE_NAME")


class FakeBlob:
    def __init__(self, data):
        self.data = data

    def readall(self):
        return self.data


class FakeContainer:
    def __init__(self):
        self.blobs: dict[str, bytes] = {}
        self.uploads: list[tuple[str, bool]] = []

    def upload_blob(self, name, data, overwrite=False, **kwargs):
        self.uploads.append((name, overwrite))
        if not overwrite and name in self.blobs:
            raise ResourceExistsError("exists")
        self.blobs[name] = data

    def download_blob(self, name):
        return FakeBlob(self.blobs[name])

    def list_blobs(self, name_starts_with=None):
        return []


def _boom(*a, **k):
    raise AssertionError("must not be called")


@pytest.fixture
def disabled(monkeypatch):
    for v in ENV:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(storage, "DefaultAzureCredential", _boom)
    monkeypatch.setattr(storage, "BlobServiceClient", _boom)


@pytest.fixture
def fake(monkeypatch):
    for v in ENV:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("AZURE_STORAGE_ACCOUNT_URL", "https://example.invalid")
    c = FakeContainer()
    monkeypatch.setattr(storage, "_container", lambda: c)
    return c


def test_disabled_every_function_raises(disabled) -> None:
    assert not storage.storage_enabled()
    calls = [
        lambda: storage.list_files(),
        lambda: storage.upload("a", b"x"),
        lambda: storage.download("a"),
        lambda: storage.get("a"),
        lambda: storage.check_container(),
        lambda: storage.put_document(b"x", "text/plain"),
        lambda: storage.put_export("w", 1, "f", b"x", "csv"),
    ]
    for call in calls:
        with pytest.raises(storage.StorageUnavailable):
            call()


def test_put_document_is_content_addressed_and_written_once(fake) -> None:
    data = b"invoice"
    a = storage.put_document(data, "application/pdf")
    b = storage.put_document(data, "application/pdf")
    digest = hashlib.sha256(data).hexdigest()
    assert a == b
    assert a.key == f"documents/{digest}" and a.sha256 == digest and a.size == len(data)
    assert storage.get(a.key) == data
    assert all(not ow for _, ow in fake.uploads)
    assert len(fake.blobs) == 1


def test_put_export_same_bytes_noop_different_bytes_raises(fake) -> None:
    key = storage.put_export("wp1", 2, "abc", b"a,b", "csv")
    assert key == "exports/wp1/v2-abc.csv"
    assert storage.put_export("wp1", 2, "abc", b"a,b", "csv") == key
    with pytest.raises(storage.FrozenExportConflict):
        storage.put_export("wp1", 2, "abc", b"other", "csv")
    assert fake.blobs[key] == b"a,b"
    assert all(not ow for _, ow in fake.uploads)


def test_app_service_enables_with_default_url(monkeypatch) -> None:
    for v in ENV:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("WEBSITE_SITE_NAME", "site")
    seen = {}

    class Client:
        def __init__(self, url, credential=None):
            seen["url"] = url

        def get_container_client(self, name):
            return FakeContainer()

    monkeypatch.setattr(storage, "DefaultAzureCredential", lambda: object())
    monkeypatch.setattr(storage, "BlobServiceClient", Client)
    storage._build_container.cache_clear()
    assert storage.storage_enabled()
    storage.list_files()
    assert seen["url"] == storage.DEFAULT_ACCOUNT_URL
    storage._build_container.cache_clear()


def test_probe_client_has_no_retries_and_short_timeouts(monkeypatch) -> None:
    for v in ENV:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("AZURE_STORAGE_ACCOUNT_URL", "https://probe.invalid")
    seen = {}

    class Client:
        def __init__(self, url, **kw):
            seen.update(kw)

        def get_container_client(self, name):
            return object()

    monkeypatch.setattr(storage, "DefaultAzureCredential", lambda: "cred")
    monkeypatch.setattr(storage, "BlobServiceClient", Client)
    storage._build_probe_container.cache_clear()
    try:
        storage._probe_container()
    finally:
        storage._build_probe_container.cache_clear()
    assert seen["retry_total"] == 0
    assert seen["connection_timeout"] <= 5 and seen["read_timeout"] <= 5
