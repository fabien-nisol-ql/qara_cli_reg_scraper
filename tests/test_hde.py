import json

import responses

from qara_reg_scraper.config import HttpSettings
from qara_reg_scraper.http_client import PoliteHttpClient
from qara_reg_scraper.manifest import Manifest
from qara_reg_scraper.regulations.fda.hde import LISTING_URL, HdeScraper, _parse_rows
from qara_reg_scraper.storage.local import LocalStorage

LISTING_HTML = """
<table>
<thead><tr>
<th>Approval Date</th><th>HDE Numberand Docket Number</th><th>Device Name</th>
<th>Company Name and Address</th><th>Device Description / Device Indications</th>
</tr></thead>
<tbody>
<tr role="row">
<td>08/14/2026</td>
<td><a href="https://www.accessdata.fda.gov/scripts/cdrh/cfdocs/cfhde/hde.cfm?id=467600">H200002</a></td>
<td>OncoSil&trade;</td>
<td>OncoSil Medical USA, Inc.<br>215 E Woodland Rd.<br>Lake Bluff, IL 60044</td>
<td>Treatment of distal cholangiocarcinoma.</td>
</tr>
<tr role="row">
<td>H000007<br>05-Apr-2002<br>02M-0167</td>
<td>H000007<br>05-Apr-2002<br>02M-0167</td>
<td>Amplatzer&reg; PFO Occluder</td>
<td>AGA Medical Corporation<br>Golden Valley, MN</td>
<td>This document has been withdrawn as of October 31, 2006.</td>
</tr>
</tbody>
</table>
"""


def make_scraper(tmp_path, **kwargs):
    storage = LocalStorage(root=str(tmp_path))
    manifest = Manifest(storage, "fda", "hde", run_id="test-run")
    http = PoliteHttpClient(
        HttpSettings(requests_per_second=1000, respect_robots_txt=False, max_retries=1),
        "hde",
    )
    return storage, manifest, HdeScraper(http, manifest, **kwargs)


def test_parse_rows_extracts_a_normal_linked_row():
    rows = _parse_rows(LISTING_HTML)
    linked = next(r for r in rows if r["hde_number"] == "H200002")
    assert linked["approval_date"] == "08/14/2026"
    assert linked["device_name"] == "OncoSil™"
    assert "Lake Bluff" in linked["company"]
    assert linked["has_detail_page"] is True


def test_parse_rows_extracts_a_withdrawn_row_with_duplicated_cells_and_no_link():
    rows = _parse_rows(LISTING_HTML)
    withdrawn = next(r for r in rows if r["hde_number"] == "H000007")
    assert withdrawn["device_name"] == "Amplatzer® PFO Occluder"
    assert withdrawn["has_detail_page"] is False


def test_parse_rows_returns_empty_list_when_no_table_present():
    assert _parse_rows("<p>no table here</p>") == []


@responses.activate
def test_metadata_and_order_letter_both_saved_for_a_linked_row(tmp_path):
    storage, _manifest, scraper = make_scraper(tmp_path)
    row = _parse_rows(LISTING_HTML)[0]
    responses.add(
        responses.GET,
        "https://www.accessdata.fda.gov/cdrh_docs/pdf20/H200002A.pdf",
        body=b"%PDF-1.4 fake pdf bytes",
        status=200,
        content_type="application/pdf",
    )

    scraper._save_metadata(row)
    scraper._fetch_order(row)

    assert storage.exists("fda/hde/documents/H200002/metadata/current.json")
    meta = json.loads(storage.read_text("fda/hde/documents/H200002/metadata/current.json"))
    assert meta["device_name"] == "OncoSil™"

    assert storage.exists("fda/hde/documents/H200002/order/current.pdf")
    assert storage.read_bytes("fda/hde/documents/H200002/order/current.pdf") == b"%PDF-1.4 fake pdf bytes"


def test_withdrawn_row_gets_not_applicable_order_without_any_network_call(tmp_path):
    """No `responses.activate` at all — if this tried an HTTP call it
    raises, proving a row known in advance to have no detail page is
    never blindly attempted."""
    storage, _manifest, scraper = make_scraper(tmp_path)
    row = _parse_rows(LISTING_HTML)[1]  # H000007, withdrawn, no link

    scraper._fetch_order(row)

    assert not storage.exists("fda/hde/documents/H000007/order/current.pdf")
    assert scraper.already_have("H000007/order") is True
    meta = json.loads(storage.read_text("fda/hde/documents/H000007/order/current.meta.json"))
    assert meta["source_metadata"]["not_applicable"] is True


