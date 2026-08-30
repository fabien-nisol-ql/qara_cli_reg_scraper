"""A requests wrapper that never hides what it is and is polite by default.

Design choices driven directly by the "don't hide, do disclose" and
"throttling/error resilient" requirements:

- Every request carries a descriptive ``User-Agent`` and ``From`` header
  naming this tool, its purpose, and a contact address (see
  ``HttpSettings.user_agent`` in config.py). The user agent is fixed — this
  client never rotates identities, spoofs a browser, or routes through
  proxies to obscure where requests come from.
- ``robots.txt`` is fetched and honored per-host before any request, unless
  explicitly disabled in config — every directive a host actually
  publishes, not just ``Disallow``: ``Crawl-delay``/the non-standard
  ``Hit-rate`` slow a host down further than ``http.requests_per_second``
  if the host asks for slower (never faster than config, only ever more
  cautious), and the non-standard ``Visiting-hours`` — confirmed live on
  ``accessdata.fda.gov``, see ``robots_policy.py`` — refuses to fetch
  outside a host's declared window at all. None of this is hardcoded to
  any particular host in code; whatever a host's own robots.txt declares
  is what applies to it, dynamically, for any scraper that talks to it.
- Requests are throttled per host via a simple token bucket
  (``http.requests_per_second`` in config, default a conservative 1 req/s,
  or slower still if robots.txt asks for that — see above).
- Transient failures (connection errors, timeouts, 429/500/502/503/504) are
  retried with exponential backoff + jitter, capped, and a server's
  ``Retry-After`` header is honored exactly rather than guessed at.
- Every request is logged (method, URL, status, elapsed time) — nothing
  about what this tool fetched is hidden from its own operator either. At
  DEBUG level (``run --debug``, or any other command that turns the
  internal JSON log up that far) a second, fuller line is also logged per
  request: full request/response headers plus a truncated body snippet
  (see ``logging_setup.debug_body_snippet``) — off by default since it's
  meant for interactively troubleshooting one run, not routine logging.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from datetime import datetime
from typing import Self
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests
from tenacity import RetryCallState, Retrying, retry_if_exception, stop_after_attempt

from . import origin_pacing
from .config import HttpSettings
from .logging_setup import debug_body_snippet, get_logger, log_extra
from .robots_policy import (
    RobotsPolicy,
    load_cached_policy,
    now_utc,
    parse_robots_policy,
    save_cached_policy,
)

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
    """Raised when robots.txt disallows fetching a URL — a path-level
    ``Disallow``, or a fetch attempted outside the host's own declared
    ``Visiting-hours`` (robots_policy.py) — either way, this tool does not
    override or bypass what robots.txt says. Treat this as a real stop
    condition, not an error to retry around."""


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
    spaced at least ``min_interval`` seconds apart. Thread-safe."""

    def __init__(self, min_interval: float):
        self.min_interval = min_interval
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
    def __init__(self, http_settings: HttpSettings, source_name: str, *, storage=None):
        """`storage` (a `storage.base.StorageBackend`, optional) persists a
        successfully-learned robots.txt policy (Hit-rate/Crawl-delay/
        Visiting-hours — see robots_policy.py) across process runs, so a
        later run whose own robots.txt fetch gets blocked or is otherwise
        unreachable can still fall back to what a previous run actually
        learned, instead of reverting to "no restriction known" — see
        robots_policy.py's own module-level comment on why that matters.
        None (the default) disables persistence entirely — every scraper
        that constructs its own PoliteHttpClient without one still works
        exactly as before, just without the cross-run memory. The same
        `storage` also backs cross-process request pacing when it resolves
        to a real local filesystem — see `_wait_for_slot` and
        `origin_pacing.py`'s own module docstring for why that exists."""
        self.settings = http_settings
        self.source_name = source_name
        self.storage = storage
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
        self._robots_cache: dict[str, tuple[RobotFileParser | None, RobotsPolicy]] = {}
        # Resolved once, not per-request — cheap either way, but there's no
        # reason to ask a stable storage backend the same question every
        # single request. None whenever `storage` is None or doesn't back
        # onto a real filesystem (see StorageBackend.local_root's own
        # docstring) — `_wait_for_slot` falls back to the original
        # in-process-only `_TokenBucket` behavior in that case.
        self._local_storage_root = storage.local_root() if storage is not None else None

    def _effective_interval(self, robots_min_interval: float | None) -> float:
        """The pacing interval actually in force for a host: config's own
        `http.requests_per_second`, or robots.txt's Hit-rate/Crawl-delay,
        whichever asks for the *slower* pace. robots.txt can only ever
        make this slower than config, never faster — a host asking to be
        crawled gently is a floor, not a target to race up to; config
        staying stricter than that (e.g. a source deliberately throttled
        further after a real block) must still win."""
        configured_rate = self.settings.requests_per_second
        configured_interval = 1.0 / configured_rate if configured_rate > 0 else 0.0
        return max(configured_interval, robots_min_interval or 0.0)

    def _bucket_for(self, host: str, robots_min_interval: float | None) -> _TokenBucket:
        if host not in self._buckets:
            self._buckets[host] = _TokenBucket(self._effective_interval(robots_min_interval))
        return self._buckets[host]

    def _wait_for_slot(self, host: str, robots_min_interval: float | None) -> None:
        """Blocks until it's safe to issue the next request to `host`, at
        whatever interval `_effective_interval` computes. Cross-process
        (via `origin_pacing.py`) when `self._local_storage_root` is
        available; otherwise the original, in-process-only `_TokenBucket`
        — identical behavior to before cross-process pacing existed, for
        any caller with no `storage` or a non-local one. See
        `origin_pacing.py`'s module docstring for the full "why" — the
        short version: two different sources sharing a host, each with
        their own `PoliteHttpClient`, must not each think they're the
        only one pacing requests to it."""
        effective_interval = self._effective_interval(robots_min_interval)
        if self._local_storage_root is not None:
            origin_pacing.reserve_next_slot(self._local_storage_root, host, effective_interval)
        else:
            self._bucket_for(host, robots_min_interval).wait()

    def _load_robots(self, origin: str) -> tuple[RobotFileParser | None, RobotsPolicy]:
        """Fetches and parses `origin`'s robots.txt exactly once per
        process (cached after), for BOTH the standard Disallow/Crawl-delay
        ruleset (RobotFileParser, used the same way it always was) and
        this project's own Hit-rate/Visiting-hours parsing
        (robots_policy.py) — one fetch covers both, replicating
        RobotFileParser.read()'s own status-code handling (401/403 ->
        disallow everything; other 4xx, e.g. no robots.txt published at
        all -> allow everything; anything else unreachable -> also allow,
        logged) since we're fetching it ourselves via `self.session`
        rather than calling `.read()` (which does its own separate fetch
        and doesn't expose the raw text this project's own directives
        need).

        A genuine fetch failure OR a response that looks like a
        bot-management block (`looks_like_bot_block` — the same check
        `fetch_and_save` uses for real documents; confirmed live,
        repeatedly, that accessdata.fda.gov serves exactly this instead of
        its real robots.txt under load) fall back to whatever policy was
        last learned from `self.storage` (see robots_policy.py's own
        module comment on why that matters) rather than reverting to "no
        restriction known" — a block is not the same signal as "this host
        genuinely has nothing to say," and must not be read as one. Only
        a REAL 4xx that doesn't look like a block (an honest "no
        robots.txt here") clears a previously-learned policy — see the
        `elif` below."""
        if origin in self._robots_cache:
            return self._robots_cache[origin]

        cached_policy = load_cached_policy(self.storage, origin)
        rp = RobotFileParser()
        policy = cached_policy or RobotsPolicy(min_interval_seconds=None, visiting_hours=None)
        try:
            response = self.session.get(f"{origin}/robots.txt", timeout=self.settings.timeout_seconds)
        except Exception:  # noqa: BLE001 - deliberately broad: any failure to fetch
            # robots.txt (DNS, timeout, connection refused, ...) should fall
            # through to "allow", not propagate and abort a source. Failing
            # closed here would break the tool on sites that simply don't
            # publish one (common for FDA/openFDA endpoints). Must be
            # explicit (`rp = None`, checked at every call site), not just
            # "leave the freshly-constructed rp as-is": an untouched
            # RobotFileParser has `last_checked == 0`, and can_fetch()
            # itself default-DENIES in exactly that state (see its own
            # "until read... we must assume no url is allowable" comment)
            # - the opposite of the "unreachable -> allow" this is going for.
            log_extra(
                self.log, logging.WARNING, "robots_txt_unreachable",
                origin=origin, using_cached_policy=cached_policy is not None,
            )
            rp = None
        else:
            if looks_like_bot_block(response):
                log_extra(
                    self.log, logging.WARNING, "robots_txt_blocked",
                    origin=origin, status=response.status_code,
                    using_cached_policy=cached_policy is not None,
                )
                rp = None
            elif response.status_code in (401, 403):
                rp.disallow_all = True
            elif response.status_code >= 400:
                # A genuine "no robots.txt here" (not a block dressed up as
                # one — checked above) - allow everything, same as
                # RobotFileParser.read()'s own handling of this exact case.
                # Must be set explicitly: unlike that method, nothing here
                # ever calls .parse() in this branch, so `last_checked`
                # would otherwise stay unset and can_fetch() would
                # default-DENY instead (see its own "until read... we must
                # assume no url is allowable" comment) - the opposite of
                # "no robots.txt = unrestricted". A confirmed real absence,
                # unlike a block, DOES clear any previously-cached policy -
                # if the host genuinely stopped publishing one, there's
                # nothing left to remember.
                rp.allow_all = True
                policy = RobotsPolicy(min_interval_seconds=None, visiting_hours=None)
            else:
                rp.parse(response.text.splitlines())
                policy = parse_robots_policy(response.text, self.settings.user_agent, rp)
                save_cached_policy(self.storage, origin, policy)

        if policy.has_restrictions:
            log_extra(
                self.log, logging.INFO, "robots_policy_loaded", origin=origin,
                min_interval_seconds=policy.min_interval_seconds,
                visiting_hours=str(policy.visiting_hours) if policy.visiting_hours else None,
            )
        result = (rp, policy)
        self._robots_cache[origin] = result
        return result

    def next_available_at(self, url: str) -> datetime | None:
        """The next UTC time `url`'s host says (via its own robots.txt
        Visiting-hours) it's next fetchable — `None` if that host
        declares no such restriction, or if it's fetchable right now
        either way. Used by a scraper's own `estimate()` to surface this
        in `PreviewInfo.next_available_at`, which `Manifest.write_estimate`
        pushes to qara-reg-scraper-svc so ITS OWN scheduler (SourceRetryScheduler)
        can avoid triggering a job that would immediately no-op, and so
        it's visible to a human in the UI — see that service's own README
        for the other half of this. Deliberately checked here, not just
        left to `get()`'s own enforcement: knowing the answer *before*
        attempting a fetch is the whole point, not just refusing once
        asked."""
        if not self.settings.respect_robots_txt:
            return None
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        _rp, policy = self._load_robots(origin)
        if policy.visiting_hours is None:
            return None
        now = now_utc()
        if policy.visiting_hours.allows(now):
            return None
        return policy.visiting_hours.next_open_at(now)

    def visiting_hours_description(self, url: str) -> str | None:
        """A human-readable description of `url`'s host's own declared
        Visiting-hours window (e.g. "23:00-05:00 America/New_York"),
        regardless of whether it's currently open or closed — unlike
        `next_available_at` (a single future instant, only meaningful
        while closed), this is the *recurring rule itself*, useful for
        explaining WHY a source pauses on a schedule at all, not just
        when it'll next resume. `None` for the vast majority of hosts,
        which declare no such restriction."""
        if not self.settings.respect_robots_txt:
            return None
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        _rp, policy = self._load_robots(origin)
        if policy.visiting_hours is None:
            return None
        vh = policy.visiting_hours
        # A friendlier 12-hour rendering for a human reader — __str__
        # (24-hour, used in log lines) stays exactly as it was for that.
        return f"{vh.start:%-I:%M %p}–{vh.end:%-I:%M %p} {vh.zone.key.replace('_', ' ')}"

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
        robots_min_interval = None
        if self.settings.respect_robots_txt:
            parsed = urlparse(url)
            origin = f"{parsed.scheme}://{parsed.netloc}"
            rp, policy = self._load_robots(origin)
            if rp is not None and not rp.can_fetch(self.settings.user_agent, url):
                log_extra(self.log, logging.WARNING, "robots_disallowed", url=url)
                raise RobotsDisallowed(f"robots.txt disallows fetching {url}")
            if policy.visiting_hours is not None and not policy.visiting_hours.allows(now_utc()):
                log_extra(
                    self.log, logging.WARNING, "outside_visiting_hours",
                    url=url, visiting_hours=str(policy.visiting_hours),
                )
                raise RobotsDisallowed(
                    f"robots.txt Visiting-hours for {origin} ({policy.visiting_hours}) "
                    f"does not currently allow fetching"
                )
            robots_min_interval = policy.min_interval_seconds

        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        host = urlparse(url).netloc

        retryer = Retrying(
            stop=stop_after_attempt(self.settings.max_retries),
            wait=_retry_wait,
            retry=retry_if_exception(_is_retryable),
            reraise=True,
        )

        def _do_request() -> requests.Response:
            self._wait_for_slot(host, robots_min_interval)
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
                # A separate, explicit field alongside `url` — makes "queries per
                # origin" a trivial jq/grep over the session log directory (see
                # cli.py's session_log_dir) rather than needing to re-parse every
                # url's host at analysis time. Same value origin_pacing.py/
                # _wait_for_slot already pace against.
                origin=host,
                status=response.status_code,
                elapsed_ms=elapsed_ms,
            )
            # A second, separate log call (not folded into the one above) so
            # the ordinary INFO-level line stays a single terse row per
            # request even when DEBUG is on — this one carries everything
            # INFO doesn't: full request/response headers and a body
            # snippet. isEnabledFor guards it so the (sometimes large) body
            # is never even decoded unless DEBUG is actually on — see
            # --debug's help text in cli.py.
            if self.log.isEnabledFor(logging.DEBUG):
                log_extra(
                    self.log,
                    logging.DEBUG,
                    "http_request_detail",
                    method="GET",
                    url=url,
                    status=response.status_code,
                    request_headers=dict(response.request.headers),
                    response_headers=dict(response.headers),
                    response_body=debug_body_snippet(
                        response.content, response.headers.get("Content-Type", "")
                    ),
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
