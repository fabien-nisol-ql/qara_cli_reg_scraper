"""Read-only summary of what's been scraped, computed directly from the
file-based manifests — no qara-reg-scraper-svc required.

This is the service-less counterpart to `reindex`/`status`: same source of
truth (the manifests, see manifest.py's module docstring), just read
straight from storage instead of via the service's REST API. Useful when
you haven't set up qara-reg-scraper-svc at all, or just want a quick answer
without it — `qara-reg-scraper summary` works the moment you've scraped
anything, no `QARA_REG_SCRAPER_SERVICE__BASE_URL` required.
"""

from __future__ import annotations

from dataclasses import dataclass

from .manifest import estimate_path
from .storage.base import StorageBackend
from .util import read_json_lenient


@dataclass
class SourceSummary:
    regulation: str
    source: str
    documents: int = 0
    last_run_id: str | None = None
    last_started_at: str | None = None
    last_finished_at: str | None = None
    last_status: str | None = None
    last_stop_reason: str | None = None
    last_checked: int = 0
    last_new: int = 0
    last_updated: int = 0
    last_unchanged: int = 0
    last_skipped_already_known: int = 0
    last_errors: int = 0
    # "What's left to do" — from the estimate.json snapshot written after
    # the most recent real run (see Manifest.write_estimate), not
    # recomputed live here. None when no run has ever written one yet, or
    # for a source whose estimate() honestly doesn't know a total.
    total_available: int | None = None
    already_known: int | None = None
    remaining: int | None = None
    estimate_note: str | None = None
    estimate_computed_at: str | None = None


def compute_source_summary(storage: StorageBackend, regulation: str, source: str) -> SourceSummary:
    """Walk `<regulation>/<source>/documents/` and `.../_manifest/runs/`
    directly — no DB, no reindex step. `documents` counts every
    `current.meta.json` sidecar found; the `last_*` fields come from
    whichever run summary has the latest `started_at`."""
    namespace = f"{regulation}/{source}"
    summary = SourceSummary(regulation=regulation, source=source)

    for path in storage.list(f"{namespace}/documents"):
        if path.endswith("current.meta.json"):
            summary.documents += 1

    latest_run: dict | None = None
    for path in storage.list(f"{namespace}/_manifest/runs"):
        if not path.endswith(".json"):
            continue
        data = read_json_lenient(storage, path)
        if data is None:
            continue
        if latest_run is None or (data.get("started_at") or "") > (latest_run.get("started_at") or ""):
            latest_run = data

    if latest_run:
        summary.last_run_id = latest_run.get("run_id")
        summary.last_started_at = latest_run.get("started_at")
        summary.last_finished_at = latest_run.get("finished_at")
        summary.last_status = latest_run.get("status")
        summary.last_stop_reason = latest_run.get("stop_reason")
        summary.last_checked = latest_run.get("checked", 0)
        summary.last_new = latest_run.get("new", 0)
        summary.last_updated = latest_run.get("updated", 0)
        summary.last_unchanged = latest_run.get("unchanged", 0)
        summary.last_skipped_already_known = latest_run.get("skipped_already_known", 0)
        summary.last_errors = latest_run.get("errors", 0)

    estimate = read_json_lenient(storage, estimate_path(regulation, source))
    if estimate:
        summary.total_available = estimate.get("total_available")
        summary.already_known = estimate.get("already_known")
        summary.remaining = estimate.get("remaining")
        summary.estimate_note = estimate.get("note")
        summary.estimate_computed_at = estimate.get("computed_at")

    return summary
