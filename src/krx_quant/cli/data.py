"""The data's commands (#578): KIS's subscription limit and REST fallbacks, the one-minute bars'
backfill, the upload to the Object Store and the macro indicators."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime

from krx_quant.cli.core import Commands
from krx_quant.config import Settings
from krx_quant.feeds.base import KST


async def _measure() -> int:
    from krx_quant.collector import measure_subscription_limit

    limit = await measure_subscription_limit(Settings.from_env())
    print(f"KIS accepted {limit} real-time registrations in one session.")
    print(f"Set KIS_WS_MAX_SUBSCRIPTIONS={limit} in .env to stay within it.")
    return 0


async def _check_fallbacks() -> int:
    from krx_quant.collector import check_fallbacks

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


async def _backfill() -> int:
    from krx_quant.backfill import run_backfill

    filled = await run_backfill(Settings.from_env())
    print(f"filled one-minute bars for {filled} series-days")
    return 0


async def _archive(recheck: bool) -> int:
    from krx_quant.archive import run_archive

    report = await run_archive(Settings.from_env(), recheck=recheck)
    print(f"archived: {report.uploaded} uploaded, {report.unchanged} unchanged, {len(report.failed)} failed")
    for partition in report.failed:
        print(f"failed: {partition}", file=sys.stderr)
    return 1 if report.failed else 0


def _macro() -> int:
    from krx_quant.macro import run_macro

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


def add(sub: Commands) -> None:
    """The data's commands, each with its handler (``cli._parser``)."""
    sub.add_parser(
        "measure-subscriptions",
        help="count how many real-time registrations KIS allows per session (stop the collector first)",
    ).set_defaults(run=lambda args: asyncio.run(_measure()))
    sub.add_parser(
        "check-fallbacks",
        help="ask each feed's REST fallback once and print what it would record (safe beside the collector)",
    ).set_defaults(run=lambda args: asyncio.run(_check_fallbacks()))
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
    sub.add_parser("macro", help="read today's macro indicators and decide the regime, once").set_defaults(
        run=lambda args: _macro()
    )
