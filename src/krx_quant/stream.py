"""The Engine API's stream (``GET /v1/stream``): what the engine does, as it does it, over a
WebSocket.

Every event is one JSON text message with its ``type`` and ``at`` (the engine clock when it
was sent):

- ``prediction``: predictions just displayed for a bar close (baselines, an algorithm);
- ``consensus``: a consensus just displayed, ``kind`` ``fast`` or ``final``;
- ``outcome``: predictions just scored, with their outcomes;
- ``health``: the KIS status, on connecting and whenever the connection or a feed's gap
  changes (live and demo only);
- ``lifecycle``: an algorithm's status changed, by the daily review or a command run in
  another process;
- ``bar``: a symbol's one-minute bar being made, its values so far (#110), at most once a
  poll for each symbol and only when they changed;
- ``kind_prediction``: predictions of a kind besides the direction (#179) just displayed for
  a bar close (its baselines, or an algorithm), the kind named ``prediction_kind``;
- ``kind_answer``: a kind's answers just made (four minutes after their bar closes), what its
  predictions there are scored on.

The new kinds' events name their kind ``prediction_kind``: ``kind`` already says which
consensus a ``consensus`` event carries. A client that does not know an event's ``type``
ignores it (the screen does), so every connection gets them.

Predictions are told by the engine as it stores them; status changes, the KIS status and
the bars being made are polled (every second). Clients send nothing. A client that falls
behind by ``QUEUE_SIZE`` events is closed (code 1013) and should reconnect and read the
REST endpoints again.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from typing import Any

from aiohttp import WSCloseCode, web

from krx_quant.consensus import FAST, FINAL
from krx_quant.engine import (
    Change,
    KindAnswered,
    KindPredictionsStored,
    OutcomesScored,
    PredictionEngine,
)
from krx_quant.predictions import iso
from krx_quant.touch import TARGET_VERSION, WINDOW

log = logging.getLogger(__name__)

LiveStatus = Callable[[], dict[str, Any]]
QUEUE_SIZE = 1000  # events a client may fall behind by
_CLOSE_SECONDS = 5.0  # shutting down waits this long for the clients to close

_dumps = partial(json.dumps, ensure_ascii=False)
_KINDS = {FAST: "fast", FINAL: "final"}


@dataclass(eq=False)  # each client is itself
class _Client:
    ws: web.WebSocketResponse
    queue: asyncio.Queue[dict[str, Any]] = field(default_factory=lambda: asyncio.Queue(QUEUE_SIZE))


class EventStream:
    """Fans the engine's events out to every connected client."""

    def __init__(self, engine: PredictionEngine, live_status: LiveStatus | None, poll_seconds: float) -> None:
        self._engine = engine
        self._live_status = live_status
        self._poll_seconds = poll_seconds
        self._clients: set[_Client] = set()
        self._health: Any = None  # what the last health event showed
        self._closing: set[asyncio.Task[Any]] = set()
        self._last_status = engine.store.last_status_id()
        self._bars: dict[str, dict[str, Any]] = {}  # each symbol's bar as last sent

    def changed(self, change: Change) -> None:
        """The engine's listener."""
        if not self._clients:
            return
        if isinstance(change, OutcomesScored):
            self._publish({"type": "outcome", "outcomes": self._items(change.ids)})
            return
        if isinstance(change, KindPredictionsStored | KindAnswered):
            self._kind_changed(change)
            return
        bar_close = iso(change.bar_close)
        made = [i for name, ids in change.ids.items() if name not in _KINDS for i in ids]
        if made:
            self._publish({"type": "prediction", "bar_close": bar_close, "predictions": self._items(made)})
        for name, kind in _KINDS.items():
            if name in change.ids:
                items = self._items(change.ids[name])
                self._publish(
                    {"type": "consensus", "kind": kind, "bar_close": bar_close, "predictions": items}
                )

    def _items(self, ids: list[int]) -> list[dict[str, Any]]:
        """The predictions as the stream shows them: with their symbol, as several go together."""
        return [{"symbol": row["symbol"], **prediction_json(row)} for row in self._engine.store.rows(ids)]

    def _kind_changed(self, change: KindPredictionsStored | KindAnswered) -> None:
        """A kind's predictions displayed, or its answers made (#179)."""
        kind = self._engine.kind_named(change.kind)
        if kind is None:
            return
        store = self._engine.store
        if isinstance(change, KindAnswered):
            answers = store.kind_answers_at(kind.name, kind.answer_version, change.keys)
            if answers:
                self._publish(
                    {
                        "type": "kind_answer",
                        "prediction_kind": kind.name,
                        "answers": [{"symbol": a["symbol"], **kind_answer_json(a)} for a in answers],
                    }
                )
            return
        ids = [i for made in change.ids.values() for i in made]
        rows = store.kind_rows(kind.name, kind.answer_version, ids)
        if rows:
            self._publish(
                {
                    "type": "kind_prediction",
                    "prediction_kind": kind.name,
                    "bar_close": iso(change.bar_close),
                    "predictions": [{"symbol": r["symbol"], **kind_prediction_json(r)} for r in rows],
                }
            )

    async def run(self) -> None:
        """Poll the status changes and the KIS status until cancelled."""
        while True:
            try:
                for row in self._engine.store.status_changes(self._last_status):
                    self._last_status = row["id"]
                    change = {
                        "at": row["at"],
                        "from": row["previous"],
                        "to": row["status"],
                        "reason": row["reason"],
                    }
                    self._publish({"type": "lifecycle", "algorithm": row["algorithm"], "change": change})
                self._publish_bars()
                if self._live_status is not None:
                    kis = self._live_status()
                    shown = _health_key(kis)
                    if shown != self._health:
                        self._health = shown
                        self._publish({"type": "health", "kis": kis})
            except Exception:
                log.exception("stream poll failed")
            await asyncio.sleep(self._poll_seconds)

    async def handle(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        client = _Client(ws)
        self._clients.add(client)
        if self._live_status is not None:
            client.queue.put_nowait(self._event({"type": "health", "kis": self._live_status()}))
        sender = asyncio.create_task(self._send(client))
        try:
            async for _ in ws:  # clients send nothing; reading notices a close
                pass
        finally:
            self._clients.discard(client)
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
        return ws

    async def close(self) -> None:
        """Close every client at once (the server is shutting down), waiting a moment at most."""
        closing = [
            c.ws.close(code=WSCloseCode.GOING_AWAY, message=b"engine shutting down") for c in self._clients
        ]
        for task in self._closing:
            task.cancel()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(*closing, *self._closing, return_exceptions=True), _CLOSE_SECONDS
            )

    def _publish_bars(self) -> None:
        """Each symbol's latest bar, if it changed since it was last sent."""
        for symbol in self._engine.symbols:
            bar = self._engine.last_bar(symbol)
            if bar is None:
                continue
            shown = {
                "start": iso(bar.start),
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
                "trades": bar.trades,
            }
            if self._bars.get(symbol) != shown:
                self._bars[symbol] = shown
                self._publish({"type": "bar", "symbol": symbol, "bar": shown})

    def _publish(self, body: dict[str, Any]) -> None:
        event = self._event(body)
        for client in list(self._clients):
            try:
                client.queue.put_nowait(event)
            except asyncio.QueueFull:
                log.warning("stream client %s fell behind; closed", client.ws)
                self._clients.discard(client)
                task = asyncio.create_task(
                    client.ws.close(code=WSCloseCode.TRY_AGAIN_LATER, message=b"too slow")
                )
                self._closing.add(task)
                task.add_done_callback(self._closing.discard)

    def _event(self, body: dict[str, Any]) -> dict[str, Any]:
        return {"type": body["type"], "at": iso(self._engine.clock.now()), **body}

    async def _send(self, client: _Client) -> None:
        try:
            while True:
                event = await client.queue.get()
                await client.ws.send_str(_dumps(event))
        except ConnectionError:  # closed meanwhile (too slow, gone, or shutting down)
            pass


