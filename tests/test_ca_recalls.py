"""Tests for the Canadian recalls scraper against Health Canada's real
open-data shape (confirmed live while building this — see the module
docstring): one request returns every recall/alert across all
categories, filtered here to Organization == "Medical devices"."""

from __future__ import annotations

import json

import responses

from qara_reg_scraper.config import HttpSettings
from qara_reg_scraper.http_client import PoliteHttpClient
from qara_reg_scraper.manifest import Manifest
from qara_reg_scraper.regulations.ca.recalls import DATASET_URL, RecallsScraper
from qara_reg_scraper.storage.local import LocalStorage


def make_scraper(tmp_path, **kwargs):
    storage = LocalStorage(root=str(tmp_path))
    manifest = Manifest(storage, "ca", "recalls", run_id="test-run")
    http = PoliteHttpClient(
        HttpSettings(requests_per_second=1000, respect_robots_txt=False, max_retries=1), "recalls"
    )
    return storage, manifest, RecallsScraper(http, manifest, **kwargs)


def recall_record(nid: str, organization: str = "Medical devices", title: str = "Test Recall") -> dict:
    return {
        "NID": nid,
        "Title": title,
        "URL": f"https://recalls-rappels.canada.ca/en/alert-recall/{nid}",
        "Organization": organization,
        "Product": title,
        "Issue": "Performance",
        "Category": "General hospital and personal use",
        "Recall class": "Type II",
        "Last updated": "2026-08-28",
        "Archived": "0",
    }


@responses.activate
def test_only_medical_device_records_are_kept(tmp_path):
    storage, _manifest, scraper = make_scraper(tmp_path)
    records = [
        recall_record("1", "Medical devices"),
        recall_record("2", "TC"),  # a different category — must be filtered out
        recall_record("3", "Medical devices"),
    ]
    responses.add(responses.GET, DATASET_URL, json=records, status=200)

    summary = scraper.run()

    assert summary.new == 2
    assert storage.exists("ca/recalls/documents/1/current.json")
    assert storage.exists("ca/recalls/documents/3/current.json")
    assert not storage.exists("ca/recalls/documents/2/current.json")


@responses.activate
def test_source_metadata_is_captured(tmp_path):
    storage, _manifest, scraper = make_scraper(tmp_path)
    responses.add(responses.GET, DATASET_URL, json=[recall_record("1")], status=200)

    scraper.run()

    meta = json.loads(storage.read_text("ca/recalls/documents/1/current.meta.json"))
    sm = meta["source_metadata"]
    assert sm["nid"] == "1"
    assert sm["canonical_url"] == "https://recalls-rappels.canada.ca/en/alert-recall/1"
    assert sm["issue"] == "Performance"
    assert sm["recall_class"] == "Type II"


@responses.activate
def test_dataset_fetch_failure_is_a_hard_stop(tmp_path):
    _storage, _manifest, scraper = make_scraper(tmp_path)
    responses.add(responses.GET, DATASET_URL, status=500)

    summary = scraper.run()

    assert summary.stop_reason == "hard_stop"
    assert summary.errors == 1


@responses.activate
def test_already_known_document_is_skipped_without_network_call(tmp_path):
    _storage, _manifest, scraper = make_scraper(tmp_path)
    record = recall_record("1")
    responses.add(responses.GET, DATASET_URL, json=[record], status=200)
    scraper.run()

    # Second run: only the dataset call registered — no per-document fetch
    # exists for this source at all (content's already inline).
    responses.reset()
    responses.add(responses.GET, DATASET_URL, json=[record], status=200)
    _storage2, _manifest2, scraper2 = make_scraper(tmp_path)
    summary2 = scraper2.run()

    assert summary2.skipped_already_known == 1
    assert summary2.new == 0


@responses.activate
def test_budget_stops_run_after_max_new_records(tmp_path):
    storage, _manifest, scraper = make_scraper(tmp_path, max_new_documents=1)
    responses.add(
        responses.GET, DATASET_URL,
        json=[recall_record("1"), recall_record("2")], status=200,
    )

    summary = scraper.run()

    assert summary.new == 1
    assert summary.stop_reason == "budget_reached"
    assert storage.exists("ca/recalls/documents/1/current.json")
    assert not storage.exists("ca/recalls/documents/2/current.json")
