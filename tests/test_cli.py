"""Tests for the `run` command's CLI-flag override precedence:
--max-new-documents / --requests-per-second / --recheck-after-days must
beat both per-source and global config.yaml values, without needing any
real network access or a real regulation registry."""

from __future__ import annotations

from typing import ClassVar

from typer.testing import CliRunner

from qara_reg_scraper import cli as cli_module
from qara_reg_scraper.base_scraper import BaseScraper, PreviewInfo
from qara_reg_scraper.config import (
    LocalStorageSettings,
    MonitoringLogSettings,
    MonitoringSettings,
    RegulationSettings,
    ServiceSettings,
    Settings,
    SourceSettings,
    StorageSettings,
)
from qara_reg_scraper.manifest import RunSummary
from qara_reg_scraper.service_client import ServiceSyncError

runner = CliRunner()


class _RecordingScraper(BaseScraper):
    """A no-op scraper that just records what it was constructed with, so
    tests can assert on the resolved (CLI > per-source > global) values
    without touching the network."""

    regulation = "fda"
    name = "ecfr"
    description = "recording test double"
    instances: ClassVar[list[_RecordingScraper]] = []
    #: class-level override so tests can control what --preview reports
    preview_info: ClassVar[PreviewInfo] = PreviewInfo(total_available=100, already_known=40)

    def __init__(self, http, manifest, **kwargs):
        super().__init__(http, manifest, **kwargs)
        self.observed_requests_per_second = http.settings.requests_per_second
        type(self).instances.append(self)

    def run(self) -> RunSummary:
        type(self).run_was_called = True
        return self.manifest.finalize()

    def estimate(self) -> PreviewInfo:
        return type(self).preview_info

    run_was_called: ClassVar[bool] = False


class _HardStopThenSucceedScraper(_RecordingScraper):
    """Hard-stops on its first `hard_stop_count` construction/run cycles
    (one per whole-run retry attempt — see cli.py's `run` command), then
    reports a clean completed run — for testing the retry-with-backoff
    loop without any real network/HardStop plumbing. `call_count` is
    reset by each test before use, same convention as `_RecordingScraper
    .instances`."""

    regulation = "fda"
    name = "ecfr"
    hard_stop_count: ClassVar[int] = 0
    call_count: ClassVar[int] = 0

    def run(self) -> RunSummary:
        type(self).call_count += 1
        type(self).run_was_called = True
        if type(self).call_count <= type(self).hard_stop_count:
            self.manifest.record_error("bad-doc", url="u", error="simulated hard stop")
            self.manifest.summary.stop_reason = "hard_stop"
            return self.manifest.finalize(status="failed")
        self.manifest.summary.stop_reason = "completed"
        return self.manifest.finalize()


class _ErroringScraper(_RecordingScraper):
    """Same as _RecordingScraper, but records one document error — for
    testing that --quiet still surfaces errors even with live progress and
    the per-source summary line both suppressed."""

    regulation = "fda"
    name = "ecfr"

    def run(self) -> RunSummary:
        type(self).run_was_called = True
        self.manifest.record_error("bad-doc", url="u", error="simulated failure")
        return self.manifest.finalize()


def make_settings(
    tmp_path, *, global_max_new=25, source_max_new=None, source_rps=None, source_recheck=None,
    source_lookback=None, retry_budget_minutes=None,
):
    return Settings(
        storage=StorageSettings(backend="local", local=LocalStorageSettings(root=str(tmp_path))),
        # Explicit, not left to fall through to the real ambient .env / env
        # vars — fields NOT passed as init kwargs still resolve through the
        # normal env > .env > config.yaml chain (see config.py), so an
        # unrelated real .env's QARA_REG_SCRAPER_SERVICE__BASE_URL would
        # otherwise leak into these tests.
        service=ServiceSettings(),
        regulations={
            "fda": RegulationSettings(
                sources={
                    "ecfr": SourceSettings(
                        enabled=True,
                        max_new_documents_per_run=source_max_new,
                        requests_per_second=source_rps,
                        recheck_after_days=source_recheck,
                        lookback_days=source_lookback,
                    )
                }
            )
        },
        max_new_documents_per_run=global_max_new,
        lock_dir=str(tmp_path / ".locks"),
        retry_budget_minutes=retry_budget_minutes,
    )


