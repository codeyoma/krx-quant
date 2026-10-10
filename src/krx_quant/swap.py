"""Model swap on the always-on server: the night's promotion list applied at 08:00, a
rollback in one command (``model_shelf``), and the models it runs published for the
next night.

At 08:00 each trading day the latest list of an earlier night is applied, if it was
final by 07:30; otherwise yesterday's models run on. The running engine notices a model
file that changed (the swap, ``krx-quant rollback``) and switches to it before its next
bar close; the server then publishes what it runs (``live.json`` and the models), the
champions of the next night.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from asyncio.subprocess import PIPE, STDOUT
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from krx_quant.algorithms import Algorithm, Model, load_models
from krx_quant.engine import PredictionEngine
from krx_quant.feeds.base import KST, kst_day
from krx_quant.market_calendar import MarketCalendar
from krx_quant.model_shelf import Shelf
from krx_quant.nightly import LIVE, Night, model_key, promotions_key
from krx_quant.notify import Alerts
from krx_quant.object_store import ObjectStore, get_json, put_json

log = logging.getLogger(__name__)

SWAP_AT = time(8, 0)
DEADLINE = time(7, 30)  # a list final later than this waits for the next one


@dataclass(frozen=True)
class Applied:
    """A promotion list the morning swap looked at, and what came of it."""

    night: Night
    applied: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "day": self.night.day.isoformat(),
            "finalized_at": self.night.finalized_at,
            "applied": self.applied,
            "reason": self.reason,
            "promoted": self.night.promoted,
            "reports": [
                {"algorithm": r["algorithm"], "promoted": r["promoted"], "reason": r["reason"]}
                for r in self.night.reports
            ],
        }


def latest_night(store: ObjectStore, before: date) -> Night | None:
    """The promotion list of the latest night before ``before``."""
    nights = sorted(
        date.fromisoformat(key.removeprefix("promotions/").removesuffix(".json"))
        for key in store.keys("promotions/")
    )
    earlier = [d for d in nights if d < before]
    if not earlier:
        return None
    data = get_json(store, promotions_key(earlier[-1]))
    return None if data is None else Night.from_dict(data)


def apply_promotions(store: ObjectStore, shelf: Shelf, *, today: date, at: datetime) -> Applied | None:
    """The morning swap: install the latest night's promoted models, if final by 07:30
    ``today`` and not applied before. None without any list.
    """
    night = latest_night(store, today)
    if night is None:
        return None
    finalized = datetime.fromisoformat(night.finalized_at)
    deadline = datetime.combine(today, DEADLINE, KST)
    if finalized > deadline:
        reason = f"final at {finalized.astimezone(KST):%m-%d %H:%M}, after 07:30: yesterday's models run on"
        return Applied(night, False, reason)
    if shelf.applied(night.day):
        return Applied(night, True, "applied before")
    for name, version in night.promoted.items():
        data = get_json(store, model_key(name, version))
        if data is None:
            log.error("promoted %s is not in the store; %s keeps its model", version, name)
            continue
        shelf.install(name, Model.from_dict(data), f"promotion {night.day}", at)
    promoted = ", ".join(f"{n} {v}" for n, v in night.promoted.items()) or "nothing promoted"
    return Applied(night, True, promoted)


async def morning_swap(
    store: ObjectStore, shelf: Shelf, engine: PredictionEngine, *, today: date, at: datetime
) -> Applied | None:
    """Apply the night's list, switch the engine, and show the night in its status."""
    applied = await asyncio.to_thread(apply_promotions, store, shelf, today=today, at=at)
    if applied is not None:
        engine.training = applied.as_dict()
    reload_models(engine, shelf)
    return applied


def night_status(store: ObjectStore, shelf: Shelf, today: date) -> Applied | None:
    """The latest night up to ``today``, and whether its promotions run yet."""
    night = latest_night(store, today + timedelta(days=1))
    if night is None:
        return None
    if shelf.applied(night.day):
        return Applied(night, True, "applied")
    return Applied(night, False, "waiting for 08:00")


def reload_models(engine: PredictionEngine, shelf: Shelf) -> list[str]:
    """Switch the engine to the model files that changed; the names switched."""
    changed = []
    for name, model in load_models(shelf.state_dir, engine.algorithms).items():
        if engine.models.get(name) != model:
            log.info("%s now runs %s", name, model.version)
            engine.models[name] = model
            changed.append(name)
    return changed


