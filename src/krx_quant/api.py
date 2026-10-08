"""Engine API v1: what the Fincept screen reads, as JSON over HTTP.

Endpoints (all under ``/v1``):

- ``GET /status``: the clock, the market phase, the last bar close, pending outcomes and,
  live, the KIS connection and when each feed last delivered;
- ``GET /symbols``: the predicted symbols with their reference price and flat band now;
- ``GET /symbols/{symbol}/latest``: the predictions of the symbol's latest bar close;
- ``GET /symbols/{symbol}/predictions?limit=N&algorithm=A``: the predictions of the last
  N bar closes (default 100, at most 1000), newest first, with their outcomes;
- ``GET /symbols/{symbol}/bars``: today's finished one-minute bars, oldest first;
- ``GET /algorithms``: each registered algorithm's card, features and current model.

Times are ISO 8601 in KST. On the server, compose publishes it on the Tailscale address only.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from aiohttp import web

from krx_quant.engine import PredictionEngine
from krx_quant.feeds.base import kst_day
from krx_quant.predictions import iso
from krx_quant.pricing import flat_ticks, tick_size

ENGINE_KEY = web.AppKey("engine", PredictionEngine)
MODE_KEY = web.AppKey("mode", str)
LiveStatus = Callable[[], dict[str, Any]]
LIVE_STATUS_KEY: web.AppKey[LiveStatus] = web.AppKey("live_status")

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000


def create_app(
    engine: PredictionEngine,
    *,
    mode: str = "replay",
    live_status: LiveStatus | None = None,
) -> web.Application:
    app = web.Application()
    app[ENGINE_KEY] = engine
    app[MODE_KEY] = mode
    if live_status is not None:
        app[LIVE_STATUS_KEY] = live_status
    app.router.add_get("/v1/status", _status)
    app.router.add_get("/v1/symbols", _symbols)
    app.router.add_get("/v1/symbols/{symbol}/latest", _latest)
    app.router.add_get("/v1/symbols/{symbol}/predictions", _predictions)
    app.router.add_get("/v1/symbols/{symbol}/bars", _bars)
    app.router.add_get("/v1/algorithms", _algorithms)
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
    return web.json_response(body)


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
    return web.json_response({"symbols": symbols})


async def _latest(request: web.Request) -> web.Response:
    engine, symbol = _engine_and_symbol(request)
    rows = engine.store.latest(symbol)
    return web.json_response(
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
    return web.json_response({"symbol": symbol, "predictions": [_prediction(r) for r in rows]})


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
    return web.json_response({"symbol": symbol, "bars": bars})


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
                "model": {"version": model.version, "params": dict(model.params), "trained": model.trained},
            }
        )
    return web.json_response({"algorithms": algorithms})


def _engine_and_symbol(request: web.Request) -> tuple[PredictionEngine, str]:
    engine = request.app[ENGINE_KEY]
    symbol = request.match_info["symbol"]
    if symbol not in engine.symbols:
        raise _error(web.HTTPNotFound, f"unknown symbol {symbol!r}")
    return engine, symbol


def _error(kind: type[web.HTTPError], message: str) -> web.HTTPError:
    return kind(text=json.dumps({"error": message}), content_type="application/json")


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
