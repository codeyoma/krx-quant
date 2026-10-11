"""Settings from the environment: values the engine cannot work without are refused."""

from __future__ import annotations

from datetime import time

import pytest

from krx_quant.config import Settings, SettingsError

KEYS = {"KIS_APP_KEY": "key", "KIS_APP_SECRET": "secret"}


def test_the_index_codes_must_keep_kospi_for_the_circuit_breaker() -> None:
    with pytest.raises(SettingsError, match="KRX_INDEX_CODES.*0001"):
        Settings.from_env({**KEYS, "KRX_INDEX_CODES": "2001,0013"}, dotenv_path=None)

    assert "0001" in Settings.from_env({**KEYS, "KRX_INDEX_CODES": "2001,0001"}, dotenv_path=None).index_codes
    assert "0001" in Settings.from_env(KEYS, dotenv_path=None).index_codes


def test_nightly_training_starts_at_21_30_unless_moved_or_off() -> None:
    assert Settings.from_env(KEYS, dotenv_path=None).train_at == time(21, 30)
    assert Settings.from_env({**KEYS, "KRX_TRAIN_AT": "22:15"}, dotenv_path=None).train_at == time(22, 15)
    assert Settings.from_env({**KEYS, "KRX_TRAIN_AT": "off"}, dotenv_path=None).train_at is None
    with pytest.raises(SettingsError, match="KRX_TRAIN_AT"):
        Settings.from_env({**KEYS, "KRX_TRAIN_AT": "late"}, dotenv_path=None)


def test_the_macro_indicators_are_read_at_07_30_unless_moved_or_off() -> None:
    assert Settings.from_env(KEYS, dotenv_path=None).macro_at == time(7, 30)
    assert Settings.from_env({**KEYS, "KRX_MACRO_AT": "off"}, dotenv_path=None).macro_at is None
    settings = Settings.from_env(
        {**KEYS, "KRX_ECOS_API_KEY": "e", "KRX_REGIME_SHOCK_VIX": "25"}, dotenv_path=None
    )
    assert (settings.ecos_api_key, settings.fred_api_key) == ("e", None), "a source without a key is skipped"
    assert settings.regime_rules.shock_vix == 25.0
    assert settings.regime_rules.shock_sox_pct == 3.0


def test_a_cost_budget_needs_the_schedules_it_turns_off() -> None:
    with pytest.raises(SettingsError, match="KRX_TRAINING_SCHEDULES"):
        Settings.from_env({**KEYS, "KRX_COST_BUDGET": "krx-quant-monthly"}, dotenv_path=None)
    both = {**KEYS, "KRX_COST_BUDGET": "krx-quant-monthly", "KRX_TRAINING_SCHEDULES": "a,b"}
    assert Settings.from_env(both, dotenv_path=None).training_schedules == ("a", "b")
