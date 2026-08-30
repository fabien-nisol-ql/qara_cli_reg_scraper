"""Medical Devices Active Licence Listing (MDALL) — Health Canada's
database of medical devices currently (or previously) licensed for sale
in Canada.

Source: Health Canada's own documented REST API (part of the same
`health-products.canada.ca` API family cited from open.canada.ca's Open
Government Portal dataset for MDALL):

    https://health-products.canada.ca/api/medical-devices/licence/?state=active&type=json&lang=en

Unlike fda:clearances_510k/pma (openFDA's paginated search), one HTTP
request returns the ENTIRE active-licence catalog as a single JSON array
— confirmed live, 35,654 records, ~10MB, no pagination at all. Real,
official API documentation exists at
`health-products.canada.ca/api/documentation/mdall-documentation-en.html`.
Same shape as fda:classification's "full-catalog walk of a stable
reference table" — no per-record network fetch needed, the record's
already inline in the listing response (see `_save_record` below).

`original_licence_no` is the record's own natural, stable primary key —
no synthetic id needed.

This only covers *active* licences (`state=active`); the same API also
serves `state=archived` (a licence that's since been cancelled/expired) —
not fetched here, since an archived licence is no longer relevant to
"what's currently licensed" the way this source is scoped, mirroring
fda:clearances_510k's own scope (current decisions, not a full historical
archive). Revisit if archived-licence history is ever wanted.

`health-products.canada.ca` has no robots.txt at all (confirmed live:
the `/robots.txt` path itself 404s, a real 404 page from the underlying
Tomcat server) — no declared restrictions.

At ~35,600 records (5x fda:classification's ~7,100), a full backfill at
the global default `max_new_documents_per_run` (1000) takes about 36 runs
— same "spread over many scheduled runs" tradeoff every other large
source in this tool already makes, not overridden here.
"""

from __future__ import annotations

import json

from ...base_scraper import BaseScraper, BudgetExhausted, PreviewInfo
from ...manifest import RunSummary

ENDPOINT = "https://health-products.canada.ca/api/medical-devices/licence/?state=active&type=json&lang=en"


class MdallScraper(BaseScraper):
    regulation = "ca"
    name = "mdall"
    description = "Medical Devices Active Licence Listing (MDALL), active device licences (Health Canada API)"
    label = "MDALL"

    def run(self) -> RunSummary:
        try:
            records = self._fetch_all()
        except Exception as e:  # noqa: BLE001 - the listing call failed after retries
            self.log.warning(f"mdall: listing call failed, stopping run early: {e}")
            self.manifest.record_error("__listing__", url=ENDPOINT, error=str(e))
            self.manifest.summary.stop_reason = "hard_stop"
            return self.manifest.finalize()

        self.log.info(f"mdall: {len(records)} active licences")
        for record in records:
            licence_no = self._licence_id(record)
            if self.already_have(licence_no):
                self.manifest.summary.skipped_already_known += 1
                continue
            # Checked before saving, not just after via _consume_budget:
            # max_new_documents=0 must record zero new documents, and a
            # post-save-only check can't stop the very first save.
            if self._budget_exhausted():
                self.log.info(f"mdall: budget reached ({self.max_new_documents} new records)")
                self.manifest.summary.stop_reason = "budget_reached"
                return self.manifest.finalize()
            self._save_record(licence_no, record)
            try:
                self._consume_budget()
            except BudgetExhausted:
                self.log.info(f"mdall: budget reached ({self.max_new_documents} new records)")
                self.manifest.summary.stop_reason = "budget_reached"
                return self.manifest.finalize()
        self.manifest.summary.stop_reason = "completed"
        return self.manifest.finalize()

    def estimate(self) -> PreviewInfo:
        # Full listing fetch - no per-document fetch (the record's already
        # inline in the listing response either way), same tradeoff as
        # fda:classification's own estimate().
        try:
            records = self._fetch_all()
        except Exception as e:  # noqa: BLE001
            return PreviewInfo(total_available=None, already_known=None, note=f"could not fetch MDALL: {e}")
        known = sum(1 for record in records if self.already_have(self._licence_id(record)))
        return PreviewInfo(total_available=len(records), already_known=known)

    def _fetch_all(self) -> list[dict]:
        response = self.http.get(ENDPOINT)
        response.raise_for_status()
        return response.json()

    @staticmethod
    def _licence_id(record: dict) -> str:
        return str(record.get("original_licence_no", "unknown"))

    def _save_record(self, licence_no: str, record: dict) -> None:
        content = json.dumps(record, indent=2, sort_keys=True).encode("utf-8")
        self.manifest.save_document(
            licence_no,
            content,
            url=ENDPOINT,
            title=record.get("licence_name"),
            ext="json",
            content_type="application/json",
            http_status=200,
            source_metadata={
                "original_licence_no": record.get("original_licence_no"),
                "licence_status": record.get("licence_status"),
                "appl_risk_class": record.get("appl_risk_class"),
                "licence_name": record.get("licence_name"),
                "first_licence_status_dt": record.get("first_licence_status_dt"),
                "licence_type_cd": record.get("licence_type_cd"),
                "licence_type_desc": record.get("licence_type_desc"),
                "company_id": record.get("company_id"),
            },
        )
