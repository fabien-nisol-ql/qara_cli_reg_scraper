# `ca:guidance` — Health Canada guidance documents for medical devices

## What it covers

Health Canada's guidance on how the [`ca:mdr`](mdr.md)/[`ca:food_and_drugs_act`](food_and_drugs_act.md)
apply in practice — device licence applications, classification, quality
systems, clinical evidence, labelling, and more. The Canadian analog of
[`fda:guidance`](../fda/guidance.md).

## How it's fetched

Health Canada's own guidance-documents listing page — a real,
server-rendered page (no JavaScript needed, unlike `fda:guidance`'s
dataset):

```
https://www.canada.ca/en/health-canada/services/drugs-health-products/medical-devices/application-information/guidance-documents.html
```

Every link inside the page's `property="mainContentOfPage"` region (the
WET/GCWeb theme's own marker for "actual content, not chrome" — every
Government of Canada site built on that framework carries it) — confirmed
live: 79–81 links (varies slightly run to run as Health Canada updates
the page). Unlike [`eu:mdcg_guidance`](../eu/mdcg_guidance.md)'s uniform
table, this page is a loosely curated content list, not a formal table:
most links are guidance documents proper ("Guidance document: ...",
"Guidance on ...", "Notice: ..."), but a handful are related-but-distinct
content Health Canada placed on the same page (an e-learning tool, ISO
13485 quality-systems info, the MDSAP program's own external site,
device-licence fees). Rather than try to classify "true guidance" vs.
"related content" from title text (fragile — the titling isn't
consistent enough), this captures everything in the main content region.

`www.canada.ca/robots.txt` (checked in full, 51 lines) has nothing
relevant disallowed — its rules are CRA/IRCC-specific.

## Document/storage shape

Document ids come from each URL's own path slug (every canada.ca page
ends in a clean `....html` segment, extension included — e.g.
`guidance-document-labelling-vitro-diagnostic-devices.html`):

```
data/ca/guidance/documents/<slug>.html/current.html
```

Two fallbacks for links that don't fit that shape (both confirmed live):
a bare-domain external link with no path at all (the MDSAP program's own
site) uses the domain itself as the id (e.g. `www.mdsap.global`); a
query-string-only external link (the one non-canada.ca e-learning tool)
falls back to a hash of the full URL. `source_metadata`: `link_text` (the
anchor's own text — the only structured signal this page gives per
link).

## Config knobs

No source-specific overrides beyond `enabled: true` — small, single-page
source, no backlog-pacing concerns.

## Known quirks / maintenance notes

- **This is an HTML scrape, not a documented API** — same caveat as
  every other HTML-scraped source in this tool
  ([`fda:guidance`](../fda/guidance.md)/[`fda:warning_letters`](../fda/warning_letters.md)/
  [`eu:mdcg_guidance`](../eu/mdcg_guidance.md)). If
  `property="mainContentOfPage"` ever disappears from the page, that's
  treated as this source's own error (hard stop), not a zero-document
  success — the WET/GCWeb theme change would need investigating.
- **Not every captured link is strictly a "guidance document"** — see
  above. Downstream consumers wanting only the formally-labeled ones can
  filter on `source_metadata.link_text` starting with "Guidance".

## Related sources

- [`ca:mdr`](mdr.md) / [`ca:food_and_drugs_act`](food_and_drugs_act.md) —
  the regulation/statute this guidance interprets.
- [`fda:guidance`](../fda/guidance.md) / [`eu:mdcg_guidance`](../eu/mdcg_guidance.md) —
  the same role in the other two regulation namespaces.
