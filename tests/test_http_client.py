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
    bucket = _TokenBucket(min_interval=0.05)
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


@responses.activate
def test_robots_disallowed_prevents_request():
    responses.add(
        responses.GET, "https://example.gov/robots.txt",
        body="User-agent: *\nDisallow: /blocked\n", status=200,
    )
    client = PoliteHttpClient(fast_settings(respect_robots_txt=True), "test")
    with pytest.raises(RobotsDisallowed):
        client.get("https://example.gov/blocked")


@responses.activate
def test_robots_txt_not_fetched_at_all_when_disabled():
    """respect_robots_txt=False must skip the robots.txt fetch entirely,
    not just ignore its result — no mock registered for it here, so an
    attempted fetch would raise ConnectionError."""
    responses.add(responses.GET, "https://example.gov/doc", body="ok", status=200)
    client = PoliteHttpClient(fast_settings(respect_robots_txt=False), "test")
    resp = client.get("https://example.gov/doc")
    assert resp.status_code == 200
    assert not any("robots.txt" in c.request.url for c in responses.calls)


@responses.activate
def test_a_404_robots_txt_means_unrestricted():
    """No robots.txt published at all (a plain 404, not a real Disallow
    rule) must mean "allow everything" — matching
    RobotFileParser.read()'s own handling of this exact case. Regression
    guard: an earlier version of this fetch-it-ourselves code left
    RobotFileParser's `last_checked` unset in this branch, which makes
    can_fetch() default-DENY instead (see its own docstring) — the
    opposite of the intended behavior."""
    responses.add(responses.GET, "https://example.gov/robots.txt", status=404)
    responses.add(responses.GET, "https://example.gov/doc", body="ok", status=200)
    client = PoliteHttpClient(fast_settings(respect_robots_txt=True), "test")
    resp = client.get("https://example.gov/doc")
    assert resp.status_code == 200


@responses.activate
def test_robots_txt_genuinely_unreachable_means_unrestricted():
    """Same intent/regression class as the 404 case above, but for a real
    fetch failure (DNS, timeout, connection refused, ...) rather than a
    clean HTTP error status — caught live while building next_available_at:
    the exception branch left a freshly-constructed, never-.parse()'d
    RobotFileParser in place instead of explicitly setting it to None,
    which made can_fetch() default-DENY (last_checked == 0) instead of
    the intended "unreachable -> allow everything"."""
    responses.add(
        responses.GET, "https://example.gov/robots.txt",
        body=requests.ConnectionError("connection refused"),
    )
    responses.add(responses.GET, "https://example.gov/doc", body="ok", status=200)
    client = PoliteHttpClient(fast_settings(respect_robots_txt=True), "test")
    resp = client.get("https://example.gov/doc")
    assert resp.status_code == 200


@responses.activate
def test_a_401_robots_txt_means_disallow_everything():
    responses.add(responses.GET, "https://example.gov/robots.txt", status=401)
    client = PoliteHttpClient(fast_settings(respect_robots_txt=True), "test")
    with pytest.raises(RobotsDisallowed):
        client.get("https://example.gov/doc")


@responses.activate
def test_crawl_delay_slows_the_bucket_below_the_configured_rate():
    """A host asking (via the standard Crawl-delay directive) for slower
    than config's own requests_per_second must win — robots.txt is a
    floor on politeness, never something config can race past."""
    responses.add(
        responses.GET, "https://example.gov/robots.txt",
        body="User-agent: *\nCrawl-delay: 10\n", status=200,
    )
    client = PoliteHttpClient(fast_settings(respect_robots_txt=True, requests_per_second=1000), "test")
    client._load_robots("https://example.gov")
    bucket = client._bucket_for("example.gov", 10.0)
    assert bucket.min_interval == 10.0


@responses.activate
def test_hit_rate_directive_slows_the_bucket_even_though_robotparser_ignores_it():
    """Hit-rate isn't part of the robots.txt spec urllib.robotparser
    implements — confirmed live on accessdata.fda.gov, the actual host
    this was built for (see robots_policy.py) — so this only works if
    it's parsed separately, not just delegated to RobotFileParser."""
    responses.add(
        responses.GET, "https://example.gov/robots.txt",
        body="User-agent: *\nHit-rate: 30\n", status=200,
    )
    client = PoliteHttpClient(fast_settings(respect_robots_txt=True, requests_per_second=1000), "test")
    _rp, policy = client._load_robots("https://example.gov")
    assert policy.min_interval_seconds == 30.0


