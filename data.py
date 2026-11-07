"""Azure Blob Storage helpers. Auth is DefaultAzureCredential only (no keys)."""
import os
from functools import lru_cache

from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient

ACCOUNT_URL = os.environ.get(
    "STORAGE_ACCOUNT_URL", "https://ramphackaffanandrj26.blob.core.windows.net"
)
CONTAINER_NAME = os.environ.get("STORAGE_CONTAINER", "data")


@lru_cache(maxsize=1)
def _container():
    client = BlobServiceClient(ACCOUNT_URL, credential=DefaultAzureCredential())
    return client.get_container_client(CONTAINER_NAME)


def list_files(prefix=None):
    """Return a list of blob names, optionally filtered by prefix."""
    return [b.name for b in _container().list_blobs(name_starts_with=prefix)]


def upload(name, data, overwrite=True):
    """Upload bytes or str to a blob."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    _container().upload_blob(name, data, overwrite=overwrite)


def download(name):
    """Return the blob's contents as bytes."""
    return _container().download_blob(name).readall()
