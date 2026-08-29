"""eCFR — Title 21 CFR, full text.

Source: GovInfo's bulk data feed for the Electronic Code of Federal
Regulations (https://www.govinfo.gov/bulkdata/ECFR), maintained by the
National Archives / GPO. This is the actual regulatory text — the reason
this whole tool exists.

We used to fetch this per part, directly from eCFR's own versioner API
(https://www.ecfr.gov/api/versioner). That API is no longer usable here:
eCFR's robots.txt disallows `/api/versioner/v1/full/`, and PoliteHttpClient
respects robots.txt by default, so every one of those per-part fetches now
raises RobotsDisallowed -> HardStop before a single document is saved.
GovInfo publishes the same content as one bulk XML file per title, and its
robots.txt does not disallow that path.

This source now fetches all of Title 21 as a *single* document
("title-21") — deliberately no per-part or per-section parsing, and no
diffing below whole-document granularity. That's a real granularity change
from before: previously ~35 independently-versioned part-documents, each
only re-versioned when that specific part changed; now one ~20MB document
that gets a new archived version (see Manifest.save_document) whenever
*anything* in Title 21 changes — not just the device-relevant parts, since
Title 21 also covers drugs, food, cosmetics, tobacco, biologics, etc. With
`recheck_after_days` set, expect re-checks to fairly often see this as
"updated", each one archiving a fresh ~20MB copy. Parsing the bulk file back
into per-part or per-section documents (to restore fine-grained diffing and
reduce that storage growth) is a natural follow-up, not done here.

Old on-disk `data/fda/ecfr/documents/part-*/` directories from the previous
per-part scraper go stale after this change and are intentionally not
cleaned up here.

Note on `recheck_after_days`: the base class default (None) means "once
fetched, skip forever" — wrong for this source, since the whole point is
noticing regulatory text changes over time. Set
`regulations.fda.sources.ecfr.recheck_after_days` in config.yaml (already
set to 14) so this actually gets re-verified periodically.
"""

from __future__ import annotations

from ...base_scraper import BaseScraper, PreviewInfo
from ...manifest import RunSummary

GOVINFO_URL = "https://www.govinfo.gov/bulkdata/ECFR/title-21/ECFR-title21.xml"
TITLE = 21
DOCUMENT_ID = "title-21"


class EcfrScraper(BaseScraper):
    regulation = "fda"
    name = "ecfr"
    description = "21 CFR Title 21, full text (GovInfo bulk XML)"
    label = "eCFR"

    def run(self) -> RunSummary:
        def fetch_one(document_id: str) -> None:
            self.fetch_and_save(
                document_id=document_id,
                url=GOVINFO_URL,
                title=f"21 CFR (Title {TITLE}) — full text",
                ext="xml",
                content_type="application/xml",
                source_metadata={"title": TITLE, "source": "govinfo_bulkdata"},
            )

        self.process_candidates([DOCUMENT_ID], fetch_one)
        return self.manifest.finalize()

    def estimate(self) -> PreviewInfo:
        # Exact and free: exactly one document, already_have is a local
        # manifest-sidecar check, no network call at all.
        known = 1 if self.already_have(DOCUMENT_ID) else 0
        return PreviewInfo(total_available=1, already_known=known)
