"""Demo engine: a made-up morning through the real engine, served by the real Engine API.

``krx-quant demo-engine`` is for building screens (the Fincept screen, #20) without the
server or KIS. It replays the same made-up events every time: three symbols wandering
from 09:00 to 10:30 on 2026-10-12, KOSPI and KOSPI200 futures ticks, and a VI on one
symbol, forecast and scored on the touch target as the server's default (#170: its four
baselines). The KOSPI falls 8.5% at 09:04, so a circuit breaker's first stage halts the
market from 09:06 to 09:36 (#236): nothing trades nor quotes then, and the minute bars skip
those 30 minutes. The VI is 000660's, from 09:40:10 to 09:42:20 (dynamic). The KOSPI200
futures do not fall: no sidecar. Every registered algorithm is set active, so the consensus
has members, the last night's training shows as applied (each algorithm with a made-up
validation), the morning's macro read decided risk_off, Newton has a model installed and
rolled back, and the KIS status is made up (one feed in a gap).
The clock stays at 10:30. Every answer is the same each run, except the CPU budget's
timings, which are measured.

Before the morning come the last 80 minutes (14:00–15:20) of the two trading days before
it, 2026-10-07 and 2026-10-08 (Friday the 9th is Hangul Day, closed), so a chart scrolls back into them
(#108): their predictions are in the database, and their one-minute bars are stored as
the backfill stores them, under ``<db stem>-data/bars/``. Each symbol also has two years of
made-up day candles up to the 8th (``candles/``), for the chart's day view (#111), and the
finished week, month and year candles made of them (#112), and made-up daily supply data on
the same days (``state/supply/``, #226): the 12th's is not out yet, as on the server that
morning. The cost map's period base rates (``state/base_rates.json``, #307) are made up too,
each symbol's four periods.

The daily kind ``open_to_close`` (#264) has KRX day candles of its own (``krx.parquet``, the
same days, an open a tick off the integrated one's) and, for its last 20 days up to the 8th,
the three baselines' and Cox's predictions at 08:50 and their answers at 15:45, Cox fitted on
the days before them. The morning's 08:50 predictions are in too, waiting for their answer.

With a pace (``demo-engine --pace SECONDS``), the morning is replayed at once only up to
10:00; the API opens there, and the rest goes on a minute every ``pace`` seconds, in steps
of 10 seconds, so the stream sends predictions, the consensus, outcomes and the bar being
made as they are (#21, #110). It ends at
10:30 as the unpaced demo does, with the same answers.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
import shutil
from collections import defaultdict
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, date, datetime, time, timedelta
from functools import partial
from pathlib import Path
from typing import Any, cast

import pyarrow as pa
import pyarrow.parquet as pq
from aiohttp import web

from krx_quant.algorithms import ALGORITHMS, DAILY_ALGORITHMS, Model
from krx_quant.api import create_app
from krx_quant.auctions import KEEP_DAYS, MINUTE_BAR, write_auctions
from krx_quant.backfill import BAR_SCHEMA, PreviousDay, stock_bars_path
from krx_quant.base_rates import write_base_rates
from krx_quant.candles import PERIODS, period_start, write_candles, write_krx_days
from krx_quant.clock import FakeClock
from krx_quant.daily import ANSWER_FROM, StoredDays
from krx_quant.daily_features import DAILY_FEATURES, LiveFeatures
from krx_quant.engine import Event, PredictionEngine
from krx_quant.feeds import (
    BOOK,
    FOREIGN_ESTIMATE,
    INDEX,
    KOSPI200_FUTURES,
    MEMBER,
    MEMBER_REST,
    NIGHT_FUTURES,
    PROGRAM,
    PROGRAM_REST,
    TRADE,
    USD_FUTURES,
    VI_STATUS,
    Feed,
    RestFeed,
)
from krx_quant.feeds.base import KST, Row, kst_day
from krx_quant.lifecycle import Status
from krx_quant.macro import (
    INDICATORS,
    MACRO,
    MacroDay,
    Observation,
    Reading,
    Source,
    day_regime,
    macro_recorder,
)
from krx_quant.market_calendar import MarketCalendar
from krx_quant.model_shelf import Shelf
from krx_quant.nightly import Night
from krx_quant.open_to_close import OPEN_TO_CLOSE
from krx_quant.predictions import DB_NAME, PredictionStore, iso
from krx_quant.recorder import ParquetRecorder
from krx_quant.regime import RegimeRules
from krx_quant.replay import replay, replay_into
from krx_quant.structure_check import PERIODS as BASE_RATE_PERIODS
from krx_quant.supply import KIS, NET_MARKET, NXT_OPENED, TOSS, write_supply
from krx_quant.swap import NightStatus, reports_by_algorithm
from krx_quant.touch import TARGET_VERSION
from krx_quant.us_daily import write_us_daily

DEMO_DAY = date(2026, 10, 12)
DEMO_SYMBOLS = ("005930", "000660", "009150")
# Their company names, as the collector reads them from Toss or the KIS masters (#250).
DEMO_NAMES = {"005930": "삼성전자", "000660": "SK하이닉스", "009150": "삼성전기"}
_START, _END = time(9, 0), time(10, 30)
_CANDLE_DAYS = 500  # about two years of trading days
# The trading days before the morning, with the minutes of each a chart scrolls back into.
PAST_DAYS = (date(2026, 10, 7), date(2026, 10, 8))
_PAST_FROM, _PAST_TO = time(14, 0), time(15, 20)
_CALENDAR = MarketCalendar(
    {d: True for d in PAST_DAYS}
    | {date(2026, 10, 9): False, date(2026, 10, 10): False, date(2026, 10, 11): False, DEMO_DAY: True}
)
# The morning's crash (#236): the KOSPI this far (%) below its wander from CRASH_FROM. No breaker
# is called in the first 5 minutes, so the minute held starts at 09:05 and the first stage is
# called at 09:06, halting trading 20 minutes and a 10-minute single-price auction after: HALT.
CRASH, CRASH_FROM = 8.5, time(9, 4)
HALT = (time(9, 6), time(9, 36))
_PACED_FROM = time(10, 0)  # a paced demo opens here and goes on a minute at a time…
_STEPS = 6  # …in steps of 10 seconds
_FEE = 0.00015
# Each symbol's mid-point at the open and its tick.
_OPENS = {"005930": (81_250, 100), "000660": (201_000, 500), "009150": (150_050, 100)}
# The overnight before the demo morning, each indicator's (day before, last day) values: SOX
# down 1.4% and the won 0.3% weaker, so risk_off. The last day is Friday the 9th for the US
# sources, Thursday the 8th for the Korean ones (ECOS, KIS): Seoul was closed on Hangul Day.
_OVERNIGHT = {
    "usdkrw": (1384.6, 1388.2),
    "ktb3y": (2.61, 2.63),
    "us10y": (4.12, 4.15),
    "wti": (71.0, 70.2),
    "sox": (12800.0, 12620.8),
    "vix": (17.2, 18.4),
    "nq": (25010.0, 24880.0),
    "nvda": (186.0, 183.1),
    "mu": (210.0, 205.4),
    "usdf": (1388.0, 1392.2),
}
_USDF_CLOSE = _OVERNIGHT["usdf"][1]


def demo_events() -> list[Event]:
    """The demo morning's events, in receipt order."""
    events = [_crashed(e) for e in _session_events(_at(_START), _at(_END), seed=0) if not _halted(e)]
    # 000660 in a VI from 09:40:10, released at 09:42:20.
    events.append(_vi(_at(time(9, 40, 11)), "000660", "094010", None))
    events.append(_vi(_at(time(9, 42, 21)), "000660", "094010", "094220"))
    return sorted(events, key=lambda e: e[1]["received_at"])


