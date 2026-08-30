import responses

from qara_reg_scraper.config import ServiceSettings
from qara_reg_scraper.service_client import ScraperServiceClient, ServiceSyncError

BASE_URL = "http://reg-scraper:8080/api/reg-scraper"


def fast_settings(**overrides) -> ServiceSettings:
    defaults = {"base_url": BASE_URL, "timeout_seconds": 5}
    defaults.update(overrides)
    return ServiceSettings(**defaults)


@responses.activate
def test_upsert_document_posts_camelcase_body_and_unwraps_data():
    responses.add(
        responses.POST,
        f"{BASE_URL}/v1/documents",
        json={"success": True, "status": 200, "data": {"documentId": "part-800", "regulation": "fda"}},
        status=200,
    )
    client = ScraperServiceClient(fast_settings())
    result = client.upsert_document({"regulation": "fda", "source": "ecfr", "documentId": "part-800"})
    assert result == {"documentId": "part-800", "regulation": "fda"}

    sent = responses.calls[0].request
    assert sent.headers["Content-Type"] == "application/json"
    import json

    assert json.loads(sent.body) == {"regulation": "fda", "source": "ecfr", "documentId": "part-800"}


@responses.activate
def test_transient_5xx_is_retried_then_succeeds():
    responses.add(responses.POST, f"{BASE_URL}/v1/events", status=503)
    responses.add(
        responses.POST, f"{BASE_URL}/v1/events",
        json={"success": True, "status": 200, "data": {}}, status=200,
    )
    client = ScraperServiceClient(fast_settings())
    client.record_event({"documentId": "d1", "event": "new"})
    assert len(responses.calls) == 2


@responses.activate
def test_exhausted_retries_raise_service_sync_error_not_swallowed():
    # 3 failures - more than _MAX_ATTEMPTS (3) can ever succeed against.
    for _ in range(3):
        responses.add(responses.POST, f"{BASE_URL}/v1/runs", status=503)
    client = ScraperServiceClient(fast_settings())
    try:
        client.upsert_run({"runId": "r1"})
        raise AssertionError("expected ServiceSyncError")
    except ServiceSyncError as e:
        assert e.operation == "upsert_run"
        assert e.attempts == 3
        assert "after 3 attempts" in str(e)
        assert len(responses.calls) == 3  # exactly _MAX_ATTEMPTS, not more, not fewer


@responses.activate
def test_non_retryable_error_fails_after_exactly_one_attempt():
    """A 404 (bad path, not transient) must not be retried, and the raised
    error's attempt count/message must say so honestly - not claim 3
    attempts happened when only 1 real request was ever made (a real bug
    caught live this session: the message used to hardcode _MAX_ATTEMPTS
    regardless of how many attempts actually occurred)."""
    responses.add(responses.POST, f"{BASE_URL}/v1/runs", status=404)
    client = ScraperServiceClient(fast_settings())
    try:
        client.upsert_run({"runId": "r1"})
        raise AssertionError("expected ServiceSyncError")
    except ServiceSyncError as e:
        assert e.attempts == 1
        assert "after 1 attempt:" in str(e)  # singular, not "1 attempts"
        assert len(responses.calls) == 1


@responses.activate
def test_connection_error_also_raises_service_sync_error():
    import requests

    responses.add(
        responses.GET, f"{BASE_URL}/v1/status",
        body=requests.ConnectionError("connection refused"),
    )
    client = ScraperServiceClient(fast_settings())
    try:
        client.get_status(["fda:ecfr"])
        raise AssertionError("expected ServiceSyncError")
    except ServiceSyncError as e:
        assert e.operation == "get_status"


@responses.activate
def test_get_status_returns_raw_list_not_unwrapped():
    responses.add(
        responses.GET, f"{BASE_URL}/v1/status",
        json=[{"regulation": "fda", "source": "ecfr", "documents": 3}],
        status=200,
    )
    client = ScraperServiceClient(fast_settings())
    result = client.get_status(["fda:ecfr"])
    assert result == [{"regulation": "fda", "source": "ecfr", "documents": 3}]


@responses.activate
def test_sync_sources_puts_a_bulk_list_body_and_unwraps_data():
    """Unlike every other method above, sync_sources' body is a bulk LIST,
    not a single-item dict — the payload IS the whole known-source
    registry in one PUT, not a per-source call."""
    responses.add(
        responses.PUT,
        f"{BASE_URL}/v1/sources",
        json={
            "success": True,
            "status": 200,
            "data": [{"regulation": "fda", "source": "ecfr", "label": "eCFR", "description": "..."}],
        },
        status=200,
    )
    client = ScraperServiceClient(fast_settings())
    payload = [{"regulation": "fda", "source": "ecfr", "label": "eCFR", "description": "..."}]
    result = client.sync_sources(payload)

    assert result == [{"regulation": "fda", "source": "ecfr", "label": "eCFR", "description": "..."}]
    sent = responses.calls[0].request
    import json

    assert json.loads(sent.body) == payload


def test_missing_base_url_raises_immediately():
    try:
        ScraperServiceClient(ServiceSettings(base_url=None))
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


@responses.activate
def test_debug_detail_not_logged_at_info_level(caplog):
    responses.add(
        responses.GET, f"{BASE_URL}/v1/status",
        json=[{"regulation": "fda", "source": "ecfr"}], status=200,
    )
    client = ScraperServiceClient(fast_settings())
    with caplog.at_level("INFO", logger="qara_reg_scraper.service_client"):
        client.get_status(["fda:ecfr"])

    assert "service_request_detail" not in [r.getMessage() for r in caplog.records]


@responses.activate
def test_debug_detail_includes_request_body_and_response_headers_at_debug_level(caplog):
    """Same detail level as PoliteHttpClient's own --debug output (see
    test_http_client.py) — this client's own requests (pushes to
    qara-reg-scraper-svc) are just as worth --debug's full detail as the
    outbound scraping requests are."""
    responses.add(
        responses.POST, f"{BASE_URL}/v1/events",
        json={"success": True, "status": 200, "data": {"documentId": "d1"}},
        status=200,
        headers={"X-Server": "reg-scraper-svc"},
    )
    client = ScraperServiceClient(fast_settings())
    with caplog.at_level("DEBUG", logger="qara_reg_scraper.service_client"):
        client.record_event({"documentId": "d1", "event": "new"})

    detail = next(r for r in caplog.records if r.getMessage() == "service_request_detail")
    fields = detail.extra_fields
    assert fields["operation"] == "record_event"
    assert fields["method"] == "POST"
    assert fields["request_body"] == {"documentId": "d1", "event": "new"}
    assert fields["status"] == 200
    assert fields["response_headers"]["X-Server"] == "reg-scraper-svc"
    assert '"documentId"' in fields["response_body"]
