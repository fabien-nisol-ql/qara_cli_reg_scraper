# `fda:warning_letters` — FDA warning letters

## What it covers

FDA warning letters — **all centers, not just devices**. There is no
working device-specific filter on FDA's own search page (see Known
quirks below), so this scrapes everything and records `issuing_office`
in each document's metadata; filter to CDRH-issued letters downstream
(a DB query, or a config-level `source_metadata` check) rather than
relying on an FDA search parameter that doesn't actually work.

## How it's fetched

HTML scrape (`bs4`) of FDA's own warning-letters listing page:

```
https://www.fda.gov/inspections-compliance-enforcement-and-criminal-investigations
  /compliance-actions-and-activities/warning-letters
```

(the original, shorter URL — missing the
`compliance-actions-and-activities` segment — 404s; confirmed and fixed
while building this.) Unlike [`guidance`](guidance.md)'s source page,
this one's results table **is** server-rendered: the first page of real
rows, with real per-letter detail-page links, comes back in the initial
HTML, and plain `?page=N` genuinely paginates server-side (confirmed:
page 0 and page 1 return different, real rows) — no JavaScript/AJAX
needed to read it.

Paginates up to `MAX_PAGES` (25) per run — a safety cap on how many
listing pages one run walks, not the primary pacing mechanism (that's
`max_new_documents_per_run`). ~3,650 letters exist in total (365 pages @
10/page), so a full backfill takes many runs regardless, spread out the
same way [`guidance`](guidance.md)'s large backlog is.

## Document/storage shape

Document id is the href's last path segment, which already carries a
unique per-letter id + date suffix (e.g.
`thomas-brunner-hygiene-gmbh-729018-07242026`) — deliberately not a
slugified company name, since that repeats across multiple letters to
the same firm:

```
data/fda/warning_letters/documents/<slug-id>/current.html
```

`source_metadata`: `posted_date`, `letter_issue_date`, `issuing_office`.

## Config knobs

No source-specific overrides beyond `enabled: true` — same rationale as
[`guidance`](guidance.md): the backlog fills in gradually over many
scheduled runs under global defaults.

## Known quirks / maintenance notes

- **There is no working device-specific filter.** The page's own "Issuing
  Office" exposed filter (`search_api_fulltext_issuing_office`) does not
  actually filter server-side — tested directly, a request with it set
  returns the identical unfiltered first page — and the site's DataTables
  Excel export is capped at 1,000 of ~3,650 total rows and doesn't include
  per-letter links at all, so it isn't a viable substitute either. Hence
  the "scrape everything, filter downstream" approach above.
- **This is an HTML scrape, not a documented API** — FDA doesn't publish
  one for this data (same caveat as [`guidance`](guidance.md)). Written
  without the ability to load the live page and inspect its current DOM
  at the time — validate against a real run and adjust the table
  selector if rows come back empty or off-target.
- `estimate()` cannot report a real total: FDA's own displayed total is
  client-side rendered, and walking all ~365 pages just to count them
  would defeat the point of a cheap preview. What it reports instead is
  the all-time local count already captured (read straight from the
  manifest, no network call) — not scoped to any window; use
  `--max-new-documents` to bound a run instead of relying on the preview
  count.

## Related sources

None directly — this source is intentionally broad (all centers), unlike
the device-specific sources in this package.
