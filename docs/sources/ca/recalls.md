# `ca:recalls` — Medical device recalls and safety alerts

## What it covers

Health Canada's recalls and safety alerts for medical devices — the
Canadian analog of [`fda:recalls`](../fda/recalls.md) (openFDA's
`device/enforcement`).

## How it's fetched

`recalls-rappels.canada.ca`'s own open-data JSON export (linked from its
[Open Government Portal dataset page](https://open.canada.ca/data/en/dataset/d38de914-c94c-429b-8ab1-8776c31643e3)),
updated daily per Health Canada's own description:

```
https://recalls-rappels.canada.ca/sites/default/files/opendata-donneesouvertes/HCRSAMOpenData.json
```

One HTTP request returns the **whole** dataset — every recall/safety
alert across every category Health Canada tracks (food, consumer
products, vehicles, health products, etc.), confirmed live: 34,025 total
records. Filtered here to `Organization == "Medical devices"` (confirmed
live: 8,622 of the 34,025) — the same "one big shared dataset, filter to
what's relevant" shape as [`fda:guidance`](../fda/guidance.md)'s
`field_regulated_product_field` filtering. No per-record network fetch
needed (the record's already inline in the listing response), same as
[`ca:mdall`](mdall.md).

`recalls-rappels.canada.ca/robots.txt` (checked in full — a standard
Drupal robots.txt) does not disallow the
`/sites/default/files/opendata-donneesouvertes/` path used here.

## Document/storage shape

Keyed by `NID` directly — the record's own natural, stable primary key:

```
data/ca/recalls/documents/<NID>/current.json
```

Each document's `canonical_url` is the record's own dedicated
human-readable recall page (`record["URL"]`) — not the shared
`DATASET_URL` every record's raw data comes from, which would otherwise
make "browse original source" dump a reader into the entire ~34,000-
record dataset instead of the one recall they were looking at. Falls
back to `DATASET_URL` only if a record is ever missing its own `URL`
(not observed live). `source_metadata`: `nid`, `issue`, `category`,
`recall_class`, `last_updated`, `archived`.

## Config knobs

No source-specific overrides beyond `enabled: true`. At ~8,600
medical-device records, a full backfill fits comfortably within one run
at the global default `max_new_documents_per_run` (1000 — about 9 runs).

## Known quirks / maintenance notes

- **Whole-dataset re-fetch on every run**, same tradeoff as
  [`ca:mdall`](mdall.md)'s own — the dataset also covers food, vehicles,
  consumer products, etc.; `Organization == "Medical devices"` is the
  only filter applied.

## Related sources

- [`ca:mdall`](mdall.md) — device licensing, a different Health Canada
  database.
- [`fda:recalls`](../fda/recalls.md) — the same role in the FDA
  namespace.