def _health_key(kis: dict[str, Any]) -> Any:
    """What a health event changes on: the connection and each feed's gap, not its lag."""
    return kis["connected"], sorted((f["feed"], f["key"], f["gap"] or "") for f in kis["feeds"])


# Why a prediction scored without a touch label has none (#170): its bar close was never
# labelled (before the recording began, 2026-10-12, or not filled in yet).
NO_TOUCH = "no touch label"


def prediction_json(row: dict[str, Any]) -> dict[str, Any]:
    """A stored prediction as the API shows it.

    Its outcome is scored on the bar close's touch label (#170, ``touch``). For one release
    ``class_a`` and ``class_b`` both carry that label too, and ``unscorable`` why it has none,
    so a screen from before reads the new label where it read the old bases; the mid-points
    stay as stored."""
    scored = row["scored_at"] is not None
    label = row.get("touch_class")
    why = row.get("touch_unscorable") if row.get("touch_path") is not None else NO_TOUCH
    return {
        "algorithm": row["algorithm"],
        "model_version": row["model_version"],
        "bar_close": row["bar_close"],
        "displayed_at": row["displayed_at"],
        "target_at": row["target_at"],
        "status": row["status"],
        "blocked": row.get("blocked"),
        "votes": None
        if row.get("votes_up") is None
        else {"up": row["votes_up"], "flat": row["votes_flat"], "down": row["votes_down"]},
        "pooled": row.get("pooled"),
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
            "class_a": label,
            "class_b": label,
            "special": bool(row["special"]),
            "unscorable": None if label is not None else why,
            "scored_at": row["scored_at"],
            "touch": _touch_json(row),
        },
    }


