# `fda:pma` — Premarket Approval (PMA) decisions + approval order letters

## What it covers

Individual PMA decisions — original approvals and every supplement —
the PMA pathway's own decision database, the exact counterpart of
[`clearances_510k`](clearances_510k.md) for Class III devices requiring
scientific evidence of safety/effectiveness rather than a substantial-
equivalence argument. Each record: applicant, device name, decision date,
decision code (approved/denied/...), product code (joins to
[`classification`](classification.md)), supplement type/reason — plus,
when published, the FDA approval order letter itself.

Until this source existed, PMA was the one FDA approval pathway with no
decision-level coverage at all in this package — the regulatory-text
layer (`ecfr`/`fdc_act`/`guidance`) covers *the rules* for PMA, but not
the actual approval decisions, unlike 510(k)'s own dedicated database.

## How it's fetched

openFDA's documented `device/pma` JSON API
(https://open.fda.gov/apis/device/pma/) for metadata, plus a direct PDF
fetch from `accessdata.fda.gov` for the approval order letter — same
two-document shape as [`clearances_510k`](clearances_510k.md), same
reasoning: openFDA's record is *metadata about* the decision, not the
regulatory document itself.

Records with a `decision_date` in the last `lookback_days` (30 by
default) are pulled on every run, sorted newest first, via the shared
`iter_openfda_results` helper.

## Document/storage shape

```
data/fda/pma/documents/<pma_number><supplement_number>/metadata/current.json   — the openFDA record
data/fda/pma/documents/<pma_number><supplement_number>/order/current.pdf       — the approval order letter
```

`<supplement_number>` is empty for an original approval (e.g. `P160035`)
and something like `S014` for a supplement (`P160035S014`) — a
composite id, since a PMA number accumulates many supplements over its
life and each is its own decision worth tracking separately.

The order-letter URL is predictable —
`https://www.accessdata.fda.gov/cdrh_docs/pdf<yy>/<pma_number><supplement_number>A.pdf`
— where `<yy>` is the two digits embedded in the *original* PMA number
itself (e.g. `P160035` → `16`), confirmed live to hold even for a
supplement decided years later (P160035's supplement S006, decided 2020,
still resolves under `pdf16/`, not `pdf20/`). Same convention confirmed
for [`clearances_510k`](clearances_510k.md)'s summary PDFs and
[`hde`](hde.md)'s approval orders — apparently a package-wide
`accessdata.fda.gov` convention, not specific to any one database.

Unlike 510(k)'s `statement_or_summary` field, openFDA's PMA records carry
no signal for "does an order letter exist" — confirmed live, roughly
*half* of all supplement records have none posted at all (see Known
quirks below). At that rate, a bare per-document error (this package's
usual "routine miss" handling) would mean re-attempting roughly half the
lookback window's decisions forever, since `already_have` only ever
recognizes a saved document, not a logged error. So a missing order
letter here — once confirmed as a clean, non-block miss — is instead
persisted as a `not_applicable` sentinel via `save_document`, exactly
like 510(k)'s own Statement-type clearances, just discovered per-decision
rather than predicted from a field.

## Config knobs

| Key | Value | Why |
|---|---|---|
| `requests_per_second` | `0.2` (1 req/5s) | `accessdata.fda.gov` sits behind Akamai bot management — see [`clearances_510k.md`](clearances_510k.md)'s Known quirks section; the same host serves this source's order letters. |
| `max_new_documents_per_run` | `10` | Same reasoning as `clearances_510k` — a bot-managed host is where you want a run to stop early and retry tomorrow. |
| `lookback_days` | `30` (built-in default; `365` in this repo's own `config.yaml`) | How many days back to ask openFDA for. Overridable per invocation with `run --lookback-days`. |

## Known quirks / maintenance notes

Same `accessdata.fda.gov` bot-management posture as
[`clearances_510k`](clearances_510k.md) — see that source's doc for the
full remediation ladder if runs start stopping early with
`bot_detection_suspected`.

**Expect a high routine-miss rate for supplements specifically** —
confirmed live: roughly half of all supplement records have no order
letter posted at that URL (routine 404s, not blocks), while original
approvals reliably do. This tracks how FDA actually publishes these:
only originals and some major/panel-track supplements get their own
posted order letter; many routine ones (30-day notices, real-time
process changes, ...) apparently don't. A `status=partial_failure` /
high `errors` count on a `pma` run is therefore expected, not a sign
something's broken — `already_have`/content-hashing means each of those
misses is only ever attempted once per document id either way.

## Related sources

- [`classification`](classification.md) — joined on `product_code`.
- [`clearances_510k`](clearances_510k.md) — the 510(k) pathway's own
  equivalent decision database; same shape, same host, same reasoning.
- [`hde`](hde.md) — the Humanitarian Device Exemption pathway's
  equivalent, added alongside this source for the same reason.
- [`fdc_act`](fdc_act.md) (Section 515) / [`ecfr`](ecfr.md) (21 CFR
  Part 814) / [`guidance`](guidance.md) — the statutory and regulatory
  basis for the PMA pathway these decisions are made under.