@responses.activate
def test_a_linked_row_whose_order_letter_404s_is_persisted_as_not_applicable(tmp_path):
    """A row with a real detail-page link can still turn out to have no
    order letter posted (confirmed live for pma.py's own equivalent case)
    — this must settle as not_applicable, not a bare error that
    already_have would never recognize (see the module docstring)."""
    storage, _manifest, scraper = make_scraper(tmp_path)
    row = _parse_rows(LISTING_HTML)[0]  # H200002, has a detail-page link
    responses.add(
        responses.GET,
        "https://www.accessdata.fda.gov/cdrh_docs/pdf20/H200002A.pdf",
        body="Not Found",
        status=404,
        content_type="text/html",
    )

    assert scraper.already_have("H200002/order") is False
    scraper._fetch_order(row)  # must not raise

    assert not storage.exists("fda/hde/documents/H200002/order/current.pdf")
    assert scraper.already_have("H200002/order") is True
    meta = json.loads(storage.read_text("fda/hde/documents/H200002/order/current.meta.json"))
    assert meta["source_metadata"]["not_applicable"] is True


@responses.activate
def test_already_known_metadata_and_order_are_skipped_on_a_second_run(tmp_path):
    responses.add(responses.GET, LISTING_URL, body=LISTING_HTML, status=200)
    responses.add(
        responses.GET, "https://www.accessdata.fda.gov/cdrh_docs/pdf20/H200002A.pdf",
        body=b"%PDF-1.4 fake pdf bytes", status=200, content_type="application/pdf",
    )
    _storage, _manifest, scraper = make_scraper(tmp_path)
    summary1 = scraper.run()
    # H200002's metadata + order, H000007's metadata + not_applicable order.
    assert summary1.new == 4
    assert summary1.stop_reason == "completed"

    responses.reset()
    responses.add(responses.GET, LISTING_URL, body=LISTING_HTML, status=200)
    _storage2, _manifest2, scraper2 = make_scraper(tmp_path)
    summary2 = scraper2.run()
    assert summary2.skipped_already_known == 2  # both hde_numbers' order documents
    assert summary2.new == 0


@responses.activate
def test_budget_stops_run_after_max_new_order_letters(tmp_path):
    storage, _manifest, scraper = make_scraper(tmp_path, max_new_documents=1)
    responses.add(responses.GET, LISTING_URL, body=LISTING_HTML, status=200)
    responses.add(
        responses.GET, "https://www.accessdata.fda.gov/cdrh_docs/pdf20/H200002A.pdf",
        body=b"%PDF-1.4 fake pdf bytes", status=200, content_type="application/pdf",
    )
    # No mock for H000007's not_applicable path needed — it never touches the network.

    summary = scraper.run()

    assert summary.stop_reason == "budget_reached"
    assert storage.exists("fda/hde/documents/H200002/order/current.pdf")


@responses.activate
def test_a_bot_block_ends_the_whole_run_not_just_one_record(tmp_path):
    storage, _manifest, scraper = make_scraper(tmp_path)
    responses.add(responses.GET, LISTING_URL, body=LISTING_HTML, status=200)
    responses.add(
        responses.GET, "https://www.accessdata.fda.gov/cdrh_docs/pdf20/H200002A.pdf",
        body="<html>FDA Apology</html>", status=404, content_type="text/html",
    )

    summary = scraper.run()

    assert summary.stop_reason == "bot_block"
    assert storage.exists("fda/hde/documents/H200002/metadata/current.json")
    # H000007 (parsed second) never reached — the run stopped at H200002's block.
    assert not storage.exists("fda/hde/documents/H000007/metadata/current.json")


@responses.activate
def test_listing_fetch_failure_is_recorded_and_hard_stops(tmp_path):
    storage, _manifest, scraper = make_scraper(tmp_path)
    responses.add(responses.GET, LISTING_URL, status=500)

    summary = scraper.run()

    assert summary.stop_reason == "hard_stop"
    events = list(storage.list("fda/hde/_manifest/events"))
    assert len(events) == 1
    payload = json.loads(storage.read_text(events[0]))
    assert payload["document_id"] == "__listing__"


@responses.activate
def test_estimate_reports_next_available_at_when_outside_visiting_hours(tmp_path, monkeypatch):
    from datetime import UTC, datetime

    responses.add(responses.GET, LISTING_URL, body=LISTING_HTML, status=200)
    responses.add(
        responses.GET, "https://www.accessdata.fda.gov/robots.txt",
        body="User-agent: *\nVisiting-hours: 23:00EDT-05:00EDT\n", status=200,
    )
    monkeypatch.setattr(
        "qara_reg_scraper.http_client.now_utc",
        lambda: datetime(2026, 6, 15, 12, 0, tzinfo=UTC),  # outside the window
    )

    storage = LocalStorage(root=str(tmp_path))
    manifest = Manifest(storage, "fda", "hde", run_id="test-run")
    http = PoliteHttpClient(
        HttpSettings(requests_per_second=1000, respect_robots_txt=True, max_retries=1), "hde",
    )
    scraper = HdeScraper(http, manifest)

    info = scraper.estimate()

    assert info.next_available_at == datetime(2026, 6, 16, 3, 0, tzinfo=UTC)
