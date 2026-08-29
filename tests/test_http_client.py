import time

import pytest
import requests
import responses

from qara_reg_scraper.config import HttpSettings
from qara_reg_scraper.http_client import (
    PoliteHttpClient,
    RobotsDisallowed,
    ThrottlingError,
    _retry_wait,
    _TokenBucket,
)


def fast_settings(**overrides) -> HttpSettings:
    defaults = {"requests_per_second": 1000, "timeout_seconds": 5, "max_retries": 3, "respect_robots_txt": False}
    defaults.update(overrides)
    return HttpSettings(**defaults)


class _FakeOutcome:
    def __init__(self, exc):
        self._exc = exc

    def exception(self):
        return self._exc


class _FakeRetryState:
    def __init__(self, exc, attempt_number=1):
        self.outcome = _FakeOutcome(exc)
        self.attempt_number = attempt_number


def test_user_agent_and_from_header_present():
    client = PoliteHttpClient(fast_settings(), "test")
    assert "qara-reg-scraper" in client.session.headers["User-Agent"]
    assert "contact:" in client.session.headers["User-Agent"]
    assert client.session.headers["From"] == fast_settings().contact_email


def test_user_agent_override_replaces_composed_default():
    settings = fast_settings(user_agent_override="my-custom-agent/1.0 (contact: a@b.com)")
    client = PoliteHttpClient(settings, "test")
    assert client.session.headers["User-Agent"] == "my-custom-agent/1.0 (contact: a@b.com)"
    # project_url/contact_email are ignored for UA purposes once overridden,
    # but the From header is independent and still set.
    assert client.session.headers["From"] == settings.contact_email


def test_user_agent_override_unset_falls_back_to_composed_default():
    settings = fast_settings()
    assert settings.user_agent_override is None
    assert settings.user_agent.startswith("qara-reg-scraper/0.1 (+")


def test_retry_wait_honors_retry_after_header():
    fake_response = requests.Response()
    fake_response.status_code = 429
    fake_response.headers["Retry-After"] = "7"
    exc = ThrottlingError(fake_response)
    wait = _retry_wait(_FakeRetryState(exc))
    assert wait == 7.0


def test_retry_wait_falls_back_to_exponential_backoff_without_header():
    fake_response = requests.Response()
    fake_response.status_code = 500
    exc = ThrottlingError(fake_response)
    wait = _retry_wait(_FakeRetryState(exc, attempt_number=2))
    assert 4.0 <= wait <= 5.0  # 2**2 + jitter[0,1)


def test_token_bucket_enforces_min_interval():
    bucket = _TokenBucket(rate_per_second=20)  # min_interval = 0.05s
    start = time.monotonic()
    bucket.wait()
    bucket.wait()
    elapsed = time.monotonic() - start
    assert elapsed >= 0.04


@responses.activate
def test_get_success_returns_response():
    responses.add(responses.GET, "https://example.gov/doc", body="ok", status=200)
    client = PoliteHttpClient(fast_settings(), "test")
    resp = client.get("https://example.gov/doc")
    assert resp.status_code == 200
    assert resp.text == "ok"


@responses.activate
def test_get_retries_on_500_then_succeeds(monkeypatch):
    monkeypatch.setattr("qara_reg_scraper.http_client._retry_wait", lambda state: 0)
    responses.add(responses.GET, "https://example.gov/flaky", status=500)
    responses.add(responses.GET, "https://example.gov/flaky", body="ok", status=200)
    client = PoliteHttpClient(fast_settings(), "test")
    resp = client.get("https://example.gov/flaky")
    assert resp.status_code == 200
    assert len(responses.calls) == 2


@responses.activate
def test_get_raises_after_exhausting_retries(monkeypatch):
    monkeypatch.setattr("qara_reg_scraper.http_client._retry_wait", lambda state: 0)
    for _ in range(3):
        responses.add(responses.GET, "https://example.gov/down", status=503)
    client = PoliteHttpClient(fast_settings(max_retries=3), "test")
    with pytest.raises(ThrottlingError):
        client.get("https://example.gov/down")
    assert len(responses.calls) == 3


@responses.activate
def test_conditional_headers_are_sent():
    responses.add(responses.GET, "https://example.gov/doc", status=304)
    client = PoliteHttpClient(fast_settings(), "test")
    client.get("https://example.gov/doc", etag='"abc123"', last_modified="Mon, 01 Jan 2026 00:00:00 GMT")
    sent = responses.calls[0].request
    assert sent.headers["If-None-Match"] == '"abc123"'
    assert sent.headers["If-Modified-Since"] == "Mon, 01 Jan 2026 00:00:00 GMT"


def test_robots_disallowed_prevents_request(monkeypatch):
    client = PoliteHttpClient(fast_settings(respect_robots_txt=True), "test")
    monkeypatch.setattr(client, "_robots_allowed", lambda url: False)
    with pytest.raises(RobotsDisallowed):
        client.get("https://example.gov/blocked")
