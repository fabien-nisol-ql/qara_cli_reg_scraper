import json

import pytest
import responses

from qara_reg_scraper.base_scraper import BudgetExhausted, HardStop
from qara_reg_scraper.config import HttpSettings
from qara_reg_scraper.http_client import PoliteHttpClient
from qara_reg_scraper.manifest import Manifest
from qara_reg_scraper.regulations.fda.clearances_510k import ENDPOINT, Clearances510kScraper
from qara_reg_scraper.storage.local import LocalStorage


def make_scraper(tmp_path, **kwargs):
    storage = LocalStorage(root=str(tmp_path))
    manifest = Manifest(storage, "fda", "clearances_510k", run_id="test-run")
    http = PoliteHttpClient(
        HttpSettings(requests_per_second=1000, respect_robots_txt=False, max_retries=1),
        "clearances_510k",
    )
    return storage, manifest, Clearances510kScraper(http, manifest, **kwargs)


def openfda_record(k_number: str, statement_or_summary: str = "Summary") -> dict:
    return {
        "k_number": k_number,
        "device_name": f"Test Device {k_number}",
        "applicant": "Acme Devices Inc.",
        "decision_date": "2026-07-25",
        "decision_code": "SESE",
        "product_code": "ABC",
        "clearance_type": "Traditional",
        "statement_or_summary": statement_or_summary,
    }


def test_effective_lookback_days_defaults_to_thirty(tmp_path):
    _storage, _manifest, scraper = make_scraper(tmp_path)
    assert scraper.effective_lookback_days == 30


def test_effective_lookback_days_respects_override(tmp_path):
    _storage, _manifest, scraper = make_scraper(tmp_path, lookback_days=90)
    assert scraper.effective_lookback_days == 90


@responses.activate
def test_lookback_days_override_changes_the_openfda_query_window(tmp_path):
    """The actual point of --lookback-days / config's lookback_days: a
    wider window must be reflected in the real openFDA query, not just in
    the effective_lookback_days property."""
    import re
    from datetime import UTC, datetime, timedelta
    from urllib.parse import unquote

    _storage, _manifest, scraper = make_scraper(tmp_path, lookback_days=90)
    responses.add(responses.GET, ENDPOINT, json={"results": []}, status=200)

    scraper.run()

    request_url = unquote(responses.calls[0].request.url)
    expected_start = (datetime.now(UTC).date() - timedelta(days=90)).strftime("%Y-%m-%d")
    match = re.search(r"decision_date:\[([\d-]+)", request_url)
    assert match is not None
    assert match.group(1) == expected_start


@responses.activate
def test_metadata_and_summary_pdf_both_saved(tmp_path):
    storage, _manifest, scraper = make_scraper(tmp_path)
    record = openfda_record("K261367")
    responses.add(
        responses.GET, ENDPOINT, json={"results": [record]}, status=200,
    )
    responses.add(
        responses.GET,
        "https://www.accessdata.fda.gov/cdrh_docs/pdf26/K261367.pdf",
        body=b"%PDF-1.4 fake pdf bytes",
        status=200,
        content_type="application/pdf",
    )

    for rec in [record]:
        scraper._save_metadata(rec["k_number"], rec)
        scraper._fetch_summary_pdf(rec["k_number"], rec)

    assert storage.exists("fda/clearances_510k/documents/K261367/metadata/current.json")
    meta = json.loads(storage.read_text("fda/clearances_510k/documents/K261367/metadata/current.json"))
    assert meta["device_name"] == "Test Device K261367"

    assert storage.exists("fda/clearances_510k/documents/K261367/summary/current.pdf")
    assert storage.read_bytes("fda/clearances_510k/documents/K261367/summary/current.pdf") == b"%PDF-1.4 fake pdf bytes"
    summary_meta = json.loads(
        storage.read_text("fda/clearances_510k/documents/K261367/summary/current.meta.json")
    )
    assert summary_meta["content_type"] == "application/pdf"


def test_statement_type_skips_pdf_fetch_without_network_call(tmp_path):
    storage, _manifest, scraper = make_scraper(tmp_path)
    record = openfda_record("K261999", statement_or_summary="Statement")

    # No `responses.activate` at all — if this tries an HTTP call it raises,
    # proving we never attempt a fetch for a non-"Summary" record.
    scraper._fetch_summary_pdf(record["k_number"], record)

    assert not storage.exists("fda/clearances_510k/documents/K261999/summary/current.pdf")
    events = list(storage.list("fda/clearances_510k/_manifest/events"))
    assert len(events) == 1
    payload = json.loads(storage.read_text(events[0]))
    assert payload["event"] == "new"


