"""Structured logging setup.

Three separate output channels, on purpose:

1. **The internal structured (JSON) log** — every HTTP request, retry,
   bot-detection event, etc. `configure_logging()` below. Off by default
   (no sink at all): a plain `run` used to dump this straight to stdout,
   which read as noisy machine-oriented output rather than "what's it
   doing" — see `--log <path>` / config.yaml's `log_file` to turn it back
   on, e.g. pointed at a real file or a device path like "/dev/stderr".
2. **Human-oriented progress** — "what's it doing right now", printed via
   Rich in cli.py (the `[N/budget] new K12345` lines, the per-source
   header/summary), on by default and controllable with `run --quiet`.
   Entirely separate from this module; nothing about disabling the JSON
   sink here silences that, and vice versa.
3. **The per-session log directory** — `configure_session_log()` below.
   Same JSON content as (1), but always-on (once `monitoring.log` is
   configured — see config.py's `MonitoringLogSettings`) and durable: one
   uniquely-named file per CLI invocation in a shared directory, with
   automatic retention, rather than one human opting into one fixed sink
   for one run. A temporary stand-in for real metrics (Prometheus/Grafana)
   — see qara_cli_reg_scraper's README for the full story.
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        extra = getattr(record, "extra_fields", None)
        if extra:
            payload.update(extra)
        return json.dumps(payload, default=str)


def ensure_unbuffered_stdout() -> None:
    """Line-buffer stdout regardless of what it's connected to. Python only
    auto-line-buffers a real TTY; piped through `make`, a container
    runtime, or anything else that isn't a TTY, stdout is fully
    block-buffered by default — the human-oriented progress output (Rich,
    in cli.py) could otherwise sit invisible in the buffer for a long time
    during a slow, rate-limited run instead of appearing as it happens.
    Called once at CLI startup (see cli.py's module level), independent of
    whether the JSON log sink below is even enabled."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)


def configure_logging(level: str = "INFO", sink: str | None = None) -> None:
    """Wires up (or explicitly silences) the internal structured logger.

    `sink=None` (the default) attaches a `NullHandler` — no output at all,
    not even to stderr, and no "no handlers found" warning either. Pass a
    real file path or a device path like "/dev/stderr" to turn it on; it's
    opened in line-buffered append mode so multiple runs against the same
    path (or a device file) don't clobber each other and lines show up
    promptly rather than sitting in a buffer.

    Always reconfigures from scratch (clears any handler from a previous
    call) rather than the old "idempotent, skip if already configured"
    behavior — each CLI invocation should reflect *its own* --log/level,
    and process-lifetime callers (the test suite, invoking this many times
    against one shared logger object) need that to actually take effect
    each time, not just once."""
    root = logging.getLogger("qara_reg_scraper")
    root.setLevel(level.upper())
    for existing in list(root.handlers):
        root.removeHandler(existing)
        # Don't close a stream we don't own (e.g. a previous call's file)
        # while it might still be referenced elsewhere; explicit close of
        # this handler's own stream is enough and StreamHandler does that
        # on close().
        existing.close()

    if sink is None:
        root.addHandler(logging.NullHandler())
        return

    stream = open(sink, "a", buffering=1)  # noqa: SIM115 - lifetime is this process's, closed via the handler above on reconfigure
    handler = logging.StreamHandler(stream=stream)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)


_SESSION_LOG_SUFFIX = ".jsonl"


def configure_session_log(log_dir: Path, command: str, retention_days: int) -> Path | None:
    """Adds ONE MORE handler to the same `qara_reg_scraper` logger
    `configure_logging()` already set up — call this AFTER that, never
    before, or its own from-scratch handler reset above would silently
    wipe this one out. Writes this session's own uniquely-named JSON-lines
    file into `log_dir` (created if missing), then sweeps `log_dir` for
    any file older than `retention_days` — every call, not a separate
    cron/service, so retention self-enforces just by this being used at
    all.

    Returns the path written to, or None if session logging couldn't be
    set up (a read-only/missing-parent `log_dir`, for example) — never
    raises. A run whose only problem is its own activity logging must
    still complete; see this module's own top-level docstring for why
    this exists in the first place, and config.py's `MonitoringLogSettings`
    for how a caller opts in."""
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        _sweep_old_session_logs(log_dir, retention_days)

        # {command}-{timestamp}-{8 hex chars}.jsonl - sortable by name, and
        # the timestamp alone is human-readable without opening the file;
        # the short random suffix is only there to keep two sessions
        # started in the same second from colliding, not for uniqueness on
        # its own (unlike, e.g., a run's own runId elsewhere in this
        # project, nothing here needs to be traced back to a specific
        # source or record).
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        path = log_dir / f"{command}-{timestamp}-{uuid.uuid4().hex[:8]}{_SESSION_LOG_SUFFIX}"

        stream = open(path, "a", buffering=1)  # noqa: SIM115 - lifetime is this process's; never explicitly closed (process exit does it), same as configure_logging()'s own sink
        handler = logging.StreamHandler(stream=stream)
        handler.setFormatter(JsonFormatter())
        logging.getLogger("qara_reg_scraper").addHandler(handler)
        return path
    except OSError as e:
        get_logger("cli").warning(f"session log directory unavailable, continuing without it: {e}")
        return None


def _sweep_old_session_logs(log_dir: Path, retention_days: int) -> None:
    """Best-effort: every job container sharing this directory runs this
    same sweep on its own startup, so a file can legitimately be deleted
    by a DIFFERENT process between this one listing it and trying to
    unlink it — that race is expected and harmless (the end state, "the
    file is gone," is what both processes wanted), not an error."""
    if retention_days <= 0:
        return
    cutoff = datetime.now(UTC).timestamp() - retention_days * 86400
    for path in log_dir.glob(f"*{_SESSION_LOG_SUFFIX}"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            continue


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"qara_reg_scraper.{name}")


# Shared by http_client.py's PoliteHttpClient and service_client.py's
# ScraperServiceClient — both log a full request/response snapshot at
# DEBUG level (headers + this body snippet), gated behind
# `logger.isEnabledFor(logging.DEBUG)` so the (potentially large) body is
# never even read/decoded at INFO level. Truncated rather than dumped in
# full: eCFR alone fetches a ~20MB XML document, and a DEBUG log capturing
# that verbatim on every run would defeat its own purpose (unreadable,
# and the file grows unbounded). Binary content (PDFs, images) is never
# decoded as text at all — only its size and content-type are shown.
DEBUG_BODY_MAX_CHARS = 2000
_DEBUG_BODY_TEXT_CONTENT_TYPES = ("json", "xml", "html", "text", "csv")


def debug_body_snippet(content: bytes, content_type: str) -> str:
    if not any(kind in content_type.lower() for kind in _DEBUG_BODY_TEXT_CONTENT_TYPES):
        return f"<{len(content)} bytes, content-type={content_type!r} — not shown>"
    text = content.decode("utf-8", errors="replace")
    if len(text) <= DEBUG_BODY_MAX_CHARS:
        return text
    return f"{text[:DEBUG_BODY_MAX_CHARS]}... ({len(text)} chars total, truncated)"


def log_extra(logger: logging.Logger, level: int, message: str, **fields) -> None:
    logger.log(level, message, extra={"extra_fields": fields})
