"""Command-line entry point: ``krx-quant <command>``."""

from __future__ import annotations

import argparse
import asyncio
import itertools
import logging
import signal
import sys
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path

from aiohttp import web

from krx_quant.algorithms import ALGORITHMS
from krx_quant.archive import run_archive
from krx_quant.backfill import run_backfill
from krx_quant.collector import measure_subscription_limit, run_collector
from krx_quant.config import Settings, SettingsError
from krx_quant.demo import demo_app
from krx_quant.fake_kis import FakeKis
from krx_quant.feeds.base import KST
from krx_quant.kis import KisError
from krx_quant.lifecycle import Status
from krx_quant.model_shelf import Shelf
from krx_quant.nightly import run_training
from krx_quant.notify import send_notice, send_test_alert
from krx_quant.predictions import DB_NAME, PredictionStore
from krx_quant.replay import replay_day
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
    print(f"replayed {day} into {replayed.db} (counts on basis A)")
    for s in summary:
        print(
            f"{s.algorithm}: {s.predictions} predictions, {s.skipped} skipped, {s.scored} scored,"
            f" {s.unscorable} unscorable, {s.pending} pending"
        )
    if summary:
        s = summary[0]
        print(f"outcomes: {s.up} up, {s.flat} flat, {s.down} down")
    return 0


def _fit(name: str, days: int | None) -> int:
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


def _train(day: date | None) -> int:
    try:
        night = asyncio.run(run_training(Settings.from_env(require_kis=False), day))
    except SettingsError:
        raise  # a configuration error, reported as one (exit 2)
    except (FileNotFoundError, ValueError) as e:
        print(e, file=sys.stderr)
        return 1
    print(night.summary())
    print(f"promoted: {night.promoted or 'nothing'}; the server applies it at 08:00")
    return 0


def _rollback(name: str) -> int:
    settings = Settings.from_env(require_kis=False)
    if name not in {a.name for a in ALGORITHMS}:
        print(f"unknown algorithm {name!r}", file=sys.stderr)
        return 1
    try:
        model = Shelf(settings.state_dir).rollback(name, datetime.now(KST))
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


async def _demo_engine(port: int, host: str, db: Path) -> int:
    db.parent.mkdir(parents=True, exist_ok=True)
    try:
        app = await demo_app(db)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    print(f"demo Engine API: http://{host}:{port}/v1/status (2026-10-12 10:30, made-up data)", flush=True)
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
    fit_cmd = sub.add_parser(
        "fit", help="train an algorithm on the latest recorded or backfilled days and save its model"
    )
    fit_cmd.add_argument("algorithm", help="e.g. newton")
    fit_cmd.add_argument(
        "--days", type=_positive, help="trading days to train on (default: the algorithm's own)"
    )
    train_cmd = sub.add_parser(
        "train", help="train every algorithm on KRX_OBJECT_STORE's latest days and store the promotion list"
    )
    train_cmd.add_argument(
        "--day", type=date.fromisoformat, help="the night to train, YYYY-MM-DD (default: latest)"
    )
    rollback_cmd = sub.add_parser("rollback", help="put an algorithm back on the model it ran before")
    rollback_cmd.add_argument("algorithm")
    retire_cmd = sub.add_parser("retire", help="retire an algorithm: it is no longer run; its records stay")
    retire_cmd.add_argument("algorithm")
    unretire_cmd = sub.add_parser("unretire", help="put a retired algorithm back on probation")
    unretire_cmd.add_argument("algorithm")
    sub.add_parser("test-alert", help="send a test message to the Telegram chat that receives alerts")
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
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        if args.command == "collect":
            asyncio.run(_collect())
        elif args.command == "measure-subscriptions":
            asyncio.run(_measure())
        elif args.command == "backfill":
            asyncio.run(_backfill())
        elif args.command == "archive":
            return asyncio.run(_archive(args.recheck))
        elif args.command == "replay":
            return _replay(args.day, args.db)
        elif args.command == "fit":
            return _fit(args.algorithm, args.days)
        elif args.command == "train":
            return _train(args.day)
        elif args.command == "rollback":
            return _rollback(args.algorithm)
        elif args.command == "retire":
            return _retire(args.algorithm)
        elif args.command == "unretire":
            return _unretire(args.algorithm)
        elif args.command == "notify":
            return asyncio.run(_notify(args.text))
        elif args.command == "test-alert":
            return asyncio.run(_test_alert())
        elif args.command == "fake-kis":
            asyncio.run(_fake_kis(args.port, args.host, args.trades))
        elif args.command == "demo-engine":
            return asyncio.run(_demo_engine(args.port, args.host, args.db))
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
