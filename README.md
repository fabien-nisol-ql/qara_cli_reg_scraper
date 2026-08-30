# qara_cli_reg_scraper

A Python tool that monitors public medical-device regulatory sources — FDA
today, with the abstraction built in for whatever comes next (EU IVDR,
Turkish or Chinese device regs, ...) — for a regulatory compliance
workflow. Designed to run daily, unattended, resiliently, and honestly.

Every source is addressed as `<regulation>:<source>` — `fda:ecfr`,
`fda:recalls`, eventually `eu:ivdr` — never a bare name, since regulations
are independent namespaces (see [Regulations & sources](#regulations--sources)
and [Adding a new regulation](#adding-a-new-regulation)).

## What this is, in one paragraph

It's a scraper, and it says so. Every HTTP request it makes carries a
descriptive `User-Agent` and `From` header naming the tool, its purpose,
and a contact address (see [Identification policy](#identification-policy)
below) — it never pretends to be a browser, rotates identities, or routes
around rate limits or `robots.txt`. It fetches content, hashes it, stores
it wherever you tell it to (local disk, S3, Azure Blob, or SharePoint), and
writes a plain-file record of exactly what it did, when — that file record
is the durable source of truth. [`qara-reg-scraper-svc`](../qara-reg-scraper-svc)
(a separate Micronaut service) owns a Postgres index built from it via REST,
and can always rebuild it from nothing but the manifest files.

## Architecture

```
                        ┌──────────────────────────┐
  cron (one entry   ──▶ │ qara-reg-scraper run     │  one process per source,
  per source)           │ --source <reg>:<name>    │  independent & concurrent-safe
                        └─────────┬────────────────┘
                                  │
                 ┌────────────────┼─────────────────┐
                 ▼                ▼                  ▼
         PoliteHttpClient   Source scraper      Manifest
         (UA, robots.txt,   (regulations/<reg>/  (hash, version,
          rate limit,        <name>.py)           event log)
          retry+backoff)                                │
                                                          ▼
                                              StorageBackend
                                       (local / s3 / azure_blob / sharepoint)
                                                          │
                                                          ▼
                              <regulation>/<source>/documents/<id>/current.*
                              <regulation>/<source>/documents/<id>/current.meta.json
                              <regulation>/<source>/documents/<id>/versions/*
                              <regulation>/<source>/_manifest/events/**/*.json
                              <regulation>/<source>/_manifest/runs/*.json

                                                          │ REST (ScraperServiceClient),
                                                          │ once per manifest write —
                                                          │ or `qara-reg-scraper reindex`
                                                          │ for current-state-only recovery
                                                          ▼
                                    qara-reg-scraper-svc → Postgres (derived index)
```

**The file manifest is mandatory and authoritative.** Every run writes it,
regardless of whether qara-reg-scraper-svc is configured/reachable. The
service's Postgres index is disposable — `qara-reg-scraper reindex` can
always rebuild its current-state tables from nothing but the manifest
files (see [Checking what's been scraped](#checking-whats-been-scraped-with-or-without-a-service)) —
but a *sync failure during `run`* is not silently ignored: `Manifest`
retries a few times, then cancels that source's run with a clear error
rather than reporting "success" while quietly writing nothing to the
service (see [service_client.py](src/qara_reg_scraper/service_client.py)).

**These calls are intentionally never authenticated.** `service.base_url`
points straight at `qara-reg-scraper-svc` on the internal docker network
(e.g. `http://reg-scraper:8080/api/reg-scraper`) — no auth-gw, no JWT, no
credential to configure here, by design. The service's own role-based
access control (`admin`/`viewer`, see its README's "Access control"
section) only gates the endpoints a *human* reaches through auth-gw; the
endpoints this CLI calls (`/v1/documents`, `/v1/runs`, `/v1/events`,
`/v1/source-estimates`, `/v1/sources` PUT, `/v1/status`, `/v1/runs` GET)
stay open to unauthenticated callers specifically so this CLI keeps
working exactly as before — this is an internal network trust boundary,
not an oversight, and it stays that way after that access-control work.

## Directory layout

Everything lives under one storage root, organized by regulation, then by
source, then by document:

```
<root>/
  fda/
    ecfr/
      documents/
        title-21/
          current.xml
          current.meta.json
          versions/
            20260615T090000Z__a1b2c3d4.xml
      _manifest/
        events/2026/08/10/fda-ecfr-20260810T030000Z-9f3a1b2c__00001__title-21__unchanged.json
        runs/fda-ecfr-20260810T030000Z-9f3a1b2c.json
    guidance/
      documents/<slug>/...
    clearances_510k/
      documents/<k-number>/
        metadata/current.json     # the openFDA record (device name, applicant, decision date...)
        summary/current.pdf       # the actual 510(k) summary PDF, when FDA publishes one
    warning_letters/
      documents/<slug>/...
    recalls/
      documents/<recall-number>/...
  # eu/
  #   ivdr/
  #     documents/...
```

Every `current.meta.json` carries: regulation, source, canonical URL,
title, original filename (what the source itself called the file, from its
`Content-Disposition` header or the URL — distinct from `current.<ext>`,
which is always the same internal "latest version" convention regardless of
source), first-seen / last-checked / last-changed timestamps, content hash,
storage path, full version history, and source-specific metadata (CFR part
number, k-number, decision date, etc.). Every event file records one fetch
attempt: what happened (`new` / `updated` / `unchanged` / `error`), when,
against which URL, with what HTTP status and content hash.

## Identification policy

This is the concrete answer to "don't hide that this is a scraping tool":

- **User-Agent**: `qara-reg-scraper/0.1 (+<project_url>; contact: <email>;
  purpose: regulatory-compliance monitoring, low-volume daily batch job)`
  — composed from `http.contact_email` / `http.project_url` in
  `config.yaml`, sent on every request, never rotated or randomized. Set
  `http.user_agent_override` instead to replace it with an exact string of
  your own — still sent verbatim on every request; there's no code path
  that spoofs a browser UA or varies it per-request regardless of what you
  put here.
- **`From` header**: your contact email, on every request.
- **`robots.txt`**: fetched and honored per host before any request
  (`http.respect_robots_txt: true` by default) — every directive a host
  actually publishes, not just `Disallow`:
  - A disallowed path is skipped and logged, not worked around.
  - The standard `Crawl-delay`, and the non-standard `Hit-rate` some
    sites publish alongside it (confirmed live on
    `accessdata.fda.gov` — see `robots_policy.py`), slow requests to
    that host down to whatever it asks for, if that's slower than
    `http.requests_per_second`. robots.txt is a floor on politeness,
    never a ceiling config can race past — it only ever makes a host
    *more* cautious than configured, never less.
  - The non-standard `Visiting-hours` (also confirmed live on
    `accessdata.fda.gov`: `23:00EDT-05:00EDT`) refuses to fetch from
    that host outside its declared window at all, in whatever timezone
    it declares (properly DST-aware via `zoneinfo`, not a fixed UTC
    offset).

  None of this is hardcoded to any particular host anywhere in code —
  whatever a host's own robots.txt says is what applies to it,
  dynamically, for any source that talks to it; a host with no such
  directives (most of them) is completely unaffected.

  **`Visiting-hours` specifically is surfaced upstream, not just enforced
  locally.** A source whose own `estimate()` checks it
  (`clearances_510k.py`/`pma.py`/`hde.py` today —
  `self.http.next_available_at(origin)`) reports the next UTC time its
  host reopens as `PreviewInfo.next_available_at`, which
  `Manifest.write_estimate` persists locally and pushes to
  `qara-reg-scraper-svc` (`PUT /v1/source-estimates/{regulation}/{source}`,
  as `nextAvailableAt`) alongside every other estimate field, right after
  every real run. This is what lets that service's own scheduler
  (`SourceRetryScheduler`) avoid triggering a job that would immediately
  no-op against a closed window — see its README for the other half of
  this — and what it shows a human in the UI, instead of nothing that
  looks any different from a source that's simply idle. A source with no
  `Visiting-hours`-restricted host (the vast majority) always reports
  `None` here; nothing changes for them.
- **Rate limiting**: a conservative default of 1 request/second per host
  (`http.requests_per_second`), enforced by a blocking token bucket — not a
  best-effort delay — unless robots.txt asks for slower (see above). See
  "Pacing across sources and processes, not just within one" below for why
  that alone isn't the whole story once more than one source talks to the
  same host.
- **Throttling responses**: a `429` or `5xx` triggers exponential backoff
  with jitter; if the server sends `Retry-After`, that value is used
  exactly rather than guessed at.
- **No evasion**: no proxy rotation, no CAPTCHA solving, no header spoofing.
  If a source starts blocking this tool, the right fix is to slow it down
  or contact the site owner — not to hide harder.
- **Logging**: every HTTP request (method, URL, status, latency) is logged
  at INFO — nothing the tool does is invisible to its own operator either.

### Pacing across sources and processes, not just within one

The rate limiting described above (`http.requests_per_second`, robots.txt's
`Hit-rate`/`Crawl-delay`) is enforced by `PoliteHttpClient`'s own
`_TokenBucket` — but that bucket lives in memory, inside one
`PoliteHttpClient` instance. Two things about how this tool is actually
deployed make that too narrow on its own:

1. **Multiple sources can share a host.** `fda:pma`, `fda:hde`, and
   `fda:clearances_510k` all fetch PDFs from `accessdata.fda.gov` — three
   different sources, three different `PoliteHttpClient` instances (`run`
   constructs a fresh one per source, even within one invocation covering
   several — see `cli.py`). Each paces *itself* correctly; none of them
   know the others exist.
2. **A triggered run is a brand-new process.** `qara-reg-scraper-svc`'s
   `SourceRetryScheduler` doesn't run this CLI in one long-lived worker —
   it launches a fresh Docker container per triggered source. In-memory
   pacing state can't survive past one process, so even a single source's
   own memory of "when did I last hit this host" is gone the moment its
   run ends.

**Confirmed live, 2026-08-30**: the scheduler triggered `fda:pma` and
`fda:clearances_510k` within the same second — both shared the exact same
`nextAvailableAt` (accessdata.fda.gov's `Visiting-hours` window opening),
with `fda:hde` following two minutes later. Each of the three processes
correctly paced itself to the host's own `Hit-rate: 30` (~2s/request) — but
combined, three independent, uncoordinated streams meant the *aggregate*
request rate was roughly triple what any one process intended. That tripped
Akamai's own, separate "excessive requests" abuse detection — a real
block, reproduced live, well inside the robots.txt-allowed window.
**Complying with `Visiting-hours` does not make a client compliant with
`Hit-rate` in aggregate across multiple uncoordinated processes** — they're
different directives in the same robots.txt, and only coordinated pacing
satisfies the second one.

The fix (`origin_pacing.py`) moves the "when is this host next allowed to
be hit" decision out of process memory and onto disk, in the same shared
storage every scraper (and every separately-launched job container)
already reads and writes documents through. A `filelock.FileLock` guards
one small state file per host (`_origin_pacing/<host>.json`, alongside the
robots.txt policy cache's own `_robots_cache/<host>.json` — see
`robots_policy.py`) recording the next allowed request time. Every
`PoliteHttpClient`, in every process, in every container, reserves its
next slot against that one shared file before issuing a request to that
host — regardless of which source it belongs to, or which process/
container launched it. The lock is held only for the brief
read-decide-write step, not for the wait itself, so one process's wait
never blocks another's turn to check in.

This only activates when `storage` resolves to a real local filesystem
(`StorageBackend.local_root()` — true for `storage.backend: local`, which
is what every job container in `qara_iac_local_docker` actually uses; a
`FileLock` needs a real POSIX path, which S3/Azure Blob/SharePoint-backed
storage can't provide). Without one, `PoliteHttpClient` falls back to the
original in-memory-only `_TokenBucket` — correct for a single process with
a single source, exactly as it always was. A corrupt state file, a lock
timeout, or any other failure here degrades to "proceed immediately"
rather than raising: a slower-than-strictly-necessary request, or in the
worst case a collision this mechanism failed to prevent, is a far smaller
problem than a scraper run failing outright over its own politeness
bookkeeping.

The pre-existing **same-source lock** (`config.lock_dir`, preventing two
instances of *one* source running concurrently, e.g. an overrunning cron
entry) had the identical gap: its default (`./.locks`) resolves inside
whichever container happens to run a given invocation, never shared across
the separately-launched containers this project actually deploys. `run`
now auto-derives `lock_dir` from the same shared local storage root
whenever it hasn't been explicitly overridden in `config.yaml` — a real
override there always still wins.

### Monitoring — `config.yaml`'s `monitoring:` section

One shared home for how this tool reports its own activity, regardless of
the specific mechanism — everything below lives under `monitoring:` in
`config.yaml`.

```yaml
monitoring:
  log:
    session_log_dir: null       # see below — usually left unset
    session_log_retention_days: 90
  prometheus:
    pushgateway_url: null       # reserved, not implemented yet
```

**`monitoring.log`** is a temporary stand-in for real metrics, until
`monitoring.prometheus` exists: every CLI session (`run`/`reindex`) writes
its own uniquely-named JSON-lines file into a shared directory — the exact
same structured log content `--log`/`log_file` already produce (every
`http_request` line included, each carrying an explicit `origin` field —
see "Pacing across sources and processes" above), just durable and
always-on rather than one human opting into one fixed sink for one run.
That's enough to answer "queries per origin" and similar questions today
via a plain

```bash
jq -s 'map(select(.message=="http_request")) | group_by(.origin) | map({origin: .[0].origin, count: length})' _session_logs/*.jsonl
```

against the shared directory, without waiting on Prometheus/Grafana to
exist. Each file is named `<command>-<UTC timestamp>-<8 hex chars>.jsonl`
(e.g. `run-20260830T070032Z-e639c60c.jsonl`); a sweep at the start of every
session that has this enabled deletes anything older than
`session_log_retention_days` (~3 months by default) — no separate cron or
service needed.

`session_log_dir: null` (the default) doesn't mean "disabled" the way most
`null` defaults in this project do — it means "auto-derive from
`storage.local.root`", the same "off, or auto-derived from local storage"
pattern `lock_dir` uses just above, for the identical reason: every
separately-launched job container needs to land these on the ONE shared
volume they all mount, not their own ephemeral local disk. It only
actually resolves to nothing when storage is a non-local backend (S3/
Azure Blob/SharePoint) too — there's no shared local directory to use in
that case, and this is skipped entirely rather than erroring. Set an
explicit path to override the auto-derivation, same precedence every other
setting in this file gets.

**`monitoring.prometheus`** is reserved for later: since this CLI is a
short-lived batch job, not a persistent server, the standard fit is a
Pushgateway — this process pushes a counter per request at run-time,
Prometheus scrapes the gateway, not this process. `pushgateway_url: null`
is the only behavior today (disabled); nothing reads this field yet.

## Install

Everything here goes through `make` — see `Makefile` (and the scripts it
calls) for exactly what each target does; nothing below is a Makefile
substitute, it's the whole interface:

```bash
cd qara_cli_reg_scraper
make install-dev     # creates .venv, editable install + dev tools + all storage extras
cp config.yaml.example config.yaml
cp .env.example .env            # fill in qara-reg-scraper-svc URL / storage credentials
```

`make install` (no `-dev`) is the leaner variant — editable install only,
no dev tools/storage extras, no local storage backend beyond `local`. Both
create `.venv` themselves; there's nothing to activate or manage by hand.

## Configure

Edit `config.yaml` for non-secret settings (storage backend choice, rate
limits, which sources are enabled) and `.env` for secrets
(qara-reg-scraper-svc's URL if it needs one, cloud storage credentials) —
see the comments in each example file.
Per-source settings live under `regulations.<code>.sources.<name>` (e.g.
`regulations.fda.sources.ecfr.recheck_after_days: 14`) — a regulation or
source absent from the file just uses its defaults, you don't need an
entry for everything. Every `config.yaml` key can also be set as an
environment variable, e.g. `QARA_REG_SCRAPER_STORAGE__BACKEND=s3`.

## Run

`make run` (local, venv-backed) and `make drun` (same, in Docker) are the
*only* two ways this project runs itself through `make`. Neither wraps,
renames, or defaults anything: everything after `--`, subcommand included,
goes straight to the real `qara-reg-scraper` CLI unmodified, and both echo
the exact command they're about to run — read `Makefile`'s `run`/`drun`
targets if you want the mechanics, not just the interface:

```bash
make run -- list-sources                                # see what's registered/enabled, by regulation
make run -- run --source fda:ecfr                       # scrape one source
make run -- run --source fda:all                        # every enabled source within one regulation
make run -- run --source all                             # every enabled source, across every regulation
make run -- run --source fda:ecfr,fda:guidance,eu:ivdr   # a comma-separated pick-list, any mix of the above

make run -- run --source fda:all --preview               # dry run: available/remaining/ETA table only, nothing fetched
# (a plain `run`, no flags, prints that same table too, THEN scrapes for real — see "Knowing what a run is about to do" below)
make run -- reindex --source all                         # push current manifest state to qara-reg-scraper-svc (recovery/backfill)
make run -- status --source all                          # quick per-source summary via qara-reg-scraper-svc (needs it running)
make run -- summary --source all                         # same idea, but from the manifests directly — no service needed

make run -- --help                                       # see qara-reg-scraper's actual top-level help
make run -- run --help                                   # see the run subcommand's actual options
```

`--source` is one flag, always: a single expression (`fda:ecfr`), an `all`
form (`fda:all`, `all`), or several comma-separated expressions — mix and
match freely (`fda:ecfr,eu:ivdr`, even `fda:all,fda:ecfr`, which just runs
`fda:ecfr` once, not twice — overlapping expressions are de-duplicated).
Every command that takes `--source` (`run`, `reindex`, `status`, `summary`)
accepts this same syntax.

Required options (`run`'s `--source`) aren't defaulted by `make` either —
omit one and you get `qara-reg-scraper`'s own "Missing option" error, not a
silent Makefile guess. (One cosmetic quirk: `make run -- run ...` prints a
harmless trailing `make: Nothing to be done for `run'.` line — that's
Make's own goal-deduplication for the word "run" appearing twice, not an
error; the command already ran correctly right above it. `make drun`
doesn't have this, since no `qara-reg-scraper` subcommand is named `drun`.)

### One-off overrides on the command line

`run` also takes flags that override [Pacing a large
backlog](#pacing-a-large-backlog)'s config for that single invocation only
— they beat both `regulations.<reg>.sources.<name>.*` and the global
`config.yaml` value, without editing the file your scheduled cron runs
read:

```bash
make run -- run --source fda:ecfr --max-new-documents 200               # a one-off bigger catch-up run
make run -- run --source fda:clearances_510k --max-new-documents 0      # fetch nothing new, just re-check skip logic
make run -- run --source fda:ecfr --requests-per-second 0.1             # go slower just this once
make run -- run --source fda:ecfr --recheck-after-days 1                # force-recheck already-captured docs today
make run -- run --source fda:clearances_510k --lookback-days 90         # widen the openFDA query window (default 30)
```

`--lookback-days` only affects "listing window" sources —
`fda:clearances_510k` and `fda:recalls`, which ask openFDA for "everything
decided/reported in the last N days" — and does nothing on `fda:ecfr`/
`fda:guidance`/`fda:warning_letters`, which don't have that concept.
Widen it for a one-off backfill (`--lookback-days 365`), or set
`regulations.fda.sources.clearances_510k.lookback_days` /
`regulations.fda.sources.recalls.lookback_days` in `config.yaml` to change
it for every scheduled run, not just one invocation. Combine with
`--preview` to see the new window's size before committing to it:
`run --source fda:clearances_510k --lookback-days 90 --preview`.

`--max-new-documents 0` is not the same as omitting the flag — `0` means
"fetch nothing new this run" (already-known documents are still skipped as
normal); omitting it means "use whatever config.yaml says." `-1` means the
opposite extreme — unlimited, catch the entire backlog up in one run —
and, when combined with `--retry-budget-minutes` (or
`QARA_REG_SCRAPER_RETRY_BUDGET_MINUTES`, how `qara-reg-scraper-svc` sets
it when launching a job), a hard-stop mid-run is retried in-process with
exponential backoff instead of ending the run immediately. See
[`docs/retry-and-backlog-catchup.md`](docs/retry-and-backlog-catchup.md)
for the full mechanism, including why `-1` is normalized to "unlimited"
only after the CLI-flag/config precedence chain resolves, not before.

`--retry-budget-minutes` also bounds the run itself, not just backoff
between attempts — `BaseScraper`'s own `time_budget_minutes`, set from
this exact value. An unlimited document count (`-1`) at a genuinely
enforced per-request pace (a host's own robots.txt `Hit-rate`/
`Crawl-delay` — see `http_client.py`/`robots_policy.py`) could otherwise
mean one uninterruptible run lasting many hours instead of the roughly-N-
minutes a retry-triggered job is actually meant to take before yielding
back to the next scheduled attempt — confirmed live: `fda:pma`'s own
`-1`-budget retry job, at `accessdata.fda.gov`'s correctly-honored 30s
`Hit-rate`, was on track to run for something like 18 hours straight
before this existed. Once elapsed, this stops the run exactly the same
clean way `max_new_documents` running out does (`stop_reason=budget_reached`)
— no per-scraper code needed, since every scraper already shares this
check. A plain manual `run` (no `--retry-budget-minutes`, no
`QARA_REG_SCRAPER_RETRY_BUDGET_MINUTES`) is completely unaffected — this
only ever applies when something has actually opted an invocation into a
retry budget in the first place.

### Knowing what a run is about to do

Every `run` — not just a dry run — prints an available/already-have
/remaining/ETA table *before* it fetches anything, so you always know how
much work is ahead and roughly how long it'll take, not just after the
fact from the per-document log lines:

```bash
make run -- run --source fda:all
#                           source  available already have remaining this run ETA (this run) note
#                fda:ecfr              1          0           1         1     10s
#                fda:clearances_510k  127         0         127        10     50s            counts clearances decided in the last 30 days only
#
# ... then the real scrape starts, same as always.
```

It's the same effective config resolution as the real run that follows
(CLI flags beat per-source beat global `config.yaml`), so it reflects
whatever `--max-new-documents` / `--requests-per-second` you'd actually use.

- **available** / **already have** / **remaining** come from each scraper's
  `estimate()` (`base_scraper.py`) — cheap by design, no document content is
  fetched. Some sources (`fda:guidance`, `fda:warning_letters`) have no
  cheap way to know FDA's true total (see their **note** column) and show
  `?` for `available`/`remaining` rather than a fabricated number;
  `already have` is always real, since it's read straight from the local
  manifest.
- **this run** / **ETA (this run)** apply this invocation's effective
  `--max-new-documents` and `--requests-per-second` to `remaining`, so the
  ETA is specific to the flags you'd actually run with, not some fixed
  default.

Two flags control this:

- `--preview` — show the table, then **exit without scraping anything**. A
  pure dry run.
- `--no-estimate` — the opposite: **skip the table**, go straight to
  scraping. Useful for a high-frequency or unattended invocation (cron,
  Docker's scheduler — both are set up this way by default, see
  [`docker/crontab`](docker/crontab) / [`scripts/install_cron.sh`](scripts/install_cron.sh))
  where nothing is watching stdout in real time and the table's one extra
  listing call per source isn't worth paying every single day.

Plain `run` with neither flag does both: prints the table, then scrapes.

### Watching it work — two separate channels, on purpose

**Human-oriented progress** (on by default, `run`/`reindex` only) — every
document outcome prints its own line as it happens, not just the estimate
table beforehand and the one-line summary after:

```
== fda:recalls == (FDA medical device recalls / enforcement reports (openFDA device/enforcement))
  [1/3] new       Z-2755-2026
  [2/3] new       Z-2753-2026
  [3/3] new       Z-2772-2026
  checked=3 new=3 updated=0 unchanged=0 skipped(already have)=0 errors=0 status=success stop_reason=budget_reached
```

`[checked/this-run-budget]` counts up as each document is fetched; the
color/label (`new`/`updated`/`unchanged`/`error`) matches the manifest
event, and an error's message is appended inline. Already-known documents
that get skipped without a network call are deliberately silent here (that
could be thousands of lines on a big backlog) — they only show up in the
final `skipped(already have)=N` count. This fires from one place
(`Manifest.record_event`) so it covers every source the same way.

Turn it off with **`--quiet`** (`-q`) — for an unattended invocation where
nothing's watching stdout live: no storage-backend line, no pre-flight
estimate table, no per-source/per-document lines. Errors still print
(compactly, per source) even under `--quiet`, so a broken run isn't a
total black box, and the exit code is unaffected either way. `--preview`
always shows its table regardless of `--quiet` — showing it is the whole
point of that particular invocation.

**A heartbeat guarantees you never see more than ~5s of total silence**
while progress output is on. Some phases are genuinely slow with no
"document done" event of their own to hang a line off of — a rate-limited
wait between requests (`fda:clearances_510k` at its configured 0.2
req/sec is one request every 5s, right at the edge on its own),
`fda:guidance`'s big dataset download, an openFDA listing walk during
`estimate()`, or `PoliteHttpClient` quietly retrying a failed request with
backoff. If 5 seconds pass with nothing else printed, a dim status line
fills the gap:

```
  [1/3] new       K261658/summary
  … still scraping fda:clearances_510k (5s, no progress printed yet)
  [2/3] new       K261046/summary
  … still computing the post-run estimate for fda:clearances_510k (5s, no progress printed yet)
```

It's a background thread ([heartbeat.py](src/qara_reg_scraper/heartbeat.py)) whose silence clock resets every
time anything real prints; `--quiet` disables it along with everything
else.

```bash
make run -- run --source fda:ecfr --quiet                    # silent unless something errors
make run -- run --source fda:ecfr --quiet --preview           # still shows the table, nothing else
```

**The internal structured (JSON) log** — every HTTP request, retry,
bot-detection event, etc. — is a *separate* channel, off by default (no
sink at all, not even stdout). Turn it on with `--log <path>` (`run` and
`reindex`) or `config.yaml`'s `log_file`, either a real file or a device
path like `/dev/stderr` to fold it into whatever's already capturing
stdout/stderr:

```bash
make run -- run --source fda:ecfr --log /var/log/qara/ecfr.log
make run -- run --source fda:ecfr --quiet --log /dev/stderr   # nothing on stdout, full detail on stderr
```

```json
{"ts": "2026-08-11T06:53:28Z", "level": "INFO", "logger": "qara_reg_scraper.http.fda:recalls", "message": "http_request", "method": "GET", "url": "https://api.fda.gov/device/enforcement.json", "status": 200, "elapsed_ms": 1161}
```

`--log` beats `config.yaml`'s `log_file` when both are set. Combine
`--quiet --log <path>` for the classic "silent unless piped to a log file"
cron shape.

**`--debug`** (`run` and `reindex`) goes further than `--log` alone: every
request also gets a second, fuller log line with the complete request AND
response headers, plus a truncated response body — not just
method/URL/status/elapsed. Forces this invocation's log level to `DEBUG`
regardless of `config.yaml`'s `log_level`, and defaults the sink to
`/dev/stderr` if `--log`/`log_file` isn't already set, so `--debug` alone
works standalone:

```bash
make run -- run --source fda:clearances_510k --max-new-documents 1 --debug
```

```json
{"ts": "2026-08-29T16:59:55Z", "level": "DEBUG", "logger": "qara_reg_scraper.http.fda:classification", "message": "http_request_detail", "method": "GET", "url": "https://api.fda.gov/device/classification.json", "status": 200, "request_headers": {"User-Agent": "...", "Accept": "...", "From": "..."}, "response_headers": {"Content-Type": "application/json; charset=utf-8", "Etag": "...", "..."}, "response_body": "{\n  \"meta\": {...}, ...(truncated at 2000 chars)"}
```

Meant for interactively troubleshooting one run (why a source is being
blocked, what a new source's API actually returns, ...), not routine/cron
use — the body snippets alone make for a much noisier log than plain
`--log`. Binary bodies (PDFs, images) are never decoded as text, only
their size and content-type are shown; text/JSON/XML/HTML bodies are
truncated at 2000 characters (see `logging_setup.debug_body_snippet`).
`qara-reg-scraper-svc` pushes (`ScraperServiceClient`, used by `reindex`
and every real `run`'s own sync calls) get the same treatment, logged as
`service_request_detail`.

If you started a run and genuinely saw nothing at all print (not even
`Storage backend: ...`, and you didn't pass `--quiet`), that's almost
always output buffering in whatever's running the process, not this tool
going quiet — `run` force-line-buffers stdout on startup specifically to
rule that out.

### Checking what's been scraped, with or without a service

`status` reads via qara-reg-scraper-svc's REST API; `summary` reads the
file manifests directly and needs neither the service running nor
`QARA_REG_SCRAPER_SERVICE__BASE_URL` set — useful the moment you've
scraped anything at all, or anywhere you don't want to stand up the
service just to see what's been collected. Both show **what's left to
do**, not just what's done — `available`/`remaining` come from a snapshot
(`estimate.json` per source, pushed to the service's `source_estimate`
table too) that a real `run` writes right after it finishes (another call
to the scraper's `estimate()`, regardless of `--quiet`/`--no-estimate` —
those only control what *prints*, not what's *persisted*) and `reindex`
re-pushes from that same file, same as everything else it derives from the
manifests. `?`/`-` means no run has computed one for that source yet:

```bash
make run -- summary --source fda:all
#   source              documents (files, all-time) available remaining last run                  last status       stop reason last errors note
#   fda:ecfr             1                           1         0        2026-08-10T12:03:11+00:00  success           completed   0           (empty — ecfr isn't a "listing window" source)
#   fda:clearances_510k  6301                         198       195      2026-08-11T12:31:36+00:00  success           budget_reached 0        counts clearances decided in the last 30 days only
```

**"documents" and "available"/"remaining" are deliberately not the same
unit** — don't be alarmed if "documents" is *bigger* than "available",
which looks backwards at first glance. "documents" is a flat, all-time
count of every file ever saved, for any source, no exceptions; a source
that saves more than one file per real-world item (`fda:clearances_510k`
saves a metadata record *and* a summary PDF per clearance) already has
~2x the item count baked in, before time even enters into it.
"available"/"remaining" count real-world items (clearances, recalls,
guidance documents, ...), and for a "listing window" source
(`fda:clearances_510k`, `fda:recalls`) are further scoped to just the
*current* `lookback_days` window, not all-time — a source that's been
scraped for longer than its lookback window naturally accumulates more
`documents` than are ever `available` in any single snapshot. The `note`
column says so explicitly whenever it applies (`--lookback-days` widens
that window — see [One-off overrides](#one-off-overrides-on-the-command-line)).

Each `--source <regulation>:<name>` run is a self-contained process —
that's what makes "run multiple instances of the tool to scrape specific
ones" a matter of running the CLI multiple times (in cron, in parallel
terminals, wherever), not a special mode. A per-source file lock
(`.locks/<regulation>/<name>.lock`) prevents two overlapping runs of the
*same* source from stepping on each other; different sources — even across
regulations — never contend since they write disjoint subtrees.

## Scheduling

Two ways to run this daily, same underlying idea either way: a plain CLI
entrypoint invoked by *some* scheduler, never a custom in-process daemon.

**Option A — host cron**, scheduling the venv install above directly. See
[`scripts/install_cron.sh`](scripts/install_cron.sh), which prints (but
does not install) a staggered crontab, one line per source:

```bash
./scripts/install_cron.sh /path/to/qara-reg-scraper
crontab -e   # paste the printed lines
```

For macOS `launchd` or a systemd timer instead of cron, the unit just needs
to run `qara-reg-scraper run --source <regulation>:<name>` once a day; the
lock file and exit code (`0` = clean, `1` = at least one document errored)
work the same way.

**Option B — scheduled *inside* Docker** — see [Docker](#docker) below: the
image bundles [supercronic](https://github.com/aptible/supercronic) and
[`docker/crontab`](docker/crontab), so `make up` alone gives you a running
daily schedule with no host cron entry at all.

## Docker

```bash
cp config.yaml.example config.yaml   # storage.local.root: ./data by default
cp .env.example .env                 # qara-reg-scraper-svc URL / storage credentials

make up      # builds + starts the always-on scheduler container (detached)
make logs    # follow it (supercronic logs each job run as JSON)
make down    # stop it
```

That one container *is* the schedule: [`docker/crontab`](docker/crontab)
lists one line per source (mirrors `scripts/install_cron.sh`'s timing), and
[supercronic](https://github.com/aptible/supercronic) runs them in the
foreground as PID 1 — no root, no syslog, logs straight to stdout so `make
logs` shows every run. `restart: unless-stopped` in
[`docker-compose.yml`](docker-compose.yml) is what makes it durable across
host reboots, same as a real cron entry would be.

`data/`, `.locks/`, and `logs/` are bind-mounted onto the host (see
`docker-compose.yml`), so manifests land in `./data` exactly as they would
running locally — nothing scraped lives only inside the container.

For a one-off run instead of the scheduler (e.g. to test a source, or to
run `reindex` by hand), use `make drun` — same pass-through convention as
`make run`, just executed via `docker compose run --rm` instead of the
local venv:

```bash
make drun -- run --source fda:ecfr        # = docker compose run --rm qara-reg-scraper run --source fda:ecfr
make drun -- reindex --source all         # = docker compose run --rm qara-reg-scraper reindex --source all
```

Want `status`/`reindex` to actually do something? Point this container at a
running [`qara-reg-scraper-svc`](../qara-reg-scraper-svc) instance (which
owns its own Postgres — this repo has no database service of its own to
stand up):

```bash
# in .env:
QARA_REG_SCRAPER_SERVICE__BASE_URL=http://reg-scraper-svc:8080/api/reg-scraper
```

To change the schedule, edit `docker/crontab` and `make docker-build`
again, or bind-mount your own file over `/etc/qara-reg-scraper/crontab` in
`docker-compose.yml` instead of rebuilding.

## Pacing a large backlog

Several sources (`fda:guidance`, `fda:warning_letters` especially) have a
much bigger total document count than a polite daily job should try to
catch up on in one run. Three things, all in `base_scraper.py` (shared by
every regulation), spread that backlog over days or weeks instead:

1. **Skip what we already have** (`already_have`) — once a document is in
   the manifest, later runs skip it with *zero network calls*, by default
   forever. Set `regulations.<reg>.sources.<name>.recheck_after_days` if
   you want a source to periodically re-verify already-captured documents
   instead (done for `fda:ecfr` in `config.yaml.example`, since regulatory
   text changing is the whole point of that source).
2. **Per-run budget** (`max_new_documents_per_run`, global default 1000,
   overridable per source) — a run stops cleanly once it's fetched that
   many *new* documents, `stop_reason="budget_reached"`. Not a failure —
   the rest of the backlog is just tomorrow's job.
3. **Hard-stop on the first unretryable failure** — a document fetch that
   fails after `PoliteHttpClient`'s own retries are exhausted, is blocked
   by `robots.txt`, or looks like a bot-management block raises
   `HardStop`, which ends *the entire run* immediately
   (`stop_reason="hard_stop"`) rather than working through the rest of the
   day's candidates against a server that's already signaling trouble.

All three show up in the run summary — `qara-reg-scraper run` prints
`skipped(already have)=N ... stop_reason=<completed|budget_reached|hard_stop>`
alongside the usual counts, and both fields are in every run's manifest
JSON (`_manifest/runs/<run_id>.json`) and, if qara-reg-scraper-svc is
configured, its `scrape_run` table too.

## Error resilience, concretely

- **Hard-stop, not per-document swallow-and-continue**: a document fetch
  failing after retries, a robots.txt disallow, or a suspected
  bot-management block stops the whole run on purpose (see "Pacing a large
  backlog" above) — the failure is always recorded in the manifest first,
  so it's visible either way, but the run doesn't push on to the next
  candidate against a server that just said no.
- **Retry with backoff**: connection errors, timeouts, and 429/5xx are
  retried up to `http.max_retries` times with exponential backoff + jitter
  *before* any of that counts as an unretryable failure (`http_client.py`).
- **Idempotent re-runs**: content is hashed; an unchanged document is a
  cheap `unchanged` event, not a rewrite. Re-running a failed day is always
  safe — already-captured documents are skipped, so it resumes roughly
  where it left off rather than starting over.
- **Crash safety**: local writes are atomic (write-temp-then-rename);
  manifest events are one-file-per-event so a crash mid-run leaves partial
  history, not a corrupted log.
- **Exit codes**: `qara-reg-scraper run` exits `1` if any document errored
  (configurable via `--fail-on-error`), so cron/monitoring can alert on it
  without scraping logs — a qara-reg-scraper-svc sync failure (see next
  bullet) counts as an error here too.
- **REST sync: retry, then cancel loudly — never silently swallow**: if
  `QARA_REG_SCRAPER_SERVICE__BASE_URL` is set, every document/event/run/
  estimate push retries a few times (`service_client.py`); if it still
  can't sync, that source's run is cancelled with a clear
  `[red]Aborting <source>: ...[/red]` error rather than reporting
  "success" while quietly reporting nothing to the service. The manifest
  write itself already happened and is never at risk — only the sync is
  cancelled, so nothing scraped is lost, and `reindex` can catch the
  service back up later.
- **Corrupt-file tolerance**: `reindex` skips (and logs) any manifest file
  it can't parse instead of aborting the whole rebuild.
- **CLI-level safety net**: if a scraper's own `run()` somehow still leaks
  an exception (it shouldn't — every source catches its own failures), the
  CLI catches it, records it, and moves on to the next `--source all`
  source rather than crashing the whole process.

## Regulations & sources

Each source has its own doc at `docs/sources/<regulation>/<name>.md` —
what it covers, exactly how it's fetched, its document/storage shape, its
source-specific config knobs, and known quirks/maintenance notes. This
table is just an index; run `make run -- list-sources` for the live
enabled/disabled state — this file can drift, that command can't.

| regulation | source                                                       | what                                            | how                                   |
|------------|---------------------------------------------------------------|--------------------------------------------------|----------------------------------------|
| `fda`      | [`fdc_act`](docs/sources/fda/fdc_act.md)                       | Federal Food, Drug, and Cosmetic Act, full text (one document) | GovInfo Statute Compilations (official, USLM XML) |
| `fda`      | [`ecfr`](docs/sources/fda/ecfr.md)                             | 21 CFR Title 21, full text (one document)         | GovInfo bulk data (official, XML) |
| `fda`      | [`guidance`](docs/sources/fda/guidance.md)                     | FDA guidance documents                            | HTML scrape (`bs4`) — FDA has no public API for these |
| `fda`      | [`classification`](docs/sources/fda/classification.md)        | Device product classification (product code → class/regulation) | openFDA `device/classification` (official JSON API) |
| `fda`      | [`clearances_510k`](docs/sources/fda/clearances_510k.md)      | 510(k) / De Novo clearances **+ summary PDFs**    | openFDA `device/510k` (metadata) + accessdata.fda.gov (the actual PDF) |
| `fda`      | [`pma`](docs/sources/fda/pma.md)                               | Premarket Approval (PMA) decisions **+ approval order letters** | openFDA `device/pma` (metadata) + accessdata.fda.gov (the actual PDF) |
| `fda`      | [`hde`](docs/sources/fda/hde.md)                               | Humanitarian Device Exemption (HDE) approvals **+ approval order letters** | fda.gov listing page (HTML scrape, `bs4` — no openFDA endpoint exists) + accessdata.fda.gov (the actual PDF) |
| `fda`      | [`recalls`](docs/sources/fda/recalls.md)                      | Device recalls / enforcement                      | openFDA `device/enforcement` (official JSON API) |
| `fda`      | [`warning_letters`](docs/sources/fda/warning_letters.md)      | Warning letters, all centers                      | HTML scrape (`bs4`) — FDA has no public API for these |
| `eu`       | [`mdr`](docs/sources/eu/mdr.md)                                | Regulation (EU) 2017/745 (MDR), consolidated full text (one document) | EUR-Lex (official) — consolidated-version discovery + HTML fetch |
| `eu`       | [`ivdr`](docs/sources/eu/ivdr.md)                              | Regulation (EU) 2017/746 (IVDR), consolidated full text (one document) | EUR-Lex (official) — consolidated-version discovery + HTML fetch |
| `eu`       | [`mdcg_guidance`](docs/sources/eu/mdcg_guidance.md)            | MDCG guidance and other MDR/IVDR guidance         | HTML scrape (`bs4`) — the Commission has no public API for these |

Roughly, the chain these sources cover: `fdc_act` (the statute) →
`ecfr`/`guidance` (FDA's regulations and interpretation of it) →
`classification` (product code → device class/regulation) →
`clearances_510k`/`pma`/`hde` (the actual approval-pathway decisions —
510(k), PMA, and HDE respectively — and their supporting PDFs, for a
given product code). Together these three decision sources cover every
FDA device pathway that has its own public decision database; the
"Exempt" and "De Novo" pathways (see `compliance-svc`'s own
`market_path` table) don't — De Novo decisions are public but not via a
scrapeable database found so far, and "Exempt" isn't a decision database
at all, just a classification-level flag already captured by
`classification`.

This table (and `list-sources`) is also what gets pushed to
`qara-reg-scraper-svc` so its own `GET /v1/sources` stays current without
hand-maintenance — see [`docs/source-registry-sync.md`](docs/source-registry-sync.md)
for exactly when that push happens.

## Adding a new regulation (or a new source within one)

Nothing outside `regulations/` needs to change — config, storage, the
service client, and the CLI are all regulation-agnostic. `regulations/eu/`
already exists (`mdr`/`ivdr`/`mdcg_guidance` — see
[`docs/sources/eu/`](docs/sources/eu/)); adding e.g. its next source
works the same way a brand-new regulation namespace would:

1. Create `src/qara_reg_scraper/regulations/<code>/` if it doesn't exist
   yet (it does for `eu`).
2. Write one or more `BaseScraper` subclasses there (see
   `regulations/fda/` for a full worked example covering seven different
   source shapes: two single-fixed-document bulk-text fetches, two
   paginated HTML scrapes, and three openFDA-JSON-listing-backed sources —
   one lookback-windowed with a shared per-document fetch, one
   lookback-windowed with content inline in the listing, one a full-catalog
   walk of a stable reference table; `regulations/eu/` adds two more —
   `mdcg_guidance.py`, a single server-rendered HTML table page, and
   `mdr.py`/`ivdr.py`, a single fixed document whose URL still needs a
   discovery step first, since — unlike `fda:ecfr`'s GovInfo feed —
   EUR-Lex's consolidated text has no one URL that's always "current"; see
   `regulations/eu/eur_lex_consolidated.py`). Each sets
   `regulation = "<code>"` and its own `name`, e.g. `"mdr"`.
3. Export it from that package's `dict[str, type[BaseScraper]]` (e.g.
   `EU_SOURCES` in `regulations/eu/__init__.py`, mirroring
   `regulations/fda/__init__.py`).
4. If it's a brand-new regulation namespace, add one line to
   `REGULATION_REGISTRY` in `regulations/__init__.py` (not needed for a
   new source within `eu` — already registered).
5. Write `docs/sources/<code>/<name>.md` for each new source (mirrors
   `docs/sources/fda/*.md`/`docs/sources/eu/*.md` — see any of those for
   the expected shape: what it covers, how it's fetched, document/storage
   shape, config knobs, known quirks, related sources).

That's it — `qara-reg-scraper list-sources` picks it up automatically,
`--source eu:mdr` / `eu:all` work, manifests land under `eu/mdr/...`, and
`config.yaml`'s `regulations.eu.sources.mdr.*` (optional — defaults apply
if you skip it) all follow the same shape `fda` already uses.

## Tests

```bash
make test
```

Covers: HTTP client rate limiting/retry/Retry-After handling, local storage
backend, manifest write/read/versioning logic (including that two
regulations sharing a document id stay isolated, and that a REST sync
failure propagates out of `Manifest`'s methods instead of being swallowed),
`sync.py`'s current-state push against a mocked qara-reg-scraper-svc
(`responses`), the `clearances_510k` metadata+PDF split (including the
Akamai-block failure mode, mocked), and the CLI's
`--max-new-documents`/`--requests-per-second`/`--recheck-after-days`
override precedence plus its down-service abort/exit-code behavior.
