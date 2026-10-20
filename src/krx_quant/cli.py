"""Command-line entry point: ``krx-quant <command>``."""

from __future__ import annotations

import argparse
import asyncio
import base64
import itertools
import json
import logging
import math
import signal
import sys
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path

import boto3
from aiohttp import web
from botocore.exceptions import BotoCoreError, ClientError

from krx_quant.algorithms import ALGORITHMS, KIND_ALGORITHMS, RegistrationError
from krx_quant.archive import run_archive
from krx_quant.backfill import run_backfill
from krx_quant.candle_probe import probe_candles
from krx_quant.close_probe import probe_close
from krx_quant.collector import check_fallbacks, measure_subscription_limit, run_collector
from krx_quant.config import Settings, SettingsError
from krx_quant.daily_check import DAYS as DAILY_CHECK_DAYS
from krx_quant.daily_check import daily_check
from krx_quant.daily_training import daily_algorithm, fit_daily, prevalidate_daily
from krx_quant.demo import demo_app, demo_morning
from krx_quant.diagnose import DEFAULT_DAYS, diagnose
from krx_quant.dividend_probe import probe_dividends
from krx_quant.doctor import doctor, doctor_settings
from krx_quant.expected_open_probe import DAYS as EXPECTED_OPEN_DAYS
from krx_quant.expected_open_probe import probe_expected_open
from krx_quant.fake_kis import FakeKis
from krx_quant.feeds.base import KST
from krx_quant.kind_registry import DAILY_KINDS, kind_named
from krx_quant.kis import KisError
from krx_quant.lifecycle import Status
from krx_quant.macro import run_macro
from krx_quant.market_calendar import calendar_store
from krx_quant.model_shelf import Shelf
from krx_quant.nav_probe import probe_nav
from krx_quant.net_buys_probe import probe_net_buys
from krx_quant.night_probe import probe_night
from krx_quant.nightly import from_the_server, run_training
from krx_quant.notify import send_notice, send_test_alert
from krx_quant.predictions import DB_NAME, PredictionStore
from krx_quant.prevalidation import (
    PREVALIDATION_DAYS,
    REGISTERED_BEFORE,
    PrevalidationRunning,
    prevalidate,
    report_path,
)
from krx_quant.replay import replay_day
from krx_quant.rescore import EngineRunning, rescore
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
from krx_quant.structure_check import structure_check
from krx_quant.supply_probe import probe_supply
from krx_quant.training import fit_algorithm


async def _collect() -> None:
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


async def _measure() -> None:
    limit = await measure_subscription_limit(Settings.from_env())
    print(f"KIS accepted {limit} real-time registrations in one session.")
    print(f"Set KIS_WS_MAX_SUBSCRIPTIONS={limit} in .env to stay within it.")


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


def _registry_password() -> int:
    """The password of this AWS account's ECR registry, for ``docker login --username AWS
    --password-stdin`` (deploy/push-image.sh), from the server's own keys."""
    region = Settings.from_env(require_kis=False).aws_region
    try:
        token = boto3.client("ecr", region_name=region).get_authorization_token()
    except (BotoCoreError, ClientError) as e:
        print(f"AWS error: {e}", file=sys.stderr)
        return 1
    _, password = base64.b64decode(token["authorizationData"][0]["authorizationToken"]).decode().split(":", 1)
    print(password)
    return 0


async def _probe_candles() -> int:
    await probe_candles(Settings.from_env(), datetime.now(KST).date())
    return 0


async def _probe_night(day: date | None) -> int:
    print(await probe_night(Settings.from_env(require_kis=False), datetime.now(KST), day))
    return 0


def _probe_expected_open(days: int) -> int:
    print(probe_expected_open(Settings.from_env(require_kis=False), datetime.now(KST), days))
    return 0


async def _probe_supply() -> int:
    calls, failed = await probe_supply(Settings.from_env(), datetime.now(KST).date())
    return 1 if failed == calls else 0  # nothing answered


async def _probe_net_buys() -> int:
    calls, failed = await probe_net_buys(Settings.from_env(), datetime.now(KST).date())
    return 1 if failed == calls else 0  # nothing answered


async def _probe_close() -> int:
    calls, failed = await probe_close(Settings.from_env(), datetime.now(KST).date())
    return 1 if failed == calls else 0  # nothing answered


async def _probe_dividends() -> int:
    calls, failed = await probe_dividends(Settings.from_env(), datetime.now(KST).date())
    return 1 if failed == calls else 0  # nothing answered


async def _probe_nav() -> int:
    calls, failed = await probe_nav(Settings.from_env(), datetime.now(KST))
    return 1 if failed == calls else 0  # nothing answered


async def _backfill() -> None:
    filled = await run_backfill(Settings.from_env())
    print(f"filled one-minute bars for {filled} series-days")


