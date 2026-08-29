"""Device recalls / enforcement reports — openFDA `device/enforcement`.

https://open.fda.gov/apis/device/enforcement/

Same pattern as clearances_510k.py: last `lookback_days` of activity by
report_date, newest first, content-hashed per recall so unchanged records
cost almost nothing on a re-run.

Unlike the other sources, a record's full content arrives inline in the
paginated *listing* response — there's no separate per-document fetch, so
`already_have`/budget/hard-stop apply a little differently here:

- Skipping an already-known record still saves the local write, but the
  network cost was already paid by the listing page it arrived on.
- The real lever for "don't redownload everything every night" is that
  `iter_openfda_results` is a lazy generator — once the per-run budget is
  hit and this loop `break`s, it simply never requests the next listing
  page. Fewer new records wanted -> fewer listing pages fetched.
- If the listing call itself fails after retries are exhausted, that's
  treated as a hard stop for the run (same spirit as HardStop elsewhere),
  just handled directly here since there's no fetch_and_save in the loop.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from ...base_scraper import BaseScraper, BudgetExhausted, PreviewInfo
from ...manifest import RunSummary
from .openfda_common import iter_openfda_results

ENDPOINT = "https://api.fda.gov/device/enforcement.json"
# Built-in fallback when neither --lookback-days nor
# regulations.fda.sources.recalls.lookback_days sets one — see
# BaseScraper.lookback_days / effective_lookback_days below.
DEFAULT_LOOKBACK_DAYS = 30


class RecallsScraper(BaseScraper):
    regulation = "fda"
    name = "recalls"
    description = "FDA medical device recalls / enforcement reports (openFDA device/enforcement)"
    label = "Recalls"

    @property
    def effective_lookback_days(self) -> int:
        return self.lookback_days if self.lookback_days is not None else DEFAULT_LOOKBACK_DAYS

    def run(self) -> RunSummary:
        end = datetime.now(UTC).date()
        start = end - timedelta(days=self.effective_lookback_days)
        # A literal space here, not "+": requests percent-encodes params
        # itself (space -> "+" in the wire form), and openFDA's parser
        # wants that "+" to mean "encoded space", not a literal plus sign.
        # Passing a literal "+" gets double-encoded to "%2B" and openFDA's
        # range-query parser rejects it.
        search = f"report_date:[{start:%Y-%m-%d} TO {end:%Y-%m-%d}]"
        self.log.info(f"recalls: fetching enforcement reports {start} .. {end}")

        try:
            for record in iter_openfda_results(self.http, ENDPOINT, search, sort="report_date:desc"):
                recall_number = record.get("recall_number") or record.get("event_id", "unknown")
                if self.already_have(recall_number):
                    self.manifest.summary.skipped_already_known += 1
                    continue
                # Checked before saving, not just after via _consume_budget:
                # max_new_documents=0 must record zero new documents, and a
                # post-save-only check can't stop the very first save.
                if self._budget_exhausted():
                    self.log.info(f"recalls: budget reached ({self.max_new_documents} new records)")
                    self.manifest.summary.stop_reason = "budget_reached"
                    return self.manifest.finalize()
                self._save_record(recall_number, record)
                try:
                    self._consume_budget()
                except BudgetExhausted:
                    self.log.info(f"recalls: budget reached ({self.max_new_documents} new records)")
                    self.manifest.summary.stop_reason = "budget_reached"
                    return self.manifest.finalize()
            self.manifest.summary.stop_reason = "completed"
        except Exception as e:  # noqa: BLE001 - the listing call failed after retries
            self.log.warning(f"recalls: listing call failed, stopping run early: {e}")
            self.manifest.record_error("__listing__", url=ENDPOINT, error=str(e))
            self.manifest.summary.stop_reason = "hard_stop"

        return self.manifest.finalize()

    def estimate(self) -> PreviewInfo:
        # Full pagination of the lookback window — cheap, window-bounded,
        # no per-document fetch (the record's already inline in the
        # listing response either way).
        end = datetime.now(UTC).date()
        start = end - timedelta(days=self.effective_lookback_days)
        search = f"report_date:[{start:%Y-%m-%d} TO {end:%Y-%m-%d}]"
        try:
            recall_numbers = [
                record.get("recall_number") or record.get("event_id", "unknown")
                for record in iter_openfda_results(self.http, ENDPOINT, search, sort="report_date:desc")
            ]
        except Exception as e:  # noqa: BLE001
            return PreviewInfo(total_available=None, already_known=None, note=f"could not query openFDA: {e}")
        known = sum(1 for r in recall_numbers if self.already_have(r))
        return PreviewInfo(
            total_available=len(recall_numbers), already_known=known,
            note=f"counts recalls reported in the last {self.effective_lookback_days} days only",
        )

    def _save_record(self, recall_number: str, record: dict) -> None:
        content = json.dumps(record, indent=2, sort_keys=True).encode("utf-8")
        self.manifest.save_document(
            recall_number,
            content,
            url=f"{ENDPOINT}?search=recall_number:{recall_number}",
            title=record.get("product_description"),
            ext="json",
            content_type="application/json",
            http_status=200,
            source_metadata={
                "recall_number": recall_number,
                "classification": record.get("classification"),
                "status": record.get("status"),
                "firm_name": record.get("recalling_firm"),
                "report_date": record.get("report_date"),
                "product_code": record.get("product_code"),
            },
        )
