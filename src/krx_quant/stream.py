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
  another process.

Predictions are told by the engine as it stores them; status changes and the KIS status
are polled. Clients send nothing. A client that falls behind by ``QUEUE_SIZE`` events is
closed (code 1013) and should reconnect and read the REST endpoints again.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from aiohttp import WSCloseCode, web

from krx_quant.consensus import FAST, FINAL
from krx_quant.engine import OutcomesScored, PredictionEngine, PredictionsStored
from krx_quant.predictions import iso

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

    def changed(self, change: PredictionsStored | OutcomesScored) -> None:
        """The engine's listener."""
        if not self._clients:
            return
        if isinstance(change, OutcomesScored):
            self._publish({"type": "outcome", "outcomes": self._items(change.ids)})
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


def prediction_json(row: dict[str, Any]) -> dict[str, Any]:
    """A stored prediction as the API shows it."""
    scored = row["scored_at"] is not None
    return {
        "algorithm": row["algorithm"],
        "model_version": row["model_version"],
        "bar_close": row["bar_close"],
        "displayed_at": row["displayed_at"],
        "target_at": row["target_at"],
        "status": row["status"],
        "blocked": row.get("blocked"),
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
