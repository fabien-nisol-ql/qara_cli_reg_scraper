import json

import pytest
import responses

from qara_reg_scraper.base_scraper import BaseScraper, BudgetExhausted, HardStop, PreviewInfo
from qara_reg_scraper.config import HttpSettings
from qara_reg_scraper.http_client import PoliteHttpClient
from qara_reg_scraper.manifest import Manifest
from qara_reg_scraper.storage.local import LocalStorage


class _DummyScraper(BaseScraper):
    regulation = "test"
    name = "dummy"

    def run(self):  # pragma: no cover - not exercised here
        raise NotImplementedError


def make_scraper(tmp_path, **kwargs):
    storage = LocalStorage(root=str(tmp_path))
    manifest = Manifest(storage, "test", "dummy", run_id="test-run")
    http = PoliteHttpClient(
        HttpSettings(requests_per_second=1000, respect_robots_txt=False, max_retries=1), "dummy"
    )
    return storage, manifest, _DummyScraper(http, manifest, **kwargs)


@responses.activate
def test_fetch_and_save_stores_a_normal_page(tmp_path):
    storage, _manifest, scraper = make_scraper(tmp_path)
    responses.add(
        responses.GET,
        "https://example.gov/guidance/doc-1",
        body="<html><body>a perfectly normal, several-kilobyte guidance document " + "x" * 5000 + "</body></html>",
        status=200,
        content_type="text/html",
    )
    scraper.fetch_and_save(
        document_id="doc-1", url="https://example.gov/guidance/doc-1",
        title="Doc 1", ext="html", content_type=None,
    )
    assert storage.exists("test/dummy/documents/doc-1/current.html")


@responses.activate
def test_fetch_and_save_records_original_filename_from_content_disposition(tmp_path):
    storage, _manifest, scraper = make_scraper(tmp_path)
    responses.add(
        responses.GET,
        "https://example.gov/download?id=doc-2",
        body=b"%PDF-1.4 ...",
        status=200,
        content_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="Doc 2 Summary.pdf"'},
    )
    scraper.fetch_and_save(
        document_id="doc-2", url="https://example.gov/download?id=doc-2",
        title="Doc 2", ext="pdf", content_type=None,
    )
    meta = json.loads(storage.read_text("test/dummy/documents/doc-2/current.meta.json"))
    assert meta["original_filename"] == "Doc 2 Summary.pdf"


@responses.activate
def test_fetch_and_save_falls_back_to_url_for_original_filename(tmp_path):
    storage, _manifest, scraper = make_scraper(tmp_path)
    responses.add(
        responses.GET,
        "https://example.gov/docs/doc-3.pdf",
        body=b"%PDF-1.4 ...",
        status=200,
        content_type="application/pdf",
    )
    scraper.fetch_and_save(
        document_id="doc-3", url="https://example.gov/docs/doc-3.pdf",
        title="Doc 3", ext="pdf", content_type=None,
    )
    meta = json.loads(storage.read_text("test/dummy/documents/doc-3/current.meta.json"))
    assert meta["original_filename"] == "doc-3.pdf"


@responses.activate
def test_fetch_and_save_does_not_silently_archive_a_200_block_page(tmp_path, caplog):
    """This is the dangerous case a plain raise_for_status() check misses:
    a bot-management challenge served with a normal 200 status. Before this
    fix, fetch_and_save would have saved the apology page as if it were the
    real document, with no error and no log line at all. Now it must raise
    HardStop (not just record-and-continue) so the whole run stops."""
    storage, _manifest, scraper = make_scraper(tmp_path)
    responses.add(
        responses.GET,
        "https://example.gov/guidance/doc-2",
        body="<html><title>FDA Apology</title>Excessive requests, please slow down.</html>",
        status=200,
        content_type="text/html",
    )

    with caplog.at_level("WARNING", logger="qara_reg_scraper.test.dummy"), pytest.raises(HardStop):
        scraper.fetch_and_save(
            document_id="doc-2", url="https://example.gov/guidance/doc-2",
            title="Doc 2", ext="html", content_type=None,
        )

    assert not storage.exists("test/dummy/documents/doc-2/current.html")
    assert any(r.getMessage() == "bot_detection_suspected" for r in caplog.records)

    events = list(storage.list("test/dummy/_manifest/events"))
    assert len(events) == 1
    payload = json.loads(storage.read_text(events[0]))
    assert payload["event"] == "error"
    assert "bot-management" in payload["error"]


