"""Push each source's *current* manifest state to qara-reg-scraper-svc via
REST — the `reindex` command's backing logic. Walks storage directly (same
namespace layout manifest.py writes), no database session anywhere; this
replaces the old db/reindex.py's SQLAlchemy-backed wipe-and-reload.

Deliberately NOT a historical replay: only pushes each document's *current*
current.meta.json state, the latest run summary file, and the current
estimate.json snapshot — never the full events/ history. POST /v1/events is
insert-only (see qara-reg-scraper-svc's EventController.java: "Always an
insert — every event, including repeated 'unchanged' checks, is its own
row."), so a bulk re-walk-and-repost of every historical event would create
duplicate rows on every repeat `reindex` invocation. Events are meant to
arrive live, exactly once, during a real `run` (see manifest.py) — `reindex`
exists to recover/backfill documents/runs/estimates if this service's
database was ever lost or rebuilt, not to replay history.
"""

from __future__ import annotations

from typing import Any

from .manifest import estimate_path
from .service_client import ScraperServiceClient
from .storage.base import StorageBackend
from .util import read_json_lenient


def sync_source(client: ScraperServiceClient, storage: StorageBackend, regulation: str, source: str) -> dict[str, int]:
    """Push one (regulation, source)'s current manifest state. Returns
    counts of what was pushed, for `reindex`'s summary table. A sync
    failure here raises ServiceSyncError the same as everywhere else (see
    service_client.py) — `reindex` doesn't catch it, so it surfaces plainly
    instead of reporting a partial/misleading count."""
    namespace = f"{regulation}/{source}"
    counts = {"documents": 0, "runs": 0, "estimate": 0}

    # -- documents (current state only, not version history) --------------
    for path in storage.list(f"{namespace}/documents"):
        if not path.endswith("current.meta.json"):
            continue
        meta = read_json_lenient(storage, path)
        if meta is None:
            continue
        client.upsert_document(_document_dto(meta, regulation, source))
        counts["documents"] += 1

    # -- most recent run summary only (not the full run history) -----------
    run_paths = sorted(p for p in storage.list(f"{namespace}/_manifest/runs") if p.endswith(".json"))
    if run_paths:
        data = read_json_lenient(storage, run_paths[-1])
        if data is not None:
            client.upsert_run(_run_dto(data, regulation, source))
            counts["runs"] = 1

    # -- current "what's left to do" snapshot -------------------------------
    estimate_data = read_json_lenient(storage, estimate_path(regulation, source))
    if estimate_data:
        client.put_source_estimate(regulation, source, _estimate_dto(estimate_data, regulation, source))
        counts["estimate"] = 1

    return counts


def sync(client: ScraperServiceClient, storage: StorageBackend, qualified_names: list[str]) -> dict[str, dict[str, int]]:
    """`qualified_names` are "regulation:source" strings, e.g. "fda:ecfr"."""
    results = {}
    for qualified_name in qualified_names:
        regulation, _, source = qualified_name.partition(":")
        results[qualified_name] = sync_source(client, storage, regulation, source)
    return results


# -- DTO builders (snake_case manifest JSON -> camelCase service wire format,
# same translation manifest.py's own _sync_* methods do at write time) ------


def _document_dto(meta: dict[str, Any], regulation: str, source: str) -> dict[str, Any]:
    return {
        "regulation": meta.get("regulation", regulation),
        "source": meta.get("source", source),
        "documentId": meta["document_id"],
        "title": meta.get("title"),
        "originalFilename": meta.get("original_filename"),
        "canonicalUrl": meta.get("canonical_url"),
        "storagePath": meta.get("current_storage_path"),
        "contentHash": meta.get("current_hash"),
        "contentType": meta.get("content_type"),
        "sizeBytes": meta.get("size_bytes"),
        "versionCount": len(meta.get("version_history", [])) or 1,
        "firstSeenAt": meta.get("first_seen_at"),
        "lastScrapedAt": meta.get("last_scraped_at"),
        "lastCheckedAt": meta.get("last_checked_at"),
        "lastChangedAt": meta.get("last_changed_at"),
        "sourceMetadata": meta.get("source_metadata") or {},
    }


def _run_dto(data: dict[str, Any], regulation: str, source: str) -> dict[str, Any]:
    return {
        "runId": data["run_id"],
        "regulation": data.get("regulation", regulation),
        "source": data.get("source", source),
        "startedAt": data.get("started_at"),
        "finishedAt": data.get("finished_at"),
        "status": data.get("status", "unknown"),
        "checked": data.get("checked", 0),
        "new": data.get("new", 0),
        "updated": data.get("updated", 0),
        "unchanged": data.get("unchanged", 0),
        "errors": data.get("errors", 0),
        "errorDetails": data.get("error_details", []),
    }


def _estimate_dto(data: dict[str, Any], regulation: str, source: str) -> dict[str, Any]:
    return {
        "regulation": regulation,
        "source": source,
        "computedAt": data.get("computed_at"),
        "totalAvailable": data.get("total_available"),
        "alreadyKnown": data.get("already_known"),
        "remaining": data.get("remaining"),
        "note": data.get("note"),
    }
