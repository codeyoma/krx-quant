"""Model swap on the always-on server: the night's promotion list applied at 08:00, a
rollback in one command (``model_shelf``), and the models it runs published for the
next night.

At 08:00 each trading day the latest night's list not applied yet is, if it was final by
07:30 and every model it names is in the store; otherwise yesterday's models run on. The
running engine notices a model file that changed (the swap, ``krx-quant fit``,
``krx-quant rollback``) and switches to it within half a minute; the server then
publishes what it runs (``live.json`` and the models), the champions of the next night.

Until #23 moves the training to EC2, the collector also starts it here, in its own
process, at ``KRX_TRAIN_AT`` on trading days.
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
from krx_quant.archive import NIGHTLY_ARCHIVE
from krx_quant.engine import PredictionEngine
from krx_quant.feeds.base import KST, kst_day
from krx_quant.market_calendar import MarketCalendar, next_on_trading_day
from krx_quant.model_shelf import Shelf
from krx_quant.nightly import LIVE, Night, model_key, promotions_key
from krx_quant.notify import Alerts
from krx_quant.object_store import ObjectStore, get_json, put_json

log = logging.getLogger(__name__)

SWAP_AT = time(8, 0)
DEADLINE = time(7, 30)  # a list final later than this is not applied
WATCH_SECONDS = 30.0  # how soon the engine notices a model file that changed
# A night's training must be done well before the morning; a run still going is stopped.
TRAINING_TIMEOUT = timedelta(hours=6)
# A collector started this long after a scheduled training (a deploy, a crash) runs it at
# once: the night resumes from its checkpoints.
CATCH_UP = timedelta(hours=8)
_TRAINING_ALERT = "training"


@dataclass(frozen=True)
class NightStatus:
    """A night's promotion list as the server sees it."""

    night: Night
    applied: bool  # its promotions run (or it promoted nothing and was taken)
    reason: str

    def as_dict(self) -> dict[str, Any]:
        reports = [
            {"algorithm": r["algorithm"], "promoted": r["promoted"], "reason": r["reason"]}
            for r in self.night.reports
        ]
        return {**self.night.as_dict(), "applied": self.applied, "reason": self.reason, "reports": reports}


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


def apply_promotions(store: ObjectStore, shelf: Shelf, *, today: date, at: datetime) -> NightStatus | None:
    """The morning swap: install the promoted models of the latest night not applied yet, if
    its list was final by 07:30 ``today`` and every model it names is in the store. None
    without a list.

    A list final after 07:30 waits for the next morning, unless a later night replaces it.
    """
    night = latest_night(store, today)
    if night is None:
        return None
    if shelf.applied(night.day):
        return night_status(store, shelf, today)
    finalized = datetime.fromisoformat(night.finalized_at)
    if finalized > datetime.combine(today, DEADLINE, KST):
        reason = f"final at {finalized.astimezone(KST):%m-%d %H:%M}, after 07:30: yesterday's models run on"
        return NightStatus(night, False, reason)
    models = {name: get_json(store, model_key(name, version)) for name, version in night.promoted.items()}
    missing = [night.promoted[name] for name, data in models.items() if data is None]
    if missing:
        log.error("promoted %s not in the store; the list is not applied", ", ".join(missing))
        return NightStatus(night, False, f"not in the store: {', '.join(missing)}; yesterday's models run on")
    for name, data in models.items():
        shelf.install(name, Model.from_dict(data), f"promotion {night.day}", at)
    shelf.mark_applied(night.day)
    promoted = ", ".join(f"{n} {v}" for n, v in night.promoted.items()) or "nothing promoted"
    return NightStatus(night, True, promoted)


def night_status(store: ObjectStore, shelf: Shelf, today: date) -> NightStatus | None:
    """The latest night up to ``today``, and whether its promotions run now: a rollback or a
    fit since may have replaced them.
    """
    night = latest_night(store, today + timedelta(days=1))
    if night is None:
        return None
    if not shelf.applied(night.day):
        return NightStatus(night, False, "waiting for 08:00")
    replaced = {
        name: running
        for name, version in night.promoted.items()
        if (running := shelf.current_version(name)) != version
    }
    if replaced:
        now_running = ", ".join(f"{n} runs {v or 'its defaults'}" for n, v in replaced.items())
        return NightStatus(night, False, f"applied, then replaced: {now_running}")
    return NightStatus(night, True, "applied")


async def show_night(store: ObjectStore, shelf: Shelf, engine: PredictionEngine, today: date) -> None:
    """Show the latest night in the engine's status."""
    status = await asyncio.to_thread(night_status, store, shelf, today)
    engine.training = None if status is None else status.as_dict()


async def morning_swap(
    store: ObjectStore, shelf: Shelf, engine: PredictionEngine, *, today: date, at: datetime
) -> NightStatus | None:
    """Apply the night's list, switch the engine, and show the night in its status."""
    status = await asyncio.to_thread(apply_promotions, store, shelf, today=today, at=at)
    if status is not None:
        engine.training = status.as_dict()
    reload_models(engine, shelf)
    return status


