import responses

from qara_reg_scraper.config import HttpSettings
from qara_reg_scraper.http_client import PoliteHttpClient
from qara_reg_scraper.manifest import Manifest
from qara_reg_scraper.regulations.fda.classification import ENDPOINT, ClassificationScraper
from qara_reg_scraper.storage.local import LocalStorage


def make_scraper(tmp_path, **kwargs):
    storage = LocalStorage(root=str(tmp_path))
    manifest = Manifest(storage, "fda", "classification", run_id="test-run")
    http = PoliteHttpClient(
        HttpSettings(requests_per_second=1000, respect_robots_txt=False, max_retries=1), "classification"
    )
    return storage, manifest, ClassificationScraper(http, manifest, **kwargs)


def classification_record(product_code: str) -> dict:
    return {
        "product_code": product_code,
        "device_name": "Test Device",
        "device_class": "2",
        "regulation_number": "880.2801",
        "review_panel": "HO",
        "medical_specialty": "HO",
        "medical_specialty_description": "General Hospital",
        "submission_type_id": "1",
        "definition": "A test device definition.",
    }


@responses.activate
def test_zero_budget_saves_no_new_records(tmp_path):
    """max_new_documents=0 must record nothing new this run — even though
    a record's content already arrived for free on the listing page, same
    contract as recalls.py."""
    storage, _manifest, scraper = make_scraper(tmp_path, max_new_documents=0)
    records = [classification_record("ABC"), classification_record("XYZ")]
    responses.add(responses.GET, ENDPOINT, json={"results": records}, status=200)

    summary = scraper.run()

    assert summary.new == 0
    assert summary.stop_reason == "budget_reached"
    assert not storage.exists("fda/classification/documents/ABC/current.json")


@responses.activate
def test_budget_stops_run_after_max_new_records(tmp_path):
    storage, _manifest, scraper = make_scraper(tmp_path, max_new_documents=1)
    records = [classification_record("ABC"), classification_record("XYZ")]
    responses.add(responses.GET, ENDPOINT, json={"results": records}, status=200)

    summary = scraper.run()

    assert summary.new == 1
    assert summary.stop_reason == "budget_reached"
    assert storage.exists("fda/classification/documents/ABC/current.json")
    assert not storage.exists("fda/classification/documents/XYZ/current.json")


@responses.activate
def test_full_catalog_is_walked_across_pages(tmp_path):
    """Unlike recalls.py, there's no lookback window here — a run should
    walk every page of the listing (via iter_openfda_results' skip-based
    pagination) until the catalog is exhausted."""
    storage, _manifest, scraper = make_scraper(tmp_path)
    page1 = [classification_record("ABC")]
    page2 = [classification_record("XYZ")]
    responses.add(responses.GET, ENDPOINT, json={"results": page1}, status=200)
    responses.add(responses.GET, ENDPOINT, json={"results": page2}, status=200)
    responses.add(responses.GET, ENDPOINT, json={"results": []}, status=200)

    summary = scraper.run()

    assert summary.new == 2
    assert summary.stop_reason == "completed"
    assert storage.exists("fda/classification/documents/ABC/current.json")
    assert storage.exists("fda/classification/documents/XYZ/current.json")


@responses.activate
def test_already_known_records_are_skipped_on_a_second_run(tmp_path):
    storage, _manifest, scraper = make_scraper(tmp_path)
    record = classification_record("ABC")
    responses.add(responses.GET, ENDPOINT, json={"results": [record]}, status=200)
    responses.add(responses.GET, ENDPOINT, json={"results": []}, status=200)

    summary1 = scraper.run()
    assert summary1.new == 1
    assert storage.exists("fda/classification/documents/ABC/current.json")

    responses.reset()
    responses.add(responses.GET, ENDPOINT, json={"results": [record]}, status=200)
    responses.add(responses.GET, ENDPOINT, json={"results": []}, status=200)
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
    records = [classification_record("ABC"), classification_record("XYZ")]
    responses.add(responses.GET, ENDPOINT, json={"results": records}, status=200)
    responses.add(responses.GET, ENDPOINT, json={"results": []}, status=200)

    preview = scraper.estimate()

    assert preview.total_available == 2
    assert preview.already_known == 0
