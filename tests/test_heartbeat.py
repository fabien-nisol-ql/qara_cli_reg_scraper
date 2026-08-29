import time

from rich.console import Console

from qara_reg_scraper.heartbeat import Heartbeat


def make_console_and_buffer():
    from io import StringIO

    buf = StringIO()
    return Console(file=buf, width=160, force_terminal=False), buf


def test_prints_a_heartbeat_line_after_silence_exceeds_interval():
    console, buf = make_console_and_buffer()
    with Heartbeat(console, interval=0.1):
        time.sleep(0.4)
    assert "still working" in buf.getvalue()


def test_beat_resets_the_silence_clock_so_no_line_prints():
    console, buf = make_console_and_buffer()
    with Heartbeat(console, interval=0.3) as hb:
        # Beat faster than the interval, several times, for longer than
        # one interval's worth of wall-clock time — should never fire.
        for _ in range(5):
            time.sleep(0.1)
            hb.beat()
    assert buf.getvalue() == ""


def test_disabled_heartbeat_never_prints():
    console, buf = make_console_and_buffer()
    with Heartbeat(console, interval=0.1, enabled=False):
        time.sleep(0.4)
    assert buf.getvalue() == ""


def test_set_activity_is_reflected_in_the_heartbeat_line():
    console, buf = make_console_and_buffer()
    with Heartbeat(console, interval=0.1) as hb:
        hb.set_activity("scraping fda:ecfr")
        time.sleep(0.4)
    assert "scraping fda:ecfr" in buf.getvalue()


def test_set_activity_alone_does_not_reset_the_silence_clock():
    """A phase change isn't visible progress on its own — it shouldn't
    delay the heartbeat that would otherwise have fired."""
    console, buf = make_console_and_buffer()
    with Heartbeat(console, interval=0.1) as hb:
        time.sleep(0.05)
        hb.set_activity("still the same long phase")
        time.sleep(0.15)
    assert "still the same long phase" in buf.getvalue()


def test_exiting_the_context_stops_the_background_thread():
    console, _buf = make_console_and_buffer()
    with Heartbeat(console, interval=0.1) as hb:
        pass
    assert hb._thread is not None
    assert not hb._thread.is_alive()