def patch_registry(monkeypatch, fake_registry: dict) -> None:
    """Patching cli_module.REGULATION_REGISTRY alone is NOT enough: cli.py's
    `run()` calls the imported `resolve()`, which closes over
    regulations/__init__.py's OWN module-global registry, not cli.py's copy
    of the name — so `resolve()` would still resolve against the *real*
    registry (and, for `fda:ecfr`, actually hit the network) unless it's
    patched too. Route it through the same fake_registry so both paths
    agree — this is exactly the bug that made an earlier version of this
    test suite hang on a real network call instead of failing fast."""
    monkeypatch.setattr(cli_module, "REGULATION_REGISTRY", fake_registry)

    def fake_resolve(qualified_name: str):
        regulation, _, name = qualified_name.partition(":")
        return fake_registry[regulation][name]

    monkeypatch.setattr(cli_module, "resolve", fake_resolve)
    monkeypatch.setattr(
        cli_module, "all_qualified_names",
        lambda: [f"{reg}:{src}" for reg, sources in fake_registry.items() for src in sources],
    )


def run_cli(monkeypatch, settings, args):
    """Invokes the CLI with `--no-estimate` tacked on by default, so these
    override-precedence tests see exactly the one scraper instance the real
    run constructs — not also the separate instance `run`'s now-default
    pre-flight estimate table constructs (see test_estimate_* below for
    that feature specifically)."""
    _RecordingScraper.instances = []
    monkeypatch.setattr(cli_module, "get_settings", lambda: settings)
    patch_registry(monkeypatch, {"fda": {"ecfr": _RecordingScraper}})
    if "--preview" not in args and "--no-estimate" not in args:
        args = [*args, "--no-estimate"]
    result = runner.invoke(cli_module.app, args)
    assert result.exit_code == 0, result.output
    assert len(_RecordingScraper.instances) == 1
    return _RecordingScraper.instances[0]


def test_cli_flag_overrides_global_config(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, global_max_new=25)
    scraper = run_cli(monkeypatch, settings, ["run", "--source", "fda:ecfr", "--max-new-documents", "2"])
    assert scraper.max_new_documents == 2


def test_cli_flag_overrides_per_source_config(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, global_max_new=25, source_max_new=10)
    scraper = run_cli(monkeypatch, settings, ["run", "--source", "fda:ecfr", "--max-new-documents", "3"])
    assert scraper.max_new_documents == 3


def test_no_cli_flag_falls_back_to_per_source_then_global(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, global_max_new=25, source_max_new=10)
    scraper = run_cli(monkeypatch, settings, ["run", "--source", "fda:ecfr"])
    assert scraper.max_new_documents == 10  # per-source beats global

    settings2 = make_settings(tmp_path, global_max_new=25, source_max_new=None)
    scraper2 = run_cli(monkeypatch, settings2, ["run", "--source", "fda:ecfr"])
    assert scraper2.max_new_documents == 25  # falls all the way back to global


def test_requests_per_second_cli_flag_overrides_config(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, source_rps=1.0)
    scraper = run_cli(monkeypatch, settings, ["run", "--source", "fda:ecfr", "--requests-per-second", "0.1"])
    assert scraper.observed_requests_per_second == 0.1


def test_recheck_after_days_cli_flag_overrides_config(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, source_recheck=30)
    scraper = run_cli(monkeypatch, settings, ["run", "--source", "fda:ecfr", "--recheck-after-days", "5"])
    assert scraper.recheck_after_days == 5


def test_lookback_days_cli_flag_overrides_config(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, source_lookback=90)
    scraper = run_cli(monkeypatch, settings, ["run", "--source", "fda:ecfr", "--lookback-days", "7"])
    assert scraper.lookback_days == 7


def test_no_lookback_days_flag_falls_back_to_per_source_config(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, source_lookback=90)
    scraper = run_cli(monkeypatch, settings, ["run", "--source", "fda:ecfr"])
    assert scraper.lookback_days == 90


def test_max_new_documents_zero_is_respected_not_treated_as_unset(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, global_max_new=25)
    scraper = run_cli(monkeypatch, settings, ["run", "--source", "fda:ecfr", "--max-new-documents", "0"])
    assert scraper.max_new_documents == 0


