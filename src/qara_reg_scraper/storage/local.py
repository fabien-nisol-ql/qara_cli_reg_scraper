from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from .base import StorageBackend


class LocalStorage(StorageBackend):
    def __init__(self, root: str):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _full_path(self, path: str) -> Path:
        full = (self.root / path).resolve()
        if self.root not in full.parents and full != self.root:
            raise ValueError(f"path escapes storage root: {path}")
        return full

    def write_bytes(self, path: str, data: bytes, *, content_type: str | None = None) -> None:
        full = self._full_path(path)
        full.parent.mkdir(parents=True, exist_ok=True)
        tmp = full.with_suffix(full.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(full)  # atomic on same filesystem — no partial writes on crash

    def read_bytes(self, path: str) -> bytes:
        full = self._full_path(path)
        if not full.is_file():
            raise FileNotFoundError(path)
        return full.read_bytes()

    def exists(self, path: str) -> bool:
        return self._full_path(path).is_file()

    def list(self, prefix: str) -> Iterator[str]:
        base = self._full_path(prefix)
        if not base.exists():
            return
        if base.is_file():
            yield str(base.relative_to(self.root)).replace("\\", "/")
            return
        for p in base.rglob("*"):
            if p.is_file():
                yield str(p.relative_to(self.root)).replace("\\", "/")

    def describe(self) -> str:
        return f"local:{self.root}"

    def local_root(self) -> Path | None:
        return self.root
