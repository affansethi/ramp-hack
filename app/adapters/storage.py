"""Azure Blob Storage adapter, ported from the original data.py (decision D2).

Auth is DefaultAzureCredential only (no keys): locally it uses your
`az login`, in Azure the app's managed identity. Account and container
match Technical Spec v2 §9. When storage is not configured every public
function raises StorageUnavailable and no credential is ever constructed.

Health asks ``credentials_configured`` before constructing a credential.
When that is false, nothing here builds DefaultAzureCredential.
"""

import hashlib
import os
from collections.abc import Mapping
from functools import lru_cache
from typing import NamedTuple

from azure.core.exceptions import ResourceExistsError
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient, ContainerClient

DEFAULT_ACCOUNT_URL = "https://ramphackaffanandrj26.blob.core.windows.net"
EXPORT_EXTENSIONS = ("csv", "json")

# Environment names that can configure a credential. Health clears these in tests.
AZURE_CREDENTIAL_ENV: tuple[str, ...] = (
    "AZURE_CLIENT_ID",
    "AZURE_CLIENT_SECRET",
    "AZURE_TENANT_ID",
    "AZURE_CLIENT_CERTIFICATE_PATH",
    "AZURE_USERNAME",
    "AZURE_PASSWORD",
    "AZURE_FEDERATED_TOKEN_FILE",
    "IDENTITY_ENDPOINT",
    "IDENTITY_HEADER",
    "IMDS_ENDPOINT",
    "MSI_ENDPOINT",
    "MSI_SECRET",
)


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


class _Sources(NamedTuple):
    environment: bool
    workload: bool
    managed: bool


def _filled(env: Mapping[str, str], *names: str) -> bool:
    return all(bool(env.get(name)) for name in names)


def _sources(env: Mapping[str, str]) -> _Sources:
    environment = (
        _filled(env, "AZURE_CLIENT_ID", "AZURE_TENANT_ID", "AZURE_CLIENT_SECRET")
        or _filled(
            env, "AZURE_CLIENT_ID", "AZURE_TENANT_ID", "AZURE_CLIENT_CERTIFICATE_PATH"
        )
        or _filled(env, "AZURE_CLIENT_ID", "AZURE_USERNAME", "AZURE_PASSWORD")
    )
    workload = _filled(
        env, "AZURE_CLIENT_ID", "AZURE_TENANT_ID", "AZURE_FEDERATED_TOKEN_FILE"
    )
    managed = bool(env.get("MSI_ENDPOINT")) or (
        bool(env.get("IDENTITY_ENDPOINT"))
        and bool(env.get("IDENTITY_HEADER") or env.get("IMDS_ENDPOINT"))
    )
    return _Sources(environment=environment, workload=workload, managed=managed)


def credentials_configured() -> bool:
    """True when environment variables configure an Azure credential.

    Reads the process environment only. Does not construct
    DefaultAzureCredential, start the Azure CLI, contact IMDS, or call a model.
    """
    sources = _sources(os.environ)
    return sources.environment or sources.workload or sources.managed


def _health_credential() -> DefaultAzureCredential:
    sources = _sources(os.environ)
    return DefaultAzureCredential(
        exclude_environment_credential=not sources.environment,
        exclude_workload_identity_credential=not sources.workload,
        exclude_managed_identity_credential=not sources.managed,
        exclude_shared_token_cache_credential=True,
        exclude_visual_studio_code_credential=True,
        exclude_cli_credential=True,
        exclude_powershell_credential=True,
        exclude_developer_cli_credential=True,
        exclude_interactive_browser_credential=True,
        exclude_broker_credential=True,
    )


def probe_container() -> bool:
    """True when the configured container exists.

    Caller must check ``credentials_configured`` first. This constructs
    DefaultAzureCredential and contacts storage.
    """
    service = BlobServiceClient(
        _account_url() or DEFAULT_ACCOUNT_URL,
        credential=_health_credential(),
        connection_timeout=5,
        read_timeout=5,
        retry_total=1,
    )
    return bool(service.get_container_client(_container_name()).exists())


@lru_cache(maxsize=4)
def _build_container(account_url: str, container_name: str) -> ContainerClient:
    client = BlobServiceClient(account_url, credential=DefaultAzureCredential())
    return client.get_container_client(container_name)


def _container() -> ContainerClient:
    url = _account_url()
    if url is None:
        raise StorageUnavailable("Blob storage is not configured")
    return _build_container(url, _container_name())


@lru_cache(maxsize=4)
def _build_probe_container(account_url: str, container_name: str) -> ContainerClient:
    # No retries and short timeouts so an unreachable account cannot stall /health.
    client = BlobServiceClient(
        account_url,
        credential=DefaultAzureCredential(),
        retry_total=0,
        connection_timeout=2,
        read_timeout=3,
    )
    return client.get_container_client(container_name)


def _probe_container() -> ContainerClient:
    url = _account_url()
    if url is None:
        raise StorageUnavailable("Blob storage is not configured")
    return _build_probe_container(url, _container_name())


def check_container() -> bool:
    """Cheap, bounded reachability check; False when the call fails."""
    container = _probe_container()
    try:
        container.get_container_properties(timeout=3)
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
