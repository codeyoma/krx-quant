"""The trader's own settings (#394), read from the environment and ``.env`` beside the engine's.

They are read by the trader alone (``krx-quant trade``): the collector never reads them, so a
wrong one stops the trader, never the recording. Every limit is the code's at most: ``.env`` may
narrow the capital, never widen it (#393).

- ``KRX_TRADER_MODE``: ``paper`` (the default and, for now, the only one; ``mock`` and ``live``
  come with the KIS order module, #404, #405, #408);
- ``KRX_TRADER_THRESHOLD``: θ, the rise's probability that buys (0.6 unless set);
- ``KRX_TRADER_CAPITAL``: the capital in won (1,000,000 unless set; at most that);
- ``KRX_TRADER_DAILY_LOSS_PERCENT``, ``KRX_TRADER_CUMULATIVE_LOSS_PERCENT`` (#396): the loss
  limits in percent of the capital (10 and 20 unless set; above 0 and at most those);
- ``KRX_TRADER_QUOTES_URL``: the collector's quote stream (``quotes.py``); compose sets it to the
  engine service's;
- ``KRX_TRADER_ENGINE_URL``: the Engine API the cost map is read from (``selection.py``, #395);
  compose sets it to the engine service's, inside the compose network.

``KRX_TRADER_KIND`` and ``KRX_TRADER_PREDICTOR`` (#394: the one cell traded) are gone: the cost map
picks the cells (#395). One still set is not read; the trader says so at its start (``ignored``).
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from krx_quant.config import SettingsError, read_values
from krx_quant.trader import (
    CAPITAL,
    CUMULATIVE_LOSS_PERCENT,
    DAILY_LOSS_PERCENT,
    LATER_MODES,
    MODES,
    PAPER,
    THRESHOLD,
)

DEFAULT_QUOTES_URL = "ws://127.0.0.1:8081/v1/quotes"
DEFAULT_ENGINE_URL = "http://127.0.0.1:8080"
# Settings no longer read (#395: the cost map picks the cells).
GONE = ("KRX_TRADER_KIND", "KRX_TRADER_PREDICTOR")
_HOST = re.compile(r"^https?://[^/\s]+$")  # a scheme and a host, with its port: no path


@dataclass(frozen=True)
class TraderSettings:
    mode: str = PAPER
    threshold: float = THRESHOLD
    capital: int = CAPITAL
    daily_loss_percent: float = DAILY_LOSS_PERCENT
    cumulative_loss_percent: float = CUMULATIVE_LOSS_PERCENT
    quotes_url: str = DEFAULT_QUOTES_URL
    engine_url: str = DEFAULT_ENGINE_URL
    poll_seconds: float = 0.5  # how often the engine's database is read for new predictions
    ignored: tuple[str, ...] = ()  # the settings found that are no longer read (``GONE``)

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None, dotenv_path: Path | None = Path(".env")
    ) -> TraderSettings:
        values = read_values(env, dotenv_path)
        mode = values.get("KRX_TRADER_MODE", PAPER).strip().lower() or PAPER
        if mode in LATER_MODES:
            raise SettingsError(
                f"KRX_TRADER_MODE {mode} is not built yet: the trader has no order code until the KIS "
                "order module (#404); set paper or remove it"
            )
        if mode not in MODES:
            raise SettingsError(f"KRX_TRADER_MODE must be one of {', '.join(MODES)}, got {mode!r}")
        threshold = _number(values, "KRX_TRADER_THRESHOLD", str(THRESHOLD), float, 0.0, 1.0)
        capital = int(_number(values, "KRX_TRADER_CAPITAL", str(CAPITAL), int, 1, None))
        if capital > CAPITAL:
            raise SettingsError(
                f"KRX_TRADER_CAPITAL ({capital:,}) is above the trader's limit of {CAPITAL:,} won: "
                ".env may narrow it, never widen it"
            )
        daily = _limit(values, "KRX_TRADER_DAILY_LOSS_PERCENT", DAILY_LOSS_PERCENT)
        cumulative = _limit(values, "KRX_TRADER_CUMULATIVE_LOSS_PERCENT", CUMULATIVE_LOSS_PERCENT)
        quotes_url = values.get("KRX_TRADER_QUOTES_URL", DEFAULT_QUOTES_URL).strip() or DEFAULT_QUOTES_URL
        if not quotes_url.startswith(("ws://", "wss://")):
            raise SettingsError(f"KRX_TRADER_QUOTES_URL must be a ws:// URL, got {quotes_url!r}")
        engine_url = values.get("KRX_TRADER_ENGINE_URL", DEFAULT_ENGINE_URL).strip().rstrip("/")
        engine_url = engine_url or DEFAULT_ENGINE_URL
        if not _HOST.fullmatch(engine_url):
            raise SettingsError(
                f"KRX_TRADER_ENGINE_URL must be http://host:port, without a path, got {engine_url!r}"
            )
        return cls(
            mode=mode,
            threshold=float(threshold),
            capital=capital,
            daily_loss_percent=daily,
            cumulative_loss_percent=cumulative,
            quotes_url=quotes_url,
            engine_url=engine_url,
            ignored=tuple(name for name in GONE if values.get(name, "").strip()),
        )


def _limit(values: Mapping[str, str], name: str, code: float) -> float:
    """A loss limit in percent of the capital: above 0, and at most the code's (#393: ``.env``
    narrows a limit, never widens it)."""
    value = float(_number(values, name, f"{code:g}", float, 0.0, None))
    if value <= 0:
        raise SettingsError(f"{name} must be above 0, got {value:g}: a limit of 0 stops every trade")
    if value > code:
        raise SettingsError(
            f"{name} ({value:g}%) is above the trader's limit of {code:g}% of the capital: "
            ".env may narrow it, never widen it"
        )
    return value


def _number(
    values: Mapping[str, str],
    name: str,
    default: str,
    kind: type[int] | type[float],
    minimum: float,
    maximum: float | None,
) -> int | float:
    raw = values.get(name, default).strip() or default
    try:
        value = kind(raw)
    except ValueError as e:
        raise SettingsError(f"{name} must be a number, got {raw!r}") from e
    if not math.isfinite(value) or value < minimum or (maximum is not None and value > maximum):
        bounds = f"from {minimum:g}" + (f" to {maximum:g}" if maximum is not None else "")
        raise SettingsError(f"{name} must be a number {bounds}, got {raw!r}")
    return value
