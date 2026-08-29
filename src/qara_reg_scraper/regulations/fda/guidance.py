"""FDA guidance documents relevant to medical devices.

Source: FDA's own pre-generated static JSON dataset that backs the
DataTables widget on the guidance search page —
https://www.fda.gov/files/api/datatables/static/search-for-guidance.json
— found by loading the live search page in a real browser and watching
its network requests, since the search page itself is entirely
client-side rendered (the `<table>` in the server-sent HTML has a
`<thead>` but zero `<tbody>` rows; a naive HTML-table scrape of that page
returns nothing, which is exactly the bug this file replaces the fix for).
The dataset is FDA's own, not an inferred/reverse-engineered API shape —
confirmed working directly (2786 total guidance records across every
center, 641 tagged "Medical Devices", a sampled detail-page URL resolving
200) while fixing this.

Each dataset record covers *every* FDA guidance document, all centers —
we filter to ones whose `field_regulated_product_field` contains "Medical
Devices" (a comma-joined taxonomy string, so a combination-product
guidance like "Biologics, Medical Devices" still matches). One HTTP
request fetches the whole dataset; no pagination, unlike the old
approach.

This is also the source most likely to have a large backlog, so it still
leans on the shared skip/budget/hard-stop policy in base_scraper.py:
already-fetched documents are never requested again, one run only fetches
up to `max_new_documents_per_run` new ones, and an unretryable failure on
a document stops the run (a routine 404 for one document does not — see
base_scraper.py's fetch_and_save). The backlog fills in gradually over
many scheduled runs.

MAINTENANCE NOTE: if this dataset URL 404s or the "Medical Devices" tag
stops appearing, FDA has changed their DataTables config — reload the
search page in a real browser (JS execution required) and check its
network requests for whatever replaced this URL.
"""

from __future__ import annotations

import html as html_module

from bs4 import BeautifulSoup

from ...base_scraper import BaseScraper, PreviewInfo
from ...manifest import RunSummary

BASE_URL = "https://www.fda.gov"
DATASET_URL = f"{BASE_URL}/files/api/datatables/static/search-for-guidance.json"
PRODUCT_FIELD_MATCH = "Medical Devices"


class GuidanceScraper(BaseScraper):
    regulation = "fda"
    name = "guidance"
    description = "FDA guidance documents for medical devices (static datatables JSON dataset)"
    label = "Guidance Documents"

    def run(self) -> RunSummary:
        try:
            by_id = self._discover()
        except Exception as e:  # noqa: BLE001 - can't proceed at all without this
            self.log.warning(f"guidance: could not fetch/parse the dataset: {e}")
            self.manifest.record_error("__dataset__", url=DATASET_URL, error=str(e))
            self.manifest.summary.stop_reason = "hard_stop"
            return self.manifest.finalize()

        self.log.info(f"guidance: {len(by_id)} medical-device guidance documents in the dataset")

        def fetch_one(document_id: str) -> None:
            entry = by_id[document_id]
            self._save_row(document_id, entry["href"], entry["title"], entry["record"])

        self.process_candidates(list(by_id.keys()), fetch_one)
        return self.manifest.finalize()

    def estimate(self) -> PreviewInfo:
        try:
            by_id = self._discover()
        except Exception as e:  # noqa: BLE001
            return PreviewInfo(total_available=None, already_known=None, note=f"could not fetch dataset: {e}")
        known = sum(1 for document_id in by_id if self.already_have(document_id))
        return PreviewInfo(total_available=len(by_id), already_known=known)

    def _discover(self) -> dict[str, dict]:
        """One request for the whole dataset, filtered to Medical Devices
        — shared by `run()` and `estimate()` so the preview counts exactly
        what a real run would see."""
        response = self.http.get(DATASET_URL)
        response.raise_for_status()
        records = response.json()

        by_id: dict[str, dict] = {}
        for record in records:
            if PRODUCT_FIELD_MATCH not in record.get("field_regulated_product_field", ""):
                continue
            href, title = self._parse_link(record.get("title", ""))
            if not href or not title:
                continue
            by_id[self._document_id(href)] = {"href": href, "title": title, "record": record}
        return by_id

    @staticmethod
    def _parse_link(cell_html: str) -> tuple[str | None, str | None]:
        """Dataset text fields (`title`, `field_docket_number`, ...) are
        tiny HTML snippets, typically a single `<a href="...">text</a>`."""
        if not cell_html:
            return None, None
        soup = BeautifulSoup(cell_html, "lxml")
        a = soup.find("a", href=True)
        if a is None:
            return None, None
        return a["href"], a.get_text(strip=True)

    @staticmethod
    def _document_id(href: str) -> str:
        return href.rstrip("/").rsplit("/", 1)[-1]

    def _save_row(self, document_id: str, href: str, title: str, record: dict) -> None:
        url = href if href.startswith("http") else f"{BASE_URL}{href}"
        _docket_href, docket_number = self._parse_link(record.get("field_docket_number", ""))
        self.fetch_and_save(
            document_id=document_id,
            url=url,
            title=title,
            ext="html",
            content_type="text/html",
            source_metadata={
                "issue_date": record.get("field_issue_datetime") or None,
                "status": record.get("field_final_guidance_1") or None,
                "communication_type": record.get("field_communication_type") or None,
                "issuing_office": html_module.unescape(record.get("field_issuing_office_taxonomy", "")) or None,
                "center": html_module.unescape(record.get("field_center", "")) or None,
                "regulated_product_field": html_module.unescape(record.get("field_regulated_product_field", ""))
                or None,
                "docket_number": docket_number,
                "open_for_comment": (record.get("open-comment") or "").strip() or None,
            },
        )
