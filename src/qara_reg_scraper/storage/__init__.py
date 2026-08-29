from __future__ import annotations

from ..config import StorageSettings
from .base import StorageBackend


def build_storage_backend(settings: StorageSettings) -> StorageBackend:
    if settings.backend == "local":
        from .local import LocalStorage

        return LocalStorage(root=settings.local.root)
    if settings.backend == "s3":
        from .s3 import S3Storage

        return S3Storage(
            bucket=settings.s3.bucket,
            prefix=settings.s3.prefix,
            region=settings.s3.region,
            endpoint_url=settings.s3.endpoint_url,
        )
    if settings.backend == "azure_blob":
        from .azure_blob import AzureBlobStorage

        return AzureBlobStorage(
            container=settings.azure_blob.container,
            prefix=settings.azure_blob.prefix,
            account_url=settings.azure_blob.account_url,
        )
    if settings.backend == "sharepoint":
        from .sharepoint import SharePointStorage

        return SharePointStorage(
            site_url=settings.sharepoint.site_url,
            drive_path=settings.sharepoint.drive_path,
            tenant_id=settings.sharepoint.tenant_id,
            client_id=settings.sharepoint.client_id,
        )
    raise ValueError(f"Unknown storage backend: {settings.backend}")


__all__ = ["StorageBackend", "build_storage_backend"]
