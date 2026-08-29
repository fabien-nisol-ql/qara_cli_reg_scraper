import json

import responses

from qara_reg_scraper.base_scraper import PreviewInfo
from qara_reg_scraper.config import ServiceSettings
from qara_reg_scraper.manifest import Manifest
from qara_reg_scraper.service_client import ScraperServiceClient, ServiceSyncError
from qara_reg_scraper.storage.local import LocalStorage
from qara_reg_scraper.sync import sync, sync_source

BASE_URL = "http://reg-scraper:8080/api/reg-scraper"


def _client() -> ScraperServiceClient:
    return ScraperServiceClient(ServiceSettings(base_url=BASE_URL, timeout_seconds=5))


def _ok(body=None):
    return {"success": True, "status": 200, "data": body or {}}


@responses.activate
def test_sync_pushes_current_document_latest_run_and_estimate(tmp_path):
    storage = LocalStorage(root=str(tmp_path / "data"))
    manifest = Manifest(storage, "fda", "ecfr", run_id="run-1")  # no service_client - local-only writes
    manifest.save_document(
        "part-800", b"v1", url="u", title="Part 800", ext="xml",
        content_type="application/xml", http_status=200, source_metadata={"part": 800},
        original_filename="part-800.xml",
    )
    manifest.finalize()
    manifest.write_estimate(PreviewInfo(total_available=35, already_known=1, note="a note"))

    responses.add(responses.POST, f"{BASE_URL}/v1/documents", json=_ok(), status=200)
    responses.add(responses.POST, f"{BASE_URL}/v1/runs", json=_ok(), status=200)
    responses.add(responses.PUT, f"{BASE_URL}/v1/source-estimates/fda/ecfr", json=_ok(), status=200)

    counts = sync_source(_client(), storage, "fda", "ecfr")
    assert counts == {"documents": 1, "runs": 1, "estimate": 1}

    doc_body = json.loads(responses.calls[0].request.body)
    assert doc_body["documentId"] == "part-800"
    assert doc_body["title"] == "Part 800"
    assert doc_body["originalFilename"] == "part-800.xml"
    assert doc_body["sourceMetadata"] == {"part": 800}

    run_body = json.loads(responses.calls[1].request.body)
    assert run_body["runId"] == "run-1"
    assert run_body["status"] == "success"

    estimate_body = json.loads(responses.calls[2].request.body)
    assert estimate_body["totalAvailable"] == 35
    assert estimate_body["remaining"] == 34
    assert estimate_body["note"] == "a note"


@responses.activate
def test_sync_only_latest_run_file_is_pushed(tmp_path):
    # run_id embeds a sortable UTC timestamp (see manifest.py's new_run_id) -
    # lexical sort of the runs/ directory listing is chronological.
    storage = LocalStorage(root=str(tmp_path / "data"))
    m1 = Manifest(storage, "fda", "ecfr", run_id="fda-ecfr-20260101T000000Z-aaaaaaaa")
    m1.finalize()
    m2 = Manifest(storage, "fda", "ecfr", run_id="fda-ecfr-20260102T000000Z-bbbbbbbb")
    m2.finalize()

    responses.add(responses.POST, f"{BASE_URL}/v1/runs", json=_ok(), status=200)

    counts = sync_source(_client(), storage, "fda", "ecfr")
    assert counts["runs"] == 1
    assert len(responses.calls) == 1
    body = json.loads(responses.calls[0].request.body)
    assert body["runId"] == "fda-ecfr-20260102T000000Z-bbbbbbbb"  # the later one


@responses.activate
def test_sync_no_estimate_file_means_no_put_call_and_zero_count(tmp_path):
    storage = LocalStorage(root=str(tmp_path / "data"))
    manifest = Manifest(storage, "fda", "guidance", run_id="run-1")
    manifest.finalize()

    responses.add(responses.POST, f"{BASE_URL}/v1/runs", json=_ok(), status=200)

    counts = sync_source(_client(), storage, "fda", "guidance")
    assert counts["estimate"] == 0
    assert not any(c.request.url.endswith("/v1/source-estimates/fda/guidance") for c in responses.calls)


