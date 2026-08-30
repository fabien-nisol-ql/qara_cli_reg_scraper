"""Medical device recalls and safety alerts — filtered from Health
Canada's "Recalls and Safety Alerts" open dataset.

Source: `recalls-rappels.canada.ca`'s own open-data JSON export (linked
from its Open Government Portal dataset page), updated daily per Health
Canada's own description:

    https://recalls-rappels.canada.ca/sites/default/files/opendata-donneesouvertes/HCRSAMOpenData.json

One HTTP request returns the WHOLE dataset — every recall/safety alert
across every category (food, consumer products, vehicles, health
products, etc.), confirmed live: 34,025 total records. Filtered here to
`Organization == "Medical devices"` (confirmed live: 8,622 of the
34,025) — the same "one big shared dataset, filter to what's relevant"
shape as fda:guidance's `field_regulated_product_field` filtering, and
the EU analog of fda:recalls (openFDA's `device/enforcement`).

`NID` is the record's own natural, stable primary key — no synthetic id
needed. No per-record network fetch needed (the record's already inline
in the listing response), same as ca:mdall/fda:classification; `URL`
(the human-readable recall page) is captured in `source_metadata` for
reference but not itself fetched.

`recalls-rappels.canada.ca/robots.txt` (checked in full — a standard
Drupal robots.txt) does not disallow the
`/sites/default/files/opendata-donneesouvertes/` path used here.

At ~8,600 medical-device records, a full backfill fits comfortably within
one run at the global default `max_new_documents_per_run` (1000 — would
take ~9 runs) — not overridden here, consistent with how this tool treats
similarly-sized sources.
"""

from __future__ import annotations

import json

from ...base_scraper import BaseScraper, BudgetExhausted, PreviewInfo
from ...manifest import RunSummary

DATASET_URL = "https://recalls-rappels.canada.ca/sites/default/files/opendata-donneesouvertes/HCRSAMOpenData.json"
ORGANIZATION_MATCH = "Medical devices"


class RecallsScraper(BaseScraper):
    regulation = "ca"
    name = "recalls"
    description = "Medical device recalls and safety alerts (Health Canada open dataset, filtered)"
    label = "Recalls & Safety Alerts"

    def run(self) -> RunSummary:
        try:
            records = self._fetch_filtered()
        except Exception as e:  # noqa: BLE001 - the dataset call failed after retries
            self.log.warning(f"recalls: could not fetch/parse the dataset: {e}")
            self.manifest.record_error("__dataset__", url=DATASET_URL, error=str(e))
            self.manifest.summary.stop_reason = "hard_stop"
            return self.manifest.finalize()

        self.log.info(f"recalls: {len(records)} medical-device recalls/alerts in the dataset")
        for nid, record in records.items():
            if self.already_have(nid):
                self.manifest.summary.skipped_already_known += 1
                continue
            if self._budget_exhausted():
                self.log.info(f"recalls: budget reached ({self.max_new_documents} new records)")
                self.manifest.summary.stop_reason = "budget_reached"
                return self.manifest.finalize()
            self._save_record(nid, record)
            try:
                self._consume_budget()
            except BudgetExhausted:
                self.log.info(f"recalls: budget reached ({self.max_new_documents} new records)")
                self.manifest.summary.stop_reason = "budget_reached"
                return self.manifest.finalize()
        self.manifest.summary.stop_reason = "completed"
        return self.manifest.finalize()

    def estimate(self) -> PreviewInfo:
        try:
            records = self._fetch_filtered()
        except Exception as e:  # noqa: BLE001
            return PreviewInfo(total_available=None, already_known=None, note=f"could not fetch dataset: {e}")
        known = sum(1 for nid in records if self.already_have(nid))
        return PreviewInfo(total_available=len(records), already_known=known)

    def _fetch_filtered(self) -> dict[str, dict]:
        """One request for the whole dataset, filtered to Medical
        devices — shared by `run()` and `estimate()` so the preview
        counts exactly what a real run would see."""
        response = self.http.get(DATASET_URL)
        response.raise_for_status()
        records = response.json()
        return {
            str(record["NID"]): record
            for record in records
            if record.get("Organization") == ORGANIZATION_MATCH and "NID" in record
        }

    def _save_record(self, nid: str, record: dict) -> None:
        content = json.dumps(record, indent=2, sort_keys=True).encode("utf-8")
        self.manifest.save_document(
            nid,
            content,
            url=DATASET_URL,
            title=record.get("Title"),
            ext="json",
            content_type="application/json",
            http_status=200,
            source_metadata={
                "nid": record.get("NID"),
                "canonical_url": record.get("URL"),
                "issue": record.get("Issue"),
                "category": record.get("Category"),
                "recall_class": record.get("Recall class"),
                "last_updated": record.get("Last updated"),
                "archived": record.get("Archived"),
            },
        )
