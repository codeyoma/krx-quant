"""Demo engine: a made-up morning through the real engine, served by the real Engine API.

``krx-quant demo-engine`` is for building screens (the Fincept screen, #20) without the
server or KIS. It replays the same made-up events every time: three symbols wandering
from 09:00 to 10:30 on 2026-10-12, KOSPI and KOSPI200 futures ticks, and a VI on one
symbol. Every registered algorithm is set active, so the consensus has members, the
last night's training shows as applied, the morning's macro read decided risk_off, Newton
has a model installed and rolled back, and the KIS status is made up (one feed in a gap).
The clock stays at 10:30. Every answer is the same each run, except the CPU budget's
timings, which are measured.

Before the morning come the last 80 minutes (14:00–15:20) of the two trading days before
it, 2026-10-07 and 2026-10-08 (Friday the 9th is Hangul Day, closed), so a chart scrolls back into them
(#108): their predictions are in the database, and their one-minute bars are stored as
the backfill stores them, under ``<db stem>-data/bars/``. Each symbol also has two years of
made-up day candles up to the 8th (``candles/``), for the chart's day view (#111), and the
finished week, month and year candles made of them (#112).

With a pace (``demo-engine --pace SECONDS``), the morning is replayed at once only up to
10:00; the API opens there, and the rest goes on a minute every ``pace`` seconds, in steps
of 10 seconds, so the stream sends predictions, the consensus, outcomes and the bar being
made as they are (#21, #110). It ends at
10:30 as the unpaced demo does, with the same answers.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import shutil
from collections import defaultdict
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, date, datetime, time, timedelta
from functools import partial
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from aiohttp import web

from krx_quant.algorithms import ALGORITHMS, Model
from krx_quant.api import create_app
from krx_quant.backfill import BAR_SCHEMA, stock_bars_path
from krx_quant.candles import PERIODS, period_start, write_candles
from krx_quant.clock import FakeClock
from krx_quant.engine import Event, PredictionEngine
from krx_quant.feeds import BOOK, INDEX, KOSPI200_FUTURES, TRADE, USD_FUTURES, VI_STATUS, Feed
from krx_quant.feeds.base import KST, Row
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
from krx_quant.predictions import DB_NAME, PredictionStore, iso
from krx_quant.recorder import ParquetRecorder
from krx_quant.regime import RegimeRules
from krx_quant.replay import replay, replay_into
from krx_quant.swap import NightStatus

DEMO_DAY = date(2026, 10, 12)
DEMO_SYMBOLS = ("005930", "000660", "009150")
_START, _END = time(9, 0), time(10, 30)
_CANDLE_DAYS = 500  # about two years of trading days
# The trading days before the morning, with the minutes of each a chart scrolls back into.
PAST_DAYS = (date(2026, 10, 7), date(2026, 10, 8))
_PAST_FROM, _PAST_TO = time(14, 0), time(15, 20)
_CALENDAR = MarketCalendar(
    {d: True for d in PAST_DAYS}
    | {date(2026, 10, 9): False, date(2026, 10, 10): False, date(2026, 10, 11): False, DEMO_DAY: True}
)
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
    events = _session_events(_at(_START), _at(_END), seed=0)
    # 000660 in a VI from 09:40:10, released at 09:42:20.
    events.append(_vi(_at(time(9, 40, 11)), "000660", "094010", None))
    events.append(_vi(_at(time(9, 42, 21)), "000660", "094010", "094220"))
    return sorted(events, key=lambda e: e[1]["received_at"])


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
        for period in PERIODS:
            if period != "day":  # the finished ones, made of the days, as KIS's would be (#112)
                write_candles(data_dir, period, symbol, _finished_periods(oldest_first, period))


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
    for a in ALGORITHMS:  # active from the first day, so the consensus has members
        store.add_status(a.name, datetime.combine(PAST_DAYS[0], time(), KST), None, Status.ACTIVE, "demo")
    store.close()
    data_dir = db.with_name(f"{db.stem}-data")
    write_demo_macro(data_dir)  # first: Markov reads the morning's regime
    shutil.rmtree(data_dir / "bars", ignore_errors=True)
    write_demo_candles(data_dir)
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
        )  # no macro read those mornings: no regime
        engine.store.close()
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
    )
    reports = [{"algorithm": a.name, "promoted": False, "reason": "demo"} for a in ALGORITHMS]
    night = Night(date(2026, 10, 9), "2026-10-09T21:31:40+09:00", {}, reports)
    engine.training = NightStatus(night, True, "applied").as_dict()
    shelf = demo_shelf(data_dir / "state")
    app = create_app(
        engine, mode="demo", live_status=lambda: demo_kis(engine.clock.now()), data_dir=data_dir, shelf=shelf
    )
    if pace is not None:
        app.cleanup_ctx.append(partial(_paced, engine, events, pace))
    return app


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
    return {"connected": True, "feeds": feeds}


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
