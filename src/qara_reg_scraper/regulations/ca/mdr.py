"""Medical Devices Regulations (SOR/98-282), full text.

Source: the Justice Laws Website (laws-lois.justice.gc.ca), the Canadian
government's own consolidated-legislation site — the Canadian analog of
GovInfo (fda:ecfr/fdc_act) and EUR-Lex (eu:mdr/ivdr):

    https://laws-lois.justice.gc.ca/eng/regulations/SOR-98-282/FullText.html

Simpler than either of those: unlike eu:mdr's EUR-Lex consolidated-CELEX
dance (a legal act's "current version" lives at a different URL each time
it's amended — see eur_lex_consolidated.py), Justice Laws keeps ONE fixed
URL always current in place — closer to fda:ecfr's GovInfo bulk feed. No
discovery step needed. Confirmed live: 210K chars of text, one GET.

The regulation's own `index.html` (not used here) is a short landing/table-
of-contents page — real content lives at the separate `FullText.html`
sibling, confirmed live while building this.

laws-lois.justice.gc.ca has no robots.txt at all (confirmed live: the
`/robots.txt` path itself 404s, a real 404 page, not an empty/missing
file being silently allowed) — no declared restrictions.

Note on `recheck_after_days`: same rationale as fda:ecfr's own note — the
base class default (None) means "once fetched, skip forever," which
would mean an amendment to the Medical Devices Regulations is never
noticed after the first fetch.
"""

from __future__ import annotations

from ...base_scraper import BaseScraper, PreviewInfo
from ...manifest import RunSummary

FULL_TEXT_URL = "https://laws-lois.justice.gc.ca/eng/regulations/SOR-98-282/FullText.html"
DOCUMENT_ID = "mdr"


class MdrScraper(BaseScraper):
    regulation = "ca"
    name = "mdr"
    description = "Medical Devices Regulations (SOR/98-282), full text (Justice Laws Website)"
    label = "Medical Devices Regulations"

    def run(self) -> RunSummary:
        def fetch_one(document_id: str) -> None:
            self.fetch_and_save(
                document_id=document_id,
                url=FULL_TEXT_URL,
                title="Medical Devices Regulations (SOR/98-282) — full text",
                ext="html",
                content_type="text/html",
                source_metadata={"instrument_number": "SOR/98-282", "source": "justice_laws"},
            )

        self.process_candidates([DOCUMENT_ID], fetch_one)
        return self.manifest.finalize()

    def estimate(self) -> PreviewInfo:
        # Exact and free, same as fda:ecfr: exactly one document, and
        # already_have is a local manifest-sidecar check, no network call.
        known = 1 if self.already_have(DOCUMENT_ID) else 0
        return PreviewInfo(total_available=1, already_known=known)
