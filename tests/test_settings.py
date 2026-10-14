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


def test_the_target_is_the_touch_target_unless_the_four_minute_move_is_chosen() -> None:
    # The default since #170: the deploy is the switch.
    assert Settings.from_env(KEYS, dotenv_path=None).target_version == 2
    assert Settings.from_env({**KEYS, "KRX_TARGET_VERSION": "1"}, dotenv_path=None).target_version == 1
    for bad in ("3", "0", "two", ""):
        with pytest.raises(SettingsError, match="KRX_TARGET_VERSION"):
            Settings.from_env({**KEYS, "KRX_TARGET_VERSION": bad}, dotenv_path=None)


def test_the_commit_running_is_the_deploy_s_or_unknown() -> None:
    assert Settings.from_env(KEYS, dotenv_path=None).commit == "unknown"
    assert Settings.from_env({**KEYS, "KRX_COMMIT": "0123abc"}, dotenv_path=None).commit == "0123abc"


def test_the_statuses_are_judged_on_rising_days_at_a_level_below_one() -> None:
    defaults = Settings.from_env(KEYS, dotenv_path=None)
    assert (defaults.probation_review_days, defaults.bench_review_days) == ((10, 20, 40), (20, 40, 60))
    assert defaults.review_confidence == 0.983
    chosen = Settings.from_env({**KEYS, "KRX_PROBATION_REVIEW_DAYS": "15, 30"}, dotenv_path=None)
    assert chosen.probation_review_days == (15, 30)
    for name in ("KRX_PROBATION_REVIEW_DAYS", "KRX_BENCH_REVIEW_DAYS"):
        for bad in ("", "20,10", "0,10", "10,10", "ten", "10,401"):
            with pytest.raises(SettingsError, match=name):
                Settings.from_env({**KEYS, name: bad}, dotenv_path=None)
    for bad in ("0.4", "1", "nan", "inf"):
        with pytest.raises(SettingsError, match="KRX_REVIEW_CONFIDENCE"):
            Settings.from_env({**KEYS, "KRX_REVIEW_CONFIDENCE": bad}, dotenv_path=None)


def test_one_algorithms_limit_is_below_the_cpu_budget() -> None:
    # One algorithm at its limit must leave the others room in the budget.
    defaults = Settings.from_env(KEYS, dotenv_path=None)
    assert (defaults.algorithm_seconds, defaults.cpu_budget_seconds) == (10.0, 30.0)
    for limit, budget in (("30", "30"), ("40", "30")):
        with pytest.raises(SettingsError, match="KRX_ALGORITHM_SECONDS"):
            Settings.from_env(
                {**KEYS, "KRX_ALGORITHM_SECONDS": limit, "KRX_CPU_BUDGET_SECONDS": budget}, dotenv_path=None
            )