def test_statement_type_persists_a_marker_so_already_have_recognizes_it(tmp_path):
    """Regression test for a real bug: the not_applicable case used to only
    call record_event, which never creates the current.meta.json sidecar
    already_have() checks — so a Statement clearance was silently
    re-derived and re-logged on every single run forever, contradicting
    this source's own documented "never attempted again," and permanently
    inflating estimate()'s "remaining" count by exactly the number of
    Statement clearances in the window (confirmed against a real
    documents/available/remaining triple that was only reconcilable once
    every Statement clearance was assumed excluded from already_known)."""
    storage, _manifest, scraper = make_scraper(tmp_path)
    record = openfda_record("K261999", statement_or_summary="Statement")

    assert scraper.already_have("K261999/summary") is False
    scraper._fetch_summary_pdf(record["k_number"], record)
    assert scraper.already_have("K261999/summary") is True

    assert storage.exists("fda/clearances_510k/documents/K261999/summary/current.meta.json")
    meta = json.loads(storage.read_text("fda/clearances_510k/documents/K261999/summary/current.meta.json"))
    assert meta["source_metadata"]["not_applicable"] is True

    # A second call — as a real second run's listing walk would trigger,
    # since it derives the same k_number again — must not re-fetch or
    # re-log a duplicate "new" event; run()'s own already_have() check is
    # what actually prevents _fetch_summary_pdf from being called again in
    # practice, but this confirms the persisted state itself is stable and
    # idempotent (content-hash "unchanged", not a fresh "new").
    scraper._fetch_summary_pdf(record["k_number"], record)
    events = [
        json.loads(storage.read_text(p))
        for p in sorted(storage.list("fda/clearances_510k/_manifest/events"))
    ]
    assert [e["event"] for e in events] == ["new", "unchanged"]


def test_statement_type_is_skipped_entirely_on_a_second_run(tmp_path):
    """The actual end-to-end fix: once already_have() recognizes a
    Statement clearance (after one run has persisted its marker),
    subsequent runs must skip it via run()'s own already_have() check —
    not just tolerate being called again."""
    _storage, _manifest, scraper = make_scraper(tmp_path)
    record = openfda_record("K261999", statement_or_summary="Statement")
    scraper._fetch_summary_pdf(record["k_number"], record)

    _storage2, _manifest2, scraper2 = make_scraper(tmp_path)
    assert scraper2.already_have("K261999/summary") is True  # run()'s skip check would fire


@responses.activate
def test_blocked_response_raises_hard_stop_and_is_recorded(tmp_path):
    """A block must stop the run (HardStop), not be swallowed — but the
    error is still recorded in the manifest before it propagates."""
    storage, _manifest, scraper = make_scraper(tmp_path)
    record = openfda_record("K261367")
    # Simulate the Akamai "apology" page: 404 with an HTML body instead of
    # the PDF (see the maintenance note in clearances_510k.py).
    responses.add(
        responses.GET,
        "https://www.accessdata.fda.gov/cdrh_docs/pdf26/K261367.pdf",
        body="<html>FDA Apology</html>",
        status=404,
        content_type="text/html",
    )

    with pytest.raises(HardStop):
        scraper._fetch_summary_pdf(record["k_number"], record)

    assert not storage.exists("fda/clearances_510k/documents/K261367/summary/current.pdf")
    events = list(storage.list("fda/clearances_510k/_manifest/events"))
    assert len(events) == 1
    payload = json.loads(storage.read_text(events[0]))
    assert payload["event"] == "error"
    assert "status=404" in payload["error"]
    assert "bot-management" in payload["error"]  # matched "FDA Apology" marker


@responses.activate
def test_bot_block_is_logged_to_the_console_not_just_the_manifest(tmp_path, caplog):
    """Regression test: a block used to only be recorded in the manifest
    file, silent on stdout/docker logs unless you went looking. It must
    now show up in the scraper's own logger in real time."""
    _storage, _manifest, scraper = make_scraper(tmp_path)
    record = openfda_record("K261367")
    responses.add(
        responses.GET,
        "https://www.accessdata.fda.gov/cdrh_docs/pdf26/K261367.pdf",
        body="<html>FDA Apology</html>",
        status=404,
        content_type="text/html",
    )

    with caplog.at_level("WARNING", logger="qara_reg_scraper.fda.clearances_510k"), pytest.raises(HardStop):
        scraper._fetch_summary_pdf(record["k_number"], record)

    assert any(r.getMessage() == "bot_detection_suspected" for r in caplog.records)


