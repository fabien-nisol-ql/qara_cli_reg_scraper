from __future__ import annotations

import os
from collections.abc import Iterator

from .base import StorageBackend


class AzureBlobStorage(StorageBackend):
    """Requires the `azure` extra: pip install 'qara-reg-scraper[azure]'.

    Auth: set AZURE_STORAGE_CONNECTION_STRING (in .env) for the simplest
    path, or leave it unset and provide `account_url` to use
    DefaultAzureCredential (managed identity / `az login`)."""

    def __init__(self, container: str, prefix: str = "", account_url: str | None = None):
        try:
            from azure.storage.blob import ContainerClient
        except ImportError as e:
            raise ImportError(
                "AzureBlobStorage requires azure-storage-blob. "
                "Install with: pip install 'qara-reg-scraper[azure]'"
            ) from e
        if not container:
            raise ValueError("storage.azure_blob.container must be set")
        self.container = container
        self.prefix = prefix.strip("/")

        conn_str = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
        if conn_str:
            self.client = ContainerClient.from_connection_string(conn_str, container_name=container)
        elif account_url:
            from azure.identity import DefaultAzureCredential

            self.client = ContainerClient(
                account_url=account_url, container_name=container, credential=DefaultAzureCredential()
            )
        else:
            raise ValueError(
                "Set AZURE_STORAGE_CONNECTION_STRING or storage.azure_blob.account_url"
            )

    def _key(self, path: str) -> str:
        return f"{self.prefix}/{path}" if self.prefix else path

    def write_bytes(self, path: str, data: bytes, *, content_type: str | None = None) -> None:
        from azure.storage.blob import ContentSettings

        settings = ContentSettings(content_type=content_type) if content_type else None
        self.client.upload_blob(
            name=self._key(path), data=data, overwrite=True, content_settings=settings
        )

    def read_bytes(self, path: str) -> bytes:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            return self.client.download_blob(self._key(path)).readall()
        except ResourceNotFoundError as e:
            raise FileNotFoundError(path) from e

    def exists(self, path: str) -> bool:
        return self.client.get_blob_client(self._key(path)).exists()

    def list(self, prefix: str) -> Iterator[str]:
        key_prefix = self._key(prefix)
        strip_len = len(self.prefix) + 1 if self.prefix else 0
        for blob in self.client.list_blobs(name_starts_with=key_prefix):
            yield blob.name[strip_len:]

    def describe(self) -> str:
        return f"azure-blob://{self.container}/{self.prefix}"
