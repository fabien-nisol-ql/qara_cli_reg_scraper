from qara_reg_scraper.base_scraper import PreviewInfo
from qara_reg_scraper.local_status import compute_source_summary
from qara_reg_scraper.manifest import Manifest
from qara_reg_scraper.storage.local import LocalStorage


def test_summary_of_untouched_source_is_all_zero(tmp_path):
    storage = LocalStorage(root=str(tmp_path))
    s = compute_source_summary(storage, "fda", "ecfr")
    assert s.documents == 0
    assert s.last_run_id is None
    assert s.last_status is None
    assert s.total_available is None
    assert s.remaining is None


def test_summary_reflects_a_written_estimate_snapshot(tmp_path):
    storage = LocalStorage(root=str(tmp_path))
    manifest = Manifest(storage, "fda", "ecfr", run_id="run-1")
    manifest.write_estimate(PreviewInfo(total_available=35, already_known=10, note="a note"))

    s = compute_source_summary(storage, "fda", "ecfr")
    assert s.total_available == 35
    assert s.already_known == 10
    assert s.remaining == 25
    assert s.estimate_note == "a note"
    assert s.estimate_computed_at


def test_summary_reflects_documents_and_latest_run(tmp_path):
    storage = LocalStorage(root=str(tmp_path))
    manifest = Manifest(storage, "fda", "ecfr", run_id="run-1")
    manifest.save_document(
        "part-800", b"v1", url="u", title="t", ext="xml",
        content_type="application/xml", http_status=200,
    )
    manifest.record_error("part-999", url="u2", error="boom")
    manifest.finalize()

    s = compute_source_summary(storage, "fda", "ecfr")
    assert s.documents == 1
    assert s.last_run_id == "run-1"
    assert s.last_status == "partial_failure"
    assert s.last_errors == 1
    assert s.last_new == 1


def test_summary_picks_the_latest_of_multiple_runs(tmp_path):
    storage = LocalStorage(root=str(tmp_path))
    Manifest(storage, "fda", "ecfr", run_id="fda-ecfr-20200101T000000Z-aaa").finalize()
    Manifest(storage, "fda", "ecfr", run_id="fda-ecfr-20260101T000000Z-bbb").finalize()

    s = compute_source_summary(storage, "fda", "ecfr")
    assert s.last_run_id == "fda-ecfr-20260101T000000Z-bbb"


def test_summary_is_scoped_to_its_own_regulation_and_source(tmp_path):
    storage = LocalStorage(root=str(tmp_path))
    Manifest(storage, "fda", "ecfr", run_id="run-fda").finalize()
    Manifest(storage, "eu", "ecfr", run_id="run-eu").finalize()
    Manifest(storage, "fda", "guidance", run_id="run-guidance").finalize()

    s = compute_source_summary(storage, "fda", "ecfr")
    assert s.last_run_id == "run-fda"
