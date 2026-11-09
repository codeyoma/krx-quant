"""Made-up histories for the daily kinds (#264): KRX day candles, integrated day candles, US
daily closes and KIS daily supply, written as the collector stores them.

``made_history`` writes about ``days`` trading days per symbol ending on ``last``. With
``signal`` the day's open→close leans against the night's SOX move (the US shock given back
during the day) and with the previous day's foreign net buys: a model that reads them can beat
the baselines; without it every day is noise.

``settings`` makes such a history ending on Friday 16 October and the settings that read it, and
``engine_on`` an engine on it that runs only the daily algorithms given, for the simulated
probation's tests and the scheduled jobs that run it (#298, #511).
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from krx_quant.auctions import KEEP_DAYS, MINUTE_BAR, write_auctions
from krx_quant.candles import write_candles, write_krx_days
from krx_quant.clock import FakeClock
from krx_quant.config import Settings
from krx_quant.engine import PredictionEngine
from krx_quant.features import FeatureVector
from krx_quant.feeds.base import KST
from krx_quant.kinds import DIRECTION_KIND
from krx_quant.macro import Observation
from krx_quant.market_calendar import MarketCalendar
from krx_quant.predictions import DB_NAME, PredictionStore
from krx_quant.supply import KIS, NET_MARKET, NXT_OPENED, write_supply
from krx_quant.touch import TARGET_VERSION
from krx_quant.us_daily import write_us_daily

SYMBOLS = ("005930", "000660", "009150")


@dataclass(frozen=True)
class Made:
    days: list[date]  # the KRX trading days, oldest first
    us_days: list[date]  # the US sessions (exchange dates)


def trading_days(calendar: MarketCalendar, last: date, n: int) -> list[date]:
    out: list[date] = []
    day = last
    while len(out) < n:
        if calendar.is_trading_day(day):
            out.append(day)
        day -= timedelta(days=1)
    return sorted(out)


def made_history(
    data_dir: Path,
    state_dir: Path,
    last: date,
    days: int,
    *,
    signal: float = 0.0,
    seed: int = 264,
    symbols: Sequence[str] = SYMBOLS,
    calendar: MarketCalendar | None = None,
    krx_history: bool = True,
    auctions: bool = True,
) -> Made:
    calendar = calendar or MarketCalendar()
    krx = trading_days(calendar, last, days)
    rng = random.Random(seed)
    # A US session on every weekday up to the evening before the last KRX day (Seoul's morning).
    us_days = [
        d
        for d in (krx[0] - timedelta(days=10) + timedelta(days=i) for i in range((last - krx[0]).days + 10))
        if d.weekday() < 5
    ]
    closes = {name: 100.0 for name in ("sox", "nvda", "mu", "vix")}
    us: dict[str, list[Observation]] = {name: [] for name in closes}
    moves: dict[date, float] = {}
    for d in us_days:
        move = rng.gauss(0, 0.02)
        moves[d] = move
        closes["sox"] *= 1 + move
        closes["nvda"] *= 1 + move + rng.gauss(0, 0.01)
        closes["mu"] *= 1 + move + rng.gauss(0, 0.01)
        closes["vix"] = max(9.0, 20 + rng.gauss(0, 3))
        for name, value in closes.items():
            us[name].append(Observation(d, value))
    for name, observations in us.items():
        write_us_daily(data_dir, name, observations)
    for k, symbol in enumerate(symbols):
        price = 50_000.0 * (k + 1)
        krx_rows, un_rows, supply = [], [], []
        foreign_yesterday = 0.0
        previous_day = krx[0] - timedelta(days=1)
        for d in krx:
            night = sum(m for u, m in moves.items() if previous_day <= u < d)
            opened = price * (1 + 0.8 * night + rng.gauss(0, 0.005))
            lean = signal * (-0.6 * night + 0.004 * foreign_yesterday)
            change = lean + rng.gauss(0, 0.012)
            close = opened * (1 + change)
            high, low = max(opened, close) * 1.003, min(opened, close) * 0.997
            volume = 1_000_000 + rng.randrange(100_000)
            foreign = rng.gauss(0, 1)
            row = {
                "symbol": symbol,
                "date": d,
                "open": round(opened),
                "high": round(high),
                "low": round(low),
                "close": round(close),
                "volume": volume,
                "value": round(volume * close),
            }
            krx_rows.append({**row, "venue": "KRX"})
            un_rows.append({**row, "venue": "UN", "open": round(opened * 1.001), "volume": volume * 2})
            supply.append(
                {
                    "date": d,
                    "foreign_net": round(foreign * volume * 0.02),
                    "institution_net": round(rng.gauss(0, 1) * volume * 0.02),
                    "individual_net": 0,
                    # The market the keeper asked (#297); without the KRX history (a backfill
                    # not in yet) the days before NXT opened have none.
                    NET_MARKET: "UN" if d >= NXT_OPENED else ("J" if krx_history else None),
                }
            )
            foreign_yesterday = foreign
            price, previous_day = close, d
        write_krx_days(data_dir, symbol, krx_rows)
        write_candles(data_dir, "day", symbol, un_rows)
        write_supply(state_dir, symbol, KIS, supply)
        if auctions:
            # The auction keeper's last year (#264): KRX's minute bars of the auctions, here the
            # candle's open and close (so the made-up days' answers are the candles').
            kept: dict[date, dict[str, Any]] = {
                d: {"symbol": symbol, "date": d, "open": r["open"], "close": r["close"], "source": MINUTE_BAR}
                for d, r in zip(krx, krx_rows, strict=True)
                if d > last - KEEP_DAYS
            }
            write_auctions(data_dir, symbol, kept)
    return Made(krx, us_days)


FEE = 0.015 / 100
LAST = date(2026, 10, 16)  # a Friday: the last made-up KRX day
NIGHT = datetime(2026, 10, 16, 23, 0, tzinfo=KST)  # that night's scheduled jobs
CALENDAR = MarketCalendar()


def settings(tmp_path: Path, signal: float, days: int = 420, seed: int = 298) -> Settings:
    made_history(tmp_path / "data", tmp_path / "state", LAST, days, signal=signal, seed=seed)
    return Settings(
        kis_app_key="",
        kis_app_secret="",
        symbols=SYMBOLS,
        etf_symbols=(),
        index_codes=(),
        us_symbols=(),
        data_dir=tmp_path / "data",
        state_dir=tmp_path / "state",
    )


def store_of(s: Settings) -> PredictionStore:
    return PredictionStore(s.state_dir / DB_NAME)


class Source:
    def days(self, symbol: str) -> list[Any]:
        return []


def no_features(algorithm: Any, symbol: str, day: date, past: Sequence[Any], band: Any) -> FeatureVector:
    return FeatureVector({}, ())


def engine_on(s: Settings, clock: FakeClock, algorithms: Sequence[Any]) -> PredictionEngine:
    return PredictionEngine(
        store_of(s),
        CALENDAR,
        SYMBOLS,
        FEE,
        clock,
        algorithms=(),
        kinds=(DIRECTION_KIND,),
        kind_algorithms=(),
        daily=Source(),
        daily_algorithms=algorithms,
        daily_features=no_features,
        daily_feature_names={"us_sox"},
        target_version=TARGET_VERSION,
    )
