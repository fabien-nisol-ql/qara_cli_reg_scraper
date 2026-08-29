# Unlimited backlog catch-up + whole-run retry with backoff

## What this is

Two related knobs on `run`, both usually set by `qara-reg-scraper-svc` when it launches a job rather than by a human:

1. **`--max-new-documents -1`** (or `max_new_documents_per_run: -1` in `config.yaml`, at either the global or per-source level) means *unlimited* — catch the entire backlog up in one run, instead of stopping after the configured budget and waiting for the next scheduled run.
2. **A retry budget** — when a source hard-stops (bot-block, robots.txt disallow, a network failure `PoliteHttpClient` already gave up retrying — see `http_client.py`), `run` retries the whole source itself, exponential backoff doubling from 1 minute, until either it succeeds or a total time budget is used up.

Neither existed before this session's "unlimited catch-up + automatic retry" work — both are opt-in, and a plain local/manual `qara-reg-scraper run` behaves exactly as it always has unless one of these is explicitly set.

## `-1` = unlimited

`BaseScraper._budget_exhausted()` already treats `max_new_documents=None` as unlimited — that's not new. What's new is `-1` as a **second, CLI/config/env-expressible spelling** of the same thing, since a Typer flag or a YAML/env scalar can't carry a bare `None` sentinel the way a Python default value can.

**Important implementation detail, not just a style choice**: `-1` is normalized to `None` via `config.normalize_unlimited()` — but only ever applied to the *final resolved value*, after the CLI-flag > per-source config > global config precedence chain has already picked a winner (see `cli.py`'s `run` command and `_print_preview`, both of which apply it at that point, not earlier). Normalizing early — e.g. on `SourceSettings.max_new_documents_per_run` itself, as a pydantic field validator — was tried and reverted: it makes "this level explicitly set -1 (unlimited)" indistinguishable from "this level didn't set anything, fall through to the next one," so `-1` would silently lose to a lower-priority level's real value instead of winning. If you're touching this code, keep the normalization at the end of each precedence chain, not on the individual settings fields.

## Whole-run retry with backoff

`Settings.retry_budget_minutes` (env `QARA_REG_SCRAPER_RETRY_BUDGET_MINUTES`, or `run --retry-budget-minutes`, CLI flag wins — same precedence shape as `log_file`/`--log`) is a **total time budget in minutes**, not a retry count. `None` (the default, on both) means zero retries — a hard-stop ends the run immediately, exactly as before this feature existed.

When set, `run`'s per-source loop (in `cli.py`) retries like this on a hard-stop:
- A **fresh** `PoliteHttpClient` + `Manifest` + scraper instance is constructed for every attempt — a hard-stopped `Manifest` is one-shot (one `run_id`, writes one `runs/{id}.json` on `finalize()`) and isn't reused.
- Backoff doubles from 1 minute (1, 2, 4, 8, 16, 32, ...), but the actual sleep each time is clamped to whatever's left in the budget — so a 60-minute budget is used right up to its edge (7 attempts, summing to exactly 60 minutes) rather than stopping early the moment the *ideal* next wait would overshoot it.
- Only the **last** attempt's summary feeds the existing post-run bookkeeping (error/exit-code handling, the `write_estimate` push) — attempts before that are just retried and discarded.
- One case is deliberately **never** retried by this mechanism: a scraper/`Manifest` construction failure so early that there's nothing local to even finalize (see `cli.py`'s `if manifest is None or scraper is None` branch) — that's a genuine crash, not the transient failure this backoff is meant for.

This sits *above*, not instead of, `PoliteHttpClient`'s own per-request retry/backoff (`http_client.py` — up to `max_retries` attempts per HTTP call, exponential with a 120s cap). A `HardStop` only reaches this layer after that one has already given up on the request in front of it — see `http_client.py`'s own module docstring for the one narrow exception (a genuinely unexpected exception type gets converted to `HardStop` with zero retries at that layer, so this whole-run retry is, in that rare case, the *first* retry rather than a second layer on top of one).

## Where the budget number actually comes from

Deliberately **not** a CLI-hardcoded constant. `qara-reg-scraper-svc` computes it — its own scheduled retry interval (`qaralink.scheduler.retry-interval-minutes`, default 60) is the exact value it passes down as `QARA_REG_SCRAPER_RETRY_BUDGET_MINUTES` when launching a job, so the CLI's own retries never run longer than the window before the service would trigger a fresh attempt anyway. See the sibling `qara-reg-scraper-svc` repo's README, "Automatic retry & circuit breaker" section, for the other half of this (the scheduler that decides *when* to relaunch a source, and the consecutive-failure circuit breaker that eventually stops retrying and flags a source for engineering review).

## Relevant code

- `config.py` — `normalize_unlimited()`, `Settings.retry_budget_minutes`.
- `cli.py` — `run`'s `-1` handling (both `_print_preview`'s `effective_max` and the main per-source loop's `max_new`), the `--retry-budget-minutes` option, and the retry-with-backoff loop itself (search for `retry_wait_minutes`).
- `base_scraper.py` — `_budget_exhausted`/`_consume_budget` (unchanged; `None` already meant unlimited before this feature).
- `http_client.py` — `PoliteHttpClient`'s own per-request retry/backoff, one layer below this.
