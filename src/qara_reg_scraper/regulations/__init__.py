"""Regulation registry: every scraper this tool knows how to run, grouped
by regulation namespace and addressed as ``<regulation>:<name>`` everywhere
— CLI `--source`, config.yaml, storage/manifest paths, and DB rows all use
the same string.

**Adding a new regulation** (EU IVDR, Turkish/Chinese device regs, ...) is
exactly:

1. Create `regulations/<code>/` (e.g. `regulations/eu/`).
2. Write one or more `BaseScraper` subclasses there, each setting
   `regulation = "<code>"` and its own `name` — see `regulations/fda/` for
   a full worked example covering five different source shapes (a fixed
   list, two paginated HTML scrapes, two openFDA-JSON-listing-backed
   sources with a shared per-document fetch).
3. Export them as a `dict[str, type[BaseScraper]]` from that package's
   `__init__.py` (see `regulations/fda/__init__.py`).
4. Add one line to `REGULATION_REGISTRY` below.

Nothing else in the codebase needs to change — config, storage, the
database, and the CLI are all regulation-agnostic.
"""

from __future__ import annotations

from ..base_scraper import BaseScraper
from .ca import CA_SOURCES
from .eu import EU_SOURCES
from .fda import FDA_SOURCES

#: regulation code -> {source name -> scraper class}
REGULATION_REGISTRY: dict[str, dict[str, type[BaseScraper]]] = {
    "fda": FDA_SOURCES,
    "eu": EU_SOURCES,
    "ca": CA_SOURCES,
}


class UnknownSource(Exception):
    pass


def resolve(qualified_name: str) -> type[BaseScraper]:
    """"regulation:name" -> scraper class, or raise UnknownSource with a
    message listing what's actually registered."""
    regulation, _, name = qualified_name.partition(":")
    sources = REGULATION_REGISTRY.get(regulation)
    if sources is None or not name or name not in sources:
        raise UnknownSource(
            f"Unknown source {qualified_name!r}. Known: {', '.join(all_qualified_names())}"
        )
    return sources[name]


def all_qualified_names() -> list[str]:
    return [
        f"{regulation}:{name}"
        for regulation, sources in REGULATION_REGISTRY.items()
        for name in sources
    ]


def qualified_names_for_regulation(regulation: str) -> list[str]:
    return [f"{regulation}:{name}" for name in REGULATION_REGISTRY.get(regulation, {})]


__all__ = [
    "REGULATION_REGISTRY",
    "UnknownSource",
    "all_qualified_names",
    "qualified_names_for_regulation",
    "resolve",
]