def test_default_lock_dir_moves_onto_shared_local_storage(tmp_path, monkeypatch):
    """config.lock_dir's own default ("./.locks") resolves inside
    whichever container happens to run a given invocation - never shared
    across the separately-launched job containers qara-reg-scraper-svc
    actually deploys (see cli.py's own comment at the lock_dir
    auto-derivation site). Left at that default, the same-source lock
    should instead land on the same shared storage.local.root every job
    container already mounts - proven here by NOT overriding lock_dir at
    all (unlike every other test in this file's make_settings(), which
    always does) and checking the lock file actually appears under
    storage's own tmp_path, not some unrelated "./.locks"."""
    settings = Settings(
        storage=StorageSettings(backend="local", local=LocalStorageSettings(root=str(tmp_path))),
        service=ServiceSettings(),
        regulations={"fda": RegulationSettings(sources={"ecfr": SourceSettings(enabled=True)})},
        # lock_dir intentionally NOT set - exercising the real default.
    )
    monkeypatch.setattr(cli_module, "get_settings", lambda: settings)
    patch_registry(monkeypatch, {"fda": {"ecfr": _RecordingScraper}})

    result = runner.invoke(cli_module.app, ["run", "--source", "fda:ecfr", "--no-estimate"])

    assert result.exit_code == 0, result.output
    assert (tmp_path / ".locks" / "fda" / "ecfr.lock").exists()


def test_default_session_log_dir_also_moves_onto_shared_local_storage(tmp_path, monkeypatch):
    """Same reasoning, same auto-derivation pattern as lock_dir just
    above - monitoring.log.session_log_dir left unset should land under
    storage's own root too, not be silently disabled."""
    settings = Settings(
        storage=StorageSettings(backend="local", local=LocalStorageSettings(root=str(tmp_path))),
        service=ServiceSettings(),
        regulations={"fda": RegulationSettings(sources={"ecfr": SourceSettings(enabled=True)})},
        # monitoring.log.session_log_dir intentionally NOT set.
    )
    monkeypatch.setattr(cli_module, "get_settings", lambda: settings)
    patch_registry(monkeypatch, {"fda": {"ecfr": _RecordingScraper}})

    result = runner.invoke(cli_module.app, ["run", "--source", "fda:ecfr", "--no-estimate"])

    assert result.exit_code == 0, result.output
    session_logs = list((tmp_path / "_session_logs").glob("run-*.jsonl"))
    assert len(session_logs) == 1


def test_explicit_session_log_dir_overrides_the_local_storage_default(tmp_path, monkeypatch):
    explicit_dir = tmp_path / "somewhere-else"
    settings = Settings(
        storage=StorageSettings(backend="local", local=LocalStorageSettings(root=str(tmp_path / "storage"))),
        service=ServiceSettings(),
        regulations={"fda": RegulationSettings(sources={"ecfr": SourceSettings(enabled=True)})},
        monitoring=MonitoringSettings(log=MonitoringLogSettings(session_log_dir=str(explicit_dir))),
    )
    monkeypatch.setattr(cli_module, "get_settings", lambda: settings)
    patch_registry(monkeypatch, {"fda": {"ecfr": _RecordingScraper}})

    result = runner.invoke(cli_module.app, ["run", "--source", "fda:ecfr", "--no-estimate"])

    assert result.exit_code == 0, result.output
    assert list(explicit_dir.glob("run-*.jsonl"))
    assert not (tmp_path / "storage" / "_session_logs").exists()


