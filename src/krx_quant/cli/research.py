"""The research's commands (#578): reviews and experiments on the recordings, the predictions and
the training samples, read-only; most are scheduled jobs' (``scheduled.research``)."""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta

from krx_quant.cli.core import Commands, positive
from krx_quant.config import Settings
from krx_quant.feeds.base import KST


def _smt(day: date | None) -> int:
    """A day's SMT events (#520), made again from its recording or backfilled bars, beside the
    engine's live ones: counts and times, no price. 1 if the day has neither."""
    from krx_quant.market_calendar import calendar_store
    from krx_quant.predictions import DB_NAME, read_only, smt_events
    from krx_quant.smt import lines as smt_lines
    from krx_quant.smt import reconstruct as reconstruct_smt

    settings = Settings.from_env(require_kis=False)
    day = day or datetime.now(KST).date()
    start = datetime(day.year, day.month, day.day, tzinfo=KST)
    with read_only(settings.state_dir / DB_NAME) as db:
        live = [] if db is None else smt_events(db, start, start + timedelta(days=1))
    made = reconstruct_smt(settings.data_dir, day, calendar_store(settings).calendar)
    for line in smt_lines(day, made, live):
        print(line)
    return 0 if made is not None or live else 1


def _smt_experiment(holdout: bool) -> int:
    """The SMT experiment (#523): the discovery, or with ``holdout`` the pre-registered verdict."""
    from krx_quant.market_calendar import calendar_store
    from krx_quant.smt_experiment import smt_experiment

    settings = Settings.from_env(require_kis=False)
    calendar = calendar_store(settings).calendar
    print(smt_experiment(settings, calendar, datetime.now(KST), holdout))
    return 0


def _nightly_hit_rates(nights: int) -> int:
    """The hit rates beside the nightly swap over the latest ``nights`` nights (#437); exit 1 without
    the store or a night that carries them."""
    from krx_quant.nightly_hit_rates import compared_nights, lines
    from krx_quant.object_store import open_store

    settings = Settings.from_env(require_kis=False)
    store = open_store(settings)
    if store is None:
        print("KRX_OBJECT_STORE is not set: the nights' promotion lists are there", file=sys.stderr)
        return 1
    found = compared_nights(store, nights)
    print("\n".join(lines(found, nights)))
    return 0 if found else 1


def _nightly_hit_rates_arguments(parser: argparse.ArgumentParser) -> None:
    from krx_quant.nightly_hit_rates import HIT_RATE_NIGHTS

    parser.add_argument(
        "--nights",
        type=positive,
        default=HIT_RATE_NIGHTS,
        help=f"the latest N nights whose promotion lists carry the hit rates (default {HIT_RATE_NIGHTS})",
    )


def _inverse_replay(day: date | None) -> int:
    """The down calls replayed through the inverse ETF's recorded quotes (#606): counts, ticks and won."""
    from krx_quant.inverse_replay import inverse_replay

    report, code = inverse_replay(Settings.from_env(require_kis=False), datetime.now(KST), day)
    print(report)
    return code


def _verdict_replay(day: date | None, days: int) -> int:
    """The verdict (#676) made again at each recorded bar close and its BUYs replayed (#679): counts, ticks
    and won."""
    from krx_quant.verdict_replay import verdict_replay

    report, code = verdict_replay(Settings.from_env(require_kis=False), datetime.now(KST), day, days)
    print(report)
    return code


def add(sub: Commands) -> None:
    """The research's commands, each with its handler (``cli._parser``)."""
    smt_cmd = sub.add_parser(
        "smt",
        help="count a day's SMT divergences (#520), made again from its recording or backfilled bars,"
        " beside the engine's live ones; reads only, no price",
    )
    smt_cmd.add_argument("--day", type=date.fromisoformat, help="the day, YYYY-MM-DD (default: today)")
    smt_cmd.set_defaults(run=lambda args: _smt(args.day))
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
    sub.add_parser(
        "nightly-hit-rates",
        help="print the champion's and the challenger's hit rates the nightly reports carry beside the"
        " log loss's swap (#437), by algorithm over the latest nights: mismatches, what a hit-rate-first"
        " rule would have chosen; reads the store only, no price",
        arguments=_nightly_hit_rates_arguments,
    ).set_defaults(run=lambda args: _nightly_hit_rates(args.nights))
    inverse_cmd = sub.add_parser(
        "inverse-replay",
        help="replay the explicit down calls through the inverse ETF's recorded KRX quotes (#606): one"
        " share at the first ask after the call, sold at the bid on the stock's barrier or expiry; net"
        " won after the commissions; reads only, places no order, no price",
    )
    inverse_cmd.add_argument(
        "--day",
        type=date.fromisoformat,
        help="the last day replayed, YYYY-MM-DD (default: the latest trading day whose session is over)",
    )
    inverse_cmd.set_defaults(run=lambda args: _inverse_replay(args.day))
    verdict_cmd = sub.add_parser(
        "verdict-replay",
        help="make the verdict (#676) again at each bar close of the recorded days, with the barrier"
        " algorithms the live engine ran and with every one walked forward (#679), and replay its BUYs a"
        " share each by the paper trader's rules; reads only, places no order, no price",
    )
    verdict_cmd.add_argument(
        "--day",
        type=date.fromisoformat,
        help="the last day replayed, YYYY-MM-DD (default: the latest trading day whose session is over)",
    )
    verdict_cmd.add_argument(
        "--days", type=positive, default=20, help="the recorded trading days replayed (default 20)"
    )
    verdict_cmd.set_defaults(run=lambda args: _verdict_replay(args.day, args.days))
