"""The Federal Food, Drug, and Cosmetic Act (FD&C Act), full text — the
statutory basis every other FDA source in this package implements or
interprets: 21 CFR Parts 862-892 (ecfr.py) are FDA's *regulations* under
this Act, and every FDA guidance document (guidance.py) is FDA's own
*interpretation* of it. Nothing else in this package scrapes the statute
itself — this closes that gap. See docs/sources/fda/fdc_act.md.

Source: GovInfo's "Statute Compilations" (COMPS) collection — package
COMPS-973 — https://www.govinfo.gov/app/details/COMPS-973, confirmed live
while building this. NOT GovInfo's bulkdata service: that repository
(https://www.govinfo.gov/bulkdata) does not carry a USCODE collection at
all (confirmed directly — the only Title-21-adjacent collections it lists
are CFR/ECFR, both regulations, not statute) — a bare U.S. Code bulk feed
doesn't exist there. `uscode.house.gov` (the Office of the Law Revision
Counsel's own site, the other candidate) was unreachable to probe from
this environment.

COMPS is the better fit anyway, not just the available one: it's OLRC's
own *positive-law-style compilation of one specific act, consolidated and
kept current* — "Federal Food, Drug, and Cosmetic Act [As Amended Through
P.L. ...]" — rather than the full, much broader Title 21 U.S. Code (which
also covers unrelated things folded into that title over the decades).
That's a closer match to "the FD&C Act" as discussed in the regulatory
chain (Sections 513/513(g)/510(k)) than a raw whole-title dump would be.

Found via GovInfo's own search (not URL-guessing): search
`collection:COMPS "Federal Food, Drug, and Cosmetic Act"` at
https://www.govinfo.gov/app/search/ resolves to package COMPS-973; the
package's own Content Details page lists the direct content URLs used
below. GovInfo re-publishes the SAME package id in place as the
compilation is re-amended (confirmed: the page's own "Amended Through"
note already reflects a public law enacted after this file was written) —
so `recheck_after_days` (see config.yaml) is what notices a new
amendment, exactly like ecfr.py's own rationale.

Fetches the USLM XML rendition (a structured, section-addressable format,
GPO's modern successor to plain HTML/PDF for this content) rather than the
PDF — smaller (6.1MB vs the PDF's own layout-heavy encoding at similar
size) and machine-parseable if per-section extraction is ever wanted, the
same tradeoff ecfr.py already made for CFR text.

MAINTENANCE NOTE: if COMPS-973 ever 404s, GovInfo has re-numbered the
package (has not been observed, but COMPS package ids are assigned
per-compilation, not derived from a stable slug) — re-run the search
above and update COMPS_PACKAGE_ID.
"""

from __future__ import annotations

from ...base_scraper import BaseScraper, PreviewInfo
from ...manifest import RunSummary

COMPS_PACKAGE_ID = "COMPS-973"
GOVINFO_URL = f"https://www.govinfo.gov/content/pkg/{COMPS_PACKAGE_ID}/uslm/{COMPS_PACKAGE_ID}.xml"
DOCUMENT_ID = "fdc-act"


class FdcActScraper(BaseScraper):
    regulation = "fda"
    name = "fdc_act"
    description = "Federal Food, Drug, and Cosmetic Act, full text (GovInfo Statute Compilations, USLM XML)"
    label = "FD&C Act"

    def run(self) -> RunSummary:
        def fetch_one(document_id: str) -> None:
            self.fetch_and_save(
                document_id=document_id,
                url=GOVINFO_URL,
                title="Federal Food, Drug, and Cosmetic Act — full text",
                ext="xml",
                content_type="application/xml",
                source_metadata={
                    "title": "Federal Food, Drug, and Cosmetic Act",
                    "source": "govinfo_comps",
                    "package_id": COMPS_PACKAGE_ID,
                },
            )

        self.process_candidates([DOCUMENT_ID], fetch_one)
        return self.manifest.finalize()

    def estimate(self) -> PreviewInfo:
        # Exact and free: exactly one document, already_have is a local
        # manifest-sidecar check, no network call at all.
        known = 1 if self.already_have(DOCUMENT_ID) else 0
        return PreviewInfo(total_available=1, already_known=known)
