import json

import pytest

from qara_reg_scraper.base_scraper import PreviewInfo
from qara_reg_scraper.manifest import Manifest, estimate_path
from qara_reg_scraper.service_client import ServiceSyncError
from qara_reg_scraper.storage.local import LocalStorage


def make_manifest(tmp_path, regulation="fda", source="ecfr", run_id="test-run-1"):
    storage = LocalStorage(root=str(tmp_path))
    return storage, Manifest(storage, regulation, source, run_id=run_id)


class _FakeServiceClient:
    """Test double standing in for ScraperServiceClient - records every
    call instead of making a real HTTP request, and can be told to raise
    ServiceSyncError to exercise the "not caught inside Manifest" contract."""

    def __init__(self, fail_on: set[str] | None = None):
        self.calls: list[tuple[str, tuple]] = []
        self._fail_on = fail_on or set()

    def _record_or_raise(self, name, *args):
        self.calls.append((name, args))
        if name in self._fail_on:
            raise ServiceSyncError(name, "simulated failure")
        return {}

    def upsert_run(self, dto):
        return self._record_or_raise("upsert_run", dto)

    def record_event(self, dto):
        return self._record_or_raise("record_event", dto)

    def upsert_document(self, dto):
        return self._record_or_raise("upsert_document", dto)

    def put_source_estimate(self, regulation, source, dto):
        return self._record_or_raise("put_source_estimate", regulation, source, dto)


def test_new_document_is_stored_and_recorded(tmp_path):
    storage, manifest = make_manifest(tmp_path)
    result = manifest.save_document(
        "part-800",
        b"<xml>v1</xml>",
        url="https://example.gov/part-800",
        title="21 CFR Part 800",
        ext="xml",
        content_type="application/xml",
        http_status=200,
        source_metadata={"part": 800},
    )
    assert result.event == "new"
    assert storage.read_bytes("fda/ecfr/documents/part-800/current.xml") == b"<xml>v1</xml>"
    meta = json.loads(storage.read_text("fda/ecfr/documents/part-800/current.meta.json"))
    assert meta["current_hash"] == result.content_hash
    assert meta["title"] == "21 CFR Part 800"
    assert len(meta["version_history"]) == 1


def test_new_document_records_original_filename(tmp_path):
    storage, manifest = make_manifest(tmp_path)
    manifest.save_document(
        "part-800", b"<xml>v1</xml>", url="https://example.gov/part-800",
        title="21 CFR Part 800", ext="xml", content_type="application/xml",
        http_status=200, original_filename="part-800.xml",
    )
    meta = json.loads(storage.read_text("fda/ecfr/documents/part-800/current.meta.json"))
    assert meta["original_filename"] == "part-800.xml"


def test_unchanged_document_backfills_missing_original_filename(tmp_path):
    storage, manifest = make_manifest(tmp_path)
    manifest.save_document(
        "part-800", b"same bytes", url="u", title="t", ext="xml",
        content_type="application/xml", http_status=200,  # no original_filename yet
    )
    manifest.save_document(
        "part-800", b"same bytes", url="u", title="t", ext="xml",
        content_type="application/xml", http_status=200, original_filename="part-800.xml",
    )
    meta = json.loads(storage.read_text("fda/ecfr/documents/part-800/current.meta.json"))
    assert meta["original_filename"] == "part-800.xml"


def test_unchanged_document_does_not_rewrite_or_version(tmp_path):
    storage, manifest = make_manifest(tmp_path)
    manifest.save_document(
        "part-800", b"same bytes", url="u", title="t", ext="xml",
        content_type="application/xml", http_status=200,
    )
    result = manifest.save_document(
        "part-800", b"same bytes", url="u", title="t", ext="xml",
        content_type="application/xml", http_status=200,
    )
    assert result.event == "unchanged"
    assert list(storage.list("fda/ecfr/documents/part-800/versions")) == []


def test_changed_document_archives_previous_version(tmp_path):
    storage, manifest = make_manifest(tmp_path)
    manifest.save_document(
        "part-800", b"version one", url="u", title="t", ext="xml",
        content_type="application/xml", http_status=200,
    )
    result = manifest.save_document(
        "part-800", b"version two", url="u", title="t", ext="xml",
        content_type="application/xml", http_status=200,
    )
    assert result.event == "updated"
    archived = list(storage.list("fda/ecfr/documents/part-800/versions"))
    assert len(archived) == 1
    assert storage.read_bytes("fda/ecfr/documents/part-800/current.xml") == b"version two"

    meta = json.loads(storage.read_text("fda/ecfr/documents/part-800/current.meta.json"))
    assert len(meta["version_history"]) == 2


def test_error_is_recorded_as_event_and_summary(tmp_path):
    storage, manifest = make_manifest(tmp_path)
    manifest.record_error("part-999", url="u", error="boom")
    summary = manifest.finalize()
    assert summary.errors == 1
    assert summary.status == "partial_failure"

    run_file = json.loads(storage.read_text(f"fda/ecfr/_manifest/runs/{manifest.run_id}.json"))
    assert run_file["errors"] == 1
    assert run_file["error_details"][0]["document_id"] == "part-999"


