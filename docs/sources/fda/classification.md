# `fda:classification` — Device Product Classification

## What it covers

FDA's Product Classification database: one record per **product code** (a
3-letter code, e.g. `LWW`), giving the device type's class (I/II/III),
its implementing CFR regulation number (e.g. `880.2801`), review panel /
medical specialty, and a plain-language definition.

This is the entry point for predicate-device research — it turns "what
does my device do" into a specific product code, which is the same field
every [`fda:clearances_510k`](clearances_510k.md) record carries. Filtering
510(k) clearances by product code (rather than scanning the whole
database) is what makes predicate search targeted. See
[`fdc_act.md`](fdc_act.md) and [`clearances_510k.md`](clearances_510k.md)
for how this fits into the rest of the chain (statute → regulation →
guidance → classification → clearance/predicate).

## How it's fetched

openFDA's own documented JSON API — `device/classification`
(https://open.fda.gov/apis/device/classification/) — via the shared
`iter_openfda_results` helper in `openfda_common.py`, the same one
[`clearances_510k`](clearances_510k.md) and [`recalls`](recalls.md) use.

Unlike those two, this is **not** a lookback-windowed activity stream —
the classification catalog is a stable reference table that changes only
occasionally (FDA reclassification proceedings), so every run walks the
**whole catalog** (`search=product_code:*`, sorted by
`regulation_number.exact` — `product_code` itself isn't a sortable field
in this index, confirmed live) rather than "what changed in the last N
days". Content-hashing in `Manifest.save_document` keeps a full walk
cheap on repeat runs: an unchanged record just bumps `last_checked_at`
without archiving a new version.

Catalog size is ~7,100 product codes (confirmed live) — comfortably under
the `max_records` cap in `classification.py` and openFDA's own ~25,000
deep-paging limit, so one run walks the entire thing.

## Document/storage shape

Keyed by `product_code` directly (it's already the record's natural,
stable primary key — no synthetic id needed):

```
data/fda/classification/documents/<product_code>/current.json
```

`source_metadata` carries `product_code`, `device_name`, `device_class`,
`regulation_number`, `review_panel`, `medical_specialty` (+ description),
`submission_type_id`, and `definition`.

## Config knobs

| Key | Default | Why |
|---|---|---|
| `recheck_after_days` | `90` | Reference table, not an activity stream — device classifications do change (reclassification proceedings) but rarely, much less often than [`ecfr`](ecfr.md)'s CFR text (14 days). Without this, the global default (never re-check) means a reclassification is never noticed after the first fetch. |
| `max_new_documents_per_run` | global default | No source-specific override — the whole catalog is small enough that the global default budget clears a full backfill in a handful of runs. |

## Known quirks / maintenance notes

- **No cheap way to sort by `product_code`.** openFDA rejects
  `sort=product_code:asc` ("Sorting allowed by non-analyzed fields only")
  and `product_code.exact` doesn't exist in this index's mapping either
  (confirmed live) — `regulation_number.exact` does, and is what's used
  for deterministic skip-based pagination.
- **Empty `search=` is rejected** (400) — openFDA wants an explicit
  wildcard (`product_code:*`) to mean "everything", not a bare empty
  string.
- Unlike `accessdata.fda.gov` (see [`clearances_510k.md`](clearances_510k.md)),
  `api.fda.gov` has shown no bot-management behavior — no equivalent
  maintenance concern here.

## Related sources

- [`clearances_510k`](clearances_510k.md) — joined on `product_code`;
  this source is the entry point, that one is where the actual predicate
  clearances (and their summary PDFs) live.
- [`fdc_act`](fdc_act.md) / [`ecfr`](ecfr.md) — the statute and
  regulation this classification scheme implements.
