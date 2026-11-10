"""The trader's own settings (#394), read from the environment and ``.env`` beside the engine's.

They are read by the trader alone (``krx-quant trade``): the collector never reads them, so a
wrong one stops the trader, never the recording. Every limit is the code's at most: ``.env`` may
narrow the capital, never widen it (#393).

- ``KRX_TRADER_MODE``: ``paper`` (the default), ``mock`` (#405: the champion orders on KIS's mock
  server with the trader's own key, ``KRX_TRADER_KIS_*``, which the trader checks as it starts,
  ``kis_orders.trader_broker``) or ``live`` (#408: the champion orders on the live account with the
  live key, ``KRX_TRADER_LIVE_KIS_*``, ``kis_orders.trader_live_broker``, once the live gate and the
  execution checks pass; until then it runs as paper, ``live.py``);
- ``KRX_TRADER_THRESHOLD``: θ, the rise's probability that buys (0.6 unless set);
- ``KRX_TRADER_CAPITAL``: the capital in won (1,000,000 unless set; at most that);
- ``KRX_TRADER_DAILY_LOSS_PERCENT``, ``KRX_TRADER_CUMULATIVE_LOSS_PERCENT`` (#396): the loss
  limits in percent of the capital (10 and 20 unless set; above 0 and at most those);
- ``KRX_TRADER_RISK_PER_TRADE_PERCENT`` (#471): a trade's loss budget in percent of the capital it
  trades on (0.5 unless set; above 0 and at most that), which sizes each buy (``money.size``);
- ``KRX_TRADER_LOSS_STREAK``, ``KRX_TRADER_COOLDOWN_MINUTES`` (#471): the net losses in a row (3
  unless set; from 1 to 3) that stop new buys for the cooldown (30 minutes unless set; at least 30);
- ``KRX_TRADER_DAILY_BUY_ORDERS`` (#471): the buy orders a day, those refused counted (20 unless set;
  from 1 to 20). The risk rules, ``book.risk_held``: ``.env`` may make them stricter, never looser;
- ``KRX_TRADER_QUOTES_URL``: the collector's quote stream (``quotes.py``); compose sets it to the
  engine service's;
- ``KRX_TRADER_ENGINE_URL``: the Engine API the cost map is read from (``selection.py``, #395);
  compose sets it to the engine service's, inside the compose network;
- ``KRX_TRADER_MOCK_PRACTICE`` (#427): ``off`` (the default) or ``on``: on a day no champion cell
  trades, the mock book practises one cell, a share at a time (``desk.Desk``), the cell and its θ
  frozen for each review cycle by the nightly job (#474, ``practice.py``; it chooses none while off). For
  ``KRX_TRADER_MODE=mock`` alone: ``on`` with ``paper`` or ``live`` is a configuration error, checked
  here, before a live trader falls back to paper (``run.py``);
- ``KRX_TRADER_MOCK_PRACTICE_SPACING_MINUTES`` (#598): the least minutes between two practice buys,
  from the bar close of the day's last practice buy order to the next one's (15 unless set; from 0,
  which spaces none: ``book.spaced``). The practice's pace, no risk rule: ``.env`` may set it either
  way. Read in every mode, used with the practice alone; the verdict policy's buys are spaced by it
  too (#677).
- ``KRX_TRADER_MOCK_VERDICT`` (#677): ``on`` or ``off``; unset, ``on`` in mock mode and ``off`` in the
  others. On a day no champion cell trades, the mock book trades the engine's verdict too
  (``verdicts.py``), sized by the loss budget. For ``KRX_TRADER_MODE=mock`` alone: ``on`` with ``paper``
  or ``live`` is a configuration error, as the practice's. ``off`` narrows what the trader does.

``KRX_TRADER_KIND`` and ``KRX_TRADER_PREDICTOR`` (#394: the one cell traded) are gone: the cost map
picks the cells (#395). One still set is not read, and since #644 nothing says so: in the 14 days
before 2026-11-09 the trader's warning for them was logged by no start.
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
    COOLDOWN,
    CUMULATIVE_LOSS_PERCENT,
    DAILY_BUY_ORDERS,
    DAILY_LOSS_PERCENT,
    LOSS_STREAK,
    MOCK,
    MODES,
    PAPER,
    PRACTICE_SPACING,
    RISK_PER_TRADE_PERCENT,
    THRESHOLD,
)

DEFAULT_QUOTES_URL = "ws://127.0.0.1:8081/v1/quotes"
DEFAULT_ENGINE_URL = "http://127.0.0.1:8080"
_HOST = re.compile(r"^https?://[^/\s]+$")  # a scheme and a host, with its port: no path
# KRX_TRADER_MOCK_PRACTICE's values (#427).
ON, OFF = "on", "off"


@dataclass(frozen=True)
class TraderSettings:
    mode: str = PAPER
    threshold: float = THRESHOLD
    capital: int = CAPITAL
    daily_loss_percent: float = DAILY_LOSS_PERCENT
    cumulative_loss_percent: float = CUMULATIVE_LOSS_PERCENT
    # The risk rules (#471): a trade's loss budget, the losses in a row and the cooldown they start,
    # the buy orders a day.
    risk_per_trade_percent: float = RISK_PER_TRADE_PERCENT
    loss_streak: int = LOSS_STREAK
    cooldown_minutes: float = COOLDOWN.total_seconds() / 60
    daily_buy_orders: int = DAILY_BUY_ORDERS
    quotes_url: str = DEFAULT_QUOTES_URL
    engine_url: str = DEFAULT_ENGINE_URL
    poll_seconds: float = 0.5  # how often the engine's database is read for new predictions
    # The mock book practises a cell on a day no champion cell trades (#427, ``desk.Desk``), its buys
    # this many minutes apart at least (#598).
    mock_practice: bool = False
    practice_spacing_minutes: float = PRACTICE_SPACING.total_seconds() / 60
    # The mock book trades the engine's verdict on a day no champion cell trades (#677, ``verdicts.py``).
    mock_verdict: bool = False

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None, dotenv_path: Path | None = Path(".env")
    ) -> TraderSettings:
        values = read_values(env, dotenv_path)
        mode = values.get("KRX_TRADER_MODE", PAPER).strip().lower() or PAPER
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
        risk = _limit(values, "KRX_TRADER_RISK_PER_TRADE_PERCENT", RISK_PER_TRADE_PERCENT)
        streak = _fewer(values, "KRX_TRADER_LOSS_STREAK", LOSS_STREAK, "net losses in a row")
        orders = _fewer(values, "KRX_TRADER_DAILY_BUY_ORDERS", DAILY_BUY_ORDERS, "buy orders a day")
        cooldown = float(
            _number(
                values, "KRX_TRADER_COOLDOWN_MINUTES", f"{COOLDOWN.total_seconds() / 60:g}", float, 0.0, None
            )
        )
        if cooldown < COOLDOWN.total_seconds() / 60:
            raise SettingsError(
                f"KRX_TRADER_COOLDOWN_MINUTES ({cooldown:g}) is below the trader's cooldown of"
                f" {COOLDOWN.total_seconds() / 60:g} minutes: .env may lengthen it, never shorten it"
            )
        quotes_url = values.get("KRX_TRADER_QUOTES_URL", DEFAULT_QUOTES_URL).strip() or DEFAULT_QUOTES_URL
        if not quotes_url.startswith(("ws://", "wss://")):
            raise SettingsError(f"KRX_TRADER_QUOTES_URL must be a ws:// URL, got {quotes_url!r}")
        engine_url = values.get("KRX_TRADER_ENGINE_URL", DEFAULT_ENGINE_URL).strip().rstrip("/")
        engine_url = engine_url or DEFAULT_ENGINE_URL
        if not _HOST.fullmatch(engine_url):
            raise SettingsError(
                f"KRX_TRADER_ENGINE_URL must be http://host:port, without a path, got {engine_url!r}"
            )
        practice = values.get("KRX_TRADER_MOCK_PRACTICE", OFF).strip().lower() or OFF
        if practice not in (ON, OFF):
            raise SettingsError(f"KRX_TRADER_MOCK_PRACTICE must be on or off, got {practice!r}")
        if practice == ON and mode != MOCK:
            # Before a live trader falls back to paper (#408): the setting is wrong whatever it runs as.
            raise SettingsError(
                f"KRX_TRADER_MOCK_PRACTICE=on is for KRX_TRADER_MODE=mock alone, not {mode} (#427): the"
                " practice trades order on KIS's mock server only"
            )
        verdict = values.get("KRX_TRADER_MOCK_VERDICT", "").strip().lower() or (ON if mode == MOCK else OFF)
        if verdict not in (ON, OFF):
            raise SettingsError(f"KRX_TRADER_MOCK_VERDICT must be on or off, got {verdict!r}")
        if verdict == ON and mode != MOCK:
            raise SettingsError(
                f"KRX_TRADER_MOCK_VERDICT=on is for KRX_TRADER_MODE=mock alone, not {mode} (#677): the"
                " verdict's trades order on KIS's mock server only"
            )
        spacing = float(
            _number(
                values,
                "KRX_TRADER_MOCK_PRACTICE_SPACING_MINUTES",
                f"{PRACTICE_SPACING.total_seconds() / 60:g}",
                float,
                0.0,
                None,
            )
        )
        return cls(
            mode=mode,
            threshold=float(threshold),
            capital=capital,
            daily_loss_percent=daily,
            cumulative_loss_percent=cumulative,
            risk_per_trade_percent=risk,
            loss_streak=streak,
            cooldown_minutes=cooldown,
            daily_buy_orders=orders,
            quotes_url=quotes_url,
            engine_url=engine_url,
            mock_practice=practice == ON,
            practice_spacing_minutes=spacing,
            mock_verdict=verdict == ON,
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


def _fewer(values: Mapping[str, str], name: str, code: int, what: str) -> int:
    """A count of the risk rules (#471): from 1, and at most the code's (``.env`` makes a rule
    stricter, never looser)."""
    value = int(_number(values, name, str(code), int, 1, None))
    if value > code:
        raise SettingsError(
            f"{name} ({value}) is above the trader's {code} {what}: .env may lower it, never raise it"
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