def _halted(event: Event) -> bool:
    """Whether ``event`` is a trade or a quote during the morning's circuit breaker (``HALT``):
    none comes then. The walks go on unseen, so the market reopens where they are."""
    feed, row = event
    return feed in (TRADE.tr_id, BOOK.tr_id) and _at(HALT[0]) <= row["received_at"] < _at(HALT[1])


def _crashed(event: Event) -> Event:
    """``event``, a KOSPI tick from ``CRASH_FROM`` ``CRASH``% further down; any other as it is."""
    feed, row = event
    if feed != INDEX.tr_id or row["received_at"] < _at(CRASH_FROM):
        return event
    change = row["change_pct"] - CRASH
    return _index(row["received_at"].astimezone(KST), row["symbol"], 2600 * (1 + change / 100), change)


def past_events(day: date) -> list[Event]:
    """The last 80 minutes of an earlier trading day (``PAST_DAYS``), in receipt order."""
    seed = 10 * (1 + PAST_DAYS.index(day))
    return _session_events(datetime.combine(day, _PAST_FROM, KST), datetime.combine(day, _PAST_TO, KST), seed)


def _session_events(start: datetime, end: datetime, seed: int) -> list[Event]:
    """Each symbol wandering from its open (``seed`` picks the walk), with the index and the
    futures, from ``start`` to ``end``, in receipt order."""
    events: list[Event] = []
    for n, symbol in enumerate(DEMO_SYMBOLS):
        rng = random.Random(seed + n)
        mid, tick = _OPENS[symbol]
        when = start + timedelta(seconds=3 * n)
        while when <= end:
            mid += tick * rng.choice((-2, -1, 0, 0, 1, 2))
            events.append(_book(when, symbol, mid - tick // 2, mid + tick // 2))
            price = mid + rng.choice((-1, 1)) * (tick // 2)  # at the bid or the ask
            events.append(_trade(when + timedelta(seconds=1), symbol, price, rng.randint(1, 50)))
            when += timedelta(seconds=11)
    rng = random.Random(seed + len(DEMO_SYMBOLS))
    level = 0.0
    for when in _every(start, end, 5):
        level += rng.gauss(0, 0.02)
        events.append(_index(when, "0001", 2600 * (1 + level / 100), level))
        events.append(_futures(when + timedelta(seconds=2), 350 * (1 + level / 100), level))
        # The USD futures around Friday's close (the morning's macro reading).
        usd = _USDF_CLOSE * (1 + (level + 0.2) / 100)
        events.append(
            _futures(when + timedelta(seconds=3), usd, level + 0.2, feed=USD_FUTURES, contract="A75610")
        )
    return sorted(events, key=lambda e: e[1]["received_at"])


def write_past_bars(data_dir: Path, day: date, events: Sequence[Event]) -> None:
    """``day``'s one-minute bars of the demo symbols from its trades, stored as the backfill
    stores them (``backfill.py``), replacing earlier ones."""
    minutes: dict[str, dict[datetime, list[tuple[int, int]]]] = defaultdict(lambda: defaultdict(list))
    for tr_id, row in events:
        if tr_id == TRADE.tr_id:
            traded = row["received_at"].astimezone(KST)
            minutes[row["symbol"]][traded.replace(second=0, microsecond=0)].append((row["price"], row["qty"]))
    for symbol, bars in minutes.items():
        cum = 0
        rows = []
        for start in sorted(bars):
            prices = [p for p, _ in bars[start]]
            volume = sum(q for _, q in bars[start])
            cum += sum(p * q for p, q in bars[start])
            rows.append(
                {
                    "symbol": symbol,
                    "bar_time": start,
                    "venue": "UN",
                    "open": prices[0],
                    "high": max(prices),
                    "low": min(prices),
                    "close": prices[-1],
                    "volume": volume,
                    "cum_value": cum,
                }
            )
        path = stock_bars_path(data_dir, day, symbol)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")  # whole or absent, as the backfill writes
        pq.write_table(pa.Table.from_pylist(rows, schema=BAR_SCHEMA), tmp)
        tmp.rename(path)


def write_demo_candles(data_dir: Path) -> None:
    """Each demo symbol's made-up day candles, the last on the trading day before the morning
    and closing near its open, stored as the collector stores them (``candles.py``)."""
    shutil.rmtree(data_dir / "candles", ignore_errors=True)
    days: list[date] = []
    day = DEMO_DAY
    while len(days) < _CANDLE_DAYS:
        day = _CALENDAR.previous_trading_day(day)
        days.append(day)
    for n, symbol in enumerate(DEMO_SYMBOLS):
        rng = random.Random(100 + n)
        close, tick = _OPENS[symbol]
        rows = []
        for day in days:  # newest first, walking back
            open_ = max(tick, close + tick * round(close * rng.gauss(0, 0.012) / tick))
            high = max(open_, close) + tick * rng.randint(0, 8)
            low = min(open_, close) - tick * rng.randint(0, 8)
            volume = rng.randint(2_000_000, 20_000_000)
            rows.append(
                {
                    "symbol": symbol,
                    "date": day,
                    "venue": "UN",
                    "open": open_,
                    "high": high,
                    "low": max(tick, low),
                    "close": close,
                    "volume": volume,
                    "value": volume * close,
                }
            )
            close = max(tick, open_ + tick * round(open_ * rng.gauss(0, 0.004) / tick))  # the day before's
        oldest_first = list(reversed(rows))
        write_candles(data_dir, "day", symbol, oldest_first)
        # The KRX market's (#264): an opening auction a tick off NXT's pre-market open, now and then.
        krx = [
            {**r, "venue": "KRX", "open": max(tick, cast(int, r["open"]) - tick * (i % 3 - 1))}
            for i, r in enumerate(oldest_first)
        ]
        write_krx_days(data_dir, symbol, krx)
        # The auctions' prices the history task keeps (#264, the last year): the candle's here.
        newest = cast(date, krx[-1]["date"])
        kept: dict[date, dict[str, Any]] = {}
        for r in krx:
            day = cast(date, r["date"])
            if day > newest - KEEP_DAYS:
                kept[day] = {
                    "symbol": symbol,
                    "date": day,
                    "open": r["open"],
                    "close": r["close"],
                    "source": MINUTE_BAR,
                }
        write_auctions(data_dir, symbol, kept)
        for period in PERIODS:
            if period != "day":  # the finished ones, made of the days, as KIS's would be (#112)
                write_candles(data_dir, period, symbol, _finished_periods(oldest_first, period))


# Each demo symbol's made-up listed shares, foreign holdings at the first day and lending and
# credit balances around which its supply wanders (#226).
_SUPPLY = {
    "005930": (4_000_000_000, 2_000_000_000, 90_000_000, 12_000_000),
    "000660": (700_000_000, 380_000_000, 20_000_000, 3_000_000),
    "009150": (75_000_000, 26_000_000, 4_000_000, 1_500_000),
}


def write_demo_supply(state_dir: Path) -> None:
    """Each demo symbol's made-up daily supply data on the demo candles' days (up to the 8th),
    stored as the collector stores it (``supply.py``): KIS's fields in its file, the foreign
    holdings and ratio in Toss's. Made up: no Toss data is in the repository."""
    shutil.rmtree(state_dir / "supply", ignore_errors=True)
    days: list[date] = []
    day = DEMO_DAY
    while len(days) < _CANDLE_DAYS:
        day = _CALENDAR.previous_trading_day(day)
        days.append(day)
    days.reverse()
    for n, symbol in enumerate(DEMO_SYMBOLS):
        rng = random.Random(200 + n)
        listed, holding, lending, credit = _SUPPLY[symbol]
        scale = listed // 4000  # a day's typical net buy
        kis, toss = [], []
        for day in days:
            foreign = round(rng.gauss(0, 1) * scale)
            institution = round(rng.gauss(0, 0.8) * scale)
            individual = -foreign - institution + round(rng.gauss(0, 0.1) * scale)
            holding = min(listed, max(0, holding + foreign))
            lending = max(0, lending + round(rng.gauss(0, 0.02) * lending))
            credit = max(0, credit + round(rng.gauss(0, 0.015) * credit))
            kis.append(
                {
                    "date": day,
                    "short_volume": round(abs(rng.gauss(0.05, 0.03)) * scale * 10),
                    "individual_net": individual,
                    "institution_net": institution,
                    "foreign_net": foreign,
                    "lending_balance": lending,
                    "credit_balance": credit,
                    NET_MARKET: "J" if day < NXT_OPENED else "UN",  # as the keeper splits them
                }
            )
            toss.append(
                {"date": day, "foreign_holding": holding, "foreign_ratio": round(holding / listed * 100, 2)}
            )
        write_supply(state_dir, symbol, KIS, kis)
        write_supply(state_dir, symbol, TOSS, toss)


# Each demo symbol's made-up flat share and median k in the four base-rate periods (#307), in
# the order of ``structure_check.PERIODS``, and the trading days with bars in each.
_BASE_RATES = {
    "005930": ((0.893, 0.742, 0.615, 0.503), (2, 2, 2, 2)),
    "000660": ((0.709, 0.481, 0.312, 0.172), (5, 5, 1, 1)),
    "009150": ((0.621, 0.552, 0.474, 0.398), (3, 3, 4, 4)),
}
_BASE_RATE_DAYS = (238, 45, 31, 20)


def write_demo_base_rates(state_dir: Path) -> None:
    """Made-up period base rates (#307), kept as the collector keeps them (``base_rates.py``):
    each symbol's four periods, computed after the backfill of the evening before the morning.
    The up and down labels split the rest about evenly."""
    rows = []
    for symbol, (flats, ks) in _BASE_RATES.items():
        periods = zip(BASE_RATE_PERIODS, flats, ks, _BASE_RATE_DAYS, strict=True)
        for (name, first, end), flat, k, days in periods:
            windows = days * 375  # 09:01–15:15 a day
            up = round((1 - flat) * 0.52, 3)
            rows.append(
                {
                    "symbol": symbol,
                    "period": name,
                    "from": None if first is None else first.isoformat(),
                    "to": None if end is None else (end - timedelta(days=1)).isoformat(),
                    "days": days,
                    "windows": windows,
                    "up": up,
                    "flat": flat,
                    "down": round(1 - flat - up, 3),
                    "k": k,
                    "persist": round(min(0.95, flat + 0.04), 3),
                    "persist_windows": windows - 4 * days,  # a day's first four windows have none
                }
            )
    body = {
        "computed_at": "2026-10-11T20:30:41+09:00",
        "first_day": "2025-10-13",
        "last_day": "2026-10-08",
        "rows": rows,
    }
    write_base_rates(state_dir, body)


def _finished_periods(days: list[Row], period: str) -> list[Row]:
    """The candles of ``period`` made of day candles (oldest first), each dated by its first
    day, without the one holding the demo morning: as the collector stores them."""
    out: list[Row] = []
    for day in days:
        start = period_start(day["date"], period)
        if start >= period_start(DEMO_DAY, period):
            break
        if out and out[-1]["date"] == start:
            into = out[-1]
            into["high"] = max(into["high"], day["high"])
            into["low"] = min(into["low"], day["low"])
            into["close"] = day["close"]
            into["volume"] += day["volume"]
            into["value"] += day["value"]
        else:
            out.append({**day, "date": start})
    return out


async def demo_app(db: Path, pace: float | None = None) -> web.Application:
    """The Engine API on the demo morning, replayed into ``db`` (replaced), with the
    morning's macro read and the model history in ``<db stem>-data/`` (``macro/`` and
    ``state/models/``, replaced). Refuses the live engine's database name, so a mistyped
    path cannot replace it.

    With ``pace``, the API opens at 10:00 and the morning goes on to 10:30 a minute every
    ``pace`` seconds while it is served.
    """
    # Case-insensitive: on macOS and Windows ENGINE.SQLITE3 is the same file.
    if db.name.casefold() in {n.casefold() for n in (DB_NAME, f"{DB_NAME}-wal", f"{DB_NAME}-shm")}:
        raise ValueError(f"{db} is named like the live engine's database; the demo needs its own file")
    for leftover in (db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")):
        leftover.unlink(missing_ok=True)
    start = _at(time(0))
    store = PredictionStore(db)
    first = datetime.combine(PAST_DAYS[0], time(), KST)
    for a in ALGORITHMS:  # active from the first day, so the consensus has members
        store.add_status(a.name, first, None, Status.ACTIVE, "demo")
    store.mark_target(TARGET_VERSION, first)  # already on the touch target: no restart (#170)
    store.close()
    data_dir = db.with_name(f"{db.stem}-data")
    write_demo_macro(data_dir)  # first: Markov reads the morning's regime
    shutil.rmtree(data_dir / "bars", ignore_errors=True)
    write_demo_candles(data_dir)
    write_demo_supply(data_dir / "state")  # before the daily kind's days: Cox reads the flows
    write_demo_base_rates(data_dir / "state")  # the cost map's (#307)
    for day in PAST_DAYS:
        # Each day by its own engine, ended before the daily review after the close, so
        # every algorithm stays active as on the morning.
        past = past_events(day)
        write_past_bars(data_dir, day, past)
        engine = await replay(
            past,
            db=db,
            calendar=_CALENDAR,
            symbols=DEMO_SYMBOLS,
            fee_rate=_FEE,
            start=datetime.combine(day, _PAST_FROM, KST) - timedelta(minutes=1),
            until=datetime.combine(day, time(15, 30), KST),
            target_version=TARGET_VERSION,
            previous=PreviousDay(data_dir, _CALENDAR),  # range_prev_day, slot returns (#181, #265)
        )  # no macro read those mornings: no regime
        engine.store.close()
    cox = write_demo_daily(db, data_dir)  # the daily kind's last 20 days (#264)
    events = demo_events()
    engine = await replay(
        events,
        db=db,
        calendar=_CALENDAR,
        symbols=DEMO_SYMBOLS,
        fee_rate=_FEE,
        start=start,
        until=_at(_END if pace is None else _PACED_FROM),
        regimes=partial(day_regime, data_dir),
        target_version=TARGET_VERSION,  # the touch target, as the server's default (#170)
        previous=PreviousDay(data_dir, _CALENDAR),
        models={cox.version.split("-")[0]: cox},
        daily=StoredDays(data_dir),  # the morning's 08:50 predictions (#264)
        daily_algorithms=DAILY_ALGORITHMS,
        daily_features=LiveFeatures(
            OPEN_TO_CLOSE, data_dir, data_dir / "state", _CALENDAR, DEMO_SYMBOLS, _FEE
        ),
        daily_feature_names=DAILY_FEATURES,
    )
    write_demo_briefing(data_dir)  # after the morning: Cox's 08:50 inputs stay as they were
    write_demo_flow(data_dir)  # into the events the briefing's writer began (#306)
    night = Night(date(2026, 10, 9), "2026-10-09T21:31:40+09:00", {}, demo_reports())
    engine.training = NightStatus(night, True, "applied").as_dict()
    engine.night_reports = reports_by_algorithm(night)
    shelf = demo_shelf(data_dir / "state")
    app = create_app(
        engine,
        mode="demo",
        live_status=lambda: demo_kis(engine.clock.now()),
        data_dir=data_dir,
        shelf=shelf,
        names=DEMO_NAMES,
        state_dir=data_dir / "state",
    )
    if pace is not None:
        app.cleanup_ctx.append(partial(_paced, engine, events, pace))
    return app


def write_demo_briefing(data_dir: Path) -> None:
    """What the pre-open briefing (#304) reads of the morning beyond the replay, as the collector
    records it (``events/``, replaced): the KOSPI200 night session begun on the 8th (to 06:00 on
    the 9th), NXT's pre-market trades and the opening auction's expected prices on the 12th, and
    the 12th's economic calendar (``calendar/latest.json``) and two weeks of US closes
    (``us_daily/``, Friday the 9th's included: New York traded). Written after the morning is
    replayed, so the engine's 08:50 predictions read none of it: Cox's ``premarket_gap`` and
    ``night_futures`` show as missing. The 8th's bars end at 15:20, so there is no after-hours
    price."""
    shutil.rmtree(data_dir / "events", ignore_errors=True)
    recorder = ParquetRecorder(data_dir, flush_rows=10_000, flush_seconds=3600)

    def record(feed: Feed, day: date, symbol: str, row: Row) -> None:
        full = dict.fromkeys(feed.schema.names) | row | {"symbol": symbol}
        recorder.record("events", day, feed.tr_id, symbol, full, feed.schema)

    evening = PAST_DAYS[-1]
    start = datetime.combine(evening, time(18, 0), KST)
    price, volume = 380.0, 0
    for n in range(12 * 6):  # every 10 minutes, 18:00 to 05:50
        at = start + timedelta(minutes=10 * n)
        price = round(price + (0.35 if n % 5 < 3 else -0.4), 2)
        volume += 150 + 10 * (n % 7)
        change = round((price / 380 - 1) * 100, 2)
        night = {"event_time": at, "received_at": at, "price": price, "change_pct": change}
        record(NIGHT_FUTURES, kst_day(at), "A01612", night | {"qty": 10, "cum_volume": volume})
    for i, symbol in enumerate(DEMO_SYMBOLS):
        mid, tick = _OPENS[symbol]
        for t, ticks in ((time(8, 5), -2), (time(8, 20), -1), (time(8, 42), 1 + i), (time(8, 48), 2 + i)):
            at = _at(t)
            trade = {"event_time": at, "received_at": at, "venue": "UN", "price": mid + ticks * tick}
            record(TRADE, DEMO_DAY, symbol, trade | {"qty": 30})
        for t, ticks in ((time(8, 31), 0), (time(8, 45), 1), (time(8, 59, 50), 2)):
            at = _at(t)
            book = {"event_time": at, "received_at": at, "expected_px": mid + ticks * tick}
            record(BOOK, DEMO_DAY, symbol, book | {"expected_qty": 4_000 + 500 * i, "expected_exch_cls": "1"})
    recorder.close()
    for name, last, step in (
        ("sox", 6_120.0, -40.0),
        ("nvda", 182.5, -1.1),
        ("mu", 168.0, -1.5),
        ("vix", 18.4, 0.6),
    ):
        closes = [
            Observation(evening - timedelta(days=d), round(last - step * d, 2)) for d in range(9, -1, -1)
        ]
        write_us_daily(
            data_dir,
            name,
            [c for c in closes if c.day.weekday() < 5] + [Observation(date(2026, 10, 9), last + step)],
        )
    events = [
        ("demo-kr-gdp", time(8, 0), "KR", "GDP Growth Rate QoQ Adv", "high"),
        ("demo-us-retail", time(21, 30), "US", "Retail Sales MoM", "high"),
    ]
    body = {
        "fetched_at": _at(time(7, 45)).isoformat(),
        "countries": ["KR", "US"],
        "events": [
            {
                "id": ident,
                "at": _at(t).isoformat(),
                "country": country,
                "title": title,
                "indicator": None,
                "category": None,
                "period": None,
                "actual": None,
                "forecast": None,
                "previous": None,
                "unit": "%",
                "scale": None,
                "currency": None,
                "importance": importance,
                "source": None,
                "source_url": None,
            }
            for ident, t, country, title, importance in events
        ],
    }
    path = data_dir / "calendar" / "latest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, ensure_ascii=False))


def write_demo_flow(data_dir: Path) -> None:
    """The flow screen's recordings (#306), as the collector writes them: each symbol's program
    trading and member firms through the morning (09:00–10:30 on the 12th, nothing during the
    circuit breaker's halt; 10:00–10:10 by the REST stand-ins, as when the feeds are cut), the
    investor estimates of the 09:30 slot, and the whole of the 8th for the same-time comparison.
    Written after the morning is replayed: the algorithms read none of it."""
    recorder = ParquetRecorder(data_dir, flush_rows=10_000, flush_seconds=3600)
    rng = random.Random(306)

    def record(feed: Feed | RestFeed, schema_feed: Feed | RestFeed, day: date, symbol: str, row: Row) -> None:
        full = dict.fromkeys(schema_feed.schema.names) | row | {"symbol": symbol}
        recorder.record(
            "events",
            day,
            feed.tr_id if isinstance(feed, Feed) else feed.events_feed,
            symbol,
            full,
            schema_feed.schema,
        )

    days = ((PAST_DAYS[-1], time(9, 0), time(15, 30)), (DEMO_DAY, time(9, 0), _END))
    for i, symbol in enumerate(DEMO_SYMBOLS):
        mid, _ = _OPENS[symbol]
        for day, start, end in days:
            buy = sell = foreign = 0
            at = datetime.combine(day, start, KST)
            while at <= datetime.combine(day, end, KST):
                local = at.time()
                if day == DEMO_DAY and HALT[0] <= local < HALT[1]:
                    at += timedelta(minutes=1)
                    continue  # the market halted: nothing traded
                rest = day == DEMO_DAY and time(10, 0) <= local < time(10, 10)
                buy += rng.randint(200, 900) * mid // 100
                sell += rng.randint(200, 900 - 120 * i) * mid // 100
                program = {
                    "event_time": at,
                    "received_at": at + timedelta(seconds=1),
                    "buy_value": buy,
                    "sell_value": sell,
                    "net_value": buy - sell,
                    "buy_qty": buy // mid,
                    "sell_qty": sell // mid,
                    "net_qty": (buy - sell) // mid,
                }
                if not rest:
                    program |= {"sell_order_qty": 0, "buy_order_qty": 0, "whole_net_qty": 0}
                record(PROGRAM_REST if rest else PROGRAM, PROGRAM, day, symbol, program)
                foreign += rng.randint(-400, 600) - 100 * i
                member = {
                    "received_at": at + timedelta(seconds=2),
                    "foreign_net_qty": foreign,
                    "foreign_buy_qty": max(foreign, 0) + 5_000,
                    "foreign_sell_qty": max(-foreign, 0) + 5_000,
                }
                for side, names in (("sell", _SELLERS), ("buy", _BUYERS)):
                    for k, (no, name, glob) in enumerate(names, start=1):
                        member |= {
                            f"{side}_member_{k}_no": no,
                            f"{side}_member_{k}_name": name,
                            f"{side}_member_{k}_qty": (6 - k) * 1_000 + rng.randint(0, 500),
                            f"{side}_member_{k}_foreign": glob,
                        }
                record(MEMBER_REST if rest else MEMBER, MEMBER, day, symbol, member)
                at += timedelta(minutes=1)
            for t, foreign_net, institution_net in (
                (time(9, 31), 12_000 - 9_000 * i, None),
                (time(10, 2), 15_000 - 9_000 * i, -4_000 + 2_000 * i),
            ):
                if datetime.combine(day, t, KST) > datetime.combine(day, end, KST):
                    continue
                received = datetime.combine(day, t, KST)
                # Each poll gives every slot so far: the 09:30 slot's foreign figure, then the 10:00 slot's
                # institutional one too.
                slots = [("1", foreign_net, None)] + (
                    [("2", None, institution_net)] if institution_net else []
                )
                for slot, f, inst in slots:
                    record(
                        FOREIGN_ESTIMATE,
                        FOREIGN_ESTIMATE,
                        day,
                        symbol,
                        {
                            "received_at": received,
                            "slot": slot,
                            "foreign_net_qty": f,
                            "institution_net_qty": inst,
                            "total_net_qty": None,
                        },
                    )
    recorder.close()


# The top five member firms of the demo's flows (#306): number, name, foreign.
_SELLERS = (
    ("005", "미래에셋", False),
    ("044", "모건스탠리", True),
    ("036", "한국투자", False),
    ("029", "골드만삭스", True),
    ("030", "NH투자", False),
)
_BUYERS = (
    ("003", "삼성", False),
    ("040", "JP모간", True),
    ("002", "키움", False),
    ("042", "메릴린치", True),
    ("004", "KB증권", False),
)


# The daily kind's days shown answered (#264), up to the 8th.
_DAILY_SHOWN = 20


def write_demo_daily(db: Path, data_dir: Path) -> Model:
    """The daily kind's last ``_DAILY_SHOWN`` days before the morning (#264): each day's 08:50
    predictions of its baselines and of Cox (fitted on the days before them), and the day's
    answer at 15:45 (``daily.ANSWER_FROM``), as the engine would have stored them. Cox's model,
    for the morning."""
    from krx_quant.algorithms.cox import Cox  # the daily algorithm, and its features
    from krx_quant.daily import DailyInputs
    from krx_quant.daily_features import DailyFiles, band_of, samples
    from krx_quant.kinds import Answer
    from krx_quant.predictions import NewKindPrediction

    files = DailyFiles(data_dir, data_dir / "state")
    kind = OPEN_TO_CLOSE
    found = samples(kind, files, DEMO_SYMBOLS, Cox.features, _FEE, calendar=_CALENDAR)
    days = sorted({s.day for s in found})
    shown = days[-_DAILY_SHOWN:]
    model = Cox().fit([s for s in found if s.day < shown[0]])
    store = PredictionStore(db)
    store.record_models([("cox", model.version, model.target_version)])
    by = {(s.symbol, s.day): s for s in found}
    for day in shown:
        at = kind.predicted_at(day)
        made, answered = [], []
        for symbol in DEMO_SYMBOLS:
            sample = by.get((symbol, day))
            if sample is None or sample.answer is None:
                continue
            band = band_of(sample.answer)
            history = [d for d in files.days(symbol) if d.day < day]
            assert kind.answers is not None
            past = tuple((d.day, a.label) for d, a in zip(history, kind.answers(history, _FEE), strict=True))
            inputs = DailyInputs(symbol, day, at, past[-260:], band, _CALENDAR.previous_trading_day(day))
            forecasts = [(b.name, b.version, b.predict(inputs)) for b in kind.daily_baselines]
            forecasts.append(("cox", model.version, Cox().predict(sample.features, model)))
            for name, version, forecast in forecasts:
                made.append(
                    NewKindPrediction(
                        symbol,
                        name,
                        version,
                        at,
                        at + timedelta(milliseconds=200),
                        None if forecast is None else kind.output(forecast),
                        "ok" if forecast is not None else "no_previous",
                        band=None if band is None else band.value,
                    )
                )
            answer = Answer(sample.answer.label, sample.answer.unscorable, {**sample.answer.path, "vi": 0})
            answered.append((symbol, at, answer, False))
        store.add_kind_predictions(kind.name, made)
        store.add_kind_answers(kind.name, kind.answer_version, answered, scored_at=_at_on(day, ANSWER_FROM))
    store.close()
    return model


def _at_on(day: date, t: time) -> datetime:
    return datetime.combine(day, t, KST)


async def _paced(
    engine: PredictionEngine, events: Sequence[Event], pace: float, app: web.Application
) -> AsyncIterator[None]:
    """While the API is served: the rest of the morning, a minute every ``pace`` seconds."""
    going = asyncio.create_task(_replay_the_rest(engine, events, pace))
    yield
    going.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await going


async def _replay_the_rest(engine: PredictionEngine, events: Sequence[Event], pace: float) -> None:
    """The morning's events after the clock, a minute of them every ``pace`` seconds, to 10:30,
    in six steps of 10 seconds: the bar being made moves within its minute (#110)."""
    clock = engine.clock
    assert isinstance(clock, FakeClock)  # replay's
    moment = clock.now()
    rest = [e for e in events if e[1]["received_at"] > moment]  # in receipt order
    sent = 0
    while moment < _at(_END):
        await asyncio.sleep(pace / _STEPS)
        moment += timedelta(minutes=1) / _STEPS
        upto = sent
        while upto < len(rest) and rest[upto][1]["received_at"] <= moment:
            upto += 1
        await replay_into(engine, clock, rest[sent:upto], until=moment)
        sent = upto


def demo_morning(pace: float | None) -> str:
    """The demo's clock in words, for its startup line."""
    if pace is None:
        return f"{DEMO_DAY} {_END:%H:%M}"
    return f"{DEMO_DAY} {_PACED_FROM:%H:%M} to {_END:%H:%M}, a minute every {pace:g} s"


def demo_reports() -> list[dict[str, Any]]:
    """The last night's made-up reports (#165), with what ``/v1/algorithms`` shows of the
    nightly training's: every algorithm's challenger validated on the three days before and
    not promoted, so the demo runs its defaults."""
    reports = []
    for i, a in enumerate(ALGORITHMS):
        champion, challenger = 1.0 + i / 100, 1.0 + i / 100 + 0.0021
        reports.append(
            {
                "algorithm": a.name,
                "day": "2026-10-09",
                "validation_days": ["2026-10-06", "2026-10-07", "2026-10-08"],
                "champion": {
                    "version": f"{a.name}-default",
                    "loss": champion,
                    "trained_on_validation": False,
                },
                "challenger": {"version": f"{a.name}-20261002-{i:06x}", "loss": challenger},
                "promoted": False,
                "reason": f"{challenger:.4f} ≥ {champion:.4f} ({a.name}-default)",
                "samples": {"validation": 1854},
            }
        )
    return reports


def demo_shelf(state_dir: Path) -> Shelf:
    """Newton's model history: one fitted on the 8th, rolled back on the 9th to the defaults
    the demo runs."""
    shutil.rmtree(state_dir / "models", ignore_errors=True)
    shelf = Shelf(state_dir)
    fitted = Model(
        "newton-20261008-5e1f0a",
        {"damping": 0.25, "threshold": 1.5},
        "2026-09-10..2026-10-08, 9120 samples, log loss 0.9312",
        date(2026, 10, 8),
    )
    shelf.install("newton", fitted, "fit", datetime(2026, 10, 8, 22, 5, tzinfo=KST))
    shelf.rollback("newton", datetime(2026, 10, 9, 9, 10, tzinfo=KST))
    return shelf


def demo_kis(now: datetime) -> dict[str, object]:
    """A made-up KIS status at ``now``: connected, the member flow of 000660 quiet for 16 minutes."""
    last = {(TRADE.tr_id, s): now - timedelta(seconds=2) for s in DEMO_SYMBOLS}
    last |= {(BOOK.tr_id, s): now - timedelta(seconds=1) for s in DEMO_SYMBOLS}
    last |= {
        (INDEX.tr_id, "0001"): now - timedelta(seconds=5),
        ("H0UNMBC0", "000660"): now - timedelta(minutes=16),
    }
    feeds = [
        {
            "feed": feed,
            "key": key,
            "last_received_at": iso(at),
            "seconds_since": (now - at).total_seconds(),
            "gap": "stale" if feed == "H0UNMBC0" else None,
        }
        for (feed, key), at in last.items()
    ]
    frame = now - timedelta(seconds=1)  # the last frame of any kind: the book's
    return {
        "connected": True,
        "last_frame_at": iso(frame),
        "seconds_since_frame": (now - frame).total_seconds(),
        "feeds": feeds,
    }


def write_demo_macro(data_dir: Path) -> None:
    """The demo morning's macro read at 07:30, recorded as the collector records it."""
    shutil.rmtree(data_dir / MACRO, ignore_errors=True)
    wednesday, thursday, friday = date(2026, 10, 7), date(2026, 10, 8), date(2026, 10, 9)
    macro = MacroDay(DEMO_DAY, thursday)  # the KRX trading day before
    for indicator in INDICATORS:
        before, last = _OVERNIGHT[indicator.name]
        seoul = indicator.source in (Source.ECOS, Source.KIS)
        days = (wednesday, thursday) if seoul else (thursday, friday)
        macro.readings[indicator.name] = Reading(
            indicator, Observation(days[1], last), Observation(days[0], before)
        )
    macro.decide(RegimeRules())
    parquet = ParquetRecorder(data_dir, flush_rows=1000, flush_seconds=3600)
    try:
        macro_recorder(parquet)(macro, _at(time(7, 30)))
    finally:
        parquet.close()


def _at(t: time) -> datetime:
    return datetime.combine(DEMO_DAY, t, KST)


def _every(start: datetime, end: datetime, seconds: float) -> list[datetime]:
    times = []
    while start <= end:
        times.append(start)
        start += timedelta(seconds=seconds)
    return times


def _row(feed: Feed, when: datetime, values: dict[str, str]) -> Event:
    """A row through the feed's own normalizer, as the collector records it."""
    fields = dict.fromkeys(feed.columns, "0") | values
    return (feed.tr_id, feed.normalize(fields, when.astimezone(UTC)))


def _book(when: datetime, symbol: str, bid: int, ask: int) -> Event:
    values = {
        "MKSC_SHRN_ISCD": symbol,
        "BSOP_HOUR": when.strftime("%H%M%S"),
        "BIDP1": str(bid),
        "ASKP1": str(ask),
    }
    return _row(BOOK, when, values)


def _trade(when: datetime, symbol: str, price: int, qty: int) -> Event:
    values = {
        "MKSC_SHRN_ISCD": symbol,
        "BSOP_DATE": when.strftime("%Y%m%d"),
        "STCK_CNTG_HOUR": when.strftime("%H%M%S"),
        "STCK_PRPR": str(price),
        "CNTG_VOL": str(qty),
    }
    return _row(TRADE, when, values)


def _index(when: datetime, code: str, value: float, change_pct: float) -> Event:
    values = {
        "bstp_cls_code": code,
        "bsop_hour": when.strftime("%H%M%S"),
        "prpr_nmix": f"{value:.2f}",
        "prdy_ctrt": f"{change_pct:.2f}",
    }
    return _row(INDEX, when, values)


def _futures(
    when: datetime,
    price: float,
    change_pct: float,
    *,
    feed: Feed = KOSPI200_FUTURES,
    contract: str = "A01612",
) -> Event:
    values = {
        "futs_shrn_iscd": contract,
        "bsop_hour": when.strftime("%H%M%S"),
        "futs_prpr": f"{price:.2f}",
        "last_cnqn": "1",
        "futs_prdy_ctrt": f"{change_pct:.2f}",
    }
    return _row(feed, when, values)


def _vi(received: datetime, symbol: str, start: str, end: str | None) -> Event:
    row = {
        "mksc_shrn_iscd": symbol,
        "vi_cls_code": "N" if end else "Y",
        "bsop_date": received.strftime("%Y%m%d"),
        "cntg_vi_hour": start,
        "vi_cncl_hour": end or "",
        "vi_kind_code": "2",
        "vi_prc": "0",
        "vi_count": "1",
    }
    [event] = VI_STATUS.normalize({"output": [row]}, symbol, received.astimezone(UTC))
    return (VI_STATUS.tr_id, event)
