"""Engine API v1: what the Fincept screen reads, as JSON over HTTP.

Endpoints (all under ``/v1``):

- ``GET /status``: the clock, the market phase, the last bar close, pending outcomes and,
  live, the KIS connection and when each feed last delivered;
- ``GET /symbols``: the predicted symbols with their reference price and flat band now;
- ``GET /symbols/{symbol}/latest``: the predictions of the symbol's latest bar close;
- ``GET /symbols/{symbol}/predictions?limit=N&algorithm=A``: the predictions of the last
  N bar closes (default 100, at most 1000), newest first, with their outcomes;
- ``GET /symbols/{symbol}/bars``: today's finished one-minute bars, oldest first;
- ``GET /algorithms``: each registered algorithm's card, features, current model and status
  (probation, active, bench, retired, clone) with its history;
- ``GET /algorithms/{name}``: one of them, with the models installed on the server for it;
- ``GET /leaderboard?symbol=&period=&basis=&special=``: algorithms ranked by the lower
  bound of their weakest lift over the baselines, with their stats;
- ``GET /algorithms/{name}/stats?symbol=&period=&basis=&special=``: one algorithm's
  stats, by symbol, 30-minute slot and model version, with its calibration curve;
- ``GET /consensus``: the consensus members now and the weights last fitted;
- ``GET /macro``: the latest day's market regime and macro readings (``macro.py``).

``symbol`` is a symbol or ``all`` (default), ``period`` is ``today`` (default), ``5d`` or
``20d`` (the latest days with predictions), ``basis`` is ``b`` (default: from when it was
displayed) or ``a`` (from the bar close), and ``special`` is ``exclude`` (default) or
``include`` (predictions with a VI or circuit breaker in their window).

Times are ISO 8601 in KST. On the server, compose publishes it on the Tailscale address only.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from functools import partial
from pathlib import Path
from typing import Any, Literal

from aiohttp import web

from krx_quant.algorithms import Algorithm
from krx_quant.baselines import BASELINE_NAMES, BASELINES
from krx_quant.consensus import NAMES, slot
from krx_quant.engine import PredictionEngine
from krx_quant.feeds.base import kst_day
from krx_quant.macro import latest_macro
from krx_quant.model_shelf import Shelf
from krx_quant.predictions import iso
from krx_quant.pricing import flat_ticks, tick_size
from krx_quant.stats import (
    DEFAULT_MIN_SAMPLE,
    CalibrationBin,
    Entry,
    Interval,
    Scored,
    entry,
    leaderboard,
    scored_by_algorithm,
    split,
)

ENGINE_KEY = web.AppKey("engine", PredictionEngine)
# live: the collector; demo: `krx-quant demo-engine`; replay: tests.
Mode = Literal["live", "demo", "replay"]
MODE_KEY = web.AppKey("mode", str)
LiveStatus = Callable[[], dict[str, Any]]
LIVE_STATUS_KEY: web.AppKey[LiveStatus] = web.AppKey("live_status")

# Readable as is (curl, logs): indented, and Korean as UTF-8 rather than \u escapes.
_dumps = partial(json.dumps, ensure_ascii=False, indent=2)

MIN_SAMPLE_KEY = web.AppKey("min_sample", int)
DATA_DIR_KEY: web.AppKey[Path | None] = web.AppKey("data_dir")
SHELF_KEY: web.AppKey[Shelf | None] = web.AppKey("shelf")
STATS_CACHE_KEY = web.AppKey("stats_cache", dict)
PERIODS = {"today": 1, "5d": 5, "20d": 20}  # days with predictions
# Results kept per question until the store changes (the screen asks the same often).
_CACHED_QUESTIONS = 64

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000


def create_app(
    engine: PredictionEngine,
    *,
    mode: Mode = "replay",
    live_status: LiveStatus | None = None,
    min_sample: int = DEFAULT_MIN_SAMPLE,
    data_dir: Path | None = None,
    shelf: Shelf | None = None,
) -> web.Application:
    """The Engine API of ``engine``. ``data_dir`` holds the macro readings and ``shelf`` the
    models installed on the server; without them there is no macro and no model history."""
    app = web.Application()
    app[ENGINE_KEY] = engine
    app[MODE_KEY] = mode
    app[MIN_SAMPLE_KEY] = min_sample
    app[STATS_CACHE_KEY] = {}
    app[DATA_DIR_KEY] = data_dir
    app[SHELF_KEY] = shelf
    if live_status is not None:
        app[LIVE_STATUS_KEY] = live_status
    app.router.add_get("/v1/status", _status)
    app.router.add_get("/v1/symbols", _symbols)
    app.router.add_get("/v1/symbols/{symbol}/latest", _latest)
    app.router.add_get("/v1/symbols/{symbol}/predictions", _predictions)
    app.router.add_get("/v1/symbols/{symbol}/bars", _bars)
    app.router.add_get("/v1/algorithms", _algorithms)
    app.router.add_get("/v1/algorithms/{name}", _algorithm)
    app.router.add_get("/v1/leaderboard", _leaderboard)
    app.router.add_get("/v1/algorithms/{name}/stats", _algorithm_stats)
    app.router.add_get("/v1/consensus", _consensus)
    app.router.add_get("/v1/macro", _macro)
    return app


async def _status(request: web.Request) -> web.Response:
    engine = request.app[ENGINE_KEY]
    now = engine.clock.now()
    session = engine.calendar.session(kst_day(now))
    body: dict[str, Any] = {
        "mode": request.app[MODE_KEY],
        "now": iso(now),
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
            "predictions": [_prediction(r) for r in rows],
        }
    )


async def _predictions(request: web.Request) -> web.Response:
    engine, symbol = _engine_and_symbol(request)
    raw = request.query.get("limit", str(DEFAULT_LIMIT))
    try:
        limit = int(raw)
    except ValueError:
        limit = 0
    if not 1 <= limit <= MAX_LIMIT:
        raise _error(web.HTTPBadRequest, f"limit must be 1 to {MAX_LIMIT}, got {raw!r}")
    rows = engine.store.recent(symbol, limit, request.query.get("algorithm"))
    return _json({"symbol": symbol, "predictions": [_prediction(r) for r in rows]})


async def _bars(request: web.Request) -> web.Response:
    engine, symbol = _engine_and_symbol(request)
    bars = [
        {
            "start": iso(b.start),
            "open": b.open,
            "high": b.high,
            "low": b.low,
            "close": b.close,
            "volume": b.volume,
            "trades": b.trades,
        }
        for b in engine.bars(symbol)
    ]
    return _json({"symbol": symbol, "bars": bars})


async def _algorithms(request: web.Request) -> web.Response:
    engine = request.app[ENGINE_KEY]
    return _json({"algorithms": [_algorithm_json(engine, a) for a in engine.algorithms]})


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
        "card": {
            "scientist": a.card.scientist,
            "character": a.card.character,
            "logic": a.card.logic,
            "unique_features": list(a.card.unique_features),
            "training": a.card.training,
        },
        "features": list(a.features),
        "training_days": a.training_days,
        "model": engine.models[a.name].as_dict(),
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
    engine = request.app[ENGINE_KEY]
    data_dir = request.app[DATA_DIR_KEY]
    today = kst_day(engine.clock.now())
    latest = None if data_dir is None else await asyncio.to_thread(latest_macro, data_dir, today)
    if latest is None:
        empty: dict[str, Any] = {"day": None, "decided_at": None, "regime": None, "reasons": []}
        return _json({**empty, "readings": [], "failed": {}, "skipped": []})
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
    return _json(
        {
            "day": latest.day.isoformat(),
            "decided_at": iso(latest.decided_at),
            "regime": latest.regime,
            "reasons": latest.reasons,
            "readings": readings,
            "failed": latest.failed,
            "skipped": latest.skipped,
        }
    )


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

    def build(algorithms: dict[str, list[Scored]], baselines: dict[str, list[Scored]]) -> dict[str, Any]:
        board = leaderboard(algorithms, baselines, min_sample, engine.fee_rate)
        return {
            **query.describe(),
            "min_sample": min_sample,
            "ranked": [_entry_json(e) for e in board.ranked],
            "unranked": [_entry_json(e) for e in board.unranked],
            "baselines": [_entry_json(e) for e in board.baselines],
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
        {a.name for a in engine.algorithms}
        | {b.name for b in BASELINES}
        | set(NAMES)
        | engine.store.algorithms()
    )
    if name not in known:
        raise _error(web.HTTPNotFound, f"unknown algorithm {name!r}")
    min_sample = request.app[MIN_SAMPLE_KEY]

    def build(algorithms: dict[str, list[Scored]], baselines: dict[str, list[Scored]]) -> dict[str, Any]:
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
                k: _entry_json(entry(name, g, baselines, engine.fee_rate)) for k, g in sorted(groups.items())
            }

        return {
            "algorithm": name,
            **query.describe(),
            "min_sample": min_sample,
            "overall": {
                **_entry_json(overall),
                "calibration": [_bin_json(b) for b in overall.summary.calibration],
            },
            "by_symbol": by(lambda r: r.symbol),
            "by_slot": by(lambda r: slot(r.bar_close)),
            "by_model_version": by(lambda r: r.model_version),
        }

    return _json(await _stats(request, ("algorithm", name), query, build))


Build = Callable[[dict[str, list[Scored]], dict[str, list[Scored]]], dict[str, Any]]


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
        rows = engine.store.scored_predictions(query.days, query.symbol, query.special)
        algorithms, baselines = split(scored_by_algorithm(rows, query.basis), BASELINE_NAMES)
        for a in engine.algorithms:
            algorithms.setdefault(a.name, [])
        return build(algorithms, baselines)

    body = await asyncio.to_thread(compute)
    if len(cache) >= _CACHED_QUESTIONS:
        cache.clear()
    cache[key] = (version, body)
    return body


def _interval_json(i: Interval | None) -> dict[str, Any] | None:
    return None if i is None else {"value": i.value, "low": i.low, "high": i.high, "n": i.n}


def _entry_json(e: Entry) -> dict[str, Any]:
    s = e.summary
    out: dict[str, Any] = {
        "rank": e.rank,
        "algorithm": e.algorithm,
        "model_versions": e.model_versions,
        "n": s.n,
        "accuracy": _interval_json(s.accuracy),
        "precision": _interval_json(s.precision),
        "coverage": _interval_json(s.coverage),
        "brier": _interval_json(s.brier),
        "pnl": {
            "trades": s.pnl.trades,
            "total_ticks": s.pnl.total,
            "per_trade": _interval_json(s.pnl.per_trade),
        },
    }
    if e.lifts:
        out["lift"] = {
            "low": e.lift_low,
            "vs": {
                name: {
                    "n": t.lift.n,
                    "value": t.lift.value,
                    "low": t.lift.low,
                    "high": t.lift.high,
                    "p": t.lift.p,
                    "q": t.q,
                    "significant": t.significant,
                }
                for name, t in e.lifts.items()
            },
        }
    return out


def _bin_json(b: CalibrationBin) -> dict[str, Any]:
    return {
        "low": b.low,
        "high": b.high,
        "count": b.count,
        "mean_p": b.mean_p,
        "observed": _interval_json(b.observed),
    }


def _json(body: dict[str, Any]) -> web.Response:
    return web.json_response(body, dumps=_dumps)


def _error(kind: type[web.HTTPError], message: str) -> web.HTTPError:
    return kind(text=_dumps({"error": message}), content_type="application/json")


def _prediction(row: dict[str, Any]) -> dict[str, Any]:
    scored = row["scored_at"] is not None
    return {
        "algorithm": row["algorithm"],
        "model_version": row["model_version"],
        "bar_close": row["bar_close"],
        "displayed_at": row["displayed_at"],
        "target_at": row["target_at"],
        "status": row["status"],
        "p_up": row["p_up"],
        "p_flat": row["p_flat"],
        "p_down": row["p_down"],
        "expected_ticks": row["expected_ticks"],
        "outcome": None
        if not scored
        else {
            "mid_close": row["mid_close"],
            "mid_display": row["mid_display"],
            "mid_target": row["mid_target"],
            "flat_ticks": row["flat_ticks"],
            "class_a": row["class_a"],
            "class_b": row["class_b"],
            "special": bool(row["special"]),
            "unscorable": row["unscorable"],
            "scored_at": row["scored_at"],
        },
    }
