"""Tests for the warning letters scraper against FDA's real page shape
(confirmed live while building this — see the module docstring): the
results table IS server-rendered, and plain `?page=N` genuinely paginates."""

from __future__ import annotations

import responses

from qara_reg_scraper.config import HttpSettings
from qara_reg_scraper.http_client import PoliteHttpClient
from qara_reg_scraper.manifest import Manifest
from qara_reg_scraper.regulations.fda.warning_letters import (
    BASE_URL,
    SEARCH_PATH,
    WarningLettersScraper,
)
from qara_reg_scraper.storage.local import LocalStorage

LISTING_URL = f"{BASE_URL}{SEARCH_PATH}"


def make_scraper(tmp_path, **kwargs):
    storage = LocalStorage(root=str(tmp_path))
    manifest = Manifest(storage, "fda", "warning_letters", run_id="test-run")
    http = PoliteHttpClient(
        HttpSettings(requests_per_second=1000, respect_robots_txt=False, max_retries=1), "warning_letters"
    )
    return storage, manifest, WarningLettersScraper(http, manifest, **kwargs)


def table_html(rows: list[tuple[str, str]]) -> str:
    """rows: list of (slug, company_name)."""
    trs = "".join(
        f"""<tr>
            <td><time>08/04/2026</time></td>
            <td><time>07/24/2026</time></td>
            <td><a href="/inspections-compliance-enforcement-and-criminal-investigations/warning-letters/{slug}">{name}</a></td>
            <td>Center for Devices and Radiological Health</td>
            <td>CGMP/QSR/Medical Devices/Adulterated</td>
            <td></td>
            <td></td>
        </tr>"""
        for slug, name in rows
    )
    return f"<html><body><table><tbody>{trs}</tbody></table></body></html>"


@responses.activate
def test_real_row_shape_is_parsed_and_saved(tmp_path):
    storage, _manifest, scraper = make_scraper(tmp_path)
    responses.add(
        responses.GET, LISTING_URL,
        body=table_html([("acme-devices-123-08042026", "Acme Devices Inc.")]),
        status=200, content_type="text/html",
    )
    responses.add(responses.GET, LISTING_URL, body=table_html([]), status=200, content_type="text/html")
    responses.add(
        responses.GET,
        f"{BASE_URL}/inspections-compliance-enforcement-and-criminal-investigations/warning-letters/acme-devices-123-08042026",
        body="<html>letter content</html>", status=200, content_type="text/html",
    )

    summary = scraper.run()

    assert summary.new == 1
    assert summary.stop_reason == "completed"
    assert storage.exists("fda/warning_letters/documents/acme-devices-123-08042026/current.html")


@responses.activate
def test_document_id_uses_href_slug_not_company_name(tmp_path):
    """Two different letters to the same company must not collide — the
    href's unique id+date suffix is the document id, not the company name."""
    storage, _manifest, scraper = make_scraper(tmp_path)
    responses.add(
        responses.GET, LISTING_URL,
        body=table_html([
            ("acme-devices-111-01012026", "Acme Devices Inc."),
            ("acme-devices-222-06012026", "Acme Devices Inc."),
        ]),
        status=200, content_type="text/html",
    )
    responses.add(responses.GET, LISTING_URL, body=table_html([]), status=200, content_type="text/html")
    for slug in ("acme-devices-111-01012026", "acme-devices-222-06012026"):
        responses.add(
            responses.GET,
            f"{BASE_URL}/inspections-compliance-enforcement-and-criminal-investigations/warning-letters/{slug}",
            body="<html>letter content</html>", status=200, content_type="text/html",
        )

    summary = scraper.run()

    assert summary.new == 2
    assert storage.exists("fda/warning_letters/documents/acme-devices-111-01012026/current.html")
    assert storage.exists("fda/warning_letters/documents/acme-devices-222-06012026/current.html")


@responses.activate
def test_pagination_stops_on_empty_page(tmp_path):
    _storage, _manifest, scraper = make_scraper(tmp_path)
    responses.add(
        responses.GET, LISTING_URL, body=table_html([("letter-a-01012026", "Company A")]),
        status=200, content_type="text/html",
    )
    responses.add(responses.GET, LISTING_URL, body=table_html([]), status=200, content_type="text/html")
    responses.add(
        responses.GET,
        f"{BASE_URL}/inspections-compliance-enforcement-and-criminal-investigations/warning-letters/letter-a-01012026",
        body="<html>letter content</html>", status=200, content_type="text/html",
    )

    summary = scraper.run()

    assert summary.stop_reason == "completed"
    assert len(responses.calls) == 3  # page 0 (with a row), page 1 (empty), the one detail fetch
