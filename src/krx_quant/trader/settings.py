"""The trader's own settings (#394), read from the environment and ``.env`` beside the engine's.

They are read by the trader alone (``krx-quant trade``): the collector never reads them, so a
wrong one stops the trader, never the recording. Every limit is the code's at most: ``.env`` may
narrow the capital, never widen it (#393).

- ``KRX_TRADER_MODE``: ``paper`` (the default and, for now, the only one; ``mock`` and ``live``
  come with the KIS order module, #404, #405, #408);
- ``KRX_TRADER_KIND``, ``KRX_TRADER_PREDICTOR``: the cell traded (``direction`` and
  ``consensus-final`` unless set); the kind one of ``TRADED_KINDS``;
- ``KRX_TRADER_THRESHOLD``: θ, the rise's probability that buys (0.6 unless set);
- ``KRX_TRADER_CAPITAL``: the capital in won (1,000,000 unless set; at most that);
- ``KRX_TRADER_QUOTES_URL``: the collector's quote stream (``quotes.py``); compose sets it to the
  engine service's.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from krx_quant.config import SettingsError, read_values
from krx_quant.consensus import FINAL
from krx_quant.trader import CAPITAL, LATER_MODES, MODES, PAPER, THRESHOLD, TRADED_KINDS
from krx_quant.trader.cells import Cell

DEFAULT_QUOTES_URL = "ws://127.0.0.1:8081/v1/quotes"
_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")  # an algorithm's, a baseline's or a consensus's name


@dataclass(frozen=True)
class TraderSettings:
    mode: str = PAPER
    cell: Cell = Cell("direction", FINAL)
    threshold: float = THRESHOLD
    capital: int = CAPITAL
    quotes_url: str = DEFAULT_QUOTES_URL
    poll_seconds: float = 0.5  # how often the engine's database is read for new predictions

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
        kind = values.get("KRX_TRADER_KIND", "direction").strip().lower() or "direction"
        if kind not in TRADED_KINDS:
            raise SettingsError(f"KRX_TRADER_KIND must be one of {', '.join(TRADED_KINDS)}, got {kind!r}")
        predictor = values.get("KRX_TRADER_PREDICTOR", FINAL).strip() or FINAL
        if not _NAME.fullmatch(predictor):
            raise SettingsError(f"KRX_TRADER_PREDICTOR must be a predictor's name, got {predictor!r}")
        threshold = _number(values, "KRX_TRADER_THRESHOLD", str(THRESHOLD), float, 0.0, 1.0)
        capital = int(_number(values, "KRX_TRADER_CAPITAL", str(CAPITAL), int, 1, None))
        if capital > CAPITAL:
            raise SettingsError(
                f"KRX_TRADER_CAPITAL ({capital:,}) is above the trader's limit of {CAPITAL:,} won: "
                ".env may narrow it, never widen it"
            )
        quotes_url = values.get("KRX_TRADER_QUOTES_URL", DEFAULT_QUOTES_URL).strip() or DEFAULT_QUOTES_URL
        if not quotes_url.startswith(("ws://", "wss://")):
            raise SettingsError(f"KRX_TRADER_QUOTES_URL must be a ws:// URL, got {quotes_url!r}")
        return cls(
            mode=mode,
            cell=Cell(kind, predictor),
            threshold=float(threshold),
            capital=capital,
            quotes_url=quotes_url,
        )


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
