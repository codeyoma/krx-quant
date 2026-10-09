"""Settings from the environment: values the engine cannot work without are refused."""

from __future__ import annotations

import pytest

from krx_quant.config import Settings, SettingsError

KEYS = {"KIS_APP_KEY": "key", "KIS_APP_SECRET": "secret"}


def test_the_index_codes_must_keep_kospi_for_the_circuit_breaker() -> None:
    with pytest.raises(SettingsError, match="KRX_INDEX_CODES.*0001"):
        Settings.from_env({**KEYS, "KRX_INDEX_CODES": "2001,0013"}, dotenv_path=None)

    assert "0001" in Settings.from_env({**KEYS, "KRX_INDEX_CODES": "2001,0001"}, dotenv_path=None).index_codes
    assert "0001" in Settings.from_env(KEYS, dotenv_path=None).index_codes