async def _archive(recheck: bool) -> int:
    report = await run_archive(Settings.from_env(), recheck=recheck)
    print(f"archived: {report.uploaded} uploaded, {report.unchanged} unchanged, {len(report.failed)} failed")
    for partition in report.failed:
        print(f"failed: {partition}", file=sys.stderr)
    return 1 if report.failed else 0


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
    if result.earlier_runs:
        print(
            f"run before: {result.earlier_runs}, {result.earlier_on_these_days} of them on any of these days"
            " (a day walked again after a fix is no longer out of sample)"
        )
    print(f"report: {report_path(settings.state_dir, name)}")
    if result.passed:
        print(f"the engine starts {name} on probation at its next 08:50, unless it runs it already")
    else:
        print(f"the engine does not start {name}; fix it and run this again")
    return 0 if result.passed else 1


def _train(day: date | None, from_store: bool) -> int:
    try:
        settings = Settings.from_env(require_kis=False)
        if from_store:
            settings = from_the_server(settings)
        night = asyncio.run(run_training(settings, day))
    except SettingsError:
        raise  # a configuration error, reported as one (exit 2)
    except (FileNotFoundError, ValueError) as e:
        print(e, file=sys.stderr)
        return 1
    print(night.summary())
    print(f"promoted: {night.promoted or 'nothing'}; the server applies it at 08:00")
    return 0


def _prevalidate(name: str, days: int) -> int:
    settings = Settings.from_env(require_kis=False)
    if daily_algorithm(name) is not None:  # a daily kind's: its walk-forward (#264)
        return _prevalidate_daily(settings, name)
    try:
        result = asyncio.run(prevalidate(settings, name, days=days))
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
    print(f"report: {report_path(settings.state_dir, name)}")
    print(f"predictions: {result.db} (not in the official record)")
    if name in REGISTERED_BEFORE:
        print(f"{name} was registered before pre-validation: the engine runs it either way")
    elif result.passed:
        print(f"the engine starts {name} on probation at its next bar close, unless it runs it already")
    else:
        print(f"the engine does not start {name}; fix it and run this again")
    return 0 if result.passed else 1


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


async def _fake_kis(port: int, host: str, trades: bool) -> None:
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