def test_source_without_colon_is_rejected(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    monkeypatch.setattr(cli_module, "get_settings", lambda: settings)
    patch_registry(monkeypatch, {"fda": {"ecfr": _RecordingScraper}})
    result = runner.invoke(cli_module.app, ["run", "--source", "ecfr"])
    assert result.exit_code == 2
    assert "regulation" in result.output.lower()


def test_regulation_all_runs_every_source_in_that_regulation(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    monkeypatch.setattr(cli_module, "get_settings", lambda: settings)
    patch_registry(monkeypatch, {"fda": {"ecfr": _RecordingScraper, "guidance": _RecordingScraper}})
    _RecordingScraper.instances = []
    result = runner.invoke(cli_module.app, ["run", "--source", "fda:all", "--no-estimate"])
    assert result.exit_code == 0, result.output
    assert len(_RecordingScraper.instances) == 2


def test_comma_separated_sources_runs_each_once(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    monkeypatch.setattr(cli_module, "get_settings", lambda: settings)
    patch_registry(
        monkeypatch,
        {"fda": {"ecfr": _RecordingScraper, "guidance": _RecordingScraper}, "eu": {"ivdr": _RecordingScraper}},
    )
    _RecordingScraper.instances = []
    result = runner.invoke(cli_module.app, ["run", "--source", "fda:ecfr,fda:guidance,eu:ivdr", "--no-estimate"])
    assert result.exit_code == 0, result.output
    assert len(_RecordingScraper.instances) == 3


def test_comma_separated_sources_dedupes_overlapping_expressions(tmp_path, monkeypatch):
    """"fda:all,fda:ecfr" mentions fda:ecfr twice (once via the "all"
    expansion) — it must still run exactly once, not twice."""
    settings = make_settings(tmp_path)
    monkeypatch.setattr(cli_module, "get_settings", lambda: settings)
    patch_registry(
        monkeypatch, {"fda": {"ecfr": _RecordingScraper, "guidance": _RecordingScraper}}
    )
    _RecordingScraper.instances = []
    result = runner.invoke(cli_module.app, ["run", "--source", "fda:all,fda:ecfr", "--no-estimate"])
    assert result.exit_code == 0, result.output
    assert len(_RecordingScraper.instances) == 2  # fda:ecfr + fda:guidance, not 3


def test_run_shows_estimate_by_default_before_the_real_work(tmp_path, monkeypatch):
    """The exact complaint that motivated this: a plain `run` (no
    --preview) gave no idea beforehand how many documents it would fetch or
    how long that would take. Now it prints the same estimate table
    --preview does, then actually runs."""
    settings = make_settings(tmp_path)
    _RecordingScraper.instances = []
    _RecordingScraper.run_was_called = False
    _RecordingScraper.preview_info = PreviewInfo(total_available=100, already_known=40)
    monkeypatch.setattr(cli_module, "get_settings", lambda: settings)
    patch_registry(monkeypatch, {"fda": {"ecfr": _RecordingScraper}})

    result = runner.invoke(cli_module.app, ["run", "--source", "fda:ecfr"])

    assert result.exit_code == 0, result.output
    assert _RecordingScraper.run_was_called  # unlike --preview, the real scrape DOES happen
    assert "100" in result.output  # available, from the pre-flight estimate table
    assert "60" in result.output  # remaining = 100 - 40
    # one instance for the estimate table, one for the real run
    assert len(_RecordingScraper.instances) == 2


def test_no_estimate_flag_skips_the_pre_flight_table(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    _RecordingScraper.instances = []
    _RecordingScraper.run_was_called = False
    _RecordingScraper.preview_info = PreviewInfo(total_available=100, already_known=40)
    monkeypatch.setattr(cli_module, "get_settings", lambda: settings)
    patch_registry(monkeypatch, {"fda": {"ecfr": _RecordingScraper}})

    result = runner.invoke(cli_module.app, ["run", "--source", "fda:ecfr", "--no-estimate"])

    assert result.exit_code == 0, result.output
    assert _RecordingScraper.run_was_called
    assert "100" not in result.output  # no pre-flight table printed
    assert len(_RecordingScraper.instances) == 1  # just the real run, no estimate pass


def test_preview_shows_counts_and_does_not_run(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    _RecordingScraper.instances = []
    _RecordingScraper.run_was_called = False
    _RecordingScraper.preview_info = PreviewInfo(total_available=100, already_known=40)
    monkeypatch.setattr(cli_module, "get_settings", lambda: settings)
    patch_registry(monkeypatch, {"fda": {"ecfr": _RecordingScraper}})

    result = runner.invoke(cli_module.app, ["run", "--source", "fda:ecfr", "--preview"])

    assert result.exit_code == 0, result.output
    assert not _RecordingScraper.run_was_called  # the actual scrape must never happen
    assert "100" in result.output  # available
    assert "40" in result.output  # already have
    assert "60" in result.output  # remaining = 100 - 40


def test_preview_handles_unknown_totals_gracefully(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    _RecordingScraper.instances = []
    _RecordingScraper.preview_info = PreviewInfo(
        total_available=None, already_known=12, note="no cheap total for this source"
    )
    monkeypatch.setattr(cli_module, "get_settings", lambda: settings)
    patch_registry(monkeypatch, {"fda": {"ecfr": _RecordingScraper}})

    result = runner.invoke(cli_module.app, ["run", "--source", "fda:ecfr", "--preview"])

    assert result.exit_code == 0, result.output
    assert "no cheap total for this source" in result.output


def test_summary_command_works_without_any_service(tmp_path, monkeypatch):
    """The whole point of `summary`: it must never require
    QARA_REG_SCRAPER_SERVICE__BASE_URL or qara-reg-scraper-svc running."""
    from qara_reg_scraper.manifest import Manifest

    settings = make_settings(tmp_path)
    assert settings.service.base_url is None
    monkeypatch.setattr(cli_module, "get_settings", lambda: settings)
    patch_registry(monkeypatch, {"fda": {"ecfr": _RecordingScraper}})

    storage = cli_module.build_storage_backend(settings.storage)
    manifest = Manifest(storage, "fda", "ecfr", run_id="run-1")
    manifest.save_document(
        "part-800", b"data", url="u", title="t", ext="xml",
        content_type="application/xml", http_status=200,
    )
    manifest.finalize()

    result = runner.invoke(cli_module.app, ["summary", "--source", "fda:ecfr"])

    assert result.exit_code == 0, result.output
    assert "1" in result.output  # documents count
    assert "success" in result.output


def test_quiet_suppresses_storage_backend_and_progress_lines(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    monkeypatch.setattr(cli_module, "get_settings", lambda: settings)
    patch_registry(monkeypatch, {"fda": {"ecfr": _RecordingScraper}})

    result = runner.invoke(cli_module.app, ["run", "--source", "fda:ecfr", "--quiet"])

    assert result.exit_code == 0, result.output
    assert "Storage backend" not in result.output
    assert "==" not in result.output  # no per-source header
    assert "checked=" not in result.output  # no per-source summary line
    assert "100" not in result.output  # no pre-flight estimate table either


def test_quiet_still_shows_compact_error_details(tmp_path, monkeypatch):
    """--quiet suppresses routine narration, but an unattended run
    shouldn't be a total black box when something actually broke."""
    settings = make_settings(tmp_path)
    monkeypatch.setattr(cli_module, "get_settings", lambda: settings)
    patch_registry(monkeypatch, {"fda": {"ecfr": _ErroringScraper}})

    result = runner.invoke(
        cli_module.app, ["run", "--source", "fda:ecfr", "--quiet", "--no-fail-on-error"]
    )

    assert result.exit_code == 0, result.output
    assert "bad-doc" in result.output
    assert "simulated failure" in result.output
    assert "Storage backend" not in result.output  # still no routine narration


def test_preview_shown_even_with_quiet(tmp_path, monkeypatch):
    """--preview is an explicit request to see the table — --quiet doesn't
    defeat the one thing that invocation was for."""
    settings = make_settings(tmp_path)
    _RecordingScraper.preview_info = PreviewInfo(total_available=100, already_known=40)
    monkeypatch.setattr(cli_module, "get_settings", lambda: settings)
    patch_registry(monkeypatch, {"fda": {"ecfr": _RecordingScraper}})

    result = runner.invoke(cli_module.app, ["run", "--source", "fda:ecfr", "--quiet", "--preview"])

    assert result.exit_code == 0, result.output
    assert "100" in result.output


def test_log_flag_writes_structured_logs_to_the_given_file(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    monkeypatch.setattr(cli_module, "get_settings", lambda: settings)
    patch_registry(monkeypatch, {"fda": {"ecfr": _RecordingScraper}})
    log_path = tmp_path / "run.log"

    result = runner.invoke(
        cli_module.app, ["run", "--source", "fda:ecfr", "--no-estimate", "--log", str(log_path)]
    )

    assert result.exit_code == 0, result.output
    assert log_path.exists()


def test_no_log_flag_means_no_log_file_at_all(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    monkeypatch.setattr(cli_module, "get_settings", lambda: settings)
    patch_registry(monkeypatch, {"fda": {"ecfr": _RecordingScraper}})

    result = runner.invoke(cli_module.app, ["run", "--source", "fda:ecfr", "--no-estimate"])

    assert result.exit_code == 0, result.output
    # Nothing under tmp_path should have been created for logging purposes
    # beyond the manifest itself — no stray log file anywhere.
    assert not list(tmp_path.glob("*.log"))


def test_debug_flag_forces_debug_level_regardless_of_config_log_level(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    settings.log_level = "WARNING"  # what --debug must override
    monkeypatch.setattr(cli_module, "get_settings", lambda: settings)
    patch_registry(monkeypatch, {"fda": {"ecfr": _RecordingScraper}})
    log_path = tmp_path / "debug.log"

    result = runner.invoke(
        cli_module.app,
        ["run", "--source", "fda:ecfr", "--no-estimate", "--log", str(log_path), "--debug"],
    )

    assert result.exit_code == 0, result.output
    import logging

    assert logging.getLogger("qara_reg_scraper").level == logging.DEBUG


def test_debug_flag_without_explicit_log_still_writes_somewhere(tmp_path, monkeypatch):
    """--debug alone (no --log, no config.yaml log_file) must not be a
    silent no-op — see _effective_logging's own docstring: it defaults
    the sink to stderr specifically so this works standalone."""
    settings = make_settings(tmp_path)
    monkeypatch.setattr(cli_module, "get_settings", lambda: settings)
    patch_registry(monkeypatch, {"fda": {"ecfr": _RecordingScraper}})

    result = runner.invoke(
        cli_module.app, ["run", "--source", "fda:ecfr", "--no-estimate", "--debug"]
    )

    assert result.exit_code == 0, result.output
    import logging

    root = logging.getLogger("qara_reg_scraper")
    assert root.level == logging.DEBUG
    assert not isinstance(root.handlers[0], logging.NullHandler)


def test_real_run_persists_a_what_is_left_estimate(tmp_path, monkeypatch):
    """The actual point of the estimate.json/SourceEstimate work: a real
    `run` (not just --preview) must leave a fresh snapshot behind for
    `summary`/`status` to read later, regardless of --quiet/--no-estimate."""
    import json

    settings = make_settings(tmp_path)
    _RecordingScraper.preview_info = PreviewInfo(total_available=35, already_known=12, note="a note")
    monkeypatch.setattr(cli_module, "get_settings", lambda: settings)
    patch_registry(monkeypatch, {"fda": {"ecfr": _RecordingScraper}})

    result = runner.invoke(
        cli_module.app, ["run", "--source", "fda:ecfr", "--quiet", "--no-estimate"]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads((tmp_path / "fda" / "ecfr" / "_manifest" / "estimate.json").read_text())
    assert payload["total_available"] == 35
    assert payload["already_known"] == 12
    assert payload["remaining"] == 23
    assert payload["note"] == "a note"


class _FailingServiceClient:
    """Stands in for a genuinely unreachable qara-reg-scraper-svc — every
    method raises ServiceSyncError immediately (as ScraperServiceClient
    itself would, after exhausting its own retries)."""

    def __init__(self, _settings):
        pass

    def _fail(self, operation):
        raise ServiceSyncError(operation, "simulated: service unreachable")

    def upsert_run(self, dto):
        self._fail("upsert_run")

    def record_event(self, dto):
        self._fail("record_event")

    def upsert_document(self, dto):
        self._fail("upsert_document")

    def put_source_estimate(self, regulation, source, dto):
        self._fail("put_source_estimate")

    def sync_sources(self, sources):
        # `run` catches this specific failure gracefully (logs a warning,
        # doesn't abort) — unlike every other method above, which feeds
        # into the per-source failure path this test is actually about.
        self._fail("sync_sources")


def test_run_cancels_each_source_independently_on_service_sync_failure(tmp_path, monkeypatch):
    """The core contract of this session's REST-sync rework: a source
    whose sync to qara-reg-scraper-svc fails (after retries — simulated
    here via a client that always raises) gets a clear, unconditional red
    error, is marked failed, and makes `run` exit non-zero — but that
    failure is caught PER-SOURCE (see cli.py's `run` command), not left to
    propagate out of the whole --source a,b loop. Both requested sources
    must be individually attempted and reported on, not just the first
    before the process dies."""
    settings = make_settings(tmp_path)
    settings.service = ServiceSettings(base_url="http://fake-service")
    monkeypatch.setattr(cli_module, "get_settings", lambda: settings)
    monkeypatch.setattr(cli_module, "ScraperServiceClient", _FailingServiceClient)
    patch_registry(monkeypatch, {"fda": {"ecfr": _RecordingScraper, "guidance": _RecordingScraper}})

    result = runner.invoke(cli_module.app, ["run", "--source", "fda:ecfr,fda:guidance", "--quiet"])

    assert result.exit_code == 1, result.output
    assert result.output.count("Aborting fda:") == 2  # both sources individually reported
    assert "Aborting fda:ecfr:" in result.output
    assert "Aborting fda:guidance:" in result.output


def test_run_without_service_base_url_never_touches_service_client(tmp_path, monkeypatch):
    """Standalone/local use (no QARA_REG_SCRAPER_SERVICE__BASE_URL) must
    keep working exactly as before this session's rework — manifests only,
    zero attempts to construct or call a service client."""
    settings = make_settings(tmp_path)
    assert settings.service.base_url is None
    monkeypatch.setattr(cli_module, "get_settings", lambda: settings)

    def _unexpected(*_args, **_kwargs):
        raise AssertionError("ScraperServiceClient must not be constructed when base_url is unset")

    monkeypatch.setattr(cli_module, "ScraperServiceClient", _unexpected)
    patch_registry(monkeypatch, {"fda": {"ecfr": _RecordingScraper}})

    result = runner.invoke(cli_module.app, ["run", "--source", "fda:ecfr", "--no-estimate"])

    assert result.exit_code == 0, result.output
    assert "Not reporting to qara-reg-scraper-svc" in result.output


class _RecordingServiceClient:
    """Stands in for a reachable qara-reg-scraper-svc — records every
    sync_sources(...) call's argument instead of making a real request, so
    tests can assert on exactly what payload was pushed. Every other
    method is a harmless no-op: `run`'s per-source loop also hands this
    same client to Manifest (see manifest.py), which calls upsert_run/
    record_event/upsert_document/put_source_estimate regardless — this
    class isn't testing those, just sync_sources, so they're stubbed out
    rather than left missing (which would AttributeError mid-run)."""

    sync_sources_calls: ClassVar[list[list[dict]]] = []

    def __init__(self, _settings):
        pass

    def sync_sources(self, sources):
        type(self).sync_sources_calls.append(sources)

    def upsert_run(self, dto):
        return {}

    def record_event(self, dto):
        return {}

    def upsert_document(self, dto):
        return {}

    def put_source_estimate(self, regulation, source, dto):
        return {}


def test_sync_sources_requires_service_base_url(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    assert settings.service.base_url is None
    monkeypatch.setattr(cli_module, "get_settings", lambda: settings)

    result = runner.invoke(cli_module.app, ["sync-sources"])

    assert result.exit_code == 2, result.output
    assert "service.base_url is not set" in result.output


def test_sync_sources_pushes_the_full_registry(tmp_path, monkeypatch):
    _RecordingServiceClient.sync_sources_calls = []
    settings = make_settings(tmp_path).model_copy(update={"service": ServiceSettings(base_url="http://svc")})
    monkeypatch.setattr(cli_module, "get_settings", lambda: settings)
    monkeypatch.setattr(cli_module, "ScraperServiceClient", _RecordingServiceClient)
    fake_registry = {"fda": {"ecfr": _RecordingScraper}}
    patch_registry(monkeypatch, fake_registry)

    result = runner.invoke(cli_module.app, ["sync-sources"])

    assert result.exit_code == 0, result.output
    assert len(_RecordingServiceClient.sync_sources_calls) == 1
    pushed = _RecordingServiceClient.sync_sources_calls[0]
    assert pushed == [
        {
            "regulation": "fda",
            "source": "ecfr",
            "label": _RecordingScraper.label or "ecfr",
            "description": _RecordingScraper.description,
        }
    ]
    assert "Synced 1 known source(s)" in result.output


def test_run_pushes_source_registry_when_service_configured(tmp_path, monkeypatch):
    """The graceful, per-invocation counterpart to the standalone
    `sync-sources` command — every real `run` also pushes the full
    registry as one of its first steps, when a service is configured."""
    _RecordingServiceClient.sync_sources_calls = []
    settings = make_settings(tmp_path).model_copy(update={"service": ServiceSettings(base_url="http://svc")})
    monkeypatch.setattr(cli_module, "get_settings", lambda: settings)
    monkeypatch.setattr(cli_module, "ScraperServiceClient", _RecordingServiceClient)
    _RecordingScraper.instances = []
    patch_registry(monkeypatch, {"fda": {"ecfr": _RecordingScraper}})

    result = runner.invoke(cli_module.app, ["run", "--source", "fda:ecfr", "--no-estimate"])

    assert result.exit_code == 0, result.output
    assert len(_RecordingServiceClient.sync_sources_calls) == 1
    assert _RecordingServiceClient.sync_sources_calls[0][0]["source"] == "ecfr"


def test_minus_one_max_new_documents_means_unlimited(tmp_path, monkeypatch):
    """--max-new-documents -1 must reach the scraper as None (BaseScraper's
    own "unlimited" spelling — see base_scraper.py), not as a literal -1."""
    settings = make_settings(tmp_path)
    scraper = run_cli(monkeypatch, settings, ["run", "--source", "fda:ecfr", "--max-new-documents", "-1"])
    assert scraper.max_new_documents is None


def test_hard_stop_retries_and_succeeds_within_budget(tmp_path, monkeypatch):
    """The core retry contract: a hard-stopped run is retried with a
    fresh manifest/scraper, sleeping (mocked) 1 minute before the retry
    that succeeds — exactly the doubling-from-1-minute backoff, bounded
    by retry_budget_minutes."""
    _HardStopThenSucceedScraper.call_count = 0
    _HardStopThenSucceedScraper.hard_stop_count = 1
    settings = make_settings(tmp_path, retry_budget_minutes=10)
    monkeypatch.setattr(cli_module, "get_settings", lambda: settings)
    patch_registry(monkeypatch, {"fda": {"ecfr": _HardStopThenSucceedScraper}})
    sleep_calls: list[float] = []
    monkeypatch.setattr(cli_module.time, "sleep", lambda seconds: sleep_calls.append(seconds))

    result = runner.invoke(cli_module.app, ["run", "--source", "fda:ecfr", "--no-estimate"])

    assert result.exit_code == 0, result.output
    assert _HardStopThenSucceedScraper.call_count == 2  # hard-stop once, succeed on the retry
    assert sleep_calls == [60]  # 1 minute, in seconds


def test_hard_stop_exhausts_budget_then_gives_up(tmp_path, monkeypatch):
    """A source that never recovers retries until the budget is used up
    (not one attempt over it — see cli.py's `remaining_budget` clamping),
    then reports the failure normally, same exit-code contract as an
    unretried hard-stop always had."""
    _HardStopThenSucceedScraper.call_count = 0
    _HardStopThenSucceedScraper.hard_stop_count = 999  # always hard-stops
    settings = make_settings(tmp_path, retry_budget_minutes=3)
    monkeypatch.setattr(cli_module, "get_settings", lambda: settings)
    patch_registry(monkeypatch, {"fda": {"ecfr": _HardStopThenSucceedScraper}})
    sleep_calls: list[float] = []
    monkeypatch.setattr(cli_module.time, "sleep", lambda seconds: sleep_calls.append(seconds))

    result = runner.invoke(cli_module.app, ["run", "--source", "fda:ecfr", "--no-estimate"])

    assert result.exit_code == 1, result.output
    assert _HardStopThenSucceedScraper.call_count == 3  # original + 2 retries, budget then exhausted
    assert sleep_calls == [60, 120]  # 1 min, then 2 min (3 min budget used exactly, not exceeded)


def test_no_retry_budget_means_zero_retries(tmp_path, monkeypatch):
    """Unset retry_budget_minutes (the default — nothing opted this
    invocation in) must preserve the exact pre-feature behavior: one
    attempt, no sleep, immediate failure."""
    _HardStopThenSucceedScraper.call_count = 0
    _HardStopThenSucceedScraper.hard_stop_count = 999
    settings = make_settings(tmp_path)  # retry_budget_minutes defaults to None
    assert settings.retry_budget_minutes is None
    monkeypatch.setattr(cli_module, "get_settings", lambda: settings)
    patch_registry(monkeypatch, {"fda": {"ecfr": _HardStopThenSucceedScraper}})
    sleep_calls: list[float] = []
    monkeypatch.setattr(cli_module.time, "sleep", lambda seconds: sleep_calls.append(seconds))

    result = runner.invoke(cli_module.app, ["run", "--source", "fda:ecfr", "--no-estimate"])

    assert result.exit_code == 1, result.output
    assert _HardStopThenSucceedScraper.call_count == 1
    assert sleep_calls == []


def test_retry_budget_minutes_flag_overrides_config(tmp_path, monkeypatch):
    """--retry-budget-minutes beats config.yaml/env, same precedence as
    --log over log_file."""
    _HardStopThenSucceedScraper.call_count = 0
    _HardStopThenSucceedScraper.hard_stop_count = 1
    settings = make_settings(tmp_path, retry_budget_minutes=None)  # config says "don't retry"
    monkeypatch.setattr(cli_module, "get_settings", lambda: settings)
    patch_registry(monkeypatch, {"fda": {"ecfr": _HardStopThenSucceedScraper}})
    sleep_calls: list[float] = []
    monkeypatch.setattr(cli_module.time, "sleep", lambda seconds: sleep_calls.append(seconds))

    result = runner.invoke(
        cli_module.app,
        ["run", "--source", "fda:ecfr", "--no-estimate", "--retry-budget-minutes", "10"],
    )

    assert result.exit_code == 0, result.output
    assert _HardStopThenSucceedScraper.call_count == 2
    assert sleep_calls == [60]


def test_retry_budget_minutes_also_becomes_the_scrapers_own_time_budget(tmp_path, monkeypatch):
    """The actual fix this was built for: retry_budget_minutes isn't just
    the post-HardStop backoff cap anymore — it's also passed straight
    through as time_budget_minutes on the scraper itself, so a
    retry-triggered job with an unlimited document budget
    (ScrapeJobService#triggerRetry's own --max-new-documents -1) still
    stops cleanly after roughly that many minutes of real wall-clock work,
    instead of running for as long as an unbounded document count would
    otherwise take against a correctly rate-limited host."""
    settings = make_settings(tmp_path)
    monkeypatch.setattr(cli_module, "get_settings", lambda: settings)
    patch_registry(monkeypatch, {"fda": {"ecfr": _RecordingScraper}})
    _RecordingScraper.instances.clear()

    result = runner.invoke(
        cli_module.app,
        ["run", "--source", "fda:ecfr", "--no-estimate", "--retry-budget-minutes", "10"],
    )

    assert result.exit_code == 0, result.output
    assert _RecordingScraper.instances[-1]._deadline is not None
