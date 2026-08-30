# `eu:mdr` — Regulation (EU) 2017/745 (MDR), consolidated full text

## What it covers

The complete, current consolidated text of the Medical Devices
Regulation — the EU analog of [`fda:ecfr`](../fda/ecfr.md): the actual
regulatory text [`mdcg_guidance`](mdcg_guidance.md) interprets, rather
than an interpretation of it.

## How it's fetched

EUR-Lex, via a two-step discovery mechanism (`eur_lex_consolidated.py`,
shared with [`eu:ivdr`](ivdr.md)):

1. Fetch `https://eur-lex.europa.eu/legal-content/EN/ALL/?uri=CELEX:32017R0745`
   — every consolidated version of the MDR is listed here, and the
   currently-in-force one is explicitly labeled ("Access current
   version (DD/MM/YYYY)"), confirmed live.
2. Fetch `https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:<resolved>`
   for the actual text — a single ~1.8MB HTML page (confirmed live,
   2026-08-30: `CELEX:02017R0745-20260719`).

Unlike GovInfo's bulk feed for `fda:ecfr`, there is no one fixed URL
that's always "the current MDR text" — a legal act's consolidated CELEX
id changes every time an amendment is folded in (8 versions since 2017,
confirmed live). See `eur_lex_consolidated.py`'s module docstring for the
full mechanism and its defensive fallback (max date-suffixed CELEX found
on the page) if the "current version" label ever goes missing.

`eur-lex.europa.eu/robots.txt` (checked in full) doesn't disallow either
the `/ALL/` or `/TXT/` paths used here — only the `/TXT/DOC/` and
`/TXT/SIG/` per-language export sub-paths — and publishes
`Crawl-delay: 10`, already honored automatically like every other
source's robots.txt directives.

## Document/storage shape

Single fixed document id, regardless of which consolidated version is
actually current — same philosophy as `fda:ecfr`'s `title-21`:

```
data/eu/mdr/documents/mdr/current.html
```

`Manifest.save_document`'s own hash-based versioning is what notices a
new consolidated version's text differs from what's stored and archives
it — not a per-version document id. `source_metadata`: `original_celex`
(`"32017R0745"`, never changes), `consolidated_celex` (the specific
version actually captured), `source` (`"eur_lex"`).

## Config knobs

| Key | Value | Why |
|---|---|---|
| `recheck_after_days` | `14` | Same rationale as `fda:ecfr`'s — the base-class default (`None`) means "once fetched, skip forever," which would mean a new consolidated version (after an amendment) is never noticed. |

## Known quirks / maintenance notes

- **Two HTTP requests per run** (discovery + text), unlike `fda:ecfr`'s
  one — the discovery step (`resolve_latest_consolidated`) is what makes
  this work at all despite there being no fixed "current" URL.
- **If EUR-Lex ever drops the "Access current version" label**, this
  falls back to the lexicographically-max date-suffixed CELEX found on
  the page — correct as long as the suffix stays `YYYYMMDD`, but a
  defensive fallback, not the primary mechanism. If both the label and
  the fallback ever misbehave, EUR-Lex has likely redesigned the `/ALL/`
  page — reload it in a real browser and re-derive the shape.

## Related sources

- [`eu:ivdr`](ivdr.md) — identical mechanism, the IVDR's own CELEX.
- [`eu:mdcg_guidance`](mdcg_guidance.md) — the Commission's own
  interpretation of this text.
