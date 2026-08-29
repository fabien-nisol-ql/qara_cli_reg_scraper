"""A requests wrapper that never hides what it is and is polite by default.

Design choices driven directly by the "don't hide, do disclose" and
"throttling/error resilient" requirements:

- Every request carries a descriptive ``User-Agent`` and ``From`` header
  naming this tool, its purpose, and a contact address (see
  ``HttpSettings.user_agent`` in config.py). The user agent is fixed — this
  client never rotates identities, spoofs a browser, or routes through
  proxies to obscure where requests come from.
- ``robots.txt`` is fetched and honored per-host before any request, unless
  explicitly disabled in config.
- Requests are throttled per host via a simple token bucket
  (``http.requests_per_second`` in config, default a conservative 1 req/s).
- Transient failures (connection errors, timeouts, 429/500/502/503/504) are
  retried with exponential backoff + jitter, capped, and a server's
  ``Retry-After`` header is honored exactly rather than guessed at.
- Every request is logged (method, URL, status, elapsed time) — nothing
  about what this tool fetched is hidden from its own operator either.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from typing import Self
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests
from tenacity import RetryCallState, Retrying, retry_if_exception, stop_after_attempt

from .config import HttpSettings
from .logging_setup import get_logger, log_extra

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
MAX_BACKOFF_SECONDS = 120.0

# Phrases seen on bot-management "apology"/challenge pages (Akamai and
# similar). This is deliberately a plain substring match on a *small* HTML
# body — a real multi-KB guidance page is never mistaken for one, but a
# terse challenge stub is. Confirmed against a live block on
# accessdata.fda.gov while building this tool (see clearances_510k.py's
# module docstring) — extend this list if another host's wording differs.
_BOT_BLOCK_MARKERS = (
    "fda apology",
    "abuse-detection",
    "excessive-requests",
    "access denied",
    "captcha",
    "are you a robot",
    "request blocked",
    "automated access",
)
_BOT_BLOCK_SNIFF_MAX_BYTES = 20_000


def looks_like_bot_block(response: requests.Response) -> bool:
    """Best-effort detection of a bot-management challenge/apology page
    served with a 2xx/mundane status instead of the real document — the
    case a plain `raise_for_status()` check can't catch, since the server
    isn't reporting an error at the HTTP-status level."""
    content_type = response.headers.get("Content-Type", "")
    if "html" not in content_type.lower():
        return False
    if len(response.content) > _BOT_BLOCK_SNIFF_MAX_BYTES:
        return False
    snippet = response.text[:4000].lower()
    return any(marker in snippet for marker in _BOT_BLOCK_MARKERS)


class ThrottlingError(Exception):
    """Wraps a 429/5xx response so it flows through the tenacity retry path
    while still giving the wait callback access to `Retry-After`."""

    def __init__(self, response: requests.Response):
        self.response = response
        super().__init__(f"{response.status_code} from {response.url}")


class RobotsDisallowed(Exception):
    """Raised when robots.txt disallows fetching a URL. This tool does not
    override or bypass robots.txt — treat this as a real stop condition,
    not an error to retry around."""


def _is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, (ThrottlingError, requests.ConnectionError, requests.Timeout))


def _retry_wait(retry_state: RetryCallState) -> float:
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    if isinstance(exc, ThrottlingError):
        retry_after = exc.response.headers.get("Retry-After")
        if retry_after:
            try:
                return min(float(retry_after), MAX_BACKOFF_SECONDS)
            except ValueError:
                pass  # not a numeric Retry-After (could be an HTTP-date) — fall through
    attempt = retry_state.attempt_number
    backoff = min(MAX_BACKOFF_SECONDS, (2 ** attempt))
    return backoff + random.uniform(0, 1)


class _TokenBucket:
    """Per-host rate limiter: blocks the caller so requests to one host are
    spaced at least ``1/rate`` seconds apart. Thread-safe."""

    def __init__(self, rate_per_second: float):
        self.min_interval = 1.0 / rate_per_second if rate_per_second > 0 else 0.0
        self._lock = threading.Lock()
        self._last_call = 0.0

    def wait(self) -> None:
        if self.min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            sleep_for = self.min_interval - (now - self._last_call)
            if sleep_for > 0:
                time.sleep(sleep_for)
            self._last_call = time.monotonic()


class PoliteHttpClient:
    def __init__(self, http_settings: HttpSettings, source_name: str):
        self.settings = http_settings
        self.source_name = source_name
        self.log = get_logger(f"http.{source_name}")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": http_settings.user_agent,
                "From": http_settings.contact_email,
                "Accept": "application/json, text/html, application/xml;q=0.9, */*;q=0.8",
            }
        )
        self._buckets: dict[str, _TokenBucket] = {}
        self._robots_cache: dict[str, RobotFileParser | None] = {}

    def _bucket_for(self, host: str) -> _TokenBucket:
        rate = self.settings.requests_per_second
        if host not in self._buckets:
            self._buckets[host] = _TokenBucket(rate)
        return self._buckets[host]

    def _robots_allowed(self, url: str) -> bool:
        if not self.settings.respect_robots_txt:
            return True
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self._robots_cache:
            rp = RobotFileParser()
            rp.set_url(f"{origin}/robots.txt")
            try:
                rp.read()
            except Exception:  # noqa: BLE001 - deliberately broad: any failure to
                # fetch robots.txt (DNS, timeout, malformed file, ...) should
                # fall through to "allow", not propagate and abort a source.
                # No robots.txt reachable -> default to allow. Failing closed
                # here would break the tool on sites that simply don't
                # publish one (common for FDA/openFDA endpoints).
                log_extra(self.log, logging.WARNING, "robots_txt_unreachable", origin=origin)
                rp = None
            self._robots_cache[origin] = rp
        rp = self._robots_cache[origin]
        if rp is None:
            return True
        return rp.can_fetch(self.settings.user_agent, url)

    def get(
        self,
        url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        **kwargs,
    ) -> requests.Response:
        """GET a URL with politeness, retry, and optional conditional-request
        headers (If-None-Match / If-Modified-Since) so unchanged documents
        can short-circuit to a cheap 304 instead of a full download."""
        if not self._robots_allowed(url):
            log_extra(self.log, logging.WARNING, "robots_disallowed", url=url)
            raise RobotsDisallowed(f"robots.txt disallows fetching {url}")

        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        bucket = self._bucket_for(urlparse(url).netloc)

        retryer = Retrying(
            stop=stop_after_attempt(self.settings.max_retries),
            wait=_retry_wait,
            retry=retry_if_exception(_is_retryable),
            reraise=True,
        )

        def _do_request() -> requests.Response:
            bucket.wait()
            start = time.monotonic()
            response = self.session.get(
                url, timeout=self.settings.timeout_seconds, headers=headers, **kwargs
            )
            elapsed_ms = round((time.monotonic() - start) * 1000)
            log_extra(
                self.log,
                logging.INFO,
                "http_request",
                method="GET",
                url=url,
                status=response.status_code,
                elapsed_ms=elapsed_ms,
            )
            if response.status_code in RETRYABLE_STATUS_CODES:
                if response.status_code == 429:
                    log_extra(
                        self.log,
                        logging.WARNING,
                        "throttled_by_server",
                        url=url,
                        retry_after=response.headers.get("Retry-After"),
                    )
                raise ThrottlingError(response)
            return response

        return retryer(_do_request)

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
