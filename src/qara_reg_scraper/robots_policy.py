"""Parses what a robots.txt says about being a well-behaved automated
client, beyond plain ``Disallow`` (that part stays exactly what it always
was — ``urllib.robotparser.RobotFileParser``, used as-is in
http_client.py). Two things specifically:

- ``Crawl-delay`` — a standard directive ``RobotFileParser`` already
  parses (``.crawl_delay(user_agent)``) but that, before this module,
  nothing in this codebase ever actually read or applied to the request
  rate.
- ``Hit-rate`` / ``Visiting-hours`` — non-standard directives that
  ``RobotFileParser`` silently ignores entirely (they're not part of the
  robots.txt spec it implements). Confirmed live on
  ``accessdata.fda.gov/robots.txt`` — the exact host behind the PDF
  fetches in clearances_510k.py/pma.py/hde.py:

    Hit-rate: 30 # wait 30 seconds before starting a new URL request
    Visiting-hours: 23:00EDT-05:00EDT #index this site between 11PM - 5AM EDT

Nothing about any of this is source- or host-specific in code — no
FDA-only special-casing anywhere here or in http_client.py. Whatever a
given host's own robots.txt declares is what applies to any scraper
talking to that host, automatically; a source that talks to a host with
no such directives (most of them) is completely unaffected. This is the
literal, load-bearing meaning of "robots.txt is honored" (see
http_client.py's and README.md's own words) — extended to cover every
directive a host actually publishes, not just Disallow.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from datetime import time as dtime
from urllib.parse import quote
from urllib.robotparser import RobotFileParser
from zoneinfo import ZoneInfo

from .logging_setup import get_logger, log_extra

log = get_logger("robots_policy")

# robots.txt's Visiting-hours directive isn't part of any published spec
# found for it (accessdata.fda.gov appears to be following an older,
# informal .gov convention) — it uses US timezone abbreviations, not IANA
# zone keys, so this is a best-effort mapping covering the zones a .gov
# site is realistic to publish. An unrecognized abbreviation means the
# window is simply not enforced (fails open — same "don't act on what we
# can't understand" posture as an unreachable robots.txt entirely) rather
# than raising or guessing.
_TZ_ABBREVIATIONS = {
    "EST": "America/New_York", "EDT": "America/New_York",
    "CST": "America/Chicago", "CDT": "America/Chicago",
    "MST": "America/Denver", "MDT": "America/Denver",
    "PST": "America/Los_Angeles", "PDT": "America/Los_Angeles",
    "UTC": "UTC", "GMT": "UTC",
}

_HIT_RATE_RE = re.compile(r"(?im)^\s*Hit-rate\s*:\s*(\d+(?:\.\d+)?)")
_VISITING_HOURS_RE = re.compile(
    r"(?im)^\s*Visiting-hours\s*:\s*(\d{1,2}):(\d{2})\s*([A-Za-z]+)\s*-\s*(\d{1,2}):(\d{2})\s*([A-Za-z]+)"
)


@dataclass
class VisitingHours:
    """One host's declared "only crawl during this window" policy, in its
    own local time — e.g. 23:00-05:00 America/New_York for
    accessdata.fda.gov's published 23:00EDT-05:00EDT. The published
    abbreviation (EDT vs EST) only picks the zone *family*; the actual
    UTC offset applied always reflects whatever DST state that zone is
    really in at the moment being checked, via zoneinfo — not a
    hardcoded fixed offset, which would drift wrong for half the year."""

    start: dtime
    end: dtime
    zone: ZoneInfo

    def allows(self, now_utc: datetime) -> bool:
        local = now_utc.astimezone(self.zone).time()
        if self.start <= self.end:
            return self.start <= local < self.end
        # Wraps past midnight (23:00-05:00): "inside the window" means at
        # or after the start OR before the end, not strictly between them.
        return local >= self.start or local < self.end

    def next_open_at(self, now_utc: datetime) -> datetime:
        """The next UTC moment this window opens. Only meaningful (and
        only ever called) when `now_utc` is currently OUTSIDE the window —
        see `PoliteHttpClient.next_available_at`, the one caller — but the
        formula itself doesn't need to special-case wrapping vs
        non-wrapping windows: "today's start time, or tomorrow's if
        today's has already passed" is correct either way once we already
        know we're outside."""
        local_now = now_utc.astimezone(self.zone)
        candidate = local_now.replace(
            hour=self.start.hour, minute=self.start.minute, second=0, microsecond=0
        )
        if candidate <= local_now:
            candidate += timedelta(days=1)
        return candidate.astimezone(UTC)

    def __str__(self) -> str:
        return f"{self.start:%H:%M}-{self.end:%H:%M} {self.zone.key}"


