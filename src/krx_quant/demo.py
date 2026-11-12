"""Demo engine: a made-up morning through the real engine, served by the real Engine API.

``krx-quant demo-engine`` is for building screens (the Fincept screen, #20) without the
server or KIS. It replays the same made-up events every time: three symbols wandering
from 09:00 to 10:30 on 2026-10-12, KOSPI and KOSPI200 futures ticks, and a VI on one
symbol, forecast and scored on the touch target as the server's default (#170: its four
baselines). The KOSPI falls 8.5% at 09:05, so a circuit breaker's first stage halts the
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
(#108): their predictions are in the database, and their one-minute bars (the KOSPI200 futures'
too) are stored as the backfill stores them, under ``<db stem>-data/bars/``. The SMT divergences
(#521): the engine kept the 8th's and the morning's live; the 7th's are taken out, as on a day
before the engine judged them (#520), so the API makes that day again from its backfilled bars.
Each symbol also has two years of made-up day candles up to the 8th (``candles/``), for the
chart's day view (#111), and the finished week, month and year candles made of them (#112), and
made-up daily supply data on the same days (``state/supply/``, #226): the 12th's is not out yet,
as on the server that morning.

The daily kind ``open_to_close`` (#264) has KRX day candles of its own (``krx.parquet``, the
same days, an open a tick off the integrated one's) and, for its last 20 days up to the 8th,
the three baselines' and Cox's predictions at 08:50 and their answers at 15:45, Cox fitted on
the days before them. The morning's 08:50 predictions are in too, waiting for their answer.
The five-session kind ``open_to_close_5d`` (#310) has its three baselines' predictions of the
windows begun on those 20 days and the answers of those ended by the 8th, each at its end day's
20:35; the four begun on the 5th to the 8th and the morning's wait for their end. Its algorithm
``cox-5d`` makes none: its pre-validation was refused on the server, so neither engine runs it, and
the API shows it refused, with a made-up report (#561). The 60-minute barrier's Breiman waits for its
pre-validation, as on the server: no engine runs it either.
The verdict (#676): each bar close's BUY, SELL or WAIT from the barrier's Breimans that run (the 4-,
15- and 30-minute; the 60-minute waits), each with a made-up model (``demo_barrier_models``): trees
fitted on made-up samples whose up first leans with the price's velocity and, a little, with the time
of day, a phase a horizon, so the members lean apart and the verdict changes over the morning. The
trader's position on the morning reads as held.
The KRX day candles carry KIS's 락 구분 as the collector keeps it (``ex_code``): 00 every day but a
made-up interim ex-dividend day (05) of 005930 on 2026-09-29, so the windows begun the four sessions
before it are marked ``ex_dividend``.

With a pace (``demo-engine --pace SECONDS``), the morning is replayed at once only up to
10:00; the API opens there, and the rest goes on a minute every ``pace`` seconds, in steps
of 10 seconds, so the stream sends predictions, the consensus, outcomes and the bar being
made as they are (#21, #110). It ends at
10:30 as the unpaced demo does, with the same answers.

The trader's store (``state/trader.sqlite3``, #398) holds made-up trades on 005930, filled on the
demo's own quotes by the paper rules: one each on the two afternoons before, and seven on the
morning, paper up to 10:05 and live after, the last still held at 10:30. A paced demo has them all
from the start; a screen marks each once its bar is there. With them (#397), for the auto trader
screen: the trader's state at 10:30 (the live trader running, that trade open), each mode's day
ledgers by the trader's own limits, a pause of new entries from the screen on the 8th and the
server's resume on the 12th. Policy 1, fitted on the 7th's night, is the first champion: the
trades from the 8th are its, with their cell's p* and lower bound (the 7th's has none, entered
before the picks, #395); policy 2, fitted on the 8th's night, is a challenger with two paper trades
of its own on the morning (#400), and each policy's paper days are kept, so the state has the
champion, the live gate's progress and the challenger's standing. The evaluation version (#469) is
begun on the 8th's night, so the records count from the 12th, the first day of its first review
cycle, whose cohort (#473) is policy 2 against policy 1. Each policy's cell carries the nightly fit's
figures (#472: its signals measured alone a share at each θ, the bound it entered on) and its
calibration (#475), with a cell it left out (``no_edge``); each trade its execution cost (#475: the
mid-point known at each decision) and the live book's buys their orders (``unfilled_buys``), which
the state's risk rules count. A paced demo has that 10:30 state from the start:
not alive (it is written at 10:30) until its clock gets near there. A pause posted to the demo is
written as the engine writes it, and stays unread: no trader runs.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
import shutil
import sqlite3
from collections import defaultdict
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, date, datetime, time, timedelta
from functools import partial
from pathlib import Path
from typing import Any, cast

import pyarrow as pa
import pyarrow.parquet as pq
from aiohttp import web

from krx_quant.algorithms import ALGORITHMS, Algorithm, KindAlgorithm, Model
from krx_quant.api import create_app
from krx_quant.assembly import Gates, Mode, build_engine
from krx_quant.auctions import KEEP_DAYS, MINUTE_BAR, write_auctions
from krx_quant.backfill import (
    BAR_SCHEMA,
    FUTURES_BAR_SCHEMA,
    FUTURES_BARS_TR,
    bars_key,
    stock_bars_path,
)
from krx_quant.candles import EX_CODE, PERIODS, period_start, write_candles, write_krx_days
from krx_quant.clock import FakeClock
from krx_quant.config import Settings
from krx_quant.contracts import KOSPI200
from krx_quant.daily import ANSWER_FROM, NO_EX_CODE, DailyAlgorithm, StoredDays
from krx_quant.engine import Event, PredictionEngine
from krx_quant.feeds import (
    BOOK,
    FOREIGN_ESTIMATE,
    INDEX,
    KOSPI200_FUTURES,
    TRADE,
    USD_FUTURES,
    VI_STATUS,
    Feed,
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
    macro_recorder,
)
from krx_quant.market_calendar import MarketCalendar
from krx_quant.model_shelf import Shelf
from krx_quant.nightly import Night
from krx_quant.open_to_close import OPEN_TO_CLOSE
from krx_quant.open_to_close_5d import OPEN_TO_CLOSE_5D
from krx_quant.predictions import DB_NAME, PredictionStore, iso
from krx_quant.prevalidation import Refusal, judging
from krx_quant.recorder import ParquetRecorder
from krx_quant.regime import RegimeRules
from krx_quant.replay import replay_into
from krx_quant.supply import KIS, NET_MARKET, NXT_OPENED, write_supply
from krx_quant.swap import NightStatus, reports_by_algorithm
from krx_quant.touch import TARGET_VERSION
from krx_quant.trader import CAPITAL, PAPER, THRESHOLD, cohort, review
from krx_quant.trader import SYMBOL as TRADER_SYMBOL
from krx_quant.trader import policy as policies
from krx_quant.trader.book import Rules as TraderRules
from krx_quant.trader.limits import Guard
from krx_quant.trader.money import commission, open_net, round_trip, settled, shares
from krx_quant.trader.paper import PaperTrader
from krx_quant.trader.quotes import BestQuote
from krx_quant.trader.store import API, CLI, PAUSE, RESUME, TraderStore
from krx_quant.trader.store import DB_NAME as TRADER_DB
from krx_quant.trader.store import Entry as TraderEntry
from krx_quant.trader.store import Exit as TraderExit
from krx_quant.trader.store import Placed as TraderPlaced

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
# The morning's crash (#236): the KOSPI this far (%) below its wander from CRASH_FROM. Held a minute,
# the first stage is called at 09:06, halting trading 20 minutes and a 10-minute single-price
# auction after: HALT. The trades carry no halt flag: the breaker's window alone (#625).
CRASH, CRASH_FROM = 8.5, time(9, 5)
HALT = (time(9, 6), time(9, 36))
_PACED_FROM = time(10, 0)  # a paced demo opens here and goes on a minute at a time…
_STEPS = 6  # …in steps of 10 seconds
_FEE = 0.00015
# The algorithms the server's pre-validation refused, which the engine does not run: cox-5d
# (2026-10-23, #310; kept refused by the user's decision of 2026-10-24), with a made-up report's
# failed check (#561). The demo runs every other but one waiting for its pre-validation (#561): the
# 60-minute barrier's Breiman, as on the server since before 2026-10-23. Neither the earlier
# afternoons' engines nor the morning's run them, so neither has a status.
_REFUSED = {
    "cox-5d": (
        "2026-10-23T23:41:07+09:00",
        "stats",
        "weakest lift 0.0204; by trading day low -0.0072 at 95.00%",
    )
}
_WAITING = frozenset({"breiman-60m"})


def _admitted(algorithm: Algorithm | KindAlgorithm | DailyAlgorithm) -> bool:
    return algorithm.name not in _REFUSED and algorithm.name not in _WAITING


def _refusal(algorithm: Algorithm | KindAlgorithm | DailyAlgorithm) -> Refusal | None:
    """The demo's refusal of ``algorithm`` (#561), as ``prevalidation.refusals`` reads a report: of
    the code, target and inputs it has now."""
    if algorithm.name not in _REFUSED:
        return None
    at, check, detail = _REFUSED[algorithm.name]
    judged = {"prevalidated_at": at, **judging(algorithm, TARGET_VERSION)}
    return Refusal(json.dumps(judged, sort_keys=True), f"{check}: {detail}", (check,))


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
    """``day``'s one-minute bars of the demo symbols and of the KOSPI200 futures (their continuous
    series, #521) from its trades, stored as the backfill stores them (``backfill.py``), replacing
    earlier ones."""
    minutes: dict[str, dict[datetime, list[tuple[float, int]]]] = defaultdict(lambda: defaultdict(list))
    contract = None  # the futures' month
    for tr_id, row in events:
        if tr_id == TRADE.tr_id:
            name = row["symbol"]
        elif tr_id == KOSPI200_FUTURES.tr_id:
            name, contract = KOSPI200.series, row["symbol"]
        else:
            continue
        traded = row["received_at"].astimezone(KST)
        minutes[name][traded.replace(second=0, microsecond=0)].append((row["price"], row["qty"]))
    for name, bars in minutes.items():
        stock = name != KOSPI200.series
        cum = 0
        rows = []
        for start in sorted(bars):
            prices = [p for p, _ in bars[start]]
            volume = sum(q for _, q in bars[start])
            cum += int(sum(p * q for p, q in bars[start]))
            rows.append(
                {
                    "symbol": name,
                    "bar_time": start,
                    **({"venue": "UN"} if stock else {"contract": contract}),
                    "open": prices[0],
                    "high": max(prices),
                    "low": min(prices),
                    "close": prices[-1],
                    "volume": volume,
                    "cum_value": cum,
                }
            )
        path = (
            stock_bars_path(data_dir, day, name) if stock else data_dir / bars_key(day, FUTURES_BARS_TR, name)
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")  # whole or absent, as the backfill writes
        pq.write_table(pa.Table.from_pylist(rows, schema=BAR_SCHEMA if stock else FUTURES_BAR_SCHEMA), tmp)
        tmp.rename(path)


def forget_smt(db: Path, day: date) -> None:
    """``day`` as an engine from before #520 left it: no live SMT events, so the API makes the day
    again from its bars (#521)."""
    start = datetime.combine(day, time(), KST)
    with contextlib.closing(sqlite3.connect(db)) as kept, kept:
        kept.execute(
            "DELETE FROM smt_events WHERE bar_close >= ? AND bar_close < ?",
            (iso(start), iso(start + timedelta(days=1))),
        )


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
        # The KRX market's (#264): an opening auction a tick off NXT's pre-market open, now and then,
        # with KIS's 락 구분 of the day (#310): 00, or the demo's made-up ex-dividend days.
        krx = [
            {
                **r,
                "venue": "KRX",
                "open": max(tick, cast(int, r["open"]) - tick * (i % 3 - 1)),
                EX_CODE: _EX_CODES.get((symbol, cast(date, r["date"])), NO_EX_CODE),
            }
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


# The demo's made-up ex-dividend days (#310), KIS's 락 구분 of the KRX day candle: 005930's interim
# (05) on a Tuesday the five-session kind's shown windows cross.
_EX_CODES = {("005930", date(2026, 9, 29)): "05"}


# Each demo symbol's made-up listed shares and lending and credit balances around which its supply
# wanders (#226). Its foreign holdings were Toss's, until #694.
_SUPPLY = {
    "005930": (4_000_000_000, 90_000_000, 12_000_000),
    "000660": (700_000_000, 20_000_000, 3_000_000),
    "009150": (75_000_000, 4_000_000, 1_500_000),
}


def write_demo_supply(state_dir: Path) -> None:
    """Each demo symbol's made-up daily supply data on the demo candles' days (up to the 8th),
    stored as the collector stores it (``supply.py``): KIS's fields in its file (the foreign
    holdings and ratio none, since #694)."""
    shutil.rmtree(state_dir / "supply", ignore_errors=True)
    days: list[date] = []
    day = DEMO_DAY
    while len(days) < _CANDLE_DAYS:
        day = _CALENDAR.previous_trading_day(day)
        days.append(day)
    days.reverse()
    for n, symbol in enumerate(DEMO_SYMBOLS):
        rng = random.Random(200 + n)
        listed, lending, credit = _SUPPLY[symbol]
        scale = listed // 4000  # a day's typical net buy
        kis = []
        for day in days:
            foreign = round(rng.gauss(0, 1) * scale)
            institution = round(rng.gauss(0, 0.8) * scale)
            individual = -foreign - institution + round(rng.gauss(0, 0.1) * scale)
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
        write_supply(state_dir, symbol, KIS, kis)


# The trader's made-up trades on 005930 (#398), for the chart's buys and sells: each entry's time
# (the barrier prediction of its minute read two seconds after the bar close) and its barriers'
# width w in ticks; paper on the afternoons before and on the morning up to 10:05, live after, so
# the chart tells the two apart.
_DEMO_ENTRIES = (
    (datetime(2026, 10, 7, 14, 22, 2, tzinfo=KST), 4),
    (datetime(2026, 10, 8, 14, 41, 2, tzinfo=KST), 2),
    *((datetime.combine(DEMO_DAY, time(9, m, 2), KST), w) for m, w in ((43, 4), (49, 4), (55, 2))),
    *((datetime.combine(DEMO_DAY, time(10, m, 2), KST), w) for m, w in ((6, 4), (12, 6), (18, 2), (27, 8))),
)
_DEMO_LIVE_FROM = time(10, 5)
# The trader's runs and controls (#397), for the screen's state and days: the paper trader started
# on the 7th before the open; new entries paused from the screen on the 8th after its trade, and
# resumed on the server on the 12th before the open; the live one started at 10:05 (a mode is the
# server's ``.env``'s: a restart). Each control is read this long after it was asked for.
_DEMO_PAPER_FROM = datetime(2026, 10, 7, 7, 20, 3, tzinfo=KST)
_DEMO_PAUSE = datetime(2026, 10, 8, 15, 2, 10, tzinfo=KST)
_DEMO_RESUME = datetime(2026, 10, 12, 8, 41, 0, tzinfo=KST)
_DEMO_READ = timedelta(milliseconds=400)
# The trader's policies (#399, #400, made up): version 1 fitted on the 7th's night, the first
# champion, so the 8th's trade on is its; version 2 fitted on the 8th's night, a challenger entered
# that night into the cohort of a review cycle from the 12th (#473), so it runs in paper beside the
# champion from the 12th (its pick at 08:30). Each trades the top algorithm's 4-minute barrier at its
# threshold θ (#471: a policy has no Kelly fraction λ).
_DEMO_POLICIES = (
    (datetime(2026, 10, 7, 22, 31, 4, tzinfo=KST), THRESHOLD),
    (datetime(2026, 10, 8, 22, 31, 4, tzinfo=KST), 0.65),
)
# The cell as the cost map picked it (#395, made up): the predictor's role, its breakeven p*, its hit
# rate's lower bound and Kelly f*; bought with all of the capital (λ × f* cut to 1: the demo's days are
# before #471's loss budget). The 7th's trade, before the first champion, was entered before the picks:
# none of these.
_DEMO_PICK = ("top", 0.61, 0.66, 9.6)
# The challenger's paper trades on the morning (#400): its own book, beside the champion's, on the
# same quotes: each entry's time and width w.
_DEMO_CHALLENGER_ENTRIES = (
    (datetime.combine(DEMO_DAY, time(9, 46, 2), KST), 4),
    (datetime.combine(DEMO_DAY, time(9, 58, 2), KST), 2),
)


def write_demo_trader(state_dir: Path, books: Sequence[Event]) -> None:
    """The trader's store (``state/trader.sqlite3``, replaced) with a made-up trade at each of
    ``_DEMO_ENTRIES``, filled on 005930's quotes in ``books`` as paper fills are (``trader/paper.py``):
    the buy at the ask of the first quote after the decision, as many shares as the capital and the
    cash buy (``money.shares``), its barriers w ticks either side of the mid-point then, and its exit
    as ``_demo_exit`` finds it (the time barrier four minutes after the bar close).

    With them the trader's runs, its pause and resume (``_DEMO_PAUSE``, ``_DEMO_RESUME``), each day's
    ledger by the trader's own limits (``limits.Guard``: each trade marked at every bid it was held
    through, then its exit) and its state at 10:30 (#397): the live trader's, written by a paper trader
    of that mode (``PaperTrader``) on the quotes since its open trade's entry. And its policies
    (``_DEMO_POLICIES``, #399, #400): the champion's trades carry its version, the challenger's paper
    trades (``_DEMO_CHALLENGER_ENTRIES``) theirs, and each policy's trading days in paper are kept
    (``policy_days``), from which the Engine API measures the live gate and the challenger's standing."""
    for leftover in (TRADER_DB, f"{TRADER_DB}-wal", f"{TRADER_DB}-shm"):
        (state_dir / leftover).unlink(missing_ok=True)
    quotes = sorted(
        (row["received_at"].astimezone(KST), float(row["bid_px_1"]), float(row["ask_px_1"]))
        for feed, row in books
        if feed == BOOK.tr_id and row["symbol"] == TRADER_SYMBOL
    )
    rules = TraderRules(fee_rate=_FEE)
    live_from = _at(_DEMO_LIVE_FROM)
    store = TraderStore(state_dir / TRADER_DB)
    try:
        db = policies.connect(state_dir / TRADER_DB)
        try:
            versions = [
                policies.add(
                    db,
                    night,
                    _demo_policy_reason(night),
                    [_demo_policy_cell(night, θ)],
                    [_demo_left_out(night)],
                    {},
                )
                for night, θ in _DEMO_POLICIES
            ]
            (champion, _), (challenger, _) = versions
            # The evaluation version (#469), begun on the 8th's night: its first review cycle from the
            # 12th, the records counted from it. Its cohort (#473): judged against policy 1 on the cycle's
            # review night.
            entered = _DEMO_POLICIES[1][0]
            cycle = review.cycle_of(review.begin(db, entered, _CALENDAR.session), DEMO_DAY, _CALENDAR.session)
            cohort.form(db, entered, review.EVALUATION, cycle, champion, [challenger])
        finally:
            db.close()
        champion_from = _DEMO_POLICIES[0][0]
        store.add_run(_DEMO_PAPER_FROM, PAPER, {"demo": True})
        guard = Guard(store, PAPER, rules.limits, _DEMO_PAPER_FROM, lambda text: None)
        controls = [(_DEMO_PAUSE, PAUSE, API, False), (_DEMO_RESUME, RESUME, CLI, None)]
        for n, (decided, w) in enumerate(_DEMO_ENTRIES):
            mode = "live" if decided >= live_from else PAPER
            while controls and controls[0][0] < decided:
                asked, action, source, sell_now = controls.pop(0)
                _demo_day(guard, asked)
                guard.apply(store.add_control(asked, action, source, sell_now=sell_now), asked + _DEMO_READ)
                store.put_day(guard.mode, guard.day, guard.ledger(), asked + _DEMO_READ)
            if mode != guard.mode:  # the paper day ends at the switch; the live trader starts
                store.put_day(guard.mode, guard.day, guard.ledger(), live_from)
                store.add_run(live_from, mode, {"demo": True})
                guard = Guard(store, mode, rules.limits, live_from, lambda text: None)
            _demo_day(guard, decided)
            picked = decided > champion_from
            _demo_trade(
                store,
                quotes,
                decided,
                w,
                n + 1,
                mode,
                policy_id=champion if picked else None,
                pick=picked,
                cash=CAPITAL + guard.realized_all,
                guard=guard,
            )
        # The challenger's book: its own cash, held by no limit or pause.
        realized = 0.0
        for n, (decided, w) in enumerate(_DEMO_CHALLENGER_ENTRIES):
            net = _demo_trade(
                store,
                quotes,
                decided,
                w,
                101 + n,
                PAPER,
                policy_id=challenger,
                pick=True,
                cash=CAPITAL + realized,
                threshold=_DEMO_POLICIES[1][1],
                champion=False,
            )
            realized += net or 0.0
        # Each policy's trading days in paper (``desk.record``): the champion's from the 8th, stopped
        # that day by the screen's pause in the session; the challenger's on the 12th.
        for version, day, as_champion, stopped in (
            (champion, PAST_DAYS[1], True, True),
            (champion, DEMO_DAY, True, False),
            (challenger, DEMO_DAY, False, False),
        ):
            store.put_policy_day(
                PAPER, version, day, as_champion, stopped, datetime.combine(day, time(15, 30), KST)
            )
        # The live trader's state at 10:30, its open trade marked at each bid since it was bought.
        trader = PaperTrader(store, rules, _CALENDAR.session, live_from, guard.mode)
        held = store.open_trade(guard.mode)
        for at, bid, ask in quotes:
            if held is not None and held.entry_at is not None and held.entry_at < at <= _at(_END):
                trader.on_quote(BestQuote(at, bid, ask))
        trader.write(_at(_END))
    finally:
        store.close()


def _demo_policy_reason(night: datetime) -> str:
    return f"{night.date()} 밤 맞추기: 비용 지도가 시장가 왕복으로 통과시킨 1칸 가운데 1칸(데모, 지어낸 값)"


def _demo_policy_cell(night: datetime, threshold: float) -> policies.PolicyCell:
    """The demo policy's one cell, fitted as the nightly re-fit keeps it (made-up figures): the cost
    map's figures, a diagnosis (#472), the days its recording was read, its signals measured alone a
    share at each θ (#470) and the bound it entered on (#472), its calibration (#475) and its policy's
    replayed trades."""
    role, p_star, low, kelly = _DEMO_PICK
    fitted: dict[str, Any] = {"p_star": p_star, "low": low, "kelly": kelly, "verdict": "pass"}
    fitted |= {"days": [night.date().isoformat()], "recorded_days": 20, "read": {"read": 380, "rises": 120}}
    fitted |= {"signals": _demo_signals(), "calibration": _demo_calibration()}
    fitted |= {"bound": {"value": 0.42, "low": 0.08, "high": 0.76, "days": 20, "level": 0.99}}
    fitted |= {"trades": 24, "wins": 14, "net": 8_400.0, "growth": 0.0084}
    return policies.PolicyCell("barrier", role, "breiman", threshold, fitted)


def _demo_left_out(night: datetime) -> dict[str, Any]:
    """The cell the demo's nightly fit left out (made up): the direction's consensus, whose replayed net
    per signal has no lower bound above 0 at any θ (#472, ``no_edge``)."""
    best = {"value": -0.31, "low": -0.9, "high": 0.28, "days": 20, "level": 0.99}
    return {
        "kind": "direction",
        "role": "consensus",
        "predictor": "consensus-final",
        "p_star": 0.58,
        "low": 0.52,
        "kelly": None,
        "verdict": "fail",
        "why": "no_edge",
        "recorded_days": 20,
        "best": {"threshold": 0.6, "bound": best},
        "read": {"read": 1_640, "rises": 410},
        "signals": _demo_signals(-0.3),
        "calibration": _demo_calibration(-1),
    }


def _demo_signals(shift: float = 0.0) -> dict[str, Any]:
    """A cell's made-up signals measured alone a share at each θ of the grid (#470, #472)."""
    out = {}
    for n, threshold in enumerate((0.4, 0.5, 0.6, 0.7, 0.8)):
        signals = 120 - 25 * n
        bought = signals - 6
        per = round(-0.2 + 0.18 * n + shift, 2)
        out[f"{threshold:g}"] = {
            "signals": signals,
            "measured": signals - 2,
            "missing": {"gap": 2},
            "refused": {"target_reached": 4},
            "bought": bought,
            "wins": bought // 2 + n,
            "exits": {
                "stop_loss": bought // 3,
                "take_profit": bought // 2,
                "time": bought - bought // 3 - bought // 2,
            },
            "days_bought": 20 - n,
            "net": round(per * 100 * (signals - 2), 3),
            "ticks": round(per * (signals - 2), 4),
            "per_signal": {
                "value": per,
                "low": round(per - 0.35, 2),
                "high": round(per + 0.35, 2),
                "days": 20,
            },
        }
    return out


def _demo_calibration(shift: int = 0) -> dict[str, Any]:
    """A cell's made-up calibration (#475): its rises in the grid's five bins, the higher the score the
    more of them rose first."""
    bins = []
    edges = (0.4, 0.5, 0.6, 0.7, 0.8, 1.0)
    for n, (low, high) in enumerate(zip(edges, edges[1:], strict=False)):
        signals = (25, 25, 25, 25, 18)[n]
        up = max(0, 8 + 2 * n + shift)
        down = max(0, 9 - 2 * n - shift)
        flat = signals - up - down
        bins.append(
            {
                "from": low,
                "to": high,
                "signals": signals,
                "missing": 1 if n == 2 else 0,
                "answered": signals,
                "outcomes": {"up": up, "flat": flat, "down": down},
                "hits": up,
                "first": max(0, up - 1),
                "bought": signals - 2,
                "profitable": max(0, up - 2),
            }
        )
    return {"bins": bins, "below": 7}


def _demo_trade(
    store: TraderStore,
    quotes: Sequence[tuple[datetime, float, float]],
    decided: datetime,
    w: int,
    prediction_id: int,
    mode: str,
    *,
    policy_id: int | None,
    pick: bool,
    cash: float,
    threshold: float = THRESHOLD,
    champion: bool = True,
    guard: Guard | None = None,
) -> float | None:
    """A demo trade decided at ``decided`` on 005930's ``quotes`` (``write_demo_trader``), kept in
    ``store`` with its decision; its net P&L once it exits, None while held at 10:30. The champion's
    is marked in ``guard`` at each bid it was held through, from its buy's quote to the one it was
    sold on (that one sells it, as the trader's ``on_quote`` does): the day's lowest P&L and the
    cumulative one's count those dips, not the exit alone; then its exit, and the day's ledger kept."""
    after = [q for q in quotes if q[0] > decided]
    known = [q for q in quotes if q[0] <= decided]  # #475: the latest quote known at the decision
    bought_at, bid, ask = after[0]
    tick = _OPENS[TRADER_SYMBOL][1]
    anchor = (bid + ask) / 2
    bar_close = decided.replace(second=0, microsecond=0)
    quantity = shares(CAPITAL, ask, cash, _FEE)
    role, p_star, low, kelly = _DEMO_PICK if pick else (None, None, None, None)
    decision = store.add_decision(
        mode=mode,
        symbol=TRADER_SYMBOL,
        kind="barrier",
        predictor="breiman",
        prediction_id=prediction_id,
        bar_close=bar_close,
        displayed_at=decided,
        decided_at=decided,
        probability=0.68,
        action="buy",
        reason=None,
        policy_id=policy_id,
    )
    entry = TraderEntry(
        mode=mode,
        symbol=TRADER_SYMBOL,
        kind="barrier",
        predictor="breiman",
        prediction_id=prediction_id,
        bar_close=bar_close,
        probability=0.68,
        threshold=threshold,
        anchor=anchor,
        tick=tick,
        width_ticks=w,
        target=anchor + w * tick,
        stop=anchor - w * tick,
        time_barrier=bar_close + timedelta(minutes=4),
        decided_at=decided,
        entry_at=bought_at,
        entry_price=ask,
        quantity=quantity,
        buy_fee=commission(ask, quantity, _FEE),
        quote_at=bought_at,
        role=role,
        p_star=p_star,
        low=low,
        kelly=kelly,
        fraction=1.0 if pick else None,
        policy_id=policy_id,
        champion=champion,
        decided_mid=(known[-1][1] + known[-1][2]) / 2 if known else None,
    )
    trade = store.enter(decision, entry)
    if mode == "live":  # a KIS book keeps each buy it sends (#406), the day's buy orders' count (#471)
        store.add_unfilled_buy(decision, mode, TraderPlaced(ask, quantity), bought_at, "filled")
    out = _demo_exit(entry, [q for q in after[1:] if q[0] < entry.time_barrier])
    if out is None:
        return None
    if guard is not None:
        for at, held_bid, _ in after:
            if at >= out.at:
                break
            guard.mark(open_net(trade, held_bid, _FEE), at)
    store.exit(trade, out)
    net = settled(entry.entry_price, quantity, entry.buy_fee, out.price * quantity, out.sell_fee, out.tax).net
    if guard is not None:
        guard.exited(net, out.at)
        store.put_day(mode, guard.day, guard.ledger(), out.at)
    return net


def _demo_day(guard: Guard, at: datetime) -> None:
    """``guard`` on to the day of ``at``, as the trader's clock goes: past the midnight after its
    day, which keeps that day's ledger as it ended (``Guard.roll``), and on to ``at``."""
    if kst_day(at) > guard.day:
        guard.roll(datetime.combine(guard.day + timedelta(days=1), time(), KST))
        guard.roll(at)


def _demo_exit(entry: TraderEntry, held: Sequence[tuple[datetime, float, float]]) -> TraderExit | None:
    """How a demo trade ends on the quotes received while it is held (``held``: their times, bids
    and asks, before its time barrier), by the paper rules; None while it is still held at 10:30."""
    taken = next((q for q in held if q[1] >= entry.target), None)
    stopped = next((q for q in held if q[1] <= entry.stop), None)
    sold: tuple[datetime, float, float] | None = None  # the quote a sale was decided on (#475)
    if stopped is not None and (taken is None or stopped[0] < taken[0]):
        at, price, reason, quote_at, sold = stopped[0], stopped[1], "stop_loss", stopped[0], stopped
    elif taken is not None:
        at, price, reason, quote_at = taken[0], entry.target, "take_profit", taken[0]
    elif held and entry.time_barrier <= _at(_END):  # sold at the barrier, at the last bid before it
        at, price, reason, quote_at, sold = entry.time_barrier, held[-1][1], "time", held[-1][0], held[-1]
    else:
        return None
    cost = round_trip(entry.entry_price, entry.quantity, entry.buy_fee, price * entry.quantity, _FEE)
    decided = (None, None) if sold is None else (at, (sold[1] + sold[2]) / 2)
    return TraderExit(at, price, reason, cost.sell_fee, cost.tax, quote_at, *decided)


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


def demo_barrier_models() -> dict[str, Model]:
    """Made-up models of the barrier Breimans the demo runs (#676, module doc): trees fitted on 3,000
    made-up samples each, every feature missing but the velocity (−3 to 3 ticks a minute) and the
    minutes since the open, its up first ``0.35 + d / 2`` and down first ``0.35 − d / 2`` for a lean
    ``d = 0.6 tanh(velocity) + 0.15 sin(minutes / 9 + phase)``, the phase its horizon's. The same each
    run (a seed a horizon, LightGBM deterministic)."""
    import lightgbm as lgb
    import numpy as np

    from krx_quant.algorithms import KIND_ALGORITHMS
    from krx_quant.algorithms.breiman import BOOSTING
    from krx_quant.barriers import ANSWER_VERSION

    models: dict[str, Model] = {}
    for phase, algorithm in enumerate(a for a in KIND_ALGORITHMS if a.name.startswith("breiman")):
        if algorithm.name in _WAITING:
            continue
        rng = np.random.default_rng(676 + phase)
        x = np.full((3000, len(algorithm.features)), np.nan)
        velocity = rng.uniform(-3, 3, len(x))
        minutes = rng.uniform(0, 390, len(x))
        x[:, algorithm.features.index("velocity")] = velocity
        x[:, algorithm.features.index("minutes_since_open")] = minutes
        lean = 0.6 * np.tanh(velocity) + 0.15 * np.sin(minutes / 9 + 2 * phase)
        up = np.clip(0.35 + lean / 2, 0.02, 0.68)
        down = 0.7 - up
        y = np.array([rng.choice(3, p=(u, 0.3, d)) for u, d in zip(up, down, strict=True)])
        data = lgb.Dataset(x, y, feature_name=list(algorithm.features), free_raw_data=True)
        trees = lgb.train({**BOOSTING, "verbosity": -1}, data, num_boost_round=40)
        models[algorithm.name] = Model(
            f"{algorithm.name}-demo",
            {"rounds": 40.0},
            trained="made up for the demo (#676)",
            blob=trees.model_to_string(),
            target_version=ANSWER_VERSION,
        )
    return models


def demo_settings(data_dir: Path) -> Settings:
    """The demo's settings (#647): its made-up symbols and fee, the touch target, no leading series
    besides the symbols and the two futures, the data under ``data_dir`` (its state in ``state/``),
    and the server's defaults for the rest. The engine is built from them as the server's."""
    return Settings(
        kis_app_key="",
        kis_app_secret="",
        symbols=DEMO_SYMBOLS,
        index_codes=(),
        us_symbols=(),
        etf_symbols=(),
        fee_rate=_FEE,
        data_dir=data_dir,
        state_dir=data_dir / "state",
        target_version=TARGET_VERSION,
    )


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
    settings = demo_settings(data_dir)
    gates = Gates(_admitted, _refusal)
    write_demo_macro(data_dir)  # first: Markov reads the morning's regime
    shutil.rmtree(data_dir / "bars", ignore_errors=True)
    write_demo_candles(data_dir)
    write_demo_supply(data_dir / "state")  # before the daily kind's days: Cox reads the flows
    # The trader's trades (#398) on the quotes of the days before and of the morning.
    write_demo_trader(data_dir / "state", [e for day in PAST_DAYS for e in past_events(day)] + demo_events())
    barriers = demo_barrier_models()  # the verdict's members (#676)
    for day in PAST_DAYS:
        # Each day by its own engine, ended before the daily review after the close, so
        # every algorithm stays active as on the morning.
        past = past_events(day)
        write_past_bars(data_dir, day, past)
        clock = FakeClock(datetime.combine(day, _PAST_FROM, KST) - timedelta(minutes=1))
        engine = build_engine(
            settings,
            Mode.REPLAY,  # no daily kinds those days
            PredictionStore(db),
            clock,
            _CALENDAR,
            models=barriers,
            gates=gates,  # the morning's waiting one never ran (#561)
        )  # no macro read those mornings: no regime
        await replay_into(engine, clock, past, until=datetime.combine(day, time(15, 30), KST))
        engine.store.close()
    forget_smt(db, PAST_DAYS[0])  # the API makes the 7th again from its backfilled bars (#521)
    cox = write_demo_daily(db, data_dir)  # the daily kind's last 20 days (#264)
    write_demo_windows(db, data_dir)  # the five-session kind's last 20 windows (#310)
    events = demo_events()
    clock = FakeClock(start)
    engine = build_engine(
        settings,
        Mode.DEMO,  # with the morning's 08:50 daily predictions (#264)
        PredictionStore(db),
        clock,
        _CALENDAR,
        models={cox.version.split("-")[0]: cox, **barriers},
        gates=gates,  # as the server's pre-validations (#310, #561)
    )
    await replay_into(engine, clock, events, until=_at(_END if pace is None else _PACED_FROM))
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
            answers = kind.answers_of(history, _FEE, _CALENDAR)
            past = tuple((d.day, a.label) for d, a in zip(history, answers, strict=True))
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


def write_demo_windows(db: Path, data_dir: Path) -> None:
    """The five-session kind's (#310) windows begun on the last ``_DAILY_SHOWN`` days before the
    morning: each start's 08:50 predictions of its baselines (``cox-5d`` makes none: its
    pre-validation was refused, ``_REFUSED``), and the answers of those ended by the 8th at their end day's
    20:35 (``DailyKind.answer_from``), as the engine would have stored them. A window's answer and
    band are read from every stored day at once: each is set from the windows ended before it
    began, so later days change neither."""
    from krx_quant.daily import Band, DailyInputs, window_end
    from krx_quant.kinds import Answer
    from krx_quant.predictions import NewKindPrediction

    kind = OPEN_TO_CLOSE_5D
    source = StoredDays(data_dir)
    store = PredictionStore(db)
    made: dict[date, list[NewKindPrediction]] = defaultdict(list)
    answered: dict[date, list[tuple[str, datetime, Answer, bool]]] = defaultdict(list)
    for symbol in DEMO_SYMBOLS:
        days = source.days(symbol)
        answers = kind.answers_of(days, _FEE, _CALENDAR)
        ends = [window_end(d.day, a) for d, a in zip(days, answers, strict=True)]
        for i in range(len(days) - _DAILY_SHOWN, len(days)):
            day, at = days[i].day, kind.predicted_at(days[i].day)
            ended = [j for j in range(i) if (end := ends[j]) is not None and end < day]
            path = answers[i].path
            band = None if path["band"] is None else Band(path["band"], path["sigma"], path["scale"])
            inputs = DailyInputs(
                symbol,
                day,
                at,
                tuple((days[j].day, answers[j].label) for j in ended),
                band,
                _CALENDAR.previous_trading_day(day),
                tuple(cast(date, ends[j]) for j in ended),
            )
            for b in kind.daily_baselines:
                forecast = b.predict(inputs)
                made[day].append(
                    NewKindPrediction(
                        symbol,
                        b.name,
                        b.version,
                        at,
                        at + timedelta(milliseconds=200),
                        None if forecast is None else kind.output(forecast),
                        "ok" if forecast is not None else "no_previous",
                        band=None if band is None else band.value,
                    )
                )
            if (end := ends[i]) is not None:
                answer = Answer(answers[i].label, answers[i].unscorable, {**path, "vi": 0})
                answered[end].append((symbol, at, answer, False))
    for day in sorted(made):
        store.add_kind_predictions(kind.name, made[day])
    for end in sorted(answered):
        store.add_kind_answers(
            kind.name, kind.answer_version, answered[end], scored_at=_at_on(end, kind.answer_from)
        )
    store.close()


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
    """A made-up KIS status at ``now``: connected, the member flow of 000660 quiet for 16 minutes,
    with one frame short of fields that morning, and the investor estimates' polls with a few
    short outages, 000660's going on (#441)."""
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
            # The 08:00 member frame short of fields (#381): one, kept raw only.
            "short_frames": 1 if feed == "H0UNMBC0" else 0,
        }
        for (feed, key), at in last.items()
    ]
    frame = now - timedelta(seconds=1)  # the last frame of any kind: the book's
    return {
        "connected": True,
        "last_frame_at": iso(frame),
        "seconds_since_frame": (now - frame).total_seconds(),
        "feeds": feeds,
        "polls": [_demo_poll(now, symbol) for symbol in DEMO_SYMBOLS],
    }


def _demo_poll(now: datetime, symbol: str) -> dict[str, object]:
    """The investor estimates' poll of ``symbol`` (every 300 s) since 06:00: each had an outage of
    two calls, and 000660 fails now, its second call in a row (#441)."""
    failing = symbol == "000660"
    errors = {"EGW00316": 2, "OPSQ1002": 2} if failing else {"EGW00316": 1, "OPSQ1002": 1}
    return {
        "feed": FOREIGN_ESTIMATE.tr_id,
        "key": symbol,
        "attempts": 54,
        "failures": sum(errors.values()),
        "errors": errors,
        "outages": 2 if failing else 1,
        "consecutive_failures": 2 if failing else 0,
        "max_consecutive_failures": 2,
        "outage_since": iso(now - timedelta(seconds=310)) if failing else None,
        "last_success_at": iso(now - timedelta(seconds=(610 if failing else 10))),
        "last_recovery_seconds": 300.0,
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
