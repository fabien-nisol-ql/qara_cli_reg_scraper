"""Tests for the guidance scraper against FDA's real static dataset shape
(https://www.fda.gov/files/api/datatables/static/search-for-guidance.json,
confirmed live while building this — see the module docstring)."""

from __future__ import annotations

import responses

from qara_reg_scraper.config import HttpSettings
from qara_reg_scraper.http_client import PoliteHttpClient
from qara_reg_scraper.manifest import Manifest
from qara_reg_scraper.regulations.fda.guidance import DATASET_URL, GuidanceScraper
from qara_reg_scraper.storage.local import LocalStorage


def make_scraper(tmp_path, **kwargs):
    storage = LocalStorage(root=str(tmp_path))
    manifest = Manifest(storage, "fda", "guidance", run_id="test-run")
    http = PoliteHttpClient(
        HttpSettings(requests_per_second=1000, respect_robots_txt=False, max_retries=1), "guidance"
    )
    return storage, manifest, GuidanceScraper(http, manifest, **kwargs)


def dataset_record(slug: str, product_field: str = "Medical Devices") -> dict:
    return {
        "title": f'<a href="/regulatory-information/search-fda-guidance-documents/{slug}">Title for {slug}</a>',
        "field_issue_datetime": "01/01/2020",
        "field_issuing_office_taxonomy": "Center for Devices and Radiological Health",
        "field_regulated_product_field": product_field,
        "field_final_guidance_1": "Final",
        "field_communication_type": "Guidance Document",
        "field_center": "Center for Devices and Radiological Health",
        "field_docket_number": '<a href="https://www.regulations.gov/docket/FDA-2020-D-0001">FDA-2020-D-0001</a>',
        "open-comment": "  No ",
        "changed": "<time>2020-01-01</time>",
    }


@responses.activate
def test_only_medical_device_records_are_kept(tmp_path):
    storage, _manifest, scraper = make_scraper(tmp_path)
    records = [
        dataset_record("device-guidance-one", "Medical Devices"),
        dataset_record("combo-guidance", "Biologics, Medical Devices"),
        dataset_record("drug-guidance", "Drugs"),  # must be filtered out
    ]
    responses.add(responses.GET, DATASET_URL, json=records, status=200)
    for slug in ("device-guidance-one", "combo-guidance"):
        responses.add(
            responses.GET,
            f"https://www.fda.gov/regulatory-information/search-fda-guidance-documents/{slug}",
            body="<html>guidance content</html>", status=200, content_type="text/html",
        )

    summary = scraper.run()

    assert summary.new == 2
    assert storage.exists("fda/guidance/documents/device-guidance-one/current.html")
    assert storage.exists("fda/guidance/documents/combo-guidance/current.html")
    assert not storage.exists("fda/guidance/documents/drug-guidance/current.html")


@responses.activate
def test_source_metadata_is_captured(tmp_path):
    storage, _manifest, scraper = make_scraper(tmp_path)
    responses.add(responses.GET, DATASET_URL, json=[dataset_record("device-guidance-one")], status=200)
    responses.add(
        responses.GET,
        "https://www.fda.gov/regulatory-information/search-fda-guidance-documents/device-guidance-one",
        body="<html>guidance content</html>", status=200, content_type="text/html",
    )

    scraper.run()

    import json
    meta = json.loads(storage.read_text("fda/guidance/documents/device-guidance-one/current.meta.json"))
    sm = meta["source_metadata"]
    assert sm["issue_date"] == "01/01/2020"
    assert sm["status"] == "Final"
    assert sm["docket_number"] == "FDA-2020-D-0001"
    assert sm["center"] == "Center for Devices and Radiological Health"


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
    record = dataset_record("device-guidance-one")
    responses.add(responses.GET, DATASET_URL, json=[record], status=200)
    responses.add(
        responses.GET,
        "https://www.fda.gov/regulatory-information/search-fda-guidance-documents/device-guidance-one",
        body="<html>guidance content</html>", status=200, content_type="text/html",
    )
    scraper.run()

    # Second run: only the dataset call registered — a re-fetch of the
    # detail page would raise ConnectionError from `responses`.
    responses.reset()
    responses.add(responses.GET, DATASET_URL, json=[record], status=200)
    _storage2, _manifest2, scraper2 = make_scraper(tmp_path)
    summary2 = scraper2.run()

    assert summary2.skipped_already_known == 1
    assert summary2.new == 0