@dataclass
class RobotsPolicy:
    """Everything one host's robots.txt says about pacing/timing, beyond
    plain Disallow (that stays on the caller's own RobotFileParser)."""

    #: Minimum seconds between requests this host asks for — from
    #: Hit-rate if published (the more specific, purpose-built directive
    #: where it exists), else Crawl-delay, else None (host expressed no
    #: preference; the caller's own configured rate applies unmodified).
    min_interval_seconds: float | None
    #: When this host asks to only be crawled, or None for no restriction.
    visiting_hours: VisitingHours | None

    @property
    def has_restrictions(self) -> bool:
        return self.min_interval_seconds is not None or self.visiting_hours is not None

    def to_dict(self) -> dict:
        return {
            "min_interval_seconds": self.min_interval_seconds,
            "visiting_hours": (
                {
                    "start": self.visiting_hours.start.isoformat(),
                    "end": self.visiting_hours.end.isoformat(),
                    "zone": self.visiting_hours.zone.key,
                }
                if self.visiting_hours
                else None
            ),
        }

    @classmethod
    def from_dict(cls, data: dict) -> RobotsPolicy:
        vh_data = data.get("visiting_hours")
        visiting_hours = None
        if vh_data:
            visiting_hours = VisitingHours(
                start=dtime.fromisoformat(vh_data["start"]),
                end=dtime.fromisoformat(vh_data["end"]),
                zone=ZoneInfo(vh_data["zone"]),
            )
        return cls(min_interval_seconds=data.get("min_interval_seconds"), visiting_hours=visiting_hours)


def parse_robots_policy(raw_text: str, user_agent: str, parser: RobotFileParser | None) -> RobotsPolicy:
    """`raw_text` is the exact robots.txt body already fetched (and fed to
    `parser`, if given) by the caller — parsed here again only for the
    directives RobotFileParser itself doesn't expose."""
    hit_rate_match = _HIT_RATE_RE.search(raw_text)
    min_interval = float(hit_rate_match.group(1)) if hit_rate_match else None
    if min_interval is None and parser is not None:
        delay = parser.crawl_delay(user_agent)
        if delay is not None:
            min_interval = float(delay)

    visiting_hours = None
    vh_match = _VISITING_HOURS_RE.search(raw_text)
    if vh_match:
        # The end side's own timezone abbreviation is intentionally unused
        # — every real example found (and the directive's own convention)
        # repeats the same one on both sides ("23:00EDT-05:00EDT"); the
        # start side's zone is treated as authoritative for the whole
        # window rather than requiring (or acting on) the two matching.
        start_h, start_m, start_tz, end_h, end_m, _end_tz = vh_match.groups()
        zone_name = _TZ_ABBREVIATIONS.get(start_tz.upper())
        if zone_name:
            visiting_hours = VisitingHours(
                start=dtime(int(start_h) % 24, int(start_m)),
                end=dtime(int(end_h) % 24, int(end_m)),
                zone=ZoneInfo(zone_name),
            )

    return RobotsPolicy(min_interval_seconds=min_interval, visiting_hours=visiting_hours)


