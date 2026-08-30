"""Tests for the MDR scraper. CELEX-resolution logic itself is covered in
test_eur_lex_consolidated.py — these just confirm mdr.py wires it up
correctly (fixed document id, hard-stop on resolution failure, source
metadata, already-have skip)."""

from __future__ import annotations

import json

import responses

from qara_reg_scraper.config import HttpSettings
from qara_reg_scraper.http_client import PoliteHttpClient
from qara_reg_scraper.manifest import Manifest
from qara_reg_scraper.regulations.eu.eur_lex_consolidated import BASE_URL
from qara_reg_scraper.regulations.eu.mdr import DOCUMENT_ID, ORIGINAL_CELEX, MdrScraper
from qara_reg_scraper.storage.local import LocalStorage

ALL_URL = f"{BASE_URL}/legal-content/EN/ALL/?uri=CELEX:{ORIGINAL_CELEX}"
CONSOLIDATED_CELEX = "02017R0745-20260719"
TEXT_URL = f"{BASE_URL}/legal-content/EN/TXT/?uri=CELEX:{CONSOLIDATED_CELEX}"
ALL_PAGE_HTML = (
    "<html><body>"
    f'<a href="./../../../legal-content/EN/AUTO/?uri=CELEX:{CONSOLIDATED_CELEX}">'
    "Access current version (19/07/2026)</a>"
    "</body></html>"
)


def make_scraper(tmp_path, **kwargs):
    storage = LocalStorage(root=str(tmp_path))
    manifest = Manifest(storage, "eu", "mdr", run_id="test-run")
    http = PoliteHttpClient(
        HttpSettings(requests_per_second=1000, respect_robots_txt=False, max_retries=1), "mdr"
    )
    return storage, manifest, MdrScraper(http, manifest, **kwargs)


@responses.activate
def test_run_resolves_and_fetches_the_current_consolidated_text(tmp_path):
    storage, _manifest, scraper = make_scraper(tmp_path)
    responses.add(responses.GET, ALL_URL, body=ALL_PAGE_HTML, status=200, content_type="text/html")
    responses.add(responses.GET, TEXT_URL, body="<html>mdr text</html>", status=200, content_type="text/html")

    summary = scraper.run()

    assert summary.new == 1
    assert summary.stop_reason == "completed"
    assert storage.exists(f"eu/mdr/documents/{DOCUMENT_ID}/current.html")
    meta = json.loads(storage.read_text(f"eu/mdr/documents/{DOCUMENT_ID}/current.meta.json"))
    assert meta["canonical_url"] == TEXT_URL
    assert meta["source_metadata"] == {
        "original_celex": ORIGINAL_CELEX,
        "consolidated_celex": CONSOLIDATED_CELEX,
        "source": "eur_lex",
    }


@responses.activate
def test_resolution_failure_is_a_hard_stop_without_touching_the_text_page(tmp_path):
    _storage, _manifest, scraper = make_scraper(tmp_path)
    responses.add(responses.GET, ALL_URL, status=500)

    summary = scraper.run()

    assert summary.stop_reason == "hard_stop"
    assert summary.errors == 1
    assert len(responses.calls) == 1  # never even tried the text page


def test_estimate_reports_one_document_with_no_network_call(tmp_path):
    _storage, _manifest, scraper = make_scraper(tmp_path)

    info = scraper.estimate()

    assert info.total_available == 1
    assert info.already_known == 0


@responses.activate
def test_already_known_document_is_skipped_without_refetching_the_text(tmp_path):
    _storage, _manifest, scraper = make_scraper(tmp_path)
    responses.add(responses.GET, ALL_URL, body=ALL_PAGE_HTML, status=200, content_type="text/html")
    responses.add(responses.GET, TEXT_URL, body="<html>mdr text</html>", status=200, content_type="text/html")
    scraper.run()

    # Second run: resolution still runs unconditionally (it's what decides
    # whether there's even anything new to check), but re-registering the
    # text URL would raise ConnectionError from `responses` if fetch_and_save
    # were called again for an already-known document.
    responses.reset()
    responses.add(responses.GET, ALL_URL, body=ALL_PAGE_HTML, status=200, content_type="text/html")
    _storage2, _manifest2, scraper2 = make_scraper(tmp_path)
    summary2 = scraper2.run()

    assert summary2.skipped_already_known == 1
    assert summary2.new == 0
