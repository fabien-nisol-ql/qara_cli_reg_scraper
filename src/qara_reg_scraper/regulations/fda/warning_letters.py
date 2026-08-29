"""FDA warning letters.

Source: fda.gov/inspections-compliance-enforcement-and-criminal-investigations/
compliance-actions-and-activities/warning-letters — confirmed live and
fixed while building this: the original URL (missing the
"compliance-actions-and-activities" segment) 404s. Unlike guidance.py's
source page, this one's results table IS server-rendered — the first page
of real rows, with real per-letter detail-page links, comes back in the
initial HTML, and plain `?page=N` genuinely paginates server-side
(confirmed: page 0 and page 1 return different, real rows) — no
JavaScript/AJAX needed to read it, unlike the guidance search page.

There is no working device-specific filter: the page's own "Issuing
Office" exposed filter (`search_api_fulltext_issuing_office`) does not
actually filter server-side (tested directly — a request with it set
returns the identical unfiltered first page), and the site's DataTables
Excel export is capped at 1,000 of ~3,650 total rows and doesn't include
per-letter links at all, so it isn't a viable substitute. Rather than ship
a filter that silently does nothing, this scrapes every warning letter
(all centers, not just devices) and records `issuing_office` in each
document's metadata — filter to CDRH-issued letters downstream (DB query
or a config-level source_metadata check) instead of relying on an FDA
search parameter that doesn't work.

See guidance.py's module docstring for the skip/budget/hard-stop policy
that spreads this large backlog over many scheduled runs instead of one.
"""

from __future__ import annotations

from bs4 import BeautifulSoup

from ...base_scraper import BaseScraper, BudgetExhausted, HardStop, PreviewInfo
from ...local_status import compute_source_summary
from ...manifest import RunSummary

BASE_URL = "https://www.fda.gov"
SEARCH_PATH = (
    "/inspections-compliance-enforcement-and-criminal-investigations"
    "/compliance-actions-and-activities/warning-letters"
)
# Real pagination confirmed (page 0 vs page 1 return different rows); this
# is a safety cap on how many listing pages one run walks, not the primary
# pacing mechanism (max_new_documents_per_run is) — ~3,650 letters exist
# in total (365 pages @ 10/page), so a full backfill takes many runs
# regardless.
MAX_PAGES = 25


class WarningLettersScraper(BaseScraper):
    regulation = "fda"
    name = "warning_letters"
    description = "FDA warning letters, all centers (warning-letters listing, paginated)"
    label = "Warning Letters"

    def run(self) -> RunSummary:
        page = 0
        while page < MAX_PAGES:
            url = f"{BASE_URL}{SEARCH_PATH}"
            try:
                response = self.http.get(url, params={"page": page})
                response.raise_for_status()
            except Exception as e:  # noqa: BLE001 - retries already exhausted inside http.get
                self.log.warning(f"warning_letters: listing page {page} failed, stopping run early: {e}")
                self.manifest.record_error(f"__listing_page_{page}__", url=url, error=str(e))
                self.manifest.summary.stop_reason = "hard_stop"
                return self.manifest.finalize()

            rows = self._parse_result_rows(response.text)
            if not rows:
                self.log.info(f"warning_letters: no more rows at page {page}, stopping")
                self.manifest.summary.stop_reason = "completed"
                break

            for row in rows:
                document_id = row["document_id"]
                if self.already_have(document_id):
                    self.manifest.summary.skipped_already_known += 1
                    continue
                try:
                    self._save_row(row)
                except BudgetExhausted:
                    self.log.info(f"warning_letters: budget reached ({self.max_new_documents} new documents)")
                    self.manifest.summary.stop_reason = "budget_reached"
                    return self.manifest.finalize()
                except HardStop as e:
                    self.log.warning(f"warning_letters: stopping run early: {e}")
                    self.manifest.summary.stop_reason = "hard_stop"
                    return self.manifest.finalize()
            page += 1
        else:
            self.manifest.summary.stop_reason = "completed"  # hit MAX_PAGES, not a failure

        return self.manifest.finalize()

    def estimate(self) -> PreviewInfo:
        # Honest "don't know": the real total (~3,650) is rendered
        # client-side from a dataset this scraper has no cheap access to
        # (see the module docstring) — walking all ~365 pages just to
        # count them would defeat the point of a cheap preview. What IS
        # free: how many warning letters this source has captured so far,
        # all-time, read straight from the manifest (no network at all).
        local = compute_source_summary(self.manifest.storage, self.regulation, self.name)
        return PreviewInfo(
            total_available=None,
            already_known=local.documents,
            note=(
                "FDA doesn't expose a cheap total count for this source (the real total "
                "is rendered client-side); already_known is the all-time local count, not "
                "scoped to any particular window — use --max-new-documents to bound this run."
            ),
        )

    @staticmethod
    def _parse_result_rows(html: str) -> list[dict]:
        soup = BeautifulSoup(html, "lxml")
        table = soup.find("table")
        if table is None:
            return []
        rows = []
        body = table.find("tbody") or table
        for tr in body.find_all("tr"):
            cells = tr.find_all("td")
            if not cells:
                continue
            link = tr.find("a", href=True)
            if link is None:
                continue
            title = link.get_text(strip=True)
            href = link["href"]
            if not title or not href:
                continue
            cell_text = [c.get_text(strip=True) for c in cells]
            rows.append(
                {
                    # The href's last path segment already carries a unique
                    # per-letter id + date suffix (e.g.
                    # "thomas-brunner-hygiene-gmbh-729018-07242026") — much
                    # less collision-prone than slugifying the company name,
                    # which repeats across multiple letters to the same firm.
                    "document_id": href.rstrip("/").rsplit("/", 1)[-1],
                    "title": title,
                    "href": href,
                    "posted_date": cell_text[0] if len(cell_text) > 0 else None,
                    "letter_issue_date": cell_text[1] if len(cell_text) > 1 else None,
                    "issuing_office": cell_text[3] if len(cell_text) > 3 else None,
                    "subject": cell_text[4] if len(cell_text) > 4 else None,
                }
            )
        return rows

    def _save_row(self, row: dict) -> None:
        url = row["href"] if row["href"].startswith("http") else f"{BASE_URL}{row['href']}"
        self.fetch_and_save(
            document_id=row["document_id"],
            url=url,
            title=row["title"],
            ext="html",
            content_type="text/html",
            source_metadata={
                "company_name": row["title"],
                "posted_date": row["posted_date"],
                "letter_issue_date": row["letter_issue_date"],
                "issuing_office": row["issuing_office"],
                "subject": row["subject"],
            },
        )