def reload_models(engine: PredictionEngine, shelf: Shelf) -> bool:
    """Switch the engine to the model files that changed; whether any did."""
    return engine.switch_models(load_models(shelf.state_dir, engine.algorithms))


def publish_live(store: ObjectStore, shelf: Shelf, algorithms: Sequence[Algorithm]) -> None:
    """Store the models the server runs, and which they are (``live.json``)."""
    live = {}
    for name, model in load_models(shelf.state_dir, algorithms).items():
        live[name] = model.version
        if model.trained is not None and store.digest(model_key(name, model.version)) is None:
            put_json(store, model_key(name, model.version), model.as_dict())
    put_json(store, LIVE, live)


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
    swap_at = _first_swap(calendar, now())
    if store is not None:
        try:
            await show_night(store, shelf, engine, kst_day(now()))
            await asyncio.to_thread(publish_live, store, shelf, engine.algorithms)
        except Exception:
            log.exception("could not read the last night or publish the live models")
    while True:
        await asyncio.sleep(WATCH_SECONDS)
        try:
            if store is not None and now() >= swap_at:
                today = kst_day(swap_at)
                swap_at = next_on_trading_day(calendar, SWAP_AT, now())
                status = await morning_swap(store, shelf, engine, today=today, at=now())
                if status is not None and (not status.applied or status.night.promoted):
                    await notify(_swap_notice(status))
                await asyncio.to_thread(publish_live, store, shelf, engine.algorithms)
            elif reload_models(engine, shelf) and store is not None:  # a fit or a rollback
                await show_night(store, shelf, engine, kst_day(now()))
                await asyncio.to_thread(publish_live, store, shelf, engine.algorithms)
        except Exception:
            log.exception("the model swap failed")


async def keep_training(
    train_at: time,
    calendar: MarketCalendar,
    alerts: Alerts,
    now: Callable[[], datetime],
    refresh: Callable[[], Awaitable[None]],
) -> None:
    """Start ``krx-quant train --day <day>`` in its own process at ``train_at`` each trading
    day, until cancelled; at once for a night a restart interrupted or missed (``CATCH_UP``).
    The process sends its own summary; a failure raises an alert, cleared by the next night
    that trains.
    """
    start = _first_training(calendar, train_at, now())
    while True:
        await asyncio.sleep(max(0.0, (start - now()).total_seconds()))
        day = night_to_train(calendar, start)
        start = next_on_trading_day(calendar, train_at, max(now(), start))
        try:
            failure = await _train(day)
        except Exception as e:
            log.exception("could not run the nightly training")
            failure = str(e)
        if failure is None:
            alerts.resolve(_TRAINING_ALERT, "밤 학습이 다시 됩니다.")
        else:
            alerts.raise_(_TRAINING_ALERT, f"{day} 밤 학습이 실패했습니다: {failure}")
        try:
            await refresh()
        except Exception:
            log.exception("could not read the night's result")


async def _train(day: date) -> str | None:
    """Run the night's training; None if it finished, else why not."""
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "krx_quant.cli", "train", "--day", day.isoformat(), stdout=PIPE, stderr=STDOUT
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), TRAINING_TIMEOUT.total_seconds())
    except TimeoutError:
        process.kill()
        await process.wait()
        return f"not done in {TRAINING_TIMEOUT}"
    text = output.decode(errors="replace").strip()
    if process.returncode == 0:
        log.info("nightly training of %s finished:\n%s", day, text)
        return None
    log.error("nightly training of %s failed (exit %s):\n%s", day, process.returncode, text)
    return text.splitlines()[-1] if text else f"exit {process.returncode}"


def _swap_notice(status: NightStatus) -> str:
    if not status.applied:
        return f"{status.night.day} 밤 학습 결과를 쓰지 않습니다: {status.reason}"
    changes = ", ".join(f"{name} → {version}" for name, version in status.night.promoted.items())
    return f"모델 교체 ({status.night.day} 밤 학습): {changes}"


def _first_swap(calendar: MarketCalendar, started: datetime) -> datetime:
    """Today's 08:00 if the collector starts before the open on a trading day, else the next."""
    local = started.astimezone(KST)
    session = calendar.session(local.date())
    if session is not None and local < session.open:
        return datetime.combine(local.date(), SWAP_AT, KST)
    return next_on_trading_day(calendar, SWAP_AT, started)


def _first_training(calendar: MarketCalendar, train_at: time, started: datetime) -> datetime:
    """The last scheduled training if it was due less than ``CATCH_UP`` ago, else the next."""
    local = started.astimezone(KST)
    day = local.date()
    while not (calendar.is_trading_day(day) and datetime.combine(day, train_at, KST) <= local):
        day -= timedelta(days=1)
    last = datetime.combine(day, train_at, KST)
    return last if local - last < CATCH_UP else next_on_trading_day(calendar, train_at, started)


def night_to_train(calendar: MarketCalendar, start: datetime) -> date:
    """The day a training started at ``start`` trains on: that day once its bars are
    archived (21:00), else the trading day before.
    """
    local = start.astimezone(KST)
    if local.time() >= NIGHTLY_ARCHIVE:
        return local.date()
    day = local.date() - timedelta(days=1)
    while not calendar.is_trading_day(day):
        day -= timedelta(days=1)
    return day
