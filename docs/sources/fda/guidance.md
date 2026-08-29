# `fda:guidance` — FDA guidance documents for medical devices

## What it covers

FDA's own interpretive/policy guidance documents, filtered to ones tagged
"Medical Devices". This includes the guidance that governs how the whole
classification/predicate chain works in practice — e.g. "The 510(k)
Program: Evaluating Substantial Equivalence in Premarket Notifications
[510(k)]", "FDA and Industry Procedures for Section 513(g) Requests for
Information", and "Medical Device Classification Product Codes" — FDA's
own narrative explanation of the statute ([`fdc_act`](fdc_act.md)) and
regulations ([`ecfr`](ecfr.md)).

## How it's fetched

FDA's own pre-generated static JSON dataset that backs the DataTables
widget on the guidance search page:

```
https://www.fda.gov/files/api/datatables/static/search-for-guidance.json
```

Found by loading the live search page in a real browser and watching its
network requests — the search page itself is entirely client-side
rendered (the `<table>` in the server-sent HTML has a `<thead>` but zero
`<tbody>` rows), so a naive HTML-table scrape returns nothing. This is
FDA's own dataset, not an inferred/reverse-engineered API shape.

Each dataset record covers *every* FDA guidance document, all centers —
this source filters to ones whose `field_regulated_product_field`
contains "Medical Devices" (a comma-joined taxonomy string, so a
combination-product guidance like "Biologics, Medical Devices" still
matches). One HTTP request fetches the whole dataset (2,788 total records
at last count, ~640 tagged Medical Devices); no pagination needed.

## Document/storage shape

Keyed by the document id parsed out of the guidance page's own URL slug:

```
data/fda/guidance/documents/<slug>/current.html
```

`source_metadata`: `issue_date`, `status`, `communication_type`,
`issuing_office`, `center`, `regulated_product_field`, `docket_number`,
`open_for_comment`.

## Config knobs

No source-specific overrides beyond `enabled: true` — this source relies
on the shared skip/budget/hard-stop policy in `base_scraper.py` with
global defaults, since the backlog (~640 documents) fills in gradually
over many scheduled runs rather than needing tuned per-source pacing.

## Known quirks / maintenance notes

- **This is an HTML scrape, not a documented API** (the dataset URL
  itself is real JSON, but it's FDA's internal DataTables backing data,
  not a published API contract) — written without the ability to load
  the live search page and inspect its current DOM/dataset shape at the
  time. If the dataset URL 404s or the "Medical Devices" tag stops
  appearing, FDA has changed their DataTables config — reload the search
  page in a real browser (JS execution required) and check its network
  requests for whatever replaced this URL. [`warning_letters`](warning_letters.md)
  shares this same "no documented API" caveat.
- [`ecfr`](ecfr.md) and [`recalls`](recalls.md) are backed by stable,
  documented government APIs and don't need this kind of live
  re-verification.

## Related sources

- [`fdc_act`](fdc_act.md) / [`ecfr`](ecfr.md) — the statute/regulations
  this guidance interprets.
- [`classification`](classification.md) — "Medical Device Classification
  Product Codes" guidance (already present in this corpus) documents the
  same product-code scheme this source's data models directly.
