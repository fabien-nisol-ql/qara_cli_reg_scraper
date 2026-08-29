import responses

from qara_reg_scraper.config import HttpSettings
from qara_reg_scraper.http_client import PoliteHttpClient
from qara_reg_scraper.manifest import Manifest
from qara_reg_scraper.regulations.fda.recalls import ENDPOINT, RecallsScraper
from qara_reg_scraper.storage.local import LocalStorage


def make_scraper(tmp_path, **kwargs):
    storage = LocalStorage(root=str(tmp_path))
    manifest = Manifest(storage, "fda", "recalls", run_id="test-run")
    http = PoliteHttpClient(
        HttpSettings(requests_per_second=1000, respect_robots_txt=False, max_retries=1), "recalls"
    )
    return storage, manifest, RecallsScraper(http, manifest, **kwargs)


def enforcement_record(recall_number: str) -> dict:
    return {
        "recall_number": recall_number,
        "product_description": "Test device",
        "classification": "Class II",
        "status": "Ongoing",
        "recalling_firm": "Acme Devices Inc.",
        "report_date": "2026-07-25",
        "product_code": "ABC",
    }


def test_effective_lookback_days_defaults_to_thirty(tmp_path):
    _storage, _manifest, scraper = make_scraper(tmp_path)
    assert scraper.effective_lookback_days == 30


def test_effective_lookback_days_respects_override(tmp_path):
    _storage, _manifest, scraper = make_scraper(tmp_path, lookback_days=7)
    assert scraper.effective_lookback_days == 7


@responses.activate
def test_zero_budget_saves_no_new_records(tmp_path):
    """max_new_documents=0 must record nothing new this run — even though
    a recall's content already arrived for free on the listing page, the
    documented contract for --max-new-documents 0 is "fetch nothing new
    this run", not "write anything that happened to be free.\""""
    storage, _manifest, scraper = make_scraper(tmp_path, max_new_documents=0)
    records = [enforcement_record("Z-1111-2026"), enforcement_record("Z-2222-2026")]
    responses.add(responses.GET, ENDPOINT, json={"results": records}, status=200)

    summary = scraper.run()

    assert summary.new == 0
    assert summary.stop_reason == "budget_reached"
    assert not storage.exists("fda/recalls/documents/Z-1111-2026/current.json")


@responses.activate
def test_budget_stops_run_after_max_new_records(tmp_path):
    storage, _manifest, scraper = make_scraper(tmp_path, max_new_documents=1)
    records = [enforcement_record("Z-1111-2026"), enforcement_record("Z-2222-2026")]
    responses.add(responses.GET, ENDPOINT, json={"results": records}, status=200)

    summary = scraper.run()

    assert summary.new == 1
    assert summary.stop_reason == "budget_reached"
    assert storage.exists("fda/recalls/documents/Z-1111-2026/current.json")
    assert not storage.exists("fda/recalls/documents/Z-2222-2026/current.json")


@responses.activate
def test_already_known_records_are_skipped_on_a_second_run(tmp_path):
    storage, _manifest, scraper = make_scraper(tmp_path)
    record = enforcement_record("Z-1111-2026")
    responses.add(responses.GET, ENDPOINT, json={"results": [record]}, status=200)
    responses.add(responses.GET, ENDPOINT, json={"results": []}, status=200)

    summary1 = scraper.run()
    assert summary1.new == 1
    assert storage.exists("fda/recalls/documents/Z-1111-2026/current.json")

    responses.reset()
    responses.add(responses.GET, ENDPOINT, json={"results": [record]}, status=200)
    responses.add(responses.GET, ENDPOINT, json={"results": []}, status=200)
    _storage2, _manifest2, scraper2 = make_scraper(tmp_path)
    summary2 = scraper2.run()

    assert summary2.new == 0
    assert summary2.skipped_already_known == 1
