# `fda:fdc_act` — Federal Food, Drug, and Cosmetic Act (full text)

## What it covers

The full text of the Federal Food, Drug, and Cosmetic Act (FD&C Act) —
the statutory basis every other FDA source in this package implements or
interprets: 21 CFR Parts 862–892 ([`ecfr`](ecfr.md)) are FDA's
*regulations* under this Act, and every FDA guidance document
([`guidance`](guidance.md)) is FDA's own *interpretation* of it. Sections
513 (device classification), 513(g) (formal classification requests), and
510(k) (premarket notification) all live here. Nothing else in this
package scraped the statute itself before this source was added.

## How it's fetched

GovInfo's **Statute Compilations (COMPS)** collection — specifically
package `COMPS-973`, the Office of the Law Revision Counsel's own
positive-law-style compilation of this one act, consolidated and kept
current ("Federal Food, Drug, and Cosmetic Act [As Amended Through
P.L. ...]") — fetched as USLM XML:

```
https://www.govinfo.gov/content/pkg/COMPS-973/uslm/COMPS-973.xml
```

**Not** GovInfo's bulkdata service (the same one [`ecfr`](ecfr.md) uses
for CFR Title 21) — confirmed directly that bulkdata does not carry a
USCODE collection at all; its only Title-21-adjacent collections are
CFR/ECFR, which are regulations, not statute. `uscode.house.gov` (OLRC's
own site, the other candidate for a full-Title-21-U.S.-Code feed) was
unreachable to probe when this source was built.

COMPS is arguably the better fit anyway, not just the available one: it's
a compilation of *one specific act*, rather than the full, much broader
Title 21 U.S. Code (which also covers unrelated material folded into that
title over the decades) — closer to "the FD&C Act" as a document than a
raw whole-title dump would be.

Found via GovInfo's own search UI (`collection:COMPS "Federal Food, Drug,
and Cosmetic Act"`), not URL-guessing — GovInfo's bulkdata paths return a
misleading 200-status HTML "Bulkdata Service Error" page for any wrong
guess, the same kind of soft-failure `looks_like_bot_block` exists to
catch elsewhere in this package, so discovery had to go through the real
search UI/package details page rather than probing filenames blind.

Uses the USLM XML rendition rather than the PDF — smaller than the PDF at
similar information density, and section-addressable if per-section
extraction is ever wanted (same tradeoff [`ecfr`](ecfr.md) already made
for CFR text).

## Document/storage shape

Single fixed document id (there's exactly one document — the whole Act,
same "no per-record loop" shape as `ecfr`):

```
data/fda/fdc_act/documents/fdc-act/current.xml
```

`source_metadata`: `{"title": "Federal Food, Drug, and Cosmetic Act",
"source": "govinfo_comps", "package_id": "COMPS-973"}`.

## Config knobs

| Key | Default | Why |
|---|---|---|
| `recheck_after_days` | `30` | Same rationale as [`ecfr`](ecfr.md)'s own `recheck_after_days`: without it, the global default (never re-check) means statutory amendments are never noticed after the first fetch. Longer than `ecfr`'s 14 days — FD&C Act amendments are far less frequent than the CFR's own churn. |

## Known quirks / maintenance notes

- **GovInfo re-publishes the same package id in place** as the
  compilation is re-amended — confirmed live: the package's own "Amended
  Through" note already reflected a public law enacted after this source
  was written. That's what makes `recheck_after_days` meaningful here —
  content-hash comparison will pick up the next amendment automatically.
- **If `COMPS-973` ever 404s**, GovInfo has re-numbered the package (not
  observed, but COMPS package ids are assigned per-compilation, not
  derived from a stable slug) — re-run the search above
  (`collection:COMPS "Federal Food, Drug, and Cosmetic Act"`) and update
  `COMPS_PACKAGE_ID` in `regulations/fda/fdc_act.py`.
- `www.govinfo.gov` has shown no bot-management behavior in either the
  bulkdata (`ecfr`) or content/pkg (this source) URL families — no
  equivalent concern to `clearances_510k`'s Akamai issue.

## Related sources

- [`ecfr`](ecfr.md) — the implementing CFR regulations (21 CFR Parts
  862–892) for the device-classification provisions this Act sets out.
- [`guidance`](guidance.md) — FDA's own interpretive guidance on this
  Act, including "FDA and Industry Procedures for Section 513(g) Requests
  for Information" and "The 510(k) Program: Evaluating Substantial
  Equivalence in Premarket Notifications".
- [`classification`](classification.md) / [`clearances_510k`](clearances_510k.md)
  — the classification scheme and clearance records this Act's Section
  513/510(k) provisions establish.