def now_utc() -> datetime:
    """Thin wrapper so tests can monkeypatch "now" in one place."""
    return datetime.now(UTC)


# -- persistence across process runs -----------------------------------
#
# A fresh `qara-reg-scraper run` is a brand-new process every time (one CLI
# invocation per job, no long-lived daemon) — PoliteHttpClient's own
# in-memory `_robots_cache` starts empty on every single run, so without
# this, a robots.txt fetch that gets blocked (a bot-management challenge
# page served instead of the real file — confirmed live, repeatedly, on
# accessdata.fda.gov while building this) leaves the scraper with *zero*
# memory of a policy it may have successfully learned on a completely
# different, earlier run. That's a real, closed loop: the more a host's
# bot management flares up, the less this tool can even see the very
# policy that would let it behave well enough to calm it back down.
#
# So a successfully-parsed policy (Hit-rate/Crawl-delay/Visiting-hours) is
# persisted through the caller's own StorageBackend (whatever
# storage.backend config.yaml already points at — local disk, S3, Azure
# Blob, SharePoint, no new dependency) and reused as the fallback the next
# time a fetch for that same origin is blocked or unreachable, instead of
# silently reverting to "no restriction known." One file per origin,
# shared across every source that happens to talk to it (the policy
# belongs to the host, not to any one scraper) — whichever source learns
# it first benefits every other one immediately, including ones that have
# never once successfully reached that host's real robots.txt themselves.
_CACHE_DIR = "_robots_cache"


def origin_slug(origin: str) -> str:
    """A stable, human-inspectable filename fragment per origin/host — not
    a hash, since there are only ever a handful of distinct hosts scraped
    at all, no collision/length concern worth trading readability away
    for. Public (not just this module's own `_cache_path`) so
    `origin_pacing.py` can key its own, separate per-origin state files
    the same recognizable way — same host, same-looking filename, just
    under a different directory (`_origin_pacing/` instead of
    `_robots_cache/`). Works the same whether given a full origin
    (`https://host`) or a bare host — the scheme prefix, if any, is
    just stripped."""
    return quote(origin.replace("https://", "").replace("http://", ""), safe="")


def _cache_path(origin: str) -> str:
    return f"{_CACHE_DIR}/{origin_slug(origin)}.json"


def load_cached_policy(storage, origin: str) -> RobotsPolicy | None:
    """`storage` is a `qara_reg_scraper.storage.base.StorageBackend`
    (typed loosely here to avoid a hard import-time dependency in the
    common case this is never used at all — respect_robots_txt=False, or
    a host with nothing worth caching). None if nothing's been learned
    yet, or `storage` itself is None (a caller that hasn't opted in)."""
    if storage is None:
        return None
    path = _cache_path(origin)
    try:
        raw = storage.read_text(path)
    except FileNotFoundError:
        return None
    except Exception as e:  # noqa: BLE001 - a corrupt/unreadable cache entry must
        # never break scraping - worst case, this behaves exactly like
        # "nothing learned yet."
        log_extra(log, logging.WARNING, "robots_policy_cache_unreadable", origin=origin, error=str(e))
        return None
    try:
        return RobotsPolicy.from_dict(json.loads(raw))
    except Exception as e:  # noqa: BLE001 - same reasoning
        log_extra(log, logging.WARNING, "robots_policy_cache_corrupt", origin=origin, error=str(e))
        return None


def save_cached_policy(storage, origin: str, policy: RobotsPolicy) -> None:
    """Only ever called after a genuinely successful robots.txt fetch+parse
    - never with a policy learned from a cache read, so a stale entry
    can't perpetuate itself indefinitely once a real fetch succeeds again
    (a fresh success always overwrites, even with fewer restrictions than
    what was cached before). No-op for a policy with nothing worth
    remembering - keeps the cache directory limited to hosts that
    actually declared something."""
    if storage is None or not policy.has_restrictions:
        return
    storage.write_text(_cache_path(origin), json.dumps(policy.to_dict(), indent=2))
