import json
import time

from qara_reg_scraper.origin_pacing import reserve_next_slot


def test_none_storage_root_is_a_noop():
    """No shared filesystem available - matches storage=None/a non-local
    backend, see StorageBackend.local_root()'s own docstring. Must never
    wait or raise."""
    start = time.monotonic()
    reserve_next_slot(None, "example.gov", 5.0)
    assert time.monotonic() - start < 0.1


def test_zero_interval_is_a_noop(tmp_path):
    reserve_next_slot(tmp_path, "example.gov", 0.0)
    reserve_next_slot(tmp_path, "example.gov", 0.0)
    # Two calls, no interval requested - neither should have waited at all,
    # and no state file should even have been written.
    assert not (tmp_path / "_origin_pacing").exists()


def test_second_call_for_the_same_host_waits_for_the_interval(tmp_path):
    """The core guarantee: two SEPARATE calls (standing in for two
    separate processes/PoliteHttpClient instances - see
    test_http_client.py's own cross-instance test for the full picture)
    against the same storage_root+host are paced against each other, not
    just within one caller."""
    start = time.monotonic()
    reserve_next_slot(tmp_path, "accessdata.fda.gov", 0.05)
    reserve_next_slot(tmp_path, "accessdata.fda.gov", 0.05)
    elapsed = time.monotonic() - start
    assert elapsed >= 0.04


def test_different_hosts_do_not_block_each_other(tmp_path):
    reserve_next_slot(tmp_path, "example.gov", 5.0)
    start = time.monotonic()
    reserve_next_slot(tmp_path, "a-completely-different-host.gov", 5.0)
    assert time.monotonic() - start < 0.5


def test_a_corrupt_state_file_degrades_to_proceeding_immediately(tmp_path):
    """A previous write that never completed, or was hand-edited into
    garbage, must never wedge every future request to this host - see
    module docstring: this whole mechanism is best-effort, not a hard
    requirement."""
    pacing_dir = tmp_path / "_origin_pacing"
    pacing_dir.mkdir()
    (pacing_dir / "example.gov.json").write_text("{not valid json")

    start = time.monotonic()
    reserve_next_slot(tmp_path, "example.gov", 5.0)
    assert time.monotonic() - start < 0.5


def test_a_lock_acquisition_failure_degrades_to_proceeding_immediately(tmp_path, monkeypatch):
    """A lock left behind by a killed/crashed process (or any other
    filelock-level failure) must never permanently block requests to this
    host - degrades to "proceed immediately," same as a corrupt state
    file, per this module's own documented tradeoff."""
    import qara_reg_scraper.origin_pacing as origin_pacing_module

    def _raising_filelock(*args, **kwargs):
        raise RuntimeError("simulated lock failure")

    monkeypatch.setattr(origin_pacing_module, "FileLock", _raising_filelock)

    start = time.monotonic()
    reserve_next_slot(tmp_path, "example.gov", 5.0)
    assert time.monotonic() - start < 0.5


def test_state_file_records_a_plausible_next_allowed_at(tmp_path):
    """Not just "did it wait," but "did it persist something a later,
    separate call can actually read back" - the whole point of moving
    this out of process memory."""
    before = time.time()
    reserve_next_slot(tmp_path, "example.gov", 10.0)
    after = time.time()

    state = json.loads((tmp_path / "_origin_pacing" / "example.gov.json").read_text())
    assert before + 10.0 <= state["next_allowed_at"] <= after + 10.0
