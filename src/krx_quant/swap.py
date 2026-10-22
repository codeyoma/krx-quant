"""Model swap on the always-on server: the night's promotion list applied at 08:00, a
rollback in one command (``model_shelf``), and the models it runs published for the
next night.

At 08:00 each trading day the latest night's list not applied yet is, if it was final by
07:30 and every model it names is in the store; otherwise yesterday's models run on. The
running engine notices a model file that changed (the swap, ``krx-quant fit``,
``krx-quant rollback``) and switches to it within half a minute; the server then
publishes what it runs (``live.json`` and the models), the champions of the next night.

The nightly training runs at ``KRX_TRAIN_AT`` on trading days: here, in its own process
(``KRX_TRAIN_ON=server``; its whole output and exit code in ``state/logs/training/<day>.log``,
#356), or on EC2 Spot started by AWS at the same time (``spot``, #23).
On Spot, the server publishes what the run needs from it, waits for the night's result in
the store until the next trading day's 07:30, and sends its summary (the Spot run has no
Telegram token). The scheduled jobs (#215) wait for the night either way (``NightTraining``).

Each night's outcome is kept in the state folder (#355, ``training_nights``). A failure is
alerted with the exit code and the last step the training started; from ``STREAK_ALERT`` nights
in a row whose list does not run, each new one is alerted, restarts or not (``TrainingWatch``).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from krx_quant.algorithms import Algorithm, KindAlgorithm, Model, load_models
from krx_quant.archive import NIGHTLY_ARCHIVE
from krx_quant.climate import CLIMATE, load_climate, load_tables
from krx_quant.config import Settings
from krx_quant.cost_guard import training_stopped
from krx_quant.daily import DailyAlgorithm
from krx_quant.engine import PredictionEngine
from krx_quant.feeds.base import KST, kst_day
from krx_quant.log_files import KRX_QUANT, Child, night_file, run_logged
from krx_quant.log_files import TRAINING as TRAINING_LOGS
from krx_quant.market_calendar import MarketCalendar, next_on_trading_day
from krx_quant.model_shelf import Shelf
from krx_quant.nightly import (
    LIVE,
    TRAINING_CALENDAR_KEY,
    TRAINING_SETTINGS_KEY,
    Night,
    model_key,
    promotions_key,
    training_settings,
)
from krx_quant.notify import Alerts
from krx_quant.object_store import ObjectStore, file_digest, get_json, put_json
from krx_quant.training_nights import Failure, Outcome, TrainingNights, exit_text, last_step

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
# From this many nights in a row whose list does not run (failed, or not applied), each new
# one is alerted (#355).
STREAK_ALERT = 2
TRAIN_COMMAND = (*KRX_QUANT, "train")
# How often the server looks for the result of a night trained on EC2 Spot (#23), and the
# last such night whose summary it sent (in the state directory).
SPOT_POLL_SECONDS = 300.0
SUMMARIZED_FILE = "spot_summarized.json"


class NightTraining:
    """The nightly training as the scheduled jobs see it (#215): they wait while it runs, or
    is due to start, but no longer than ``TRAINING_TIMEOUT`` after it started. A training
    process here is stopped by then; a night on Spot whose result never comes (the Spot run
    failed, or the Cost Guard turned it off) must not hold the jobs all night.

    ``keep_training`` tells it when the next night starts, and when one starts and ends.
    """

    def __init__(self) -> None:
        self.next_start: datetime | None = None
        self.running_since: datetime | None = None
        self._changed = asyncio.Event()

    def due(self, at: datetime) -> None:
        self.next_start = at
        self._change()

    def started(self, at: datetime, next_start: datetime) -> None:
        self.running_since, self.next_start = at, next_start
        self._change()

    def finished(self) -> None:
        self.running_since = None
        self._change()

    def busy_until(self, now: datetime) -> datetime | None:
        """Until when the jobs wait at ``now``; None if no training runs or is due."""
        since = self.running_since
        if since is None and self.next_start is not None and self.next_start <= now:
            since = self.next_start  # due: keep_training starts it at once
        if since is None or now >= since + TRAINING_TIMEOUT:
            return None
        return since + TRAINING_TIMEOUT

    async def changed(self) -> None:
        """Return once the training has started, ended or been scheduled anew."""
        await self._changed.wait()

    def _change(self) -> None:
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()


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


async def show_night(
    store: ObjectStore,
    shelf: Shelf,
    engine: PredictionEngine,
    today: date,
    watch: TrainingWatch | None = None,
) -> None:
    """Show the latest night in the engine's status, and each algorithm's report of it; with
    ``watch``, the last night applied and the nights not applied since (#355)."""
    status = await asyncio.to_thread(night_status, store, shelf, today)
    engine.training = _shown(status, watch)
    engine.night_reports = {} if status is None else reports_by_algorithm(status.night)


NO_NIGHT = "no night's promotion list yet"


def _shown(status: NightStatus | None, watch: TrainingWatch | None) -> dict[str, Any] | None:
    """``/v1/status``'s ``training``: the latest night's list and whether it runs (None without
    one); with ``watch``, the nights not applied (#355), also before any list was stored, the
    night's own fields empty then: nights that all failed are shown too."""
    if status is None:
        if watch is None:
            return None
        night: dict[str, Any] = {
            "day": None,
            "finalized_at": None,
            "promoted": {},
            "reports": [],
            "applied": False,
        }
        return {**night, "reason": NO_NIGHT, **watch.fields()}
    return status.as_dict() if watch is None else {**status.as_dict(), **watch.fields()}


def reports_by_algorithm(night: Night) -> dict[str, dict[str, Any]]:
    """The night's reports as stored, by algorithm: ``/v1/algorithms`` shows each one's
    validation from them (#165), from memory, never reading the store on a request."""
    return {r["algorithm"]: r for r in night.reports}


async def morning_swap(
    store: ObjectStore,
    shelf: Shelf,
    engine: PredictionEngine,
    *,
    today: date,
    at: datetime,
    watch: TrainingWatch | None = None,
) -> NightStatus | None:
    """Apply the night's list, switch the engine, and show the night in its status."""
    status = await asyncio.to_thread(apply_promotions, store, shelf, today=today, at=at)
    if status is not None:
        engine.training = _shown(status, watch)
        engine.night_reports = reports_by_algorithm(status.night)
    elif watch is not None:  # no list yet: the nights not applied go on showing (#355)
        engine.training = _shown(None, watch)
    reload_models(engine, shelf)
    return status


def reload_models(engine: PredictionEngine, shelf: Shelf) -> bool:
    """Switch the engine to the model files that changed; whether any did. Those of the
    algorithms waiting for their pre-validation too, so they start on their latest, and of
    the kinds besides the direction (#179) and of the daily kinds (#264); with the touch
    target, the climate baseline's table (#169); and the kinds' baselines' tables (#180, the
    range's climate)."""
    models = load_models(
        shelf.state_dir, [*engine.registered, *engine.kind_registered, *engine.daily_registered]
    )
    if CLIMATE in engine.baseline_names:
        models[CLIMATE] = load_climate(shelf.state_dir)
    models.update(load_tables(shelf.state_dir, engine.kinds))
    return engine.switch_models(models)


def publish_live(
    store: ObjectStore, shelf: Shelf, algorithms: Sequence[Algorithm | KindAlgorithm | DailyAlgorithm]
) -> None:
    """Store the models the server runs, and which they are (``live.json``): those of the
    algorithms waiting for their pre-validation too, the models they will start on, so the
    night compares each challenger with the model installed."""
    live = {}
    for name, model in load_models(shelf.state_dir, algorithms).items():
        live[name] = model.version
        if model.trained is not None and store.digest(model_key(name, model.version)) is None:
            put_json(store, model_key(name, model.version), model.as_dict(blob=True))
    put_json(store, LIVE, live)


async def keep_models(
    store: ObjectStore | None,
    shelf: Shelf,
    engine: PredictionEngine,
    calendar: MarketCalendar,
    notify: Callable[[str], Awaitable[bool]],
    now: Callable[[], datetime],
    watch: TrainingWatch | None = None,
) -> None:
    """The collector's model task, until cancelled: the morning swap on trading days (at
    once if started between 08:00 and the open), the engine switched to any model file that
    changes, and what it runs published. Without a store only the files are watched. With
    ``watch``, a night not applied by the swap counts to the nights in a row (#355).
    """
    swap_at = _first_swap(calendar, now())
    if store is not None:
        try:
            await show_night(store, shelf, engine, kst_day(now()), watch)
            await asyncio.to_thread(
                publish_live,
                store,
                shelf,
                [*engine.registered, *engine.kind_registered, *engine.daily_registered],
            )
        except Exception:
            log.exception("could not read the last night or publish the live models")
    while True:
        await asyncio.sleep(WATCH_SECONDS)
        try:
            if store is not None and now() >= swap_at:
                today = kst_day(swap_at)
                swap_at = next_on_trading_day(calendar, SWAP_AT, now())
                status = await morning_swap(store, shelf, engine, today=today, at=now(), watch=watch)
                if status is not None and (not status.applied or status.night.promoted):
                    await notify(_swap_notice(status))
                if watch is not None:
                    watch.morning()
                await asyncio.to_thread(
                    publish_live,
                    store,
                    shelf,
                    [*engine.registered, *engine.kind_registered, *engine.daily_registered],
                )
            elif reload_models(engine, shelf) and store is not None:  # a fit or a rollback
                await show_night(store, shelf, engine, kst_day(now()), watch)
                await asyncio.to_thread(
                    publish_live,
                    store,
                    shelf,
                    [*engine.registered, *engine.kind_registered, *engine.daily_registered],
                )
        except Exception:
            log.exception("the model swap failed")


async def keep_training(
    train_at: time,
    calendar: MarketCalendar,
    alerts: Alerts,
    now: Callable[[], datetime],
    refresh: Callable[[], Awaitable[None]],
    run_night: Callable[[date], Awaitable[str | None]] | None = None,
    first: Callable[[MarketCalendar, time, datetime], datetime] | None = None,
    training: NightTraining | None = None,
    watch: TrainingWatch | None = None,
    logs: Path | None = None,
) -> None:
    """Start ``krx-quant train --day <day>`` in its own process at ``train_at`` each trading
    day, until cancelled; at once for a night a restart interrupted or missed (``CATCH_UP``).
    The process sends its own summary; a failure raises an alert, cleared by the next night
    that trains. With ``run_night`` (the training on EC2 Spot), that runs the night instead
    (None once done, else why not), and ``first`` picks the first night to run. ``training``
    is told when each night is due, starts and ends (the scheduled jobs wait for it, #215).
    With ``watch`` (#355), each night's outcome is kept and the alerts are ``TrainingWatch``'s.
    ``logs`` (``state/logs``) gets each night's training output here (#356).
    """

    async def run(day: date) -> Failure | None:
        if run_night is None:
            return await _train(day, logs=logs)
        why = await run_night(day)
        return None if why is None else Failure(why)

    schedule = training or NightTraining()
    start = (first or _first_training)(calendar, train_at, now())
    while True:
        schedule.due(start)  # before the first await: a job catching up at start waits for it
        await asyncio.sleep(max(0.0, (start - now()).total_seconds()))
        day = night_to_train(calendar, start)
        start = next_on_trading_day(calendar, train_at, max(now(), start))
        schedule.started(now(), start)
        try:
            failure = await run(day)
        except Exception as e:
            log.exception("could not run the nightly training")
            failure = Failure(str(e))
        finally:
            schedule.finished()
        if watch is not None:
            try:
                watch.ended(day, failure)
            except Exception:  # the outcome's file: the alert still goes
                log.exception("could not keep the night's outcome")
                _alert(alerts, day, failure)
        else:
            _alert(alerts, day, failure)
        try:
            await refresh()
        except Exception:
            log.exception("could not read the night's result")


def _alert(alerts: Alerts, day: date, failure: Failure | None) -> None:
    """A night's end as alerted without a ``TrainingWatch``."""
    if failure is None:
        alerts.resolve(_TRAINING_ALERT, "밤 학습이 다시 됩니다.")
    else:
        alerts.raise_(_TRAINING_ALERT, f"{day} 밤 학습이 실패했습니다: {failure.reason}")


class TrainingWatch:
    """The nights whose promotion list does not run, counted across restarts and deploys (#355,
    ``training_nights``): each night's outcome kept, its failure alerted with the exit code and the
    last step it started; from ``STREAK_ALERT`` nights in a row on, each new such night alerted
    with them all (once: a restart does not repeat it), whether it failed or the morning swap
    passed without its list. ``fields`` are ``/v1/status``'s."""

    def __init__(
        self, nights: TrainingNights, alerts: Alerts, calendar: MarketCalendar, now: Callable[[], datetime]
    ) -> None:
        self.log = nights
        self._alerts = alerts
        self._calendar = calendar
        self._now = now
        outcomes = nights.outcomes()
        if outcomes and not outcomes[max(outcomes)].ok:  # a failure alerted before a restart
            alerts.mark(_TRAINING_ALERT)

    def ended(self, day: date, failure: Failure | None) -> None:
        """A night's training ended: kept, and alerted."""
        self.log.record(
            Outcome(
                day,
                failure is None,
                self._now(),
                None if failure is None else failure.exit_code,
                None if failure is None else failure.step,
                None if failure is None else failure.reason,
            )
        )
        if failure is None:
            self._alerts.resolve(_TRAINING_ALERT, "밤 학습이 다시 됩니다.")
            return
        text = f"{day} 밤 학습이 실패했습니다: {failure.reason}"
        if not self._remind(text):
            self._alerts.raise_(_TRAINING_ALERT, text)

    def morning(self) -> None:
        """After the morning swap: a night it passed without its list counts too."""
        self._remind(None)

    def fields(self) -> dict[str, Any]:
        return self.log.fields(self._calendar, SWAP_AT, self._now())

    def _remind(self, text: str | None) -> bool:
        """Alert the nights in a row not applied, once per night, from ``STREAK_ALERT`` of them
        on (after ``text``); whether it did."""
        missed = self.log.not_applied(self._calendar, SWAP_AT, self._now())
        if len(missed) < STREAK_ALERT or self.log.alerted_for() == missed[-1]:
            return False
        applied = self.log.last_applied()
        outcomes = self.log.outcomes()
        failed = sum(1 for d in missed if d in outcomes and not outcomes[d].ok)
        streak = (
            f"밤 학습이 거래일 밤 {len(missed)}번 연속 적용되지 않았습니다"
            f"(마지막으로 적용된 밤 {applied or '없음'}, 그 뒤 {', '.join(f'{d:%m-%d}' for d in missed)}"
            f", 그중 학습 실패 {failed}번). 그동안 마지막으로 적용된 모델이 돕니다."
        )
        self._alerts.raise_(_TRAINING_ALERT, streak if text is None else f"{text}. {streak}", again=True)
        self.log.mark_alerted(missed[-1])
        return True


async def _train(
    day: date,
    command: Sequence[str] = TRAIN_COMMAND,
    timeout: timedelta = TRAINING_TIMEOUT,
    logs: Path | None = None,
) -> Failure | None:
    """Run the night's training; None if it finished, else why not: the exit code and the last
    step it started (#355), also when it is stopped at ``timeout``. Its whole output and exit
    code also go to the night's file as they come (``training/<day>.log`` under ``logs``, #356),
    and to Loki when it is set, labelled ``process="training"`` (#358)."""
    ran = await run_logged(  # read as it comes: what it wrote is kept if it is stopped
        (*command, "--day", day.isoformat()),
        timeout,
        night_file(logs, TRAINING_LOGS, day),
        merge=True,  # stdout and stderr in the order written
        child=Child.training(day),  # its lines' copy in Loki (#358)
    )
    code = None if ran.timed_out else ran.exit_code
    text = ran.stdout.strip()
    step = last_step(text)
    started = f"마지막으로 시작한 단계 {step or '없음'}"
    if code is None:
        log.error("nightly training of %s stopped after %s:\n%s", day, timeout, text)
        return Failure(f"not done in {timeout}, {started}", None, step)
    if code == 0:
        log.info("nightly training of %s finished:\n%s", day, text)
        return None
    log.error("nightly training of %s failed (exit %s):\n%s", day, code, text)
    return Failure(f"{exit_text(code)}, {started}", code, step)


def publish_training_inputs(store: ObjectStore, settings: Settings) -> None:
    """Put what a night trained on EC2 Spot needs from this server in the store: the
    settings training reads, and the market calendar (``krx-quant train --from-store``)."""
    put_json(store, TRAINING_SETTINGS_KEY, training_settings(settings))
    calendar = settings.state_dir / "market_calendar.json"
    if calendar.exists():
        store.put(TRAINING_CALENDAR_KEY, calendar, file_digest(calendar))


def spot_night_deadline(calendar: MarketCalendar, day: date) -> datetime:
    """When the night of ``day``, trained elsewhere, must be done: ``DEADLINE`` on the next
    trading day (a later list is not applied that morning)."""
    return next_on_trading_day(calendar, DEADLINE, datetime.combine(day, time.max, KST))


def first_spot_wait(calendar: MarketCalendar, train_at: time, started: datetime) -> datetime:
    """The last scheduled Spot training if its night is still due (before the next trading
    day's ``DEADLINE``: a collector restarted at 06:00 still waits for it), else the next."""
    local = started.astimezone(KST)
    day = local.date()
    while not (calendar.is_trading_day(day) and datetime.combine(day, train_at, KST) <= local):
        day -= timedelta(days=1)
    last = datetime.combine(day, train_at, KST)
    if local < spot_night_deadline(calendar, night_to_train(calendar, last)):
        return last
    return next_on_trading_day(calendar, train_at, started)


async def await_spot_night(
    store: ObjectStore,
    calendar: MarketCalendar,
    day: date,
    alerts: Alerts,
    now: Callable[[], datetime],
    state_dir: Path,
    poll_seconds: float = SPOT_POLL_SECONDS,
) -> str | None:
    """Wait for the night of ``day``, trained on EC2 Spot, to leave its promotion list in
    the store, and send its summary once (the Spot run has no Telegram token; a restart
    waits again but does not send it again, unless it could not be sent): None once it came,
    or if the Cost Guard turned the Spot training off this month; else, by the deadline, why
    not. A store that cannot be read is tried again.
    """
    deadline = spot_night_deadline(calendar, day)
    while True:
        try:
            data = await asyncio.to_thread(get_json, store, promotions_key(day))
        except (OSError, ValueError, BotoCoreError, ClientError) as e:
            log.warning("could not read the night's promotion list: %s", e)
        else:
            if data is not None:
                if _summarized(state_dir) == day:
                    return None
                if await alerts.deliver(Night.from_dict(data).summary()):
                    _mark_summarized(state_dir, day)
                    return None
                if now() >= deadline:
                    return None  # the night came; its summary waits for a later wait
                # Not sent (Telegram unreachable): tried again until the deadline.
        if now() >= deadline:
            if training_stopped(state_dir, now()):
                log.info("no Spot training of %s: turned off for the month's cost cap", day)
                return None
            return f"Spot 학습 결과가 {deadline:%Y-%m-%d %H:%M}까지 오지 않았습니다"
        await asyncio.sleep(poll_seconds)


def _summarized(state_dir: Path) -> date | None:
    """The last night trained on Spot whose summary was sent."""
    try:
        return date.fromisoformat(json.loads((state_dir / SUMMARIZED_FILE).read_text())["night"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _mark_summarized(state_dir: Path, day: date) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / SUMMARIZED_FILE).write_text(json.dumps({"night": day.isoformat()}))


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
