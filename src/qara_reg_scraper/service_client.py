"""Thin REST client for qara-reg-scraper-svc — the CLI's only path to
persistence besides the file-based manifest (see manifest.py's module
docstring: the manifest stays the mandatory source of truth; this pushes a
REST-synced view of it to the service that owns Postgres via Flyway).

Deliberately NOT "polite" like http_client.py's PoliteHttpClient — no
robots.txt, no per-host throttling, no minutes-long exponential backoff.
This talks to our own internal service, typically on the same docker
network: a short, fast retry is the right shape here. A transient blip
should be invisible; a genuinely unreachable service should fail fast and
loud, not retry for minutes while a scrape run hangs.

Retry-then-raise, not retry-then-swallow: every method retries a few times,
then raises ServiceSyncError if the service still can't be reached or keeps
erroring. Manifest deliberately does not catch this — a sync failure
cancels the run with a clear error instead of being logged and silently
swallowed (see manifest.py / cli.py's `run` command).
"""

from __future__ import annotations

import logging
from typing import Any, Self

import requests
from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_exponential

from .config import ServiceSettings
from .logging_setup import debug_body_snippet, get_logger, log_extra

log = get_logger("service_client")

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 3


class ServiceSyncError(Exception):
    """Raised when qara-reg-scraper-svc can't be reached, or errors, on
    every retry attempt for one operation. Deliberately not caught inside
    Manifest — see its module docstring for why a sync failure cancels the
    run rather than being logged and swallowed."""

    def __init__(self, operation: str, cause: BaseException | str, attempts: int = 1):
        self.operation = operation
        self.cause = cause
        self.attempts = attempts
        # `attempts` reflects what actually happened, not always _MAX_ATTEMPTS
        # — a non-retryable error (e.g. a 404, a bad request body) fails on
        # the first try, only the retryable ones (429/5xx, connection
        # errors/timeouts) actually exhaust every attempt. Saying "after 3
        # attempts" for a 1-attempt failure would misrepresent what
        # qara-reg-scraper-svc actually did.
        plural = "attempt" if attempts == 1 else "attempts"
        super().__init__(f"qara-reg-scraper-svc sync failed ({operation}) after {attempts} {plural}: {cause}")


class _RetryableStatus(Exception):
    """Wraps a 429/5xx response so it flows through the tenacity retry path."""

    def __init__(self, response: requests.Response):
        self.response = response
        super().__init__(f"{response.status_code} from {response.url}")


def _is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, (_RetryableStatus, requests.ConnectionError, requests.Timeout))


class ScraperServiceClient:
    """One instance per `run`/`reindex`/`status` invocation. `base_url`
    should already include the service's context path, e.g.
    "http://reg-scraper:8080/api/reg-scraper" — every method below appends
    only the endpoint's own path under that."""

    def __init__(self, settings: ServiceSettings):
        if not settings.base_url:
            raise ValueError("ServiceSettings.base_url is required to construct a ScraperServiceClient")
        self.base_url = settings.base_url.rstrip("/")
        self.timeout = settings.timeout_seconds
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

    def _request(self, operation: str, method: str, path: str, **kwargs) -> Any:
        url = f"{self.base_url}{path}"
        retryer = Retrying(
            stop=stop_after_attempt(_MAX_ATTEMPTS),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=2),
            retry=retry_if_exception(_is_retryable),
            reraise=True,
        )

        def _do() -> Any:
            response = self.session.request(method, url, timeout=self.timeout, **kwargs)
            # Every request (success or failure — service_sync_failed above
            # only fires for the latter, and only after retries are
            # exhausted) at DEBUG level, gated the same way and for the same
            # reason as http_client.py's PoliteHttpClient — see
            # logging_setup.debug_body_snippet.
            if log.isEnabledFor(logging.DEBUG):
                log_extra(
                    log,
                    logging.DEBUG,
                    "service_request_detail",
                    operation=operation, method=method, url=url,
                    request_body=kwargs.get("json"),
                    status=response.status_code,
                    response_headers=dict(response.headers),
                    response_body=debug_body_snippet(
                        response.content, response.headers.get("Content-Type", "")
                    ),
                )
            if response.status_code in _RETRYABLE_STATUS_CODES:
                raise _RetryableStatus(response)
            response.raise_for_status()
            return response.json() if response.content else None

        try:
            return retryer(_do)
        except (requests.ConnectionError, requests.Timeout, _RetryableStatus, requests.HTTPError) as e:
            attempts = retryer.statistics.get("attempt_number", 1)
            log_extra(log, logging.ERROR, "service_sync_failed", operation=operation, url=url, attempts=attempts, error=str(e))
            raise ServiceSyncError(operation, e, attempts) from e

    # -- upserts (POST/.../PUT — fire-and-forget from the caller's point of
    # view; the return value is the service's own record, mostly useful for
    # debugging, not consumed by Manifest) -----------------------------------

    def upsert_run(self, dto: dict[str, Any]) -> dict[str, Any] | None:
        return self._unwrap(self._request("upsert_run", "POST", "/v1/runs", json=dto))

    def record_event(self, dto: dict[str, Any]) -> dict[str, Any] | None:
        return self._unwrap(self._request("record_event", "POST", "/v1/events", json=dto))

    def upsert_document(self, dto: dict[str, Any]) -> dict[str, Any] | None:
        return self._unwrap(self._request("upsert_document", "POST", "/v1/documents", json=dto))

    def put_source_estimate(self, regulation: str, source: str, dto: dict[str, Any]) -> dict[str, Any] | None:
        return self._unwrap(
            self._request("put_source_estimate", "PUT", f"/v1/source-estimates/{regulation}/{source}", json=dto)
        )

    def sync_sources(self, sources: list[dict[str, Any]]) -> Any:
        """PUT /v1/sources — a bulk replace-in-place of the whole known-source
        registry (regulation/source/label/description), not a single-item
        upsert like every method above. The service upserts each entry by
        (regulation, source) and deletes any row not present in `sources`,
        so this is a full sync, not an additive push — always pass the
        *entire* registry, never a partial list. See
        docs/source-registry-sync.md for when/why this gets called."""
        return self._unwrap(self._request("sync_sources", "PUT", "/v1/sources", json=sources))

    # -- reads ---------------------------------------------------------------

    def get_status(self, qualified_names: list[str]) -> list[dict[str, Any]]:
        """GET /v1/status?source=fda:ecfr,fda:recalls — unlike the upsert
        endpoints this returns a raw JSON array, not an ApiResponse envelope
        (see StatusController.java)."""
        response = self._request(
            "get_status", "GET", "/v1/status", params={"source": ",".join(qualified_names)}
        )
        return response if isinstance(response, list) else []

    @staticmethod
    def _unwrap(response: Any) -> dict[str, Any] | None:
        """Every upsert endpoint wraps its DTO in qara_lib_mn's
        ApiResponse<T> — {"success": true, "status": 200, "data": {...}}."""
        if isinstance(response, dict):
            return response.get("data", response)
        return response

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
