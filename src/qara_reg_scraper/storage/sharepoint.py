from __future__ import annotations

import os
from collections.abc import Iterator
from urllib.parse import urlparse

from .base import StorageBackend


class SharePointStorage(StorageBackend):
    """Requires the `sharepoint` extra:
    pip install 'qara-reg-scraper[sharepoint]'

    Auth is app-only (client credentials flow) against an Azure AD app
    registration granted Sites.Selected (or Sites.ReadWrite.All) on the
    target site — never a personal SharePoint password. The client secret
    comes from the QARA_REG_SCRAPER_SHAREPOINT_CLIENT_SECRET env var (put it in
    .env, never in config.yaml).

    Note: the office365-rest-python-client API has shifted across major
    versions; this targets >=2.5. If your installed version differs, the
    folder/file helper method names below (`ensure_folder_path`,
    `get_files`) are the first thing to check against your installed
    version's docs.
    """

    def __init__(self, site_url: str, drive_path: str, tenant_id: str | None, client_id: str | None):
        try:
            from office365.runtime.auth.client_credential import ClientCredential
            from office365.sharepoint.client_context import ClientContext
        except ImportError as e:
            raise ImportError(
                "SharePointStorage requires Office365-REST-Python-Client. "
                "Install with: pip install 'qara-reg-scraper[sharepoint]'"
            ) from e

        if not (site_url and client_id):
            raise ValueError("storage.sharepoint.site_url and client_id must be set")
        client_secret = os.environ.get("QARA_REG_SCRAPER_SHAREPOINT_CLIENT_SECRET")
        if not client_secret:
            raise ValueError(
                "Set QARA_REG_SCRAPER_SHAREPOINT_CLIENT_SECRET in .env "
                "(the app registration's client secret)"
            )

        self.site_url = site_url.rstrip("/")
        self.drive_path = drive_path.strip("/")
        self._site_relative_path = urlparse(self.site_url).path.rstrip("/")
        self.ctx = ClientContext(self.site_url).with_credentials(
            ClientCredential(client_id, client_secret)
        )

    def _server_relative_url(self, path: str) -> str:
        clean = path.strip("/")
        return f"{self._site_relative_path}/{self.drive_path}/{clean}"

    def write_bytes(self, path: str, data: bytes, *, content_type: str | None = None) -> None:

        server_url = self._server_relative_url(path)
        folder_url, file_name = server_url.rsplit("/", 1)
        self.ctx.web.ensure_folder_path(folder_url).execute_query()
        target_folder = self.ctx.web.get_folder_by_server_relative_url(folder_url)
        target_folder.upload_file(file_name, data).execute_query()

    def read_bytes(self, path: str) -> bytes:
        from office365.runtime.client_request_exception import ClientRequestException
        from office365.sharepoint.files.file import File

        server_url = self._server_relative_url(path)
        try:
            response = File.open_binary(self.ctx, server_url)
            return response.content
        except ClientRequestException as e:
            if getattr(e, "response", None) is not None and e.response.status_code == 404:
                raise FileNotFoundError(path) from e
            raise

    def exists(self, path: str) -> bool:
        try:
            self.read_bytes(path)
            return True
        except FileNotFoundError:
            return False

    def list(self, prefix: str) -> Iterator[str]:
        folder_url = self._server_relative_url(prefix)
        folder = self.ctx.web.get_folder_by_server_relative_url(folder_url)
        files = folder.get_files(True)  # recursive
        self.ctx.execute_query()
        strip_prefix = f"{self._site_relative_path}/{self.drive_path}/"
        for f in files:
            server_rel = f.serverRelativeUrl
            if server_rel.startswith(strip_prefix):
                yield server_rel[len(strip_prefix) :]

    def describe(self) -> str:
        return f"sharepoint://{self.site_url}/{self.drive_path}"