@responses.activate
def test_non_bot_unexpected_response_is_recorded_and_does_not_stop_the_run(tmp_path, caplog):
    """A clean 4xx that isn't a bot-block signal is routine (one specific
    document is unavailable) — it must NOT raise HardStop, just record and
    return so the caller moves on to the next candidate."""
    _storage, _manifest, scraper = make_scraper(tmp_path)
    record = openfda_record("K261367")
    # 403 (not one of the retried statuses) with a JSON body — genuinely
    # unexpected, but nothing about it resembles a bot-block challenge page.
    responses.add(
        responses.GET,
        "https://www.accessdata.fda.gov/cdrh_docs/pdf26/K261367.pdf",
        json={"error": "forbidden"},
        status=403,
        content_type="application/json",
    )

    with caplog.at_level("WARNING", logger="qara_reg_scraper.fda.clearances_510k"):
        scraper._fetch_summary_pdf(record["k_number"], record)  # must not raise

    messages = [r.getMessage() for r in caplog.records]
    assert "unexpected_pdf_response" in messages
    assert "bot_detection_suspected" not in messages


@responses.activate
def test_a_real_plain_404_is_recorded_and_does_not_stop_the_run(tmp_path):
    """Regression test for the actual bug hit in production: a genuine
    404 (no Akamai markers — not a block) for one k-number was stopping
    the entire run. It must now just record an error and return."""
    storage, _manifest, scraper = make_scraper(tmp_path)
    record = openfda_record("K253277")
    responses.add(
        responses.GET,
        "https://www.accessdata.fda.gov/cdrh_docs/pdf25/K253277.pdf",
        body="<html><body>Not Found</body></html>",
        status=404,
        content_type="text/html",
    )

    scraper._fetch_summary_pdf(record["k_number"], record)  # must not raise

    assert not storage.exists("fda/clearances_510k/documents/K253277/summary/current.pdf")
    events = list(storage.list("fda/clearances_510k/_manifest/events"))
    assert len(events) == 1
    payload = json.loads(storage.read_text(events[0]))
    assert payload["event"] == "error"
    assert "bot-management" not in payload["error"]


def test_unparseable_k_number_is_recorded_and_does_not_stop_the_run(tmp_path):
    storage, _manifest, scraper = make_scraper(tmp_path)
    record = openfda_record("weird-id")

    scraper._fetch_summary_pdf(record["k_number"], record)  # must not raise

    events = list(storage.list("fda/clearances_510k/_manifest/events"))
    assert len(events) == 1
    payload = json.loads(storage.read_text(events[0]))
    assert payload["event"] == "error"


@responses.activate
def test_already_known_metadata_and_summary_are_skipped_without_network_call(tmp_path):
    """Second run of the same k-number: no HTTP calls at all beyond the
    listing page itself — proves the "don't redownload what we already
    have" behavior end-to-end through run(), not just the unit pieces."""
    _storage, _manifest, scraper = make_scraper(tmp_path)
    record = openfda_record("K261367")
    # First page: the one record. Second page: empty — the real signal
    # `iter_openfda_results` uses to stop paginating (`responses` replays
    # the last-registered response forever otherwise, which would make this
    # loop through the same "record" up to `max_records` times).
    responses.add(responses.GET, ENDPOINT, json={"results": [record]}, status=200)
    responses.add(responses.GET, ENDPOINT, json={"results": []}, status=200)
    responses.add(
        responses.GET,
        "https://www.accessdata.fda.gov/cdrh_docs/pdf26/K261367.pdf",
        body=b"%PDF-1.4 fake pdf bytes", status=200, content_type="application/pdf",
    )
    summary1 = scraper.run()
    assert summary1.new == 2  # the metadata document AND the summary PDF, each a separate "new" event
    assert summary1.stop_reason == "completed"

    # Second run, same storage root (same tmp_path): only the listing calls
    # are registered — if either the metadata or the PDF were re-fetched,
    # `responses` would raise ConnectionError for the unregistered request.
    responses.reset()
    responses.add(responses.GET, ENDPOINT, json={"results": [record]}, status=200)
    responses.add(responses.GET, ENDPOINT, json={"results": []}, status=200)
    _storage2, _manifest2, scraper2 = make_scraper(tmp_path)
    summary2 = scraper2.run()
    assert summary2.skipped_already_known == 1
    assert summary2.new == 0


def test_zero_budget_skips_pdf_fetch_without_network_call(tmp_path):
    """max_new_documents=0 must not fetch even the first PDF. No
    `responses.activate` at all — an attempted network call would raise
    ConnectionError, proving the guard fires before self.http.get()."""
    storage, _manifest, scraper = make_scraper(tmp_path, max_new_documents=0)
    record = openfda_record("K261367")

    with pytest.raises(BudgetExhausted):
        scraper._fetch_summary_pdf(record["k_number"], record)

    assert not storage.exists("fda/clearances_510k/documents/K261367/summary/current.pdf")


