"""Shared helper for the two sources backed by the official openFDA JSON
API (device 510(k)/De Novo clearances, device enforcement/recalls).

openFDA is FDA's own structured, documented API — https://open.fda.gov —
rather than an HTML page, so "scraping" it means polite, paginated,
rate-limited JSON fetches through the same PoliteHttpClient as every other
source (same User-Agent, same throttling, same retry/backoff).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from ...http_client import PoliteHttpClient

OPENFDA_PAGE_LIMIT = 100  # openFDA's per-request max


def iter_openfda_results(
    http: PoliteHttpClient,
    endpoint: str,
    search: str,
    *,
    sort: str,
    max_records: int = 5000,
) -> Iterator[dict[str, Any]]:
    """Yield openFDA result records for `search`, paginating with `skip`
    until exhausted or `max_records` is reached. openFDA caps deep paging
    (skip beyond ~25000) — max_records keeps a daily job from ever trying
    to walk the full historical archive."""
    skip = 0
    fetched = 0
    while fetched < max_records:
        limit = min(OPENFDA_PAGE_LIMIT, max_records - fetched)
        params = {"search": search, "sort": sort, "limit": limit, "skip": skip}
        response = http.get(endpoint, params=params)
        if response.status_code == 404:
            # openFDA returns 404 (not an empty array) once `skip` runs
            # past the end of the result set — that's the natural stop
            # condition, not an error.
            return
        response.raise_for_status()
        payload = response.json()
        results = payload.get("results", [])
        if not results:
            return
        for record in results:
            yield record
            fetched += 1
            if fetched >= max_records:
                return
        skip += len(results)
