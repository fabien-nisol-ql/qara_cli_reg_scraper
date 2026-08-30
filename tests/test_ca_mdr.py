"""Tests for the Canadian MDR scraper — single fixed Justice Laws URL, no
discovery step needed (unlike eu:mdr's EUR-Lex consolidated-CELEX dance —
see mdr.py's own module docstring for why)."""

from __future__ import annotations

import json

import responses

from qara_reg_scraper.config import HttpSettings
from qara_reg_scraper.http_client import PoliteHttpClient
from qara_reg_scraper.manifest import Manifest
from qara_reg_scraper.regulations.ca.mdr import DOCUMENT_ID, FULL_TEXT_URL, MdrScraper
from qara_reg_scraper.storage.local import LocalStorage


def make_scraper(tmp_path, **kwargs):
    storage = LocalStorage(root=str(tmp_path))
    manifest = Manifest(storage, "ca", "mdr", run_id="test-run")
    http = PoliteHttpClient(
        HttpSettings(requests_per_second=1000, respect_robots_txt=False, max_retries=1), "mdr"
    )
    return storage, manifest, MdrScraper(http, manifest, **kwargs)


@responses.activate
def test_run_fetches_the_full_text_as_one_document(tmp_path):
    storage, _manifest, scraper = make_scraper(tmp_path)
    responses.add(responses.GET, FULL_TEXT_URL, body="<html>mdr text</html>", status=200, content_type="text/html")

    summary = scraper.run()

    assert summary.new == 1
    assert summary.stop_reason == "completed"
    assert storage.exists(f"ca/mdr/documents/{DOCUMENT_ID}/current.html")
    meta = json.loads(storage.read_text(f"ca/mdr/documents/{DOCUMENT_ID}/current.meta.json"))
    assert meta["canonical_url"] == FULL_TEXT_URL
    assert meta["source_metadata"] == {"instrument_number": "SOR/98-282", "source": "justice_laws"}


def test_estimate_reports_one_document_with_no_network_call(tmp_path):
    _storage, _manifest, scraper = make_scraper(tmp_path)

    info = scraper.estimate()

    assert info.total_available == 1
    assert info.already_known == 0


@responses.activate
def test_fetch_failure_is_a_hard_stop(tmp_path):
    _storage, _manifest, scraper = make_scraper(tmp_path)
    responses.add(responses.GET, FULL_TEXT_URL, status=500)

    summary = scraper.run()

    assert summary.stop_reason == "hard_stop"
    assert summary.errors == 1


@responses.activate
def test_already_known_document_is_skipped_without_network_call(tmp_path):
    _storage, _manifest, scraper = make_scraper(tmp_path)
    responses.add(responses.GET, FULL_TEXT_URL, body="<html>mdr text</html>", status=200, content_type="text/html")
    scraper.run()

    # Second run: nothing registered — a re-fetch attempt would raise
    # ConnectionError from `responses`.
    responses.reset()
    _storage2, _manifest2, scraper2 = make_scraper(tmp_path)
    summary2 = scraper2.run()

    assert summary2.skipped_already_known == 1
    assert summary2.new == 0