def test_zero_budget_still_records_a_statement_type_as_not_applicable(tmp_path):
    """The budget guard sits after the free statement_or_summary check, not
    before it: a "Statement" clearance never touches the network or the
    budget either way, so max_new_documents=0 shouldn't stop it from being
    recorded — only an actual PDF fetch."""
    storage, _manifest, scraper = make_scraper(tmp_path, max_new_documents=0)
    record = openfda_record("K261999", statement_or_summary="Statement")

    scraper._fetch_summary_pdf(record["k_number"], record)  # must not raise

    events = list(storage.list("fda/clearances_510k/_manifest/events"))
    assert len(events) == 1
    payload = json.loads(storage.read_text(events[0]))
    assert payload["event"] == "new"
    assert scraper.already_have("K261999/summary") is True


@responses.activate
def test_budget_stops_run_after_max_new_pdfs(tmp_path):
    storage, _manifest, scraper = make_scraper(tmp_path, max_new_documents=1)
    records = [openfda_record("K261111"), openfda_record("K261222")]
    responses.add(responses.GET, ENDPOINT, json={"results": records}, status=200)
    responses.add(
        responses.GET, "https://www.accessdata.fda.gov/cdrh_docs/pdf26/K261111.pdf",
        body=b"%PDF-1.4 one", status=200, content_type="application/pdf",
    )
    # No mock registered for K261222's PDF — if the budget cap didn't stop
    # the run, this would raise ConnectionError from `responses`.

    summary = scraper.run()

    assert summary.new == 2  # K261111's metadata + summary PDF; the budget cap fired right after
    assert summary.stop_reason == "budget_reached"
    assert storage.exists("fda/clearances_510k/documents/K261111/summary/current.pdf")
    assert not storage.exists("fda/clearances_510k/documents/K261222/summary/current.pdf")


@responses.activate
def test_a_bot_block_ends_the_whole_run_not_just_one_record(tmp_path):
    storage, _manifest, scraper = make_scraper(tmp_path)
    records = [openfda_record("K261111"), openfda_record("K261222")]
    responses.add(responses.GET, ENDPOINT, json={"results": records}, status=200)
    responses.add(
        responses.GET, "https://www.accessdata.fda.gov/cdrh_docs/pdf26/K261111.pdf",
        body="<html>FDA Apology</html>", status=404, content_type="text/html",
    )
    # No mock for K261222 at all — proves the run stopped entirely at
    # K261111's failure rather than skipping it and moving on.

    summary = scraper.run()

    # bot_block specifically, not just "hard_stop" - see BotBlockDetected's
    # own docstring in base_scraper.py for why the distinction matters
    # (never retried in-process, unlike other hard-stop causes).
    assert summary.stop_reason == "bot_block"
    assert summary.new == 1  # only K261111's metadata; its PDF failed, K261222 was never reached
    assert storage.exists("fda/clearances_510k/documents/K261111/metadata/current.json")
    assert not storage.exists("fda/clearances_510k/documents/K261222/metadata/current.json")
    assert not storage.exists("fda/clearances_510k/documents/K261222/summary/current.pdf")


@responses.activate
def test_run_continues_past_a_routine_404_to_the_next_clearance(tmp_path):
    """The actual production scenario: one k-number's PDF is a genuine
    404 (not a block), the next one is fine. The whole run must complete,
    not stop at the first miss."""
    storage, _manifest, scraper = make_scraper(tmp_path)
    records = [openfda_record("K261111"), openfda_record("K261222")]
    responses.add(responses.GET, ENDPOINT, json={"results": records}, status=200)
    responses.add(responses.GET, ENDPOINT, json={"results": []}, status=200)
    responses.add(
        responses.GET, "https://www.accessdata.fda.gov/cdrh_docs/pdf26/K261111.pdf",
        body="Not Found", status=404, content_type="text/html",
    )
    responses.add(
        responses.GET, "https://www.accessdata.fda.gov/cdrh_docs/pdf26/K261222.pdf",
        body=b"%PDF-1.4 two", status=200, content_type="application/pdf",
    )

    summary = scraper.run()

    assert summary.stop_reason == "completed"
    assert summary.errors == 1  # K261111's PDF miss, recorded
    assert not storage.exists("fda/clearances_510k/documents/K261111/summary/current.pdf")
    assert storage.exists("fda/clearances_510k/documents/K261222/summary/current.pdf")  # reached and saved


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
    manifest = _Manifest(storage, "fda", "clearances_510k", run_id="test-run")
    http = _PoliteHttpClient(
        _HttpSettings(requests_per_second=1000, respect_robots_txt=True, max_retries=1),
        "clearances_510k",
    )
    scraper = Clearances510kScraper(http, manifest)

    info = scraper.estimate()

    assert info.next_available_at == datetime(2026, 6, 16, 3, 0, tzinfo=UTC)
