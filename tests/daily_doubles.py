"""Made-up histories for the daily kinds (#264): KRX day candles, integrated day candles, US
daily closes and KIS daily supply, written as the collector stores them.

``made_history`` writes about ``days`` trading days per symbol ending on ``last``. With
``signal`` the day's open→close leans against the night's SOX move (the US shock given back
during the day) and with the previous day's foreign net buys: a model that reads them can beat
the baselines; without it every day is noise.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from krx_quant.candles import write_candles, write_krx_days
from krx_quant.macro import Observation
from krx_quant.market_calendar import MarketCalendar
from krx_quant.supply import KIS, NET_MARKET, NXT_OPENED, write_supply
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
    return Made(krx, us_days)
