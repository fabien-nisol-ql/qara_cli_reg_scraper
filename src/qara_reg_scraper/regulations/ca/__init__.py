"""CA — Health Canada / Santé Canada medical device regulatory sources.
See the parent package's docstring (`regulations/__init__.py`) for how
this fits into the overall regulation registry, and for the pattern to
follow when adding another regulation.
"""

from __future__ import annotations

from ...base_scraper import BaseScraper
from .food_and_drugs_act import FoodAndDrugsActScraper
from .guidance import GuidanceScraper
from .mdall import MdallScraper
from .mdr import MdrScraper
from .recalls import RecallsScraper

#: source name -> class, for this regulation only. Registered as
#: REGULATION_REGISTRY["ca"] in regulations/__init__.py.
CA_SOURCES: dict[str, type[BaseScraper]] = {
    MdrScraper.name: MdrScraper,
    FoodAndDrugsActScraper.name: FoodAndDrugsActScraper,
    MdallScraper.name: MdallScraper,
    RecallsScraper.name: RecallsScraper,
    GuidanceScraper.name: GuidanceScraper,
}

__all__ = ["CA_SOURCES", "BaseScraper"]
