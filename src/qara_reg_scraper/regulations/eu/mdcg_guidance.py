"""EU MDCG (Medical Device Coordination Group) guidance documents and
other Commission guidance on the MDR/IVDR.

Source: the European Commission's own guidance listing page —
https://health.ec.europa.eu/medical-devices-sector/new-regulations/
guidance-mdcg-endorsed-documents-and-other-guidance_en — confirmed live
while building this: it's a plain, server-rendered page (no JavaScript
needed, unlike fda:guidance's search widget), 21 separate `<table
class="ecl-table">` elements, one per topic category (Annex XVI products,
Borderline and Classification, EUDAMED, Notified bodies, UDI, ...), each
with a Reference / Title / Publication header row and 156 data rows total
(confirmed by parsing a live fetch: every row has exactly one link, no
empty rows).

health.ec.europa.eu/robots.txt (checked in full) doesn't disallow
anything under /medical-devices-sector/, and doesn't publish a
Crawl-delay — robots_policy.py still honors whatever it does say
automatically, same as every other source, no special-casing here.

Each row's link is USUALLY `/document/download/<uuid>_en?filename=...`
(a PDF, occasionally .docx/.doc/.xlsx) sitting in the Reference cell — but
not always: a handful of rows (7 out of 156, confirmed) have the link in
the Title cell instead (when Reference is plain text like "Q&A"), and 3
rows link to an entirely different destination — a EUR-Lex Official
Journal notice, a health.ec.europa.eu HTML "overview" page, and an
ema.europa.eu HTML page — none of which carry the usual UUID or a
filename extension at all. `_parse_row` checks both cells for the link
rather than assuming Reference; `_document_id`/`_ext_and_content_type`
both fall back cleanly (a hash of the URL, and html/text-html
respectively) for that handful of non-standard rows instead of assuming
every link fits the common shape.

Single page, no pagination, no per-document detail fetch needed beyond
the linked file itself — closest in shape to fda:warning_letters (a real,
server-rendered HTML table) rather than fda:guidance (a client-rendered
page whose real data lives in a separate JSON dataset).

MAINTENANCE NOTE: if this page's table count/structure changes
significantly, or `ecl-table`/`data-ecl-table-header` disappear, the
Commission has redesigned the page — reload it in a real browser and
re-derive the row shape.
"""

from __future__ import annotations

import hashlib
import re

from bs4 import BeautifulSoup

from ...base_scraper import BaseScraper, PreviewInfo
from ...manifest import RunSummary

BASE_URL = "https://health.ec.europa.eu"
LISTING_PATH = (
    "/medical-devices-sector/new-regulations/"
    "guidance-mdcg-endorsed-documents-and-other-guidance_en"
)
LISTING_URL = f"{BASE_URL}{LISTING_PATH}"

# The common case: a document-store link, filename's extension tells us
# what we're actually downloading. Rows that don't match this (a handful
# of external HTML pages, see module docstring) fall back to html/text-html.
_DOWNLOAD_ID_RE = re.compile(r"/document/download/([0-9a-f-]{36})_en")
_EXT_CONTENT_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "doc": "application/msword",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


class MdcgGuidanceScraper(BaseScraper):
    regulation = "eu"
    name = "mdcg_guidance"
    description = "EU MDCG guidance documents and other MDR/IVDR guidance (single listing page)"
    label = "MDCG Guidance"

    def run(self) -> RunSummary:
        try:
            by_id = self._discover()
        except Exception as e:  # noqa: BLE001 - can't proceed at all without this
            self.log.warning(f"mdcg_guidance: could not fetch/parse the listing page: {e}")
            self.manifest.record_error("__listing__", url=LISTING_URL, error=str(e))
            self.manifest.summary.stop_reason = "hard_stop"
            return self.manifest.finalize()

        self.log.info(f"mdcg_guidance: {len(by_id)} documents on the listing page")

        def fetch_one(document_id: str) -> None:
            self._save_row(document_id, by_id[document_id])

        self.process_candidates(list(by_id.keys()), fetch_one)
        return self.manifest.finalize()

    def estimate(self) -> PreviewInfo:
        try:
            by_id = self._discover()
        except Exception as e:  # noqa: BLE001
            return PreviewInfo(total_available=None, already_known=None, note=f"could not fetch listing page: {e}")
        known = sum(1 for document_id in by_id if self.already_have(document_id))
        return PreviewInfo(total_available=len(by_id), already_known=known)

    def _discover(self) -> dict[str, dict]:
        """One request for the whole listing page — shared by `run()` and
        `estimate()` so the preview counts exactly what a real run would
        see. Every `<table class="ecl-table">` on the page is one topic
        category; its preceding heading is that category's name."""
        response = self.http.get(LISTING_URL)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "lxml")

        by_id: dict[str, dict] = {}
        for table in soup.find_all("table", class_="ecl-table"):
            heading = table.find_previous(["h2", "h3", "h4"])
            category = heading.get_text(strip=True) if heading else None
            rows = table.find_all("tr")[1:]  # skip the header row
            for tr in rows:
                row = self._parse_row(tr, category)
                if row is not None:
                    by_id[row["document_id"]] = row
        return by_id

    @staticmethod
    def _parse_row(tr, category: str | None) -> dict | None:
        cells = tr.find_all("td")
        if not cells:
            return None
        by_header: dict[str, str] = {}
        href: str | None = None
        for cell in cells:
            header = cell.get("data-ecl-table-header", "").strip()
            by_header[header] = cell.get_text(strip=True)
            if href is None:
                a = cell.find("a", href=True)
                if a is not None:
                    href = a["href"]
        if not href:
            return None
        url = href if href.startswith("http") else f"{BASE_URL}{href}"
        return {
            "document_id": MdcgGuidanceScraper._document_id(url),
            "url": url,
            "reference": by_header.get("Reference") or None,
            "title": by_header.get("Title") or None,
            "publication": by_header.get("Publication") or None,
            "category": category,
        }

    @staticmethod
    def _document_id(url: str) -> str:
        match = _DOWNLOAD_ID_RE.search(url)
        if match:
            return match.group(1)
        # A handful of rows link somewhere other than the document store
        # (see module docstring) - no UUID to key off, so hash the URL
        # itself. Not pretty, but stable and unique.
        return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _ext_and_content_type(url: str) -> tuple[str, str]:
        match = re.search(r"\.([A-Za-z0-9]+)$", url)
        ext = match.group(1).lower() if match else None
        if ext in _EXT_CONTENT_TYPES:
            return ext, _EXT_CONTENT_TYPES[ext]
        return "html", "text/html"

    def _save_row(self, document_id: str, row: dict) -> None:
        ext, content_type = self._ext_and_content_type(row["url"])
        title = row["title"] or row["reference"] or document_id
        self.fetch_and_save(
            document_id=document_id,
            url=row["url"],
            title=title,
            ext=ext,
            content_type=content_type,
            source_metadata={
                "reference": row["reference"],
                "category": row["category"],
                "publication": row["publication"],
            },
        )
