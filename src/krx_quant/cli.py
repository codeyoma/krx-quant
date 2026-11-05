"""Command-line entry point: ``krx-quant <command>``."""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import logging
import math
import signal
import sys
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from aiohttp import web

from krx_quant import loki
from krx_quant.algorithms import ALGORITHMS, KIND_ALGORITHMS, RegistrationError
from krx_quant.archive import run_archive
from krx_quant.backfill import run_backfill
from krx_quant.climate_gate import climate_gate
from krx_quant.climate_prior import REPORT as CLIMATE_PRIOR_REPORT
from krx_quant.climate_prior import climate_prior_experiment
from krx_quant.climate_prior import lines as climate_prior_lines
from krx_quant.closing_flow_check import check_closing_flow, check_krx_net_assets
from krx_quant.collector import check_fallbacks, measure_subscription_limit, run_collector
from krx_quant.config import Settings, SettingsError, read_values
from krx_quant.daily import DailyKind
from krx_quant.daily_check import DAYS as DAILY_CHECK_DAYS
from krx_quant.daily_check import daily_check
from krx_quant.daily_training import daily_algorithm, fit_daily, prevalidate_daily
from krx_quant.demo import demo_app, demo_morning
from krx_quant.diagnose import DEFAULT_DAYS, diagnose
from krx_quant.doctor import doctor, doctor_settings
from krx_quant.error_breakdown import DEFAULT_ALGORITHM as ERROR_ALGORITHM
from krx_quant.error_breakdown import DEFAULT_DAYS as ERROR_DAYS
from krx_quant.error_breakdown import error_breakdown
from krx_quant.fake_kis import FakeKis
from krx_quant.feeds.base import KST
from krx_quant.gaps_probe import probe_gaps
from krx_quant.holdout import error_holdout
from krx_quant.kind_registry import DAILY_KINDS, kind_named
from krx_quant.kis import KisError
from krx_quant.krx_etf_probe import probe_krx_etf
from krx_quant.lifecycle import Status
from krx_quant.live_probe import probe_live_account
from krx_quant.macro import run_macro
from krx_quant.market_calendar import calendar_store, first_whole_day
from krx_quant.model_shelf import Shelf
from krx_quant.nav_probe import probe_nav
from krx_quant.neyman_backfill import neyman_backfill
from krx_quant.neyman_variants import neyman_variants
from krx_quant.night_probe import probe_night
from krx_quant.nightly import run_training
from krx_quant.nightly_hit_rates import HIT_RATE_NIGHTS, compared_nights
from krx_quant.nightly_hit_rates import lines as hit_rate_lines
from krx_quant.normal_order import REPORT as NORMAL_ORDER_REPORT
from krx_quant.normal_order import lines as normal_order_lines
from krx_quant.normal_order import normal_order_experiment
from krx_quant.notify import send_notice, send_test_alert
from krx_quant.object_store import ObjectStore, open_store
from krx_quant.order_probe import probe_kis_orders
from krx_quant.predictions import (
    DB_NAME,
    REVISION_CHANGED,
    PredictionStore,
    latest_revisions,
    read_only,
    smt_events,
)
from krx_quant.prevalidation import (
    PREVALIDATION_DAYS,
    REGISTERED_BEFORE,
    PrevalidationRunning,
    prevalidate,
    report_path,
)
from krx_quant.process_log import FORMAT as LOG_FORMAT
from krx_quant.process_log import open_process_log
from krx_quant.redaction import secrets_of
from krx_quant.replay import replay_day
from krx_quant.rescore import EngineRunning, rescore
from krx_quant.sample_keys_probe import probe_sample_keys
from krx_quant.sample_profile import LIMIT as SAMPLE_PROFILE_LIMIT
from krx_quant.sample_profile import PROFILED, sample_profile
from krx_quant.sample_profile import TOP as SAMPLE_PROFILE_TOP
from krx_quant.signal_review import DEFAULT_DAYS as SIGNAL_REVIEW_DAYS
from krx_quant.signal_review import signal_review
from krx_quant.simulation import (
    BAR_CLOSE_COMMAND,
    DAILY,
    SimulationRunning,
    report,
    simulate_bar_close,
    simulate_daily,
)
from krx_quant.smt import lines as smt_lines
from krx_quant.smt import reconstruct as reconstruct_smt
from krx_quant.smt_experiment import smt_experiment
from krx_quant.state_backup import KEY as STATE_BACKUP_KEY
from krx_quant.state_backup import BackupRefused, RestoreRefused, back_up, drill, restore
from krx_quant.structure_check import structure_check
from krx_quant.supply_probe import probe_supply
from krx_quant.trader import LIVE
from krx_quant.trader.limits import CONTROL_SECONDS
from krx_quant.trader.limits import ask as ask_trader
from krx_quant.trader.mock_check import check_mock_trades
from krx_quant.trader.notice_layout import fill_notice_layout
from krx_quant.trader.promotion import mode_at
from krx_quant.trader.refit import run_refit
from krx_quant.trader.report import run_night, run_report
from krx_quant.trader.run import run_trader
from krx_quant.trader.settings import TraderSettings
from krx_quant.trader.store import DB_NAME as TRADER_DB
from krx_quant.trader.store import TraderStore
from krx_quant.training import fit_algorithm

# A command's handler (#499): the parsed arguments in, the exit code out.
Run = Callable[[argparse.Namespace], int]


async def _collect() -> int:
    settings = Settings.from_env()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows: Ctrl+C raises KeyboardInterrupt instead
            pass
    logging.getLogger(__name__).info(
        "recording %s into %s", ",".join(settings.symbols), settings.data_dir.resolve()
    )
    await run_collector(settings, stop=stop)
    return 0


async def _trade() -> int:
    # The KIS keys are the collector's, which the trader never uses (#393): paper needs none, mock
    # its own (KRX_TRADER_KIS_*, #405) and live the live account's (KRX_TRADER_LIVE_KIS_*, #408), which
    # the trader checks as it starts. A trader that stops by itself (#408: the live mode's answer
    # changed at a pick) exits 0, and Docker starts it again (``restart: unless-stopped``).
    settings = Settings.from_env(require_kis=False)
    trader = TraderSettings.from_env()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows: Ctrl+C raises KeyboardInterrupt instead
            pass
    await run_trader(settings, trader, stop=stop)
    return 0


def _trade_control(action: str) -> int:
    """``krx-quant trade resume|rearm`` (#396): the trader's pause lifted, or its cumulative loss
    limit re-armed, written into its store for the running trader to read; whether it did."""
    settings = Settings.from_env(require_kis=False)
    mode = TraderSettings.from_env().mode  # a re-arm is of the mode the trader runs in
    store = TraderStore(settings.state_dir / TRADER_DB)
    try:
        if mode == LIVE:  # #408: a live trader runs as paper until the gate and the checks pass
            mode = mode_at(store.db)
        asked = ask_trader(store, action, mode, datetime.now(KST))
        if isinstance(asked, str):
            print(asked)
            return 1
        print(f"{action} asked for (control {asked.id}) at {asked.at.isoformat()}")
        deadline = time.monotonic() + 3 * CONTROL_SECONDS + 1
        while time.monotonic() < deadline:
            control = store.control(asked.id)
            if control is not None and control.applied_at is not None:
                print(f"the trader read it at {control.applied_at.isoformat()}")
                return 0
            time.sleep(CONTROL_SECONDS / 2)
    finally:
        store.close()
    print("the trader has not read it yet: it does when it runs (docker compose ps trader)")
    return 0


