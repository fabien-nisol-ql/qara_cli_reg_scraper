"""Regulation (EU) 2017/745 (MDR) — consolidated full text.

Source: EUR-Lex, via `eur_lex_consolidated.resolve_latest_consolidated`
— see that module's docstring for why this needs a discovery step
(finding the current consolidated CELEX id) rather than one fixed URL
the way fda:ecfr's GovInfo feed is.

Stored under a single, fixed document id ("mdr") regardless of which
consolidated version is actually current — same philosophy as
fda:ecfr's single always-current "title-21" document: Manifest.save_document's
own hash-based versioning is what notices a new consolidated version's
text differs from what's stored and archives it, not a per-version
document id. `source_metadata` records exactly which consolidated CELEX
was captured, so that's never ambiguous after the fact.

Note on `recheck_after_days`: the base class default (None) means "once
fetched, skip forever" — wrong here for the same reason it's wrong for
fda:ecfr (see that module's own note). Set
`regulations.eu.sources.mdr.recheck_after_days` in config.yaml (14, same
cadence as eCFR) so a new consolidated version actually gets noticed.
"""

from __future__ import annotations

from ...base_scraper import BaseScraper, PreviewInfo
from ...manifest import RunSummary
from .eur_lex_consolidated import resolve_latest_consolidated

ORIGINAL_CELEX = "32017R0745"
DOCUMENT_ID = "mdr"


class MdrScraper(BaseScraper):
    regulation = "eu"
    name = "mdr"
    description = "Regulation (EU) 2017/745 (MDR), consolidated full text (EUR-Lex)"
    label = "MDR"

    def run(self) -> RunSummary:
        try:
            consolidated_celex, url = resolve_latest_consolidated(self.http, ORIGINAL_CELEX)
        except Exception as e:  # noqa: BLE001 - can't proceed at all without this
            self.log.warning(f"mdr: could not resolve the latest consolidated version: {e}")
            self.manifest.record_error(DOCUMENT_ID, url=None, error=str(e))
            self.manifest.summary.stop_reason = "hard_stop"
            return self.manifest.finalize()

        def fetch_one(document_id: str) -> None:
            self.fetch_and_save(
                document_id=document_id,
                url=url,
                title="Regulation (EU) 2017/745 (MDR) — consolidated text",
                ext="html",
                content_type="text/html",
                source_metadata={
                    "original_celex": ORIGINAL_CELEX,
                    "consolidated_celex": consolidated_celex,
                    "source": "eur_lex",
                },
            )

        self.process_candidates([DOCUMENT_ID], fetch_one)
        return self.manifest.finalize()

    def estimate(self) -> PreviewInfo:
        # Exact and free, same as fda:ecfr: exactly one document, and
        # whether we already have "mdr" at all is a local manifest-sidecar
        # check — doesn't need the /ALL/ discovery fetch just to answer
        # that. recheck_after_days (not this) is what decides whether a
        # new consolidated version gets noticed.
        known = 1 if self.already_have(DOCUMENT_ID) else 0
        return PreviewInfo(total_available=1, already_known=known)
