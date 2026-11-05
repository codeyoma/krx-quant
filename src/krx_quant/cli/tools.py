"""The tools' commands (#578): the server's state, the Telegram notices, and the fake KIS and the
demo engine to work without the server."""

from __future__ import annotations

import argparse
import asyncio
import itertools
import math
import sys
from datetime import datetime
from pathlib import Path

from krx_quant.cli.core import Commands
from krx_quant.config import Settings, lenient_settings
from krx_quant.feeds.base import KST


async def _test_alert() -> int:
    from krx_quant.notify import send_test_alert

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


async def _doctor(api: str | None) -> int:
    """The summary even when a setting keeps the engine from starting: the error is in it."""
    from krx_quant.doctor import doctor

    settings, error = lenient_settings()
    print(await doctor(settings, datetime.now(KST), api, config_error=error), end="")
    return 0


async def _notify(text: str) -> int:
    """Send a notice from a script (the server's auto-update); 1 if it did not go."""
    from krx_quant.notify import send_notice

    return 0 if await send_notice(Settings.from_env(require_kis=False), text) else 1


async def _fake_kis(port: int, host: str, trades: bool) -> int:
    from krx_quant.fake_kis import FakeKis

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
    from aiohttp import web

    from krx_quant.demo import demo_app, demo_morning

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


def add(sub: Commands) -> None:
    """The tools' commands, each with its handler (``cli._parser``)."""
    sub.add_parser(
        "test-alert", help="send a test message to the Telegram chat that receives alerts"
    ).set_defaults(run=lambda args: asyncio.run(_test_alert()))
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
