"""Humanitarian Device Exemption (HDE) approvals — fda.gov's own listing
page, plus the actual FDA approval order letter for each one.

https://www.fda.gov/medical-devices/hde-approvals/listing-cdrh-humanitarian-device-exemptions

Unlike 510(k)/PMA, openFDA has no HDE endpoint at all (confirmed against
open.fda.gov's own API index) - this is fda.gov's own listing page
instead. Its "Show 10/25/.../All entries" control is DataTables-driven,
but - unlike guidance.py's search page - the *entire* dataset is embedded
directly in the plain server-rendered HTML on first load, not fetched via
a separate AJAX/JSON call (confirmed live: a single unauthenticated GET
with no JS execution returns all ~89 rows, including the oldest); the
"Show N entries" control just paginates that already-fully-delivered
table client-side. So, like classification.py, this is a full-catalog
walk every run rather than a lookback window - the dataset is small
(under 100 total, confirmed live) and grows only a few times a year, and
content-hashing in Manifest.save_document makes repeat full walks cheap.

Two documents per approval, same shape as clearances_510k.py/pma.py: the
listing row IS the free metadata (arrives with the one already-fetched
page, no extra request) - the actual approval order letter is a separate
PDF at a predictable accessdata.fda.gov URL, confirmed live via the
individual HDE detail page (accessdata.fda.gov/.../hde.cfm?id=<internal
id>, itself only reachable through this listing page's own per-row
links - there's no public search/listing endpoint on accessdata.fda.gov
itself worth scraping separately):

    https://www.accessdata.fda.gov/cdrh_docs/pdf<yy>/<hde_number>A.pdf

where <yy> is the two digits embedded in the HDE number itself (e.g.
"H200002" -> "20") - the exact same convention confirmed for 510(k)
summary PDFs and PMA approval orders (see pma.py's module docstring).
`fetch_and_save` is used as-is (same reasoning as pma.py) since accessdata
sits behind the same Akamai bot management as those other two sources -
its bot-block detection applies here without any HDE-specific code.

A handful of very old HDE records (confirmed live: two, formally
withdrawn per FDA's own note on the page) have no detail-page link at
all - their listing row carries the HDE number as plain text instead of a
link. Known in advance from the row itself (not a blind fetch-and-see),
so those get a `not_applicable` order sentinel before ever attempting a
fetch. For a row that DOES have a detail-page link, whether the order
letter itself was actually posted still isn't knowable in advance
(confirmed live for pma.py: roughly half of PMA supplements have none,
despite each one having its own detail/metadata page — no reason to
assume HDE never has the same gap) - so `_fetch_order` hand-rolls the
same bot-block-aware fetch as pma.py's own `_fetch_order`/
clearances_510k.py's `_fetch_summary_pdf`, persisting a `not_applicable`
sentinel via `save_document` on any routine (non-block) miss too, rather
than a bare `record_error` that `already_have` would never recognize -
see pma.py's module docstring for why that distinction matters.
"""

from __future__ import annotations

import json
import logging
import re

from bs4 import BeautifulSoup

from ...base_scraper import BaseScraper, BotBlockDetected, BudgetExhausted, HardStop, PreviewInfo
from ...http_client import looks_like_bot_block
from ...logging_setup import log_extra
from ...manifest import RunSummary

LISTING_URL = "https://www.fda.gov/medical-devices/hde-approvals/listing-cdrh-humanitarian-device-exemptions"
HDE_NUMBER_RE = re.compile(r"H\d{6}")
# Where the approval order letters actually live (not the listing page's
# own host, www.fda.gov) — see clearances_510k.py's own ACCESSDATA_ORIGIN
# comment. This is the one that carries Visiting-hours.
ACCESSDATA_ORIGIN = "https://www.accessdata.fda.gov"


