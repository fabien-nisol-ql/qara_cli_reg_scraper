"""File-based manifest: the mandatory, DB-less record of what has been
scraped, when, and with what metadata.

This is the source of truth. qara-reg-scraper-svc's Postgres index (see
``qara_reg_scraper.service_client``) is purely a derived, disposable,
rebuildable convenience for querying — everything it contains can always be
reconstructed by walking the manifest files under a storage root
(``qara-reg-scraper reindex``, see ``qara_reg_scraper.sync``).

Layout under a storage root, per regulation and source (e.g. "fda/ecfr",
"fda/guidance", ...)::

    <regulation>/<source>/
      documents/
        <document_id>/
          current.<ext>              # latest fetched content
          current.meta.json          # sidecar: current state + version history
          versions/
            <timestamp>__<hash8>.<ext>   # prior versions, kept on every change
      _manifest/
        events/<yyyy>/<mm>/<dd>/<run_id>__<seq>__<document_id>__<event>.json
        runs/<run_id>.json           # one summary file per run

Events are one-file-per-event rather than an appended log, on purpose: none
of the four storage backends (local, S3, Azure Blob, SharePoint) offer an
atomic append primitive, so "append" here means "write a new, uniquely named
object" — safe under concurrent writers with zero locking, on every backend.

Every local write here is mirrored, right after it succeeds, to
qara-reg-scraper-svc (if a ``service_client`` was passed in) — this is the
only place that happens, once per manifest write, never as a bulk re-walk
(see ``service_client.py``'s module docstring for why). The local write is
never conditional on the REST push succeeding; the REST push failing after
retries raises ``ServiceSyncError`` (see ``service_client.py``), which this
module deliberately does not catch — cli.py's ``run`` command catches it at
the per-source level and cancels that source's run with a clear error,
rather than silently reporting nothing (see cli.py's ``run``).
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from .storage.base import StorageBackend

if TYPE_CHECKING:
    from .base_scraper import PreviewInfo  # deferred: base_scraper imports Manifest itself
    from .service_client import ScraperServiceClient

EventType = Literal["new", "updated", "unchanged", "error", "skipped_disallowed"]

_UNSAFE_PATH = re.compile(r"(^|/)\.\.(?=/|$)")


def estimate_path(regulation: str, source: str) -> str:
    """Path to the one 'what's left to do' snapshot file for a source —
    unlike runs/events (one file per run, historical), this is *current
    state*, overwritten every time it's recomputed. Shared between
    Manifest.write_estimate (the writer, in a real run) and
    local_status.compute_source_summary (the reader, for `summary`)."""
    return f"{regulation}/{source}/_manifest/estimate.json"


def utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def new_run_id(regulation: str, source: str) -> str:
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{regulation}-{source}-{ts}-{uuid.uuid4().hex[:8]}"


def _sanitize_document_id(document_id: str) -> str:
    doc_id = document_id.strip().strip("/")
    if not doc_id or _UNSAFE_PATH.search(doc_id):
        raise ValueError(f"unsafe document_id: {document_id!r}")
    return doc_id


@dataclass
class DocumentResult:
    document_id: str
    event: EventType
    storage_path: str | None = None
    content_hash: str | None = None
    size_bytes: int | None = None
    error: str | None = None


@dataclass
class RunSummary:
    run_id: str
    regulation: str
    source: str
    started_at: str
    finished_at: str | None = None
    status: Literal["running", "success", "partial_failure", "failed"] = "running"
    checked: int = 0
    new: int = 0
    updated: int = 0
    unchanged: int = 0
    errors: int = 0
    error_details: list[dict[str, str]] = field(default_factory=list)
    # How many candidates were skipped without any network call because
    # they're already in the manifest — the "don't redownload what we
    # already have" count. Not included in `checked`.
    skipped_already_known: int = 0
    # Why the run ended: "completed" (ran out of candidates on its own),
    # "budget_reached" (hit max_new_documents_per_run — expected, not a
    # problem), "hard_stop" (an unretryable, non-bot-block failure stopped
    # the run early to avoid hammering a server that's signaling trouble),
    # or "bot_block" — a HardStop specifically caused by a suspected
    # bot-management block (BotBlockDetected — see base_scraper.py's own
    # docstring). Distinct from plain "hard_stop" because it's handled
    # differently at every layer above: cli.py's run() never retries it
    # in-process (unlike other hard-stops, when a retry budget is set),
    # and qara-reg-scraper-svc's SourceRetryScheduler suspends automatic
    # retry immediately on seeing it, rather than after several
    # consecutive failures — continuing to probe a host mid-block
    # plausibly extends its own cooldown rather than ever letting it
    # clear (confirmed live, 2026-08-30).
    stop_reason: Literal["completed", "budget_reached", "hard_stop", "bot_block"] = "completed"

    def record(self, result: DocumentResult) -> None:
        self.checked += 1
        if result.event == "new":
            self.new += 1
        elif result.event == "updated":
            self.updated += 1
        elif result.event == "unchanged":
            self.unchanged += 1
        elif result.event in ("error", "skipped_disallowed"):
            self.errors += 1
            self.error_details.append({"document_id": result.document_id, "error": result.error or ""})

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "regulation": self.regulation,
            "source": self.source,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "status": self.status,
            "checked": self.checked,
            "new": self.new,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "errors": self.errors,
            "error_details": self.error_details,
            "skipped_already_known": self.skipped_already_known,
            "stop_reason": self.stop_reason,
        }


class Manifest:
    """Bound to one (storage backend, regulation, source) triple for the
    duration of a run — e.g. (local disk, "fda", "ecfr")."""

    def __init__(
        self,
        storage: StorageBackend,
        regulation: str,
        source: str,
        run_id: str | None = None,
        on_event: Callable[[str, EventType, str | None, int], None] | None = None,
        service_client: ScraperServiceClient | None = None,
    ):
        self.storage = storage
        self.regulation = regulation
        self.source = source
        self._namespace = f"{regulation}/{source}"
        self.run_id = run_id or new_run_id(regulation, source)
        self._event_seq = 0
        self.summary = RunSummary(
            run_id=self.run_id, regulation=regulation, source=source, started_at=utcnow_iso()
        )
        # Fires after every recorded event (new/updated/unchanged/error),
        # as (document_id, event, error, checked_so_far) — every scraper's
        # per-document outcome flows through record_event, so this is the
        # one place that can report live progress regardless of which
        # source is running. The CLI wires this to a console line so `run`
        # shows something happening between the start-of-source header and
        # the end-of-source summary, instead of just the raw
        # per-HTTP-request JSON log lines.
        self._on_event = on_event
        # None means "not reporting to qara-reg-scraper-svc" (no
        # service.base_url configured) — every _sync_* call below is then a
        # no-op. See the module docstring for why a sync failure, once a
        # client IS configured, propagates instead of being swallowed.
        self._service_client = service_client
        if self._service_client is not None:
            self._service_client.upsert_run(self._run_dto())

    # -- REST sync (qara-reg-scraper-svc) --------------------------------
    # One method per upsert endpoint, called from the exact local-write call
    # site below it. Field names are translated snake_case -> camelCase here
    # (verified live against the service's actual DTOs/JSON wire format) —
    # everywhere else in this file stays snake_case, matching the manifest's
    # own on-disk JSON.

    def _run_dto(self) -> dict[str, Any]:
        return {
            "runId": self.summary.run_id,
            "regulation": self.summary.regulation,
            "source": self.summary.source,
            "startedAt": self.summary.started_at,
            "finishedAt": self.summary.finished_at,
            "status": self.summary.status,
            "checked": self.summary.checked,
            "new": self.summary.new,
            "updated": self.summary.updated,
            "unchanged": self.summary.unchanged,
            "errors": self.summary.errors,
            "errorDetails": self.summary.error_details,
            # Previously omitted entirely — the local manifest JSON has always had this
            # (RunSummary.to_dict()), but it never actually reached the service, which is
            # exactly what SourceRetryScheduler now needs to react to a detected bot-block
            # immediately (see stop_reason's own docstring above).
            "stopReason": self.summary.stop_reason,
        }

    def _sync_event(self, payload: dict[str, Any]) -> None:
        if self._service_client is None:
            return
        self._service_client.record_event(
            {
                "runId": payload["run_id"],
                "regulation": payload["regulation"],
                "source": payload["source"],
                "documentId": payload["document_id"],
                "event": payload["event"],
                "ts": payload["ts"],
                "url": payload["url"],
                "httpStatus": payload["http_status"],
                "contentHash": payload["content_hash"],
                "storagePath": payload["storage_path"],
                "error": payload["error"],
            }
        )

    def _sync_document(self, meta: dict[str, Any]) -> None:
        if self._service_client is None:
            return
        self._service_client.upsert_document(
            {
                "regulation": meta["regulation"],
                "source": meta["source"],
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
        )

    # -- paths -----------------------------------------------------------

    def _doc_dir(self, document_id: str) -> str:
        return f"{self._namespace}/documents/{_sanitize_document_id(document_id)}"

    def _current_path(self, document_id: str, ext: str) -> str:
        return f"{self._doc_dir(document_id)}/current.{ext.lstrip('.')}"

    def _meta_path(self, document_id: str) -> str:
        return f"{self._doc_dir(document_id)}/current.meta.json"

    def _version_path(self, document_id: str, ts: str, content_hash: str, ext: str) -> str:
        return f"{self._doc_dir(document_id)}/versions/{ts}__{content_hash[:8]}.{ext.lstrip('.')}"

    def _event_path(self, document_id: str, event: EventType) -> str:
        now = datetime.now(UTC)
        self._event_seq += 1
        safe_doc = _sanitize_document_id(document_id).replace("/", "_")
        return (
            f"{self._namespace}/_manifest/events/{now:%Y}/{now:%m}/{now:%d}/"
            f"{self.run_id}__{self._event_seq:05d}__{safe_doc}__{event}.json"
        )

    # -- reading existing state ------------------------------------------

    def get_current_meta(self, document_id: str) -> dict[str, Any] | None:
        path = self._meta_path(document_id)
        if not self.storage.exists(path):
            return None
        return json.loads(self.storage.read_text(path))

    # -- recording ---------------------------------------------------------

    def record_event(
        self,
        document_id: str,
        event: EventType,
        *,
        url: str | None = None,
        http_status: int | None = None,
        content_hash: str | None = None,
        storage_path: str | None = None,
        error: str | None = None,
    ) -> None:
        payload = {
            "run_id": self.run_id,
            "regulation": self.regulation,
            "source": self.source,
            "document_id": document_id,
            "event": event,
            "ts": utcnow_iso(),
            "url": url,
            "http_status": http_status,
            "content_hash": content_hash,
            "storage_path": storage_path,
            "error": error,
        }
        self.storage.write_text(self._event_path(document_id, event), json.dumps(payload, indent=2))
        # Local state (summary counters, the live progress callback) is
        # brought fully up to date for this event BEFORE the REST push is
        # attempted — if the push then fails and raises, everything else
        # about this event is already durable and consistent, so the
        # exception is cleanly about the sync itself, nothing else.
        self.summary.record(
            DocumentResult(
                document_id=document_id,
                event=event,
                storage_path=storage_path,
                content_hash=content_hash,
                error=error,
            )
        )
        if self._on_event:
            self._on_event(document_id, event, error, self.summary.checked)
        self._sync_event(payload)

    def save_document(
        self,
        document_id: str,
        content: bytes,
        *,
        url: str,
        title: str | None,
        ext: str,
        content_type: str | None,
        http_status: int,
        original_filename: str | None = None,
        source_metadata: dict[str, Any] | None = None,
    ) -> DocumentResult:
        """Store a freshly fetched document, comparing against the existing
        sidecar (if any) by content hash. Archives the previous version on
        change. This is the main per-document entry point scrapers call.

        `original_filename` is what the source called this document (from
        Content-Disposition or the URL — see util.derive_original_filename),
        kept purely for display/identification. It's independent of
        `current.<ext>`, the internal "latest version" storage path every
        document uses regardless of source — see this module's docstring."""
        content_hash = sha256_hex(content)
        existing = self.get_current_meta(document_id)
        now = utcnow_iso()

        if existing and existing.get("current_hash") == content_hash:
            existing["last_checked_at"] = now
            # Backfill for documents captured before this field existed —
            # otherwise leave the existing value alone (a re-check that
            # yields no new filename shouldn't erase one already recorded).
            if original_filename and not existing.get("original_filename"):
                existing["original_filename"] = original_filename
            self.storage.write_text(self._meta_path(document_id), json.dumps(existing, indent=2))
            self._sync_document(existing)
            self.record_event(
                document_id,
                "unchanged",
                url=url,
                http_status=http_status,
                content_hash=content_hash,
                storage_path=existing.get("current_storage_path"),
            )
            return DocumentResult(document_id, "unchanged", existing.get("current_storage_path"), content_hash, len(content))

        current_path = self._current_path(document_id, ext)
        event: EventType = "updated" if existing else "new"

        if existing:
            # Archive the version being replaced before overwriting it.
            prev_hash = existing.get("current_hash", "unknown")
            prev_path = existing.get("current_storage_path")
            if prev_path and self.storage.exists(prev_path):
                archive_ts = existing.get("last_changed_at", now).replace(":", "").replace("-", "")
                version_path = self._version_path(document_id, archive_ts, prev_hash, ext)
                try:
                    self.storage.write_bytes(version_path, self.storage.read_bytes(prev_path))
                except FileNotFoundError:
                    pass  # nothing to archive — treat as best-effort

        self.storage.write_bytes(current_path, content, content_type=content_type)

        version_history = existing.get("version_history", []) if existing else []
        version_history.append({"ts": now, "hash": content_hash, "size_bytes": len(content)})

        meta = {
            "document_id": document_id,
            "regulation": self.regulation,
            "source": self.source,
            "title": title,
            "original_filename": original_filename or (existing.get("original_filename") if existing else None),
            "canonical_url": url,
            "first_seen_at": existing.get("first_seen_at", now) if existing else now,
            "last_scraped_at": now,
            "last_checked_at": now,
            "last_changed_at": now,
            "current_hash": content_hash,
            "current_storage_path": current_path,
            "content_type": content_type,
            "size_bytes": len(content),
            "version_history": version_history,
            "source_metadata": source_metadata or {},
        }
        self.storage.write_text(self._meta_path(document_id), json.dumps(meta, indent=2))
        self._sync_document(meta)
        self.record_event(
            document_id,
            event,
            url=url,
            http_status=http_status,
            content_hash=content_hash,
            storage_path=current_path,
        )
        return DocumentResult(document_id, event, current_path, content_hash, len(content))

    def record_error(self, document_id: str, *, url: str | None, error: str) -> None:
        self.record_event(document_id, "error", url=url, error=error)

    def write_estimate(self, info: PreviewInfo) -> None:
        """Persist the latest known 'what's left to do' snapshot for this
        source — total_available/already_known/remaining/note/
        next_available_at, as of right now. Called by the CLI right after
        a real run finishes (it calls scraper.estimate() once more);
        overwrites whatever was there before, since this is current
        state, not run history. `summary` (DB-less) and `status`/`reindex`
        (DB-backed, via this same file) both read it so "what's left"
        doesn't need a live re-query every time someone just wants to
        look. `next_available_at` specifically (None for most sources —
        see PreviewInfo's own docstring) is what lets
        qara-reg-scraper-svc's SourceRetryScheduler avoid triggering a job
        that would immediately no-op against a host's own declared
        robots.txt Visiting-hours, and what it surfaces to a human in the
        UI — see that service's README for the other half of this."""
        next_available_at = info.next_available_at.isoformat() if info.next_available_at else None
        payload = {
            "regulation": self.regulation,
            "source": self.source,
            "computed_at": utcnow_iso(),
            "total_available": info.total_available,
            "already_known": info.already_known,
            "remaining": info.remaining,
            "note": info.note,
            "next_available_at": next_available_at,
            "next_available_note": info.next_available_note,
        }
        self.storage.write_text(estimate_path(self.regulation, self.source), json.dumps(payload, indent=2))
        if self._service_client is not None:
            self._service_client.put_source_estimate(
                self.regulation,
                self.source,
                {
                    "regulation": payload["regulation"],
                    "source": payload["source"],
                    "computedAt": payload["computed_at"],
                    "totalAvailable": payload["total_available"],
                    "alreadyKnown": payload["already_known"],
                    "remaining": payload["remaining"],
                    "note": payload["note"],
                    "nextAvailableAt": next_available_at,
                    "nextAvailableNote": payload["next_available_note"],
                },
            )

    # -- run lifecycle -----------------------------------------------------

    def finalize(self, status: Literal["success", "partial_failure", "failed"] | None = None) -> RunSummary:
        self.summary.finished_at = utcnow_iso()
        self.summary.status = status or ("partial_failure" if self.summary.errors else "success")
        run_path = f"{self._namespace}/_manifest/runs/{self.run_id}.json"
        self.storage.write_text(run_path, json.dumps(self.summary.to_dict(), indent=2))
        if self._service_client is not None:
            self._service_client.upsert_run(self._run_dto())
        return self.summary
