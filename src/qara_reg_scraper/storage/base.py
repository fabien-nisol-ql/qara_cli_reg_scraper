"""Storage backend abstraction.

Every scraper writes through one of these, chosen by ``storage.backend`` in
config — local disk, S3, Azure Blob, or SharePoint — so "store the data
anywhere" is a config change, not a code change. All paths passed to a
backend are POSIX-style relative paths (e.g. ``ecfr/part-800/800.10.json``);
each backend maps that onto its own notion of "root" (a local directory, an
S3 bucket+prefix, a container+prefix, or a SharePoint document library
folder).

Design note: backends expose only whole-object write/read/list/exists —
deliberately no append. S3, Azure Blob, and SharePoint have no atomic append
primitive, so the manifest layer (see ``qara_reg_scraper.manifest``) writes one
small file per event instead of appending to a growing log. That keeps every
backend implementation simple and makes concurrent writers (e.g. two source
scrapers running at once, writing to disjoint subtrees) safe by construction.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from pathlib import Path


class StorageBackend(ABC):
    @abstractmethod
    def write_bytes(self, path: str, data: bytes, *, content_type: str | None = None) -> None:
        """Write (or overwrite) an object at `path`."""

    @abstractmethod
    def read_bytes(self, path: str) -> bytes:
        """Read an object. Raises FileNotFoundError if it doesn't exist."""

    @abstractmethod
    def exists(self, path: str) -> bool: ...

    @abstractmethod
    def list(self, prefix: str) -> Iterator[str]:
        """Yield relative paths of every object under `prefix`, recursively."""

    def write_text(self, path: str, text: str, *, encoding: str = "utf-8") -> None:
        self.write_bytes(path, text.encode(encoding), content_type="application/json")

    def read_text(self, path: str, *, encoding: str = "utf-8") -> str:
        return self.read_bytes(path).decode(encoding)

    def describe(self) -> str:
        """Human-readable identifier for logs (e.g. 'local:/data')."""
        return self.__class__.__name__

    def local_root(self) -> Path | None:
        """A real, on-disk filesystem path backing this storage, if one
        exists — None for every backend by default (S3/Azure Blob/
        SharePoint have no such thing; a network object store isn't a
        filesystem). Only `LocalStorage` overrides this.

        Exists for exactly one purpose: `filelock.FileLock` (used by
        `origin_pacing.py` for cross-process request pacing, and by
        cli.py's own per-source run lock) needs a real POSIX path to lock
        against — it can't lock "an S3 key". Everything that wants
        cross-process coordination should go through this rather than
        assuming `storage` is local, so it degrades gracefully (falls
        back to in-process-only behavior, never raises) for every other
        backend."""
        return None
