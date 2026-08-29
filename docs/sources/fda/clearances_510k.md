# `fda:clearances_510k` — 510(k) / De Novo device clearances + summary PDFs

## What it covers

Individual 510(k)/De Novo clearance records — the actual predicate-device
data: device name, applicant, decision date, product code (joins to
[`classification`](classification.md)), and — when published — the
510(k) summary PDF itself, the document that shows *how* a prior
submitter argued substantial equivalence against their own predicate.
This is the source the whole classification → predicate-search chain
(discussed at length in this project's own history) exists to reach.

## How it's fetched

openFDA's documented `device/510k` JSON API
(https://open.fda.gov/apis/device/510k/) for metadata, plus a direct
per-clearance PDF fetch from `accessdata.fda.gov` for the summary
document itself — **two separate documents per clearance**, since
they're two different things: openFDA's record is *metadata about* the
clearance, not the regulatory document a reviewer would actually want to
read.

Records with a `decision_date` in the last `lookback_days` (30 by
default) are pulled on every run, sorted newest first, via the shared
`iter_openfda_results` helper — enough to catch new clearances and any
late corrections without re-walking the full historical archive daily.

## Document/storage shape

```
data/fda/clearances_510k/documents/<k_number>/metadata/current.json   — the openFDA record
data/fda/clearances_510k/documents/<k_number>/summary/current.pdf     — the 510(k) summary PDF
```

Not every clearance has a summary PDF: FDA only requires one when
`statement_or_summary == "Summary"` (the alternative, `"Statement"`,
means the safety/effectiveness statement is available on request but not
posted online) — those are recorded as `not_applicable`, not an error,
and never retried.

Budget applies to the PDF fetch specifically, since that's the part of
this source that costs a real per-document round-trip to a host known to
bot-block (see below) — metadata arrives free with the listing page, so
saving it never consumes budget.

## Config knobs

| Key | Value | Why |
|---|---|---|
| `requests_per_second` | `0.2` (1 req/5s) | `accessdata.fda.gov` sits behind Akamai bot management and has been observed blocking well-identified, rate-limited requests from datacenter/cloud IPs — see Known quirks below. Slower than the global default (`1.0`) on purpose. |
| `max_new_documents_per_run` | `10` | Lower than the global default — a bot-managed host is exactly where you want a run to stop early and retry tomorrow rather than push through a big batch. |
| `lookback_days` | `30` (built-in default; `365` in this repo's own `config.yaml`) | How many days back to ask openFDA for. 30 is enough for a daily job to never miss a clearance; widen it for a one-off backfill or narrow it to cut per-run listing size — also overridable per invocation with `run --lookback-days`. |

## Known quirks / maintenance notes

**This is the source with real bot-management blocking — the most
operationally sensitive one in this package.** `accessdata.fda.gov` (the
PDF host) sits behind Akamai bot management, a materially different
posture from `api.fda.gov`/`www.govinfo.gov` (no bot-management
challenge observed on either): a well-identified, rate-limited client can
still get served an HTML "apology" page instead of the PDF, especially
from a datacenter/cloud egress IP — and that page has been observed
served with a **404 status**, not just 200/403, which is why
`looks_like_bot_block` runs regardless of status code, before deciding
whether a non-200 response is a block (hard stop) or just a routine miss
(record and continue).

Only a *suspected bot-management block* or a genuine network/retry
failure raises `HardStop` and ends the whole run — a clean 404/403 for
one specific k-number (PDF never published, withdrawn record, ...) is
routine and does **not** stop the run; it's recorded as a per-document
error and the next clearance is attempted normally.

If you see runs consistently stopping early with
`bot_detection_suspected`, the remediation ladder, in order: run from a
non-datacenter IP first, then lower
`regulations.fda.sources.clearances_510k.requests_per_second` further,
then look into an official bulk-data channel from FDA/CDRH — confirmed
during this project's own investigation that **no such channel exists**
for the actual PDFs (openFDA's live API and its bulk JSON export are
both metadata-only, no PDF URL field at all) — only an unofficial,
third-party OCR'd dataset was found, which raises its own
provenance/authenticity concerns for regulated-product use and hasn't
been adopted.

## Related sources

- [`classification`](classification.md) — joined on `product_code`; the
  entry point for narrowing which clearances are worth pulling as
  candidate predicates, rather than scanning this whole database.
- [`fdc_act`](fdc_act.md) / [`guidance`](guidance.md) — the statutory
  basis (Section 510(k)) and FDA's own interpretive guidance ("The
  510(k) Program: Evaluating Substantial Equivalence...") for the
  comparison these summary PDFs document.
