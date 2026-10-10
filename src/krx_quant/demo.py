"""Demo engine: a made-up morning through the real engine, served by the real Engine API.

``krx-quant demo-engine`` is for building screens (the Fincept screen, #20) without the
server or KIS. It replays the same made-up events every time: three symbols wandering
from 09:00 to 10:30 on 2026-10-12, KOSPI and KOSPI200 futures ticks, and a VI on one
symbol. Every registered algorithm is set active, so the consensus has members, the
last night's training shows as applied, and the morning's macro read decided risk_off.
The clock stays at 10:30.
"""

from __future__ import annotations

import random
import shutil
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from aiohttp import web

from krx_quant.algorithms import ALGORITHMS
from krx_quant.api import create_app
from krx_quant.engine import Event
from krx_quant.feeds import BOOK, INDEX, KOSPI200_FUTURES, TRADE, VI_STATUS, Feed
from krx_quant.feeds.base import KST
from krx_quant.lifecycle import Status
from krx_quant.macro import INDICATORS, MACRO, MacroDay, Observation, Reading, macro_recorder
from krx_quant.market_calendar import MarketCalendar
from krx_quant.nightly import Night
from krx_quant.predictions import DB_NAME, PredictionStore
from krx_quant.recorder import ParquetRecorder
from krx_quant.regime import RegimeRules
from krx_quant.replay import replay
from krx_quant.swap import NightStatus

DEMO_DAY = date(2026, 10, 12)
DEMO_SYMBOLS = ("005930", "000660", "009150")
_START, _END = time(9, 0), time(10, 30)
_FEE = 0.00015
# Each symbol's mid-point at the open and its tick.
_OPENS = {"005930": (81_250, 100), "000660": (201_000, 500), "009150": (150_050, 100)}
# The overnight before the demo morning, each indicator's (Thursday, Friday) values: SOX
# down 1.4% and the won 0.3% weaker, so risk_off.
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


def demo_events() -> list[Event]:
    """The demo morning's events, in receipt order."""
    events: list[Event] = []
    start, end = _at(_START), _at(_END)
    for n, symbol in enumerate(DEMO_SYMBOLS):
        rng = random.Random(n)
        mid, tick = _OPENS[symbol]
        when = start + timedelta(seconds=3 * n)
        while when <= end:
            mid += tick * rng.choice((-2, -1, 0, 0, 1, 2))
            events.append(_book(when, symbol, mid - tick // 2, mid + tick // 2))
            price = mid + rng.choice((-1, 1)) * (tick // 2)  # at the bid or the ask
            events.append(_trade(when + timedelta(seconds=1), symbol, price, rng.randint(1, 50)))
            when += timedelta(seconds=11)
    rng = random.Random(len(DEMO_SYMBOLS))
    level = 0.0
    for when in _every(start, end, 5):
        level += rng.gauss(0, 0.02)
        events.append(_index(when, "0001", 2600 * (1 + level / 100), level))
        events.append(_futures(when + timedelta(seconds=2), 350 * (1 + level / 100), level))
    # 000660 in a VI from 09:40:10, released at 09:42:20.
    events.append(_vi(_at(time(9, 40, 11)), "000660", "094010", None))
    events.append(_vi(_at(time(9, 42, 21)), "000660", "094010", "094220"))
    return sorted(events, key=lambda e: e[1]["received_at"])


async def demo_app(db: Path) -> web.Application:
    """The Engine API on the demo morning, replayed into ``db`` (replaced), with the
    morning's macro read in ``<db name>-data/macro`` (replaced). Refuses the live engine's
    database name, so a mistyped path cannot replace it.
    """
    # Case-insensitive: on macOS and Windows ENGINE.SQLITE3 is the same file.
    if db.name.casefold() in {n.casefold() for n in (DB_NAME, f"{DB_NAME}-wal", f"{DB_NAME}-shm")}:
        raise ValueError(f"{db} is named like the live engine's database; the demo needs its own file")
    for leftover in (db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")):
        leftover.unlink(missing_ok=True)
    start = _at(time(0))
    store = PredictionStore(db)
    for a in ALGORITHMS:  # active from the start, so the consensus has members
        store.add_status(a.name, start, None, Status.ACTIVE, "demo")
    store.close()
    engine = await replay(
        demo_events(),
        db=db,
        calendar=MarketCalendar({DEMO_DAY: True}),
        symbols=DEMO_SYMBOLS,
        fee_rate=_FEE,
        start=start,
        until=_at(_END),
    )
    reports = [{"algorithm": a.name, "promoted": False, "reason": "demo"} for a in ALGORITHMS]
    night = Night(date(2026, 10, 9), "2026-10-09T21:31:40+09:00", {}, reports)
    engine.training = NightStatus(night, True, "applied").as_dict()
    data_dir = db.with_name(f"{db.stem}-data")
    write_demo_macro(data_dir)
    return create_app(engine, mode="demo", data_dir=data_dir)


def write_demo_macro(data_dir: Path) -> None:
    """The demo morning's macro read at 07:30, recorded as the collector records it."""
    shutil.rmtree(data_dir / MACRO, ignore_errors=True)
    thursday, friday = date(2026, 10, 8), date(2026, 10, 9)
    macro = MacroDay(DEMO_DAY, friday)
    for indicator in INDICATORS:
        before, last = _OVERNIGHT[indicator.name]
        macro.readings[indicator.name] = Reading(
            indicator, Observation(friday, last), Observation(thursday, before)
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


def _futures(when: datetime, price: float, change_pct: float) -> Event:
    values = {
        "futs_shrn_iscd": "A01612",
        "bsop_hour": when.strftime("%H%M%S"),
        "futs_prpr": f"{price:.2f}",
        "last_cnqn": "1",
        "futs_prdy_ctrt": f"{change_pct:.2f}",
    }
    return _row(KOSPI200_FUTURES, when, values)


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
