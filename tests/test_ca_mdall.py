"""Tests for the MDALL scraper against Health Canada's real API shape
(confirmed live while building this — see the module docstring): one
request returns the ENTIRE active-licence catalog as a plain JSON array,
no pagination."""

from __future__ import annotations

import json

import responses

from qara_reg_scraper.config import HttpSettings
from qara_reg_scraper.http_client import PoliteHttpClient
from qara_reg_scraper.manifest import Manifest
from qara_reg_scraper.regulations.ca.mdall import ENDPOINT, MdallScraper
from qara_reg_scraper.storage.local import LocalStorage


def make_scraper(tmp_path, **kwargs):
    storage = LocalStorage(root=str(tmp_path))
    manifest = Manifest(storage, "ca", "mdall", run_id="test-run")
    http = PoliteHttpClient(
        HttpSettings(requests_per_second=1000, respect_robots_txt=False, max_retries=1), "mdall"
    )
    return storage, manifest, MdallScraper(http, manifest, **kwargs)


def licence_record(licence_no: int, name: str = "Test Device") -> dict:
    return {
        "original_licence_no": licence_no,
        "licence_status": "I",
        "appl_risk_class": 2,
        "licence_name": name,
        "first_licence_status_dt": "2001-10-03",
        "licence_type_cd": "F",
        "licence_type_desc": "Device Family",
        "company_id": 100559,
    }


@responses.activate
def test_run_saves_one_document_per_licence(tmp_path):
    storage, _manifest, scraper = make_scraper(tmp_path)
    records = [licence_record(1), licence_record(2)]
    responses.add(responses.GET, ENDPOINT, json=records, status=200)

    summary = scraper.run()

    assert summary.new == 2
    assert summary.stop_reason == "completed"
    assert storage.exists("ca/mdall/documents/1/current.json")
    assert storage.exists("ca/mdall/documents/2/current.json")
    meta = json.loads(storage.read_text("ca/mdall/documents/1/current.meta.json"))
    assert meta["source_metadata"]["original_licence_no"] == 1


@responses.activate
def test_canonical_url_is_scoped_to_that_one_licence_not_the_whole_catalog(tmp_path):
    """A regression test: canonical_url used to be the shared listing
    ENDPOINT for every record, so "browse original source" dumped a
    reader into the entire ~35,600-record catalog instead of the one
    licence they were looking at (caught live, in the UI, 2026-08-30).
    `?id=<licence_no>` is documented and confirmed live to return just
    that one record."""
    storage, _manifest, scraper = make_scraper(tmp_path)
    responses.add(responses.GET, ENDPOINT, json=[licence_record(1)], status=200)

    scraper.run()

    meta = json.loads(storage.read_text("ca/mdall/documents/1/current.meta.json"))
    assert meta["canonical_url"] == "https://health-products.canada.ca/api/medical-devices/licence/?id=1&type=json&lang=en"
    assert meta["canonical_url"] != ENDPOINT
    assert meta["title"] == "Test Device"


@responses.activate
def test_zero_budget_saves_no_new_records(tmp_path):
    storage, _manifest, scraper = make_scraper(tmp_path, max_new_documents=0)
    responses.add(responses.GET, ENDPOINT, json=[licence_record(1), licence_record(2)], status=200)

    summary = scraper.run()

    assert summary.new == 0
    assert summary.stop_reason == "budget_reached"
    assert not storage.exists("ca/mdall/documents/1/current.json")


@responses.activate
def test_budget_stops_run_after_max_new_records(tmp_path):
    storage, _manifest, scraper = make_scraper(tmp_path, max_new_documents=1)
    responses.add(responses.GET, ENDPOINT, json=[licence_record(1), licence_record(2)], status=200)

    summary = scraper.run()

    assert summary.new == 1
    assert summary.stop_reason == "budget_reached"
    assert storage.exists("ca/mdall/documents/1/current.json")
    assert not storage.exists("ca/mdall/documents/2/current.json")


@responses.activate
def test_already_known_records_are_skipped_on_a_second_run(tmp_path):
    _storage, _manifest, scraper = make_scraper(tmp_path)
    record = licence_record(1)
    responses.add(responses.GET, ENDPOINT, json=[record], status=200)
    summary1 = scraper.run()
    assert summary1.new == 1

    responses.reset()
    responses.add(responses.GET, ENDPOINT, json=[record], status=200)
    _storage2, _manifest2, scraper2 = make_scraper(tmp_path)
    summary2 = scraper2.run()

    assert summary2.new == 0
    assert summary2.skipped_already_known == 1


@responses.activate
def test_listing_failure_is_a_hard_stop(tmp_path):
    _storage, _manifest, scraper = make_scraper(tmp_path)
    responses.add(responses.GET, ENDPOINT, status=500)

    summary = scraper.run()

    assert summary.stop_reason == "hard_stop"
    assert summary.errors == 1


@responses.activate
def test_estimate_reports_total_and_already_known(tmp_path):
    _storage, _manifest, scraper = make_scraper(tmp_path)
    responses.add(responses.GET, ENDPOINT, json=[licence_record(1), licence_record(2)], status=200)

    preview = scraper.estimate()

    assert preview.total_available == 2
    assert preview.already_known == 0
