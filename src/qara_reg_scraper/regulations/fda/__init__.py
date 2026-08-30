"""FDA — U.S. medical device regulatory sources. See the parent package's
docstring (`regulations/__init__.py`) for how this fits into the overall
regulation registry, and for the pattern to follow when adding another
regulation.
"""

from __future__ import annotations

from ...base_scraper import BaseScraper
from .classification import ClassificationScraper
from .clearances_510k import Clearances510kScraper
from .ecfr import EcfrScraper
from .fdc_act import FdcActScraper
from .guidance import GuidanceScraper
from .hde import HdeScraper
from .pma import PmaScraper
from .recalls import RecallsScraper
from .warning_letters import WarningLettersScraper

#: source name -> class, for this regulation only. Registered as
#: REGULATION_REGISTRY["fda"] in regulations/__init__.py.
FDA_SOURCES: dict[str, type[BaseScraper]] = {
    EcfrScraper.name: EcfrScraper,
    GuidanceScraper.name: GuidanceScraper,
    Clearances510kScraper.name: Clearances510kScraper,
    WarningLettersScraper.name: WarningLettersScraper,
    RecallsScraper.name: RecallsScraper,
    ClassificationScraper.name: ClassificationScraper,
    FdcActScraper.name: FdcActScraper,
    PmaScraper.name: PmaScraper,
    HdeScraper.name: HdeScraper,
}

__all__ = ["FDA_SOURCES", "BaseScraper"]