@responses.activate
def test_fetch_and_save_raises_hard_stop_on_exhausted_retries(tmp_path):
    storage, _manifest, scraper = make_scraper(tmp_path)
    responses.add(responses.GET, "https://example.gov/doc-3", status=500)

    with pytest.raises(HardStop):
        scraper.fetch_and_save(
            document_id="doc-3", url="https://example.gov/doc-3",
            title="Doc 3", ext="html", content_type=None,
        )
    events = list(storage.list("test/dummy/_manifest/events"))
    payload = json.loads(storage.read_text(events[0]))
    assert payload["event"] == "error"


@responses.activate
def test_fetch_and_save_does_not_hard_stop_on_a_clean_404(tmp_path):
    """A plain 404 (not retried by PoliteHttpClient, not a bot-block) is
    routine — one dead link in a list of many. It must NOT raise HardStop;
    the caller should be able to move on to the next candidate."""
    storage, manifest, scraper = make_scraper(tmp_path)
    responses.add(
        responses.GET, "https://example.gov/guidance/gone",
        body="<html><body>Not Found</body></html>", status=404, content_type="text/html",
    )

    scraper.fetch_and_save(
        document_id="doc-gone", url="https://example.gov/guidance/gone",
        title="Doc Gone", ext="html", content_type=None,
    )  # must not raise

    assert not storage.exists("test/dummy/documents/doc-gone/current.html")
    events = list(storage.list("test/dummy/_manifest/events"))
    assert len(events) == 1
    payload = json.loads(storage.read_text(events[0]))
    assert payload["event"] == "error"
    assert manifest.summary.stop_reason == "completed"  # never touched by a non-hard-stop failure


def test_already_have_true_only_after_a_successful_save(tmp_path):
    _storage, manifest, scraper = make_scraper(tmp_path)
    assert scraper.already_have("doc-1") is False
    manifest.save_document(
        "doc-1", b"content", url="u", title="t", ext="html",
        content_type="text/html", http_status=200,
    )
    assert scraper.already_have("doc-1") is True


def test_already_have_respects_recheck_after_days(tmp_path):
    storage, manifest, scraper = make_scraper(tmp_path, recheck_after_days=30)
    manifest.save_document(
        "doc-1", b"content", url="u", title="t", ext="html",
        content_type="text/html", http_status=200,
    )
    # Freshly saved -> within the recheck window -> still considered "have".
    assert scraper.already_have("doc-1") is True

    # Backdate last_checked_at to simulate an old capture.
    import json as _json
    meta_path = "test/dummy/documents/doc-1/current.meta.json"
    meta = _json.loads(storage.read_text(meta_path))
    meta["last_checked_at"] = "2020-01-01T00:00:00+00:00"
    storage.write_text(meta_path, _json.dumps(meta))
    assert scraper.already_have("doc-1") is False


@responses.activate
def test_fetch_and_save_respects_zero_budget_without_fetching(tmp_path):
    """max_new_documents=0 must mean *no* new fetches at all, checked
    BEFORE the HTTP call — not just via a post-fetch budget check, which
    can't prevent the very first fetch when the budget is already zero.
    No responses mock is registered for this URL on purpose: if the guard
    were missing, the real (unmocked) request would be attempted and
    fetch_and_save would turn its failure into HardStop instead of
    BudgetExhausted — the wrong exception makes that regression obvious."""
    _storage, _manifest, scraper = make_scraper(tmp_path, max_new_documents=0)

    with pytest.raises(BudgetExhausted):
        scraper.fetch_and_save(
            document_id="doc-1", url="https://example.gov/doc-1",
            title="Doc 1", ext="html", content_type=None,
        )

    assert len(responses.calls) == 0


def test_process_candidates_with_zero_budget_never_calls_fetch_one(tmp_path):
    _storage, manifest, scraper = make_scraper(tmp_path, max_new_documents=0)
    fetched = []

    scraper.process_candidates(["new-1", "new-2"], lambda document_id: fetched.append(document_id))

    assert fetched == []
    assert manifest.summary.stop_reason == "budget_reached"


def test_budget_exhausted_after_max_new_documents(tmp_path):
    _storage, _manifest, scraper = make_scraper(tmp_path, max_new_documents=2)
    scraper._consume_budget()
    with pytest.raises(BudgetExhausted):
        scraper._consume_budget()


