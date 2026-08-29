# `fda:recalls` — Device recalls / enforcement reports

## What it covers

FDA medical device recalls / enforcement reports — recall number,
classification (Class I/II/III recall severity, not device class),
status, recalling firm, report date, and product code (joins to
[`classification`](classification.md)).

## How it's fetched

openFDA's documented `device/enforcement` JSON API
(https://open.fda.gov/apis/device/enforcement/), via the shared
`iter_openfda_results` helper. Same pattern as
[`clearances_510k`](clearances_510k.md): last `lookback_days` of activity
by `report_date`, newest first, content-hashed per record so unchanged
records cost almost nothing on a re-run.

Unlike the other sources, a record's full content arrives inline in the
paginated *listing* response — there's no separate per-document fetch,
so `already_have`/budget/hard-stop apply a little differently:

- Skipping an already-known record still saves the local write, but the
  network cost was already paid by the listing page it arrived on.
- The real lever for "don't redownload everything every night" is that
  `iter_openfda_results` is a lazy generator — once the per-run budget is
  hit and the loop stops, it simply never requests the next listing page.
- If the listing call itself fails after retries are exhausted, that's
  treated as a hard stop for the run, handled directly here since there's
  no `fetch_and_save` in the loop.

## Document/storage shape

Keyed by `recall_number` (falling back to `event_id` if absent):

```
data/fda/recalls/documents/<recall_number>/current.json
```

`source_metadata`: `recall_number`, `classification`, `status`,
`firm_name`, `report_date`, `product_code`.

## Config knobs

| Key | Value | Why |
|---|---|---|
| `lookback_days` | `30` (built-in default; `365` in this repo's own `config.yaml`) | openFDA's "reported in the last N days" window for enforcement reports — same idea as [`clearances_510k`](clearances_510k.md)'s own `lookback_days`. Also overridable per invocation with `run --lookback-days`. |

## Known quirks / maintenance notes

- Backed by a stable, documented government API (`api.fda.gov`) — no
  bot-management behavior observed, unlike
  [`clearances_510k`](clearances_510k.md)'s PDF fetch. No source-specific
  `requests_per_second` override needed.
- Preview (`estimate()`) does a full pagination of the lookback window —
  cheap and window-bounded, since there's no per-document fetch either
  way (the record's already inline in the listing response).

## Related sources

- [`classification`](classification.md) — joined on `product_code`.
