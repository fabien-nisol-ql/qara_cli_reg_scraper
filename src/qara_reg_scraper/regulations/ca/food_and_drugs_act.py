"""Food and Drugs Act (R.S.C., 1985, c. F-27), full text — the statutory
basis ca:mdr is made under, analogous to fda:fdc_act's relationship to
fda:ecfr.

Source: same Justice Laws mechanism as mdr.py — see that module's
docstring for the "single fixed URL, no discovery step" rationale and the
robots.txt finding (no file at all on this host, confirmed live).

    https://laws-lois.justice.gc.ca/eng/acts/f-27/FullText.html

Confirmed live while building this.

Note on `recheck_after_days`: same rationale as ca:mdr's own note.
"""

from __future__ import annotations

from ...base_scraper import BaseScraper, PreviewInfo
from ...manifest import RunSummary

FULL_TEXT_URL = "https://laws-lois.justice.gc.ca/eng/acts/f-27/FullText.html"
DOCUMENT_ID = "food-and-drugs-act"


class FoodAndDrugsActScraper(BaseScraper):
    regulation = "ca"
    name = "food_and_drugs_act"
    description = "Food and Drugs Act (R.S.C., 1985, c. F-27), full text (Justice Laws Website)"
    label = "Food and Drugs Act"

    def run(self) -> RunSummary:
        def fetch_one(document_id: str) -> None:
            self.fetch_and_save(
                document_id=document_id,
                url=FULL_TEXT_URL,
                title="Food and Drugs Act (R.S.C., 1985, c. F-27) — full text",
                ext="html",
                content_type="text/html",
                source_metadata={"citation": "R.S.C., 1985, c. F-27", "source": "justice_laws"},
            )

        self.process_candidates([DOCUMENT_ID], fetch_one)
        return self.manifest.finalize()

    def estimate(self) -> PreviewInfo:
        # See mdr.py's own estimate() note - exact and free, no network call.
        known = 1 if self.already_have(DOCUMENT_ID) else 0
        return PreviewInfo(total_available=1, already_known=known)