@responses.activate
def test_sync_survives_corrupt_document_meta_file(tmp_path):
    storage = LocalStorage(root=str(tmp_path / "data"))
    manifest = Manifest(storage, "fda", "ecfr", run_id="run-1")
    manifest.save_document(
        "part-800", b"v1", url="u", title="t", ext="xml", content_type="application/xml", http_status=200,
    )
    manifest.finalize()
    storage.write_text("fda/ecfr/documents/part-800/current.meta.json", "{not valid json")

    responses.add(responses.POST, f"{BASE_URL}/v1/runs", json=_ok(), status=200)

    counts = sync_source(_client(), storage, "fda", "ecfr")
    assert counts["documents"] == 0  # corrupt file skipped, not crashed
    assert counts["runs"] == 1


@responses.activate
def test_sync_never_touches_the_events_endpoint(tmp_path):
    """POST /v1/events is insert-only (see sync.py's module docstring) -
    sync must never bulk-replay the historical event log, only current
    state (documents/latest-run/estimate)."""
    storage = LocalStorage(root=str(tmp_path / "data"))
    manifest = Manifest(storage, "fda", "ecfr", run_id="run-1")
    manifest.save_document(
        "part-800", b"v1", url="u", title="t", ext="xml", content_type="application/xml", http_status=200,
    )
    manifest.record_error("part-999", url="u2", error="boom")
    manifest.finalize()

    responses.add(responses.POST, f"{BASE_URL}/v1/documents", json=_ok(), status=200)
    responses.add(responses.POST, f"{BASE_URL}/v1/runs", json=_ok(), status=200)

    sync_source(_client(), storage, "fda", "ecfr")
    assert all("/v1/events" not in c.request.url for c in responses.calls)


@responses.activate
def test_sync_propagates_service_sync_error_not_a_partial_silent_result(tmp_path):
    storage = LocalStorage(root=str(tmp_path / "data"))
    manifest = Manifest(storage, "fda", "ecfr", run_id="run-1")
    manifest.save_document(
        "part-800", b"v1", url="u", title="t", ext="xml", content_type="application/xml", http_status=200,
    )
    manifest.finalize()

    for _ in range(3):
        responses.add(responses.POST, f"{BASE_URL}/v1/documents", status=503)

    try:
        sync_source(_client(), storage, "fda", "ecfr")
        raise AssertionError("expected ServiceSyncError")
    except ServiceSyncError:
        pass


@responses.activate
def test_sync_keeps_different_regulations_isolated(tmp_path):
    storage = LocalStorage(root=str(tmp_path / "data"))
    m_fda = Manifest(storage, "fda", "ecfr", run_id="run-fda")
    m_fda.save_document(
        "part-800", b"fda content", url="u", title="FDA doc", ext="xml",
        content_type="application/xml", http_status=200,
    )
    m_fda.finalize()
    m_eu = Manifest(storage, "eu", "ecfr", run_id="run-eu")
    m_eu.save_document(
        "part-800", b"eu content", url="u", title="EU doc", ext="xml",
        content_type="application/xml", http_status=200,
    )
    m_eu.finalize()

    responses.add(responses.POST, f"{BASE_URL}/v1/documents", json=_ok(), status=200)
    responses.add(responses.POST, f"{BASE_URL}/v1/runs", json=_ok(), status=200)
    responses.add(responses.POST, f"{BASE_URL}/v1/documents", json=_ok(), status=200)
    responses.add(responses.POST, f"{BASE_URL}/v1/runs", json=_ok(), status=200)

    results = sync(_client(), storage, ["fda:ecfr", "eu:ecfr"])
    assert results["fda:ecfr"]["documents"] == 1
    assert results["eu:ecfr"]["documents"] == 1

    fda_doc_body = json.loads(responses.calls[0].request.body)
    eu_doc_body = json.loads(responses.calls[2].request.body)
    assert fda_doc_body["title"] == "FDA doc"
    assert eu_doc_body["title"] == "EU doc"