@responses.activate
def test_visiting_hours_outside_window_raises_robots_disallowed(monkeypatch):
    responses.add(
        responses.GET, "https://example.gov/robots.txt",
        body="User-agent: *\nVisiting-hours: 23:00EDT-05:00EDT\n", status=200,
    )
    # Noon UTC is early morning Eastern - well outside 23:00-05:00 either way.
    from datetime import UTC, datetime

    monkeypatch.setattr(
        "qara_reg_scraper.http_client.now_utc",
        lambda: datetime(2026, 6, 15, 12, 0, tzinfo=UTC),
    )
    client = PoliteHttpClient(fast_settings(respect_robots_txt=True), "test")
    with pytest.raises(RobotsDisallowed):
        client.get("https://example.gov/doc")


@responses.activate
def test_visiting_hours_inside_window_allows_the_request(monkeypatch):
    responses.add(
        responses.GET, "https://example.gov/robots.txt",
        body="User-agent: *\nVisiting-hours: 23:00EDT-05:00EDT\n", status=200,
    )
    responses.add(responses.GET, "https://example.gov/doc", body="ok", status=200)
    from datetime import UTC, datetime

    # 03:00 UTC in June is 23:00 the prior day Eastern (EDT, UTC-4) - inside the window.
    monkeypatch.setattr(
        "qara_reg_scraper.http_client.now_utc",
        lambda: datetime(2026, 6, 15, 3, 0, tzinfo=UTC),
    )
    client = PoliteHttpClient(fast_settings(respect_robots_txt=True), "test")
    resp = client.get("https://example.gov/doc")
    assert resp.status_code == 200


@responses.activate
def test_debug_detail_not_logged_at_info_level(caplog):
    """The extra headers/body detail line must not be emitted at all at
    the default INFO level — not just filtered by the handler, since
    isEnabledFor() guards it before the body is even read."""
    responses.add(
        responses.GET, "https://example.gov/doc",
        body="ok", status=200, headers={"X-Test": "yes"},
    )
    client = PoliteHttpClient(fast_settings(), "debug-test-info")
    with caplog.at_level("INFO", logger="qara_reg_scraper.http.debug-test-info"):
        client.get("https://example.gov/doc")

    messages = [r.getMessage() for r in caplog.records]
    assert "http_request" in messages
    assert "http_request_detail" not in messages


@responses.activate
def test_debug_detail_includes_full_headers_and_body_at_debug_level(caplog):
    """The actual point of --debug: request AND response headers, plus
    the response body, must be present — not just method/URL/status."""
    responses.add(
        responses.GET, "https://example.gov/doc",
        body='{"hello": "world"}', status=200,
        content_type="application/json",
        headers={"X-Server": "test-server"},
    )
    client = PoliteHttpClient(fast_settings(), "debug-test-detail")
    with caplog.at_level("DEBUG", logger="qara_reg_scraper.http.debug-test-detail"):
        client.get("https://example.gov/doc")

    detail = next(r for r in caplog.records if r.getMessage() == "http_request_detail")
    fields = detail.extra_fields
    assert fields["method"] == "GET"
    assert fields["url"] == "https://example.gov/doc"
    assert fields["status"] == 200
    # Request headers: session defaults (User-Agent/From) actually sent.
    assert "qara-reg-scraper" in fields["request_headers"]["User-Agent"]
    # Response headers: the server's own header, verbatim.
    assert fields["response_headers"]["X-Server"] == "test-server"
    # Response body: the actual JSON, not just its length/content-type.
    assert fields["response_body"] == '{"hello": "world"}'


@responses.activate
def test_next_available_at_is_none_when_no_visiting_hours_declared():
    responses.add(
        responses.GET, "https://example.gov/robots.txt",
        body="User-agent: *\nDisallow: /private\n", status=200,
    )
    client = PoliteHttpClient(fast_settings(respect_robots_txt=True), "test")
    assert client.next_available_at("https://example.gov/doc") is None


