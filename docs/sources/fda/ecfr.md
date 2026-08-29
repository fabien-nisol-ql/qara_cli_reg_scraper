# `fda:ecfr` — 21 CFR Title 21, full text

## What it covers

The complete regulatory text of Title 21 of the Code of Federal
Regulations — not just the device-relevant parts (862–892), but the
whole title, since Title 21 also covers drugs, food, cosmetics, tobacco,
and biologics. This is the actual regulatory document the FD&C Act
authorizes ([`fdc_act`](fdc_act.md)) and FDA's own guidance
([`guidance`](guidance.md)) interprets.

## How it's fetched

GovInfo's bulk data feed for the Electronic Code of Federal Regulations —
one single ~20MB XML document for the whole title:

```
https://www.govinfo.gov/bulkdata/ECFR/title-21/ECFR-title21.xml
```

This used to be fetched per-part directly from eCFR's own versioner API
(`ecfr.gov/api/versioner`), but that API is no longer usable here: eCFR's
`robots.txt` disallows `/api/versioner/v1/full/`, and this package's
`PoliteHttpClient` respects `robots.txt` by default, so every per-part
fetch raised `RobotsDisallowed` → `HardStop` before a single document
could be saved. GovInfo publishes the same content as one bulk XML file
per title, and its `robots.txt` does not disallow that path.

## Document/storage shape

Single fixed document id — the whole title, one document, no per-part or
per-section granularity:

```
data/fda/ecfr/documents/title-21/current.xml
```

This is a real granularity change from the old per-part scraper (~35
independently-versioned part-documents, each re-versioned only when that
specific part changed): now one document that gets a new archived version
whenever *anything* in Title 21 changes, not just device-relevant parts.
Old on-disk `documents/part-*/` directories from the previous scraper go
stale after this change and are not cleaned up automatically.
`source_metadata`: `{"title": 21, "source": "govinfo_bulkdata"}`.

## Config knobs

| Key | Value | Why |
|---|---|---|
| `recheck_after_days` | `14` | The base-class default (`None`) means "once fetched, skip forever" — wrong here, since the whole point of this source is noticing regulatory text changes over time. |

## Known quirks / maintenance notes

- **Expect frequent "updated" re-checks.** Because this is one file
  covering *all* of Title 21, a re-check is likely to see it as "updated"
  whenever anything in Title 21 changes — not just device-relevant parts
  — archiving a fresh ~20MB copy each time. Parsing the bulk file back
  into per-part or per-section documents (to restore fine-grained
  diffing and reduce that storage growth) is a natural follow-up, not
  done yet.
- `www.govinfo.gov` has shown no bot-management behavior — unlike
  `accessdata.fda.gov` (see [`clearances_510k.md`](clearances_510k.md)),
  no equivalent maintenance concern here. [`fdc_act`](fdc_act.md) uses
  the same GovInfo domain (a different collection, `COMPS` vs `ECFR`).

## Related sources

- [`fdc_act`](fdc_act.md) — the statute this title's regulations
  implement.
- [`classification`](classification.md) — device classification records
  cite regulation numbers (e.g. `880.2801`) that resolve to specific
  sections of this text.
