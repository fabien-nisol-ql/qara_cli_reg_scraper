from datetime import UTC, datetime
from urllib.robotparser import RobotFileParser
from zoneinfo import ZoneInfo

from qara_reg_scraper.robots_policy import (
    RobotsPolicy,
    VisitingHours,
    _cache_path,
    load_cached_policy,
    parse_robots_policy,
    save_cached_policy,
)

ACCESSDATA_ROBOTS_TXT = """\
#robots.txt file for Accessdata

User-agent: *
Disallow: /scripts/cdrh/cfdocs/cfClia/

Hit-rate: 30 # wait 30 seconds before starting a new URL request default=30
Visiting-hours: 23:00EDT-05:00EDT #index this site between 11PM - 5AM EDT
Concurrent-hits: 2 # limit concurrent active URLS to 2 for each index server
"""


def test_parses_the_real_accessdata_fda_gov_robots_txt():
    """The exact live directives confirmed on accessdata.fda.gov while
    building this — the whole reason this module exists."""
    policy = parse_robots_policy(ACCESSDATA_ROBOTS_TXT, "qara-reg-scraper", parser=None)
    assert policy.min_interval_seconds == 30.0
    assert policy.visiting_hours is not None
    assert policy.visiting_hours.zone.key == "America/New_York"


def test_no_directives_at_all_means_no_restrictions():
    policy = parse_robots_policy("User-agent: *\nDisallow:\n", "qara-reg-scraper", parser=None)
    assert policy.min_interval_seconds is None
    assert policy.visiting_hours is None
    assert policy.has_restrictions is False


def test_falls_back_to_crawl_delay_when_no_hit_rate_is_present():
    """Standard Crawl-delay, parsed via the real RobotFileParser (already
    supports this directive) — Hit-rate only wins when a host actually
    publishes it."""
    text = "User-agent: *\nCrawl-delay: 10\n"
    rp = RobotFileParser()
    rp.parse(text.splitlines())
    policy = parse_robots_policy(text, "qara-reg-scraper", parser=rp)
    assert policy.min_interval_seconds == 10.0


def test_hit_rate_wins_over_crawl_delay_when_both_are_present():
    text = "User-agent: *\nCrawl-delay: 10\nHit-rate: 45\n"
    rp = RobotFileParser()
    rp.parse(text.splitlines())
    policy = parse_robots_policy(text, "qara-reg-scraper", parser=rp)
    assert policy.min_interval_seconds == 45.0


def test_unrecognized_timezone_abbreviation_fails_open_not_raises():
    """An unrecognized abbreviation must not enforce a window (fail open,
    same posture as an unreachable robots.txt entirely) rather than crash
    a whole run over one line it can't confidently interpret."""
    text = "Visiting-hours: 23:00ZZZ-05:00ZZZ\n"
    policy = parse_robots_policy(text, "qara-reg-scraper", parser=None)
    assert policy.visiting_hours is None


def test_visiting_hours_case_insensitive_directive_name():
    text = "visiting-hours: 23:00EDT-05:00EDT\n"
    policy = parse_robots_policy(text, "qara-reg-scraper", parser=None)
    assert policy.visiting_hours is not None


class TestVisitingHoursAllows:
    def test_inside_a_same_day_window(self):
        vh = VisitingHours(start=_time(9, 0), end=_time(17, 0), zone=ZoneInfo("UTC"))
        assert vh.allows(datetime(2026, 6, 15, 12, 0, tzinfo=UTC)) is True

    def test_outside_a_same_day_window(self):
        vh = VisitingHours(start=_time(9, 0), end=_time(17, 0), zone=ZoneInfo("UTC"))
        assert vh.allows(datetime(2026, 6, 15, 20, 0, tzinfo=UTC)) is False

    def test_inside_a_midnight_wrapping_window_after_start(self):
        vh = VisitingHours(start=_time(23, 0), end=_time(5, 0), zone=ZoneInfo("UTC"))
        assert vh.allows(datetime(2026, 6, 15, 23, 30, tzinfo=UTC)) is True

    def test_inside_a_midnight_wrapping_window_before_end(self):
        vh = VisitingHours(start=_time(23, 0), end=_time(5, 0), zone=ZoneInfo("UTC"))
        assert vh.allows(datetime(2026, 6, 15, 3, 0, tzinfo=UTC)) is True

    def test_outside_a_midnight_wrapping_window(self):
        vh = VisitingHours(start=_time(23, 0), end=_time(5, 0), zone=ZoneInfo("UTC"))
        assert vh.allows(datetime(2026, 6, 15, 12, 0, tzinfo=UTC)) is False

    def test_respects_the_declared_zone_not_utc(self):
        """23:00 EDT (UTC-4 in June) is 03:00 UTC - a naive UTC-only check
        would get this wrong by a full four hours."""
        vh = VisitingHours(start=_time(23, 0), end=_time(5, 0), zone=ZoneInfo("America/New_York"))
        assert vh.allows(datetime(2026, 6, 15, 3, 0, tzinfo=UTC)) is True
        assert vh.allows(datetime(2026, 6, 15, 12, 0, tzinfo=UTC)) is False


