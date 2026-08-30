"""Shared helper for EU regulation sources backed by EUR-Lex's
*consolidated* legal-text mechanism (eu:mdr, eu:ivdr).

Unlike a single fixed URL that's always "the current text" (see
fda:ecfr's GovInfo bulk feed), a legal act's consolidated text on EUR-Lex
gets a new date-suffixed CELEX id each time an amendment is folded in —
e.g. the MDR's own CELEX (32017R0745) never changes, but its consolidated
versions are separate documents: CELEX 02017R0745-20170505,
...-20230311, ...-20260719 (confirmed live, 2026-08-30 — 8 versions
since 2017). There's no fixed "current" URL the way GovInfo's is for
eCFR.

EUR-Lex's own "/ALL/" view for a legal act's original CELEX lists every
consolidated version and — confirmed live — explicitly labels the current
one: an `<a>` whose text starts with "Access current version". That's the
primary signal used here, not date-sorting done ourselves. Falls back to
the lexicographically-max date-suffixed CELEX found on the page (the
suffix is plain YYYYMMDD, which sorts correctly as a string) only if that
label is ever missing — defensive, not the normal path.

eur-lex.europa.eu/robots.txt (checked in full) only disallows the
/legal-content/<LANG>/TXT/DOC/ and /TXT/SIG/ per-language export
sub-paths, plus publishes `Crawl-delay: 10` — robots_policy.py already
applies that automatically to any source talking to this host, no
special-casing needed here or in mdr.py/ivdr.py.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from ...http_client import PoliteHttpClient

BASE_URL = "https://eur-lex.europa.eu"

_CURRENT_LABEL_RE = re.compile(r"^Access current version", re.IGNORECASE)


def resolve_latest_consolidated(http: PoliteHttpClient, original_celex: str) -> tuple[str, str]:
    """`original_celex`: the legal act's own, never-changing CELEX id
    (e.g. "32017R0745" for the MDR — sector digit 3, "legislation in
    force"). Returns `(consolidated_celex, text_url)` for whichever
    consolidated version EUR-Lex currently considers current — a
    consolidated CELEX replaces that leading sector digit with "0" and
    appends "-<YYYYMMDD>" (e.g. "02017R0745-20260719").

    Raises ValueError if the /ALL/ page has no consolidated version at
    all — would mean the act was never consolidated, or EUR-Lex changed
    this page's structure; either way the caller should treat it as this
    source's own error (hard stop), not a routine miss."""
    all_url = f"{BASE_URL}/legal-content/EN/ALL/?uri=CELEX:{original_celex}"
    response = http.get(all_url)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "lxml")

    consolidated_prefix = "0" + original_celex[1:]
    consolidated_re = re.compile(rf"CELEX:({re.escape(consolidated_prefix)}-\d{{8}})")

    candidates: list[str] = []
    for a in soup.find_all("a", href=True):
        match = consolidated_re.search(a["href"])
        if match is None:
            continue
        consolidated_celex = match.group(1)
        candidates.append(consolidated_celex)
        if _CURRENT_LABEL_RE.match(a.get_text(strip=True)):
            return consolidated_celex, _text_url(consolidated_celex)

    if not candidates:
        raise ValueError(f"no consolidated version found for CELEX:{original_celex} at {all_url}")
    # Defensive fallback only - the explicit "Access current version"
    # label above is expected to match first every time this page's
    # structure hasn't changed.
    latest = max(candidates, key=lambda c: c.rsplit("-", 1)[-1])
    return latest, _text_url(latest)


def _text_url(consolidated_celex: str) -> str:
    return f"{BASE_URL}/legal-content/EN/TXT/?uri=CELEX:{consolidated_celex}"
