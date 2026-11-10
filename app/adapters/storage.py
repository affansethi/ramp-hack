"""Azure Blob Storage adapter, ported from the original data.py (decision D2).

Auth is DefaultAzureCredential only (no keys): locally it uses your
`az login`, in Azure the app's managed identity. Account and container
match Technical Spec v2 §9.

Health asks ``credentials_configured`` before constructing a credential.
When that is false, nothing here builds DefaultAzureCredential.
"""

import os
from collections.abc import Mapping
from functools import lru_cache
from typing import NamedTuple

from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient, ContainerClient

ACCOUNT_URL = os.environ.get(
    "STORAGE_ACCOUNT_URL", "https://ramphackaffanandrj26.blob.core.windows.net"
)
CONTAINER_NAME = os.environ.get("STORAGE_CONTAINER", "data")

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
        ACCOUNT_URL,
        credential=_health_credential(),
        connection_timeout=5,
        read_timeout=5,
        retry_total=1,
    )
    return bool(service.get_container_client(CONTAINER_NAME).exists())


@lru_cache(maxsize=1)
def _container() -> ContainerClient:
    client = BlobServiceClient(ACCOUNT_URL, credential=DefaultAzureCredential())
    return client.get_container_client(CONTAINER_NAME)


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
    return _container().download_blob(name).readall()
