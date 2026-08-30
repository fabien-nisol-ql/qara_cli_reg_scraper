# `fda:hde` — Humanitarian Device Exemption (HDE) approvals + approval order letters

## What it covers

Every Humanitarian Device Exemption approval FDA has ever granted — the
HDE pathway's own decision database, the equivalent of
[`clearances_510k`](clearances_510k.md)/[`pma`](pma.md) for devices
treating rare conditions (affecting no more than 8,000 individuals in the
US per year). Each record: HDE number, approval date, device/generic
name, applicant, description — plus, when published, the FDA approval
order letter itself.

Like [`pma`](pma.md), this closes a real coverage gap: until these two
sources existed, HDE and PMA were the two FDA approval pathways with no
decision-level coverage at all in this package.

## How it's fetched

Unlike [`clearances_510k`](clearances_510k.md)/[`pma`](pma.md), **openFDA
has no HDE endpoint** — confirmed against open.fda.gov's own device API
index (510(k), classification, enforcement, event, PMA, recall,
registrationlisting, udi, covid19serology; no HDE, no De Novo). This
source instead scrapes fda.gov's own listing page:

https://www.fda.gov/medical-devices/hde-approvals/listing-cdrh-humanitarian-device-exemptions

That page's "Show 10/25/.../All entries" control is DataTables-driven,
but — unlike [`guidance`](guidance.md)'s search page — the **entire**
dataset (currently 89 records) is embedded directly in the plain
server-rendered HTML on first load, not fetched via a separate AJAX/JSON
call (confirmed live with a single unauthenticated GET, no JS execution:
all 89 rows come back, including the oldest, withdrawn ones). So, like
[`classification`](classification.md), this is a **full-catalog walk
every run** rather than a lookback window — the dataset is small and
grows only a few times a year, and content-hashing in
`Manifest.save_document` keeps repeat full walks cheap.

## Document/storage shape

```
data/fda/hde/documents/<hde_number>/metadata/current.json   — the listing row's own fields
data/fda/hde/documents/<hde_number>/order/current.pdf        — the approval order letter
```

The order-letter URL follows the exact same `accessdata.fda.gov`
convention as [`clearances_510k`](clearances_510k.md)/[`pma`](pma.md):
`https://www.accessdata.fda.gov/cdrh_docs/pdf<yy>/<hde_number>A.pdf`,
where `<yy>` is the two digits embedded in the HDE number itself (e.g.
`H200002` → `20`) — confirmed live via an individual HDE's own detail
page (`accessdata.fda.gov/.../cfhde/hde.cfm?id=<internal id>`), itself
only reachable through this listing page's own per-row links (there's no
separate public search/listing endpoint on `accessdata.fda.gov` worth
scraping on its own).

A handful of very old records (confirmed live: two, formally withdrawn
per FDA's own note on the page) have no detail-page link at all — their
listing row carries the HDE number as plain text instead of a link. This
is known in advance from the row itself, not discovered by a blind fetch
attempt, so those get a `not_applicable` order sentinel exactly like
[`clearances_510k`](clearances_510k.md)'s own Statement-type clearances,
rather than ever being attempted.

## Config knobs

| Key | Value | Why |
|---|---|---|
| `requests_per_second` | `0.2` (1 req/5s) | The order letters live on `accessdata.fda.gov`, the same Akamai-bot-managed host as [`clearances_510k`](clearances_510k.md)/[`pma`](pma.md) — see `clearances_510k.md`'s Known quirks. The listing page itself (`fda.gov`) isn't bot-managed, but the per-run budget is the order-letter fetches, so the same slow pace applies package-wide for this source. |
| `max_new_documents_per_run` | `10` | Same reasoning as `clearances_510k`/`pma`. |

No `lookback_days` — this source has no lookback window at all (see
above); every run considers the full current listing.

## Known quirks / maintenance notes

- **A row's HDE number isn't always in a fixed cell.** Most rows carry it
  in the "HDE Number and Docket Number" column alongside a link to the
  detail page; the handful of withdrawn records instead duplicate the
  approval-date column's text into that cell too (confirmed live) — the
  parser searches both cells for the `H\d{6}` pattern rather than
  assuming which one it's in.
- Same `accessdata.fda.gov` bot-management posture as
  [`clearances_510k`](clearances_510k.md)/[`pma`](pma.md) for the order
  letters specifically — see that source's doc for the full remediation
  ladder if runs start stopping early with `bot_detection_suspected`.

## Related sources

- [`pma`](pma.md) — the PMA pathway's equivalent, added alongside this
  source for the same reason (closing a decision-database coverage gap).
- [`clearances_510k`](clearances_510k.md) — the 510(k) pathway's own
  equivalent; the original template this source and `pma` both follow.
- [`fdc_act`](fdc_act.md) (Section 520(m)) / [`ecfr`](ecfr.md) (21 CFR
  Part 814 Subpart H) — the statutory and regulatory basis for the HDE
  pathway these approvals are made under.