def publish_live(store: ObjectStore, shelf: Shelf, algorithms: Sequence[Algorithm]) -> None:
    """Store the models the server runs, and which they are (``live.json``)."""
    live = {}
    for name, model in load_models(shelf.state_dir, algorithms).items():
        live[name] = model.version
        if model.trained is not None and store.digest(model_key(name, model.version)) is None:
            put_json(store, model_key(name, model.version), model.as_dict())
    put_json(store, LIVE, live)


WATCH_SECONDS = 30.0  # how soon the engine notices a model file that changed
_TRAINING_ALERT = "training"


async def keep_models(
    store: ObjectStore | None,
    shelf: Shelf,
    engine: PredictionEngine,
    calendar: MarketCalendar,
    notify: Callable[[str], Awaitable[bool]],
    now: Callable[[], datetime],
) -> None:
    """The collector's model task, until cancelled: the morning swap on trading days (at
    once if started between 08:00 and the open), the engine switched to any model file that
    changes, and what it runs published. Without a store only the files are watched.
    """
    if store is not None:
        status = await asyncio.to_thread(night_status, store, shelf, kst_day(now()))
        engine.training = None if status is None else status.as_dict()
    await _publish(store, shelf, engine)
    swap_at = _first_swap(calendar, now())
    while True:
        await asyncio.sleep(WATCH_SECONDS)
        try:
            if store is not None and now() >= swap_at:
                applied = await morning_swap(store, shelf, engine, today=kst_day(swap_at), at=now())
                swap_at = _next_on_trading_day(calendar, SWAP_AT, now())
                if applied is not None and (not applied.applied or applied.night.promoted):
                    await notify(_swap_notice(applied))
                await _publish(store, shelf, engine)
            elif reload_models(engine, shelf):
                await _publish(store, shelf, engine)
        except Exception:
            log.exception("the model swap failed")


async def keep_training(
    train_at: time,
    calendar: MarketCalendar,
    alerts: Alerts,
    now: Callable[[], datetime],
    refresh: Callable[[], Awaitable[None]],
) -> None:
    """Start ``krx-quant train`` in its own process at ``train_at`` each trading day, until
    cancelled. It sends its own summary; a failure raises an alert, cleared by the next
    night that trains.
    """
    while True:
        start = _next_on_trading_day(calendar, train_at, now())
        await asyncio.sleep(max(0.0, (start - now()).total_seconds()))
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "krx_quant.cli", "train", stdout=PIPE, stderr=STDOUT
        )
        output, _ = await process.communicate()
        text = output.decode(errors="replace").strip()
        if process.returncode == 0:
            log.info("nightly training finished:\n%s", text)
            alerts.resolve(_TRAINING_ALERT, "밤 학습이 다시 됩니다.")
        else:
            log.error("nightly training failed (exit %s):\n%s", process.returncode, text)
            last = text.splitlines()[-1] if text else f"exit {process.returncode}"
            alerts.raise_(_TRAINING_ALERT, f"밤 학습이 실패했습니다: {last}")
        await refresh()


async def _publish(store: ObjectStore | None, shelf: Shelf, engine: PredictionEngine) -> None:
    if store is None:
        return
    try:
        await asyncio.to_thread(publish_live, store, shelf, engine.algorithms)
    except Exception:
        log.exception("could not publish the models the server runs")


def _swap_notice(applied: Applied) -> str:
    if not applied.applied:
        return f"{applied.night.day} 밤 학습 결과를 쓰지 않습니다: {applied.reason}"
    changes = ", ".join(f"{name} → {version}" for name, version in applied.night.promoted.items())
    return f"모델 교체 ({applied.night.day} 밤 학습): {changes}"


def _first_swap(calendar: MarketCalendar, started: datetime) -> datetime:
    """Today's 08:00 if the collector starts before the open on a trading day, else the next."""
    local = started.astimezone(KST)
    session = calendar.session(local.date())
    if session is not None and local < session.open:
        return datetime.combine(local.date(), SWAP_AT, KST)
    return _next_on_trading_day(calendar, SWAP_AT, started)


def _next_on_trading_day(calendar: MarketCalendar, at: time, after: datetime) -> datetime:
    """The first ``at`` on a trading day later than ``after``."""
    day = after.astimezone(KST).date()
    while True:
        moment = datetime.combine(day, at, KST)
        if moment > after and calendar.is_trading_day(day):
            return moment
        day += timedelta(days=1)
