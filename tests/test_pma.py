import json
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote

import responses

from qara_reg_scraper.config import HttpSettings
from qara_reg_scraper.http_client import PoliteHttpClient
from qara_reg_scraper.manifest import Manifest
from qara_reg_scraper.regulations.fda.pma import ENDPOINT, PmaScraper, _decision_id
from qara_reg_scraper.storage.local import LocalStorage


def make_scraper(tmp_path, **kwargs):
    storage = LocalStorage(root=str(tmp_path))
    manifest = Manifest(storage, "fda", "pma", run_id="test-run")
    http = PoliteHttpClient(
        HttpSettings(requests_per_second=1000, respect_robots_txt=False, max_retries=1),
        "pma",
    )
    return storage, manifest, PmaScraper(http, manifest, **kwargs)


def openfda_pma_record(pma_number: str, supplement_number: str = "") -> dict:
    return {
        "pma_number": pma_number,
        "supplement_number": supplement_number,
        "applicant": "Acme Devices Inc.",
        "trade_name": f"Test Device {pma_number}",
        "generic_name": "Test generic name",
        "product_code": "ABC",
        "decision_date": "2026-07-25",
        "decision_code": "APPR",
        "supplement_type": "",
        "supplement_reason": "",
        "advisory_committee": "CV",
    }


def test_decision_id_omits_supplement_for_an_original_approval():
    assert _decision_id(openfda_pma_record("P160035")) == "P160035"


def test_decision_id_includes_supplement_when_present():
    assert _decision_id(openfda_pma_record("P160035", supplement_number="S006")) == "P160035S006"


def test_effective_lookback_days_defaults_to_thirty(tmp_path):
    _storage, _manifest, scraper = make_scraper(tmp_path)
    assert scraper.effective_lookback_days == 30


@responses.activate
def test_lookback_days_override_changes_the_openfda_query_window(tmp_path):
    _storage, _manifest, scraper = make_scraper(tmp_path, lookback_days=90)
    responses.add(responses.GET, ENDPOINT, json={"results": []}, status=200)

    scraper.run()

    request_url = unquote(responses.calls[0].request.url)
    expected_start = (datetime.now(UTC).date() - timedelta(days=90)).strftime("%Y-%m-%d")
    match = re.search(r"decision_date:\[([\d-]+)", request_url)
    assert match is not None
    assert match.group(1) == expected_start


@responses.activate
def test_metadata_and_order_letter_both_saved(tmp_path):
    storage, _manifest, scraper = make_scraper(tmp_path)
    record = openfda_pma_record("P160035", supplement_number="S006")
    responses.add(
        responses.GET,
        "https://www.accessdata.fda.gov/cdrh_docs/pdf16/P160035S006A.pdf",
        body=b"%PDF-1.4 fake pdf bytes",
        status=200,
        content_type="application/pdf",
    )

    scraper._save_metadata("P160035S006", record)
    scraper._fetch_order("P160035S006", record)

    assert storage.exists("fda/pma/documents/P160035S006/metadata/current.json")
    meta = json.loads(storage.read_text("fda/pma/documents/P160035S006/metadata/current.json"))
    assert meta["trade_name"] == "Test Device P160035"

    assert storage.exists("fda/pma/documents/P160035S006/order/current.pdf")
    assert storage.read_bytes("fda/pma/documents/P160035S006/order/current.pdf") == b"%PDF-1.4 fake pdf bytes"


@responses.activate
def test_a_routine_404_is_persisted_as_not_applicable_not_a_bare_error(tmp_path):
    """No `statement_or_summary`-style signal exists for PMA, and a real
    live run confirmed roughly half of all supplements have no order
    letter posted — so, unlike a bare per-document error (which
    already_have would never recognize, causing this exact miss to be
    re-attempted on every future run forever), a routine miss here is
    persisted as a not_applicable sentinel, settling it after one try."""
    storage, _manifest, scraper = make_scraper(tmp_path)
    record = openfda_pma_record("P160035")
    responses.add(
        responses.GET,
        "https://www.accessdata.fda.gov/cdrh_docs/pdf16/P160035A.pdf",
        body="Not Found",
        status=404,
        content_type="text/html",
    )

    assert scraper.already_have("P160035/order") is False
    scraper._fetch_order("P160035", record)  # must not raise

    assert not storage.exists("fda/pma/documents/P160035/order/current.pdf")
    assert scraper.already_have("P160035/order") is True
    meta = json.loads(storage.read_text("fda/pma/documents/P160035/order/current.meta.json"))
    assert meta["source_metadata"]["not_applicable"] is True


