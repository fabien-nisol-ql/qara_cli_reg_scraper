"""Structured logging setup.

Two separate output channels, on purpose:

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
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime


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


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"qara_reg_scraper.{name}")


def log_extra(logger: logging.Logger, level: int, message: str, **fields) -> None:
    logger.log(level, message, extra={"extra_fields": fields})