@responses.activate
def test_next_available_at_is_none_when_currently_inside_the_window(monkeypatch):
    responses.add(
        responses.GET, "https://example.gov/robots.txt",
        body="User-agent: *\nVisiting-hours: 23:00EDT-05:00EDT\n", status=200,
    )
    from datetime import UTC, datetime

    # 03:00 UTC in June is 23:00 the prior day EDT - inside the window.
    monkeypatch.setattr(
        "qara_reg_scraper.http_client.now_utc",
        lambda: datetime(2026, 6, 15, 3, 0, tzinfo=UTC),
    )
    client = PoliteHttpClient(fast_settings(respect_robots_txt=True), "test")
    assert client.next_available_at("https://example.gov/doc") is None


@responses.activate
def test_next_available_at_returns_the_next_open_time_when_currently_outside(monkeypatch):
    responses.add(
        responses.GET, "https://example.gov/robots.txt",
        body="User-agent: *\nVisiting-hours: 23:00EDT-05:00EDT\n", status=200,
    )
    from datetime import UTC, datetime

    monkeypatch.setattr(
        "qara_reg_scraper.http_client.now_utc",
        lambda: datetime(2026, 6, 15, 12, 0, tzinfo=UTC),  # 08:00 EDT, outside
    )
    client = PoliteHttpClient(fast_settings(respect_robots_txt=True), "test")
    next_at = client.next_available_at("https://example.gov/doc")
    assert next_at == datetime(2026, 6, 16, 3, 0, tzinfo=UTC)  # 23:00 EDT == 03:00 UTC next day


def test_next_available_at_is_none_when_robots_txt_disabled():
    """No `responses.activate` at all - if this attempted a robots.txt
    fetch it would raise ConnectionError, proving it's skipped entirely
    when respect_robots_txt is off, same posture as the disallow check."""
    client = PoliteHttpClient(fast_settings(respect_robots_txt=False), "test")
    assert client.next_available_at("https://example.gov/doc") is None


class _FakeStorage:
    def __init__(self):
        self.files: dict[str, str] = {}

    def write_text(self, path, text, *, encoding="utf-8"):
        self.files[path] = text

    def read_text(self, path, *, encoding="utf-8"):
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    def local_root(self):
        # Duck-typed fake, not a real StorageBackend subclass — mirrors
        # StorageBackend.local_root()'s own default (no real filesystem
        # backing this fake) so PoliteHttpClient.__init__ can call it
        # unconditionally, same as it does for every real backend.
        return None


@responses.activate
def test_a_successful_robots_txt_fetch_is_persisted_to_storage():
    responses.add(
        responses.GET, "https://example.gov/robots.txt",
        body="User-agent: *\nHit-rate: 30\n", status=200,
    )
    storage = _FakeStorage()
    client = PoliteHttpClient(fast_settings(respect_robots_txt=True), "test", storage=storage)
    client._load_robots("https://example.gov")

    assert len(storage.files) == 1


@responses.activate
def test_a_blocked_robots_txt_fetch_falls_back_to_the_cached_policy(monkeypatch):
    """The actual point: a live block (Akamai apology page, not a genuine
    absence) must not erase what a previous, successful run already
    learned — confirmed live, repeatedly, as the real failure mode this
    was built for (accessdata.fda.gov)."""
    from qara_reg_scraper.robots_policy import RobotsPolicy, save_cached_policy

    storage = _FakeStorage()
    save_cached_policy(storage, "https://example.gov", RobotsPolicy(min_interval_seconds=30.0, visiting_hours=None))
    responses.add(
        responses.GET, "https://example.gov/robots.txt",
        body="<html>FDA Apology - excessive-requests-apology</html>",
        status=404, content_type="text/html",
    )
    client = PoliteHttpClient(fast_settings(respect_robots_txt=True), "test", storage=storage)

    _rp, policy = client._load_robots("https://example.gov")

    assert policy.min_interval_seconds == 30.0


