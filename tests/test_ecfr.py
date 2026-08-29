from __future__ import annotations

import json

import responses

from qara_reg_scraper.config import HttpSettings
from qara_reg_scraper.http_client import PoliteHttpClient
from qara_reg_scraper.manifest import Manifest
from qara_reg_scraper.regulations.fda.ecfr import DOCUMENT_ID, GOVINFO_URL, EcfrScraper
from qara_reg_scraper.storage.local import LocalStorage


def make_scraper(tmp_path, **kwargs):
    storage = LocalStorage(root=str(tmp_path))
    manifest = Manifest(storage, "fda", "ecfr", run_id="test-run")
    http = PoliteHttpClient(
        HttpSettings(requests_per_second=1000, respect_robots_txt=False, max_retries=1), "ecfr"
    )
    return storage, manifest, EcfrScraper(http, manifest, **kwargs)


@responses.activate
def test_run_fetches_the_govinfo_bulk_xml_as_one_document(tmp_path):
    storage, _manifest, scraper = make_scraper(tmp_path)
    responses.add(
        responses.GET, GOVINFO_URL,
        body="<TITLE21>...</TITLE21>", status=200, content_type="application/xml",
    )

    summary = scraper.run()

    assert summary.new == 1
    assert summary.stop_reason == "completed"
    assert storage.exists(f"fda/ecfr/documents/{DOCUMENT_ID}/current.xml")
    meta = json.loads(storage.read_text(f"fda/ecfr/documents/{DOCUMENT_ID}/current.meta.json"))
    assert meta["content_type"] == "application/xml"
    assert meta["canonical_url"] == GOVINFO_URL
    assert meta["source_metadata"] == {"title": 21, "source": "govinfo_bulkdata"}


def test_estimate_reports_one_document_with_no_network_call(tmp_path):
    _storage, _manifest, scraper = make_scraper(tmp_path)

    info = scraper.estimate()

    assert info.total_available == 1
    assert info.already_known == 0
    assert info.remaining == 1


@responses.activate
def test_estimate_reflects_already_fetched_document(tmp_path):
    _storage, _manifest, scraper = make_scraper(tmp_path)
    responses.add(
        responses.GET, GOVINFO_URL,
        body="<TITLE21>x</TITLE21>", status=200, content_type="application/xml",
    )
    scraper.run()

    info = scraper.estimate()

    assert info.total_available == 1
    assert info.already_known == 1
    assert info.remaining == 0


@responses.activate
def test_fetch_failure_is_a_hard_stop(tmp_path):
    _storage, _manifest, scraper = make_scraper(tmp_path)
    responses.add(responses.GET, GOVINFO_URL, status=500)

    summary = scraper.run()

    assert summary.stop_reason == "hard_stop"
    assert summary.errors == 1


@responses.activate
def test_already_known_document_is_skipped_without_network_call(tmp_path):
    _storage, _manifest, scraper = make_scraper(tmp_path)
    responses.add(
        responses.GET, GOVINFO_URL,
        body="<TITLE21>x</TITLE21>", status=200, content_type="application/xml",
    )
    scraper.run()

    # Second run: nothing registered — a re-fetch attempt would raise
    # ConnectionError from `responses`.
    responses.reset()
    _storage2, _manifest2, scraper2 = make_scraper(tmp_path)
    summary2 = scraper2.run()

    assert summary2.skipped_already_known == 1
    assert summary2.new == 0