async def _refit_policy(engine_url: str | None, report: bool) -> int:
    """The trader's policy fitted again (#399); with ``report``, then the day's report above its
    output (#402): the scheduled job ``trader-report``'s, in the engine's container."""
    settings = Settings.from_env(require_kis=False)
    trader = TraderSettings.from_env()
    if report:
        return await run_night(settings, trader, datetime.now(KST), engine_url)
    return await run_refit(settings, trader, datetime.now(KST), engine_url)


async def _check_mock_trades(day: date | None) -> int:
    """A mock trading day's orders in the ledger against KIS's order inquiry (#405), read-only."""
    return await check_mock_trades(Settings.from_env(require_kis=False), datetime.now(KST), day)


async def _fill_notice_layout() -> int:
    """The mock fill notices the trader recorded, measured against the day's orders (#484),
    read-only."""
    return await fill_notice_layout(Settings.from_env(require_kis=False), datetime.now(KST))


def _trader_report(day: date | None) -> int:
    """The trader's report of a trading day (#402), read-only."""
    return run_report(Settings.from_env(require_kis=False), datetime.now(KST), day)


async def _measure() -> int:
    limit = await measure_subscription_limit(Settings.from_env())
    print(f"KIS accepted {limit} real-time registrations in one session.")
    print(f"Set KIS_WS_MAX_SUBSCRIPTIONS={limit} in .env to stay within it.")
    return 0


async def _check_fallbacks() -> int:
    checks = await check_fallbacks(Settings.from_env())
    for check in checks:
        query = " ".join(f"{k}={v}" for k, v in check.params.items())
        print(f"{check.cut_feed} {check.key} -> {check.rest.tr_id} {query}")
        if check.error is not None:
            print(f"  failed: {check.error}")
        elif not check.rows:
            print("  answered with nothing to record")
        for row in check.rows:
            print("  " + json.dumps(row, ensure_ascii=False, default=str))
    failed = sum(c.error is not None for c in checks)
    print(f"{len(checks) - failed} of {len(checks)} fallbacks answered")
    return 1 if failed else 0


async def _probe_night(day: date | None) -> int:
    print(await probe_night(Settings.from_env(require_kis=False), datetime.now(KST), day))
    return 0


def _probe_gaps() -> int:
    print(probe_gaps(Settings.from_env(require_kis=False), datetime.now(KST)))
    return 0


async def _probe_live_account() -> int:
    """The live account read by inquiry calls alone (#408): the trader's live key, no order."""
    return await probe_live_account(Settings.from_env(require_kis=False), datetime.now(KST))


async def _probe_kis_orders() -> int:
    """The trader's orders on KIS's mock server (#403): its own keys, not the collector's."""
    return await probe_kis_orders(Settings.from_env(require_kis=False), datetime.now(KST))


async def _check_closing_flow() -> int:
    print(await check_closing_flow(Settings.from_env(), datetime.now(KST)))
    return 0


def _check_krx_net_assets() -> int:
    print(check_krx_net_assets(Settings.from_env(require_kis=False), datetime.now(KST)))
    return 0


async def _probe_supply() -> int:
    calls, failed = await probe_supply(Settings.from_env(), datetime.now(KST).date())
    return 1 if failed == calls else 0  # nothing answered


async def _probe_nav() -> int:
    calls, failed = await probe_nav(Settings.from_env(), datetime.now(KST))
    return 1 if failed == calls else 0  # nothing answered


async def _probe_krx_etf() -> int:
    # The KIS keys are not required: without them the KRX part runs and the KIS comparison is skipped.
    calls, with_rows = await probe_krx_etf(Settings.from_env(require_kis=False), datetime.now(KST))
    return 0 if with_rows else 1  # no day answered with rows


async def _backfill() -> int:
    filled = await run_backfill(Settings.from_env())
    print(f"filled one-minute bars for {filled} series-days")
    return 0


async def _archive(recheck: bool) -> int:
    report = await run_archive(Settings.from_env(), recheck=recheck)
    print(f"archived: {report.uploaded} uploaded, {report.unchanged} unchanged, {len(report.failed)} failed")
    for partition in report.failed:
        print(f"failed: {partition}", file=sys.stderr)
    return 1 if report.failed else 0


def _state_store(settings: Settings) -> ObjectStore:
    store = open_store(settings)
    if store is None:
        raise SettingsError("KRX_OBJECT_STORE is not set")
    return store


def _backup_state() -> int:
    """The nightly backup of the server's state (#576), now."""
    settings = Settings.from_env(require_kis=False)
    try:
        made = back_up(settings.state_dir, _state_store(settings), datetime.now(KST), commit=settings.commit)
    except BackupRefused as e:
        print(e, file=sys.stderr)
        return 1
    print(f"backed up: {made.files} files, {made.size / 2**20:,.1f} MB in {made.seconds:.1f}s")
    return 0


def _restore_state(source: Path | None, replace: bool, if_backed_up: bool) -> int:
    """Put the last backup of the server's state back (#576), before the engine starts."""
    settings = Settings.from_env(require_kis=False)
    store = None if source is not None else open_store(settings)
    if source is None and store is None:
        if if_backed_up:
            print("KRX_OBJECT_STORE is not set: nothing restored")
            return 0
        raise SettingsError("KRX_OBJECT_STORE is not set")
    if if_backed_up and store is not None and store.digest(STATE_BACKUP_KEY) is None:
        print(f"no backup of the server state in the store ({STATE_BACKUP_KEY}): nothing restored")
        return 0
    try:
        done = restore(settings.state_dir, store, datetime.now(KST), source=source, replace=replace)
    except RestoreRefused as e:
        print(f"not restored: {e}", file=sys.stderr)
        return 1
    return 0 if done else 1


def _restore_drill() -> int:
    """The monthly restore drill (#576): the latest backup checked, the state folder left as it is."""
    settings = Settings.from_env(require_kis=False)
    return 0 if drill(settings.state_dir, _state_store(settings), datetime.now(KST)) else 1


def _probe_sample_keys(day: date | None) -> int:
    """What changes the key of a day a night stored (#565)."""
    report, code = probe_sample_keys(Settings.from_env(require_kis=False), datetime.now(KST), day=day)
    print(report)
    return code


def _sample_profile(day: date | None, algorithms: list[str], limit: int, top: int) -> int:
    """Where making a recorded day's samples spends its time (#512); each run printed as it ends."""
    settings = Settings.from_env(require_kis=False)
    return sample_profile(
        settings,
        datetime.now(KST),
        lambda line: print(line, flush=True),
        day=day,
        algorithms=algorithms,
        limit=timedelta(minutes=limit),
        top=top,
    )


def _replay(day: date, db: Path | None) -> int:
    try:
        replayed = asyncio.run(replay_day(Settings.from_env(require_kis=False), day, db))
    except (FileNotFoundError, ValueError) as e:
        print(e, file=sys.stderr)
        return 1
    for line in replayed.set_aside:
        print(line, file=sys.stderr)
    summary = replayed.summary
    print(f"replayed {day} into {replayed.db} (counts on the touch label)")
    for s in summary:
        print(
            f"{s.algorithm}: {s.predictions} predictions, {s.skipped} skipped, {s.scored} scored,"
            f" {s.unscorable} unscorable, {s.pending} pending"
        )
    if summary:
        s = summary[0]
        print(f"outcomes: {s.up} up, {s.flat} flat, {s.down} down")
    return 0


