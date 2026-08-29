"""Tests for config.py's -1-means-unlimited normalization — see
normalize_unlimited's own docstring. BaseScraper itself already treats
None as unlimited (tested in test_base_scraper.py); normalize_unlimited
converts -1 to that None, but deliberately ONLY when called on a fully
resolved value (see cli.py's `run`), never as a pydantic field validator
on SourceSettings/Settings — -1 must stay a real, distinguishable value
through the CLI-flag > per-source > global precedence chain, or it loses
to a lower-priority level's real value instead of winning as "unlimited"
(a real bug caught this session — see cli.py's own comments at both
`max_new`/`effective_max` resolution sites)."""

from qara_reg_scraper.config import Settings, SourceSettings, normalize_unlimited


def test_normalize_unlimited_converts_minus_one_to_none():
    assert normalize_unlimited(-1) is None


def test_normalize_unlimited_passes_other_values_through():
    assert normalize_unlimited(5) == 5
    assert normalize_unlimited(0) == 0
    assert normalize_unlimited(None) is None


def test_source_settings_minus_one_is_not_normalized_at_the_field_level():
    # -1 must survive as a literal -1 here — normalize_unlimited is applied
    # by cli.py AFTER precedence resolution, not by a pydantic validator on
    # this field (see the module docstring above for why).
    assert SourceSettings(max_new_documents_per_run=-1).max_new_documents_per_run == -1
    assert SourceSettings().max_new_documents_per_run is None


def test_global_settings_minus_one_is_not_normalized_at_the_field_level():
    settings = Settings(max_new_documents_per_run=-1)
    assert settings.max_new_documents_per_run == -1


def test_retry_budget_minutes_defaults_to_none_unless_set():
    assert Settings(max_new_documents_per_run=5).retry_budget_minutes is None
    assert Settings(max_new_documents_per_run=5, retry_budget_minutes=30).retry_budget_minutes == 30
