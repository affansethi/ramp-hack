"""Azure Blob Storage adapter, ported from the original data.py (decision D2).

Auth is DefaultAzureCredential only (no keys): locally it uses your
`az login`, in Azure the app's managed identity. Account and container
match Technical Spec v2 §9. When storage is not configured every public
function raises StorageUnavailable and no credential is ever constructed.
"""

import hashlib
import os
from functools import lru_cache
from typing import NamedTuple

from azure.core.exceptions import ResourceExistsError
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient, ContainerClient

DEFAULT_ACCOUNT_URL = "https://ramphackaffanandrj26.blob.core.windows.net"
EXPORT_EXTENSIONS = ("csv", "json")


class StorageUnavailable(RuntimeError):
    """Blob storage is not configured for this environment."""


class FrozenExportConflict(ValueError):
    """Different bytes were offered for an export key that already exists."""


class StoredDocument(NamedTuple):
    key: str
    sha256: str
    size: int


def _account_url() -> str | None:
    url = os.environ.get("AZURE_STORAGE_ACCOUNT_URL") or os.environ.get(
        "STORAGE_ACCOUNT_URL"
    )
    if url:
        return url
    if os.environ.get("WEBSITE_SITE_NAME"):
        return DEFAULT_ACCOUNT_URL
    return None


def _container_name() -> str:
    return os.environ.get("STORAGE_CONTAINER") or "data"


def storage_enabled() -> bool:
    return _account_url() is not None


@lru_cache(maxsize=4)
def _build_container(account_url: str, container_name: str) -> ContainerClient:
    client = BlobServiceClient(account_url, credential=DefaultAzureCredential())
    return client.get_container_client(container_name)


def _container() -> ContainerClient:
    url = _account_url()
    if url is None:
        raise StorageUnavailable("Blob storage is not configured")
    return _build_container(url, _container_name())


def check_container() -> bool:
    """Cheap reachability check; False when disabled or the call fails."""
    if not storage_enabled():
        return False
    try:
        _container().get_container_properties()
    except Exception:
        return False
    return True


def list_files(prefix: str | None = None) -> list[str]:
    """Return a list of blob names, optionally filtered by prefix."""
    return [b.name for b in _container().list_blobs(name_starts_with=prefix)]


def upload(name: str, data: bytes | str, overwrite: bool = True) -> None:
    """Upload bytes or str to a blob."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    _container().upload_blob(name, data, overwrite=overwrite)


def download(name: str) -> bytes:
    """Return the blob's contents as bytes."""
    return bytes(_container().download_blob(name).readall())


def get(key: str) -> bytes:
    return download(key)


def put_document(data: bytes, mime_type: str) -> StoredDocument:
    """Store an original document under its content hash; never rewrites."""
    container = _container()
    digest = hashlib.sha256(data).hexdigest()
    key = f"documents/{digest}"
    try:
        from azure.storage.blob import ContentSettings

        container.upload_blob(
            key,
            data,
            overwrite=False,
            content_settings=ContentSettings(content_type=mime_type),
        )
    except ResourceExistsError:
        pass
    return StoredDocument(key, digest, len(data))


def put_export(
    workpaper_id: str, version: int, fingerprint: str, data: bytes, extension: str
) -> str:
    """Store a frozen export. Same bytes again is a no-op; other bytes raise."""
    if extension not in EXPORT_EXTENSIONS:
        raise ValueError(f"extension must be one of {EXPORT_EXTENSIONS}")
    container = _container()
    key = f"exports/{workpaper_id}/v{version}-{fingerprint}.{extension}"
    try:
        container.upload_blob(key, data, overwrite=False)
    except ResourceExistsError:
        if bytes(container.download_blob(key).readall()) != data:
            raise FrozenExportConflict(
                f"frozen export {key} already exists with other bytes"
            ) from None
    return key
