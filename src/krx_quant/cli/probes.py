"""The probes' commands (#578): what an outside source's data means (KIS, Toss, the KRX Open API,
the recordings), measured before the engine uses it (CODING_STANDARDS.md "External data is probed
before it is used"). The trader's probes are the trader's (``cli.trader``)."""

from __future__ import annotations

import asyncio
from datetime import date, datetime

from krx_quant.cli.core import Commands
from krx_quant.config import Settings
from krx_quant.feeds.base import KST


async def _probe_supply() -> int:
    from krx_quant.supply_probe import probe_supply

    calls, failed = await probe_supply(Settings.from_env(), datetime.now(KST).date())
    return 1 if failed == calls else 0  # nothing answered


async def _probe_nav() -> int:
    from krx_quant.nav_probe import probe_nav

    calls, failed = await probe_nav(Settings.from_env(), datetime.now(KST))
    return 1 if failed == calls else 0  # nothing answered


async def _probe_krx_etf() -> int:
    from krx_quant.krx_etf_probe import probe_krx_etf

    # The KIS keys are not required: without them the KRX part runs and the KIS comparison is skipped.
    calls, with_rows = await probe_krx_etf(Settings.from_env(require_kis=False), datetime.now(KST))
    return 0 if with_rows else 1  # no day answered with rows


async def _probe_night(day: date | None) -> int:
    from krx_quant.night_probe import probe_night

    print(await probe_night(Settings.from_env(require_kis=False), datetime.now(KST), day))
    return 0


def _probe_gaps() -> int:
    from krx_quant.gaps_probe import probe_gaps

    print(probe_gaps(Settings.from_env(require_kis=False), datetime.now(KST)))
    return 0


def _probe_sample_keys(day: date | None) -> int:
    """What changes the key of a day a night stored (#565)."""
    from krx_quant.sample_keys_probe import probe_sample_keys

    report, code = probe_sample_keys(Settings.from_env(require_kis=False), datetime.now(KST), day=day)
    print(report)
    return code


def _bar_close_times(day: date | None, night: bool) -> int:
    """How long a trading day's bar closes took, against the days before (#589)."""
    from krx_quant.bar_close_times import bar_close_times, tonight

    now = datetime.now(KST)
    report, code = bar_close_times(Settings.from_env(require_kis=False), now, tonight(now) if night else day)
    print(report)
    return code


def _session_memory(day: date | None, night: bool) -> int:
    """What the engine's memory did after a day's close (#623)."""
    from krx_quant.bar_close_times import tonight
    from krx_quant.session_memory_probe import session_memory

    now = datetime.now(KST)
    report, code = session_memory(Settings.from_env(require_kis=False), now, tonight(now) if night else day)
    print(report)
    return code


def add(sub: Commands) -> None:
    """The probes' commands, each with its handler (``cli._parser``)."""
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
    times_cmd = sub.add_parser(
        "bar-close-times",
        help="how long a trading day's bar closes took, against the five trading days before (#589): each"
        " bar close's first record, final consensus and last record, each algorithm's time after the first"
        " record, the bar closes that held the event loop, the trader's quote stalls; read-only, no price",
    )
    times_day = times_cmd.add_mutually_exclusive_group()
    times_day.add_argument(
        "--day",
        type=date.fromisoformat,
        help="the trading day, YYYY-MM-DD (default: the latest day with predictions, through today)",
    )
    times_day.add_argument(
        "--tonight",
        action="store_true",
        help="the day of the night it runs in (the date, or the one before until 06:00), as the scheduled"
        " job runs it; exit 1 when that day has no predictions",
    )
    times_cmd.set_defaults(run=lambda args: _bar_close_times(args.day, args.tonight))
    memory_cmd = sub.add_parser(
        "session-memory",
        help="what the engine's memory did after a day's close (#623): each rest's counts let go and kept"
        " and the engine's memory before and after, the server's and the engine's memory by hour from"
        " 15:00, the server's now; read-only, no price",
    )
    memory_day = memory_cmd.add_mutually_exclusive_group()
    memory_day.add_argument("--day", type=date.fromisoformat, help="the day, YYYY-MM-DD (default: today)")
    memory_day.add_argument(
        "--tonight",
        action="store_true",
        help="the day of the night it runs in (the date, or the one before until 06:00), as the scheduled"
        " job runs it; exit 1 when the engine stored no rest that day",
    )
    memory_cmd.set_defaults(run=lambda args: _session_memory(args.day, args.tonight))
