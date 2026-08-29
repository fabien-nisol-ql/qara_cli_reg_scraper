"""Configuration for qara-reg-scraper.

Settings are assembled from, in increasing priority:
1. Defaults in this file
2. A YAML config file (``config.yaml`` by default, override with QARA_REG_SCRAPER_CONFIG_FILE)
3. Environment variables (prefix ``QARA_REG_SCRAPER_``, nested keys joined with ``__``)
4. A ``.env`` file in the working directory (for secrets — never commit this)

Only the file-based manifest under the storage root is ever load-bearing for
"what has been scraped" — qara-reg-scraper-svc's Postgres index is a
rebuildable, REST-synced view of it (see ``qara_reg_scraper.service_client``/
``qara_reg_scraper.sync``), and config controls *where* things go, not what
already happened.

Sources are namespaced by *regulation* (``fda``, and whatever gets added
next — see ``regulations/__init__.py``), addressed everywhere as
``<regulation>:<name>`` (e.g. ``fda:ecfr``). ``regulations.<code>.sources.<name>``
in config.yaml mirrors that — nothing here hardcodes which regulations or
sources exist; new ones just need a `regulations/<code>/<name>.py` module
and, optionally, a matching config.yaml block to override its defaults.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)


def normalize_unlimited(v: int | None) -> int | None:
    """-1 is a CLI-flag/config.yaml/env-expressible spelling of "unlimited"
    for max_new_documents_per_run (None already means unlimited to
    BaseScraper's own _budget_exhausted — see base_scraper.py — but None
    isn't reachable from a `--max-new-documents` CLI flag or a YAML/env
    scalar the same way a sentinel int is).

    Deliberately NOT a pydantic field validator on SourceSettings/Settings
    themselves — cli.py's CLI-flag > per-source > global precedence chain
    (see `run`) relies on `is not None` at each level to mean "this level
    actually set something." Normalizing -1 -> None *before* that chain
    runs would make "explicitly unlimited" indistinguishable from "not
    set here, fall through to the next level" — -1 would then lose to a
    lower-priority level's real value instead of winning as intended.
    Call this exactly once, on the single fully-resolved value, after the
    whole precedence chain has already picked a winner — see cli.py's
    `run` command."""
    return None if v == -1 else v


StorageBackendKind = Literal["local", "s3", "azure_blob", "sharepoint"]


class HttpSettings(BaseModel):
    """Controls how the tool talks to regulators' servers. See
    http_client.py — this tool identifies itself on every request and never
    tries to look like a browser or evade throttling."""

    contact_email: str = "fabien.nisol@gmail.com"
    project_url: str = "https://github.com/fnisol/qara-reg-scraper"
    # Set this to replace the composed User-Agent below wholesale — e.g. if
    # you want different wording, no URL, or a value a specific site's admin
    # asked you to use. Leave unset (the default) to get the
    # contact_email/project_url-composed string. Whatever you put here is
    # sent verbatim — this tool still never spoofs a browser UA; that's a
    # convention this config can't be used to break, only to word honestly.
    user_agent_override: str | None = None
    # Requests per second, per host. Deliberately conservative — this is a
    # daily batch job, not a race.
    requests_per_second: float = 1.0
    timeout_seconds: float = 30.0
    max_retries: int = 5
    respect_robots_txt: bool = True

    @property
    def user_agent(self) -> str:
        if self.user_agent_override:
            return self.user_agent_override
        return (
            f"qara-reg-scraper/0.1 "
            f"(+{self.project_url}; contact: {self.contact_email}; "
            f"purpose: regulatory-compliance monitoring, low-volume daily batch job)"
        )


class LocalStorageSettings(BaseModel):
    root: str = "./data"


class S3StorageSettings(BaseModel):
    bucket: str = ""
    prefix: str = "qara-reg-scraper"
    region: str | None = None
    endpoint_url: str | None = None  # for S3-compatible stores (MinIO, etc.)


class AzureBlobStorageSettings(BaseModel):
    container: str = ""
    prefix: str = "qara-reg-scraper"
    # Prefer a connection string via env/`.env` (AZURE_STORAGE_CONNECTION_STRING)
    # or account_url + DefaultAzureCredential for managed identity.
    account_url: str | None = None


class SharePointStorageSettings(BaseModel):
    site_url: str = ""
    drive_path: str = "Shared Documents/qara-reg-scraper"
    # Auth via client_id/client_secret/tenant_id (env/.env) — app-only auth,
    # not a user password.
    client_id: str | None = None
    tenant_id: str | None = None


class StorageSettings(BaseModel):
    backend: StorageBackendKind = "local"
    local: LocalStorageSettings = Field(default_factory=LocalStorageSettings)
    s3: S3StorageSettings = Field(default_factory=S3StorageSettings)
    azure_blob: AzureBlobStorageSettings = Field(default_factory=AzureBlobStorageSettings)
    sharepoint: SharePointStorageSettings = Field(default_factory=SharePointStorageSettings)


class ServiceSettings(BaseModel):
    """qara-reg-scraper-svc — owns the Postgres schema (via Flyway) and the
    REST endpoints this CLI upserts documents/runs/events/estimates to. The
    CLI never talks to Postgres directly (see the module docstring). If
    base_url is unset, `qara-reg-scraper run` still works (manifests are
    always written, still the mandatory source of truth) but doesn't report
    anywhere; `reindex`/`status` need this to do anything at all."""

    base_url: str | None = None  # e.g. http://reg-scraper:8080/api/reg-scraper
    timeout_seconds: float = 10.0


class SourceSettings(BaseModel):
    enabled: bool = True
    requests_per_second: float | None = None  # overrides HttpSettings default
    # Caps how many *new* documents a single run will fetch before it stops
    # cleanly and waits for tomorrow's scheduled run — the mechanism that
    # spreads a large backlog over days/weeks instead of hammering a
    # regulator's servers to catch up in one run. None here means "use the
    # global max_new_documents_per_run default" (see Settings below), not
    # "unlimited" — set -1 explicitly for that (see normalize_unlimited;
    # cli.py's `run` applies it after resolving CLI-flag > this > global
    # precedence, not here — see that function's own docstring for why).
    max_new_documents_per_run: int | None = None
    # Once a document is in the manifest, it's skipped on every later run
    # (no network call at all) unless it was last checked more than this
    # many days ago. None means "never re-check" — matches "skip everything
    # we already have" literally, at the cost of never noticing a change to
    # a document already captured. Set this if you want e.g. a monthly
    # re-verification pass instead.
    recheck_after_days: int | None = None
    # How many days back a listing-window source (fda:clearances_510k,
    # fda:recalls — anything that queries openFDA for "everything decided/
    # reported in the last N days" rather than walking a fixed list) looks.
    # None means "use that scraper's own built-in default" (30 for both
    # today) — only sources that actually have such a window read this at
    # all; it's a no-op on fda:ecfr/fda:guidance/fda:warning_letters.
    lookback_days: int | None = None


class RegulationSettings(BaseModel):
    """Config for one regulation namespace (e.g. "fda"), keyed by source
    name within it (e.g. "ecfr") — see regulations/__init__.py for how
    source names map to scraper classes."""

    sources: dict[str, SourceSettings] = Field(default_factory=dict)


DEFAULT_SOURCE_SETTINGS = SourceSettings()


def get_source_settings(settings: Settings, regulation: str, source: str) -> SourceSettings:
    """Resolve a (regulation, source) pair's settings, defaulting cleanly
    when neither the regulation nor the source is mentioned in config.yaml
    at all — every regulation/source is usable out of the box without
    requiring a config.yaml entry first."""
    regulation_settings = settings.regulations.get(regulation)
    if regulation_settings is None:
        return DEFAULT_SOURCE_SETTINGS
    return regulation_settings.sources.get(source, DEFAULT_SOURCE_SETTINGS)


def _load_yaml_defaults() -> dict:
    config_file = os.environ.get("QARA_REG_SCRAPER_CONFIG_FILE", "config.yaml")
    path = Path(config_file)
    if path.is_file():
        with path.open() as f:
            return yaml.safe_load(f) or {}
    return {}


class _YamlConfigSource(PydanticBaseSettingsSource):
    """Reads config.yaml as a settings source with lower priority than env
    vars / .env, but higher than the field defaults above."""

    def get_field_value(self, field, field_name):  # pragma: no cover - unused, required by ABC
        return None, field_name, False

    def __call__(self) -> dict:
        return _load_yaml_defaults()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="QARA_REG_SCRAPER_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    http: HttpSettings = Field(default_factory=HttpSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    service: ServiceSettings = Field(default_factory=ServiceSettings)
    # Keyed by regulation code ("fda", "eu", ...) — open-ended on purpose,
    # see the module docstring. A regulation absent here just means "use
    # every source's defaults."
    regulations: dict[str, RegulationSettings] = Field(default_factory=dict)
    log_level: str = "INFO"
    # Where the internal structured (JSON) log — every HTTP request,
    # retry, bot-detection event, etc. — is written. None (the default)
    # means no log sink at all: `run`'s human-oriented progress output
    # (see cli.py) is the only thing you see unless you ask for this too.
    # A real file path, or a device path like "/dev/stderr" to fold it
    # into whatever's already capturing stdout/stderr (e.g. `docker compose
    # logs`). Overridable per invocation with `run --log <path>` /
    # `reindex --log <path>`, which beat this when both are set.
    log_file: str | None = None
    # Where lock files live to prevent two instances scraping the same
    # source concurrently (e.g. an overrunning cron job).
    lock_dir: str = "./.locks"
    # Global default for SourceSettings.max_new_documents_per_run — how many
    # *new* documents one run of one source will fetch before stopping
    # cleanly, letting the backlog fill in gradually over many scheduled
    # runs instead of trying to catch up all at once. Was 25 (very
    # conservative — a big backlog like eCFR's ~600 documents took over 20
    # runs to catch up); raised to 1000 so a normal-sized source backfills
    # in one or two runs. Override per-source in config.yaml if a source
    # still needs a slower pace (e.g. lower for the accessdata.fda.gov-backed
    # PDF fetch in fda:clearances_510k). -1 means unlimited — catch up the
    # entire backlog in one run; see normalize_unlimited's own docstring for
    # why that's applied in cli.py after precedence resolution, not here.
    max_new_documents_per_run: int = 1000
    # Set (by the service, via QARA_REG_SCRAPER_RETRY_BUDGET_MINUTES) when
    # this run should retry itself on a HardStop (bot-block, robots.txt,
    # network failure) with exponential backoff, rather than giving up
    # immediately — see cli.py's `run` command. None (the default — a bare
    # local/manual invocation never sets this) means today's exact
    # behavior: zero retries, a HardStop ends the run. The value is a total
    # budget in minutes, not a retry count: how many attempts fit is purely
    # a function of this number and the backoff (doubling from 1 minute).
    # Overridable per invocation with `run --retry-budget-minutes`, which
    # beats this when both are set (same precedence as log_file/--log).
    retry_budget_minutes: int | None = None

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        # Priority, highest first: explicit init kwargs > env vars > .env
        # file > config.yaml > field defaults.
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            _YamlConfigSource(settings_cls),
            file_secret_settings,
        )


def get_settings() -> Settings:
    return Settings()