def test_time_budget_none_by_default_never_exhausts_on_its_own(tmp_path):
    """No time_budget_minutes passed - matches every existing caller,
    unaffected by this feature entirely."""
    _storage, _manifest, scraper = make_scraper(tmp_path, max_new_documents=1000)
    assert scraper._budget_exhausted() is False


def test_time_budget_exhausted_once_the_deadline_has_passed(tmp_path, monkeypatch):
    """The actual point: a retry-triggered job with an effectively
    unlimited document budget (max_new_documents=-1/None, matching
    ScrapeJobService#triggerRetry) must still stop cleanly once real
    wall-clock time runs out - confirmed live: at a correctly-enforced
    30s/request pace (accessdata.fda.gov's own robots.txt Hit-rate), an
    unbounded document count meant one job running for many hours before
    this existed."""
    _storage, _manifest, scraper = make_scraper(tmp_path, time_budget_minutes=10)
    assert scraper._budget_exhausted() is False  # deadline hasn't passed yet

    # Simulate 10+ minutes having elapsed without actually sleeping.
    monkeypatch.setattr("time.monotonic", lambda: scraper._deadline + 1)
    assert scraper._budget_exhausted() is True


def test_time_budget_raises_budget_exhausted_from_consume_budget_too(tmp_path, monkeypatch):
    """_consume_budget (called after every successful fetch) must also
    respect the deadline, not just the pre-fetch _budget_exhausted()
    check - same BudgetExhausted, same clean stop_reason=budget_reached
    handling every scraper already has for the document-count case."""
    _storage, _manifest, scraper = make_scraper(tmp_path, time_budget_minutes=10)
    monkeypatch.setattr("time.monotonic", lambda: scraper._deadline + 1)
    with pytest.raises(BudgetExhausted):
        scraper._consume_budget()


def test_time_budget_and_document_budget_are_independent_triggers(tmp_path, monkeypatch):
    """Whichever one fires first wins - both are checked, neither
    suppresses the other."""
    _storage, _manifest, scraper = make_scraper(tmp_path, max_new_documents=1000, time_budget_minutes=10)
    monkeypatch.setattr("time.monotonic", lambda: scraper._deadline + 1)
    assert scraper._budget_exhausted() is True  # time budget alone is enough, despite max_new_documents=1000


def test_process_candidates_skips_known_respects_budget_and_hard_stop(tmp_path):
    _storage, manifest, scraper = make_scraper(tmp_path, max_new_documents=2)
    # Pre-populate "known-1" so it's skipped without any "fetch" call.
    manifest.save_document(
        "known-1", b"x", url="u", title="t", ext="html", content_type="text/html", http_status=200,
    )

    fetched = []

    def fetch_one(document_id: str) -> None:
        fetched.append(document_id)
        manifest.save_document(
            document_id, b"x", url="u", title="t", ext="html",
            content_type="text/html", http_status=200,
        )
        scraper._consume_budget()

    scraper.process_candidates(["known-1", "new-1", "new-2", "new-3"], fetch_one)

    assert fetched == ["new-1", "new-2"]  # stopped after budget (2), never tried new-3
    assert manifest.summary.skipped_already_known == 1
    assert manifest.summary.stop_reason == "budget_reached"


def test_process_candidates_stops_entirely_on_hard_stop(tmp_path):
    _storage, manifest, scraper = make_scraper(tmp_path)
    attempted = []

    def fetch_one(document_id: str) -> None:
        attempted.append(document_id)
        if document_id == "bad":
            raise HardStop("simulated failure")

    scraper.process_candidates(["ok-1", "bad", "ok-2"], fetch_one)

    assert attempted == ["ok-1", "bad"]  # never reached ok-2
    assert manifest.summary.stop_reason == "hard_stop"


def test_preview_info_remaining():
    assert PreviewInfo(total_available=10, already_known=3).remaining == 7
    assert PreviewInfo(total_available=10, already_known=10).remaining == 0
    assert PreviewInfo(total_available=10, already_known=15).remaining == 0  # never negative
    assert PreviewInfo(total_available=None, already_known=3).remaining is None
    assert PreviewInfo(total_available=10, already_known=None).remaining is None


def test_default_estimate_is_honestly_unsupported(tmp_path):
    _storage, _manifest, scraper = make_scraper(tmp_path)
    info = scraper.estimate()
    assert info.total_available is None
    assert info.already_known is None
    assert info.note