def _signal_review(days: int) -> int:
    settings = Settings.from_env(require_kis=False)
    print(signal_review(settings.state_dir / DB_NAME, settings, datetime.now(KST).date(), days))
    return 0


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="krx-quant")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("collect", help="record KIS real-time feeds until interrupted")
    sub.add_parser(
        "measure-subscriptions",
        help="count how many real-time registrations KIS allows per session (stop the collector first)",
    )
    sub.add_parser(
        "check-fallbacks",
        help="ask each feed's REST fallback once and print what it would record (safe beside the collector)",
    )
    sub.add_parser(
        "probe-candles",
        help="measure what KIS's day/week/month/year chart query gives, read-only (safe beside collect)",
    )
    sub.add_parser(
        "probe-supply",
        help="ask where KIS gives the daily supply indicators (#226), read-only; prints no market value",
    )
    sub.add_parser(
        "probe-net-buys",
        help="compare KIS's KRX (J) and integrated (UN) investor net buys by era and count the stored"
        " ones (#264), read-only; prints no market value",
    )
    sub.add_parser(
        "probe-close",
        help="compare the KRX day candle's close with KRX's one-minute bars and the recorded closing"
        " auction (#264), read-only; prints no market value",
    )
    sub.add_parser(
        "probe-dividends",
        help="compare KIS's adjusted and as-traded KRX day candles and count their ex-date flags (#310),"
        " read-only; prints no market value",
    )
    sub.add_parser(
        "probe-nav",
        help="ask where KIS gives the single-stock leveraged and inverse ETFs' net assets (#305),"
        " read-only; prints no market value",
    )
    night_cmd = sub.add_parser(
        "probe-night",
        help="summarize a recorded KOSPI200 night session (#264), read-only; prints no market value",
    )
    night_cmd.add_argument(
        "--day",
        type=date.fromisoformat,
        help="the evening whose night to read, YYYY-MM-DD (default: the latest one subscribed for)",
    )
    expected_cmd = sub.add_parser(
        "probe-expected-open",
        help="count what the recorded books carried of the auctions' expected fill (#304), read-only;"
        " prints no market value",
    )
    expected_cmd.add_argument(
        "--days",
        type=_positive,
        default=EXPECTED_OPEN_DAYS,
        help=f"the latest recorded trading days to read (default {EXPECTED_OPEN_DAYS})",
    )
    sub.add_parser(
        "registry-password",
        help="print the ECR registry's docker login password (deploy/push-image.sh, EC2 Spot training)",
    )
    sub.add_parser(
        "backfill",
        help="fill missing one-minute bars for the past year and exit (stop the collector first)",
    )
    archive_cmd = sub.add_parser(
        "archive",
        help="merge recorded part files and upload every day to KRX_OBJECT_STORE (safe to rerun)",
    )
    archive_cmd.add_argument(
        "--recheck", action="store_true", help="compare every file with the store and upload what it lacks"
    )
    replay_cmd = sub.add_parser(
        "replay", help="replay a recorded day through the engine into its own database (no KIS needed)"
    )
    replay_cmd.add_argument("day", type=date.fromisoformat, help="the recorded day, YYYY-MM-DD")
    replay_cmd.add_argument(
        "--db",
        type=Path,
        help="database to write, replaced if it exists (default: state/replay-<day>.sqlite3)",
    )
    sub.add_parser(
        "rescore",
        help="label every recorded day's bar closes again with the touch target (stop the engine first)",
    )
    fit_cmd = sub.add_parser(
        "fit", help="train an algorithm on the latest recorded or backfilled days and save its model"
    )
    fit_cmd.add_argument("algorithm", help="e.g. newton")
    fit_cmd.add_argument(
        "--days", type=_positive, help="trading days to train on (default: the algorithm's own)"
    )
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
    train_cmd = sub.add_parser(
        "train", help="train every algorithm on KRX_OBJECT_STORE's latest days and store the promotion list"
    )
    train_cmd.add_argument(
        "--day", type=date.fromisoformat, help="the night to train, YYYY-MM-DD (default: latest)"
    )
    train_cmd.add_argument(
        "--from-store",
        action="store_true",
        help="train with the settings and market calendar the server published (EC2 Spot)",
    )
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
    rollback_cmd = sub.add_parser("rollback", help="put an algorithm back on the model it ran before")
    rollback_cmd.add_argument("algorithm")
    retire_cmd = sub.add_parser("retire", help="retire an algorithm: it is no longer run; its records stay")
    retire_cmd.add_argument("algorithm")
    unretire_cmd = sub.add_parser("unretire", help="put a retired algorithm back on probation")
    unretire_cmd.add_argument("algorithm")
    sub.add_parser("test-alert", help="send a test message to the Telegram chat that receives alerts")
    sub.add_parser("macro", help="read today's macro indicators and decide the regime, once")
    diagnose_cmd = sub.add_parser(
        "diagnose", help="print the measurements the open decisions need; reads only, writes nothing"
    )
    diagnose_cmd.add_argument(
        "--days",
        type=_positive,
        default=DEFAULT_DAYS,
        help=f"the latest N trading days with data (default {DEFAULT_DAYS})",
    )
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
    notify_cmd = sub.add_parser("notify", help="send a notice to the Telegram chat that receives alerts")
    notify_cmd.add_argument("text")
    fake = sub.add_parser("fake-kis", help="run a local fake KIS server for offline testing")
    fake.add_argument("--port", type=int, default=18080)
    fake.add_argument("--host", default="127.0.0.1", help="address to listen on (0.0.0.0 inside Docker)")
    fake.add_argument("--trades", action="store_true", help="push a synthetic trade per stock every second")
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
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        if args.command == "collect":
            asyncio.run(_collect())
        elif args.command == "measure-subscriptions":
            asyncio.run(_measure())
        elif args.command == "check-fallbacks":
            return asyncio.run(_check_fallbacks())
        elif args.command == "probe-candles":
            return asyncio.run(_probe_candles())
        elif args.command == "probe-supply":
            return asyncio.run(_probe_supply())
        elif args.command == "probe-net-buys":
            return asyncio.run(_probe_net_buys())
        elif args.command == "probe-close":
            return asyncio.run(_probe_close())
        elif args.command == "probe-dividends":
            return asyncio.run(_probe_dividends())
        elif args.command == "probe-nav":
            return asyncio.run(_probe_nav())
        elif args.command == "probe-night":
            return asyncio.run(_probe_night(args.day))
        elif args.command == "probe-expected-open":
            return _probe_expected_open(args.days)
        elif args.command == "registry-password":
            return _registry_password()
        elif args.command == "backfill":
            asyncio.run(_backfill())
        elif args.command == "archive":
            return asyncio.run(_archive(args.recheck))
        elif args.command == "replay":
            return _replay(args.day, args.db)
        elif args.command == "rescore":
            return _rescore()
        elif args.command == "fit":
            return _fit(args.algorithm, args.days)
        elif args.command == "prevalidate":
            return _prevalidate(args.algorithm, args.days)
        elif args.command == "train":
            return _train(args.day, args.from_store)
        elif args.command == "simulate":
            return _simulate(args.which)
        elif args.command == "rollback":
            return _rollback(args.algorithm)
        elif args.command == "retire":
            return _retire(args.algorithm)
        elif args.command == "unretire":
            return _unretire(args.algorithm)
        elif args.command == "notify":
            return asyncio.run(_notify(args.text))
        elif args.command == "macro":
            return _macro()
        elif args.command == "diagnose":
            return _diagnose(args.days)
        elif args.command == "doctor":
            return asyncio.run(_doctor(args.api))
        elif args.command == "signal-review":
            return _signal_review(args.days)
        elif args.command == "structure-check":
            return _structure_check(args.days)
        elif args.command == "daily-check":
            return _daily_check(args.days)
        elif args.command == "test-alert":
            return asyncio.run(_test_alert())
        elif args.command == "fake-kis":
            asyncio.run(_fake_kis(args.port, args.host, args.trades))
        elif args.command == "demo-engine":
            return asyncio.run(_demo_engine(args.port, args.host, args.db, args.pace))
    except SettingsError as e:
        print(f"configuration error: {e}", file=sys.stderr)
        return 2
    except KisError as e:
        # Non-zero so a supervisor (Docker restart policy) restarts the collector.
        print(f"KIS error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
