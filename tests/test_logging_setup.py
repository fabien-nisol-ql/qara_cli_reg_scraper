import json
import logging
import os
import time

from qara_reg_scraper.logging_setup import (
    DEBUG_BODY_MAX_CHARS,
    _sweep_old_session_logs,
    configure_logging,
    configure_session_log,
    debug_body_snippet,
    get_logger,
)


def test_no_sink_by_default_means_no_handler_output(capsys):
    configure_logging("INFO", sink=None)
    log = get_logger("test.silent")
    log.info("should not appear anywhere")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_sink_writes_json_lines_to_the_given_file(tmp_path):
    path = tmp_path / "qara.log"
    configure_logging("INFO", sink=str(path))
    log = get_logger("test.file")
    log.info("hello")

    lines = path.read_text().strip().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["message"] == "hello"
    assert payload["logger"] == "qara_reg_scraper.test.file"
    assert payload["level"] == "INFO"


def test_reconfigure_switches_sink_cleanly(tmp_path):
    """A later call must fully replace the earlier one — no duplicate
    lines from a stale handler left attached."""
    first_path = tmp_path / "first.log"
    second_path = tmp_path / "second.log"
    log = get_logger("test.switch")

    configure_logging("INFO", sink=str(first_path))
    log.info("to first")

    configure_logging("INFO", sink=str(second_path))
    log.info("to second")

    assert "to first" in first_path.read_text()
    assert "to second" not in first_path.read_text()
    assert "to second" in second_path.read_text()


def test_reconfigure_to_no_sink_silences_a_previously_configured_logger(tmp_path):
    path = tmp_path / "qara.log"
    log = get_logger("test.silence-again")

    configure_logging("INFO", sink=str(path))
    log.info("first")

    configure_logging("INFO", sink=None)
    log.info("second, should not be written")

    assert path.read_text().strip().splitlines().__len__() == 1
    assert "second" not in path.read_text()


def test_level_still_filters_below_configured_level(tmp_path):
    path = tmp_path / "qara.log"
    configure_logging("WARNING", sink=str(path))
    log = get_logger("test.level")
    log.info("info, filtered out")
    log.warning("warning, kept")

    lines = path.read_text().strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["message"] == "warning, kept"

    # Restore a sane state for any test that runs after this one in the
    # same process (root logger is a shared singleton).
    configure_logging("INFO", sink=None)


def test_root_qara_logger_level_is_set(tmp_path):
    configure_logging("DEBUG", sink=str(tmp_path / "x.log"))
    assert logging.getLogger("qara_reg_scraper").level == logging.DEBUG
    configure_logging("INFO", sink=None)


def test_debug_body_snippet_shows_short_text_bodies_verbatim():
    assert debug_body_snippet(b'{"ok": true}', "application/json") == '{"ok": true}'


def test_debug_body_snippet_truncates_long_text_bodies():
    body = ("x" * (DEBUG_BODY_MAX_CHARS + 500)).encode()
    snippet = debug_body_snippet(body, "text/html")
    assert snippet.startswith("x" * DEBUG_BODY_MAX_CHARS)
    assert "truncated" in snippet
    assert str(len(body)) in snippet


def test_debug_body_snippet_never_decodes_binary_content():
    """A PDF/image body must never be decoded as text — only its size and
    content-type are shown, regardless of length."""
    body = b"%PDF-1.4 \xff\xfe binary garbage that is not valid utf-8 on its own \x00\x01"
    snippet = debug_body_snippet(body, "application/pdf")
    assert snippet == f"<{len(body)} bytes, content-type='application/pdf' — not shown>"


def test_debug_body_snippet_handles_invalid_utf8_in_a_text_content_type_gracefully():
    """A text-labeled body that still isn't valid UTF-8 must not raise —
    decode with replacement characters rather than crashing debug logging
    itself over a malformed response."""
    body = b"not quite utf-8: \xff\xfe"
    snippet = debug_body_snippet(body, "text/plain")
    assert "not quite utf-8" in snippet


def test_configure_session_log_writes_a_uniquely_named_json_lines_file(tmp_path):
    log_dir = tmp_path / "_session_logs"
    path = configure_session_log(log_dir, "run", retention_days=90)

    assert path is not None
    assert path.parent == log_dir
    assert path.name.startswith("run-")
    assert path.suffix == ".jsonl"

    log = get_logger("test.session")
    log.info("hello from a session")
    payload = json.loads(path.read_text().strip())
    assert payload["message"] == "hello from a session"

    configure_logging("INFO", sink=None)  # reset the shared root logger for later tests


def test_configure_session_log_adds_to_not_replace_the_existing_sink(tmp_path):
    """Unlike configure_logging() (a from-scratch reset), this must be
    additive — a session log alongside an already-configured --log sink,
    not instead of it."""
    fixed_sink = tmp_path / "fixed.log"
    configure_logging("INFO", sink=str(fixed_sink))

    session_path = configure_session_log(tmp_path / "_session_logs", "run", retention_days=90)

    log = get_logger("test.additive")
    log.info("goes to both")

    assert "goes to both" in fixed_sink.read_text()
    assert "goes to both" in session_path.read_text()

    configure_logging("INFO", sink=None)


def test_configure_session_log_returns_none_and_does_not_raise_when_the_directory_is_unusable(tmp_path):
    """A read-only/missing-parent log_dir must degrade gracefully — a run
    whose only problem is its own activity logging must still complete."""
    blocking_file = tmp_path / "not_a_directory"
    blocking_file.write_text("")

    path = configure_session_log(blocking_file / "_session_logs", "run", retention_days=90)

    assert path is None
    configure_logging("INFO", sink=None)


def test_sweep_deletes_files_older_than_retention_and_keeps_newer_ones(tmp_path):
    old = tmp_path / "old.jsonl"
    new = tmp_path / "new.jsonl"
    old.write_text("{}")
    new.write_text("{}")
    old_time = time.time() - 200 * 86400  # 200 days ago
    os.utime(old, (old_time, old_time))

    _sweep_old_session_logs(tmp_path, retention_days=90)

    assert not old.exists()
    assert new.exists()


def test_sweep_ignores_non_matching_files(tmp_path):
    other = tmp_path / "unrelated.txt"
    other.write_text("keep me")
    old_time = time.time() - 200 * 86400
    os.utime(other, (old_time, old_time))

    _sweep_old_session_logs(tmp_path, retention_days=90)

    assert other.exists()


def test_sweep_is_a_noop_when_retention_days_is_not_positive(tmp_path):
    path = tmp_path / "whatever.jsonl"
    path.write_text("{}")
    old_time = time.time() - 400 * 86400
    os.utime(path, (old_time, old_time))

    _sweep_old_session_logs(tmp_path, retention_days=0)

    assert path.exists()  # retention disabled, not "delete everything"
