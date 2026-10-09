"""Engine API v1: what the Fincept screen reads, as JSON over HTTP.

Endpoints (all under ``/v1``):

- ``GET /status``: the clock, the market phase, the last bar close, pending outcomes and,
  live, the KIS connection and when each feed last delivered;
- ``GET /symbols``: the predicted symbols with their reference price and flat band now;
- ``GET /symbols/{symbol}/latest``: the predictions of the symbol's latest bar close;
- ``GET /symbols/{symbol}/predictions?limit=N&algorithm=A``: the predictions of the last
  N bar closes (default 100, at most 1000), newest first, with their outcomes;
- ``GET /symbols/{symbol}/bars``: today's finished one-minute bars, oldest first;
- ``GET /algorithms``: each registered algorithm's card, features and current model;
- ``GET /leaderboard?symbol=&period=&basis=&special=``: algorithms ranked by the lower
  bound of their weakest lift over the baselines, with their stats;
- ``GET /algorithms/{name}/stats?symbol=&period=&basis=&special=``: one algorithm's
  stats, by symbol, 30-minute slot and model version, with its calibration curve.

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
from datetime import date, datetime, timedelta
from functools import partial
from typing import Any

from aiohttp import web

from krx_quant.baselines import BASELINES
from krx_quant.engine import PredictionEngine
from krx_quant.feeds.base import KST, kst_day
from krx_quant.predictions import iso
from krx_quant.pricing import Direction, flat_ticks, tick_size
from krx_quant.stats import CalibrationBin, Entry, Interval, Scored, entry, leaderboard

ENGINE_KEY = web.AppKey("engine", PredictionEngine)
MODE_KEY = web.AppKey("mode", str)
LiveStatus = Callable[[], dict[str, Any]]
LIVE_STATUS_KEY: web.AppKey[LiveStatus] = web.AppKey("live_status")

# Readable as is (curl, logs): indented, and Korean as UTF-8 rather than \u escapes.
_dumps = partial(json.dumps, ensure_ascii=False, indent=2)

MIN_SAMPLE_KEY = web.AppKey("min_sample", int)
DEFAULT_MIN_SAMPLE = 200
PERIODS = {"today": 1, "5d": 5, "20d": 20}  # days with predictions
SLOT = timedelta(minutes=30)

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000


def create_app(
    engine: PredictionEngine,
    *,
    mode: str = "replay",
    live_status: LiveStatus | None = None,
    min_sample: int = DEFAULT_MIN_SAMPLE,
) -> web.Application:
    app = web.Application()
    app[ENGINE_KEY] = engine
    app[MODE_KEY] = mode
    app[MIN_SAMPLE_KEY] = min_sample
    if live_status is not None:
        app[LIVE_STATUS_KEY] = live_status
    app.router.add_get("/v1/status", _status)
    app.router.add_get("/v1/symbols", _symbols)
    app.router.add_get("/v1/symbols/{symbol}/latest", _latest)
    app.router.add_get("/v1/symbols/{symbol}/predictions", _predictions)
    app.router.add_get("/v1/symbols/{symbol}/bars", _bars)
    app.router.add_get("/v1/algorithms", _algorithms)
    app.router.add_get("/v1/leaderboard", _leaderboard)
    app.router.add_get("/v1/algorithms/{name}/stats", _algorithm_stats)
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
    algorithms = []
    for a in engine.algorithms:
        model = engine.models[a.name]
        algorithms.append(
            {
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
                "model": model.as_dict(),
            }
        )
    return _json({"algorithms": algorithms})


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
    days: list[date]
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
    days = [today] if period == "today" else engine.store.prediction_days(today)[: PERIODS[period]]
    return _Query(None if symbol == "all" else symbol, period, days, basis, special == "include")


def _scored(rows: list[dict[str, Any]], basis: str) -> dict[str, list[Scored]]:
    """Each algorithm's predictions scored on ``basis``."""
    out: dict[str, list[Scored]] = {}
    for r in rows:
        outcome = r["class_a"] if basis == "a" else r["class_b"]
        start = r["mid_close"] if basis == "a" else r["mid_display"]
        if outcome is None or start is None or r["mid_target"] is None:
            continue
        out.setdefault(r["algorithm"], []).append(
            Scored(
                algorithm=r["algorithm"],
                model_version=r["model_version"],
                symbol=r["symbol"],
                bar_close=datetime.fromisoformat(r["bar_close"]),
                p_up=r["p_up"],
                p_flat=r["p_flat"],
                p_down=r["p_down"],
                outcome=Direction(outcome),
                start_mid=start,
                target_mid=r["mid_target"],
            )
        )
    return out


