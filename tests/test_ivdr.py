"""Tests for the IVDR scraper — see test_mdr.py's own docstring; identical
shape, different CELEX. CELEX-resolution logic itself is covered in
test_eur_lex_consolidated.py."""

from __future__ import annotations

import json

import responses

from qara_reg_scraper.config import HttpSettings
from qara_reg_scraper.http_client import PoliteHttpClient
from qara_reg_scraper.manifest import Manifest
from qara_reg_scraper.regulations.eu.eur_lex_consolidated import BASE_URL
from qara_reg_scraper.regulations.eu.ivdr import DOCUMENT_ID, ORIGINAL_CELEX, IvdrScraper
from qara_reg_scraper.storage.local import LocalStorage

ALL_URL = f"{BASE_URL}/legal-content/EN/ALL/?uri=CELEX:{ORIGINAL_CELEX}"
CONSOLIDATED_CELEX = "02017R0746-20250110"
TEXT_URL = f"{BASE_URL}/legal-content/EN/TXT/?uri=CELEX:{CONSOLIDATED_CELEX}"
ALL_PAGE_HTML = (
    "<html><body>"
    f'<a href="./../../../legal-content/EN/AUTO/?uri=CELEX:{CONSOLIDATED_CELEX}">'
    "Access current version (10/01/2025)</a>"
    "</body></html>"
)


def make_scraper(tmp_path, **kwargs):
    storage = LocalStorage(root=str(tmp_path))
    manifest = Manifest(storage, "eu", "ivdr", run_id="test-run")
    http = PoliteHttpClient(
        HttpSettings(requests_per_second=1000, respect_robots_txt=False, max_retries=1), "ivdr"
    )
    return storage, manifest, IvdrScraper(http, manifest, **kwargs)


@responses.activate
def test_run_resolves_and_fetches_the_current_consolidated_text(tmp_path):
    storage, _manifest, scraper = make_scraper(tmp_path)
    responses.add(responses.GET, ALL_URL, body=ALL_PAGE_HTML, status=200, content_type="text/html")
    responses.add(responses.GET, TEXT_URL, body="<html>ivdr text</html>", status=200, content_type="text/html")

    summary = scraper.run()

    assert summary.new == 1
    assert summary.stop_reason == "completed"
    assert storage.exists(f"eu/ivdr/documents/{DOCUMENT_ID}/current.html")
    meta = json.loads(storage.read_text(f"eu/ivdr/documents/{DOCUMENT_ID}/current.meta.json"))
    assert meta["canonical_url"] == TEXT_URL
    assert meta["source_metadata"] == {
        "original_celex": ORIGINAL_CELEX,
        "consolidated_celex": CONSOLIDATED_CELEX,
        "source": "eur_lex",
    }


@responses.activate
def test_resolution_failure_is_a_hard_stop(tmp_path):
    _storage, _manifest, scraper = make_scraper(tmp_path)
    responses.add(responses.GET, ALL_URL, status=500)

    summary = scraper.run()

    assert summary.stop_reason == "hard_stop"
    assert summary.errors == 1


def test_estimate_reports_one_document_with_no_network_call(tmp_path):
    _storage, _manifest, scraper = make_scraper(tmp_path)

    info = scraper.estimate()

    assert info.total_available == 1
    assert info.already_known == 0
