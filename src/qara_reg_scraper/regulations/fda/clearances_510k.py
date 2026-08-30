"""510(k) and De Novo device clearances — openFDA `device/510k` endpoint,
plus the actual 510(k) summary PDF for each clearance.

https://open.fda.gov/apis/device/510k/

We pull records with a decision_date in the last `lookback_days` (default
30) on every run, sorted newest first. That's enough to catch new
clearances and any late corrections FDA makes to recent records, without
re-walking the full historical archive daily — and because each record is
content-hashed, re-fetching the same unchanged record on consecutive days
just logs a cheap "unchanged" event.

Two documents per clearance, since they're two different things: openFDA's
record is *metadata about* the clearance (device name, applicant, decision
date, ...), not the regulatory document itself. The actual 510(k) summary
— what a reviewer would actually want to read — is a separate PDF FDA
publishes at a predictable accessdata.fda.gov URL, fetched here too:

    documents/<k_number>/metadata/current.json   — the openFDA record
    documents/<k_number>/summary/current.pdf     — the 510(k) summary PDF

Not every clearance has one: FDA only requires a public summary when
`statement_or_summary == "Summary"` (the alternative, "Statement", means
the safety/effectiveness statement is available on request but not posted
online) — those are recorded as `not_applicable`, not an error, and never
attempted again.

Budget applies to the PDF fetch specifically, since that's the one part of
this source that costs a real per-document network round-trip to a host
known to bot-block (see the maintenance note below): metadata arrives free
with the listing page, so saving it never consumes budget.

Only a *suspected bot-management block* or a genuine network/retry
failure raises HardStop and ends the whole run — a clean 404/403/etc. for
one specific k-number (PDF never published, wrong year-digit guess for an
edge-case k-number, withdrawn record, ...) is routine and does NOT stop
the run; it's recorded as a per-document error and the next clearance is
attempted normally. Confirmed necessary in practice: a real run hit a
plain 404 (not a block — no Akamai markers matched) for one k-number and,
before this distinction existed, needlessly stopped the whole run.

MAINTENANCE NOTE: accessdata.fda.gov sits behind Akamai bot management,
which is a materially different posture than api.fda.gov/ecfr.gov (no
bot-management challenge observed there) — a well-identified, rate-limited
client can still get an HTML "apology" page back instead of the PDF,
especially from a datacenter/cloud egress IP, and that page has been
observed served with a 404 status, not just 200/403 — which is why the
block-detection check (`looks_like_bot_block`) runs regardless of status
code, before deciding whether a non-200 response is a block (hard stop) or
just a routine miss (record and continue). If you see runs consistently
stopping early with `bot_detection_suspected`, that's this host blocking
this environment's egress IP; see the discussion in the project history
for what to try (running from a non-datacenter IP first, then
`regulations.fda.sources.clearances_510k.requests_per_second` further
down, then an official bulk-data channel from FDA/CDRH as the durable fix).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

from ...base_scraper import BaseScraper, BotBlockDetected, BudgetExhausted, HardStop, PreviewInfo
from ...http_client import looks_like_bot_block
from ...logging_setup import log_extra
from ...manifest import RunSummary
from .openfda_common import iter_openfda_results

ENDPOINT = "https://api.fda.gov/device/510k.json"
# Built-in fallback when neither --lookback-days nor
# regulations.fda.sources.clearances_510k.lookback_days sets one — see
# BaseScraper.lookback_days / effective_lookback_days below.
DEFAULT_LOOKBACK_DAYS = 30
# Where the summary PDFs actually live — any path under this origin is
# governed by the same robots.txt (Hit-rate/Visiting-hours, see
# http_client.py/robots_policy.py), so the origin alone is enough to ask
# PoliteHttpClient.next_available_at whether this whole source is
# currently inside its host's declared crawling window.
ACCESSDATA_ORIGIN = "https://www.accessdata.fda.gov"


class Clearances510kScraper(BaseScraper):
    regulation = "fda"
    name = "clearances_510k"
    description = "FDA 510(k) / De Novo device clearances (openFDA device/510k) + summary PDFs"
    label = "510(k) Clearances"

    @property
    def effective_lookback_days(self) -> int:
        return self.lookback_days if self.lookback_days is not None else DEFAULT_LOOKBACK_DAYS

    def run(self) -> RunSummary:
        end = datetime.now(UTC).date()
        start = end - timedelta(days=self.effective_lookback_days)
        # See the comment in recalls.py — a literal space, not "+".
        search = f"decision_date:[{start:%Y-%m-%d} TO {end:%Y-%m-%d}]"
        self.log.info(f"clearances_510k: fetching decisions {start} .. {end}")

        try:
            for record in iter_openfda_results(self.http, ENDPOINT, search, sort="decision_date:desc"):
                k_number = record.get("k_number") or record.get("device_name", "unknown")
                if not self.already_have(f"{k_number}/metadata"):
                    self._save_metadata(k_number, record)

                if self.already_have(f"{k_number}/summary"):
                    self.manifest.summary.skipped_already_known += 1
                    continue
                try:
                    self._fetch_summary_pdf(k_number, record)
                except BudgetExhausted:
                    self.log.info(f"clearances_510k: budget reached ({self.max_new_documents} new PDFs)")
                    self.manifest.summary.stop_reason = "budget_reached"
                    return self.manifest.finalize()
                except HardStop as e:
                    self.log.warning(f"clearances_510k: stopping run early: {e}")
                    self.manifest.summary.stop_reason = "bot_block" if isinstance(e, BotBlockDetected) else "hard_stop"
                    return self.manifest.finalize()
            self.manifest.summary.stop_reason = "completed"
        except (BudgetExhausted, HardStop):
            raise  # programming error if this fires — the inner try should have caught it
        except Exception as e:  # noqa: BLE001 - the listing call itself failed after retries
            self.log.warning(f"clearances_510k: listing call failed, stopping run early: {e}")
            self.manifest.record_error("__listing__", url=ENDPOINT, error=str(e))
            self.manifest.summary.stop_reason = "hard_stop"

        return self.manifest.finalize()

    def estimate(self) -> PreviewInfo:
        # Full pagination of the lookback window, but no PDF fetches — the
        # window is small enough (30 days of clearances) that walking it
        # is cheap, unlike the guidance/warning_letters full backlogs.
        end = datetime.now(UTC).date()
        start = end - timedelta(days=self.effective_lookback_days)
        search = f"decision_date:[{start:%Y-%m-%d} TO {end:%Y-%m-%d}]"
        try:
            k_numbers = [
                record.get("k_number") or record.get("device_name", "unknown")
                for record in iter_openfda_results(self.http, ENDPOINT, search, sort="decision_date:desc")
            ]
        except Exception as e:  # noqa: BLE001
            return PreviewInfo(total_available=None, already_known=None, note=f"could not query openFDA: {e}")
        # "Already known" means the PDF (the actually network-costly part)
        # is already captured — not just the free metadata.
        known = sum(1 for k in k_numbers if self.already_have(f"{k}/summary"))
        return PreviewInfo(
            total_available=len(k_numbers), already_known=known,
            note=f"counts clearances decided in the last {self.effective_lookback_days} days only",
            next_available_at=self.http.next_available_at(ACCESSDATA_ORIGIN),
            next_available_note=self.http.visiting_hours_description(ACCESSDATA_ORIGIN),
        )

    def _save_metadata(self, k_number: str, record: dict) -> None:
        content = json.dumps(record, indent=2, sort_keys=True).encode("utf-8")
        self.manifest.save_document(
            f"{k_number}/metadata",
            content,
            url=f"{ENDPOINT}?search=k_number:{k_number}",
            title=record.get("device_name"),
            ext="json",
            content_type="application/json",
            http_status=200,
            source_metadata={
                "k_number": k_number,
                "applicant": record.get("applicant"),
                "decision_date": record.get("decision_date"),
                "decision_code": record.get("decision_code"),
                "product_code": record.get("product_code"),
                "clearance_type": record.get("clearance_type"),
                "statement_or_summary": record.get("statement_or_summary"),
            },
        )

    def _fetch_summary_pdf(self, k_number: str, record: dict) -> None:
        """Raises BudgetExhausted (via _consume_budget, on success, once the
        cap is hit) or HardStop (only for a suspected bot-management block
        or a network/retry failure — signals continuing would likely make
        things worse). A routine per-document miss (malformed k-number,
        plain 404/403/...) is recorded as an error and this returns
        normally instead, so `run()`'s loop moves on to the next
        clearance — see the module docstring."""
        document_id = f"{k_number}/summary"
        if record.get("statement_or_summary") != "Summary":
            # No public PDF to fetch — expected for "Statement" type
            # clearances, not a failure. Doesn't touch the budget or risk a
            # hard stop since no network call happens.
            #
            # Persisted via save_document (not just record_event) on
            # purpose — a plain record_event doesn't create the
            # current.meta.json sidecar already_have() checks, so a
            # Statement clearance would otherwise never actually become
            # "already have": every run would re-derive the PDF URL,
            # re-check statement_or_summary, and log another "unchanged"
            # event for it forever, contradicting this module's own
            # documented "never attempted again" — and inflating
            # estimate()'s "remaining" count by exactly the number of
            # Statement clearances in the window, permanently, since
            # already_known only ever counted candidates already_have()
            # actually recognized. Confirmed in practice: a real
            # documents/available/remaining triple was only reconcilable
            # once you assumed every Statement clearance was silently
            # excluded from already_known.
            self.manifest.save_document(
                document_id,
                b'{"not_applicable": true}',
                url=f"{ENDPOINT}?search=k_number:{k_number}",
                title=f"510(k) Summary — not applicable ({k_number})",
                ext="json",
                content_type="application/json",
                http_status=200,
                source_metadata={
                    "k_number": k_number,
                    "not_applicable": True,
                    "reason": "statement_or_summary != 'Summary'",
                },
            )
            return

        # FDA's summary PDFs live at a predictable path keyed off the two
        # digits following "K" in the k-number (the decision year).
        year_digits = k_number[1:3] if len(k_number) >= 3 else None
        if not year_digits or not year_digits.isdigit():
            # A data oddity with this one record, not a server-health
            # signal — record and move on rather than stopping the run.
            error = f"cannot derive PDF path from k_number {k_number!r}"
            self.manifest.record_error(document_id, url=None, error=error)
            return
        # Checked here, not at the top of this method: a not_applicable or
        # malformed-k-number record above never touches the network or the
        # budget either way, so max_new_documents=0 shouldn't stop those
        # from being recorded — only the actual costly PDF fetch below.
        if self._budget_exhausted():
            raise BudgetExhausted()

        url = f"https://www.accessdata.fda.gov/cdrh_docs/pdf{year_digits}/{k_number}.pdf"

        try:
            response = self.http.get(url)
        except Exception as e:
            # Retries already exhausted inside http.get() — an actual
            # signal the host is struggling, unlike a clean 4xx below.
            log_extra(
                self.log, logging.WARNING, "summary_pdf_fetch_failed",
                k_number=k_number, url=url, error=str(e),
            )
            self.manifest.record_error(document_id, url=url, error=str(e))
            raise HardStop(str(e)) from e

        # Checked regardless of status code, before deciding whether a
        # non-PDF response is a block (hard stop) or a routine miss
        # (record and continue) — Akamai's apology page has been observed
        # served with a 404 status, not just 200/403.
        if looks_like_bot_block(response):
            content_type = response.headers.get("Content-Type", "")
            error = f"likely bot-management block (status={response.status_code}, content-type={content_type!r})"
            log_extra(
                self.log, logging.WARNING, "bot_detection_suspected",
                k_number=k_number, url=url, status=response.status_code, content_type=content_type,
            )
            self.manifest.record_error(document_id, url=url, error=error)
            raise BotBlockDetected(error)

        content_type = response.headers.get("Content-Type", "")
        if response.status_code != 200 or "pdf" not in content_type.lower():
            # A genuine, non-block-page miss for this one k-number — the
            # PDF just isn't there under the expected path (never
            # published, an edge case in the year-digit guess, ...).
            # Routine, not a run-stopping problem.
            error = f"expected a PDF (status={response.status_code}, content-type={content_type!r})"
            log_extra(
                self.log, logging.WARNING, "unexpected_pdf_response",
                k_number=k_number, url=url, status=response.status_code, content_type=content_type,
            )
            self.manifest.record_error(document_id, url=url, error=error)
            return  # not a HardStop — move on to the next clearance

        self.manifest.save_document(
            document_id,
            response.content,
            url=url,
            title=f"510(k) Summary — {record.get('device_name', k_number)}",
            ext="pdf",
            content_type="application/pdf",
            http_status=response.status_code,
            source_metadata={"k_number": k_number},
        )
        self._consume_budget()