def _split(by_algorithm: dict[str, list[Scored]]) -> tuple[dict[str, list[Scored]], dict[str, list[Scored]]]:
    names = {b.name for b in BASELINES}
    baselines = {name: by_algorithm.get(name, []) for name in sorted(names)}
    algorithms = {name: records for name, records in by_algorithm.items() if name not in names}
    return algorithms, baselines


async def _leaderboard(request: web.Request) -> web.Response:
    engine = request.app[ENGINE_KEY]
    query = _query(request, engine)
    rows = engine.store.scored(query.days, query.symbol, query.special)
    min_sample = request.app[MIN_SAMPLE_KEY]

    def build() -> dict[str, Any]:
        algorithms, baselines = _split(_scored(rows, query.basis))
        for a in engine.algorithms:
            algorithms.setdefault(a.name, [])
        board = leaderboard(algorithms, baselines, min_sample, engine.fee_rate)
        return {
            **query.describe(),
            "min_sample": min_sample,
            "ranked": [_entry(e) for e in board.ranked],
            "unranked": [_entry(e) for e in board.unranked],
            "baselines": [_entry(e) for e in board.baselines],
        }

    return _json(await asyncio.to_thread(build))


async def _algorithm_stats(request: web.Request) -> web.Response:
    engine = request.app[ENGINE_KEY]
    name = request.match_info["name"]
    query = _query(request, engine)
    rows = engine.store.scored(query.days, query.symbol, query.special)
    known = {a.name for a in engine.algorithms} | {b.name for b in BASELINES} | {r["algorithm"] for r in rows}
    if name not in known:
        raise _error(web.HTTPNotFound, f"unknown algorithm {name!r}")

    def build() -> dict[str, Any]:
        algorithms, baselines = _split(_scored(rows, query.basis))
        records = baselines.get(name) or algorithms.get(name, [])

        def by(key: Callable[[Scored], str]) -> dict[str, Any]:
            groups: dict[str, list[Scored]] = {}
            for r in records:
                groups.setdefault(key(r), []).append(r)
            return {k: _entry(entry(name, g, baselines, engine.fee_rate)) for k, g in sorted(groups.items())}

        overall = entry(name, records, baselines, engine.fee_rate)
        return {
            "algorithm": name,
            **query.describe(),
            "overall": {**_entry(overall), "calibration": [_bin(b) for b in overall.summary.calibration]},
            "by_symbol": by(lambda r: r.symbol),
            "by_slot": by(_slot),
            "by_model_version": by(lambda r: r.model_version),
        }

    return _json(await asyncio.to_thread(build))


def _slot(r: Scored) -> str:
    """The 30-minute slot of the bar the prediction was made at the close of."""
    start = (r.bar_close - timedelta(minutes=1)).astimezone(KST)
    minute = start.minute - start.minute % 30
    return f"{start.hour:02d}:{minute:02d}"


def _interval(i: Interval | None) -> dict[str, Any] | None:
    return None if i is None else {"value": i.value, "low": i.low, "high": i.high, "n": i.n}


def _entry(e: Entry) -> dict[str, Any]:
    s = e.summary
    out: dict[str, Any] = {
        "rank": e.rank,
        "algorithm": e.algorithm,
        "model_versions": e.model_versions,
        "n": s.n,
        "accuracy": _interval(s.accuracy),
        "precision": _interval(s.precision),
        "coverage": _interval(s.coverage),
        "brier": _interval(s.brier),
        "pnl": {"trades": s.pnl.trades, "total_ticks": s.pnl.total, "per_trade": _interval(s.pnl.per_trade)},
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


def _bin(b: CalibrationBin) -> dict[str, Any]:
    return {"low": b.low, "high": b.high, "count": b.count, "mean_p": b.mean_p, "observed": b.observed}


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
