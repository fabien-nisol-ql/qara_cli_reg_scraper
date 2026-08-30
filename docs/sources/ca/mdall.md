# `ca:mdall` — Medical Devices Active Licence Listing (MDALL)

## What it covers

Health Canada's database of medical devices currently licensed for sale
in Canada — one record per active device licence (Class II/III/IV
devices require one; Class I devices don't and so aren't in this
catalog). The Canadian analog of the licensing side of
[`fda:clearances_510k`](../fda/clearances_510k.md)/[`fda:pma`](../fda/pma.md),
though structurally closer to
[`fda:classification`](../fda/classification.md)'s "full-catalog walk of
a stable reference table" shape.

## How it's fetched

Health Canada's own documented REST API (the same `health-products.
canada.ca` API family linked from the [MDALL Open Government Portal
dataset page](https://open.canada.ca/data/en/dataset/c801a084-210b-4cd2-8513-26a00b66eb6f)):

```
https://health-products.canada.ca/api/medical-devices/licence/?state=active&type=json&lang=en
```

Real, official API documentation exists at
`health-products.canada.ca/api/documentation/mdall-documentation-en.html`.
Unlike openFDA's paginated search, **one HTTP request returns the entire
active-licence catalog** as a single JSON array — confirmed live: 35,654
records, ~10MB, no pagination at all. No per-record network fetch needed
(the content's already inline in the listing response), same as
`fda:classification`'s own `_save_record`.

Only `state=active` licences are fetched — the same API also serves
`state=archived` (cancelled/expired licences), not fetched here since
this source is scoped to "what's currently licensed", mirroring
`fda:clearances_510k`'s own scope (current decisions, not a full
historical archive).

`health-products.canada.ca` has no `robots.txt` at all (confirmed live: a
real 404 from the underlying Tomcat server) — no declared restrictions.

## Document/storage shape

Keyed by `original_licence_no` directly — the record's own natural,
stable primary key:

```
data/ca/mdall/documents/<original_licence_no>/current.json
```

Each document's `canonical_url` is scoped to that one licence via the
documented `?id=<licence_no>` filter (confirmed live: returns just that
one record, not the whole catalog) — not the shared listing `ENDPOINT`
every record's raw data comes from, which would otherwise make "browse
original source" dump a reader into the entire ~35,600-record catalog
instead of the one licence they were looking at.

`source_metadata`: `original_licence_no`, `licence_status`,
`appl_risk_class`, `licence_name`, `first_licence_status_dt`,
`licence_type_cd`/`licence_type_desc`, `company_id`.

## Config knobs

No source-specific overrides beyond `enabled: true`. At ~35,600 records
(5x `fda:classification`'s ~7,100), a full backfill at the global default
`max_new_documents_per_run` (1000) takes about 36 runs — same
"spread over many scheduled runs" tradeoff every other large source in
this tool already makes.

## Known quirks / maintenance notes

- **Whole-catalog re-fetch on every run** (~10MB), same as
  `fda:classification`'s own tradeoff — content-hashing in
  `Manifest.save_document` keeps an unchanged record cheap (just bumps
  `last_checked_at`), but the download itself happens every time,
  `run()` and `estimate()` both.
- **Company data lives in a separate endpoint**
  (`/api/medical-devices/company/`), not fetched here — `company_id` on
  each licence record is the join key if that's ever wanted.

## Related sources

- [`ca:recalls`](recalls.md) — device recalls, a different Health Canada
  database entirely.
- [`fda:classification`](../fda/classification.md) — the closest FDA
  analog in *shape* (full-catalog walk, no per-record fetch), though a
  different kind of record (product classification vs. device licence).
