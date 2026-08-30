# `ca:food_and_drugs_act` — Food and Drugs Act, full text

## What it covers

The complete text of the Food and Drugs Act (R.S.C., 1985, c. F-27) — the
statutory basis [`ca:mdr`](mdr.md) is made under, the Canadian analog of
`fda:fdc_act`.

## How it's fetched

Same mechanism and host as [`ca:mdr`](mdr.md) — see that source's own doc
for the "single fixed URL, no discovery step" rationale and the
`laws-lois.justice.gc.ca` robots.txt finding (no file at all, confirmed
live):

```
https://laws-lois.justice.gc.ca/eng/acts/f-27/FullText.html
```

Confirmed live while building this.

## Document/storage shape

```
data/ca/food_and_drugs_act/documents/food-and-drugs-act/current.html
```

`source_metadata`: `citation` (`"R.S.C., 1985, c. F-27"`), `source`
(`"justice_laws"`).

## Config knobs

| Key | Value | Why |
|---|---|---|
| `recheck_after_days` | `14` | Same rationale as `ca:mdr`'s. |

## Known quirks / maintenance notes

Same as [`ca:mdr`](mdr.md) — see that doc.

## Related sources

- [`ca:mdr`](mdr.md) — the regulations made under this Act.
- [`fda:fdc_act`](../fda/fdc_act.md) — the same role in the FDA namespace.
