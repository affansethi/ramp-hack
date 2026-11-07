from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient

ACCOUNT_URL = "https://ramphackaffanandrj26.blob.core.windows.net"
client = BlobServiceClient(ACCOUNT_URL, credential=DefaultAzureCredential())
container = client.get_container_client("data")

container.upload_blob("hello.txt", b"it works", overwrite=True)
for blob in container.list_blobs():
    print(blob.name)

