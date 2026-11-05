"""The probes' commands (#578): what an outside source's data means (KIS, Toss, the KRX Open API,
the recordings), measured before the engine uses it (CODING_STANDARDS.md "External data is probed
before it is used"), and the checks of what the closing flow kept. The trader's probes are the
trader's (``cli.trader``)."""

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


async def _check_closing_flow() -> int:
    from krx_quant.closing_flow_check import check_closing_flow

    print(await check_closing_flow(Settings.from_env(), datetime.now(KST)))
    return 0


def _check_krx_net_assets() -> int:
    from krx_quant.closing_flow_check import check_krx_net_assets

    print(check_krx_net_assets(Settings.from_env(require_kis=False), datetime.now(KST)))
    return 0


def _probe_sample_keys(day: date | None) -> int:
    """What changes the key of a day a night stored (#565)."""
    from krx_quant.sample_keys_probe import probe_sample_keys

    report, code = probe_sample_keys(Settings.from_env(require_kis=False), datetime.now(KST), day=day)
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
