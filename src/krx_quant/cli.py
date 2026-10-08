"""Command-line entry point: ``krx-quant <command>``."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from krx_quant.collector import measure_subscription_limit, run_collector
from krx_quant.config import Settings, SettingsError
from krx_quant.fake_kis import FakeKis
from krx_quant.kis import KisError


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


async def _fake_kis(port: int) -> None:
    fake = FakeKis(port=port)
    await fake.start()
    print(f"fake KIS listening: KIS_REST_URL={fake.rest_url} KIS_WS_URL={fake.ws_url}", flush=True)
    print(f"credentials: KIS_APP_KEY={fake.app_key} KIS_APP_SECRET={fake.app_secret}", flush=True)
    try:
        while True:
            await asyncio.sleep(10)
            await fake.send_pingpong()
    finally:
        await fake.stop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="krx-quant")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("collect", help="record KIS real-time feeds until interrupted")
    sub.add_parser(
        "measure-subscriptions",
        help="count how many real-time registrations KIS allows per session (stop the collector first)",
    )
    fake = sub.add_parser("fake-kis", help="run a local fake KIS server for offline testing")
    fake.add_argument("--port", type=int, default=18080)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        if args.command == "collect":
            asyncio.run(_collect())
        elif args.command == "measure-subscriptions":
            asyncio.run(_measure())
        elif args.command == "fake-kis":
            asyncio.run(_fake_kis(args.port))
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
