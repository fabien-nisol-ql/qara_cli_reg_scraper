"""Tests for the Food and Drugs Act scraper — see test_ca_mdr.py's own
docstring; identical shape, different Justice Laws URL."""

from __future__ import annotations

import json

import responses

from qara_reg_scraper.config import HttpSettings
from qara_reg_scraper.http_client import PoliteHttpClient
from qara_reg_scraper.manifest import Manifest
from qara_reg_scraper.regulations.ca.food_and_drugs_act import (
    DOCUMENT_ID,
    FULL_TEXT_URL,
    FoodAndDrugsActScraper,
)
from qara_reg_scraper.storage.local import LocalStorage


def make_scraper(tmp_path, **kwargs):
    storage = LocalStorage(root=str(tmp_path))
    manifest = Manifest(storage, "ca", "food_and_drugs_act", run_id="test-run")
    http = PoliteHttpClient(
        HttpSettings(requests_per_second=1000, respect_robots_txt=False, max_retries=1), "food_and_drugs_act"
    )
    return storage, manifest, FoodAndDrugsActScraper(http, manifest, **kwargs)


@responses.activate
def test_run_fetches_the_full_text_as_one_document(tmp_path):
    storage, _manifest, scraper = make_scraper(tmp_path)
    responses.add(responses.GET, FULL_TEXT_URL, body="<html>fda text</html>", status=200, content_type="text/html")

    summary = scraper.run()

    assert summary.new == 1
    assert summary.stop_reason == "completed"
    assert storage.exists(f"ca/food_and_drugs_act/documents/{DOCUMENT_ID}/current.html")
    meta = json.loads(storage.read_text(f"ca/food_and_drugs_act/documents/{DOCUMENT_ID}/current.meta.json"))
    assert meta["canonical_url"] == FULL_TEXT_URL
    assert meta["source_metadata"] == {"citation": "R.S.C., 1985, c. F-27", "source": "justice_laws"}


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