def _smt(day: date | None) -> int:
    """A day's SMT events (#520), made again from its recording or backfilled bars, beside the
    engine's live ones: counts and times, no price. 1 if the day has neither."""
    settings = Settings.from_env(require_kis=False)
    day = day or datetime.now(KST).date()
    start = datetime(day.year, day.month, day.day, tzinfo=KST)
    with read_only(settings.state_dir / DB_NAME) as db:
        live = [] if db is None else smt_events(db, start, start + timedelta(days=1))
    made = reconstruct_smt(settings.data_dir, day, calendar_store(settings).calendar)
    for line in smt_lines(day, made, live):
        print(line)
    return 0 if made is not None or live else 1


def _rescore() -> int:
    settings = Settings.from_env(require_kis=False)
    try:
        filled = rescore(settings, datetime.now(KST))
    except EngineRunning as e:
        print(e, file=sys.stderr)
        return 1
    if not filled:
        print("no predicted day to rescore")
    for f in filled:
        if f.failed is not None:
            print(f"{f.day}: not read ({f.failed}); its labels are kept", file=sys.stderr)
            continue
        line = f"{f.day}: {f.labeled} windows from {f.source or 'nowhere'}, {f.written} rows written"
        if f.not_recorded:
            line += f", {f.not_recorded} bar closes not recorded"
        print(line)
    return 1 if any(f.failed for f in filled) else 0


def _fit(name: str, days: int | None) -> int:
    if daily_algorithm(name) is not None:  # a daily kind's (#264): on the KRX days
        try:
            daily = fit_daily(Settings.from_env(require_kis=False), name)
        except (FileNotFoundError, ValueError) as e:
            print(e, file=sys.stderr)
            return 1
        print(f"{daily.model.version}: trained on {daily.samples} labelled days: {daily.model.trained}")
        print(f"saved to {daily.path}; the running engine switches to it within a minute")
        return 0
    try:
        fitted = fit_algorithm(Settings.from_env(require_kis=False), name, days)
    except (FileNotFoundError, ValueError) as e:
        print(e, file=sys.stderr)
        return 1
    print(f"{fitted.model.version}: {dict(fitted.model.params)}")
    recorded = ", ".join(d.isoformat() for d in fitted.recorded) or "none"
    print(f"trained on {fitted.days} days ({fitted.samples} samples): {fitted.model.trained}")
    print(f"recorded days, on mid-points: {recorded}; the others on backfilled closes")
    print(f"saved to {fitted.path}; the running engine switches to it within a minute")
    return 0


def _prevalidate_daily(settings: Settings, name: str) -> int:
    try:
        result = prevalidate_daily(settings, name)
    except (FileNotFoundError, ValueError, PrevalidationRunning) as e:
        print(e, file=sys.stderr)
        return 1
    span = f"{result.days[0]}..{result.days[-1]}" if result.days else "none"
    walked = result.walk_forward or {}
    print(
        f"{name}: {'passed' if result.passed else 'refused'} (walk-forward over the KRX days: "
        f"{len(result.days)} days out of sample, {span}, {walked.get('folds', 0)} folds)"
    )
    for c in result.checks:
        print(f"  {c.name}: {'ok' if c.passed else 'FAILED'} - {c.detail}")
    for symbol, got in walked.get("by_symbol", {}).items():
        accuracy = got["accuracy"]
        print(f"  {symbol}: {got['n']} days, accuracy {'-' if accuracy is None else f'{accuracy:.3f}'}")
    found = daily_algorithm(name)
    overlap = walked.get("overlap")
    if overlap is not None:  # a kind of windows of several sessions (#310)
        print(_overlap_lines(overlap, None if found is None else found[1]))
    if result.earlier_runs:
        print(
            f"run before: {result.earlier_runs}, {result.earlier_on_these_days} of them on any of these days"
            " (a day walked again after a fix is no longer out of sample)"
        )
    print(f"report: {report_path(settings.state_dir, name)}")
    held = None if found is None else found[1].review_hold
    if result.passed and held is not None:
        print(f"the engine does not start {name} yet: {held}")
    elif result.passed:
        print(f"the engine starts {name} on probation at its next 08:50, unless it runs it already")
    else:
        print(f"the engine does not start {name}; fix it and run this again")
    return 0 if result.passed else 1


def _overlap_lines(overlap: dict[str, Any], kind: DailyKind | None = None) -> str:
    """A pre-validation's measure of its windows' overlap (#310, ``daily_training.overlap_report``),
    and the review days fixed for its ``kind``: the rule giving longer ones than those is #310's
    case for fixing them again ("다음 알고리즘")."""

    def number(value: float | None) -> str:
        return "-" if value is None else f"{value:.3f}"

    ratios = ", ".join(f"{k} {number(v)}" for k, v in overlap["variance_ratio"].items())
    between = ", ".join(f"{k} {number(v)}" for k, v in overlap["baselines_variance_ratio"].items())
    lines = [
        f"  overlap: {overlap['sessions']} sessions, Newey-West lag {overlap['lag']},"
        f" {overlap['days']} days ({overlap['independent_days']} independent)",
        f"  variance ratio by baseline: {ratios}; largest {number(overlap['variance_ratio_max'])}",
        f"  variance ratio between baselines (a look): {between or '-'}",
    ]
    review = overlap["review_days"]
    if review is not None:
        lines.append(
            f"  review days by the rule (#310): {review['probation_days']} (recent {review['recent_days']},"
            f" at least {review['min_sample']} scored)"
        )
        if kind is not None:
            fixed = list(kind.review_rules.probation_days)
            longer = review["first"] > fixed[0]
            lines.append(
                f"  review days of {kind.name}: {fixed}"
                + (" - the rule gives longer ones: open an issue to fix them again (#310)" if longer else "")
            )
    for phase in overlap["phases"]:
        lines.append(
            f"  phase {phase['phase']}: {phase['days']} days,"
            f" weakest lift {number(phase['weakest_lift'])} (a look)"
        )
    return "\n".join(lines)


def _train(day: date | None) -> int:
    try:
        settings = Settings.from_env(require_kis=False)
        night = asyncio.run(run_training(settings, day))
    except SettingsError:
        raise  # a configuration error, reported as one (exit 2)
    except (FileNotFoundError, ValueError) as e:
        print(e, file=sys.stderr)
        return 1
    print(night.summary())
    print(f"promoted: {night.promoted or 'nothing'}; the server applies it at 08:00")
    return 0