def test_finalize_success_with_no_errors(tmp_path):
    _storage, manifest = make_manifest(tmp_path)
    manifest.save_document(
        "part-800", b"data", url="u", title="t", ext="xml",
        content_type="application/xml", http_status=200,
    )
    summary = manifest.finalize()
    assert summary.status == "success"
    assert summary.new == 1


def test_events_are_one_file_per_event(tmp_path):
    storage, manifest = make_manifest(tmp_path)
    manifest.save_document(
        "part-800", b"data", url="u", title="t", ext="xml",
        content_type="application/xml", http_status=200,
    )
    manifest.save_document(
        "part-801", b"data2", url="u2", title="t2", ext="xml",
        content_type="application/xml", http_status=200,
    )
    events = list(storage.list("fda/ecfr/_manifest/events"))
    assert len(events) == 2


def test_on_event_fires_for_new_updated_unchanged_and_error(tmp_path):
    """The CLI's live progress line hangs off this callback — every
    document outcome (not just saves) must reach it, with a running
    checked-so-far count, so it stays accurate across a mix of outcomes."""
    storage = LocalStorage(root=str(tmp_path))
    calls = []
    manifest = Manifest(
        storage, "fda", "ecfr", run_id="test-run-1",
        on_event=lambda document_id, event, error, checked: calls.append((document_id, event, error, checked)),
    )

    manifest.save_document(
        "part-800", b"v1", url="u", title="t", ext="xml", content_type="application/xml", http_status=200,
    )
    manifest.save_document(
        "part-800", b"v1", url="u", title="t", ext="xml", content_type="application/xml", http_status=200,
    )
    manifest.save_document(
        "part-800", b"v2", url="u", title="t", ext="xml", content_type="application/xml", http_status=200,
    )
    manifest.record_error("part-999", url="u", error="boom")

    assert calls == [
        ("part-800", "new", None, 1),
        ("part-800", "unchanged", None, 2),
        ("part-800", "updated", None, 3),
        ("part-999", "error", "boom", 4),
    ]


def test_on_event_is_optional(tmp_path):
    """Every existing caller that doesn't pass on_event (reindex, tests,
    ...) must keep working exactly as before."""
    _storage, manifest = make_manifest(tmp_path)
    manifest.save_document(
        "part-800", b"data", url="u", title="t", ext="xml", content_type="application/xml", http_status=200,
    )  # must not raise


def test_write_estimate_persists_what_is_left(tmp_path):
    storage, manifest = make_manifest(tmp_path)
    manifest.write_estimate(PreviewInfo(total_available=35, already_known=10, note="a note"))

    payload = json.loads(storage.read_text(estimate_path("fda", "ecfr")))
    assert payload["regulation"] == "fda"
    assert payload["source"] == "ecfr"
    assert payload["total_available"] == 35
    assert payload["already_known"] == 10
    assert payload["remaining"] == 25
    assert payload["note"] == "a note"
    assert payload["computed_at"]  # non-empty timestamp


def test_write_estimate_overwrites_the_previous_snapshot(tmp_path):
    storage, manifest = make_manifest(tmp_path)
    manifest.write_estimate(PreviewInfo(total_available=35, already_known=10))
    manifest.write_estimate(PreviewInfo(total_available=35, already_known=20))

    payload = json.loads(storage.read_text(estimate_path("fda", "ecfr")))
    assert payload["already_known"] == 20
    assert payload["remaining"] == 15


def test_unsafe_document_id_rejected(tmp_path):
    _storage, manifest = make_manifest(tmp_path)

    with pytest.raises(ValueError):
        manifest.save_document(
            "../escape", b"data", url="u", title="t", ext="xml",
            content_type="application/xml", http_status=200,
        )


# -- REST sync (service_client) ---------------------------------------------


def test_construction_pushes_initial_running_run(tmp_path):
    storage = LocalStorage(root=str(tmp_path))
    client = _FakeServiceClient()
    Manifest(storage, "fda", "ecfr", run_id="run-1", service_client=client)

    assert len(client.calls) == 1
    name, (dto,) = client.calls[0]
    assert name == "upsert_run"
    assert dto["startedAt"]  # non-empty, non-deterministic timestamp
    dto = {k: v for k, v in dto.items() if k != "startedAt"}
    assert dto == {
        "runId": "run-1", "regulation": "fda", "source": "ecfr",
        "finishedAt": None, "status": "running",
        "checked": 0, "new": 0, "updated": 0, "unchanged": 0, "errors": 0, "errorDetails": [],
    }


def test_record_event_pushes_camelcase_event_after_local_write(tmp_path):
    storage = LocalStorage(root=str(tmp_path))
    client = _FakeServiceClient()
    manifest = Manifest(storage, "fda", "ecfr", run_id="run-1", service_client=client)
    client.calls.clear()  # drop the constructor's initial "running" push

    manifest.record_error("part-999", url="https://x", error="boom")
    assert len(client.calls) == 1
    name, (dto,) = client.calls[0]
    assert name == "record_event"
    assert dto["documentId"] == "part-999"
    assert dto["event"] == "error"
    assert dto["error"] == "boom"
    assert dto["url"] == "https://x"
    assert dto["runId"] == "run-1"


