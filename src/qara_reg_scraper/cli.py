"""qara-reg-scraper CLI.

Every source is addressed as ``<regulation>:<source>`` (e.g. ``fda:ecfr``),
never a bare name — regulations are independent namespaces (see
``regulations/__init__.py``), and a document id is only unique within one
source of one regulation. ``--source`` accepts three shapes:

    --source all              every enabled source, across every regulation
    --source fda:all          every enabled source within one regulation
    --source fda:ecfr         exactly one source
    --source fda:ecfr,fda:guidance,eu:ivdr   a comma-separated list of any of the above

Each ``qara-reg-scraper run --source <x>`` invocation is a single, self-
contained process: it picks up config, scrapes the selected source(s), and
exits. That's deliberate — it's what lets you run independent cron entries
per source, each its own "instance," without any shared in-process state.
"""

from __future__ import annotations

import time
from pathlib import Path

import typer
from filelock import FileLock, Timeout
from rich.console import Console
from rich.table import Table

from .config import Settings, get_settings, get_source_settings, normalize_unlimited
from .heartbeat import Heartbeat
from .http_client import PoliteHttpClient
from .logging_setup import configure_logging, ensure_unbuffered_stdout, get_logger
from .manifest import Manifest
from .regulations import REGULATION_REGISTRY, UnknownSource, all_qualified_names, resolve
from .service_client import ScraperServiceClient, ServiceSyncError
from .storage import build_storage_backend

app = typer.Typer(add_completion=False, help="Scrape and index regulatory sources, one regulation at a time.")
# Wider than Rich's terminal-detected default (often 80 in non-interactive
# contexts like cron/CI, which wraps this CLI's wider tables — list-sources,
# status, summary, preview — into an unreadable one-character-per-line mess).
console = Console(width=160)
log = get_logger("cli")
# Once per process, regardless of whether --log is ever used — protects
# the human-oriented progress output below, not the internal JSON logger.
ensure_unbuffered_stdout()

# Shared `--log` option definition (used by both `run` and `reindex`) so
# the help text and precedence rule stay in exactly one place.
_LOG_OPTION = typer.Option(
    None, "--log",
    help="Write the internal structured (JSON) log — every HTTP request, retry, "
    "bot-detection event — to this file (or a device path like /dev/stderr). "
    "Overrides config.yaml's log_file. Unset (the default, on both) means no "
    "internal log output at all; the human-oriented progress lines below are "
    "separate and unaffected by this.",
)


def _resolve_sources(settings: Settings, requested: str) -> list[str]:
    """`requested` is one comma-separated list of "all", "<regulation>:all",
    and/or "<regulation>:<source>" expressions (a bare single expression,
    the common case, is just a one-element list). Returns a flat,
    order-preserving, de-duplicated list of "<regulation>:<source>"
    qualified names — de-duplicated so overlapping expressions (e.g.
    "fda:all,fda:ecfr") don't run the same source twice in one invocation.
    An explicit single source always runs regardless of its `enabled`
    config — only the two "all" forms filter by it."""
    seen: dict[str, None] = {}
    for expr in requested.split(","):
        expr = expr.strip()
        if not expr:
            continue
        for qualified_name in _resolve_source_expr(settings, expr):
            seen[qualified_name] = None
    return list(seen)


def _resolve_source_expr(settings: Settings, expr: str) -> list[str]:
    """Resolve a single (non-comma) `--source` expression: "all",
    "<regulation>:all", or "<regulation>:<source>"."""
    if expr == "all":
        return [
            f"{regulation}:{name}"
            for regulation, sources in REGULATION_REGISTRY.items()
            for name in sources
            if get_source_settings(settings, regulation, name).enabled
        ]

    regulation, sep, name = expr.partition(":")
    if not sep:
        console.print(
            f"[red]--source must be a comma-separated list of 'all', '<regulation>:all', "
            f"or '<regulation>:<source>' (got {expr!r}). Known: {', '.join(all_qualified_names())}[/red]"
        )
        raise typer.Exit(2)

    if name == "all":
        sources = REGULATION_REGISTRY.get(regulation)
        if sources is None:
            console.print(f"[red]Unknown regulation '{regulation}'. Known: {', '.join(REGULATION_REGISTRY)}[/red]")
            raise typer.Exit(2)
        return [
            f"{regulation}:{src_name}"
            for src_name in sources
            if get_source_settings(settings, regulation, src_name).enabled
        ]

    qualified_name = f"{regulation}:{name}"
    try:
        resolve(qualified_name)
    except UnknownSource as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(2) from e
    return [qualified_name]