@responses.activate
def test_a_genuine_404_robots_txt_clears_any_previously_cached_policy():
    """Unlike a block, a real "no robots.txt here" (no block markers) is a
    confirmed absence — nothing left to protect, so it's fine (correct,
    even) for this to clear a stale cached policy from long ago."""
    from qara_reg_scraper.robots_policy import RobotsPolicy, save_cached_policy

    storage = _FakeStorage()
    save_cached_policy(storage, "https://example.gov", RobotsPolicy(min_interval_seconds=30.0, visiting_hours=None))
    responses.add(responses.GET, "https://example.gov/robots.txt", body="Not Found", status=404)
    client = PoliteHttpClient(fast_settings(respect_robots_txt=True), "test", storage=storage)

    _rp, policy = client._load_robots("https://example.gov")

    assert policy.min_interval_seconds is None


@responses.activate
def test_visiting_hours_description_is_none_when_no_window_declared():
    responses.add(
        responses.GET, "https://example.gov/robots.txt",
        body="User-agent: *\nHit-rate: 30\n", status=200,
    )
    client = PoliteHttpClient(fast_settings(respect_robots_txt=True), "test")
    assert client.visiting_hours_description("https://example.gov/doc") is None


@responses.activate
def test_visiting_hours_description_is_human_readable_12_hour_format():
    responses.add(
        responses.GET, "https://example.gov/robots.txt",
        body="User-agent: *\nVisiting-hours: 23:00EDT-05:00EDT\n", status=200,
    )
    client = PoliteHttpClient(fast_settings(respect_robots_txt=True), "test")
    description = client.visiting_hours_description("https://example.gov/doc")
    assert description == "11:00 PM–5:00 AM America/New York"


@responses.activate
def test_visiting_hours_description_available_even_while_currently_inside_the_window(monkeypatch):
    """Unlike next_available_at (only meaningful while closed), this
    describes the recurring rule itself - present regardless of whether
    the window happens to be open right now."""
    responses.add(
        responses.GET, "https://example.gov/robots.txt",
        body="User-agent: *\nVisiting-hours: 23:00EDT-05:00EDT\n", status=200,
    )
    from datetime import UTC, datetime

    monkeypatch.setattr(
        "qara_reg_scraper.http_client.now_utc",
        lambda: datetime(2026, 6, 15, 3, 0, tzinfo=UTC),  # inside the window
    )
    client = PoliteHttpClient(fast_settings(respect_robots_txt=True), "test")
    assert client.next_available_at("https://example.gov/doc") is None  # open right now
    assert client.visiting_hours_description("https://example.gov/doc") is not None  # rule still described


@responses.activate
def test_two_sources_sharing_a_host_pace_against_each_other_via_shared_storage(tmp_path):
    """The actual bug this whole cross-process pacing mechanism (see
    origin_pacing.py's module docstring) exists to fix, reproduced at the
    PoliteHttpClient level: fda:pma and fda:clearances_510k are different
    sources, each with their own PoliteHttpClient instance - exactly
    this shape, not two calls on one client. Before origin_pacing.py
    existed, each client's own in-memory _TokenBucket had no idea the
    other existed, so two "independently well-behaved" clients could
    still hit the same host far faster, combined, than either one alone
    ever would - confirmed live against accessdata.fda.gov's own Akamai
    bot management. A LocalStorage rooted at the same tmp_path stands in
    for the shared bind-mounted volume every real job container mounts
    the same document/robots-cache/lock storage through."""
    from qara_reg_scraper.storage.local import LocalStorage

    responses.add(responses.GET, "https://accessdata.fda.gov/doc-a", body="a", status=200)
    responses.add(responses.GET, "https://accessdata.fda.gov/doc-b", body="b", status=200)

    storage = LocalStorage(str(tmp_path))
    settings = fast_settings(requests_per_second=20)  # 1/20s = 0.05s interval, same tolerance as elsewhere in this file
    pma_client = PoliteHttpClient(settings, "fda:pma", storage=storage)
    clearances_510k_client = PoliteHttpClient(settings, "fda:clearances_510k", storage=storage)

    start = time.monotonic()
    pma_client.get("https://accessdata.fda.gov/doc-a")
    clearances_510k_client.get("https://accessdata.fda.gov/doc-b")
    elapsed = time.monotonic() - start

    assert elapsed >= 0.04
