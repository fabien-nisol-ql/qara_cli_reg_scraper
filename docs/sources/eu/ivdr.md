# `eu:ivdr` — Regulation (EU) 2017/746 (IVDR), consolidated full text

## What it covers

The complete, current consolidated text of the In Vitro Diagnostic
Medical Devices Regulation — identical role to [`eu:mdr`](mdr.md), for
IVD devices instead of general medical devices.

## How it's fetched

Identical mechanism to [`eu:mdr`](mdr.md) — see that source's own doc and
`eur_lex_consolidated.py`'s module docstring for the full discovery
mechanism. Applied to the IVDR's own original CELEX (`32017R0746`)
instead of the MDR's. Confirmed live, 2026-08-30: current consolidated
version `CELEX:02017R0746-20250110`, a single ~1.7MB HTML page.

## Document/storage shape

```
data/eu/ivdr/documents/ivdr/current.html
```

Same single-fixed-document-id philosophy as `eu:mdr`. `source_metadata`:
`original_celex` (`"32017R0746"`), `consolidated_celex`, `source`
(`"eur_lex"`).

## Config knobs

| Key | Value | Why |
|---|---|---|
| `recheck_after_days` | `14` | Same rationale as `eu:mdr`'s. |

## Known quirks / maintenance notes

Same as [`eu:mdr`](mdr.md) — see that doc.

## Related sources

- [`eu:mdr`](mdr.md) — identical mechanism, the MDR's own CELEX.
- [`eu:mdcg_guidance`](mdcg_guidance.md) — MDCG guidance covers both
  regulations; its `category` metadata includes an explicit "In Vitro
  Diagnostic medical devices (IVD)" topic.