class HdeScraper(BaseScraper):
    regulation = "fda"
    name = "hde"
    description = "FDA Humanitarian Device Exemption (HDE) approvals + approval order letters"
    label = "HDE Approvals"

    def run(self) -> RunSummary:
        self.log.info("hde: walking the full HDE approvals listing")

        try:
            response = self.http.get(LISTING_URL)
            response.raise_for_status()
            rows = _parse_rows(response.text)
        except Exception as e:  # noqa: BLE001 - the listing call itself failed after retries
            self.log.warning(f"hde: listing call failed, stopping run early: {e}")
            self.manifest.record_error("__listing__", url=LISTING_URL, error=str(e))
            self.manifest.summary.stop_reason = "hard_stop"
            return self.manifest.finalize()

        for row in rows:
            hde_number = row["hde_number"]
            if not self.already_have(f"{hde_number}/metadata"):
                self._save_metadata(row)

            if self.already_have(f"{hde_number}/order"):
                self.manifest.summary.skipped_already_known += 1
                continue
            try:
                self._fetch_order(row)
            except BudgetExhausted:
                self.log.info(f"hde: budget reached ({self.max_new_documents} new order letters)")
                self.manifest.summary.stop_reason = "budget_reached"
                return self.manifest.finalize()
            except HardStop as e:
                self.log.warning(f"hde: stopping run early: {e}")
                self.manifest.summary.stop_reason = "bot_block" if isinstance(e, BotBlockDetected) else "hard_stop"
                return self.manifest.finalize()
        self.manifest.summary.stop_reason = "completed"

        return self.manifest.finalize()

    def estimate(self) -> PreviewInfo:
        try:
            response = self.http.get(LISTING_URL)
            response.raise_for_status()
            rows = _parse_rows(response.text)
        except Exception as e:  # noqa: BLE001
            return PreviewInfo(total_available=None, already_known=None, note=f"could not fetch listing: {e}")
        # "Already known" means the order letter (the actually
        # network-costly part) is already captured - not just the free
        # metadata, same convention as clearances_510k.py/pma.py.
        known = sum(1 for row in rows if self.already_have(f"{row['hde_number']}/order"))
        return PreviewInfo(
            total_available=len(rows), already_known=known,
            next_available_at=self.http.next_available_at(ACCESSDATA_ORIGIN),
            next_available_note=self.http.visiting_hours_description(ACCESSDATA_ORIGIN),
        )

    def _save_metadata(self, row: dict) -> None:
        content = json.dumps(row, indent=2, sort_keys=True).encode("utf-8")
        self.manifest.save_document(
            f"{row['hde_number']}/metadata",
            content,
            url=LISTING_URL,
            title=row["device_name"],
            ext="json",
            content_type="application/json",
            http_status=200,
            source_metadata={
                "hde_number": row["hde_number"],
                "approval_date": row["approval_date"],
                "company": row["company"],
            },
        )

    def _fetch_order(self, row: dict) -> None:
        """Raises BudgetExhausted (once the cap is hit) or HardStop (only
        for a suspected bot-management block or a network/retry failure).
        A withdrawn record with no detail-page link (known in advance
        from the listing row itself) gets a not_applicable sentinel
        before ever attempting a fetch; a linked row whose order letter
        still turns out missing (a routine, non-block miss) gets the same
        sentinel too, just discovered empirically instead — see the
        module docstring for why that matters here, not just for
        withdrawn rows."""
        hde_number = row["hde_number"]
        document_id = f"{hde_number}/order"
        if not row["has_detail_page"]:
            self._save_not_applicable(hde_number, reason="no detail page listed")
            return

        if self._budget_exhausted():
            raise BudgetExhausted()

        year_digits = hde_number[1:3]
        url = f"https://www.accessdata.fda.gov/cdrh_docs/pdf{year_digits}/{hde_number}A.pdf"
        try:
            response = self.http.get(url)
        except Exception as e:
            log_extra(
                self.log, logging.WARNING, "order_letter_fetch_failed",
                hde_number=hde_number, url=url, error=str(e),
            )
            self.manifest.record_error(document_id, url=url, error=str(e))
            raise HardStop(str(e)) from e

        # Checked regardless of status code - same reasoning as
        # clearances_510k.py's/pma.py's own maintenance notes: Akamai's
        # apology page has been observed served with a 404 status, not
        # just 200/403.
        if looks_like_bot_block(response):
            content_type = response.headers.get("Content-Type", "")
            error = f"likely bot-management block (status={response.status_code}, content-type={content_type!r})"
            log_extra(
                self.log, logging.WARNING, "bot_detection_suspected",
                hde_number=hde_number, url=url, status=response.status_code, content_type=content_type,
            )
            self.manifest.record_error(document_id, url=url, error=error)
            raise BotBlockDetected(error)

        content_type = response.headers.get("Content-Type", "")
        if response.status_code != 200 or "pdf" not in content_type.lower():
            log_extra(
                self.log, logging.INFO, "order_letter_not_found",
                hde_number=hde_number, url=url, status=response.status_code, content_type=content_type,
            )
            self._save_not_applicable(hde_number, reason=f"no order letter found (status={response.status_code})")
            return

        self.manifest.save_document(
            document_id,
            response.content,
            url=url,
            title=f"HDE Approval Order — {row['device_name']}",
            ext="pdf",
            content_type="application/pdf",
            http_status=response.status_code,
            source_metadata={"hde_number": hde_number},
        )
        self._consume_budget()

    def _save_not_applicable(self, hde_number: str, *, reason: str) -> None:
        self.manifest.save_document(
            f"{hde_number}/order",
            b'{"not_applicable": true}',
            url=LISTING_URL,
            title=f"HDE Approval Order — not applicable ({hde_number})",
            ext="json",
            content_type="application/json",
            http_status=200,
            source_metadata={"hde_number": hde_number, "not_applicable": True, "reason": reason},
        )


def _parse_rows(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table")
    if table is None:
        return []
    body = table.find("tbody") or table
    rows = []
    for tr in body.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 5:
            continue
        # The HDE number normally lives in cell[1] alone ("Hxxxxxx" plus a
        # link); a handful of old withdrawn records instead duplicate
        # cell[0]'s text into cell[1] (confirmed live) - searching both
        # cells for the H###### pattern handles either shape without
        # assuming which cell it's actually in.
        match = HDE_NUMBER_RE.search(cells[1].get_text(" ", strip=True)) or HDE_NUMBER_RE.search(
            cells[0].get_text(" ", strip=True)
        )
        if not match:
            continue
        link = cells[1].find("a", href=True)
        rows.append(
            {
                "hde_number": match.group(0),
                "approval_date": cells[0].get_text(" ", strip=True),
                "device_name": cells[2].get_text(" ", strip=True),
                "company": cells[3].get_text(" ", strip=True),
                "description": cells[4].get_text(" ", strip=True),
                "has_detail_page": link is not None,
            }
        )
    return rows
