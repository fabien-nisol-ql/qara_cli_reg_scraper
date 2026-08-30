"""Cross-process, cross-container request pacing, per origin.

## The problem this fixes

`PoliteHttpClient`'s own per-host rate limiter (`_TokenBucket` in
http_client.py) lives in memory, inside one `PoliteHttpClient` instance —
i.e. scoped to one source, in one process. That's too narrow in two ways
this project's actual deployment hits in practice:

1. **Multiple sources, one host.** `fda:pma`, `fda:hde`, and
   `fda:clearances_510k` all fetch PDFs from the same host,
   `accessdata.fda.gov` — but each is a *different* scraper, and
   `cli.py`'s `run()` constructs a brand new `PoliteHttpClient` per
   source, even within one `run --source fda:pma,fda:hde` invocation.
   Three sources sharing a host still means three independent, mutually
   unaware token buckets.

2. **Every triggered run is a brand new process.** qara-reg-scraper-svc's
   `SourceRetryScheduler` doesn't run this CLI in one long-lived worker —
   it launches a fresh `docker run` job container per triggered source
   (see `DockerWorkloadOrchestrator` in that service). In-memory state
   can't survive past one process, so even a *single* source's own
   pacing memory is gone the moment its run ends, let alone shared with
   a sibling source's container.

**Confirmed live, 2026-08-30**: `SourceRetryScheduler` triggered
`fda:pma` and `fda:clearances_510k` within the same second (both shared
the exact same `nextAvailableAt` — the robots.txt Visiting-hours window
opening), with `fda:hde` following two minutes later. Each process
correctly paced *itself* to accessdata.fda.gov's own `Hit-rate: 30`
(~2s/request) — but with three independent processes doing that at once,
the *combined* request rate to the one shared host was roughly triple
what any single process intended. That tripped Akamai's own, separate
"excessive requests" abuse detection — a real block, reproduced live,
well inside the robots.txt-allowed window. Being compliant with
Visiting-hours does not make a client compliant with Hit-rate in
aggregate across multiple uncoordinated processes; both are the same
robots.txt, but only coordinated pacing satisfies the second one.

## The fix

Move the "when is this host next allowed to be hit" decision out of
process memory and onto disk, in the same shared storage every scraper
(and every separately-launched job container) already reads and writes
documents through — see `storage/base.py`'s `StorageBackend` and
`robots_policy.py`'s own cache, which this deliberately mirrors. A real
`filelock.FileLock` (already a dependency — see cli.py's existing
per-source run lock) guards one small state file per host, recording the
next allowed request time. Every `PoliteHttpClient`, in every process, in
every container, reserves its next slot against that ONE shared file
before issuing a request to that host — regardless of which source it
belongs to, or which process/container launched it. See
`http_client.py`'s `PoliteHttpClient._wait_for_slot` for the call site.

## Why this is best-effort, not a hard requirement

This only takes effect when `storage` resolves to a real local
filesystem path (`StorageBackend.local_root()`) — a `FileLock` needs an
actual POSIX path to lock against, which S3/Azure Blob/SharePoint-backed
storage can't provide (there's no equivalent of `flock()` for an object
store here, and building one is out of scope for what this project
actually needs today: the *current* deployment's collision was between
sibling Docker containers on the same host, sharing one local bind
mount). When no local root is available, `reserve_next_slot` below is a
no-op, and `PoliteHttpClient` falls back to its original, pre-existing
`_TokenBucket` behavior — correct for a single process/single source,
exactly as it always was before this module existed. Nothing regresses
for a caller with no `storage`, or a non-local one.

Likewise, a corrupt state file, a lock timeout, or any other unexpected
error here degrades to "proceed immediately" rather than raising — a
slower-than-strictly-necessary request, or in the worst case one
collision this mechanism failed to prevent, is a far smaller problem
than a scraper run failing outright over its own politeness bookkeeping.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from filelock import FileLock

from .logging_setup import get_logger, log_extra
from .robots_policy import origin_slug

log = get_logger(__name__)

_PACING_DIR = "_origin_pacing"

# How long to wait to ACQUIRE the lock file itself — not the pacing wait
# computed once it's held (that can legitimately be much longer, e.g. a
# slow Hit-rate). This bounds only "how long do I wait for my turn to
# even check the shared state," so a lock left behind by a killed/crashed
# process can never permanently wedge every future run against this host.
_LOCK_ACQUIRE_TIMEOUT_SECONDS = 30.0


def reserve_next_slot(storage_root: Path | None, host: str, min_interval_seconds: float) -> None:
    """Blocks the caller (via `time.sleep`) until it's this process's turn
    to make a request to `host`, then reserves the following slot for
    whichever caller — same process, or a different one entirely — asks
    next. The net effect, enforced across every process sharing
    `storage_root`, is the same "at most one request every
    `min_interval_seconds` to this host" a single `_TokenBucket` already
    gives within one process (see this module's own docstring for why
    that alone isn't enough).

    A no-op if `storage_root` is None (no shared local filesystem
    available) or `min_interval_seconds <= 0` (nothing to pace) — the
    caller is expected to have already resolved both before calling this,
    same as `_TokenBucket.wait()` always has.
    """
    if storage_root is None or min_interval_seconds <= 0:
        return

    pacing_dir = storage_root / _PACING_DIR
    lock_path = pacing_dir / f"{origin_slug(host)}.lock"
    state_path = pacing_dir / f"{origin_slug(host)}.json"

    try:
        pacing_dir.mkdir(parents=True, exist_ok=True)
        wait_seconds = 0.0
        with FileLock(str(lock_path), timeout=_LOCK_ACQUIRE_TIMEOUT_SECONDS):
            # Real wall-clock time (`time.time()`), not `time.monotonic()`:
            # this value is written by one process and read by another, so
            # it must mean the same thing across process boundaries —
            # `time.monotonic()` only guarantees ordering *within* the
            # process that read it, not across separate ones. This accepts
            # the usual wall-clock risk (NTP adjustment, DST) as the
            # lesser problem, same tradeoff `PreviewInfo.next_available_at`
            # already makes for the same cross-process reason.
            now = time.time()
            next_allowed_at = _read_next_allowed_at(state_path)
            if next_allowed_at is not None and next_allowed_at > now:
                wait_seconds = next_allowed_at - now
            reserved_until = max(now, next_allowed_at or 0.0) + min_interval_seconds
            _write_next_allowed_at(state_path, reserved_until)
        # Sleep AFTER releasing the lock — it's held only for the brief
        # read-decide-write step above, not for the wait itself, so this
        # process's own wait never blocks anyone else's turn to check in.
        if wait_seconds > 0:
            log_extra(log, logging.DEBUG, "origin_pacing_wait", host=host, wait_seconds=round(wait_seconds, 2))
            time.sleep(wait_seconds)
    except Exception as e:  # noqa: BLE001 - see module docstring: never block a run over this
        log_extra(log, logging.WARNING, "origin_pacing_unavailable", host=host, error=str(e))


def _read_next_allowed_at(state_path: Path) -> float | None:
    try:
        return json.loads(state_path.read_text())["next_allowed_at"]
    except (FileNotFoundError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def _write_next_allowed_at(state_path: Path, value: float) -> None:
    tmp = state_path.with_suffix(state_path.suffix + ".tmp")
    tmp.write_text(json.dumps({"next_allowed_at": value}))
    tmp.replace(state_path)  # atomic on the same filesystem — same pattern as LocalStorage.write_bytes