def _prevalidate(name: str, days: int, revision: bool = False) -> int:
    settings = Settings.from_env(require_kis=False)
    if daily_algorithm(name) is not None:  # a daily kind's: its walk-forward (#264)
        if revision:
            print(f"{name} is a daily kind's: --revision is for the bar-close algorithms", file=sys.stderr)
            return 1
        return _prevalidate_daily(settings, name)
    boundary = since = None
    if revision:  # the revision its code changed to (#454): judged on the days after the change
        with read_only(settings.state_dir / DB_NAME) as db:
            changed = None if db is None else latest_revisions(db).get(name)
        if changed is None or changed.state != REVISION_CHANGED:
            print(f"{name} runs no changed code the engine recorded: no revision to judge", file=sys.stderr)
            return 1
        boundary = changed.boundary
        since = first_whole_day(calendar_store(settings).calendar, boundary)
        print(f"{name}: code {changed.code}, run since {boundary.isoformat()}: recorded days from {since}")
    try:
        result = asyncio.run(prevalidate(settings, name, days=days, revision=boundary, since=since))
    except (
        FileNotFoundError,
        RegistrationError,
        ValueError,
        PrevalidationRunning,
    ) as e:  # RegistrationError: named, though a ValueError
        print(e, file=sys.stderr)
        return 1
    span = f"{result.days[0]}..{result.days[-1]}"
    print(f"{name}: {'passed' if result.passed else 'refused'} ({len(result.days)} recorded days, {span})")
    for c in result.checks:
        print(f"  {c.name}: {'ok' if c.passed else 'FAILED'} - {c.detail}")
    if result.earlier_runs:
        print(
            f"run before: {result.earlier_runs}, {result.earlier_on_these_days} of them on any of these days"
            " (a day replayed again after a fix is no longer out of sample)"
        )
    for line in result.diagnostic_lines:  # what it measured besides the checks (#283), never judged
        print(line)
    print(f"report: {report_path(settings.state_dir, name)}")
    print(f"predictions: {result.db} (not in the official record)")
    if revision:
        print(
            f"the engine keeps {name} on probation; its review makes it active only now it passed"
            if result.passed
            else f"the engine retires {name} at its next bar close, unless this was a look on fewer days"
        )
    elif name in REGISTERED_BEFORE:
        print(f"{name} was registered before pre-validation: the engine runs it either way")
    elif result.passed:
        print(f"the engine starts {name} on probation at its next bar close, unless it runs it already")
    else:
        print(f"the engine does not start {name}; fix it and run this again")
    return 0 if result.passed else 1


def _climate_prior(days: int) -> int:
    """The climate prior experiment for shannon (#429): its lines; exit 1 without a recorded day."""
    settings = Settings.from_env(require_kis=False)
    try:
        result = climate_prior_experiment(settings, days=days)
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        return 1
    print("\n".join(climate_prior_lines(result, settings.state_dir / CLIMATE_PRIOR_REPORT)))
    return 0


def _normal_order(days: int) -> int:
    """The label-order experiment for normal_forecast's algorithms (#529): its lines; exit 1 without a
    recorded day."""
    settings = Settings.from_env(require_kis=False)
    try:
        result = normal_order_experiment(settings, days=days)
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        return 1
    print("\n".join(normal_order_lines(result, settings.state_dir / NORMAL_ORDER_REPORT)))
    return 0


def _neyman_variants(backfill: bool = False) -> int:
    """Neyman's variants experiment (#455): its next step's lines (the discovery, the window's
    progress or its verdict), or with ``backfill`` its verdict on the backfilled days (#532); exit 1
    without a recorded day in the discovery period (without a discovery, for the backfill), with a
    report that cannot be read, or a discovery that cannot set N."""
    settings = Settings.from_env(require_kis=False)
    try:
        run = neyman_backfill(settings) if backfill else neyman_variants(settings)
    except (FileNotFoundError, ValueError) as e:
        print(e, file=sys.stderr)
        return 1
    print("\n".join(run.lines))
    return 0


def _simulate(which: str) -> int:
    """Simulate the probations that wait for it (#298), print what came of them; exit 1 if one
    could not run."""
    settings = Settings.from_env(require_kis=False)
    try:
        run = simulate_daily(settings) if which == DAILY else asyncio.run(simulate_bar_close(settings))
    except (FileNotFoundError, RegistrationError, SimulationRunning) as e:
        print(e, file=sys.stderr)
        return 1
    level = (
        next((k.review_rules.confidence for k in DAILY_KINDS), settings.review_confidence)
        if which == DAILY
        else settings.review_confidence
    )
    print(f"simulated probation (#298), {which}:")
    for line in report(run, level):
        print(line)
    return 0


def _rollback(name: str) -> int:
    settings = Settings.from_env(require_kis=False)
    # An algorithm of a kind besides the direction rolls back on its kind's answers (#179).
    of_kind = next((a for a in KIND_ALGORITHMS if a.name == name), None)
    kind = None if of_kind is None else kind_named(of_kind.kind)
    if name not in {a.name for a in ALGORITHMS} and kind is None:
        print(f"unknown algorithm {name!r}", file=sys.stderr)
        return 1
    target = settings.target_version if kind is None else kind.answer_version
    try:
        model = Shelf(settings.state_dir).rollback(name, datetime.now(KST), target)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    running = model.version if model else "its defaults"
    print(f"{name} now runs {running}; the running engine switches within a minute")
    return 0


def _retire(name: str) -> int:
    """Retire an algorithm, as the operator approves: it is no longer run; its records stay."""
    return _set_status(name, Status.RETIRED, "retired by command", allowed=lambda s: s is not Status.RETIRED)


def _unretire(name: str) -> int:
    """Put a retired algorithm back on probation."""
    return _set_status(name, Status.PROBATION, "unretired by command", allowed=lambda s: s is Status.RETIRED)


def _set_status(name: str, to: Status, reason: str, *, allowed: Callable[[Status], bool]) -> int:
    settings = Settings.from_env(require_kis=False)
    store = PredictionStore(settings.state_dir / DB_NAME)
    try:
        standings = store.statuses()
        if name not in standings:
            print(f"unknown algorithm {name!r}; known: {', '.join(sorted(standings))}", file=sys.stderr)
            return 1
        current = standings[name].status
        if not allowed(current):
            print(f"{name} is {current.value}", file=sys.stderr)
            return 1
        if not store.change_status(name, datetime.now(KST), current, to, reason):
            print(f"{name} changed status meanwhile; try again", file=sys.stderr)
            return 1
    finally:
        store.close()
    print(f"{name}: {current.value} -> {to.value}; the engine follows from its next bar close")
    return 0


async def _notify(text: str) -> int:
    """Send a notice from a script (the server's auto-update); 1 if it did not go."""
    return 0 if await send_notice(Settings.from_env(require_kis=False), text) else 1


async def _test_alert() -> int:
    try:
        sent = await send_test_alert(Settings.from_env())
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 1
    if not sent:
        print("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are not set; alerts only go to the log")
        return 1
    print("test alert sent")
    return 0


async def _fake_kis(port: int, host: str, trades: bool) -> int:
    fake = FakeKis(port=port, host=host)
    await fake.start()
    print(f"fake KIS listening: KIS_REST_URL={fake.rest_url} KIS_WS_URL={fake.ws_url}", flush=True)
    print(f"credentials: KIS_APP_KEY={fake.app_key} KIS_APP_SECRET={fake.app_secret}", flush=True)
    try:
        for tick in itertools.count(1):
            await asyncio.sleep(1)
            if trades:
                await fake.send_demo_trades(datetime.now(KST))
            if tick % 10 == 0:
                await fake.send_pingpong()
    finally:
        await fake.stop()
    return 0  # never reached: it serves until interrupted


def _diagnose(days: int) -> int:
    settings = Settings.from_env(require_kis=False)
    calendar = calendar_store(settings).calendar
    print(
        diagnose(settings.data_dir, settings.state_dir / DB_NAME, calendar, days, fee_rate=settings.fee_rate)
    )
    return 0


