"""Engine API v1: what the Fincept screen reads, as JSON over HTTP.

Endpoints (all under ``/v1``):

- ``GET /status``: the clock, the market phase, the last bar close, pending outcomes and,
  live, the KIS connection and when each feed last delivered;
- ``GET /symbols``: the predicted symbols with their reference price and flat band now;
- ``GET /symbols/{symbol}/latest``: the predictions of the symbol's latest bar close;
- ``GET /symbols/{symbol}/predictions?limit=N&algorithm=A&before=T``: the predictions of the
  last N bar closes (default 100, at most 1000; before ``T`` if given), newest first, with
  their outcomes; ``?bar_close=T`` gives that bar close's;
- ``GET /symbols/{symbol}/bars``: today's finished one-minute bars, oldest first;
  ``?before=T&limit=N``: the last N bars before ``T`` across days (the backfill's files);
- ``GET /symbols/{symbol}/candles?period=day|week|month|year&before=T&limit=N``: the last N
  stored candles of the period (default 400, at most 2000) that start before ``T`` (default
  now), oldest first, never the one holding today (``candles``);
- ``GET /algorithms``: each registered algorithm's card, features, current model and status
  (probation, active, bench, retired, clone) with its history, the meaning of each feature
  and model parameter; and the baselines' and the consensus' cards;
- ``GET /algorithms/{name}``: one of them, with the models installed on the server for it;
- ``GET /leaderboard?symbol=&period=&basis=&special=``: algorithms ranked by the lower
  bound of their weakest lift over the baselines, with their stats;
- ``GET /algorithms/{name}/stats?symbol=&period=&basis=&special=``: one algorithm's
  stats, by symbol, 30-minute slot and model version, with its calibration curve;
- ``GET /consensus``: the consensus members now and the weights last fitted;
- ``GET /macro``: the latest day's market regime and macro readings (``macro.py``);
- ``GET /calendar?limit=``: the economic calendar from the start of today, as the collector
  last read it (``econ_calendar.py``);
- ``GET /stream``: a WebSocket of predictions, consensus, outcomes, the KIS status and
  status changes as they happen (``stream.py``).

``symbol`` is a symbol or ``all`` (default), ``period`` is ``today`` (default), ``5d`` or
``20d`` (the latest days with predictions), ``basis`` is ``b`` (default: from when it was
displayed) or ``a`` (from the bar close), and ``special`` is ``exclude`` (default) or
``include`` (predictions with a VI or circuit breaker in their window).

Times are ISO 8601 in KST. On the server, compose publishes it on the Tailscale address only.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import date, datetime, time
from functools import partial
from pathlib import Path
from time import monotonic
from typing import Any, Literal

from aiohttp import web

from krx_quant.algorithms import Algorithm
from krx_quant.backfill import stock_bars_path, stored_bars
from krx_quant.candles import PERIODS as CANDLE_PERIODS
from krx_quant.candles import stored_candles
from krx_quant.consensus import NAMES, slot
from krx_quant.consensus import cards as consensus_cards
from krx_quant.contracts import USD
from krx_quant.econ_calendar import calendar_answer, read_latest
from krx_quant.engine import PredictionEngine
from krx_quant.features import FEATURE_MEANINGS
from krx_quant.feeds.base import KST, Row, kst_day
from krx_quant.macro import LatestMacro, latest_macro, reading_before
from krx_quant.model_shelf import Shelf
from krx_quant.predictions import Excluded, iso
from krx_quant.pricing import flat_ticks, tick_size
from krx_quant.reference import Bar
from krx_quant.stats import (
    DEFAULT_MIN_SAMPLE,
    CalibrationBin,
    Entry,
    Scored,
    entry,
    entry_json,
    interval_json,
    leaderboard,
    scored_by_algorithm,
    split,
)
from krx_quant.stream import EventStream, LiveStatus, prediction_json

ENGINE_KEY = web.AppKey("engine", PredictionEngine)
# live: the collector; demo: `krx-quant demo-engine`; replay: tests.
Mode = Literal["live", "demo", "replay"]
MODE_KEY = web.AppKey("mode", str)
LIVE_STATUS_KEY: web.AppKey[LiveStatus] = web.AppKey("live_status")

# Readable as is (curl, logs): indented, and Korean as UTF-8 rather than \u escapes.
_dumps = partial(json.dumps, ensure_ascii=False, indent=2)

MIN_SAMPLE_KEY = web.AppKey("min_sample", int)
DATA_DIR_KEY: web.AppKey[Path | None] = web.AppKey("data_dir")
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
STATS_CACHE_KEY = web.AppKey("stats_cache", dict)
PERIODS = {"today": 1, "5d": 5, "20d": 20}  # days with predictions
# Results kept per question until the store changes (the screen asks the same often).
_CACHED_QUESTIONS = 64

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000
# Bars before a time (#108): a trading day is up to 720 one-minute bars (08:00–20:00).
DEFAULT_BARS = 400
MAX_BARS = 2000
# Day candles (#111): the chart reads a screenful and scrolls back for more.
DEFAULT_CANDLES = 400
MAX_CANDLES = 2000


def create_app(
    engine: PredictionEngine,
    *,
    mode: Mode = "replay",
    live_status: LiveStatus | None = None,
    min_sample: int = DEFAULT_MIN_SAMPLE,
    data_dir: Path | None = None,
    shelf: Shelf | None = None,
    stream_poll_seconds: float = 1.0,
) -> web.Application:
    """The Engine API of ``engine``. ``data_dir`` holds the macro readings and ``shelf`` the
    models installed on the server; without them there is no macro and no model history.
    Live and demo show the KIS status: they need ``live_status``.
    """
    if mode in ("live", "demo") and live_status is None:
        raise ValueError(f"the {mode} Engine API shows the kis status: give it live_status")
    app = web.Application()
    app[ENGINE_KEY] = engine
    app[MODE_KEY] = mode
    app[MIN_SAMPLE_KEY] = min_sample
    app[STATS_CACHE_KEY] = {}
    app[DATA_DIR_KEY] = data_dir
    app[SHELF_KEY] = shelf
    app[MACRO_CACHE_KEY] = {}
    app[USD_CLOSE_CACHE_KEY] = {}
    app[CALENDAR_CACHE_KEY] = {}
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
    app.router.add_get("/v1/algorithms", _algorithms)
    app.router.add_get("/v1/algorithms/{name}", _algorithm)
    app.router.add_get("/v1/leaderboard", _leaderboard)
    app.router.add_get("/v1/algorithms/{name}/stats", _algorithm_stats)
    app.router.add_get("/v1/consensus", _consensus)
    app.router.add_get("/v1/macro", _macro)
    app.router.add_get("/v1/calendar", _calendar)
    app.router.add_get("/v1/stream", stream.handle)
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
    if training is not None:  # finalized where the night ran: shown in KST like every time here
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
    return _json(body)


async def _symbols(request: web.Request) -> web.Response:
    engine = request.app[ENGINE_KEY]
    symbols = []
    for symbol in engine.symbols:
        mid = engine.mid(symbol)
        symbols.append(
            {
                "symbol": symbol,
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
    return _json(
        {
            "algorithms": [_algorithm_json(engine, a) for a in engine.algorithms],
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
    algorithm = next((a for a in engine.algorithms if a.name == name), None)
    if algorithm is None:
        raise _error(web.HTTPNotFound, f"unknown algorithm {name!r}")
    shelf = request.app[SHELF_KEY]
    installed = shelf.history(name) if shelf is not None else []
    history = [
        {"at": iso(datetime.fromisoformat(e["at"])), "version": e["version"], "why": e["why"]}
        for e in installed
    ]
    return _json({**_algorithm_json(engine, algorithm), "model_history": history})


def _algorithm_json(engine: PredictionEngine, a: Algorithm) -> dict[str, Any]:
    return {
        "name": a.name,
        "card": a.card.as_dict(),
        "features": list(a.features),
        "feature_meanings": {f: FEATURE_MEANINGS[f].as_dict() for f in a.features},
        "training_days": a.training_days,
        "model": engine.models[a.name].as_dict(),
        "param_meanings": {p: m.as_dict() for p, m in a.param_meanings.items()},
        "status": _status_json(engine, a.name),
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
        cached = cache[today] = (monotonic(), _macro_json(latest))
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


def _macro_json(latest: LatestMacro | None) -> dict[str, Any]:
    if latest is None:
        empty: dict[str, Any] = {"day": None, "decided_at": None, "regime": None, "reasons": []}
        return {**empty, "readings": [], "failed": {}, "skipped": []}
    readings = [
        {
            "indicator": indicator.name,
            "label": indicator.label,
            "source": row["source"],
            "series": row["series"],
            "as_of": row["as_of"].isoformat() if row["as_of"] else None,
            "value": row["value"],
            "change_pct": row["change_pct"],
        }
        for indicator, row in latest.readings
    ]
    return {
        "day": latest.day.isoformat(),
        "decided_at": iso(latest.decided_at),
        "regime": latest.regime,
        "reasons": latest.reasons,
        "readings": readings,
        "failed": latest.failed,
        "skipped": latest.skipped,
    }


def _status_json(engine: PredictionEngine, name: str) -> dict[str, Any]:
    history = engine.store.status_history(name)
    return {
        "status": history[-1]["status"] if history else None,
        "since": history[-1]["at"] if history else None,
        "history": [
            {"at": h["at"], "from": h["previous"], "to": h["status"], "reason": h["reason"]} for h in history
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
    basis: str
    special: bool

    def describe(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol or "all",
            "period": self.period,
            "days": [d.isoformat() for d in self.days],
            "basis": self.basis,
            "special": "include" if self.special else "exclude",
        }


def _query(request: web.Request, engine: PredictionEngine) -> _Query:
    q = request.query
    symbol = q.get("symbol", "all")
    period = q.get("period", "today")
    basis = q.get("basis", "b")
    special = q.get("special", "exclude")
    if symbol != "all" and symbol not in engine.symbols:
        raise _error(web.HTTPBadRequest, f"symbol must be all or one of {', '.join(engine.symbols)}")
    if period not in PERIODS:
        raise _error(web.HTTPBadRequest, f"period must be one of {', '.join(PERIODS)}")
    if basis not in ("a", "b"):
        raise _error(web.HTTPBadRequest, "basis must be a or b")
    if special not in ("exclude", "include"):
        raise _error(web.HTTPBadRequest, "special must be exclude or include")
    today = kst_day(engine.clock.now())
    days = (today,) if period == "today" else tuple(engine.store.prediction_days(today, PERIODS[period]))
    return _Query(None if symbol == "all" else symbol, period, days, basis, special == "include")


async def _leaderboard(request: web.Request) -> web.Response:
    engine = request.app[ENGINE_KEY]
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

    return _json({**body, "ranked": with_status(body["ranked"]), "unranked": with_status(body["unranked"])})


async def _algorithm_stats(request: web.Request) -> web.Response:
    engine = request.app[ENGINE_KEY]
    name = request.match_info["name"]
    query = _query(request, engine)
    known = (
        {a.name for a in engine.algorithms} | engine.baseline_names | set(NAMES) | engine.store.algorithms()
    )
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
            },
            "by_symbol": by(lambda r: r.symbol),
            "by_slot": by(lambda r: slot(r.bar_close)),
            "by_model_version": by(lambda r: r.model_version),
        }

    return _json(await _stats(request, ("algorithm", name), query, build))


Build = Callable[[dict[str, list[Scored]], dict[str, list[Scored]], dict[str, Excluded]], dict[str, Any]]


async def _stats(
    request: web.Request, question: tuple[str, ...], query: _Query, build: Build
) -> dict[str, Any]:
    """Statistics for ``query``, computed off the event loop and kept until the store changes."""
    engine = request.app[ENGINE_KEY]
    cache: dict[Any, tuple[int, dict[str, Any]]] = request.app[STATS_CACHE_KEY]
    key = (question, query)
    version = engine.store.version
    cached = cache.get(key)
    if cached is not None and cached[0] == version:
        return cached[1]

    def compute() -> dict[str, Any]:
        # Reading and computing both run in the worker: a long history must not hold up
        # the event loop that records and predicts.
        rows, excluded = engine.store.scored_and_excluded(
            query.days, query.symbol, query.special, query.basis
        )
        algorithms, baselines = split(scored_by_algorithm(rows, query.basis), engine.baseline_names)
        for a in engine.algorithms:
            algorithms.setdefault(a.name, [])
        return build(algorithms, baselines, excluded)

    body = await asyncio.to_thread(compute)
    if len(cache) >= _CACHED_QUESTIONS:
        cache.clear()
    cache[key] = (version, body)
    return body


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


def _json(body: dict[str, Any]) -> web.Response:
    return web.json_response(body, dumps=_dumps)


def _error(kind: type[web.HTTPError], message: str) -> web.HTTPError:
    return kind(text=_dumps({"error": message}), content_type="application/json")
