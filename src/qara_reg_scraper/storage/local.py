from __future__ import annotations

import os
import uuid
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
        # The tmp filename must be unique PER WRITER, not just per target
        # path: two separate `qara-reg-scraper` processes (e.g. two
        # sources sharing a host, each writing that host's robots.txt
        # cache entry for the first time) can legitimately race to write
        # the exact same `path` at the exact same instant — confirmed
        # live, 2026-08-30, eu:mdr and eu:ivdr both scheduled in the same
        # SourceRetryScheduler tick, both caching eur-lex.europa.eu's
        # robots.txt concurrently. A shared `.tmp` name meant whichever
        # writer's `replace()` below lost the race got FileNotFoundError
        # renaming a `.tmp` file the winner had already consumed (`.tmp`
        # -> final is destructive to the source). Each writer now gets its
        # own tmp file, so both `replace()` calls succeed independently —
        # whichever runs last simply wins the write, which is fine here:
        # both writers were producing equivalent content anyway.
        tmp = full.with_suffix(f"{full.suffix}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
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