async def _doctor(api: str | None) -> int:
    """The summary even when a setting keeps the engine from starting: the error is in it."""
    settings, error = doctor_settings()
    print(await doctor(settings, datetime.now(KST), api, config_error=error), end="")
    return 0


def _signal_review(days: int, since: date | None) -> int:
    settings = Settings.from_env(require_kis=False)
    print(signal_review(settings.state_dir / DB_NAME, settings, datetime.now(KST).date(), days, since))
    return 0


def _error_breakdown(days: int, algorithm: str) -> int:
    settings = Settings.from_env(require_kis=False)
    print(error_breakdown(settings.state_dir / DB_NAME, settings, datetime.now(KST).date(), days, algorithm))
    return 0


def _error_holdout() -> int:
    settings = Settings.from_env(require_kis=False)
    print(error_holdout(settings.state_dir / DB_NAME))
    return 0


def _climate_gate() -> int:
    settings = Settings.from_env(require_kis=False)
    print(climate_gate(settings.state_dir / DB_NAME))
    return 0


def _smt_experiment(holdout: bool) -> int:
    """The SMT experiment (#523): the discovery, or with ``holdout`` the pre-registered verdict."""
    settings = Settings.from_env(require_kis=False)
    calendar = calendar_store(settings).calendar
    print(smt_experiment(settings, calendar, datetime.now(KST), holdout))
    return 0


def _nightly_hit_rates(nights: int) -> int:
    """The hit rates beside the nightly swap over the latest ``nights`` nights (#437); exit 1 without
    the store or a night that carries them."""
    settings = Settings.from_env(require_kis=False)
    store = open_store(settings)
    if store is None:
        print("KRX_OBJECT_STORE is not set: the nights' promotion lists are there", file=sys.stderr)
        return 1
    found = compared_nights(store, nights)
    print("\n".join(hit_rate_lines(found, nights)))
    return 0 if found else 1


def _daily_check(days: int) -> int:
    settings = Settings.from_env(require_kis=False)
    calendar = calendar_store(settings).calendar
    print(
        daily_check(
            settings.data_dir,
            settings.state_dir / DB_NAME,
            calendar,
            settings.symbols,
            datetime.now(KST).date(),
            days,
        )
    )
    return 0


def _structure_check(days: int | None) -> int:
    settings = Settings.from_env(require_kis=False)
    calendar = calendar_store(settings).calendar
    print(
        structure_check(
            settings.data_dir,
            settings.state_dir,
            calendar,
            settings.symbols,
            fee_rate=settings.fee_rate,
            days=days,
        )
    )
    return 0


def _macro() -> int:
    settings = Settings.from_env(require_kis=False)
    macro = asyncio.run(run_macro(settings, lambda: datetime.now(KST)))
    for name, reading in macro.readings.items():
        current, change = reading.current, reading.change_pct
        value = "missing" if current is None else f"{current.value:g} ({current.day})"
        moved = "" if change is None else f" {change:+.2f}% since {macro.previous}"
        print(f"{name:7} {value}{moved}  {reading.indicator.label}")
    for source in macro.skipped:
        print(f"{source.value}: skipped, no API key")
    for source, why in macro.failed.items():
        print(f"{source.value}: failed: {why}", file=sys.stderr)
    assert macro.decision is not None
    print(f"regime {macro.day}: {macro.decision.regime.value} ({'; '.join(macro.decision.reasons)})")
    return 1 if macro.failed else 0


def _seconds_per_minute(text: str) -> float:
    """``--pace``: seconds per minute of the demo morning, a finite number more than 0 (an
    infinite one would leave the demo at 10:00 for good)."""
    seconds = float(text)
    if not (math.isfinite(seconds) and seconds > 0):
        raise argparse.ArgumentTypeError(
            f"seconds per minute of the demo morning: a finite number more than 0, not {text}"
        )
    return seconds


