"""Health Canada guidance documents for medical devices.

Source: Health Canada's own guidance-documents listing page —
confirmed live while building this: a real, server-rendered page (no
JavaScript needed, unlike fda:guidance's dataset), 81 links inside its
main content region (`property="mainContentOfPage"`, the WET/GCWeb
theme's own marker for "this is the actual content, not chrome" — every
Government of Canada site built on that framework carries it):

    https://www.canada.ca/en/health-canada/services/drugs-health-products/medical-devices/application-information/guidance-documents.html

Unlike eu:mdcg_guidance's uniform table (Reference/Title/Publication
columns, every row a document-store PDF link), this page is a loosely
curated content list, not a formal table: most links are guidance
documents proper ("Guidance document: ...", "Guidance on ...", "Notice:
..."), each its own canada.ca HTML article — but a handful are related-
but-distinct content Health Canada chose to place on the same page (an
e-learning tool, ISO 13485 quality-systems info, MDSAP program info
including one link straight to `mdsap.global`, a device-licence fee
schedule). Rather than try to classify "true guidance" vs. "related
content" from title text alone (fragile — Health Canada's own titling
isn't consistent enough to draw that line reliably), this captures
everything in the main content region, same philosophy as
mdcg_guidance.py's own "capture the real page, don't assume a narrower
shape" approach to its own edge cases.

All 81 links confirmed unique (no duplicate hrefs). Document ids come
from each URL's own path slug (every canada.ca page ends in a clean
`....html` segment) — except the one non-HTML, query-string link (the
e-learning tool), which falls back to a hash of the full URL, same
"prefer a natural id, hash fallback for anything unusual" pattern as
mdcg_guidance.py's own `_document_id`.

`www.canada.ca/robots.txt` (checked in full, 51 lines) has nothing
relevant disallowed — its rules are CRA/IRCC-specific, unrelated to this
path.

Single page, no pagination, no per-document detail fetch beyond the
linked page itself — closest in shape to fda:warning_letters (a real,
server-rendered listing) among the FDA sources.

**A network-level failure on an EXTERNAL link (not www.canada.ca) is a
routine per-document miss, not a HardStop** — see `run()`'s own
`fetch_one`. Unlike every other source in this tool, this page's
documents span several independent hosts (canada.ca itself, plus the
MDSAP program's own site and a PHAC e-learning tool), so a network
failure on one of those external hosts says nothing about canada.ca's
own health — confirmed live, 2026-08-30: `www.mdsap.global` timed out or
dropped the connection on every automatic retry attempt for hours
straight, which (before this was handled) repeatedly stopped the entire
run before it ever reached the other ~80 genuinely Health-Canada-hosted
documents. A failure on www.canada.ca itself still stops the run as
usual — that IS real signal.
"""

from __future__ import annotations

import hashlib
import logging
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from ...base_scraper import BaseScraper, HardStop, PreviewInfo
from ...logging_setup import log_extra
from ...manifest import RunSummary

BASE_URL = "https://www.canada.ca"
LISTING_PATH = (
    "/en/health-canada/services/drugs-health-products/medical-devices"
    "/application-information/guidance-documents.html"
)
LISTING_URL = f"{BASE_URL}{LISTING_PATH}"


class GuidanceScraper(BaseScraper):
    regulation = "ca"
    name = "guidance"
    description = "Health Canada guidance documents for medical devices (single listing page)"
    label = "Guidance Documents"

    def run(self) -> RunSummary:
        try:
            by_id = self._discover()
        except Exception as e:  # noqa: BLE001 - can't proceed at all without this
            self.log.warning(f"guidance: could not fetch/parse the listing page: {e}")
            self.manifest.record_error("__listing__", url=LISTING_URL, error=str(e))
            self.manifest.summary.stop_reason = "hard_stop"
            return self.manifest.finalize()

        self.log.info(f"guidance: {len(by_id)} links on the listing page")

        def fetch_one(document_id: str) -> None:
            entry = by_id[document_id]
            try:
                self.fetch_and_save(
                    document_id=document_id,
                    url=entry["url"],
                    title=entry["title"],
                    ext="html",
                    content_type="text/html",
                    source_metadata={"link_text": entry["title"]},
                )
            except HardStop as e:
                if urlparse(entry["url"]).netloc == urlparse(BASE_URL).netloc:
                    raise  # a canada.ca fetch failing this way IS real signal - propagate as usual
                # An external reference link (MDSAP's own site, the PHAC
                # e-learning tool, ...) failing at the network level says
                # nothing about canada.ca's own health - unlike every
                # other source here, this one's documents span several
                # independent hosts, so treating any one of them as
                # "stop the whole run" would let one broken third-party
                # site indefinitely block the other ~80 genuinely
                # Health-Canada-hosted documents. Confirmed live,
                # 2026-08-30: www.mdsap.global timed out/dropped the
                # connection on every automatic retry for hours straight
                # - fetch_and_save already recorded the per-document
                # error before raising, so nothing more to do here except
                # not let it end the run.
                log_extra(
                    self.log, logging.WARNING, "external_link_fetch_failed_not_fatal",
                    document_id=document_id, url=entry["url"], error=str(e),
                )

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
        see."""
        response = self.http.get(LISTING_URL)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "lxml")

        main = soup.find(attrs={"property": "mainContentOfPage"})
        if main is None:
            raise ValueError("could not find the main content region (property=mainContentOfPage) on the page")

        by_id: dict[str, dict] = {}
        for a in main.find_all("a", href=True):
            href = a["href"]
            title = a.get_text(strip=True)
            if not href or not title:
                continue
            url = href if href.startswith("http") else f"{BASE_URL}{href}"
            by_id[self._document_id(url)] = {"url": url, "title": title}
        return by_id

    @staticmethod
    def _document_id(url: str) -> str:
        parsed = urlparse(url)
        if not parsed.query:
            slug = parsed.path.rstrip("/").rsplit("/", 1)[-1]
            if slug:
                return slug
            # A bare domain root with no path at all (e.g. the MDSAP
            # link, confirmed live: "https://www.mdsap.global/") - the
            # domain itself is still a perfectly readable, stable id.
            if parsed.netloc:
                return parsed.netloc
        # The one non-HTML, query-string link (an external e-learning
        # tool) - no clean path slug or bare domain to key off, so hash
        # the full URL. Stable and unique, not pretty.
        return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
