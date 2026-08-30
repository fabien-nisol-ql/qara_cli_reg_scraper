# `ca:mdr` — Medical Devices Regulations (SOR/98-282), full text

## What it covers

The complete text of Canada's Medical Devices Regulations — the Canadian
analog of `fda:ecfr`/`eu:mdr`: the actual regulatory text
[`ca:guidance`](guidance.md) interprets, made under the
[`ca:food_and_drugs_act`](food_and_drugs_act.md).

## How it's fetched

The Justice Laws Website (`laws-lois.justice.gc.ca`), the Canadian
government's own consolidated-legislation site:

```
https://laws-lois.justice.gc.ca/eng/regulations/SOR-98-282/FullText.html
```

The regulation's `index.html` (not used) is a short landing/table-of-
contents page — real content lives at the separate `FullText.html`
sibling, confirmed live. Unlike `eu:mdr`'s EUR-Lex consolidated-CELEX
resolution (a legal act's "current version" moves to a new URL each time
it's amended), Justice Laws keeps ONE fixed URL always current in place —
no discovery step needed, closer to `fda:ecfr`'s GovInfo bulk feed.
Confirmed live: 210K characters of text, one GET.

`laws-lois.justice.gc.ca` has no `robots.txt` at all (confirmed live: the
`/robots.txt` path itself returns a real 404 page, not an empty file) —
no declared restrictions.

## Document/storage shape

Single fixed document id:

```
data/ca/mdr/documents/mdr/current.html
```

`Manifest.save_document`'s own hash-based versioning notices when an
amendment changes the text. `source_metadata`: `instrument_number`
(`"SOR/98-282"`), `source` (`"justice_laws"`).

## Config knobs

| Key | Value | Why |
|---|---|---|
| `recheck_after_days` | `14` | Same rationale as `fda:ecfr`'s/`eu:mdr`'s — the base-class default (`None`) means "once fetched, skip forever," which would mean an amendment is never noticed. |

## Known quirks / maintenance notes

- Simpler than either FDA or EU equivalent — one fixed URL, no
  version-discovery mechanics to maintain.

## Related sources

- [`ca:food_and_drugs_act`](food_and_drugs_act.md) — the statute this
  regulation is made under.
- [`ca:guidance`](guidance.md) — Health Canada's own interpretation.
- [`eu:mdr`](../eu/mdr.md) / [`fda:ecfr`](../fda/ecfr.md) — the same role
  in the other two regulation namespaces.