async def _demo_engine(port: int, host: str, db: Path, pace: float | None = None) -> int:
    db.parent.mkdir(parents=True, exist_ok=True)
    try:
        app = await demo_app(db, pace)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        await web.TCPSite(runner, host, port).start()
    except OSError as e:  # the port is taken, or the address is not this machine's
        await runner.cleanup()
        print(f"demo Engine API not started on {host}:{port}: {e}", file=sys.stderr)
        return 1
    print(f"demo Engine API: http://{host}:{port}/v1/status ({demo_morning(pace)}, made-up data)", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
    return 0


def _positive(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {value}")
    return value


def _parser() -> argparse.ArgumentParser:
    """``krx-quant``'s parser. Each command's parser carries the handler that runs it, as its ``run``
    default (#499): it takes the parsed arguments and returns the exit code. A new command adds its
    parser here with ``set_defaults(run=...)`` and a row in README's command table."""
    parser = argparse.ArgumentParser(prog="krx-quant")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("collect", help="record KIS real-time feeds until interrupted").set_defaults(
        run=lambda args: asyncio.run(_collect())
    )
    trade_cmd = sub.add_parser(
        "trade",
        help="trade 005930 on the cells the cost map passes, in paper (#394, #395), on KIS's mock server"
        " (KRX_TRADER_MODE=mock, #405) or on the live account once the live gate and the execution"
        " checks pass (KRX_TRADER_MODE=live, #408), until interrupted;"
        " resume: lift the screen's pause; rearm: re-arm the cumulative loss limit (#396)",
    )
    trade_cmd.add_argument(
        "control",
        nargs="?",
        choices=("resume", "rearm"),
        help="instead of trading, ask the running trader to resume after a pause, or to re-arm its"
        " cumulative loss limit (counted again from then)",
    )
    trade_cmd.set_defaults(
        run=lambda args: asyncio.run(_trade()) if args.control is None else _trade_control(args.control)
    )
    refit_cmd = sub.add_parser(
        "refit-policy",
        help="fit the trader's policy again (#399): tonight's cost map from the Engine API, each passing"
        " cell's threshold and Kelly fraction on a small grid, from the stored predictions and answers of"
        " its 20 days (read-only); keeps it in state/trader.sqlite3, the first as the champion",
    )
    refit_cmd.add_argument(
        "--engine-url",
        help="the Engine API the cost map is read from (default KRX_TRADER_ENGINE_URL, which compose sets"
        " to the engine service's, http://engine:8080; else http://127.0.0.1:8080)",
    )
    refit_cmd.add_argument(
        "--report",
        action="store_true",
        help="then print the day's trader report (#402) above the re-fit's output: the scheduled job"
        " trader-report's",
    )
    refit_cmd.set_defaults(run=lambda args: asyncio.run(_refit_policy(args.engine_url, args.report)))
    report_cmd = sub.add_parser(
        "trader-report",
        help="print the trader's report of a trading day (#402), read-only from state/trader.sqlite3: its"
        " trades, why no cell traded, each policy's paper record, what its night changed, the live gate,"
        " limits and pauses; no price",
    )
    report_cmd.add_argument(
        "--day",
        type=date.fromisoformat,
        help="the trading day, YYYY-MM-DD (default: the latest at or before tonight's evening)",
    )
    report_cmd.set_defaults(run=lambda args: _trader_report(args.day))
    mock_check_cmd = sub.add_parser(
        "check-mock-trades",
        help="compare a mock trading day's orders in the trader's ledger with KIS's order inquiry (#405),"
        " with KRX_TRADER_KIS_* on the mock server: sides, quantities, fills and fill prices in ticks;"
        " no order number, account or price",
    )
    mock_check_cmd.add_argument(
        "--day",
        type=date.fromisoformat,
        help="the trading day, YYYY-MM-DD (default: the latest at or before tonight's evening)",
    )
    mock_check_cmd.set_defaults(run=lambda args: asyncio.run(_check_mock_trades(args.day)))
    sub.add_parser(
        "fill-notice-layout",
        help="measure the mock fill notices the trader recorded (state/fill_notices.sqlite3, #484) column by"
        " column against the day's orders in its ledger and on KIS's order inquiry (KRX_TRADER_KIS_*,"
        " read-only): each column's shape, lengths, values' count and matches; no value, order number,"
        " account or price",
    ).set_defaults(run=lambda args: asyncio.run(_fill_notice_layout()))
    sub.add_parser(
        "measure-subscriptions",
        help="count how many real-time registrations KIS allows per session (stop the collector first)",
    ).set_defaults(run=lambda args: asyncio.run(_measure()))
    sub.add_parser(
        "check-fallbacks",
        help="ask each feed's REST fallback once and print what it would record (safe beside the collector)",
    ).set_defaults(run=lambda args: asyncio.run(_check_fallbacks()))
    sub.add_parser(
        "probe-supply",
        help="ask where KIS gives the daily supply indicators (#226), read-only; prints no market value",
    ).set_defaults(run=lambda args: asyncio.run(_probe_supply()))
    sub.add_parser(
        "probe-nav",
        help="ask where KIS gives the single-stock leveraged and inverse ETFs' net assets (#305),"
        " read-only; prints no market value",
    ).set_defaults(run=lambda args: asyncio.run(_probe_nav()))
    sub.add_parser(
        "probe-krx-etf",
        help="ask what the KRX Open API's ETF daily trading service gives for the single-stock ETFs"
        " (#305), against KIS, read-only; prints no market value",
    ).set_defaults(run=lambda args: asyncio.run(_probe_krx_etf()))
    night_cmd = sub.add_parser(
        "probe-night",
        help="summarize a recorded KOSPI200 night session (#264), read-only; prints no market value",
    )
    night_cmd.add_argument(
        "--day",
        type=date.fromisoformat,
        help="the evening whose night to read, YYYY-MM-DD (default: the latest one subscribed for)",
    )
    night_cmd.set_defaults(run=lambda args: asyncio.run(_probe_night(args.day)))
    sub.add_parser(
        "probe-gaps",
        help="summarize every recorded day's gaps and the closing auction window's frames, trades and"
        " bars (#344), read-only; prints no market value",
    ).set_defaults(run=lambda args: _probe_gaps())
    sub.add_parser(
        "probe-live-account",
        help="read the trader's live account with KRX_TRADER_LIVE_KIS_* by inquiry calls alone (#408): the"
        " price, the balance, the orderable cash and the day's order inquiry, each field the trader's broker"
        " reads checked against how it reads it; places no order; keeps the result for the live mode;"
        " prints codes and field names, no key, account, order number or value",
    ).set_defaults(run=lambda args: asyncio.run(_probe_live_account()))
    sub.add_parser(
        "probe-kis-orders",
        help="place the trader's probe orders on KIS's mock-trading server with KRX_TRADER_KIS_* (#403):"
        " a 1-share marketable buy and sell of 005930, a far limit buy and its cancel, three orders KIS"
        " should refuse; refuses any server but the mock one and runs only in the KRX session; prints"
        " the answers' codes and field shapes, no key, account, order number or market value",
    ).set_defaults(run=lambda args: asyncio.run(_probe_kis_orders()))
    sub.add_parser(
        "check-closing-flow",
        help="summarize what the closing flow kept (#305): the net assets by day and when they came,"
        " the 15:20 rows, the days, and the integrated and KRX one-minute bars' closing auction values"
        " against the recorded prints (asks KIS for the KRX bars); writes nothing, no market value",
    ).set_defaults(run=lambda args: asyncio.run(_check_closing_flow()))
    sub.add_parser(
        "check-krx-net-assets",
        help="summarize the ETF net assets kept from the KRX Open API (#305): the days, when they came,"
        " which day's listed units the net asset total matches, KRX against KIS; reads the state file"
        " only, prints no value",
    ).set_defaults(run=lambda args: _check_krx_net_assets())
    sub.add_parser(
        "backfill",
        help="fill missing one-minute bars for the past year and exit (stop the collector first)",
    ).set_defaults(run=lambda args: asyncio.run(_backfill()))
    archive_cmd = sub.add_parser(
        "archive",
        help="merge recorded part files and upload every day to KRX_OBJECT_STORE (safe to rerun)",
    )
    archive_cmd.add_argument(
        "--recheck", action="store_true", help="compare every file with the store and upload what it lacks"
    )
    archive_cmd.set_defaults(run=lambda args: asyncio.run(_archive(args.recheck)))
    replay_cmd = sub.add_parser(
        "replay", help="replay a recorded day through the engine into its own database (no KIS needed)"
    )
    replay_cmd.add_argument("day", type=date.fromisoformat, help="the recorded day, YYYY-MM-DD")
    replay_cmd.add_argument(
        "--db",
        type=Path,
        help="database to write, replaced if it exists (default: state/replay-<day>.sqlite3)",
    )
    replay_cmd.set_defaults(run=lambda args: _replay(args.day, args.db))
    smt_cmd = sub.add_parser(
        "smt",
        help="count a day's SMT divergences (#520), made again from its recording or backfilled bars,"
        " beside the engine's live ones; reads only, no price",
    )
    smt_cmd.add_argument("--day", type=date.fromisoformat, help="the day, YYYY-MM-DD (default: today)")
    smt_cmd.set_defaults(run=lambda args: _smt(args.day))
    sample_profile_cmd = sub.add_parser(
        "sample-profile",
        help="make one recorded day's training samples from scratch, as a night does, and time each stage"
        " and the top functions (#512); reads only, into a temporary directory, no price",
    )
    sample_profile_cmd.add_argument(
        "--day", type=date.fromisoformat, help="the recorded day, YYYY-MM-DD (default: the latest finished)"
    )
    sample_profile_cmd.add_argument(
        "--algorithms",
        nargs="+",
        default=list(PROFILED),
        help=f"the algorithms also profiled alone, with their own features (default: {' '.join(PROFILED)})",
    )
    sample_profile_cmd.add_argument(
        "--limit",
        type=_positive,
        default=round(SAMPLE_PROFILE_LIMIT.total_seconds() / 60),
        help="minutes for every run together; a run still going stops there, the rest do not start"
        f" (default {round(SAMPLE_PROFILE_LIMIT.total_seconds() / 60)})",
    )
    sample_profile_cmd.add_argument(
        "--top",
        type=_positive,
        default=SAMPLE_PROFILE_TOP,
        help=f"functions listed by own time (default {SAMPLE_PROFILE_TOP})",
    )
    sample_profile_cmd.set_defaults(
        run=lambda args: _sample_profile(args.day, args.algorithms, args.limit, args.top)
    )
    sample_keys_cmd = sub.add_parser(
        "probe-sample-keys",
        help="what changed the key of the days the sample store holds since the night stored them (#565):"
        " their input files in the Object Store, the training cache and the data directory, the latest day's"
        " parts not merged yet, the keys changed since the last run; writes only its state file, no price",
    )
    sample_keys_cmd.add_argument(
        "--day",
        type=date.fromisoformat,
        help="the latest day looked at, YYYY-MM-DD (default: the store's latest)",
    )
    sample_keys_cmd.set_defaults(run=lambda args: _probe_sample_keys(args.day))
    sub.add_parser(
        "backup-state",
        help="back the server's state up to KRX_OBJECT_STORE now, as the collector does each night (#576)",
    ).set_defaults(run=lambda args: _backup_state())
    restore_cmd = sub.add_parser(
        "restore-state",
        help="put the latest backup of the server's state back into the state folder, once it checks out"
        " (#576; stop the engine and the trader first)",
    )
    restore_cmd.add_argument(
        "--from", dest="source", type=Path, help="a backup's state.tar.gz instead of the store's latest"
    )
    restore_cmd.add_argument(
        "--replace",
        action="store_true",
        help="move what is in the state folder aside (state/before-restore-<time>/) instead of refusing",
    )
    restore_cmd.add_argument(
        "--if-backed-up",
        action="store_true",
        help="without a backup in the store (or a store), restore nothing and exit 0 (a new server's setup)",
    )
    restore_cmd.set_defaults(run=lambda args: _restore_state(args.source, args.replace, args.if_backed_up))
    sub.add_parser(
        "restore-drill",
        help="check the latest backup of the server's state in a scratch folder (#576, read-only)",
    ).set_defaults(run=lambda args: _restore_drill())
    sub.add_parser(
        "rescore",
        help="label every recorded day's bar closes again with the touch target (stop the engine first)",
    ).set_defaults(run=lambda args: _rescore())
    fit_cmd = sub.add_parser(
        "fit", help="train an algorithm on the latest recorded or backfilled days and save its model"
    )
    fit_cmd.add_argument("algorithm", help="e.g. newton")
    fit_cmd.add_argument(
        "--days", type=_positive, help="trading days to train on (default: the algorithm's own)"
    )
    fit_cmd.set_defaults(run=lambda args: _fit(args.algorithm, args.days))
    prevalidate_cmd = sub.add_parser(
        "prevalidate",
        help="replay a new algorithm on the latest recorded days; the engine starts it only if it passes",
    )
    prevalidate_cmd.add_argument("algorithm", help="e.g. kalman")
    prevalidate_cmd.add_argument(
        "--days",
        type=_positive,
        default=PREVALIDATION_DAYS,
        help=f"recorded trading days to replay (default: {PREVALIDATION_DAYS})",
    )
    prevalidate_cmd.add_argument(
        "--revision",
        action="store_true",
        help="judge the running algorithm's changed code on the recorded days after the change (#454)",
    )
    prevalidate_cmd.set_defaults(run=lambda args: _prevalidate(args.algorithm, args.days, args.revision))
    climate_prior_cmd = sub.add_parser(
        "climate-prior",
        help="compare the climate baseline, shannon and shannon with each slot's class frequencies on"
        " the pre-validation's folds (#429; reads the training samples, replays nothing)",
    )
    climate_prior_cmd.add_argument(
        "--days",
        type=_positive,
        default=PREVALIDATION_DAYS,
        help=f"recorded trading days to predict (default: {PREVALIDATION_DAYS})",
    )
    climate_prior_cmd.set_defaults(run=lambda args: _climate_prior(args.days))
    normal_order_cmd = sub.add_parser(
        "normal-order",
        help="compare newton, gauss, granger, markov and kalman with normal_forecast's targets taken as"
        " independent and in the touch label's order on the pre-validation's folds (#529; reads the"
        " training samples, replays nothing)",
    )
    normal_order_cmd.add_argument(
        "--days",
        type=_positive,
        default=PREVALIDATION_DAYS,
        help=f"recorded trading days to predict (default: {PREVALIDATION_DAYS})",
    )
    normal_order_cmd.set_defaults(run=lambda args: _normal_order(args.days))
    neyman_variants_cmd = sub.add_parser(
        "neyman-variants",
        help="neyman's variants (#455): the discovery that fixes the window's N, then the verdict on the"
        " window after 2026-11-02 once it is full (reads the training samples, replays nothing)",
    )
    neyman_variants_cmd.add_argument(
        "--backfill",
        action="store_true",
        help="judge the three comparisons as registered on the backfilled days before the discovery's"
        " training instead, once (#532)",
    )
    neyman_variants_cmd.set_defaults(run=lambda args: _neyman_variants(args.backfill))
    train_cmd = sub.add_parser(
        "train", help="train every algorithm on KRX_OBJECT_STORE's latest days and store the promotion list"
    )
    train_cmd.add_argument(
        "--day", type=date.fromisoformat, help="the night to train, YYYY-MM-DD (default: latest)"
    )
    train_cmd.set_defaults(run=lambda args: _train(args.day))
    simulate_cmd = sub.add_parser(
        "simulate",
        help="replay the latest trading days as the probation of each algorithm that waits for it (#298),"
        " and judge it at once (safe beside the engine)",
    )
    simulate_cmd.add_argument(
        "which",
        choices=[DAILY, BAR_CLOSE_COMMAND],
        help="daily: the daily kinds' algorithms; bar-close: the direction's and the other kinds'"
        " (the latest recorded days, 10 to 20)",
    )
    simulate_cmd.set_defaults(run=lambda args: _simulate(args.which))
    rollback_cmd = sub.add_parser(
        "rollback",
        help="put an algorithm back one step, on the model it ran before (only that one is kept, #361)",
    )
    rollback_cmd.add_argument("algorithm")
    rollback_cmd.set_defaults(run=lambda args: _rollback(args.algorithm))
    retire_cmd = sub.add_parser("retire", help="retire an algorithm: it is no longer run; its records stay")
    retire_cmd.add_argument("algorithm")
    retire_cmd.set_defaults(run=lambda args: _retire(args.algorithm))
    unretire_cmd = sub.add_parser("unretire", help="put a retired algorithm back on probation")
    unretire_cmd.add_argument("algorithm")
    unretire_cmd.set_defaults(run=lambda args: _unretire(args.algorithm))
    sub.add_parser(
        "test-alert", help="send a test message to the Telegram chat that receives alerts"
    ).set_defaults(run=lambda args: asyncio.run(_test_alert()))
    sub.add_parser("macro", help="read today's macro indicators and decide the regime, once").set_defaults(
        run=lambda args: _macro()
    )
    diagnose_cmd = sub.add_parser(
        "diagnose", help="print the measurements the open decisions need; reads only, writes nothing"
    )
    diagnose_cmd.add_argument(
        "--days",
        type=_positive,
        default=DEFAULT_DAYS,
        help=f"the latest N trading days with data (default {DEFAULT_DAYS})",
    )
    diagnose_cmd.set_defaults(run=lambda args: _diagnose(args.days))
    doctor_cmd = sub.add_parser(
        "doctor",
        help="print the server's state, masked (engine, KIS, PINGPONG, scheduled jobs, disk, memory, #322);"
        " reads only, prints no market value",
    )
    doctor_cmd.add_argument(
        "--api",
        help="the Engine API to ask, e.g. http://127.0.0.1:8080 (default: this container's, then compose's"
        " engine service)",
    )
    doctor_cmd.set_defaults(run=lambda args: asyncio.run(_doctor(args.api)))
    signal_review_cmd = sub.add_parser(
        "signal-review",
        help="print what choosing the Telegram signal settings needs (#206); reads only, writes nothing",
    )
    signal_review_cmd.add_argument(
        "--days",
        type=_positive,
        default=SIGNAL_REVIEW_DAYS,
        help=f"the latest N days with predictions (default {SIGNAL_REVIEW_DAYS}, the leaderboard's 20d)",
    )
    signal_review_cmd.add_argument(
        "--from",
        dest="since",
        type=date.fromisoformat,
        help="instead, the first N days with predictions on or after this day, YYYY-MM-DD: a fixed window,"
        " the same days however late it runs (#206)",
    )
    signal_review_cmd.set_defaults(run=lambda args: _signal_review(args.days, args.since))
    error_cmd = sub.add_parser(
        "error-breakdown",
        help="print where an algorithm errs (#431): confusion matrices and lift over the climate baseline"
        " by symbol, 30-minute slot, spread and volatility; exploratory, reads only, writes nothing",
    )
    error_cmd.add_argument(
        "--days",
        type=_positive,
        default=ERROR_DAYS,
        help=f"the latest N days with predictions (default {ERROR_DAYS}, the leaderboard's 20d)",
    )
    error_cmd.add_argument(
        "--algorithm", default=ERROR_ALGORITHM, help=f"the algorithm (default {ERROR_ALGORITHM})"
    )
    error_cmd.set_defaults(run=lambda args: _error_breakdown(args.days, args.algorithm))
    sub.add_parser(
        "error-holdout",
        help="print turing's four pre-registered hypotheses of #431's report judged on the fixed holdout"
        " (#447): the first 10 trading days after 2026-10-30; reads only, writes nothing, no price",
    ).set_defaults(run=lambda args: _error_holdout())
    sub.add_parser(
        "climate-gate",
        help="print the gate that takes baseline-climate's prediction when quiet or at lunch and turing's"
        " elsewhere, against both, on #447's holdout (#449); reads only, writes nothing, no price",
    ).set_defaults(run=lambda args: _climate_gate())
    smt_experiment_cmd = sub.add_parser(
        "smt-experiment",
        help="print whether the SMT (#520) raises the four-minute prediction's hit rate (#523): the frozen"
        " rule against the engine's final consensus on the same bar closes, on the recorded days up to"
        " 2026-11-04 (the discovery), or with --holdout the four pre-registered claims judged on the first"
        " 10 eligible days after it; reads only, writes nothing, no price",
    )
    smt_experiment_cmd.add_argument(
        "--holdout", action="store_true", help="judge the pre-registered claims on the holdout window"
    )
    smt_experiment_cmd.set_defaults(run=lambda args: _smt_experiment(args.holdout))
    hit_rates_cmd = sub.add_parser(
        "nightly-hit-rates",
        help="print the champion's and the challenger's hit rates the nightly reports carry beside the"
        " log loss's swap (#437), by algorithm over the latest nights: mismatches, what a hit-rate-first"
        " rule would have chosen; reads the store only, no price",
    )
    hit_rates_cmd.add_argument(
        "--nights",
        type=_positive,
        default=HIT_RATE_NIGHTS,
        help=f"the latest N nights whose promotion lists carry the hit rates (default {HIT_RATE_NIGHTS})",
    )
    hit_rates_cmd.set_defaults(run=lambda args: _nightly_hit_rates(args.nights))
    daily_cmd = sub.add_parser(
        "daily-check",
        help="print what the daily kinds did on the latest trading days (#264); reads only, writes nothing",
    )
    daily_cmd.add_argument(
        "--days",
        type=_positive,
        default=DAILY_CHECK_DAYS,
        help=f"the latest N trading days (default {DAILY_CHECK_DAYS})",
    )
    daily_cmd.set_defaults(run=lambda args: _daily_check(args.days))
    structure_cmd = sub.add_parser(
        "structure-check",
        help="measure the research's market-structure claims on the stored bars and candles; reads only",
    )
    structure_cmd.add_argument(
        "--days",
        type=_positive,
        default=None,
        help="the latest N trading days with one-minute bars (default: all)",
    )
    structure_cmd.set_defaults(run=lambda args: _structure_check(args.days))
    notify_cmd = sub.add_parser("notify", help="send a notice to the Telegram chat that receives alerts")
    notify_cmd.add_argument("text")
    notify_cmd.set_defaults(run=lambda args: asyncio.run(_notify(args.text)))
    fake = sub.add_parser("fake-kis", help="run a local fake KIS server for offline testing")
    fake.add_argument("--port", type=int, default=18080)
    fake.add_argument("--host", default="127.0.0.1", help="address to listen on (0.0.0.0 inside Docker)")
    fake.add_argument("--trades", action="store_true", help="push a synthetic trade per stock every second")
    fake.set_defaults(run=lambda args: asyncio.run(_fake_kis(args.port, args.host, args.trades)))
    demo = sub.add_parser(
        "demo-engine", help="serve the Engine API on a made-up morning, to build screens without the server"
    )
    demo.add_argument("--port", type=int, default=8090)
    demo.add_argument("--host", default="127.0.0.1", help="address to listen on (0.0.0.0: all)")
    demo.add_argument("--db", type=Path, default=Path("state/demo.sqlite3"), help="demo database (replaced)")
    demo.add_argument(
        "--pace",
        type=_seconds_per_minute,
        help="open at 10:00 and go on to 10:30 a minute every PACE seconds, streaming as it goes",
    )
    demo.set_defaults(run=lambda args: asyncio.run(_demo_engine(args.port, args.host, args.db, args.pace)))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    # This process's lines also go to Loki when it is set (#358), masked, between a start line and
    # one with its exit code.
    values = read_values(None, Path(".env"))
    files = open_process_log(
        args.command, sys.argv[1:] if argv is None else argv, values, _log_secrets(values)
    )
    code, why = 1, None
    try:
        code, why = _command(args), None
    except SettingsError as e:
        print(f"configuration error: {e}", file=sys.stderr)
        code, why = 2, f"configuration error: {e}"
    except KisError as e:
        # Non-zero so a supervisor (Docker restart policy) restarts the collector.
        print(f"KIS error: {e}", file=sys.stderr)
        code, why = 1, f"KIS error: {e}"
    except KeyboardInterrupt:
        code, why = 0, "interrupted"
    except BaseException as e:  # its traceback goes to the file too, then on to stderr
        if files is not None:
            files.close(None, e)
            files = None
        raise
    finally:
        if files is not None:
            files.close(code, why)
    return code


def _log_secrets(values: dict[str, str]) -> tuple[str, ...]:
    """What the lines sent to Loki mask (#358): the settings' secrets, read leniently as
    ``doctor`` reads them (a malformed setting is the command's to report). None to mask
    unless Loki is set."""
    if not loki.configured(values):
        return ()
    settings, _ = doctor_settings(values, None)
    return secrets_of(settings)


def _command(args: argparse.Namespace) -> int:
    """Run the command ``args`` name with the handler its parser carries (``_parser``, #499); its
    exit code. A command whose parser carries none fails rather than doing nothing and exiting 0,
    which a scheduled job calling it would report as done."""
    run: Run | None = getattr(args, "run", None)
    if run is None:
        print(f"krx-quant: command {args.command} has no handler (set_defaults(run=...))", file=sys.stderr)
        return 1
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