def test_save_document_pushes_document_upsert_for_new_and_unchanged(tmp_path):
    storage = LocalStorage(root=str(tmp_path))
    client = _FakeServiceClient()
    manifest = Manifest(storage, "fda", "ecfr", run_id="run-1", service_client=client)
    client.calls.clear()

    manifest.save_document(
        "part-800", b"v1", url="u", title="t", ext="xml",
        content_type="application/xml", http_status=200, source_metadata={"part": 800},
    )
    doc_calls = [c for c in client.calls if c[0] == "upsert_document"]
    assert len(doc_calls) == 1
    assert doc_calls[0][1][0]["documentId"] == "part-800"
    assert doc_calls[0][1][0]["sourceMetadata"] == {"part": 800}

    client.calls.clear()
    manifest.save_document(  # same bytes -> "unchanged" branch
        "part-800", b"v1", url="u", title="t", ext="xml",
        content_type="application/xml", http_status=200,
    )
    doc_calls = [c for c in client.calls if c[0] == "upsert_document"]
    assert len(doc_calls) == 1  # unchanged still syncs (last_checked_at moved)


def test_finalize_pushes_final_run_status(tmp_path):
    storage = LocalStorage(root=str(tmp_path))
    client = _FakeServiceClient()
    manifest = Manifest(storage, "fda", "ecfr", run_id="run-1", service_client=client)
    client.calls.clear()

    manifest.record_error("part-999", url="u", error="boom")
    client.calls.clear()  # drop the record_error's own event push
    manifest.finalize()

    run_calls = [c for c in client.calls if c[0] == "upsert_run"]
    assert len(run_calls) == 1
    assert run_calls[0][1][0]["status"] == "partial_failure"
    assert run_calls[0][1][0]["errors"] == 1


def test_write_estimate_pushes_put_source_estimate(tmp_path):
    storage = LocalStorage(root=str(tmp_path))
    client = _FakeServiceClient()
    manifest = Manifest(storage, "fda", "ecfr", run_id="run-1", service_client=client)
    client.calls.clear()

    manifest.write_estimate(PreviewInfo(total_available=35, already_known=10, note="a note"))
    assert client.calls[0][0] == "put_source_estimate"
    regulation, source, dto = client.calls[0][1]
    assert (regulation, source) == ("fda", "ecfr")
    assert dto["totalAvailable"] == 35
    assert dto["remaining"] == 25


def test_no_service_client_never_calls_anything(tmp_path):
    """Every existing caller that passes no service_client (the CLI when
    QARA_REG_SCRAPER_SERVICE__BASE_URL is unset, every other test in this
    file) must keep working with zero network activity attempted."""
    _storage, manifest = make_manifest(tmp_path)  # service_client=None (default)
    manifest.save_document(
        "part-800", b"data", url="u", title="t", ext="xml", content_type="application/xml", http_status=200,
    )
    manifest.write_estimate(PreviewInfo(total_available=1, already_known=0))
    manifest.finalize()  # none of the above should raise or touch a client


def test_record_event_sync_failure_propagates_not_swallowed(tmp_path):
    """The core contract: a sync failure is NOT caught inside Manifest -
    cli.py's run command is what decides to cancel the source's run on it
    (see cli.py). If Manifest swallowed this, a down service would look
    identical to a working one from the scrape loop's point of view -
    exactly the bug this feature exists to prevent."""
    storage = LocalStorage(root=str(tmp_path))
    client = _FakeServiceClient(fail_on={"record_event"})
    manifest = Manifest(storage, "fda", "ecfr", run_id="run-1", service_client=client)

    with pytest.raises(ServiceSyncError):
        manifest.record_error("part-999", url="u", error="boom")

    # The local event file write happened BEFORE the sync attempt - it's
    # durable even though the sync itself failed.
    events = list(storage.list("fda/ecfr/_manifest/events"))
    assert len(events) == 1


def test_construction_sync_failure_propagates(tmp_path):
    storage = LocalStorage(root=str(tmp_path))
    client = _FakeServiceClient(fail_on={"upsert_run"})
    with pytest.raises(ServiceSyncError):
        Manifest(storage, "fda", "ecfr", run_id="run-1", service_client=client)


def test_finalize_sync_failure_propagates_after_local_write(tmp_path):
    storage = LocalStorage(root=str(tmp_path))
    client = _FakeServiceClient()
    manifest = Manifest(storage, "fda", "ecfr", run_id="run-1", service_client=client)
    client._fail_on = {"upsert_run"}  # only fail from here on, not the constructor's push

    with pytest.raises(ServiceSyncError):
        manifest.finalize()

    # The local run summary file is still written even though the sync failed.
    run_file = json.loads(storage.read_text(f"fda/ecfr/_manifest/runs/{manifest.run_id}.json"))
    assert run_file["run_id"] == "run-1"
