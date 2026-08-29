"""Device Product Classification — openFDA `device/classification`.

https://open.fda.gov/apis/device/classification/

Unlike clearances_510k.py/recalls.py, this is *not* a lookback-windowed
activity stream — it's a stable reference table (product code -> device
class, regulation number, review panel) that changes only occasionally
(FDA reclassification proceedings), so every run walks the *whole*
catalog rather than "what happened in the last N days". Content-hashing
in Manifest.save_document makes repeat full walks cheap: an unchanged
record just bumps `last_checked_at`, no new version is archived.

`product_code` is the record's own natural, stable primary key — no
synthetic id needed. It's also the field that joins this source to
clearances_510k.py: every 510(k)/De Novo record carries the same
product_code, so this source is the entry point for turning "what does my
device do" into "which existing clearances are worth pulling as
candidate predicates" (see docs/sources/fda/classification.md).

Sorting note: openFDA rejects `sort=product_code:asc` here
("Sorting allowed by non-analyzed fields only") and `product_code.exact`
doesn't exist in this index's mapping either (confirmed live) -
`regulation_number.exact` does, and is stable enough for deterministic
skip-based pagination across a run. `search=product_code:*` is the
wildcard-everything query openFDA accepts in place of an empty search
string (an empty search is rejected with 400 - confirmed live).

Total catalog size is ~7,100 product codes (confirmed live) - well under
`max_records`'s default cap and openFDA's own ~25000 deep-paging limit,
so one run can walk the entire thing; `max_records` is still passed
explicitly rather than relying on the shared default, in case FDA's
catalog grows substantially before this comment is revisited.
"""

from __future__ import annotations

import json

from ...base_scraper import BaseScraper, BudgetExhausted, PreviewInfo
from ...manifest import RunSummary
from .openfda_common import iter_openfda_results

ENDPOINT = "https://api.fda.gov/device/classification.json"
SEARCH = "product_code:*"
SORT = "regulation_number.exact:asc"
MAX_RECORDS = 10000


class ClassificationScraper(BaseScraper):
    regulation = "fda"
    name = "classification"
    description = "FDA device product classification (product code -> device class/regulation)"
    label = "Product Classification"

    def run(self) -> RunSummary:
        self.log.info("classification: walking the full product classification catalog")

        try:
            for record in iter_openfda_results(self.http, ENDPOINT, SEARCH, sort=SORT, max_records=MAX_RECORDS):
                product_code = record.get("product_code", "unknown")
                if self.already_have(product_code):
                    self.manifest.summary.skipped_already_known += 1
                    continue
                # Checked before saving, not just after via _consume_budget:
                # max_new_documents=0 must record zero new documents, and a
                # post-save-only check can't stop the very first save.
                if self._budget_exhausted():
                    self.log.info(f"classification: budget reached ({self.max_new_documents} new records)")
                    self.manifest.summary.stop_reason = "budget_reached"
                    return self.manifest.finalize()
                self._save_record(product_code, record)
                try:
                    self._consume_budget()
                except BudgetExhausted:
                    self.log.info(f"classification: budget reached ({self.max_new_documents} new records)")
                    self.manifest.summary.stop_reason = "budget_reached"
                    return self.manifest.finalize()
            self.manifest.summary.stop_reason = "completed"
        except Exception as e:  # noqa: BLE001 - the listing call failed after retries
            self.log.warning(f"classification: listing call failed, stopping run early: {e}")
            self.manifest.record_error("__listing__", url=ENDPOINT, error=str(e))
            self.manifest.summary.stop_reason = "hard_stop"

        return self.manifest.finalize()

    def estimate(self) -> PreviewInfo:
        # Full pagination of the catalog - no per-document fetch (the
        # record's already inline in the listing response either way).
        try:
            product_codes = [
                record.get("product_code", "unknown")
                for record in iter_openfda_results(self.http, ENDPOINT, SEARCH, sort=SORT, max_records=MAX_RECORDS)
            ]
        except Exception as e:  # noqa: BLE001
            return PreviewInfo(total_available=None, already_known=None, note=f"could not query openFDA: {e}")
        known = sum(1 for code in product_codes if self.already_have(code))
        return PreviewInfo(total_available=len(product_codes), already_known=known)

    def _save_record(self, product_code: str, record: dict) -> None:
        content = json.dumps(record, indent=2, sort_keys=True).encode("utf-8")
        self.manifest.save_document(
            product_code,
            content,
            url=f"{ENDPOINT}?search=product_code:{product_code}",
            title=record.get("device_name"),
            ext="json",
            content_type="application/json",
            http_status=200,
            source_metadata={
                "product_code": product_code,
                "device_name": record.get("device_name"),
                "device_class": record.get("device_class"),
                "regulation_number": record.get("regulation_number"),
                "review_panel": record.get("review_panel"),
                "medical_specialty": record.get("medical_specialty"),
                "medical_specialty_description": record.get("medical_specialty_description"),
                "submission_type_id": record.get("submission_type_id"),
                "definition": record.get("definition"),
            },
        )
