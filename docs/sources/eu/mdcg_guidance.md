# `eu:mdcg_guidance` — EU MDCG guidance documents and other MDR/IVDR guidance

## What it covers

Guidance from the Medical Device Coordination Group (MDCG) and other
Commission guidance interpreting the MDR ((EU) 2017/745) and IVDR ((EU)
2017/746) — the EU analog of [`fda:guidance`](../fda/guidance.md): not the
regulation text itself, but the Commission's own narrative explanation of
how it applies in practice (equivalence, classification, clinical
evaluation, UDI, notified bodies, vigilance, ...). 156 documents at last
count, grouped by the source page itself into ~19 topics (Annex XVI
products, Borderline and Classification, Clinical investigation and
evaluation, COVID-19, EUDAMED, Notified bodies, UDI, ...).

## How it's fetched

The European Commission's own guidance listing page:

```
https://health.ec.europa.eu/medical-devices-sector/new-regulations/guidance-mdcg-endorsed-documents-and-other-guidance_en
```

Unlike `fda:guidance`'s search widget, this page IS server-rendered — no
JavaScript needed, confirmed live. It's 21 separate `<table
class="ecl-table">` elements, one per topic category, each with a
Reference / Title / Publication header row. One HTTP request fetches the
whole listing; no pagination.

Each row's link is usually in the Reference cell —
`/document/download/<uuid>_en?filename=...` (a PDF, occasionally
`.docx`/`.doc`/`.xlsx`) — but a real minority (7 of 156, confirmed) has
the link in the Title cell instead, when Reference is plain text like
"Q&A". A handful of rows (3 of 156, confirmed) link somewhere else
entirely — a EUR-Lex Official Journal notice, a
`health.ec.europa.eu/publications/...` overview page, an
`ema.europa.eu` page — none of which carry the usual UUID or a filename
extension. The scraper checks both cells for a link, and falls back to a
stable hash of the URL (for the document id) and `html`/`text/html` (for
ext/content-type) for that non-standard handful, rather than assuming
every row fits the common shape.

`health.ec.europa.eu/robots.txt` doesn't disallow anything under
`/medical-devices-sector/` and publishes no `Crawl-delay` — same
generic robots.txt handling as every other source, no special-casing.

## Document/storage shape

Keyed by the UUID out of the `/document/download/<uuid>_en` URL (a hash
of the URL for the non-standard handful of rows that don't have one):

```
data/eu/mdcg_guidance/documents/<uuid>/current.pdf   # or .docx/.doc/.xlsx/.html
```

`source_metadata`: `reference` (e.g. "MDCG 2023-6", or "Q&A"), `category`
(the topic heading its table sits under), `publication` (e.g. "December
2023").

## Config knobs

No source-specific overrides beyond `enabled: true` — small, single-page
source, no backlog-pacing concerns like `fda:guidance`'s.

## Known quirks / maintenance notes

- **This is an HTML scrape, not a documented API** — same caveat as
  [`fda:guidance`](../fda/guidance.md)/[`fda:warning_letters`](../fda/warning_letters.md).
  If the table count/structure changes significantly, or the
  `ecl-table`/`data-ecl-table-header` markup disappears, the Commission
  has redesigned the page — reload it in a real browser and re-derive the
  row shape.
- **Link position and destination both vary per row** (see above) — don't
  assume the Reference cell always has the link, or that every link goes
  to `health.ec.europa.eu`'s own document store.

## Related sources

- [`eu:mdr`](mdr.md) / [`eu:ivdr`](ivdr.md) — the regulation text this
  guidance interprets.