def _format_duration(seconds: float) -> str:
    seconds = round(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def _print_preview(
    settings: Settings,
    qualified_names: list[str],
    storage,
    requests_per_second: float | None,
    max_new_documents: int | None,
    lookback_days: int | None,
    heartbeat: Heartbeat,
) -> None:
    """Backs `run --preview`: for each source, a cheap (no document
    content fetched) look at how much work a real run has ahead of it —
    see each scraper's `estimate()` (base_scraper.py) for what "cheap"
    means per source and where it's honestly unsupported. `estimate()`
    itself can be slow for a few sources (fda:guidance's full dataset
    download, openFDA pagination) with nothing else to show for it until
    it returns — `heartbeat` covers that silence."""
    table = Table(title="qara-reg-scraper preview (no documents fetched)")
    table.add_column("source")
    table.add_column("available")
    table.add_column("already have")
    table.add_column("remaining")
    table.add_column("this run")
    table.add_column("ETA (this run)")
    table.add_column("note")

    for qualified_name in qualified_names:
        regulation, _, name = qualified_name.partition(":")
        scraper_cls = resolve(qualified_name)
        source_settings = get_source_settings(settings, regulation, name)
        effective_rps = (
            requests_per_second
            if requests_per_second is not None
            else (
                source_settings.requests_per_second
                if source_settings.requests_per_second is not None
                else settings.http.requests_per_second
            )
        )
        effective_max = normalize_unlimited(
            max_new_documents
            if max_new_documents is not None
            else (
                source_settings.max_new_documents_per_run
                if source_settings.max_new_documents_per_run is not None
                else settings.max_new_documents_per_run
            )
        )
        effective_lookback = lookback_days if lookback_days is not None else source_settings.lookback_days
        http_settings = settings.http.model_copy(update={"requests_per_second": effective_rps})
        heartbeat.set_activity(f"computing the preview estimate for {qualified_name}")
        with PoliteHttpClient(http_settings, qualified_name) as http:
            manifest = Manifest(storage, regulation, name)
            scraper = scraper_cls(http, manifest, max_new_documents=effective_max, lookback_days=effective_lookback)
            info = scraper.estimate()
        heartbeat.beat()

        remaining = info.remaining
        # effective_max is None for BOTH "no cap configured anywhere" (the
        # old, only-ever-possible case) and "-1 → unlimited" (new) — either
        # way there's no cap to apply, so this_run is just whatever's left.
        if remaining is None:
            this_run = None
        elif effective_max is None:
            this_run = remaining
        else:
            this_run = min(remaining, effective_max)
        eta = _format_duration(this_run / effective_rps) if this_run and effective_rps > 0 else "-"
        table.add_row(
            qualified_name,
            str(info.total_available) if info.total_available is not None else "?",
            str(info.already_known) if info.already_known is not None else "?",
            str(remaining) if remaining is not None else "?",
            str(this_run) if this_run is not None else "?",
            eta,
            info.note or "",
        )
    console.print(table)


@app.command()
def list_sources() -> None:
    """List every known source, grouped by regulation, with its enabled
    state and description."""
    settings = get_settings()
    table = Table(title="qara-reg-scraper sources")
    table.add_column("regulation")
    table.add_column("source")
    table.add_column("enabled")
    table.add_column("description")
    for regulation, sources in REGULATION_REGISTRY.items():
        for name, cls in sources.items():
            enabled = get_source_settings(settings, regulation, name).enabled
            table.add_row(regulation, name, "yes" if enabled else "no", cls.description)
    console.print(table)


def _source_registry_payload() -> list[dict[str, str]]:
    """Every entry in REGULATION_REGISTRY, in the shape
    qara-reg-scraper-svc's PUT /v1/sources expects — shared by `run`'s
    graceful per-invocation push and the standalone `sync-sources`
    command. See docs/source-registry-sync.md: this is always the FULL
    registry, never a partial/filtered list — the service treats a sync
    as a replace-in-place (upserts what's here, deletes what isn't)."""
    return [
        {
            "regulation": regulation,
            "source": name,
            "label": cls.label or name,
            "description": cls.description,
        }
        for regulation, sources in REGULATION_REGISTRY.items()
        for name, cls in sources.items()
    ]


@app.command(name="sync-sources")
def sync_sources(
    quiet: bool = typer.Option(
        False, "--quiet", "-q",
        help="Suppress the confirmation line — errors still print regardless. "
        "qara-reg-scraper-svc launches this command with --quiet (see "
        "ScrapeJobService#triggerSourceSync in that repo); a human running it "
        "manually usually wants the confirmation, so this defaults to off.",
    ),
) -> None:
    """Push the full known-source registry (regulation/source/label/
    description) to qara-reg-scraper-svc's PUT /v1/sources, so GET
    /v1/sources reflects this CLI's own REGULATION_REGISTRY without a
    hand-maintained mirror on the service side. Meaningless without a
    configured service — unlike `run`'s own graceful push of the same
    payload (see its module docstring), this command hard-requires
    `service.base_url`, same as `reindex`/`status`. See
    docs/source-registry-sync.md for when this actually gets called
    (service startup, and once per real `run` invocation) — it is not on
    any independent schedule of its own."""
    settings = get_settings()
    if not settings.service.base_url:
        console.print("[red]service.base_url is not set (QARA_REG_SCRAPER_SERVICE__BASE_URL).[/red]")
        raise typer.Exit(2)
    client = ScraperServiceClient(settings.service)
    payload = _source_registry_payload()
    try:
        client.sync_sources(payload)
    except ServiceSyncError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e
    if not quiet:
        console.print(f"Synced {len(payload)} known source(s) to qara-reg-scraper-svc.")


@app.command()
def run(
    source: str = typer.Option(
        ..., "--source",
        help="'all', '<regulation>:all', or '<regulation>:<source>' — e.g. fda:ecfr. "
        "Comma-separate multiple, e.g. fda:ecfr,fda:guidance. "
        f"Known: {', '.join(all_qualified_names())}",
    ),
    fail_on_error: bool = typer.Option(
        True, help="Exit non-zero if any document errored (good for cron alerting)."
    ),
    max_new_documents: int | None = typer.Option(
        None, "--max-new-documents",
        help=(
            "Cap new documents fetched THIS run before stopping cleanly "
            "(stop_reason=budget_reached). Overrides config.yaml's "
            "regulations.<reg>.sources.<name>.max_new_documents_per_run / "
            "max_new_documents_per_run for every selected source in this invocation. "
            "0 = fetch nothing new (already-known documents are still skipped as usual)."
        ),
    ),
    requests_per_second: float | None = typer.Option(
        None, "--requests-per-second",
        help="Rate limit for THIS run, overriding config.yaml's http.requests_per_second "
        "/ regulations.<reg>.sources.<name>.requests_per_second for every selected source.",
    ),
    recheck_after_days: int | None = typer.Option(
        None, "--recheck-after-days",
        help="Re-check already-captured documents older than this many days, overriding "
        "config.yaml's regulations.<reg>.sources.<name>.recheck_after_days for every "
        "selected source. Unset (the default) leaves each source's own config.yaml value "
        "in place.",
    ),
    lookback_days: int | None = typer.Option(
        None, "--lookback-days",
        help="How many days back a 'listing window' source (fda:clearances_510k, "
        "fda:recalls — anything that asks openFDA for 'everything in the last N days' "
        "rather than walking a fixed list) looks, overriding config.yaml's "
        "regulations.<reg>.sources.<name>.lookback_days for every selected source. "
        "No effect on sources that don't have such a window (fda:ecfr, fda:guidance, "
        "fda:warning_letters). Unset (the default) leaves each source's own "
        "config.yaml value, or its built-in default (30), in place.",
    ),
    preview: bool = typer.Option(
        False, "--preview",
        help="Show what this run would do — documents available, already scraped, "
        "remaining, and an ETA — without fetching any document content, then exit. "
        "Some sources can't cheaply know a total (see the printed note); "
        "already-scraped counts are always real, read from the manifests.",
    ),
    estimate: bool = typer.Option(
        True, "--estimate/--no-estimate",
        help="Print the same available/already-have/remaining/ETA summary as --preview "
        "before doing the real work, so you see what's about to happen without a "
        "separate --preview invocation. On by default; pass --no-estimate to skip it "
        "(e.g. a high-frequency cron entry where the extra listing call isn't worth it "
        "— --preview always shows it regardless of this flag). --quiet also skips it, "
        "but --preview always wins over both.",
    ),
    quiet: bool = typer.Option(
        False, "--quiet", "-q",
        help="Suppress the human-oriented progress output (storage backend line, "
        "pre-flight estimate table, per-source/per-document lines) — for an "
        "unattended run where nothing's watching stdout live. Errors still print "
        "(compactly, per source) even under --quiet, and the exit code is "
        "unaffected either way. --preview still shows its table even with --quiet, "
        "since showing it is the whole point of that invocation.",
    ),
    retry_budget_minutes: int | None = typer.Option(
        None, "--retry-budget-minutes",
        help="Retry a source that hard-stops (bot-block, robots.txt, network failure) "
        "in-process, exponential backoff doubling from 1 minute, capped so the total "
        "elapsed time across all retries never exceeds this many minutes — then give up "
        "and report the failure normally. Overrides config.yaml's retry_budget_minutes / "
        "QARA_REG_SCRAPER_RETRY_BUDGET_MINUTES. Unset (the default) means zero retries — "
        "a hard-stop ends the run immediately, same as always. The service sets this via "
        "the env var when it launches a job; a human rarely needs this flag directly.",
    ),
    log_file: Path | None = _LOG_OPTION,
) -> None:
    """Scrape one source (or many, per --source) and write manifests.

    --max-new-documents / --requests-per-second / --recheck-after-days /
    --lookback-days are one-off overrides for this invocation only — they
    take priority over both the per-source and global config.yaml values,
    but don't change config.yaml itself. Useful for e.g. a manual catch-up
    run (`--max-new-documents 200`) without touching the file your
    scheduled cron runs use.

    Before doing any real work, this prints the same available/already-have
    /remaining/ETA table as --preview (see --estimate/--no-estimate / --quiet
    to turn that off) — so a normal `run` always tells you up front how much
    it's about to do and roughly how long that will take, not just a dry run.
    After a real run, it also persists a fresh "what's left to do" snapshot
    (regardless of --quiet/--no-estimate) so `summary`/`status` stay current
    without a live re-query — see local_status.py (the `summary` command)
    and manifest.py's `write_estimate` (which both writes it locally and
    pushes it to qara-reg-scraper-svc, the `status` command's source).
    """
    settings = get_settings()
    configure_logging(settings.log_level, sink=str(log_file) if log_file else settings.log_file)
    # CLI flag beats config.yaml/env, same precedence as log_file/--log
    # just above. None (the default on both) means zero retries — today's
    # exact behavior, unless something (typically the service, via
    # QARA_REG_SCRAPER_RETRY_BUDGET_MINUTES) opts this invocation in.
    effective_retry_budget_minutes = (
        retry_budget_minutes if retry_budget_minutes is not None else settings.retry_budget_minutes
    )
    qualified_names = _resolve_sources(settings, source)
    if not qualified_names:
        if not quiet:
            console.print("[yellow]No enabled sources to run.[/yellow]")
        raise typer.Exit(0)

    storage = build_storage_backend(settings.storage)
    if not quiet:
        console.print(f"Storage backend: {storage.describe()}")

    # One client, reused across every source in this invocation — None
    # means "not reporting to qara-reg-scraper-svc" (manifests are still
    # always written either way; see the module docstring / manifest.py).
    service_client = ScraperServiceClient(settings.service) if settings.service.base_url else None
    if service_client is None and not quiet:
        console.print(
            "[dim]Not reporting to qara-reg-scraper-svc — set "
            "QARA_REG_SCRAPER_SERVICE__BASE_URL to enable.[/dim]"
        )

    # Push the FULL known-source registry (not just qualified_names — every
    # source this CLI knows about, whether or not it's being run right
    # now), once per invocation, before any real scraping starts. This is
    # what keeps qara-reg-scraper-svc's GET /v1/sources current without a
    # hand-maintained mirror — see docs/source-registry-sync.md and the
    # standalone `sync-sources` command this reuses (_source_registry_payload).
    # A sync failure logs a warning and does NOT fail the run: reporting
    # the registry is a courtesy, not this invocation's actual job.
    if service_client is not None:
        try:
            service_client.sync_sources(_source_registry_payload())
        except ServiceSyncError as e:
            get_logger("cli").warning(f"source registry sync failed (continuing run): {e}")

    # A human watching this run should never see more than ~5s of total
    # silence — a slow rate-limited wait, a big listing/dataset download,
    # or PoliteHttpClient quietly retrying with backoff all have no
    # natural "something happened" event of their own to print. One
    # heartbeat spans the whole invocation (preview + the real per-source
    # loop below); every real console.print resets its clock via
    # `hb.beat()`, and `hb.set_activity(...)` marks up what a heartbeat
    # line says if the silence threshold is hit before real progress does.
    # Silent under --quiet, same as everything else it would print.
    with Heartbeat(console, enabled=not quiet) as hb:
        if preview or (estimate and not quiet):
            _print_preview(settings, qualified_names, storage, requests_per_second, max_new_documents, lookback_days, hb)

        if preview:
            raise typer.Exit(0)

        lock_dir = Path(settings.lock_dir)

        had_errors = False
        for qualified_name in qualified_names:
            regulation, _, name = qualified_name.partition(":")
            scraper_cls = resolve(qualified_name)
            source_settings = get_source_settings(settings, regulation, name)
            lock_path = lock_dir / regulation / f"{name}.lock"
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock = FileLock(str(lock_path), timeout=1)
            try:
                with lock:
                    effective_rps = (
                        requests_per_second
                        if requests_per_second is not None
                        else (
                            source_settings.requests_per_second
                            if source_settings.requests_per_second is not None
                            else settings.http.requests_per_second
                        )
                    )
                    http_settings = settings.http.model_copy(update={"requests_per_second": effective_rps})
                    # normalize_unlimited applied to the FINAL resolved
                    # value, not any input to this chain — -1 has to stay
                    # a real, distinguishable value through CLI-flag >
                    # per-source > global precedence (an early normalize
                    # to None would make "explicitly unlimited" indistinguishable
                    # from "not set here, fall through" at whichever level
                    # it came from — see normalize_unlimited's own docstring).
                    max_new = normalize_unlimited(
                        max_new_documents
                        if max_new_documents is not None
                        else (
                            source_settings.max_new_documents_per_run
                            if source_settings.max_new_documents_per_run is not None
                            else settings.max_new_documents_per_run
                        )
                    )
                    effective_recheck = (
                        recheck_after_days
                        if recheck_after_days is not None
                        else source_settings.recheck_after_days
                    )
                    effective_lookback = (
                        lookback_days
                        if lookback_days is not None
                        else source_settings.lookback_days
                    )
                    if not quiet:
                        budget_note = f" — budget: {max_new} new this run" if max_new is not None else ""
                        console.print(
                            f"\n[bold]== {qualified_name} ==[/bold] "
                            f"({scraper_cls.description})[dim]{budget_note}[/dim]"
                        )
                    hb.beat()

                    def _on_event(document_id: str, event: str, error: str | None, checked: int) -> None:
                        """Prints one readable line per document outcome,
                        live, as the scraper works — the raw per-HTTP-
                        request JSON log lines are still there for detail,
                        but on their own they don't read as visible
                        progress. Fires for every source via
                        Manifest.record_event, so this one callback covers
                        all of them without touching individual scrapers.

                        `checked` is a running total of every event this
                        source has recorded (new/updated/unchanged/error) —
                        deliberately NOT shown as "checked/max_new": some
                        sources (fda:clearances_510k) record two events per
                        candidate (a free metadata save plus the
                        budget-gated PDF save), so `checked` routinely
                        exceeds `max_new` well before the budget is actually
                        hit — a "[134/100]"-style fraction there was
                        confusing, not informative. The budget itself is
                        shown once, above, in the per-source header."""
                        color = {
                            "new": "green", "updated": "cyan", "unchanged": "dim",
                            "error": "red", "skipped_disallowed": "red",
                        }.get(event, "white")
                        detail = f" — {error[:160]}" if error else ""
                        console.print(f"  [{color}][{checked}] {event:<9}[/{color}] {document_id}{detail}")
                        hb.beat()

                    # Retry-with-backoff state for THIS source. A fresh
                    # PoliteHttpClient/Manifest/scraper is constructed for
                    # every attempt (a hard-stopped Manifest is one-shot,
                    # not reusable — see manifest.py) — only the LAST
                    # attempt's `summary` survives past this loop, feeding
                    # the existing post-run bookkeeping below unchanged.
                    # unrecoverable=True is the one path that skips retries
                    # entirely regardless of budget: manifest/scraper never
                    # even got constructed (Manifest's own initial sync
                    # push failed, or scraper_cls(...) itself raised) — see
                    # the `if manifest is None or scraper is None` branch
                    # below, unchanged from before this feature existed.
                    unrecoverable = False
                    retry_attempt = 1
                    retry_elapsed_minutes = 0
                    retry_wait_minutes = 1
                    while True:
                        heartbeat_activity = f"scraping {qualified_name}"
                        hb.set_activity(heartbeat_activity)
                        with PoliteHttpClient(http_settings, qualified_name) as http:
                            # `manifest` is constructed INSIDE this try (not
                            # above it) so a sync failure during its own initial
                            # "running" push (see Manifest.__init__) is caught
                            # by the exact same per-source cancel-and-continue
                            # handling as every other failure mode below, rather
                            # than crashing the whole multi-source invocation.
                            manifest: Manifest | None = None
                            scraper = None
                            try:
                                manifest = Manifest(
                                    storage, regulation, name,
                                    on_event=None if quiet else _on_event,
                                    service_client=service_client,
                                )
                                scraper = scraper_cls(
                                    http, manifest,
                                    max_new_documents=max_new,
                                    recheck_after_days=effective_recheck,
                                    lookback_days=effective_lookback,
                                )
                                summary = scraper.run()
                            except Exception as e:  # noqa: BLE001 - safety net: a scraper's own
                                # run() should always catch its failures and
                                # finalize cleanly (see base_scraper.py), but if
                                # one somehow leaks an exception anyway, don't
                                # let it crash the whole process (or skip the
                                # rest of --source all) — record it and move on,
                                # same as any other hard stop. A ServiceSyncError
                                # (qara-reg-scraper-svc unreachable/erroring
                                # after retries — see service_client.py) lands
                                # here too, deliberately: it's not caught inside
                                # Manifest, precisely so it cancels the run
                                # instead of being silently logged and ignored.
                                log.error(
                                    "scraper_run_crashed",
                                    extra={"extra_fields": {"source": qualified_name, "error": str(e)}},
                                )
                                if isinstance(e, ServiceSyncError):
                                    # Deliberately unconditional (not gated by
                                    # --quiet), same treatment as the lock-Timeout
                                    # case below — this is exactly the failure
                                    # mode ("succeeded" while quietly reporting
                                    # nothing) this feature exists to prevent.
                                    console.print(f"[red]Aborting {qualified_name}: {e}[/red]")
                                if manifest is None or scraper is None:
                                    # Either never got far enough to construct a
                                    # Manifest at all (its own initial sync push
                                    # failed), or — vanishingly unlikely,
                                    # BaseScraper.__init__ does no I/O —
                                    # scraper_cls(...) itself failed. Either way
                                    # there's nothing local left to record error
                                    # history into or finalize; still count as a
                                    # hard failure for this source. Deliberately
                                    # NOT retried by this feature (unlike a clean
                                    # HardStop below) — this is a genuine crash,
                                    # not the kind of transient failure backoff
                                    # is meant for.
                                    had_errors = True
                                    unrecoverable = True
                                    break
                                try:
                                    # best-effort: a second sync failure while
                                    # already handling the first must not mask
                                    # it (see the module's Design notes).
                                    manifest.record_error("__run__", url=None, error=f"run crashed: {e}")
                                except ServiceSyncError as sync_e:
                                    log.warning(
                                        "crash_recovery_sync_failed",
                                        extra={"extra_fields": {"source": qualified_name, "error": str(sync_e)}},
                                    )
                                manifest.summary.stop_reason = "hard_stop"
                                summary = manifest.finalize(status="failed")

                            # Persist "what's left to do" for `summary`/
                            # `status` — reuses the same rate-limited http
                            # client, right after the real work, regardless of
                            # --quiet/--no-estimate (those only control what
                            # prints, not what's stored).
                            hb.set_activity(f"computing the post-run estimate for {qualified_name}")
                            try:
                                manifest.write_estimate(scraper.estimate())
                            except ServiceSyncError as e:
                                # The run itself already completed and was
                                # finalized (successfully) above — only the
                                # post-run "what's left to do" snapshot failed
                                # to sync. Nothing to re-finalize; surface it
                                # clearly and still count as a failure for
                                # exit-code purposes (had_errors/fail_on_error).
                                log.error(
                                    "estimate_sync_failed",
                                    extra={"extra_fields": {"source": qualified_name, "error": str(e)}},
                                )
                                console.print(f"[red]{qualified_name}: {e}[/red]")
                                had_errors = True
                            except Exception as e:  # noqa: BLE001 - the estimate *computation*
                                # itself (scraper.estimate(), or the local file
                                # write) is unrelated to REST sync and stays
                                # best-effort, as before — must never fail the
                                # run on its own.
                                log.warning(
                                    "post_run_estimate_failed",
                                    extra={"extra_fields": {"source": qualified_name, "error": str(e)}},
                                )

                        # Whole-run retry decision: only a clean hard-stop
                        # (bot-block, robots.txt, network failure already
                        # retried and exhausted one layer down in
                        # PoliteHttpClient — see http_client.py) is ever
                        # retried here, and only when something opted this
                        # invocation in (effective_retry_budget_minutes is
                        # not None — see the module docstring on that var).
                        if summary.stop_reason != "hard_stop" or effective_retry_budget_minutes is None:
                            break
                        remaining_budget = effective_retry_budget_minutes - retry_elapsed_minutes
                        if remaining_budget <= 0:
                            if not quiet:
                                console.print(
                                    f"  [yellow]{qualified_name}: still hard-stopping after "
                                    f"{retry_attempt} attempt(s) over {retry_elapsed_minutes} min — "
                                    f"giving up for this run (retry budget exhausted).[/yellow]"
                                )
                            break
                        # `retry_wait_minutes` is the unclamped 1,2,4,8,...
                        # ideal backoff sequence — only the actual sleep is
                        # capped to whatever's left, so the budget is used
                        # right up to its edge instead of stopping early
                        # the moment the ideal wait would overshoot it.
                        # Guarantees termination: remaining_budget is > 0
                        # here and this_wait is always >= 1, so elapsed
                        # strictly grows every iteration.
                        this_wait = min(retry_wait_minutes, remaining_budget)
                        if not quiet:
                            console.print(
                                f"  [yellow]{qualified_name}: hard-stopped (attempt {retry_attempt}) — "
                                f"retrying in {this_wait} min.[/yellow]"
                            )
                        hb.set_activity(
                            f"waiting to retry {qualified_name} after a hard stop "
                            f"(next attempt in {this_wait} min)"
                        )
                        time.sleep(this_wait * 60)
                        retry_elapsed_minutes += this_wait
                        retry_wait_minutes *= 2
                        retry_attempt += 1

                    if unrecoverable:
                        continue

                    if not quiet:
                        console.print(
                            f"  checked={summary.checked} new={summary.new} "
                            f"updated={summary.updated} unchanged={summary.unchanged} "
                            f"skipped(already have)={summary.skipped_already_known} "
                            f"errors={summary.errors} status={summary.status} "
                            f"stop_reason={summary.stop_reason}"
                        )
                        hb.beat()
                    if summary.errors:
                        had_errors = True
                        if quiet:
                            # Not shown live (on_event was disabled above)
                            # and the summary line just above didn't print
                            # either — this is the one thing --quiet
                            # doesn't hide, so a broken unattended run
                            # isn't a total black box.
                            for detail in summary.error_details[:10]:
                                console.print(f"  [red]error[/red] {detail['document_id']}: {detail['error']}")
                            hb.beat()
            except Timeout:
                log.warning(
                    "source_skipped_lock_held",
                    extra={"extra_fields": {"source": qualified_name, "lock_path": str(lock_path)}},
                )
                # Deliberately unconditional (not gated by --quiet):
                # skipping an entire source outright is an anomaly worth
                # surfacing, not routine "here's what I'm doing" narration.
                console.print(
                    f"[yellow]Skipping '{qualified_name}': another run is already in progress "
                    f"(lock held at {lock_path}).[/yellow]"
                )
                hb.beat()

    if had_errors and fail_on_error:
        raise typer.Exit(1)


@app.command()
def reindex(
    source: str = typer.Option(
        "all", "--source",
        help="'all', '<regulation>:all', or '<regulation>:<source>', comma-separated for multiple — same as `run`.",
    ),
    log_file: Path | None = _LOG_OPTION,
) -> None:
    """Push each source's *current* manifest state (documents, latest run,
    estimate) to qara-reg-scraper-svc — recovers/backfills its database if
    it was ever lost or rebuilt. Does NOT replay the full historical event
    log (see sync.py's module docstring for why: POST /v1/events is
    insert-only, so repeat invocations would duplicate it — events are
    meant to arrive live, exactly once, during a real `run`)."""
    from .sync import sync as do_sync

    settings = get_settings()
    configure_logging(settings.log_level, sink=str(log_file) if log_file else settings.log_file)
    if not settings.service.base_url:
        console.print("[red]service.base_url is not set (QARA_REG_SCRAPER_SERVICE__BASE_URL).[/red]")
        raise typer.Exit(2)
    qualified_names = _resolve_sources(settings, source)
    storage = build_storage_backend(settings.storage)
    client = ScraperServiceClient(settings.service)
    try:
        results = do_sync(client, storage, qualified_names)
    except ServiceSyncError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e
    table = Table(title="Reindex results (documents/latest run/estimate — not full event history)")
    table.add_column("source")
    table.add_column("documents")
    table.add_column("run")
    table.add_column("estimate")
    for qualified_name, counts in results.items():
        table.add_row(
            qualified_name, str(counts["documents"]),
            "yes" if counts.get("runs") else "-",
            "yes" if counts.get("estimate") else "-",
        )
    console.print(table)


@app.command()
def status(
    source: str = typer.Option(
        "all", "--source",
        help="'all', '<regulation>:all', or '<regulation>:<source>', comma-separated for multiple — same as `run`.",
    ),
) -> None:
    """Query qara-reg-scraper-svc's `GET /v1/status` for a quick summary per
    source, including what's left to do (populated by a real `run` and
    `reindex`; "-" if no run has ever computed one yet).

    "documents" and "available"/"remaining" are deliberately NOT the same
    unit — don't be alarmed if "documents" is bigger. "documents" is a
    flat, all-time, all-sources-alike count of every file ever saved
    (`scraped_document` rows) — for a source that saves more than one file
    per real-world item (fda:clearances_510k saves both a metadata record
    and a summary PDF per clearance), that's already ~2x the item count
    before time even enters into it. "available"/"remaining" count
    real-world items (clearances, recalls, ...), and for a "listing
    window" source are further scoped to just the current `lookback_days`
    window, not all-time — see the "note" column
    (`run --lookback-days` widens the window)."""
    settings = get_settings()
    if not settings.service.base_url:
        console.print("[red]service.base_url is not set (QARA_REG_SCRAPER_SERVICE__BASE_URL).[/red]")
        raise typer.Exit(2)
    qualified_names = _resolve_sources(settings, source)
    client = ScraperServiceClient(settings.service)

    table = Table(title="qara-reg-scraper status (via qara-reg-scraper-svc)")
    table.add_column("source")
    table.add_column("documents (files, all-time)")
    table.add_column("available")
    table.add_column("remaining")
    table.add_column("last run")
    table.add_column("last status")
    table.add_column("last errors")
    table.add_column("note")

    try:
        rows = client.get_status(qualified_names)
    except ServiceSyncError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e

    by_qualified_name = {f"{row['regulation']}:{row['source']}": row for row in rows}
    for qualified_name in qualified_names:
        row = by_qualified_name.get(qualified_name, {})
        table.add_row(
            qualified_name,
            str(row.get("documents", 0)),
            str(row["totalAvailable"]) if row.get("totalAvailable") is not None else "?",
            str(row["remaining"]) if row.get("remaining") is not None else "?",
            str(row["lastFinishedAt"]) if row.get("lastFinishedAt") else "-",
            row.get("lastStatus") or "-",
            str(row["lastErrors"]) if row.get("lastErrors") is not None else "-",
            row.get("estimateNote") or "",
        )
    console.print(table)


@app.command()
def summary(
    source: str = typer.Option(
        "all", "--source",
        help="'all', '<regulation>:all', or '<regulation>:<source>', comma-separated for multiple — same as `run`.",
    ),
) -> None:
    """Service-less summary of what's been scraped, read directly from the
    file manifests — same shape as `status`, but never needs
    qara-reg-scraper-svc (no QARA_REG_SCRAPER_SERVICE__BASE_URL). Works the
    moment you've scraped anything at all. "available"/"remaining" come from the
    estimate.json snapshot a real `run` writes after finishing — "?" if no
    run has ever computed one for that source yet.

    "documents" and "available"/"remaining" are deliberately NOT the same
    unit — don't be alarmed if "documents" is bigger. "documents" is a
    flat, all-time, all-sources-alike count of every file ever saved —
    for a source that saves more than one file per real-world item
    (fda:clearances_510k saves both a metadata record and a summary PDF
    per clearance), that's already ~2x the item count before time even
    enters into it. "available"/"remaining" count real-world items
    (clearances, recalls, ...), and for a "listing window" source are
    further scoped to just the current `lookback_days` window, not
    all-time — see the "note" column."""
    from .local_status import compute_source_summary

    settings = get_settings()
    qualified_names = _resolve_sources(settings, source)
    storage = build_storage_backend(settings.storage)

    table = Table(title="qara-reg-scraper summary (from manifests, no DB)")
    table.add_column("source")
    table.add_column("documents (files, all-time)")
    table.add_column("available")
    table.add_column("remaining")
    table.add_column("last run")
    table.add_column("last status")
    table.add_column("stop reason")
    table.add_column("last errors")
    table.add_column("note")

    for qualified_name in qualified_names:
        regulation, _, name = qualified_name.partition(":")
        s = compute_source_summary(storage, regulation, name)
        table.add_row(
            qualified_name,
            str(s.documents),
            str(s.total_available) if s.total_available is not None else "?",
            str(s.remaining) if s.remaining is not None else "?",
            s.last_finished_at or "-",
            s.last_status or "-",
            s.last_stop_reason or "-",
            str(s.last_errors) if s.last_run_id else "-",
            s.estimate_note or "",
        )
    console.print(table)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
