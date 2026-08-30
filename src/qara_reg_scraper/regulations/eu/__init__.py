"""EU — European Union medical device regulatory sources. See the parent
package's docstring (`regulations/__init__.py`) for how this fits into the
overall regulation registry, and for the pattern to follow when adding
another regulation.
"""

from __future__ import annotations

from ...base_scraper import BaseScraper
from .ivdr import IvdrScraper
from .mdcg_guidance import MdcgGuidanceScraper
from .mdr import MdrScraper

#: source name -> class, for this regulation only. Registered as
#: REGULATION_REGISTRY["eu"] in regulations/__init__.py.
EU_SOURCES: dict[str, type[BaseScraper]] = {
    MdrScraper.name: MdrScraper,
    IvdrScraper.name: IvdrScraper,
    MdcgGuidanceScraper.name: MdcgGuidanceScraper,
}

__all__ = ["EU_SOURCES", "BaseScraper"]
