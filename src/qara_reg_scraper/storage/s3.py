from __future__ import annotations

from collections.abc import Iterator

from .base import StorageBackend


class S3Storage(StorageBackend):
    """Requires the `s3` extra: pip install 'qara-reg-scraper[s3]'.
    Credentials come from the standard boto3 chain (env vars, ~/.aws,
    instance/role profile) — never hardcode keys in config.yaml."""

    def __init__(
        self,
        bucket: str,
        prefix: str = "",
        region: str | None = None,
        endpoint_url: str | None = None,
    ):
        try:
            import boto3
        except ImportError as e:
            raise ImportError(
                "S3Storage requires boto3. Install with: pip install 'qara-reg-scraper[s3]'"
            ) from e
        if not bucket:
            raise ValueError("storage.s3.bucket must be set")
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.client = boto3.client("s3", region_name=region, endpoint_url=endpoint_url)

    def _key(self, path: str) -> str:
        return f"{self.prefix}/{path}" if self.prefix else path

    def write_bytes(self, path: str, data: bytes, *, content_type: str | None = None) -> None:
        kwargs = {}
        if content_type:
            kwargs["ContentType"] = content_type
        self.client.put_object(Bucket=self.bucket, Key=self._key(path), Body=data, **kwargs)

    def read_bytes(self, path: str) -> bytes:
        from botocore.exceptions import ClientError

        try:
            obj = self.client.get_object(Bucket=self.bucket, Key=self._key(path))
            return obj["Body"].read()
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                raise FileNotFoundError(path) from e
            raise

    def exists(self, path: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.client.head_object(Bucket=self.bucket, Key=self._key(path))
            return True
        except ClientError:
            return False

    def list(self, prefix: str) -> Iterator[str]:
        key_prefix = self._key(prefix)
        paginator = self.client.get_paginator("list_objects_v2")
        strip_len = len(self.prefix) + 1 if self.prefix else 0
        for page in paginator.paginate(Bucket=self.bucket, Prefix=key_prefix):
            for obj in page.get("Contents", []):
                yield obj["Key"][strip_len:]

    def describe(self) -> str:
        return f"s3://{self.bucket}/{self.prefix}"