class TestVisitingHoursNextOpenAt:
    def test_before_todays_start_returns_today(self):
        """05:00 UTC, window opens 23:00 UTC - today's start hasn't
        happened yet, so the next opening is later today, not tomorrow."""
        vh = VisitingHours(start=_time(23, 0), end=_time(5, 0), zone=ZoneInfo("UTC"))
        next_open = vh.next_open_at(datetime(2026, 6, 15, 12, 0, tzinfo=UTC))
        assert next_open == datetime(2026, 6, 15, 23, 0, tzinfo=UTC)

    def test_after_todays_start_returns_tomorrow(self):
        vh = VisitingHours(start=_time(9, 0), end=_time(17, 0), zone=ZoneInfo("UTC"))
        next_open = vh.next_open_at(datetime(2026, 6, 15, 20, 0, tzinfo=UTC))
        assert next_open == datetime(2026, 6, 16, 9, 0, tzinfo=UTC)

    def test_respects_the_declared_zone_and_converts_back_to_utc(self):
        """23:00 EDT (UTC-4 in June) - a naive UTC-only computation would
        get the returned UTC instant wrong by a full four hours."""
        vh = VisitingHours(start=_time(23, 0), end=_time(5, 0), zone=ZoneInfo("America/New_York"))
        next_open = vh.next_open_at(datetime(2026, 6, 15, 12, 0, tzinfo=UTC))  # 08:00 EDT, outside
        assert next_open == datetime(2026, 6, 16, 3, 0, tzinfo=UTC)  # 23:00 EDT == 03:00 UTC next day


def _time(hour: int, minute: int):
    from datetime import time

    return time(hour, minute)


class _FakeStorage:
    """Minimal in-memory stand-in for storage.base.StorageBackend — just
    enough of write_text/read_text for load_cached_policy/save_cached_policy."""

    def __init__(self):
        self.files: dict[str, str] = {}

    def write_text(self, path, text, *, encoding="utf-8"):
        self.files[path] = text

    def read_text(self, path, *, encoding="utf-8"):
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]


def test_load_cached_policy_returns_none_when_storage_is_none():
    assert load_cached_policy(None, "https://example.gov") is None


def test_load_cached_policy_returns_none_when_nothing_cached_yet():
    assert load_cached_policy(_FakeStorage(), "https://example.gov") is None


def test_save_then_load_round_trips_a_policy_with_visiting_hours():
    storage = _FakeStorage()
    policy = RobotsPolicy(
        min_interval_seconds=30.0,
        visiting_hours=VisitingHours(start=_time(23, 0), end=_time(5, 0), zone=ZoneInfo("America/New_York")),
    )
    save_cached_policy(storage, "https://www.accessdata.fda.gov", policy)

    loaded = load_cached_policy(storage, "https://www.accessdata.fda.gov")
    assert loaded.min_interval_seconds == 30.0
    assert loaded.visiting_hours.start == _time(23, 0)
    assert loaded.visiting_hours.end == _time(5, 0)
    assert loaded.visiting_hours.zone.key == "America/New_York"


def test_save_cached_policy_is_a_noop_for_a_policy_with_nothing_worth_remembering():
    storage = _FakeStorage()
    save_cached_policy(storage, "https://example.gov", RobotsPolicy(min_interval_seconds=None, visiting_hours=None))
    assert storage.files == {}


def test_different_origins_get_separate_cache_entries():
    storage = _FakeStorage()
    save_cached_policy(storage, "https://a.gov", RobotsPolicy(min_interval_seconds=10.0, visiting_hours=None))
    save_cached_policy(storage, "https://b.gov", RobotsPolicy(min_interval_seconds=20.0, visiting_hours=None))

    assert load_cached_policy(storage, "https://a.gov").min_interval_seconds == 10.0
    assert load_cached_policy(storage, "https://b.gov").min_interval_seconds == 20.0


def test_load_cached_policy_tolerates_corrupt_json_without_raising():
    storage = _FakeStorage()
    storage.write_text(_cache_path("https://example.gov"), "not valid json{{{")
    assert load_cached_policy(storage, "https://example.gov") is None