def test_unparseable_pma_number_is_recorded_and_does_not_stop_the_run(tmp_path):
    storage, _manifest, scraper = make_scraper(tmp_path)
    record = openfda_pma_record("weird-id")

    scraper._fetch_order("weird-id", record)  # must not raise

    events = list(storage.list("fda/pma/_manifest/events"))
    assert len(events) == 1
    payload = json.loads(storage.read_text(events[0]))
    assert payload["event"] == "error"


@responses.activate
def test_already_known_metadata_and_order_are_skipped_without_network_call(tmp_path):
    _storage, _manifest, scraper = make_scraper(tmp_path)
    record = openfda_pma_record("P160035")
    responses.add(responses.GET, ENDPOINT, json={"results": [record]}, status=200)
    responses.add(responses.GET, ENDPOINT, json={"results": []}, status=200)
    responses.add(
        responses.GET,
        "https://www.accessdata.fda.gov/cdrh_docs/pdf16/P160035A.pdf",
        body=b"%PDF-1.4 fake pdf bytes", status=200, content_type="application/pdf",
    )
    summary1 = scraper.run()
    assert summary1.new == 2  # metadata + order letter
    assert summary1.stop_reason == "completed"

    responses.reset()
    responses.add(responses.GET, ENDPOINT, json={"results": [record]}, status=200)
    responses.add(responses.GET, ENDPOINT, json={"results": []}, status=200)
    _storage2, _manifest2, scraper2 = make_scraper(tmp_path)
    summary2 = scraper2.run()
    assert summary2.skipped_already_known == 1
    assert summary2.new == 0


@responses.activate
def test_budget_stops_run_after_max_new_order_letters(tmp_path):
    storage, _manifest, scraper = make_scraper(tmp_path, max_new_documents=1)
    records = [openfda_pma_record("P160035"), openfda_pma_record("P170099")]
    responses.add(responses.GET, ENDPOINT, json={"results": records}, status=200)
    responses.add(
        responses.GET, "https://www.accessdata.fda.gov/cdrh_docs/pdf16/P160035A.pdf",
        body=b"%PDF-1.4 one", status=200, content_type="application/pdf",
    )
    # No mock registered for P170099's PDF — if the budget cap didn't stop
    # the run, this would raise ConnectionError from `responses`.

    summary = scraper.run()

    assert summary.new == 2  # P160035's metadata + order letter
    assert summary.stop_reason == "budget_reached"
    assert storage.exists("fda/pma/documents/P160035/order/current.pdf")
    assert not storage.exists("fda/pma/documents/P170099/order/current.pdf")


@responses.activate
def test_hard_stop_ends_the_whole_run_not_just_one_record(tmp_path):
    storage, _manifest, scraper = make_scraper(tmp_path)
    records = [openfda_pma_record("P160035"), openfda_pma_record("P170099")]
    responses.add(responses.GET, ENDPOINT, json={"results": records}, status=200)
    responses.add(
        responses.GET, "https://www.accessdata.fda.gov/cdrh_docs/pdf16/P160035A.pdf",
        body="<html>FDA Apology</html>", status=404, content_type="text/html",
    )
    # No mock for P170099 at all — proves the run stopped entirely.

    summary = scraper.run()

    assert summary.stop_reason == "hard_stop"
    assert summary.new == 1  # only P160035's metadata; its PDF failed
    assert storage.exists("fda/pma/documents/P160035/metadata/current.json")
    assert not storage.exists("fda/pma/documents/P170099/metadata/current.json")


@responses.activate
def test_estimate_reports_next_available_at_when_outside_visiting_hours(tmp_path, monkeypatch):
    from datetime import UTC, datetime

    from qara_reg_scraper.config import HttpSettings as _HttpSettings
    from qara_reg_scraper.http_client import PoliteHttpClient as _PoliteHttpClient
    from qara_reg_scraper.manifest import Manifest as _Manifest
    from qara_reg_scraper.storage.local import LocalStorage as _LocalStorage

    responses.add(responses.GET, ENDPOINT, json={"results": []}, status=200)
    responses.add(
        responses.GET, "https://www.accessdata.fda.gov/robots.txt",
        body="User-agent: *\nVisiting-hours: 23:00EDT-05:00EDT\n", status=200,
    )
    monkeypatch.setattr(
        "qara_reg_scraper.http_client.now_utc",
        lambda: datetime(2026, 6, 15, 12, 0, tzinfo=UTC),  # outside the window
    )

    storage = _LocalStorage(root=str(tmp_path))
    manifest = _Manifest(storage, "fda", "pma", run_id="test-run")
    http = _PoliteHttpClient(
        _HttpSettings(requests_per_second=1000, respect_robots_txt=True, max_retries=1), "pma",
    )
    scraper = PmaScraper(http, manifest)

    info = scraper.estimate()

    assert info.next_available_at == datetime(2026, 6, 16, 3, 0, tzinfo=UTC)
