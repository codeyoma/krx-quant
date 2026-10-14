"""Collector: subscribes to KIS real-time feeds, polls REST feeds, and records everything."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from functools import partial
from typing import Any

import aiohttp
from aiohttp import web

from krx_quant import kis
from krx_quant.algorithms import ALGORITHMS, load_models
from krx_quant.api import create_app
from krx_quant.archive import NIGHTLY_ARCHIVE, archive
from krx_quant.auth import TokenManager, tokens_and_client
from krx_quant.backfill import keep_history
from krx_quant.climate import load_climate
from krx_quant.clock import SessionClock, SystemClock
from krx_quant.config import Settings
from krx_quant.cost_guard import keep_costs
from krx_quant.econ_calendar import CalendarCollector, keep_calendar
from krx_quant.engine import Budget, PredictionEngine, run_live
from krx_quant.features import Leaders
from krx_quant.feeds import (
    BOOK,
    ETF_FALLBACKS,
    FALLBACKS,
    FOREIGN_ESTIMATE,
    INDEX,
    KOSPI200_FUTURES,
    KRX_TRADE,
    KST,
    MEMBER,
    NXT_TRADE,
    OPTION,
    PROGRAM,
    RAW_SCHEMA,
    TRADE,
    US_TRADE,
    USD_FUTURES,
    VI_STATUS,
    WS_FEEDS,
    Feed,
    RestFeed,
    us_daytime_key,
)
from krx_quant.feeds.base import kst_day
from krx_quant.health import FeedHealth, FeedState, GapKind
from krx_quant.lifecycle import Rules
from krx_quant.macro import MacroCollector, day_regime, keep_macro, macro_recorder
from krx_quant.market_calendar import MarketCalendar, Phase, calendar_store, next_on_trading_day
from krx_quant.masters import FrontMonths, MasterError, fetch_front_months
from krx_quant.model_shelf import Shelf
from krx_quant.notify import Alerts
from krx_quant.object_store import open_store
from krx_quant.polling import CALL_ERRORS, NORMALIZE_ERRORS, Poller, PollJob
from krx_quant.predictions import DB_NAME, PredictionStore, iso
from krx_quant.prevalidation import admits
from krx_quant.recorder import ParquetRecorder
from krx_quant.rescore import EngineLock, keep_touches_filled
from krx_quant.signals import Signals
from krx_quant.stream import LiveStatus
from krx_quant.swap import (
    await_spot_night,
    first_spot_wait,
    keep_models,
    keep_training,
    publish_training_inputs,
    show_night,
)

log = logging.getLogger(__name__)

Clock = Callable[[], datetime]

_WS_ALERT = "websocket"
_ARCHIVE_ALERT = "archive"
_API_ALERT = "api"

# Recorded days are archived this long after each trading day's close, and every night,
# after the NXT and KRX after-market close and the 20:30 backfill, for the rest of the
# day's rows and the day's bars (the nightly training starts after it, KRX_TRAIN_AT).
ARCHIVE_AFTER_CLOSE = timedelta(minutes=15)

# WebSocket approval keys last about a day; take a new one on reconnect after this.
_APPROVAL_MAX_AGE = timedelta(hours=20)

# How often buffered partitions whose time limit has passed are flushed.
_TICK_SECONDS = 1.0


def subscription_plan(settings: Settings, fronts: FrontMonths | None) -> list[tuple[Feed, str]]:
    """WebSocket registrations in priority order; the session limit cuts from the end.

    Market-wide context comes first so it survives however many symbols are added,
    then each stock's trades, book and program trading, then the tier-2 feeds.
    """
    plan: list[tuple[Feed, str]] = []
    if fronts is not None:
        plan += [(KOSPI200_FUTURES, fronts.kospi200_futures), (USD_FUTURES, fronts.usd_futures)]
    plan += [(US_TRADE, us_daytime_key(t)) for t in settings.us_symbols]
    plan += [(feed, sym) for sym in settings.symbols for feed in (TRADE, BOOK, PROGRAM)]
    plan += [(MEMBER, sym) for sym in settings.symbols]
    plan += [(INDEX, code) for code in settings.index_codes]
    if fronts is not None:
        plan += [(OPTION, code) for code in fronts.kospi200_options]
    # ETFs do not trade on NXT, and the integrated feed sent nothing for them (2026-10-08).
    plan += [(KRX_TRADE, etf) for etf in settings.etf_symbols]
    if settings.venue_trades:
        plan += [(feed, sym) for feed in (KRX_TRADE, NXT_TRADE) for sym in settings.symbols]
    return plan


# The phases in which a VI can matter to predictions: from the opening auction to the close.
_VI_PHASES = {Phase.OPENING_AUCTION, Phase.CONTINUOUS, Phase.CLOSING_AUCTION}


def rest_plan(settings: Settings, calendar: MarketCalendar) -> list[PollJob]:
    """Feeds that are only available over REST."""
    plan = [
        PollJob(FOREIGN_ESTIMATE, sym, settings.foreign_estimate_poll_seconds) for sym in settings.symbols
    ]
    # The day's VI list is asked for again and again; it is recorded when it changes.
    plan += [
        PollJob(
            VI_STATUS,
            sym,
            settings.vi_poll_seconds,
            changes_only=True,
            when=lambda now: calendar.phase(now) in _VI_PHASES,
        )
        for sym in settings.symbols
    ]
    return plan


async def front_months(
    session: aiohttp.ClientSession, settings: Settings, today: date, calendar: MarketCalendar
) -> FrontMonths | None:
    """Nearest derivative contracts on ``today`` from the KIS masters; configured futures codes win.

    If the masters cannot be read, configured futures codes are still used (without
    options); with no codes configured, derivative feeds are skipped (returns None).
    """
    try:
        found = await fetch_front_months(
            session, settings.master_url, today, calendar, settings.option_strikes_each_side
        )
    except (MasterError, aiohttp.ClientError, OSError, ValueError, TimeoutError) as e:
        if settings.kospi200_futures_code and settings.usd_futures_code:
            log.error("KIS masters unavailable (%s); using configured futures codes, no options", e)
            return FrontMonths(settings.kospi200_futures_code, settings.usd_futures_code, ())
        log.error("derivative feeds skipped: nearest contracts not found (%s)", e)
        return None
    if not found.kospi200_options:
        log.error("no KOSPI200 options month with an ATM strike in the KIS master: options not recorded")
    fronts = FrontMonths(
        settings.kospi200_futures_code or found.kospi200_futures,
        settings.usd_futures_code or found.usd_futures,
        found.kospi200_options,
    )
    log.info("nearest contracts: %s", fronts)
    return fronts


def started(settings: Settings) -> dict[str, Any]:
    """What an engine start is recorded with (``runs``, kind ``start``): the code and the
    settings its results depend on, so stats can be told apart by them (#85)."""
    rule = settings.signal_rule
    return {
        "commit": settings.commit,
        "fee_rate": settings.fee_rate,
        "signal_rule": None
        if rule is None
        else {"min_probability": rule.min_probability, "min_agree": rule.min_agree},
        "algorithm_seconds": settings.algorithm_seconds,
        "cpu_budget_seconds": settings.cpu_budget_seconds,
    }


async def run_collector(
    settings: Settings, *, stop: asyncio.Event, clock: Clock | SessionClock | None = None
) -> None:
    """Record the configured feeds and predict from them until ``stop`` is set, then flush
    and return.

    ``clock`` is the Session Clock, or a function giving the time (then sleeping is real).
    """
    if isinstance(clock, SessionClock):
        session_clock: SessionClock = clock
    else:
        session_clock = SystemClock() if clock is None else SystemClock(clock)
    now = session_clock.now
    recorder = ParquetRecorder(settings.data_dir, settings.flush_rows, settings.flush_seconds)
    calendar = calendar_store(settings)
    health = FeedHealth(recorder, now, calendar.calendar)
    store = PredictionStore(settings.state_dir / DB_NAME)
    engine = PredictionEngine(
        store,
        calendar.calendar,
        settings.symbols,
        settings.fee_rate,
        session_clock,
        models=load_models(settings.state_dir, ALGORITHMS),
        budget=Budget.of(settings),
        min_sample=settings.min_sample,
        leaders=Leaders.of(settings),
        regimes=partial(day_regime, settings.data_dir),  # recorded by the 07:30 macro read
        # A new algorithm waits for its pre-validation, on the target it runs.
        admits=admits(settings.state_dir, settings.target_version),
        commit=settings.commit,
        review_rules=Rules.of(settings),
        target_version=settings.target_version,  # the target learned and forecast (#169)
        climate=load_climate(settings.state_dir) if settings.target_version >= 2 else None,
    )
    store.add_run("start", engine.started_at, None, json.dumps(started(settings)))
    # Held while it runs: a rescore refuses to run beside it (#168). One running already
    # does not stop the recording; the past touch labels are filled once it is done.
    lock = EngineLock(settings.state_dir)
    if not lock.acquire():
        log.warning("%s is held: a rescore runs", lock.path)

    def on_event(feed: str, row: dict[str, Any]) -> None:
        # Recording comes first: a prediction bug must never stop it.
        try:
            engine.on_event(feed, row)
        except Exception:
            log.exception("the engine could not take a %s event", feed)

    try:
        async with aiohttp.ClientSession() as session:
            alerts = Alerts(
                session, settings.telegram_bot_token, settings.telegram_chat_id, settings.telegram_api_url
            )
            engine.notify = alerts.deliver  # retirement candidates
            if settings.signal_rule is not None:
                engine.signals = Signals(settings.signal_rule, alerts.signal)
            tokens, client = tokens_and_client(session, settings, now)
            await _warm_up(tokens)
            poller = Poller(
                client,
                recorder,
                now,
                stop,
                on_added=health.watch,
                on_received=health.received,
                on_event=on_event,
            )
            fronts = await front_months(session, settings, kst_day(now()), calendar.calendar)
            plan = subscription_plan(settings, fronts)
            cap = len(plan) if settings.ws_max_subscriptions is None else settings.ws_max_subscriptions
            for feed, key in plan[cap:]:
                _fall_back(poller, settings, feed.tr_id, key, "beyond the configured cap")
            for job in rest_plan(settings, calendar.calendar):
                poller.add(job)
            for feed, key in plan[:cap]:
                health.watch((feed.tr_id, key))
            stream = _Stream(
                session, settings, plan[:cap], recorder, now, stop, poller, health, alerts, on_event
            )
            background = [
                asyncio.create_task(_every(_TICK_SECONDS, recorder.flush_due, "periodic flush")),
                asyncio.create_task(
                    _every(settings.health_check_seconds, stream.check_stale, "health check")
                ),
                asyncio.create_task(keep_history(settings, client, calendar, now)),
                asyncio.create_task(run_live(engine, session_clock)),
            ]
            if settings.object_store is None:
                log.warning("KRX_OBJECT_STORE is not set: recorded days are not archived nor trained on")
            else:
                background.append(
                    asyncio.create_task(_keep_archived(settings, recorder, calendar.calendar, alerts, now))
                )
            object_store = open_store(settings)
            shelf = Shelf(settings.state_dir)
            background.append(
                asyncio.create_task(
                    keep_models(object_store, shelf, engine, calendar.calendar, alerts.deliver, now)
                )
            )
            if object_store is not None and settings.train_at is not None:
                trained_on = object_store
                spot = settings.train_on == "spot"

                async def publish() -> None:
                    """What the next Spot night needs from here; a failure waits for the next."""
                    try:
                        await asyncio.to_thread(publish_training_inputs, trained_on, settings)
                    except Exception:
                        log.exception("could not publish the training inputs")

                async def refresh() -> None:
                    if spot:  # for the next night, as the calendar stands now
                        await publish()
                    await show_night(trained_on, shelf, engine, kst_day(now()))

                async def on_spot(day: date) -> str | None:
                    return await await_spot_night(
                        trained_on, calendar.calendar, day, alerts, now, settings.state_dir
                    )

                if spot:
                    await publish()
                background.append(
                    asyncio.create_task(
                        keep_training(
                            settings.train_at,
                            calendar.calendar,
                            alerts,
                            now,
                            refresh,
                            on_spot if spot else None,
                            first_spot_wait if spot else None,
                        )
                    )
                )
            if settings.cost_budget is not None:
                background.append(asyncio.create_task(keep_costs(settings, alerts, now)))
            if settings.restart_at is not None:
                background.append(
                    asyncio.create_task(restart_each_day(settings.restart_at, calendar.calendar, now, stop))
                )
            if settings.macro_at is not None:
                macro_collector = MacroCollector(
                    settings, session, alerts, macro_recorder(recorder), calendar.calendar, now
                )
                background.append(
                    asyncio.create_task(
                        keep_macro(macro_collector, calendar.calendar, settings.macro_at, now)
                    )
                )
            if settings.calendar_every is not None:
                calendar_collector = CalendarCollector(
                    session=session,
                    data_dir=settings.data_dir,
                    countries=settings.calendar_countries,
                    alerts=alerts,
                    recorder=recorder,
                    now=now,
                    url=settings.calendar_url,
                )
                background.append(
                    asyncio.create_task(keep_calendar(calendar_collector, settings.calendar_every))
                )
            api = await _serve_api(settings, engine, _live_status(stream, health, now), alerts)
            # The past bar closes' touch labels, once the API answers (#168): the 06:00 deploy
            # checks it within 3 minutes, and the fill can take longer.
            background.append(asyncio.create_task(keep_touches_filled(settings, engine, lock)))
            try:
                await stream.run()
            finally:
                if api is not None:
                    await api.cleanup()
                await stream.close()
                for task in background:
                    task.cancel()
                await asyncio.gather(*background, return_exceptions=True)
                # Jobs may be queued on the rate limiter; at shutdown, drop them.
                await poller.close()
                await alerts.close()
    finally:
        health.close()
        recorder.close()
        store.close()
        lock.release()


async def restart_each_day(
    at: time,
    calendar: MarketCalendar,
    now: Callable[[], datetime],
    stop: asyncio.Event,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Stop the collector at ``at`` on the first trading day after the one it started on.

    Docker starts it again (``restart: unless-stopped``), and the new collector reads that
    day's KIS masters: the nearest futures, the options month not expired and their strikes
    around the money. A collector left running across days would keep an expired month, and
    strikes drifting from the money (#115). One started that morning (the 06:00 deploy) read
    them already. The sleep is a parameter for tests.
    """
    started = now()
    when = next_on_trading_day(calendar, at, started)
    if kst_day(when) == kst_day(started):
        when = next_on_trading_day(calendar, at, when)
    await sleep(max(0.0, (when - now()).total_seconds()))
    log.info("stopping for the daily restart: %s's contracts are read afresh", kst_day(when))
    stop.set()


async def _serve_api(
    settings: Settings,
    engine: PredictionEngine,
    live_status: LiveStatus,
    alerts: Alerts,
) -> web.AppRunner | None:
    """Start the Engine API; if it cannot listen, alert and go on recording without it."""
    if settings.api_port is None:
        return None
    runner = web.AppRunner(
        create_app(
            engine,
            mode="live",
            live_status=live_status,
            min_sample=settings.min_sample,
            data_dir=settings.data_dir,
            shelf=Shelf(settings.state_dir),
        ),
        access_log=None,
    )
    await runner.setup()
    try:
        await web.TCPSite(runner, settings.api_host, settings.api_port).start()
    except OSError as e:
        await runner.cleanup()
        log.error("Engine API not started on %s:%s: %s", settings.api_host, settings.api_port, e)
        alerts.raise_(_API_ALERT, f"엔진 API를 열지 못했습니다 ({e}). 녹화는 계속합니다.")
        return None
    log.info("Engine API on http://%s:%s/v1", settings.api_host, settings.api_port)
    return runner


def _live_status(stream: _Stream, health: FeedHealth, now: Clock) -> LiveStatus:
    def feed(f: FeedState, at: datetime) -> dict[str, Any]:
        last = f.last_received_at
        return {
            "feed": f.feed,
            "key": f.key,
            "last_received_at": iso(last) if last else None,
            "seconds_since": (at - last).total_seconds() if last else None,
            "gap": f.gap,
        }

    def status() -> dict[str, Any]:
        at = now()
        return {"connected": stream.connected, "feeds": [feed(f, at) for f in health.snapshot()]}

    return status


async def _keep_archived(
    settings: Settings,
    recorder: ParquetRecorder,
    calendar: MarketCalendar,
    alerts: Alerts,
    now: Clock,
) -> None:
    """Archive finished days at start, then every day after the close and at night.

    Buffered rows are written first so the archive holds everything recorded so far;
    rows recorded later go into the next run. A failure (a bad store setting included)
    raises one alert, cleared by the next run that archives everything; recording goes on.
    """
    # At start, today is left for later only while none of today's runs is due yet: a
    # collector restarted after 21:00 must not leave the finished day out until tomorrow.
    started = now()
    today = kst_day(started)
    due_today = _archive_times(calendar, today)[0] <= started.astimezone(KST)
    before: date | None = None if due_today else today
    target = started
    while True:
        try:
            store = open_store(settings)
            assert store is not None
            recorder.flush()
            report = await archive(settings.data_dir, store, before=before)
        except Exception:
            log.exception("archiving failed")
            alerts.raise_(_ARCHIVE_ALERT, "장 마감 데이터 업로드가 실패했습니다.")
        else:
            log.info("archived: %d uploaded, %d unchanged", report.uploaded, report.unchanged)
            if report.failed:
                alerts.raise_(
                    _ARCHIVE_ALERT, f"장 마감 데이터 업로드가 {len(report.failed)}개 파티션에서 실패했습니다."
                )
            else:
                alerts.resolve(_ARCHIVE_ALERT, "장 마감 데이터 업로드가 다시 됩니다.")
        before = None
        target = _next_archive_time(calendar, max(now(), target))
        await asyncio.sleep(max(0.0, (target - now()).total_seconds()))


def _archive_times(calendar: MarketCalendar, day: date) -> list[datetime]:
    """The scheduled archive runs on ``day``, in order."""
    session = calendar.session(day)
    times = [session.close + ARCHIVE_AFTER_CLOSE] if session is not None else []
    return [*times, datetime.combine(day, NIGHTLY_ARCHIVE, KST)]


def _next_archive_time(calendar: MarketCalendar, after: datetime) -> datetime:
    """The first scheduled archive run later than ``after``."""
    local = after.astimezone(KST)
    candidates = _archive_times(calendar, local.date()) + _archive_times(
        calendar, local.date() + timedelta(days=1)
    )
    return min(c for c in candidates if c > local)


async def _every(seconds: float, action: Callable[[], None], name: str) -> None:
    """Run ``action`` every ``seconds`` until cancelled; a failure is logged, not fatal."""
    while True:
        await asyncio.sleep(seconds)
        try:
            action()
        except Exception:
            log.exception("%s failed", name)


async def _warm_up(tokens: TokenManager) -> None:
    """Get a REST token at startup so problems show early; REST polls retry on their own."""
    try:
        await tokens.get()
    except (kis.KisError, aiohttp.ClientError, OSError, ValueError, KeyError) as e:
        log.warning("REST access token not available yet: %s", e)


async def measure_subscription_limit(settings: Settings) -> int:
    """Register real-time trade feeds one by one until KIS refuses; return how many it accepted.

    KIS allows one WebSocket session per app key, so run this while the collector is stopped.
    Every accepted registration is removed again before returning.
    """
    accepted: list[str] = []
    async with aiohttp.ClientSession() as session:
        approval = await _approval_key(session, settings)
        async with session.ws_connect(settings.kis_ws_url + kis.WS_PATH) as ws:
            try:
                for code in MEASURE_CODES:
                    await ws.send_str(kis.subscribe_message(approval, TRADE.tr_id, code))
                    async with asyncio.timeout(_ACK_TIMEOUT_SECONDS):
                        reply = await _ack(ws, code)
                    if reply.ok:
                        accepted.append(code)
                    elif reply.is_limit_rejection:
                        return len(accepted)
                    else:
                        raise kis.KisError(f"registration of {code} refused: {reply.code} {reply.message}")
                raise kis.KisError(f"KIS accepted all {len(MEASURE_CODES)} registrations; no limit found")
            finally:
                with contextlib.suppress(Exception):
                    for code in accepted:
                        await ws.send_str(kis.subscribe_message(approval, TRADE.tr_id, code, subscribe=False))


_ACK_TIMEOUT_SECONDS = 10.0

# Liquid KOSPI stocks to register while measuring; more than any documented session limit.
MEASURE_CODES = (
    "005930 000660 009150 005380 035420 035720 051910 006400 207940 068270 005490 000270 105560 "
    "055550 012330 028260 066570 003550 096770 034730 017670 030200 032830 086790 015760 018260 "
    "011200 010130 009540 024110 033780 003670 316140 000810 010950 011170 090430 251270 036570 "
    "323410 377300 352820 259960 047050 042700 012450 064350 329180 010140 000100 128940 097950 "
    "271560 004020 078930 021240 161390 006800 029780 071050"
).split()


async def _ack(ws: aiohttp.ClientWebSocketResponse, tr_key: str) -> kis.SystemMessage:
    """Read until the registration reply for ``tr_key``, answering heartbeats on the way."""
    async for msg in ws:
        if msg.type != aiohttp.WSMsgType.TEXT:
            continue
        message = kis.parse_message(msg.data)
        if isinstance(message, kis.SystemMessage):
            if message.is_pingpong:
                await ws.pong(msg.data.encode())
            elif message.tr_key == tr_key:
                return message
    raise kis.KisError("websocket closed while measuring")


async def _approval_key(session: aiohttp.ClientSession, settings: Settings) -> str:
    return await kis.issue_approval_key(
        session, settings.kis_rest_url, settings.kis_app_key, settings.kis_app_secret
    )


def fallback_for(settings: Settings, tr_id: str, key: str) -> RestFeed | None:
    """The REST feed polled instead when ``tr_id``'s registration of ``key`` is cut, if any."""
    # The ETF mapping only covers the ETF trade feed; a code also listed as a stock keeps
    # the stock fallbacks for its other feeds.
    etf = ETF_FALLBACKS.get(tr_id) if key in settings.etf_symbols else None
    return etf or FALLBACKS.get(tr_id)


@dataclass(frozen=True)
class FallbackCheck:
    """A REST fallback asked once: the event rows it would record, or why it failed."""

    cut_feed: str  # the WebSocket feed whose cut registration it is polled for
    key: str
    rest: RestFeed
    params: dict[str, str]
    rows: list[dict[str, Any]]
    error: str | None = None


async def check_fallbacks(settings: Settings, now: Clock | None = None) -> list[FallbackCheck]:
    """Ask the REST fallback of every registration in the subscription plan once, in the
    plan's order, and normalize the answers without recording them.

    REST only, one call each: safe to run beside the collector, to see live that each
    fallback answers and what it would record.
    """
    clock = now or (lambda: datetime.now(UTC))
    checks: list[FallbackCheck] = []
    async with aiohttp.ClientSession() as session:
        _, client = tokens_and_client(session, settings, clock)
        fronts = await front_months(session, settings, kst_day(clock()), calendar_store(settings).calendar)
        plan = subscription_plan(settings, fronts)
        # A derivative left out of the plan (its contract not found) is not checked: a failure.
        planned = {feed.tr_id for feed, _ in plan}
        for feed in (KOSPI200_FUTURES, USD_FUTURES, OPTION):
            if feed.tr_id not in planned:
                why = "no contract to ask for: the KIS masters could not be read"
                checks.append(FallbackCheck(feed.tr_id, "", FALLBACKS[feed.tr_id], {}, [], why))
        for feed, key in plan:
            fallback = fallback_for(settings, feed.tr_id, key)
            if fallback is None:
                continue
            at = clock()  # one time for the day asked for and the time received
            params = fallback.params(key, kst_day(at))
            try:
                body = await client.get(fallback.tr_id, fallback.path, params)
                rows = fallback.normalize(body, key, at.astimezone(UTC))
            except (*CALL_ERRORS, *NORMALIZE_ERRORS) as e:
                checks.append(
                    FallbackCheck(feed.tr_id, key, fallback, params, [], f"{type(e).__name__}: {e}")
                )
            else:
                checks.append(FallbackCheck(feed.tr_id, key, fallback, params, rows))
    return checks


def _fall_back(poller: Poller, settings: Settings, tr_id: str, key: str, reason: str) -> None:
    fallback = fallback_for(settings, tr_id, key)
    if fallback is None:
        log.error("%s %s not recorded (%s) and has no REST fallback", tr_id, key, reason)
        return
    log.warning(
        "%s %s %s: polling REST %s every %ss instead",
        tr_id,
        key,
        reason,
        fallback.tr_id,
        settings.fallback_poll_seconds,
    )
    poller.add(PollJob.fallback(fallback, key, settings.fallback_poll_seconds))


class _Stream:
    """The KIS WebSocket session: kept up across reconnects, every message recorded."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        settings: Settings,
        subscriptions: list[tuple[Feed, str]],
        recorder: ParquetRecorder,
        now: Clock,
        stop: asyncio.Event,
        poller: Poller,
        health: FeedHealth,
        alerts: Alerts,
        on_event: Callable[[str, dict[str, Any]], None],
    ) -> None:
        self._session = session
        self._settings = settings
        # Registered on every (re)connect; keys refused for the session limit leave the list.
        self._subscriptions = list(subscriptions)
        self._recorder = recorder
        self._now = now
        self._stop = stop
        self._poller = poller
        self._health = health
        self._alerts = alerts
        self._on_event = on_event
        self._approval: str | None = None
        self._approval_at: datetime | None = None
        self._acknowledged = False  # KIS answered a registration on the current connection
        self._recovery: asyncio.Task[None] | None = None

    async def run(self) -> None:
        """Stay connected until ``stop``: reconnect with exponential backoff and register
        every subscription again after each reconnect. The backoff only resets once KIS
        has answered a registration, so a server that drops every new connection at once
        is not hammered.
        """
        settings = self._settings
        delay = settings.reconnect_initial_seconds
        while not self._stop.is_set():
            self._acknowledged = False
            try:
                await self._ensure_approval()
                async with self._session.ws_connect(settings.kis_ws_url + kis.WS_PATH) as ws:
                    for feed, key in list(self._subscriptions):
                        await ws.send_str(self._subscribe_message(feed.tr_id, key))
                    await self._receive(ws)
            except (kis.KisError, aiohttp.ClientError, OSError) as e:
                if self._stop.is_set():
                    return
                if self._acknowledged:
                    delay = settings.reconnect_initial_seconds
                self._connection_lost(str(e))
                log.warning("KIS WebSocket down (%s); reconnecting in %.1fs", e, delay)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), delay)
                delay = min(delay * 2, settings.reconnect_max_seconds)

    @property
    def connected(self) -> bool:
        """KIS has answered a registration on the current connection."""
        return self._acknowledged

    async def close(self) -> None:
        if self._recovery is not None:
            self._recovery.cancel()
            await asyncio.gather(self._recovery, return_exceptions=True)

    def check_stale(self) -> None:
        """Alert, in one message, on feeds that went quiet; announce the ones that came back."""
        newly = self._health.check_stale()
        if newly:
            self._alerts.raise_many([_stale_name(k) for k in newly], "장중 데이터가 끊긴 피드")
        still = {_stale_name(k) for k in self._health.open_gaps(GapKind.STALE)}
        back = [name for name in self._alerts.active(_STALE_PREFIX) if name not in still]
        if back:
            self._alerts.resolve_many(back, "데이터가 다시 들어오는 피드")

    async def _ensure_approval(self) -> None:
        now = self._now()
        if self._approval is None or self._approval_at is None or now - self._approval_at > _APPROVAL_MAX_AGE:
            self._approval = await kis.issue_approval_key(
                self._session,
                self._settings.kis_rest_url,
                self._settings.kis_app_key,
                self._settings.kis_app_secret,
            )
            self._approval_at = now

    def _subscribe_message(self, tr_id: str, key: str) -> str:
        assert self._approval is not None
        return kis.subscribe_message(self._approval, tr_id, key)

    def _connection_lost(self, detail: str) -> None:
        self._acknowledged = False
        if self._recovery is not None:
            self._recovery.cancel()  # it did not stay up long enough to count as recovered
            self._recovery = None
        self._health.connection_lost(((f.tr_id, k) for f, k in self._subscriptions), detail)
        self._alerts.raise_(_WS_ALERT, f"KIS WebSocket 연결이 끊겼습니다 ({detail}). 재연결을 시도합니다.")

    def _acknowledged_on_this_connection(self) -> None:
        if self._acknowledged:
            return
        self._acknowledged = True
        if self._alerts.is_active(_WS_ALERT) and self._recovery is None:
            self._recovery = asyncio.create_task(self._announce_recovery())

    async def _announce_recovery(self) -> None:
        await asyncio.sleep(self._settings.recovery_hold_seconds)
        self._recovery = None
        self._alerts.resolve(_WS_ALERT, "KIS WebSocket이 다시 연결되어 구독을 복원했습니다.")

    async def _receive(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Read messages until the socket closes; ``stop`` closes it from a helper task.

        The read itself is never cancelled: cancelling an in-flight receive can drop a
        frame that has already arrived.
        """

        async def close_on_stop() -> None:
            await self._stop.wait()
            await ws.close()

        closer = asyncio.create_task(close_on_stop())
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle(ws, msg.data)
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    raise kis.KisError(f"websocket error: {ws.exception()}")
            if not self._stop.is_set():
                raise kis.KisError(f"websocket closed by KIS (code {ws.close_code})")
        finally:
            closer.cancel()
            # gather, not suppress(CancelledError): that would also swallow our own cancellation.
            await asyncio.gather(closer, return_exceptions=True)

    async def _handle(self, ws: aiohttp.ClientWebSocketResponse, text: str) -> None:
        """Record one message raw, then answer or normalize it.

        The raw copy is written before anything can fail, and a message that cannot be
        parsed or normalized is logged and skipped so one bad frame never stops recording.
        """
        at = self._now()
        day = kst_day(at)
        received_at = at.astimezone(UTC)
        raw_row = {"received_at": received_at, "message": text}
        try:
            message = kis.parse_message(text)
        except kis.KisError as e:
            self._recorder.record("raw", day, "_unparsed", "_", raw_row, RAW_SCHEMA)
            log.warning("unparseable message kept raw: %s", e)
            return

        if isinstance(message, kis.SystemMessage):
            self._recorder.record("raw", day, "_system", message.tr_key or "_", raw_row, RAW_SCHEMA)
            if message.is_pingpong:
                await ws.pong(text.encode())
                return
            self._acknowledged_on_this_connection()
            if not message.ok:
                await self._on_rejected(ws, message)
            elif message.tr_key:
                self._health.subscribed((message.tr_id, message.tr_key))
            return

        self._recorder.record("raw", day, message.tr_id, message.symbol, raw_row, RAW_SCHEMA)
        self._health.received((message.tr_id, message.symbol))
        feed = WS_FEEDS.get(message.tr_id)
        if feed is None or message.encrypted:
            log.warning(
                "not normalizing %s frame (known feed: %s, encrypted: %s)",
                message.tr_id,
                feed is not None,
                message.encrypted,
            )
            return
        try:
            rows = [
                feed.normalize(dict(zip(feed.columns, fields, strict=True)), received_at)
                for fields in message.records(len(feed.columns))
            ]
        except (kis.KisError, ValueError, KeyError, IndexError):
            log.exception("could not normalize %s frame; kept raw only", message.tr_id)
            return
        for row in rows:
            self._recorder.record("events", day, feed.tr_id, row["symbol"], row, feed.schema)
            self._on_event(feed.tr_id, row)

    async def _on_rejected(self, ws: aiohttp.ClientWebSocketResponse, message: kis.SystemMessage) -> None:
        key = (message.tr_id, message.tr_key or "")
        if message.is_limit_rejection and message.tr_key:
            # Moved to REST for good: not registered again on reconnects.
            self._subscriptions = [(f, k) for f, k in self._subscriptions if (f.tr_id, k) != key]
            self._health.covered_elsewhere(key)
            _fall_back(
                self._poller, self._settings, message.tr_id, message.tr_key, "refused for the session limit"
            )
        elif message.is_invalid_approval:
            log.error("KIS refused the approval key; taking a new one and reconnecting")
            self._approval = None
            await ws.close()
        else:
            log.error(
                "KIS rejected %s %s: %s %s", message.tr_id, message.tr_key, message.code, message.message
            )


_STALE_PREFIX = "stale "


def _stale_name(key: tuple[str, str]) -> str:
    return f"{_STALE_PREFIX}{key[0]} {key[1]}"
