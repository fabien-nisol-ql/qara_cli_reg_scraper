import json
import logging

from qara_reg_scraper.logging_setup import configure_logging, get_logger


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
