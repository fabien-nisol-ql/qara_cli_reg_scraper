from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import unquote, urlparse

from .logging_setup import get_logger, log_extra
from .storage.base import StorageBackend

log = get_logger("util")

_SLUG_UNSAFE = re.compile(r"[^a-z0-9]+")


def slugify(text: str, max_len: int = 120) -> str:
    """Turn arbitrary text (a document title, a URL) into a filesystem- and
    URL-safe document_id fragment."""
    slug = _SLUG_UNSAFE.sub("-", text.lower()).strip("-")
    return slug[:max_len] or "untitled"


# RFC 6266 filename*=<charset>'<lang>'<url-encoded-value> — e.g.
# filename*=UTF-8''Summary%20K252474.pdf. Takes priority over plain
# filename= when both are present, per the RFC.
_CD_FILENAME_STAR = re.compile(r"filename\*\s*=\s*([^']*)'[^']*'([^;]+)", re.IGNORECASE)
# Plain filename=value or filename="value", value has no ';' when unquoted.
_CD_FILENAME = re.compile(r'filename\s*=\s*"([^"]*)"|filename\s*=\s*([^;]+)', re.IGNORECASE)


def _filename_from_content_disposition(header_value: str) -> str | None:
    """Best-effort parse of a Content-Disposition header's filename,
    preferring the RFC 6266 extended form (filename*=) over the plain one
    when both are present."""
    star = _CD_FILENAME_STAR.search(header_value)
    if star:
        charset = (star.group(1) or "utf-8").strip() or "utf-8"
        raw = star.group(2).strip().strip('"')
        try:
            return unquote(raw, encoding=charset, errors="strict") or None
        except (LookupError, UnicodeDecodeError):
            return unquote(raw) or None

    plain = _CD_FILENAME.search(header_value)
    if plain:
        name = (plain.group(1) or plain.group(2) or "").strip().strip('"')
        return name or None

    return None


def _filename_from_url(url: str) -> str | None:
    """Fallback: the last path segment of the URL, if it looks like an
    actual filename (has a dot) rather than an opaque API path."""
    path = urlparse(url).path
    last_segment = unquote(path.rsplit("/", 1)[-1]) if path else ""
    if last_segment and "." in last_segment:
        return last_segment
    return None


def derive_original_filename(url: str, headers: Mapping[str, str]) -> str | None:
    """What the source called this document — as distinct from
    `current.<ext>`, the internal "latest version" storage convention (see
    Manifest._current_path). Prefers the server's own Content-Disposition
    filename; falls back to the URL's last path segment when the server
    doesn't send one (common for plain static file URLs). Returns None when
    neither source yields anything usable — e.g. an API endpoint URL with no
    Content-Disposition, where there simply isn't an original filename."""
    content_disposition = headers.get("Content-Disposition")
    if content_disposition:
        name = _filename_from_content_disposition(content_disposition)
        if name:
            return name
    return _filename_from_url(url)


def read_json_lenient(storage: StorageBackend, path: str) -> dict[str, Any] | None:
    """Read+parse a JSON file, returning None (and logging) instead of
    raising on a corrupt or unreadable file. A single bad manifest file
    should never abort a whole reindex run."""
    try:
        return json.loads(storage.read_bytes(path))
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        log_extra(log, 30, "corrupt_manifest_file", path=path, error=str(e))
        return None
