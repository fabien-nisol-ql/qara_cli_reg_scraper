"""Tests for eur_lex_consolidated.resolve_latest_consolidated against
EUR-Lex's real /ALL/ page shape (confirmed live while building this — see
the module docstring): an `<a>` labeled "Access current version (DD/MM/
YYYY)" pointing at the current consolidated CELEX, alongside older ones
with no such label."""

from __future__ import annotations

import pytest
import responses

from qara_reg_scraper.config import HttpSettings
from qara_reg_scraper.http_client import PoliteHttpClient
from qara_reg_scraper.regulations.eu.eur_lex_consolidated import (
    BASE_URL,
    resolve_latest_consolidated,
)

ORIGINAL_CELEX = "32017R0745"
ALL_URL = f"{BASE_URL}/legal-content/EN/ALL/?uri=CELEX:{ORIGINAL_CELEX}"


def make_http():
    return PoliteHttpClient(
        HttpSettings(requests_per_second=1000, respect_robots_txt=False, max_retries=1), "mdr"
    )


def all_page_html(*versions: tuple[str, bool]) -> str:
    """versions: list of (yyyymmdd, is_current)."""
    links = "".join(
        f'<a href="./../../../legal-content/EN/AUTO/?uri=CELEX:02017R0745-{date}">'
        f'{"Access current version (" + date[6:8] + "/" + date[4:6] + "/" + date[0:4] + ")" if current else date}'
        f"</a>"
        for date, current in versions
    )
    return f"<html><body>{links}</body></html>"


@responses.activate
def test_the_explicitly_labeled_current_version_is_preferred():
    responses.add(
        responses.GET, ALL_URL,
        body=all_page_html(("20170505", False), ("20260719", True), ("20260101", False)),
        status=200, content_type="text/html",
    )

    consolidated_celex, url = resolve_latest_consolidated(make_http(), ORIGINAL_CELEX)

    assert consolidated_celex == "02017R0745-20260719"
    assert url == f"{BASE_URL}/legal-content/EN/TXT/?uri=CELEX:02017R0745-20260719"


@responses.activate
def test_falls_back_to_the_lexicographically_max_date_if_no_label_found():
    responses.add(
        responses.GET, ALL_URL,
        body=all_page_html(("20170505", False), ("20260719", False), ("20200424", False)),
        status=200, content_type="text/html",
    )

    consolidated_celex, _url = resolve_latest_consolidated(make_http(), ORIGINAL_CELEX)

    assert consolidated_celex == "02017R0745-20260719"


@responses.activate
def test_no_consolidated_version_at_all_raises():
    responses.add(responses.GET, ALL_URL, body="<html><body>nothing here</body></html>", status=200)

    with pytest.raises(ValueError):
        resolve_latest_consolidated(make_http(), ORIGINAL_CELEX)


@responses.activate
def test_fetch_failure_propagates_to_the_caller():
    responses.add(responses.GET, ALL_URL, status=500)

    with pytest.raises(Exception):  # noqa: B017 - just confirming it's not silently swallowed
        resolve_latest_consolidated(make_http(), ORIGINAL_CELEX)
