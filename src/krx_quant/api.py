"""Engine API v1: what the Fincept screen reads, as JSON over HTTP.

Endpoints (all under ``/v1``):

- ``GET /status``: the clock, the market phase, the last bar close, pending outcomes and,
  live, the KIS connection and when each feed last delivered;
- ``GET /symbols``: the predicted symbols with their company name, logo, reference price and
  flat band now;
- ``GET /symbols/{symbol}/latest``: the predictions of the symbol's latest bar close;
- ``GET /symbols/{symbol}/predictions?limit=N&algorithm=A&before=T``: the predictions of the
  last N bar closes (default 100, at most 1000; before ``T`` if given), newest first, with
  their outcomes; ``?bar_close=T`` gives that bar close's;
- ``GET /symbols/{symbol}/bars``: today's finished one-minute bars, oldest first;
  ``?before=T&limit=N``: the last N bars before ``T`` across days (the backfill's files;
  default 400, at most 20,000);
- ``GET /symbols/{symbol}/candles?period=day|week|month|year&before=T&limit=N``: the last N
  stored candles of the period (default 400, at most 2000) that start before ``T`` (default
  now), oldest first, never the one holding today (``candles``);
- ``GET /symbols/{symbol}/supply?from=D&to=D``: the symbol's daily supply data kept (#226,
  ``supply``) on the days ``from`` to ``to`` (KST dates, both included; default the year to
  today, at most 4,000 days), oldest first: the short-sale volume, the net buys of
  individuals, institutions and foreigners, the foreign holdings and ratio, the stock lending
  and credit balances;
- ``GET /symbols/{symbol}/closing-flow``: the day's closing rebalancing demand of the symbol's
  single-stock ±2x ETFs (#305, ``closing_flow``): A·(L² − L)·r at the minute (the 15:20 one from
  the closing auction on), by structure and in all, its ratio to the 20-day median closing auction
  value, each product's row; ``GET /symbols/{symbol}/closing-flow/days?from=D&to=D``: the days
  kept (default the year to today, at most 4,000 days), each with its 15:20 demand and ratio, the
  closing auction's move, whether it went the demand's way, the next day's gap and close, and
  their summary by period and by tertile of the ratio;
- ``GET /symbols/{symbol}/special?from=D&to=D``: the special situations' windows kept (#236)
  that start on the days ``from`` to ``to`` (KST dates, both included; default the last five
  trading days to today, at most 60 days): the symbol's VIs and the whole market's circuit
  breakers and sidecars, oldest first;
- ``GET /algorithms``: each registered algorithm's card, features, current model (with what
  it trained on) and status (probation, active, bench, retired, clone) with its history, the
  meaning of each feature and model parameter, its report of the latest night's training,
  and, on probation or the bench, where it stands toward its next review (#165); and the
  baselines' and the consensus' cards;
- ``GET /algorithms/{name}``: one of them, with the models installed on the server for it;
- ``GET /leaderboard?symbol=&period=&special=``: algorithms ranked by the lower bound of
  their weakest lift over the baselines, with their stats;
- ``GET /algorithms/{name}/stats?symbol=&period=&special=``: one algorithm's stats, by
  symbol, 30-minute slot and model version, with its calibration curve;
- ``GET /cost-map?symbol=&period=&special=``: each symbol's round-trip costs, and each kind
  with a direction's breakeven hit rate beside the leaderboard's hit rates (#307,
  ``cost_map.py``), with the period base rates (``base_rates.py``);
- ``GET /consensus``: the consensus members now and the weights last fitted;
- ``GET /macro``: the latest day's market regime and macro readings (``macro.py``);
- ``GET /calendar?limit=``: the economic calendar from the start of today, as the collector
  last read it (``econ_calendar.py``);
- ``GET /stream``: a WebSocket of predictions, consensus, outcomes, the KIS status and
  status changes as they happen (``stream.py``);
- ``GET /kinds``: the kinds predicted (#179, ``kinds.py``): the direction and the others,
  each with its classes, action classes, output fields, answer version and baselines' cards;
- ``GET /symbols/{symbol}/kinds/{kind}/latest`` and
  ``GET /symbols/{symbol}/kinds/{kind}/predictions?limit=N&algorithm=A&before=T``,
  ``?bar_close=T``: a kind's predictions besides the direction's, as the direction's
  ``latest`` and ``predictions``, each with its bar close's answer, the bar close's k it
  was made with (``flat_ticks``, #200), its mid-point and tick size there (``anchor``,
  ``tick``, #210) and, for a kind with barriers, their width there (``width_ticks``, #183);
- ``GET /trader/trades?policy=V&before=T&limit=N``: the trader's last N trades (#394, default 100,
  at most 1000) entered before ``T`` if given, newest first, each with its orders and their fills,
  as the trader keeps them (``state/trader.sqlite3``, ``trader/store.py``); none before the
  trader's first. The champion's trades, or with ``policy`` that policy version's (#400: a
  challenger's paper trades too);
- ``GET /trader``: the trader's state as it last wrote it (#396): its mode and status, cash,
  position, the day's and the cumulative P&L, the loss limits' use, the pause, whether it is
  running, and the last pauses, resumes and re-arms asked for; the champion, the live gate's
  progress and the policies running with their paper records (#400, ``trader/promotion.py``);
- ``GET /trader/days?from=D&to=D``: its ledger of each trading day (#396; default the year to
  today, at most 4,000 days), oldest first;
- ``POST /trader/pause`` (``{"sell_now": false|true}``, ``Content-Type: application/json``): the
  screen's pause of the trader (#396), written into its store for it to read within a second:
  new entries, or that and a sale of the open position now. The API's only write: it neither
  resumes nor re-arms the trader, nor changes its mode or limits.
- ``GET /trader/policies?limit=N``: the trader's last N policy versions (#399, default 100, at most
  1000), newest first, each with its status (``champion``, ``challenger``, ``former``,
  ``dropped``), its cells with their θ and Kelly fraction, the cells it left out, what it was
  fitted on and its paper record (#400); and every change of the champion, a promotion's with its
  figures (``trader/policy.py``); none before the first nightly fit.
- ``GET /symbols/{symbol}/trader-marks?before=T&limit=N``: the trader's buys and sells of the
  symbol as the chart marks them (#398, ``trader/marks.py``): the last N (default 100, at most 1000)
  before ``T`` if given, newest first, each at its fill's time and price, with its trade's quantity,
  mode and cell, and a sell's exit reason and net P&L.

An algorithm is of a kind (``kind`` in ``/algorithms``). ``/leaderboard?kind=`` ranks a kind's
algorithms against its baselines (the direction's by default, as before); an algorithm's
``/stats`` are of its kind.

``symbol`` is a symbol or ``all`` (default), ``period`` is ``today`` (default), ``5d`` or
``20d`` (the latest days with predictions), and ``special`` is ``exclude`` (default) or
``include`` (predictions with a VI or circuit breaker in their window). Every prediction is
scored on its bar close's touch label (#170). For one release a screen from before still
sends ``basis`` (``a`` or ``b``): it is accepted, ignored and echoed back as sent, so that
screen, which asks again until the answer's ``basis`` is its own, is answered once
(``LEGACY_BASES``). Without it the answer has no ``basis``.

Times are ISO 8601 in KST. On the server, compose publishes it on the Tailscale address only;
it has no authentication, so the trader's pause is its one write.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import partial
from pathlib import Path
from time import monotonic
from typing import Any, Literal

from aiohttp import web

from krx_quant import loki
from krx_quant.algorithms import Algorithm, KindAlgorithm
from krx_quant.backfill import MINUTE_BARS_TR, read_bars, stock_bars_path, stored_bars
from krx_quant.base_rates import read_base_rates
from krx_quant.briefing import Sources as BriefingSources
from krx_quant.briefing import briefing, day_predictions, default_day
from krx_quant.candles import PERIODS as CANDLE_PERIODS
from krx_quant.candles import stored_candles
from krx_quant.closing_flow import days_answer, demand_moment, read_days, read_demand, today_answer
from krx_quant.consensus import NAMES, slot
from krx_quant.consensus import cards as consensus_cards
from krx_quant.contracts import USD
from krx_quant.cost_map import SPREAD_TICKS, cost_card, direction_row, kind_row
from krx_quant.daily import DailyAlgorithm, DailyKind, StoredDays
from krx_quant.daily_features import DAILY_FEATURES
from krx_quant.econ_calendar import calendar_answer, read_latest
from krx_quant.engine import PredictionEngine
from krx_quant.features import FEATURE_MEANINGS
from krx_quant.feeds.base import KST, Row, kst_day
from krx_quant.flow import Window as FlowWindow
from krx_quant.flow import flow as flow_answer
from krx_quant.kind_stats import KindEntry, scored_of_kind
from krx_quant.kind_stats import entry as kind_entry
from krx_quant.kind_stats import entry_json as kind_entry_json
from krx_quant.kind_stats import leaderboard as kind_leaderboard
from krx_quant.kind_stats import split as kind_split
from krx_quant.kinds import DIRECTION, DIRECTION_KIND, KindScored
from krx_quant.kinds import Kind as PredictionKind
from krx_quant.lifecycle import Stage
from krx_quant.macro import latest_macro, macro_json, reading_before
from krx_quant.market_calendar import MarketCalendar
from krx_quant.model_shelf import Shelf
from krx_quant.names import logo_url
from krx_quant.net_assets import read_net_assets
from krx_quant.open_to_close import OPEN_TO_CLOSE
from krx_quant.predictions import Excluded, SimulationRecord, iso
from krx_quant.pricing import flat_ticks, tick_size
from krx_quant.reference import Bar
from krx_quant.skill import Skill, calls_by_day, day_kappa, skill
from krx_quant.stats import (
    DEFAULT_MIN_SAMPLE,
    CalibrationBin,
    Entry,
    Lift,
    Scored,
    entry,
    entry_json,
    interval_json,
    leaderboard,
    on_target,
    scored_by_algorithm,
    split,
)
from krx_quant.stream import EventStream, LiveStatus, kind_prediction_json, kind_target, prediction_json
from krx_quant.supply import FIELDS as SUPPLY_FIELDS
from krx_quant.supply import stored_supply
from krx_quant.trader.marks import read_marks
from krx_quant.trader.policy import read_policies
from krx_quant.trader.store import DB_NAME as TRADER_DB
from krx_quant.trader.store import read_days as read_trader_days
from krx_quant.trader.store import read_trader, read_trades, request_pause

ENGINE_KEY = web.AppKey("engine", PredictionEngine)
# live: the collector; demo: `krx-quant demo-engine`; replay: tests.
Mode = Literal["live", "demo", "replay"]
MODE_KEY = web.AppKey("mode", str)
LIVE_STATUS_KEY: web.AppKey[LiveStatus] = web.AppKey("live_status")
# The symbols' company names (#250): the collector fills them in after the API starts.
NAMES_KEY: web.AppKey[Mapping[str, str]] = web.AppKey("names")

# Readable as is (curl, logs): indented, and Korean as UTF-8 rather than \u escapes.
_dumps = partial(json.dumps, ensure_ascii=False, indent=2)

MIN_SAMPLE_KEY = web.AppKey("min_sample", int)
DATA_DIR_KEY: web.AppKey[Path | None] = web.AppKey("data_dir")
STATE_DIR_KEY: web.AppKey[Path | None] = web.AppKey("state_dir")
SHELF_KEY: web.AppKey[Shelf | None] = web.AppKey("shelf")
# /v1/macro is read again at most this often: it changes only with the morning's reads.
MACRO_SECONDS = 30.0
MACRO_CACHE_KEY = web.AppKey("macro_cache", dict)
USD_CLOSE_CACHE_KEY = web.AppKey("usd_close_cache", dict)
# /v1/calendar's kept answer is read again at most this often: the collector rewrites it every
# few minutes.
CALENDAR_SECONDS = 30.0
CALENDAR_CACHE_KEY = web.AppKey("calendar_cache", dict)
CALENDAR_LIMIT = 100  # events by default
MAX_CALENDAR_LIMIT = 1000
# /v1/symbols/{symbol}/flow (#306): today's answer is read again at most this often (the feeds'
# files are written every 30 s), a past day's this often; at most this many are kept.
FLOW_SECONDS = 30.0
FLOW_PAST_SECONDS = 600.0
FLOW_CACHE_KEY = web.AppKey("flow_cache", dict)
# /v1/briefing (#304) is read again at most this often, or once the store changes: its files
# change with the pre-market's trades and book.
BRIEFING_SECONDS = 20.0
BRIEFING_CACHE_KEY = web.AppKey("briefing_cache", dict)
STATS_CACHE_KEY = web.AppKey("stats_cache", dict)
PERIODS = {"today": 1, "5d": 5, "20d": 20}  # days with predictions
# The scoring bases a screen from before #170 still sends, accepted and echoed back for one release.
LEGACY_BASES = ("a", "b")
# Results kept per question until the store changes (the screen asks the same often).
_CACHED_QUESTIONS = 64

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000
# Bars before a time (#108): a trading day is up to 720 one-minute bars (08:00–20:00).
DEFAULT_BARS = 400
# The chart reads 300 bars of its view at a time as one-minute bars (#239): an hour view's
# 300 are 18,000, about 25 trading days.
MAX_BARS = 20_000
# Day candles (#111): the chart reads a screenful and scrolls back for more.
DEFAULT_CANDLES = 400
MAX_CANDLES = 2000
# Special windows (#236): the trading days asked for by default, and the most days one question spans.
DEFAULT_SPECIAL_DAYS = 5
MAX_SPECIAL_DAYS = 60
# Daily supply (#226): the days asked for by default, and the most one question spans (the ten
# years kept, ``supply.HISTORY_YEARS``, and more: a year view asks for all of it).
DEFAULT_SUPPLY_DAYS = 365
MAX_SUPPLY_DAYS = 4000
# The closing flow's days (#305): the days asked for by default, and the most one question spans.
DEFAULT_CLOSING_DAYS = 365
MAX_CLOSING_DAYS = 4000
# The trader's days (#396): the days asked for by default, and the most one question spans.
DEFAULT_TRADER_DAYS = 365
MAX_TRADER_DAYS = 4000
# The KRX days (``auctions.days_of``) the closing flow's previous close is read from, re-read
# when their files change.
KRX_DAYS_KEY: web.AppKey[StoredDays | None] = web.AppKey("krx_days")


def create_app(
    engine: PredictionEngine,
    *,
    mode: Mode = "replay",
    live_status: LiveStatus | None = None,
    min_sample: int = DEFAULT_MIN_SAMPLE,
    data_dir: Path | None = None,
    shelf: Shelf | None = None,
    stream_poll_seconds: float = 1.0,
    names: Mapping[str, str] | None = None,
    state_dir: Path | None = None,
) -> web.Application:
    """The Engine API of ``engine``. ``data_dir`` holds the macro readings, ``shelf`` the
    models installed on the server and ``state_dir`` the daily supply data (#226); without
    them there is no macro, no model history and no supply.
    Live and demo show the KIS status: they need ``live_status``. ``names`` are the symbols'
    company names, read as they are when asked (the collector fills them in later).
    """
    if mode in ("live", "demo") and live_status is None:
        raise ValueError(f"the {mode} Engine API shows the kis status: give it live_status")
    app = web.Application()
    app[ENGINE_KEY] = engine
    app[MODE_KEY] = mode
    app[MIN_SAMPLE_KEY] = min_sample
    app[STATS_CACHE_KEY] = {}
    app[DATA_DIR_KEY] = data_dir
    app[STATE_DIR_KEY] = state_dir
    app[SHELF_KEY] = shelf
    app[MACRO_CACHE_KEY] = {}
    app[USD_CLOSE_CACHE_KEY] = {}
    app[CALENDAR_CACHE_KEY] = {}
    app[BRIEFING_CACHE_KEY] = {}
    app[FLOW_CACHE_KEY] = {}
    app[KRX_DAYS_KEY] = None if data_dir is None else StoredDays(data_dir)
    app[NAMES_KEY] = {} if names is None else names
    stream = EventStream(engine, live_status, stream_poll_seconds)

    async def streaming(app: web.Application) -> AsyncIterator[None]:
        engine.listeners.append(stream.changed)
        polling = asyncio.create_task(stream.run())
        yield
        engine.listeners.remove(stream.changed)
        polling.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await polling

    async def closing(app: web.Application) -> None:
        await stream.close()

    app.cleanup_ctx.append(streaming)
    app.on_shutdown.append(closing)
    if live_status is not None:
        app[LIVE_STATUS_KEY] = live_status
    app.router.add_get("/v1/status", _status)
    app.router.add_get("/v1/symbols", _symbols)
    app.router.add_get("/v1/symbols/{symbol}/latest", _latest)
    app.router.add_get("/v1/symbols/{symbol}/predictions", _predictions)
    app.router.add_get("/v1/symbols/{symbol}/bars", _bars)
    app.router.add_get("/v1/symbols/{symbol}/candles", _candles)
    app.router.add_get("/v1/symbols/{symbol}/special", _special)
    app.router.add_get("/v1/symbols/{symbol}/supply", _supply)
    app.router.add_get("/v1/symbols/{symbol}/flow", _flow)
    app.router.add_get("/v1/symbols/{symbol}/closing-flow", _closing_flow)
    app.router.add_get("/v1/symbols/{symbol}/closing-flow/days", _closing_flow_days)
    app.router.add_get("/v1/algorithms", _algorithms)
    app.router.add_get("/v1/algorithms/{name}", _algorithm)
    app.router.add_get("/v1/leaderboard", _leaderboard)
    app.router.add_get("/v1/algorithms/{name}/stats", _algorithm_stats)
    app.router.add_get("/v1/cost-map", _cost_map)
    app.router.add_get("/v1/consensus", _consensus)
    app.router.add_get("/v1/macro", _macro)
    app.router.add_get("/v1/calendar", _calendar)
    app.router.add_get("/v1/briefing", _briefing)
    app.router.add_get("/v1/stream", stream.handle)
    app.router.add_get("/v1/kinds", _kinds)
    app.router.add_get("/v1/symbols/{symbol}/kinds/{kind}/latest", _kind_latest)
    app.router.add_get("/v1/symbols/{symbol}/kinds/{kind}/predictions", _kind_predictions)
    app.router.add_get("/v1/trader/trades", _trader_trades)
    app.router.add_get("/v1/trader", _trader)
    app.router.add_get("/v1/trader/days", _trader_days)
    app.router.add_post("/v1/trader/pause", _trader_pause)
    app.router.add_get("/v1/trader/policies", _trader_policies)
    app.router.add_get("/v1/symbols/{symbol}/trader-marks", _trader_marks)
    return app


async def _status(request: web.Request) -> web.Response:
    engine = request.app[ENGINE_KEY]
    now = engine.clock.now()
    session = engine.calendar.session(kst_day(now))
    body: dict[str, Any] = {
        "mode": request.app[MODE_KEY],
        "now": iso(now),
        "commit": engine.commit,
        "started_at": iso(engine.started_at),
        "phase": engine.calendar.phase(now).value,
        "session": None if session is None else {"open": iso(session.open), "close": iso(session.close)},
        "last_bar_close": engine.store.last_bar_close(),
        "pending_outcomes": engine.store.pending_count(),
        # VIs, circuit breakers and sidecars known today; symbol null is the whole market.
        "special": [
            {"kind": w.kind.value, "symbol": w.symbol, "start": iso(w.start), "end": iso(w.end)}
            for w in engine.special_today()
        ],
    }
    # The latest night's training and whether its promotions run (swap.py); null before any.
    training = engine.training
    if training is not None and training["finalized_at"] is not None:
        # Finalized where the night ran: shown in KST like every time here. None before any
        # night's list, when only the nights not applied are shown (#355).
        training = {**training, "finalized_at": iso(datetime.fromisoformat(training["finalized_at"]))}
    body["training"] = training
    cpu = engine.cpu_use()
    body["cpu"] = {
        "budget_seconds": cpu.budget.cpu,
        "algorithm_seconds": cpu.budget.per_algorithm,
        "expected_seconds": cpu.expected_seconds,
        "usage": cpu.usage,
        "algorithms": dict(cpu.expected),
        "skipped_today": cpu.skipped_today,
    }
    body["usd_futures"] = await _usd_futures(request)
    live_status = request.app.get(LIVE_STATUS_KEY)
    if live_status is not None:
        body["kis"] = live_status()
    if request.app[MODE_KEY] == "live":  # the collector's log lines' copy in Loki (#358)
        body["loki"] = loki.status_json()
    return _json(body)


async def _symbols(request: web.Request) -> web.Response:
    engine = request.app[ENGINE_KEY]
    names = request.app[NAMES_KEY]
    symbols = []
    for symbol in engine.symbols:
        mid = engine.mid(symbol)
        symbols.append(
            {
                "symbol": symbol,
                "name": names.get(symbol),
                "logo_url": logo_url(symbol),
                "mid": mid,
                "tick": tick_size(mid) if mid is not None else None,
                "flat_ticks": flat_ticks(mid, engine.fee_rate) if mid is not None else None,
            }
        )
    return _json({"symbols": symbols})


async def _latest(request: web.Request) -> web.Response:
    engine, symbol = _engine_and_symbol(request)
    rows = engine.store.latest(symbol)
    return _json(
        {
            "symbol": symbol,
            "bar_close": rows[0]["bar_close"] if rows else None,
            "predictions": [prediction_json(r) for r in rows],
        }
    )


async def _predictions(request: web.Request) -> web.Response:
    engine, symbol = _engine_and_symbol(request)
    if (bar_close := _time_query(request, "bar_close")) is not None:
        # One bar close's every prediction (#107): what the models said that minute.
        rows = engine.store.at_close(symbol, bar_close, request.query.get("algorithm"))
        return _json({"symbol": symbol, "predictions": [prediction_json(r) for r in rows]})
    limit = _limit_query(request, DEFAULT_LIMIT, MAX_LIMIT)
    before = _time_query(request, "before")  # the chart scrolled back (#108)
    rows = engine.store.recent(symbol, limit, request.query.get("algorithm"), before)
    return _json({"symbol": symbol, "predictions": [prediction_json(r) for r in rows]})


async def _bars(request: web.Request) -> web.Response:
    engine, symbol = _engine_and_symbol(request)
    today = engine.bars(symbol)
    if "before" not in request.query and "limit" not in request.query:
        return _json({"symbol": symbol, "bars": [_bar_json(b) for b in today]})
    # The chart scrolled back (#108): the last bars before a time, across days.
    before = _time_query(request, "before") or engine.clock.now()
    limit = _limit_query(request, DEFAULT_BARS, MAX_BARS)
    data_dir = request.app[DATA_DIR_KEY]
    day = kst_day(engine.clock.now())
    recent: list[dict[str, Any]] = []
    stored_before = before
    if data_dir is None or not stock_bars_path(data_dir, day, symbol).exists():
        # Today from memory until the 20:30 backfill stores the whole day, which also
        # fills what a restart during the day left out of memory.
        recent = [_bar_json(b) for b in today if b.start < before]
        stored_before = min(before, datetime.combine(day, time(), KST))
    older = []
    if data_dir is not None and len(recent) < limit:  # files: off the event loop
        older = await asyncio.to_thread(stored_bars, data_dir, symbol, stored_before, limit - len(recent))
    stored = [
        {
            "start": iso(r["bar_time"]),
            "open": r["open"],
            "high": r["high"],
            "low": r["low"],
            "close": r["close"],
            "volume": r["volume"],
            "trades": None,  # the backfill's bars do not count trades
        }
        for r in older
    ]
    return _json({"symbol": symbol, "bars": [*stored, *recent][-limit:]})


async def _candles(request: web.Request) -> web.Response:
    engine, symbol = _engine_and_symbol(request)
    period = request.query.get("period")
    if period not in CANDLE_PERIODS:
        raise _error(web.HTTPBadRequest, f"period must be one of {', '.join(CANDLE_PERIODS)}, got {period!r}")
    now = engine.clock.now()
    before = _time_query(request, "before") or now
    limit = _limit_query(request, DEFAULT_CANDLES, MAX_CANDLES)
    data_dir = request.app[DATA_DIR_KEY]
    venue: str | None = None
    rows: list[Row] = []
    if data_dir is not None:  # a file: off the event loop
        venue, rows = await asyncio.to_thread(
            stored_candles, data_dir, period, symbol, before, limit, kst_day(now)
        )
    candles = [
        {
            "date": r["date"].isoformat(),
            "open": r["open"],
            "high": r["high"],
            "low": r["low"],
            "close": r["close"],
            "volume": r["volume"],
        }
        for r in rows
    ]
    return _json({"symbol": symbol, "period": period, "venue": venue, "candles": candles})


async def _special(request: web.Request) -> web.Response:
    """The special windows kept (#236) that start on the days asked for: the symbol's VIs and
    the whole market's circuit breakers and sidecars, oldest first."""
    engine, symbol = _engine_and_symbol(request)
    to = _day_query(request, "to") or kst_day(engine.clock.now())
    since = _day_query(request, "from") or _trading_days_back(engine.calendar, to, DEFAULT_SPECIAL_DAYS)
    if since > to:
        raise _error(web.HTTPBadRequest, f"from must not be after to, got {since} and {to}")
    if (span := (to - since).days + 1) > MAX_SPECIAL_DAYS:
        raise _error(web.HTTPBadRequest, f"from and to span at most {MAX_SPECIAL_DAYS} days, got {span}")
    rows = engine.store.special_windows(
        symbol, datetime.combine(since, time(), KST), datetime.combine(to + timedelta(days=1), time(), KST)
    )
    fields = ("kind", "symbol", "start", "end", "detail")
    windows = [{f: r[f] for f in fields} for r in rows]
    return _json({"symbol": symbol, "from": since.isoformat(), "to": to.isoformat(), "special": windows})


async def _flow(request: web.Request) -> web.Response:
    """A symbol's intraday flows of a day (#306, ``flow.flow``); by default today's, or the last
    trading day's on a closed day. 404 for a day KRX did not trade."""
    engine, symbol = _engine_and_symbol(request)
    now = engine.clock.now()
    today = kst_day(now)
    day = _day_query(request, "day")
    if day is None:
        day = today if engine.calendar.is_trading_day(today) else engine.calendar.previous_trading_day(today)
    session = engine.calendar.session(day)
    if session is None:
        raise _error(web.HTTPNotFound, f"{day} is not a trading day")
    cache: dict[tuple[str, date], tuple[float, int, dict[str, Any]]] = request.app[FLOW_CACHE_KEY]
    kept = cache.get((symbol, day))
    ttl = FLOW_SECONDS if day >= today else FLOW_PAST_SECONDS
    if kept is not None and monotonic() - kept[0] < ttl and kept[1] == engine.store.version:
        return _json(kept[2])
    data_dir = request.app[DATA_DIR_KEY]
    previous_day = engine.calendar.previous_trading_day(day)
    before = engine.calendar.session(previous_day)
    start = datetime.combine(day, time(), KST)
    rows = engine.store.special_windows(symbol, start, start + timedelta(days=1))  # on the store's thread
    special = [{f: r[f] for f in ("kind", "symbol", "start", "end", "detail")} for r in rows]
    stored = data_dir is not None and stock_bars_path(data_dir, day, symbol).exists()
    memory = [(b.start, b.close) for b in engine.bars(symbol)] if day == today and not stored else []

    def answer() -> dict[str, Any]:
        prices = memory
        if not prices and data_dir is not None:
            prices = [(b.start, b.close) for b in read_bars(data_dir, day, MINUTE_BARS_TR, symbol) or []]
        window = FlowWindow(day, session.open, session.close)
        previous = None if before is None else FlowWindow(previous_day, before.open, before.close)
        state_dir = request.app[STATE_DIR_KEY]
        return flow_answer(data_dir, state_dir, symbol, window, previous, prices, now, special)

    version = engine.store.version
    body = await asyncio.to_thread(answer)
    if len(cache) >= _CACHED_QUESTIONS:
        cache.clear()
    cache[(symbol, day)] = (monotonic(), version, body)
    return _json(body)


async def _closing_flow(request: web.Request) -> web.Response:
    """The day's closing rebalancing demand of the symbol's single-stock ETFs (#305,
    ``closing_flow.today_answer``): the minute's in continuous trading, else the 15:20 one."""
    engine, symbol = _engine_and_symbol(request)
    now = engine.clock.now()
    day, at = demand_moment(engine.calendar, now)
    price = None if at is None else engine.mid_at(symbol, at)  # the engine's quotes: on its thread
    state_dir, krx_days = request.app[STATE_DIR_KEY], request.app[KRX_DAYS_KEY]
    names = request.app[NAMES_KEY]

    def answer() -> dict[str, Any]:
        days = [] if krx_days is None else krx_days.days(symbol)
        net = [] if state_dir is None else read_net_assets(state_dir)
        demands = [] if state_dir is None else read_demand(state_dir)
        kept = None if state_dir is None else read_days(state_dir)
        return today_answer(symbol, day, at, price, now, engine.calendar, days, net, demands, kept, names)

    return _json(await asyncio.to_thread(answer))


async def _closing_flow_days(request: web.Request) -> web.Response:
    """The closing flow's days kept (#305, ``closing_flow.days_answer``) from ``from`` to ``to``,
    with their summary."""
    engine, symbol = _engine_and_symbol(request)
    to = _day_query(request, "to") or kst_day(engine.clock.now())
    since = _day_query(request, "from") or to - timedelta(days=DEFAULT_CLOSING_DAYS - 1)
    if since > to:
        raise _error(web.HTTPBadRequest, f"from must not be after to, got {since} and {to}")
    if (span := (to - since).days + 1) > MAX_CLOSING_DAYS:
        raise _error(web.HTTPBadRequest, f"from and to span at most {MAX_CLOSING_DAYS} days, got {span}")
    state_dir = request.app[STATE_DIR_KEY]

    def answer() -> dict[str, Any]:
        return days_answer(symbol, None if state_dir is None else read_days(state_dir), since, to)

    return _json(await asyncio.to_thread(answer))


async def _supply(request: web.Request) -> web.Response:
    """The symbol's daily supply data kept (#226) on the days asked for, oldest first."""
    engine, symbol = _engine_and_symbol(request)
    to = _day_query(request, "to") or kst_day(engine.clock.now())
    since = _day_query(request, "from") or to - timedelta(days=DEFAULT_SUPPLY_DAYS - 1)
    if since > to:
        raise _error(web.HTTPBadRequest, f"from must not be after to, got {since} and {to}")
    if (span := (to - since).days + 1) > MAX_SUPPLY_DAYS:
        raise _error(web.HTTPBadRequest, f"from and to span at most {MAX_SUPPLY_DAYS} days, got {span}")
    state_dir = request.app[STATE_DIR_KEY]
    rows: list[Row] = []
    if state_dir is not None:  # files: off the event loop
        rows = await asyncio.to_thread(stored_supply, state_dir, symbol, since, to)
    days = [{"date": r["date"].isoformat(), **{f: r[f] for f in SUPPLY_FIELDS}} for r in rows]
    return _json({"symbol": symbol, "from": since.isoformat(), "to": to.isoformat(), "days": days})


def _day_query(request: web.Request, name: str) -> date | None:
    """The query's date ``name`` (YYYY-MM-DD), None if absent; 400 if it is not one."""
    if (raw := request.query.get(name)) is None:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        with contextlib.suppress(ValueError):
            return date.fromisoformat(raw)
    raise _error(web.HTTPBadRequest, f"{name} must be a date YYYY-MM-DD, got {raw!r}")


def _trading_days_back(calendar: MarketCalendar, end: date, days: int) -> date:
    """The first of the last ``days`` trading days up to ``end`` (``end`` itself if it trades)."""
    day, found = end, 0
    while True:
        if calendar.is_trading_day(day):
            found += 1
            if found == days:
                return day
        day -= timedelta(days=1)


def _bar_json(b: Bar) -> dict[str, Any]:
    return {
        "start": iso(b.start),
        "open": b.open,
        "high": b.high,
        "low": b.low,
        "close": b.close,
        "volume": b.volume,
        "trades": b.trades,
    }


def _time_query(request: web.Request, name: str) -> datetime | None:
    """The query's ISO 8601 time ``name`` in KST, None if absent; 400 without its offset."""
    if (raw := request.query.get(name)) is None:
        return None
    at: datetime | None = None
    try:
        parsed = datetime.fromisoformat(raw)
        # In KST: a time at the very edge of the calendar (9999-12-31T23:59-12:00) overflows.
        if parsed.tzinfo is not None:
            at = parsed.astimezone(KST)
    except (ValueError, OverflowError):
        pass
    if at is None:
        raise _error(web.HTTPBadRequest, f"{name} must be an ISO 8601 time with its offset, got {raw!r}")
    return at


def _limit_query(request: web.Request, default: int, most: int) -> int:
    raw = request.query.get("limit", str(default))
    try:
        limit = int(raw)
    except ValueError:
        limit = 0
    if not 1 <= limit <= most:
        raise _error(web.HTTPBadRequest, f"limit must be 1 to {most}, got {raw!r}")
    return limit


async def _algorithms(request: web.Request) -> web.Response:
    engine = request.app[ENGINE_KEY]
    stages = await engine.stages()
    simulations = engine.store.simulations()
    running: list[Algorithm | KindAlgorithm | DailyAlgorithm] = [
        *engine.algorithms,
        *engine.kind_algorithms,
        *engine.daily_algorithms,
    ]
    return _json(
        {
            "algorithms": [_algorithm_json(engine, a, stages, simulations) for a in running],
            "baselines": [{"name": b.name, "card": b.card.as_dict()} for b in engine.baselines],
            "consensus": [
                {"name": name, "card": card.as_dict()}
                for name, card in consensus_cards(engine.consensus_timing).items()
            ],
        }
    )


async def _algorithm(request: web.Request) -> web.Response:
    engine = request.app[ENGINE_KEY]
    name = request.match_info["name"]
    running: list[Algorithm | KindAlgorithm | DailyAlgorithm] = [
        *engine.algorithms,
        *engine.kind_algorithms,
        *engine.daily_algorithms,
    ]
    algorithm = next((a for a in running if a.name == name), None)
    if algorithm is None:
        raise _error(web.HTTPNotFound, f"unknown algorithm {name!r}")
    shelf = request.app[SHELF_KEY]
    installed = shelf.history(name) if shelf is not None else []
    history = [
        {"at": iso(datetime.fromisoformat(e["at"])), "version": e["version"], "why": e["why"]}
        for e in installed
    ]
    stages = await engine.stages()
    simulations = engine.store.simulations()
    return _json({**_algorithm_json(engine, algorithm, stages, simulations), "model_history": history})


def _algorithm_json(
    engine: PredictionEngine,
    a: Algorithm | KindAlgorithm | DailyAlgorithm,
    stages: Mapping[str, Stage],
    simulations: Mapping[str, SimulationRecord],
) -> dict[str, Any]:
    return {
        "name": a.name,
        "kind": str(getattr(a, "kind", DIRECTION)),  # the kind it predicts (#179)
        "card": a.card.as_dict(),
        "features": list(a.features),
        # A daily kind's algorithm's are the daily features' (#264).
        "feature_meanings": {
            f: (FEATURE_MEANINGS[f] if f in FEATURE_MEANINGS else DAILY_FEATURES[f].meaning).as_dict()
            for f in a.features
        },
        "training_days": a.training_days,
        "model": engine.models[a.name].as_dict(),
        "param_meanings": {p: m.as_dict() for p, m in a.param_meanings.items()},
        "status": _status_json(engine, a.name),
        "validation": _validation_json(engine, a.name),
        "stage": _stage_json(stages.get(a.name)),
        "simulation": _simulation_json(simulations.get(a.name)),
    }


def _simulation_json(record: SimulationRecord | None) -> dict[str, Any] | None:
    """An algorithm's latest simulated probation (#298), as stored: its days, how it was trained,
    and the review it made at once; null without one."""
    if record is None:
        return None
    detail = record.detail
    return {
        "made_at": iso(record.made_at),
        "probation_began": iso(record.stint),
        "cadence": record.cadence,
        "days": len(record.days),
        "first_day": record.days[0].isoformat() if record.days else None,
        "last_day": record.days[-1].isoformat() if record.days else None,
        "training": detail.get("training"),
        "review": detail.get("review"),
        "why": detail.get("why"),
    }


def _validation_json(engine: PredictionEngine, name: str) -> dict[str, Any] | None:
    """``name``'s report of the latest night (#165), its values as stored; None without one."""
    report = engine.night_reports.get(name)
    if report is None or engine.training is None:
        return None
    champion, challenger = report.get("champion"), report.get("challenger")
    return {
        "day": report.get("day", engine.training["day"]),
        "validation_days": report.get("validation_days", []),
        "samples": (report.get("samples") or {}).get("validation"),
        "challenger": None
        if challenger is None
        else {"version": challenger["version"], "loss": challenger.get("loss")},
        "champion": None
        if champion is None
        else {
            "version": champion["version"],
            "loss": champion.get("loss"),
            "trained_on_validation": champion.get("trained_on_validation"),
        },
        "promoted": report["promoted"],
        "reason": report["reason"],
    }


def _stage_json(stage: Stage | None) -> dict[str, Any] | None:
    """Where an algorithm on probation or on the bench stands toward its next review (#165)."""
    if stage is None:
        return None
    weakest = None
    if stage.weakest is not None:
        baseline, lift = stage.weakest
        weakest = {"baseline": baseline, "n": lift.n, "value": lift.value, "low": lift.low, "high": lift.high}
    return {
        "status": stage.status.value,
        "began": stage.began.isoformat(),
        # null while waiting for a model trained on the touch target (#170): nothing counted yet
        "counted_from": None if stage.counted_from is None else stage.counted_from.isoformat(),
        "days": stage.days,
        "review_days": list(stage.review_days),
        "next_review": stage.next_review,
        "next_review_on": None if stage.next_review_on is None else stage.next_review_on.isoformat(),
        "lift_days": stage.lift_days,
        "level": stage.level,
        "weakest": weakest,
        "enough": stage.enough,
        # Of ``days``, those simulated (#298): the probation's first, judged at once.
        "simulated": stage.simulated,
    }


async def _consensus(request: web.Request) -> web.Response:
    engine = request.app[ENGINE_KEY]
    weights = engine.weights
    by_symbol: dict[str, dict[str, dict[str, float]]] = {}
    for (symbol, slot_name), cell in sorted(weights.cells.items()):
        by_symbol.setdefault(symbol, {})[slot_name] = dict(cell)
    return _json(
        {
            "names": list(NAMES),
            "version": weights.version,
            "computed_at": iso(weights.computed_at) if weights.computed_at else None,
            "members": engine.members(),
            "fast_seconds": engine.consensus_timing.fast_seconds,
            "weights": by_symbol,
        }
    )


async def _macro(request: web.Request) -> web.Response:
    return _json(await _macro_now(request))


async def _macro_now(request: web.Request) -> dict[str, Any]:
    """``/v1/macro``'s answer, read again at most every ``MACRO_SECONDS``."""
    engine = request.app[ENGINE_KEY]
    cache: dict[date, tuple[float, dict[str, Any]]] = request.app[MACRO_CACHE_KEY]
    today = kst_day(engine.clock.now())
    cached = cache.get(today)
    if cached is None or monotonic() - cached[0] >= MACRO_SECONDS:
        data_dir = request.app[DATA_DIR_KEY]
        latest = None if data_dir is None else await asyncio.to_thread(latest_macro, data_dir, today)
        cache.clear()
        cached = cache[today] = (monotonic(), macro_json(latest))
    return cached[1]


async def _calendar(request: web.Request) -> web.Response:
    limit = _limit_query(request, CALENDAR_LIMIT, MAX_CALENDAR_LIMIT)
    engine = request.app[ENGINE_KEY]
    cache: dict[str, tuple[float, dict[str, Any] | None]] = request.app[CALENDAR_CACHE_KEY]
    cached = cache.get("kept")
    if cached is None or monotonic() - cached[0] >= CALENDAR_SECONDS:
        data_dir = request.app[DATA_DIR_KEY]
        kept = None if data_dir is None else await asyncio.to_thread(read_latest, data_dir)
        cached = cache["kept"] = (monotonic(), kept)
    return _json(calendar_answer(cached[1], engine.clock.now(), limit))


async def _briefing(request: web.Request) -> web.Response:
    """The pre-open briefing of ``day`` (#304, ``briefing.briefing``); by default today's, or the
    next trading day's on a closed day."""
    engine = request.app[ENGINE_KEY]
    now = engine.clock.now()
    day = _day_query(request, "day") or default_day(engine.calendar, now)
    cache: dict[date, tuple[float, int, dict[str, Any]]] = request.app[BRIEFING_CACHE_KEY]
    cached = cache.get(day)
    if cached is None or monotonic() - cached[0] >= BRIEFING_SECONDS or cached[1] != engine.store.version:
        kind = engine.kind_named(OPEN_TO_CLOSE.name)
        sources = BriefingSources(
            data_dir=request.app[DATA_DIR_KEY],
            state_dir=request.app[STATE_DIR_KEY],
            store=engine.store,
            calendar=engine.calendar,
            symbols=tuple(engine.symbols),
            open_to_close=kind if isinstance(kind, DailyKind) else None,
            now=now,
        )
        version = engine.store.version
        predictions = day_predictions(sources, day)  # the store is read on its own thread
        body = await asyncio.to_thread(briefing, sources, day, predictions)
        if len(cache) >= _CACHED_QUESTIONS:
            cache.clear()
        cached = cache[day] = (monotonic(), version, body)
    return _json(cached[2])


async def _usd_futures(request: web.Request) -> dict[str, Any] | None:
    """The USD futures' latest trade today against the previous close: today's macro read
    before the open (07:30), when it is the previous trading day's close (not an older one a
    missed backfill left). None before today's first trade."""
    engine = request.app[ENGINE_KEY]
    latest = engine.latest_lead(USD.series)
    if latest is None:
        return None
    price, traded_at = latest
    previous = await _previous_usd_close(request, kst_day(engine.clock.now()))
    change = None if not previous else (price - previous) / previous * 100
    return {"price": price, "at": iso(traded_at), "previous_close": previous, "change_pct": change}


async def _previous_usd_close(request: web.Request, today: date) -> float | None:
    """The USD futures' previous close as read before ``today``'s open, read again at most
    every ``MACRO_SECONDS``."""
    engine = request.app[ENGINE_KEY]
    cache: dict[date, tuple[float, float | None]] = request.app[USD_CLOSE_CACHE_KEY]
    cached = cache.get(today)
    if cached is None or monotonic() - cached[0] >= MACRO_SECONDS:
        data_dir, session = request.app[DATA_DIR_KEY], engine.calendar.session(today)
        reading = None
        if data_dir is not None and session is not None:
            reading = await asyncio.to_thread(reading_before, data_dir, today, "usdf", session.open)
        before = engine.calendar.previous_trading_day(today)
        close = reading["value"] if reading is not None and reading["as_of"] == before else None
        cache.clear()
        cached = cache[today] = (monotonic(), close)
    return cached[1]


def _status_json(engine: PredictionEngine, name: str) -> dict[str, Any]:
    history = engine.store.status_history(name)
    return {
        "status": history[-1]["status"] if history else None,
        "since": history[-1]["at"] if history else None,
        "history": [
            {
                "at": h["at"],
                "from": h["previous"],
                "to": h["status"],
                "reason": h["reason"],
                "simulated": h["simulated"],  # its review counted simulated days (#298)
            }
            for h in history
        ],
    }


def _engine_and_symbol(request: web.Request) -> tuple[PredictionEngine, str]:
    engine = request.app[ENGINE_KEY]
    symbol = request.match_info["symbol"]
    if symbol not in engine.symbols:
        raise _error(web.HTTPNotFound, f"unknown symbol {symbol!r}")
    return engine, symbol


@dataclass(frozen=True)
class _Query:
    symbol: str | None  # None: every symbol
    period: str
    days: tuple[date, ...]
    special: bool

    def describe(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol or "all",
            "period": self.period,
            "days": [d.isoformat() for d in self.days],
            "special": "include" if self.special else "exclude",
        }


def _query(request: web.Request, engine: PredictionEngine, kind: PredictionKind | None = None) -> _Query:
    """The query's values; its days, those of the direction's predictions, or of ``kind``'s."""
    q = request.query
    symbol = q.get("symbol", "all")
    period = q.get("period", "today")
    special = q.get("special", "exclude")
    if symbol != "all" and symbol not in engine.symbols:
        raise _error(web.HTTPBadRequest, f"symbol must be all or one of {', '.join(engine.symbols)}")
    if period not in PERIODS:
        raise _error(web.HTTPBadRequest, f"period must be one of {', '.join(PERIODS)}")
    if "basis" in q and q["basis"] not in LEGACY_BASES:
        raise _error(web.HTTPBadRequest, "basis is gone (#170): leave it out (a or b is still accepted)")
    if special not in ("exclude", "include"):
        raise _error(web.HTTPBadRequest, "special must be exclude or include")
    today = kst_day(engine.clock.now())
    if period == "today":
        days: tuple[date, ...] = (today,)
    elif kind is None:
        days = tuple(engine.store.prediction_days(today, PERIODS[period]))
    else:
        days = tuple(engine.store.kind_prediction_days(kind.name, today, PERIODS[period]))
    return _Query(None if symbol == "all" else symbol, period, days, special == "include")


def _kind_query(request: web.Request, engine: PredictionEngine) -> PredictionKind | None:
    """The kind asked for (``kind``, #179): None for the direction (the default), whose answers
    are as before; 400 for a kind the engine does not predict."""
    name = request.query.get("kind", DIRECTION)
    kind = engine.kind_named(name)
    if kind is None:
        names = ", ".join([DIRECTION, *(k.name for k in engine.kinds), *(k.name for k in engine.daily_kinds)])
        raise _error(web.HTTPBadRequest, f"kind must be one of {names}, got {name!r}")
    return None if kind is DIRECTION_KIND else kind


def _echo_basis(request: web.Request, body: dict[str, Any]) -> dict[str, Any]:
    """``body`` with the ``basis`` a screen from before #170 sent, as it sent it (``LEGACY_BASES``):
    the order of the query's fields kept, after ``days``."""
    basis = request.query.get("basis")
    if basis is None:
        return body
    out: dict[str, Any] = {}
    for key, value in body.items():
        out[key] = value
        if key == "days":
            out["basis"] = basis
    return out


async def _leaderboard(request: web.Request) -> web.Response:
    engine = request.app[ENGINE_KEY]
    kind = _kind_query(request, engine)
    if kind is not None:
        return await _kind_leaderboard(request, kind)
    query = _query(request, engine)
    min_sample = request.app[MIN_SAMPLE_KEY]

    def build(
        algorithms: dict[str, list[Scored]], baselines: dict[str, list[Scored]], excluded: dict[str, Excluded]
    ) -> dict[str, Any]:
        board = leaderboard(algorithms, baselines, min_sample, engine.fee_rate)

        def listed(entries: list[Entry]) -> list[dict[str, Any]]:
            return [{**entry_json(e), "excluded": _excluded_json(excluded.get(e.algorithm))} for e in entries]

        return {
            **query.describe(),
            "min_sample": min_sample,
            "ranked": listed(board.ranked),
            "unranked": listed(board.unranked),
            "baselines": listed(board.baselines),
        }

    body = await _stats(request, ("leaderboard",), query, build)
    # Read now, not cached with the stats: a retire command changes it from another process.
    statuses = {name: s.status.value for name, s in engine.store.statuses().items()}

    def with_status(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{**e, "status": statuses.get(e["algorithm"])} for e in entries]

    body = {**body, "ranked": with_status(body["ranked"]), "unranked": with_status(body["unranked"])}
    return _json(_echo_basis(request, body))


async def _algorithm_stats(request: web.Request) -> web.Response:
    engine = request.app[ENGINE_KEY]
    name = request.match_info["name"]
    known = (
        {a.name for a in engine.algorithms} | engine.baseline_names | set(NAMES) | engine.store.algorithms()
    )
    if name not in known and (kind := _kind_of(engine, name)) is not None:
        return await _kind_algorithm_stats(request, name, kind)  # of its kind (#179)
    query = _query(request, engine)
    if name not in known:
        raise _error(web.HTTPNotFound, f"unknown algorithm {name!r}")
    min_sample = request.app[MIN_SAMPLE_KEY]

    def build(
        algorithms: dict[str, list[Scored]], baselines: dict[str, list[Scored]], excluded: dict[str, Excluded]
    ) -> dict[str, Any]:
        records = baselines[name] if name in baselines else algorithms.get(name, [])
        # Overall lifts carry the leaderboard's FDR adjustment; breakdowns are not tested.
        board = leaderboard(algorithms, baselines, min_sample, engine.fee_rate)
        listed = board.ranked + board.unranked + board.baselines
        overall = next(
            (e for e in listed if e.algorithm == name), entry(name, records, baselines, engine.fee_rate)
        )

        def by(key: Callable[[Scored], str]) -> dict[str, Any]:
            groups: dict[str, list[Scored]] = {}
            for r in records:
                groups.setdefault(key(r), []).append(r)
            return {
                k: entry_json(entry(name, g, baselines, engine.fee_rate)) for k, g in sorted(groups.items())
            }

        terciles, cuts = by_volatility(records, engine.store.barrier_widths(query.days, query.symbol))

        return {
            "algorithm": name,
            **query.describe(),
            "min_sample": min_sample,
            "overall": {
                **entry_json(overall),
                "calibration": [_bin_json(b) for b in overall.summary.calibration],
                "calibration_by_class": {
                    d.value: [_bin_json(b) for b in bins]
                    for d, bins in overall.summary.calibration_by_class.items()
                },
                "excluded": _excluded_json(excluded.get(name)),
                "skill": skill_json(
                    skill([(r.predicted, r.outcome) for r in records]), day_kappa(calls_by_day(records))
                ),
            },
            "by_symbol": by(lambda r: r.symbol),
            "by_slot": by(lambda r: slot(r.bar_close)),
            "by_model_version": by(lambda r: r.model_version),
            "by_volatility": {
                k: entry_json(entry(name, g, baselines, engine.fee_rate)) for k, g in terciles.items() if g
            },
            "volatility_cuts": cuts,
        }

    return _json(_echo_basis(request, await _stats(request, ("algorithm", name), query, build)))


Build = Callable[[dict[str, list[Scored]], dict[str, list[Scored]], dict[str, Excluded]], dict[str, Any]]


TERCILES = ("low", "mid", "high")


def by_volatility(
    records: Sequence[Scored], widths: Mapping[tuple[str, str], float]
) -> tuple[dict[str, list[Scored]], dict[str, list[float]]]:
    """``records`` split into volatility terciles by the barrier width w at their bar close
    (#262, ``PredictionStore.barrier_widths``, basis points of the mid-point): low up to a
    symbol's lower cut, mid up to its upper, high above; and each symbol's two cuts. The cuts
    come from every bar close of the period with a width (``widths``), not from the records,
    so every predictor's terciles are the same bar closes and can be set side by side: the
    widths at a third and two thirds of the symbol's sorted ones (the lower of two). Records
    without a width are left out."""
    by_symbol: dict[str, list[float]] = {}
    for (symbol, _), width in widths.items():
        by_symbol.setdefault(symbol, []).append(width)
    cuts: dict[str, list[float]] = {}
    for symbol, ws in sorted(by_symbol.items()):
        ws.sort()
        cuts[symbol] = [ws[(len(ws) - 1) // 3], ws[2 * (len(ws) - 1) // 3]]
    groups: dict[str, list[Scored]] = {t: [] for t in TERCILES}
    for r in records:
        w = widths.get((r.symbol, iso(r.bar_close)))
        if w is None:
            continue
        low, high = cuts[r.symbol]
        groups["low" if w <= low else "mid" if w <= high else "high"].append(r)
    return groups, cuts


def skill_json(s: Skill | None, by_day: Lift | None = None) -> dict[str, Any] | None:
    if s is None:
        return None
    return {
        "n": s.n,
        "accuracy": s.accuracy,
        "expected_accuracy": s.expected_accuracy,
        "kappa": s.kappa,
        "gerrity": s.gerrity,
        "balanced_accuracy": s.balanced_accuracy,
        "independence": {"chi2": s.chi2, "df": s.df, "p": s.p},
        "by_day": None
        if by_day is None
        else {"n": by_day.n, "value": by_day.value, "low": by_day.low, "high": by_day.high, "p": by_day.p},
    }


async def _stats(
    request: web.Request, question: tuple[str, ...], query: _Query, build: Build
) -> dict[str, Any]:
    """Statistics for ``query``, computed off the event loop and kept until the store changes."""
    engine = request.app[ENGINE_KEY]

    def compute() -> dict[str, Any]:
        # Reading and computing both run in the worker: a long history must not hold up
        # the event loop that records and predicts.
        rows, excluded = engine.store.scored_and_excluded(query.days, query.symbol, query.special)
        if engine.target_version >= 2:
            # As the statuses go by (#170): an algorithm's predictions by models trained on the
            # touch target only, not an older model's scored on a label it did not learn.
            rows = on_target(rows, engine.baseline_names | set(NAMES), engine.target_version)
        algorithms, baselines = split(scored_by_algorithm(rows), engine.baseline_names)
        for a in engine.algorithms:
            algorithms.setdefault(a.name, [])
        return build(algorithms, baselines, excluded)

    return await _cached(request, (question, query), compute)


async def _cached(request: web.Request, key: Any, compute: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """``compute``'s answer to the question ``key``, computed off the event loop and kept until
    the store changes."""
    engine = request.app[ENGINE_KEY]
    cache: dict[Any, tuple[int, dict[str, Any]]] = request.app[STATS_CACHE_KEY]
    version = engine.store.version
    cached = cache.get(key)
    if cached is not None and cached[0] == version:
        return cached[1]
    body = await asyncio.to_thread(compute)
    if len(cache) >= _CACHED_QUESTIONS:
        cache.clear()
    cache[key] = (version, body)
    return body


# The kinds besides the direction (#179).

KindBuild = Callable[
    [dict[str, list[KindScored]], dict[str, list[KindScored]], dict[str, Excluded]], dict[str, Any]
]


def _kind_of(engine: PredictionEngine, name: str) -> PredictionKind | None:
    """The kind besides the direction a predictor's name is of: an algorithm's or a baseline's
    the engine runs, or one that predicted before; None if none."""
    for kind in [*engine.kinds, *engine.daily_kinds]:  # the daily kinds' too (#264)
        if name in kind.baseline_names():
            return kind
    found = engine.kind_of_algorithm(name)
    if found is not None and found is not DIRECTION_KIND:
        return found
    stored = engine.store.kind_algorithms().get(name)
    return None if stored is None else engine.kind_named(stored)


async def _kind_stats(
    request: web.Request, kind: PredictionKind, question: tuple[str, ...], query: _Query, build: KindBuild
) -> dict[str, Any]:
    """A kind's statistics for ``query`` (#179), as ``_stats``: its predictions scored on its
    answers, of models trained on them (its answer version) and its baselines'."""
    engine = request.app[ENGINE_KEY]
    names = kind.baseline_names()

    def compute() -> dict[str, Any]:
        rows, excluded = engine.store.kind_scored_and_excluded(
            kind.name, kind.answer_version, query.days, query.symbol, query.special
        )
        rows = on_target(rows, names, kind.answer_version)
        algorithms, baselines = kind_split(scored_of_kind(rows, kind), names)
        of_kind = [a.name for a in engine.kind_algorithms if a.kind == kind.name]
        of_kind += [a.name for a in engine.daily_algorithms if a.kind == kind.name]
        for name in of_kind:
            algorithms.setdefault(name, [])
        return build(algorithms, baselines, excluded)

    return await _cached(request, (kind.name, question, query), compute)


async def _kind_leaderboard(request: web.Request, kind: PredictionKind) -> web.Response:
    """``/v1/leaderboard?kind=``: a kind's algorithms ranked against its baselines (#179)."""
    engine = request.app[ENGINE_KEY]
    query = _query(request, engine, kind)
    min_sample = request.app[MIN_SAMPLE_KEY]

    def build(
        algorithms: dict[str, list[KindScored]],
        baselines: dict[str, list[KindScored]],
        excluded: dict[str, Excluded],
    ) -> dict[str, Any]:
        board = kind_leaderboard(algorithms, baselines, min_sample, kind, engine.calendar)

        def listed(entries: list[KindEntry]) -> list[dict[str, Any]]:
            return [
                {**kind_entry_json(e), "excluded": _excluded_json(excluded.get(e.algorithm))} for e in entries
            ]

        return {
            "kind": kind.name,
            **query.describe(),
            "min_sample": min_sample,
            "ranked": listed(board.ranked),
            "unranked": listed(board.unranked),
            "baselines": listed(board.baselines),
        }

    body = await _kind_stats(request, kind, ("leaderboard",), query, build)
    statuses = {name: s.status.value for name, s in engine.store.statuses().items()}

    def with_status(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{**e, "status": statuses.get(e["algorithm"])} for e in entries]

    body = {**body, "ranked": with_status(body["ranked"]), "unranked": with_status(body["unranked"])}
    return _json(_echo_basis(request, body))


async def _kind_algorithm_stats(request: web.Request, name: str, kind: PredictionKind) -> web.Response:
    """``/v1/algorithms/{name}/stats`` of a predictor of a kind besides the direction (#179)."""
    engine = request.app[ENGINE_KEY]
    query = _query(request, engine, kind)
    min_sample = request.app[MIN_SAMPLE_KEY]

    def build(
        algorithms: dict[str, list[KindScored]],
        baselines: dict[str, list[KindScored]],
        excluded: dict[str, Excluded],
    ) -> dict[str, Any]:
        records = baselines[name] if name in baselines else algorithms.get(name, [])
        board = kind_leaderboard(algorithms, baselines, min_sample, kind, engine.calendar)
        listed = board.ranked + board.unranked + board.baselines
        overall = next(
            (e for e in listed if e.algorithm == name),
            kind_entry(name, records, baselines, kind, calendar=engine.calendar),
        )

        def by(key: Callable[[KindScored], str]) -> dict[str, Any]:
            groups: dict[str, list[KindScored]] = {}
            for r in records:
                groups.setdefault(key(r), []).append(r)
            return {
                k: kind_entry_json(kind_entry(name, g, baselines, kind, calendar=engine.calendar))
                for k, g in sorted(groups.items())
            }

        return {
            "kind": kind.name,
            "algorithm": name,
            **query.describe(),
            "min_sample": min_sample,
            "overall": {
                **kind_entry_json(overall),
                "calibration": [_bin_json(b) for b in overall.summary.calibration],
                "calibration_by_class": {
                    c: [_bin_json(b) for b in bins]
                    for c, bins in overall.summary.calibration_by_class.items()
                },
                "excluded": _excluded_json(excluded.get(name)),
            },
            "by_symbol": by(lambda r: r.symbol),
            "by_slot": by(lambda r: slot(r.bar_close)),
            "by_model_version": by(lambda r: r.model_version),
        }

    body = await _kind_stats(request, kind, ("algorithm", name), query, build)
    return _json(_echo_basis(request, body))


async def _cost_map(request: web.Request) -> web.Response:
    """``/v1/cost-map`` (#307, ``cost_map.py``): the symbols' cost cards, a row each kind with
    a direction, from its leaderboard's statistics for the query (each kind's days its own,
    computed and kept as the leaderboard's), and the period base rates kept
    (``base_rates.py``)."""
    engine = request.app[ENGINE_KEY]
    query = _query(request, engine)
    min_sample = request.app[MIN_SAMPLE_KEY]
    fee = engine.fee_rate

    def direction(
        algorithms: dict[str, list[Scored]], baselines: dict[str, list[Scored]], excluded: dict[str, Excluded]
    ) -> dict[str, Any]:
        board = leaderboard(algorithms, baselines, min_sample, fee)
        return direction_row(DIRECTION_KIND, board, {**algorithms, **baselines}, fee, min_sample, query.days)

    rows = [await _stats(request, ("cost-map",), query, direction)]
    for kind in [*engine.kinds, *engine.daily_kinds]:
        if kind.sides is None:
            continue  # no direction: no long nor short to cost (the range)
        of_kind = _query(request, engine, kind)

        def build(
            algorithms: dict[str, list[KindScored]],
            baselines: dict[str, list[KindScored]],
            excluded: dict[str, Excluded],
            kind: PredictionKind = kind,
            days: tuple[date, ...] = of_kind.days,
        ) -> dict[str, Any]:
            board = kind_leaderboard(algorithms, baselines, min_sample, kind, engine.calendar)
            return kind_row(kind, board, {**algorithms, **baselines}, fee, min_sample, days)

        rows.append(await _kind_stats(request, kind, ("cost-map",), of_kind, build))
    names = request.app[NAMES_KEY]
    symbols = []
    for symbol in [query.symbol] if query.symbol is not None else engine.symbols:
        price: float | None = engine.mid(symbol)
        at: datetime | None = engine.clock.now()
        live = price is not None
        if price is None:  # out of continuous trading: the latest bar close's mid-point
            last = engine.store.last_reference(symbol)
            price, at = (None, None) if last is None else last
        symbols.append(
            {
                "symbol": symbol,
                "name": names.get(symbol),
                "price": price,
                "price_at": None if at is None else iso(at),
                "live": live,
                "costs": None if price is None else cost_card(symbol, price, fee),
            }
        )
    regimes, missing = await asyncio.to_thread(read_base_rates, request.app[STATE_DIR_KEY])
    if regimes is not None and query.symbol is not None:
        regimes = {**regimes, "rows": [r for r in regimes["rows"] if r["symbol"] == query.symbol]}
    described = query.describe()
    return _json(
        {
            "symbol": described["symbol"],
            "period": described["period"],
            "special": described["special"],
            "as_of": iso(engine.clock.now()),
            "min_sample": min_sample,
            "fee_rate": fee,
            "spread_ticks": SPREAD_TICKS,
            "symbols": symbols,
            "rows": rows,
            "regimes": regimes,
            "regimes_missing": missing,
        }
    )


async def _kinds(request: web.Request) -> web.Response:
    """``/v1/kinds``: every kind the engine predicts, the direction first (#179)."""
    engine = request.app[ENGINE_KEY]
    kinds = [DIRECTION_KIND.as_dict([(b.name, b.card) for b in engine.baselines])]
    kinds += [k.as_dict(k.baseline_cards()) for k in engine.kinds]
    kinds += [k.as_dict(k.baseline_cards()) for k in engine.daily_kinds]  # once a day (#264)
    return _json({"kinds": kinds})


def _kind_path(request: web.Request, engine: PredictionEngine) -> PredictionKind:
    """The kind besides the direction in the path; 404 for the direction (its predictions are
    ``/latest`` and ``/predictions``) and for a kind the engine does not predict."""
    name = request.match_info["kind"]
    if name == DIRECTION:
        raise _error(
            web.HTTPNotFound,
            "the direction's predictions are /v1/symbols/{symbol}/latest and .../predictions",
        )
    kind = engine.kind_named(name)
    if kind is None:
        raise _error(web.HTTPNotFound, f"unknown kind {name!r}")
    return kind


async def _kind_latest(request: web.Request) -> web.Response:
    engine, symbol = _engine_and_symbol(request)
    kind = _kind_path(request, engine)
    rows = engine.store.kind_latest(kind.name, kind.answer_version, symbol)
    return _json(
        {
            "symbol": symbol,
            "kind": kind.name,
            "bar_close": rows[0]["bar_close"] if rows else None,
            "predictions": [kind_prediction_json(r, target=kind_target(kind, engine.calendar)) for r in rows],
        }
    )


async def _kind_predictions(request: web.Request) -> web.Response:
    engine, symbol = _engine_and_symbol(request)
    kind = _kind_path(request, engine)
    algorithm = request.query.get("algorithm")
    if (bar_close := _time_query(request, "bar_close")) is not None:
        rows = engine.store.kind_at_close(kind.name, kind.answer_version, symbol, bar_close, algorithm)
    else:
        limit = _limit_query(request, DEFAULT_LIMIT, MAX_LIMIT)
        before = _time_query(request, "before")
        rows = engine.store.kind_recent(kind.name, kind.answer_version, symbol, limit, algorithm, before)
    return _json(
        {
            "symbol": symbol,
            "kind": kind.name,
            "predictions": [kind_prediction_json(r, target=kind_target(kind, engine.calendar)) for r in rows],
        }
    )


def _bin_json(b: CalibrationBin) -> dict[str, Any]:
    return {
        "low": b.low,
        "high": b.high,
        "count": b.count,
        "mean_p": b.mean_p,
        "observed": interval_json(b.observed),
    }


def _excluded_json(e: Excluded | None) -> dict[str, Any]:
    e = e or Excluded()
    return {"special": e.special, "unscorable": e.unscorable, "status": e.status}


async def _trader_trades(request: web.Request) -> web.Response:
    """The trader's trades (#394), read from its store (read-only, off the event loop): the
    champion's, or a policy version's (``policy``, #400); none without the store or before the
    trader's first trade."""
    limit = _limit_query(request, DEFAULT_LIMIT, MAX_LIMIT)
    before = _time_query(request, "before")
    raw = request.query.get("policy")
    policy: int | None = None
    if raw is not None:
        policy = int(raw) if raw.isdecimal() and raw.isascii() else 0
        if policy < 1:
            raise _error(web.HTTPBadRequest, f"policy must be a policy version (1 or more), got {raw!r}")
    state_dir = request.app[STATE_DIR_KEY]
    trades = (
        []
        if state_dir is None
        else await asyncio.to_thread(read_trades, state_dir / TRADER_DB, before, limit, policy)
    )
    return _json({"trades": trades})


async def _trader(request: web.Request) -> web.Response:
    """The trader's state (#396), read from its store (read-only, off the event loop); its fields
    null before the trader first wrote it."""
    now = request.app[ENGINE_KEY].clock.now()
    state_dir = request.app[STATE_DIR_KEY]
    path = None if state_dir is None else state_dir / TRADER_DB
    return _json(await asyncio.to_thread(read_trader, path, now))


async def _trader_days(request: web.Request) -> web.Response:
    """The trader's ledger of each day (#396) from ``from`` to ``to``, oldest first."""
    engine = request.app[ENGINE_KEY]
    to = _day_query(request, "to") or kst_day(engine.clock.now())
    since = _day_query(request, "from") or to - timedelta(days=DEFAULT_TRADER_DAYS - 1)
    if since > to:
        raise _error(web.HTTPBadRequest, f"from must not be after to, got {since} and {to}")
    if (span := (to - since).days + 1) > MAX_TRADER_DAYS:
        raise _error(web.HTTPBadRequest, f"from and to span at most {MAX_TRADER_DAYS} days, got {span}")
    state_dir = request.app[STATE_DIR_KEY]
    days = (
        []
        if state_dir is None
        else await asyncio.to_thread(read_trader_days, state_dir / TRADER_DB, since, to)
    )
    return _json({"from": since.isoformat(), "to": to.isoformat(), "days": days})


async def _trader_pause(request: web.Request) -> web.Response:
    """The screen's pause of the trader (#396): ``{"sell_now": false}`` pauses new entries,
    ``true`` also sells the open position at once. Written into the trader's store (off the event
    loop) for the trader to read within a second; 202 with what was written. A JSON body only
    (a browser's cross-site form cannot send one without asking first), with ``sell_now`` alone."""
    state_dir = request.app[STATE_DIR_KEY]
    if state_dir is None:
        raise _error(web.HTTPServiceUnavailable, "no trader here: this engine has no state folder")
    if request.content_type != "application/json":
        raise _error(
            web.HTTPUnsupportedMediaType, f"the body must be application/json, got {request.content_type!r}"
        )
    try:
        body = json.loads(await request.text())
    except ValueError:
        body = None
    if not isinstance(body, dict) or set(body) != {"sell_now"} or not isinstance(body["sell_now"], bool):
        raise _error(web.HTTPBadRequest, 'the body must be {"sell_now": false} or {"sell_now": true}')
    at = request.app[ENGINE_KEY].clock.now()
    control = await asyncio.to_thread(request_pause, state_dir / TRADER_DB, body["sell_now"], at)
    return _json({"control": control}, status=202)


async def _trader_policies(request: web.Request) -> web.Response:
    """The trader's policy versions (#399), read from its store (read-only, off the event loop);
    none without the store or before the first nightly fit."""
    limit = _limit_query(request, DEFAULT_LIMIT, MAX_LIMIT)
    state_dir = request.app[STATE_DIR_KEY]
    if state_dir is None:
        return _json({"champion": None, "policies": [], "champions": []})
    return _json(await asyncio.to_thread(read_policies, state_dir / TRADER_DB, limit))


async def _trader_marks(request: web.Request) -> web.Response:
    """The trader's buys and sells of a symbol as the chart marks them (#398), read from its store
    (read-only, off the event loop); none without the store, before the trader's first trade, or
    for a symbol it does not trade."""
    _, symbol = _engine_and_symbol(request)
    limit = _limit_query(request, DEFAULT_LIMIT, MAX_LIMIT)
    before = _time_query(request, "before")
    state_dir = request.app[STATE_DIR_KEY]
    marks = (
        []
        if state_dir is None
        else await asyncio.to_thread(read_marks, state_dir / TRADER_DB, symbol, before, limit)
    )
    return _json({"symbol": symbol, "marks": marks})


def _json(body: dict[str, Any], status: int = 200) -> web.Response:
    return web.json_response(body, dumps=_dumps, status=status)


def _error(kind: type[web.HTTPError], message: str) -> web.HTTPError:
    return kind(text=_dumps({"error": message}), content_type="application/json")
