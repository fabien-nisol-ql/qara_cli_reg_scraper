# Source registry sync (CLI → qara-reg-scraper-svc)

## What this is

`qara-reg-scraper-svc`'s `GET /v1/sources` — what a UI (e.g. the client
admin portal's Markets view) calls to list every source that exists, even
before anything's been scraped — is backed by a real Postgres table
(`regulation_source`) that this CLI keeps in sync, rather than a
hand-maintained list on the service side. Before this existed, the
service carried its own separate, manually-updated Java list
(`RegulationSourceRegistry.java`) that had to be edited by hand every time
a source was added here — it drifted (missed `classification`/`fdc_act`
for a while) precisely because nothing forced it to stay current. This
mechanism replaces that with a real push from the source of truth
(`regulations/__init__.py`'s `REGULATION_REGISTRY`, the same registry
`list-sources` reads) to the service, automatically.

## What gets pushed

The **entire** known-source registry, every time — never a partial or
filtered list. One entry per `(regulation, source)`:

```json
{"regulation": "fda", "source": "ecfr", "label": "eCFR", "description": "21 CFR Title 21, full text (GovInfo bulk XML)", "enabled": true, "requestsPerSecond": 1.0, "maxNewDocumentsPerRun": 1000, "recheckAfterDays": 14, "lookbackDays": null}
```

`label` is each `BaseScraper` subclass's `label` class attribute (a short
display name, e.g. `"eCFR"`, `"510(k) Clearances"`) — distinct from the
longer `description` already used by `list-sources`. Falls back to the
source's `name` if a scraper hasn't set one.

The five settings fields (`enabled` onward) are each source's *effective*
value — `SourceSettings` from `config.yaml` if set, else the matching
global default (`Settings.http.requests_per_second`,
`Settings.max_new_documents_per_run`) — the same precedence chain `run`'s
own preview table resolves, not the raw (possibly-`None`) override. This
is what lets `GET /v1/sources` show a viewer what to actually expect from
a source. `recheckAfterDays`/`lookbackDays` being `null` is a real,
meaningful value (never re-checked; not a lookback-windowed source),
not "unset" — see `_source_registry_payload`'s own docstring in `cli.py`.

The service treats a sync as **replace-in-place**: it upserts every entry
in the payload by `(regulation, source)`, and deletes any row it already
had that *isn't* in the payload. So this is a true sync, not an additive
push — a source removed from `REGULATION_REGISTRY` disappears from
`GET /v1/sources` on the next sync too.

## When it actually happens — exact triggers

There is **no dedicated schedule of its own** for this — no cron, no
polling loop, on either side. It rides entirely on two existing triggers:

| Trigger | What happens | Frequency |
|---|---|---|
| **`qara-reg-scraper-svc` starts** (boot/restart/redeploy) | A startup listener on the service side launches a `sync-sources`-only CLI job through the same Docker/K8s job mechanism it already uses for real scrapes — async, doesn't block the service's own startup. | Once per service process start. |
| **Any real `qara-reg-scraper run` invocation** — manual, or the service's own daily scheduled scrape | `run` pushes the full registry as one of its first steps (before any actual scraping), regardless of which specific `--source` that invocation was scoped to. | At least once a day via the existing scheduled scrape (if one's configured), more often with manual/on-demand runs. |

Practical effect: a new source added to this CLI's `REGULATION_REGISTRY`
becomes visible in `GET /v1/sources` **immediately** on the service's next
restart, or **within one day** (the default scheduled-scrape cadence) if
the service just keeps running — whichever happens first. No manual step
on the service side either way.

**Not covered by this**: whether a source is actually *scraped* on the
daily schedule is a separate, still-manually-configured setting on the
service side (`qaralink.scheduler.sources`) — this mechanism only keeps
the *discoverability* list (`GET /v1/sources`) current, not what gets
run automatically. Adding a newly-registered source to that schedule is
still a manual step.

## How to trigger it yourself

```bash
QARA_REG_SCRAPER_SERVICE__BASE_URL=http://reg-scraper:8080/api/reg-scraper qara-reg-scraper sync-sources
```

Hard-requires `service.base_url` (exits 2 without it, same as
`reindex`/`status`) — unlike the graceful push built into `run` (which
logs a warning and keeps scraping if the sync itself fails), this
standalone command's only job is the sync, so it fails loudly.

## Where the other half lives

The service-side table, endpoints (`GET`/`PUT /v1/sources`), and startup
listener live in the sibling `qara-reg-scraper-svc` repo — see that
repo's `README.md`, "Source registry sync" section, for the
`RegulationSourceEntity`/`RegulationSourceService`/
`SourceRegistrySyncStartupListener` implementation details.

## Relevant code

- `regulations/__init__.py` / `regulations/fda/__init__.py` —
  `REGULATION_REGISTRY`, the actual source of truth this payload is built
  from.
- `base_scraper.py` — `BaseScraper.label`/`description` class attributes.
- `cli.py` — `_source_registry_payload()` (shared payload builder),
  `sync_sources()` (the standalone command), and the graceful push wired
  into `run()`.
- `service_client.py` — `ScraperServiceClient.sync_sources()`.
