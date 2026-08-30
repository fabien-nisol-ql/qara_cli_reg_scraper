"""Base class for every scraper, in every regulation.

qara-reg-scraper scrapes *regulations* (FDA, and whatever gets added next —
EU IVDR, Turkish/Chinese device regs, ...), each holding one or more named
*sources*. A concrete scraper lives at
`regulations/<regulation>/<source>.py`, sets `regulation`/`name` as class
attributes, and is addressed on the CLI as `<regulation>:<name>` (e.g.
`fda:ecfr`) — see `regulations/__init__.py` for how scrapers register
themselves and `regulations/fda/` for a full worked example. This module
holds everything regulation-agnostic: no FDA-specific (or EU-specific, ...)
assumptions belong here.

A scraper's job is narrow: discover *candidate* document ids, skip the ones
already captured, fetch the rest (up to a per-run budget), and hand each one
to the manifest. Retry/backoff/throttling lives in PoliteHttpClient;
hashing/versioning/event-logging lives in Manifest. Each concrete scraper is
meant to be run as its own process (`qara-reg-scraper run --source
fda:ecfr`), so multiple sources — across any number of regulations — can
run concurrently as independent cron entries without any shared state.

Three things keep this polite to a regulator's servers across a large
backlog, spread over days/weeks instead of hammered through in one run:

1. **Skip what we already have** (`already_have`) — a document already in
   the manifest costs zero network calls on later runs, by default forever
   (see `recheck_after_days` if you want periodic re-verification instead).
2. **Per-run budget** (`max_new_documents_per_run` in config) — a run stops
   cleanly, `stop_reason="budget_reached"`, once it's fetched that many new
   documents, leaving the rest of the backlog for tomorrow's scheduled run.
3. **Hard-stop only when continuing would likely make things worse** — a
   fetch that fails after PoliteHttpClient's own retries are exhausted, is
   blocked by robots.txt, or looks like a bot-management block raises
   `HardStop`, ending the *entire run* immediately (`stop_reason="hard_stop"`)
   rather than working through the remaining candidates against a server
   that's already signaling trouble. A routine 4xx/5xx for one specific
   document (a dead link, a withdrawn record, ...) is deliberately NOT a
   HardStop — that carries no signal about the server's health, so it's
   just a per-document error; the run moves on to the next candidate.
   Tomorrow's run resumes from the same already-have state either way.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from .http_client import PoliteHttpClient, RobotsDisallowed, looks_like_bot_block
from .logging_setup import get_logger, log_extra
from .manifest import Manifest, RunSummary
from .util import derive_original_filename


class BudgetExhausted(Exception):
    """Internal control-flow signal: max_new_documents_per_run has been
    reached for this run. Not a failure — caught by process_candidates and
    turned into a clean, expected stop."""


class HardStop(Exception):
    """Raised when a failure is severe enough (robots.txt disallow, a
    fetch that failed after all retries, a suspected bot-management block)
    that the right response is to stop this run entirely rather than keep
    working through the rest of the candidate list against a server that's
    already signaling trouble. The next scheduled run picks up where this
    one left off — nothing about already-fetched documents is lost."""


@dataclass
class PreviewInfo:
    """What `qara-reg-scraper run --preview` reports for one source,
    computed without fetching any document content — only cheap discovery
    calls (a listing/dataset request, or none at all) plus local
    `already_have` checks. Either count can be `None` when a source has no
    cheap way to know it (see each scraper's `estimate()` override for
    why, when that's the case)."""

    total_available: int | None
    already_known: int | None
    note: str | None = None
    #: The next UTC time this source's own host says (via robots.txt
    #: Visiting-hours) it's next fetchable — None for the vast majority of
    #: sources, whose host declares no such restriction, or if fetchable
    #: right now either way. Only a source whose estimate() actually
    #: checks `self.http.next_available_at(...)` for its own host ever
    #: sets this (clearances_510k.py/pma.py/hde.py, today) — every other
    #: scraper's PreviewInfo leaves it at the default. See
    #: Manifest.write_estimate: this is what lets qara-reg-scraper-svc's
    #: own scheduler avoid triggering a job that would immediately no-op,
    #: and surfaces the same information to a human in the UI.
    next_available_at: datetime | None = None
    #: A human-readable description of the RECURRING window itself (e.g.
    #: "11:00 PM–5:00 AM America/New York"), not just the one future
    #: instant `next_available_at` is — set alongside it (same sources,
    #: same `self.http.visiting_hours_description(...)` call), so a UI can
    #: explain WHY a source pauses on a schedule at all, not just when it
    #: resumes this one time. None whenever `next_available_at` is too.
    next_available_note: str | None = None

    @property
    def remaining(self) -> int | None:
        if self.total_available is None or self.already_known is None:
            return None
        return max(0, self.total_available - self.already_known)


class BaseScraper(ABC):
    #: Regulation namespace this scraper belongs to (e.g. "fda", "eu") —
    #: set by the module under regulations/<regulation>/, never "base".
    regulation: str = "base"
    #: Source name within that regulation (e.g. "ecfr").
    name: str = "base"
    description: str = ""
    #: Short display name (e.g. "eCFR") — distinct from the longer
    #: `description` above. Used by `sync-sources`/`run` when pushing the
    #: known-source registry to qara-reg-scraper-svc (see
    #: docs/source-registry-sync.md); falls back to `name` if left unset.
    label: str = ""

    def __init__(
        self,
        http: PoliteHttpClient,
        manifest: Manifest,
        *,
        max_new_documents: int | None = None,
        recheck_after_days: int | None = None,
        lookback_days: int | None = None,
        time_budget_minutes: int | None = None,
    ):
        self.http = http
        self.manifest = manifest
        self.max_new_documents = max_new_documents
        self.recheck_after_days = recheck_after_days
        # Only meaningful to a "listing window" source (fda:clearances_510k,
        # fda:recalls) — None here means "use that scraper's own built-in
        # default", read via its own `effective_lookback_days` property.
        # Sources with a fixed candidate list (ecfr) or their own discovery
        # shape (guidance, warning_letters) just never read this.
        self.lookback_days = lookback_days
        self.log = get_logger(f"{self.regulation}.{self.name}")
        self._new_count = 0
        # A DOCUMENT-count budget (max_new_documents) alone assumes fetches
        # are roughly uniformly fast — true for most sources, badly wrong
        # for one whose host enforces a real per-request pace (confirmed
        # live: accessdata.fda.gov's own robots.txt Hit-rate, honored
        # automatically now — see http_client.py/robots_policy.py). A
        # retry-triggered job's own "--max-new-documents -1" (unlimited —
        # see ScrapeJobService#triggerRetry) at a 30s/request pace could
        # otherwise run for many HOURS in one uninterruptible attempt,
        # instead of yielding back to the next scheduled retry — this is
        # the second, independent budget that actually bounds that: once
        # `time_budget_minutes` of wall-clock time has elapsed, treat the
        # run exactly as budget_exhausted, the same clean stop every
        # scraper already handles — no per-scraper code needed. None (the
        # default) means no such cap, unchanged from before this existed.
        self._deadline = time.monotonic() + time_budget_minutes * 60 if time_budget_minutes is not None else None

    @property
    def qualified_name(self) -> str:
        """The `<regulation>:<name>` string used to address this scraper
        on the CLI and in config.yaml, e.g. "fda:ecfr"."""
        return f"{self.regulation}:{self.name}"

    @abstractmethod
    def run(self) -> RunSummary:
        """Discover and fetch this source's documents, recording each one
        through self.manifest, then return manifest.finalize()."""

    def estimate(self) -> PreviewInfo:
        """Cheap, no-document-content-fetched preview of how much work a
        real `run()` has ahead of it — backs `qara-reg-scraper run
        --preview`. Default: not supported (some sources genuinely have no
        cheap way to know a total, e.g. a client-rendered total count with
        no lightweight metadata endpoint) — override where it's feasible;
        see regulations/fda/*.py for examples covering three different
        answers (exact via a fixed list, exact via a cheap query, honestly
        "unknown but here's what we do know locally")."""
        return PreviewInfo(total_available=None, already_known=None, note="preview not implemented for this source")

    # -- shared "don't redownload what we already have" plumbing ---------

    def already_have(self, document_id: str) -> bool:
        """True if `document_id` should be skipped without any network
        call. True forever once a document's first captured, unless
        `recheck_after_days` is set and it's older than that."""
        meta = self.manifest.get_current_meta(document_id)
        if meta is None:
            return False
        if self.recheck_after_days is None:
            return True
        checked_at = meta.get("last_checked_at") or meta.get("last_scraped_at")
        if not checked_at:
            return False
        try:
            age_days = (datetime.now(UTC) - datetime.fromisoformat(checked_at)).days
        except ValueError:
            return False
        return age_days < self.recheck_after_days

    def _budget_exhausted(self) -> bool:
        """True once `max_new_documents` new documents have already been
        fetched this run — including immediately, if `max_new_documents`
        is 0 (no new fetches allowed at all this run). Check this BEFORE
        attempting a fetch: relying only on `_consume_budget`'s post-fetch
        raise can't stop the very first fetch when the budget is already
        zero, which is exactly the case `--max-new-documents 0` needs to
        get right (see `fetch_and_save` and `process_candidates`). Also
        true once `time_budget_minutes` of wall-clock time has elapsed
        (see `__init__`) — same clean budget_reached stop, just a second,
        independent trigger for it."""
        if self._deadline is not None and time.monotonic() >= self._deadline:
            return True
        return self.max_new_documents is not None and self._new_count >= self.max_new_documents

    def _consume_budget(self) -> None:
        self._new_count += 1
        if self._deadline is not None and time.monotonic() >= self._deadline:
            raise BudgetExhausted()
        if self.max_new_documents is not None and self._new_count >= self.max_new_documents:
            raise BudgetExhausted()

    def process_candidates(self, document_ids: Iterable[str], fetch_one: Callable[[str], None]) -> None:
        """Drive the skip/budget/hard-stop policy over a flat sequence of
        candidate document ids, calling `fetch_one(document_id)` for each
        one not already captured. Sets `self.manifest.summary.stop_reason`
        and returns — never raises. Sources with a more irregular shape
        (paginated discovery, multiple sub-documents per candidate) apply
        the same primitives (`already_have`, `_budget_exhausted`,
        `_consume_budget`, catching `HardStop`/`BudgetExhausted`) directly
        instead of using this."""
        for document_id in document_ids:
            if self.already_have(document_id):
                self.manifest.summary.skipped_already_known += 1
                continue
            if self._budget_exhausted():
                log_extra(
                    self.log, logging.INFO, "budget_exhausted",
                    max_new_documents=self.max_new_documents,
                )
                self.manifest.summary.stop_reason = "budget_reached"
                return
            try:
                fetch_one(document_id)
            except BudgetExhausted:
                log_extra(
                    self.log, logging.INFO, "budget_exhausted",
                    max_new_documents=self.max_new_documents,
                )
                self.manifest.summary.stop_reason = "budget_reached"
                return
            except HardStop as e:
                log_extra(self.log, logging.WARNING, "run_stopped_early", reason=str(e))
                self.manifest.summary.stop_reason = "hard_stop"
                return
        self.manifest.summary.stop_reason = "completed"

    def fetch_and_save(
        self,
        *,
        document_id: str,
        url: str,
        title: str | None,
        ext: str,
        content_type: str | None,
        source_metadata: dict | None = None,
    ) -> None:
        """Fetch a URL and save it.

        Raises HardStop — deliberately, not swallowed — only for signals
        that mean continuing would likely make things worse for the
        server: robots.txt disallow, a fetch that failed after all of
        PoliteHttpClient's own retries (i.e. the well-known "server is
        struggling" status codes were already retried and still failed),
        or a suspected bot-management block. Those stop the *whole run*.

        A clean 4xx/5xx that PoliteHttpClient didn't already retry —
        ordinary 404/403/410/etc. for one specific document — is NOT a
        HardStop: it just means this one document is unavailable (dead
        link, withdrawn, wrong guess at a URL pattern, ...), which is
        routine when walking a list of many documents and carries no
        signal about the server's health. That's recorded as a normal
        per-document error and this method returns normally so the caller
        moves on to the next candidate.

        Either way, the error is recorded in the manifest before returning
        or raising, so it's visible regardless. Consumes one unit of the
        per-run new-document budget on success — callers should check
        `already_have` before calling this so a cache hit never touches
        the budget. Also checks the budget BEFORE fetching (raising
        BudgetExhausted immediately if it's already exhausted) so a
        `max_new_documents=0` run never makes this call's network request
        at all — a post-fetch-only check couldn't prevent that first one."""
        if self._budget_exhausted():
            raise BudgetExhausted()
        try:
            response = self.http.get(url)
        except RobotsDisallowed as e:
            self.manifest.record_error(document_id, url=url, error=str(e))
            raise HardStop(str(e)) from e
        except Exception as e:
            log_extra(self.log, logging.WARNING, "fetch_failed", document_id=document_id, url=url, error=str(e))
            self.manifest.record_error(document_id, url=url, error=str(e))
            raise HardStop(str(e)) from e

        # Checked before raise_for_status() on purpose: a bot-management
        # challenge page can be served with a 4xx/5xx status (confirmed —
        # see clearances_510k.py's maintenance note), and that's still a
        # "stop the run" signal regardless of status code, not a routine
        # per-document miss.
        if looks_like_bot_block(response):
            error = (
                f"response looks like a bot-management block/challenge page "
                f"(status={response.status_code}), not the real document — see "
                f"http_client.looks_like_bot_block"
            )
            log_extra(
                self.log, logging.WARNING, "bot_detection_suspected",
                document_id=document_id, url=url, status=response.status_code,
            )
            self.manifest.record_error(document_id, url=url, error=error)
            raise HardStop(error)

        try:
            response.raise_for_status()
        except Exception as e:  # noqa: BLE001 - deliberately broad: any 4xx/5xx
            # raise_for_status() raises here is, by construction, one PoliteHttpClient
            # didn't already retry (see RETRYABLE_STATUS_CODES) — routine, not fatal.
            log_extra(
                self.log, logging.WARNING, "fetch_failed_non_fatal",
                document_id=document_id, url=url, status=response.status_code, error=str(e),
            )
            self.manifest.record_error(document_id, url=url, error=str(e))
            return  # not a HardStop — this one document is unavailable, move on

        self.manifest.save_document(
            document_id,
            response.content,
            url=url,
            title=title,
            ext=ext,
            content_type=content_type or response.headers.get("Content-Type"),
            http_status=response.status_code,
            original_filename=derive_original_filename(url, response.headers),
            source_metadata=source_metadata,
        )
        self._consume_budget()
