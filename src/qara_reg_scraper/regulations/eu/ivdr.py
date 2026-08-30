"""Regulation (EU) 2017/746 (IVDR) — consolidated full text.

See mdr.py's module docstring — identical mechanism (EUR-Lex consolidated
CELEX resolution via `eur_lex_consolidated`), applied to the IVDR's own
CELEX instead of the MDR's. Confirmed live, 2026-08-30: current
consolidated version CELEX:02017R0746-20250110.

Note on `recheck_after_days`: same as mdr.py — set
`regulations.eu.sources.ivdr.recheck_after_days` in config.yaml (14) so a
new consolidated version actually gets noticed.
"""

from __future__ import annotations

from ...base_scraper import BaseScraper, PreviewInfo
from ...manifest import RunSummary
from .eur_lex_consolidated import resolve_latest_consolidated

ORIGINAL_CELEX = "32017R0746"
DOCUMENT_ID = "ivdr"


class IvdrScraper(BaseScraper):
    regulation = "eu"
    name = "ivdr"
    description = "Regulation (EU) 2017/746 (IVDR), consolidated full text (EUR-Lex)"
    label = "IVDR"

    def run(self) -> RunSummary:
        try:
            consolidated_celex, url = resolve_latest_consolidated(self.http, ORIGINAL_CELEX)
        except Exception as e:  # noqa: BLE001 - can't proceed at all without this
            self.log.warning(f"ivdr: could not resolve the latest consolidated version: {e}")
            self.manifest.record_error(DOCUMENT_ID, url=None, error=str(e))
            self.manifest.summary.stop_reason = "hard_stop"
            return self.manifest.finalize()

        def fetch_one(document_id: str) -> None:
            self.fetch_and_save(
                document_id=document_id,
                url=url,
                title="Regulation (EU) 2017/746 (IVDR) — consolidated text",
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
        # See mdr.py's own estimate() note - exact and free, no network call.
        known = 1 if self.already_have(DOCUMENT_ID) else 0
        return PreviewInfo(total_available=1, already_known=known)