def _touch_json(row: dict[str, Any]) -> dict[str, Any] | None:
    """The bar close's touch label (#167), what the outcome is scored on (#170); none where it
    was scored before the engine recorded it."""
    path = row.get("touch_path")
    if path is None:
        return None
    return {
        "target_version": TARGET_VERSION,
        "class": row["touch_class"],
        "unscorable": row["touch_unscorable"],
        **json.loads(path),
    }


def kind_prediction_json(row: dict[str, Any]) -> dict[str, Any]:
    """A stored prediction of a kind besides the direction (#179) as the API shows it: its
    output (the three probabilities, ``p_<class>`` in the kind's order, then the kind's
    fields) and, once its four minutes are over, its bar close's answer, every predictor's
    of the kind there. A prediction not made (status other than ``ok``) has neither, nor a
    target time: it is not scored."""
    made = row["status"] == "ok"
    return {
        "algorithm": row["algorithm"],
        "model_version": row["model_version"],
        "bar_close": row["bar_close"],
        "displayed_at": row["displayed_at"],
        "target_at": iso(datetime.fromisoformat(row["bar_close"]) + WINDOW) if made else None,
        "status": row["status"],
        "output": row["output"] if made else None,
        "answer": _answer_json(row) if made else None,
    }


def _answer_json(row: dict[str, Any]) -> dict[str, Any] | None:
    """A prediction's bar close's answer (``kind_answers``, joined as ``answer_*``); None before
    its four minutes are over."""
    if row.get("answer_scored_at") is None:
        return None
    return {
        "answer_version": row["answer_version"],
        "class": row["answer_class"],
        "unscorable": row["answer_unscorable"],
        "special": bool(row["answer_special"]),
        "scored_at": row["answer_scored_at"],
        "path": row["answer_path"],
    }


def kind_answer_json(answer: dict[str, Any]) -> dict[str, Any]:
    """A kind's answer of a bar close (``PredictionStore.kind_answers_at``) as the API shows it."""
    return {
        "bar_close": answer["bar_close"],
        "answer_version": answer["answer_version"],
        "class": answer["class"],
        "unscorable": answer["unscorable"],
        "special": bool(answer["special"]),
        "scored_at": answer["scored_at"],
        "path": answer["path"],
    }
