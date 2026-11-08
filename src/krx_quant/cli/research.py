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


def _sample_profile(day: date | None, algorithms: list[str], limit: int, top: int) -> int:
    """Where making a recorded day's samples spends its time (#512); each run printed as it ends."""
    from krx_quant.sample_profile import sample_profile

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


def _climate_prior(days: int) -> int:
    """The climate prior experiment for shannon (#429): its lines; exit 1 without a recorded day."""
    from krx_quant.climate_prior import REPORT, climate_prior_experiment, lines

    settings = Settings.from_env(require_kis=False)
    try:
        result = climate_prior_experiment(settings, days=days)
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        return 1
    print("\n".join(lines(result, settings.state_dir / REPORT)))
    return 0


def _normal_order(days: int) -> int:
    """The label-order experiment for normal_forecast's algorithms (#529): its lines; exit 1 without a
    recorded day."""
    from krx_quant.normal_order import REPORT, lines, normal_order_experiment

    settings = Settings.from_env(require_kis=False)
    try:
        result = normal_order_experiment(settings, days=days)
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        return 1
    print("\n".join(lines(result, settings.state_dir / REPORT)))
    return 0


def _neyman_variants(backfill: bool = False) -> int:
    """Neyman's variants experiment (#455): its next step's lines (the discovery, the window's
    progress or its verdict), or with ``backfill`` its verdict on the backfilled days (#532); exit 1
    without a recorded day in the discovery period (without a discovery, for the backfill), with a
    report that cannot be read, or a discovery that cannot set N."""
    from krx_quant.neyman_backfill import neyman_backfill
    from krx_quant.neyman_variants import neyman_variants

    settings = Settings.from_env(require_kis=False)
    try:
        run = neyman_backfill(settings) if backfill else neyman_variants(settings)
    except (FileNotFoundError, ValueError) as e:
        print(e, file=sys.stderr)
        return 1
    print("\n".join(run.lines))
    return 0


def _diagnose(days: int) -> int:
    from krx_quant.diagnose import diagnose
    from krx_quant.market_calendar import calendar_store
    from krx_quant.predictions import DB_NAME

    settings = Settings.from_env(require_kis=False)
    calendar = calendar_store(settings).calendar
    print(
        diagnose(settings.data_dir, settings.state_dir / DB_NAME, calendar, days, fee_rate=settings.fee_rate)
    )
    return 0


def _signal_review(days: int, since: date | None) -> int:
    from krx_quant.predictions import DB_NAME
    from krx_quant.signal_review import signal_review

    settings = Settings.from_env(require_kis=False)
    print(signal_review(settings.state_dir / DB_NAME, settings, datetime.now(KST).date(), days, since))
    return 0


def _error_breakdown(days: int, algorithm: str) -> int:
    from krx_quant.error_breakdown import error_breakdown
    from krx_quant.predictions import DB_NAME

    settings = Settings.from_env(require_kis=False)
    print(error_breakdown(settings.state_dir / DB_NAME, settings, datetime.now(KST).date(), days, algorithm))
    return 0


def _error_holdout() -> int:
    from krx_quant.holdout import error_holdout
    from krx_quant.predictions import DB_NAME

    settings = Settings.from_env(require_kis=False)
    print(error_holdout(settings.state_dir / DB_NAME))
    return 0


def _climate_gate() -> int:
    from krx_quant.climate_gate import climate_gate
    from krx_quant.predictions import DB_NAME

    settings = Settings.from_env(require_kis=False)
    print(climate_gate(settings.state_dir / DB_NAME))
    return 0


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


def _daily_check(days: int) -> int:
    from krx_quant.daily_check import daily_check
    from krx_quant.market_calendar import calendar_store
    from krx_quant.predictions import DB_NAME

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
    from krx_quant.market_calendar import calendar_store
    from krx_quant.structure_check import structure_check

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


def _sample_profile_arguments(parser: argparse.ArgumentParser) -> None:
    from krx_quant.sample_profile import LIMIT, PROFILED, TOP

    parser.add_argument(
        "--day", type=date.fromisoformat, help="the recorded day, YYYY-MM-DD (default: the latest finished)"
    )
    parser.add_argument(
        "--algorithms",
        nargs="+",
        default=list(PROFILED),
        help=f"the algorithms also profiled alone, with their own features (default: {' '.join(PROFILED)})",
    )
    parser.add_argument(
        "--limit",
        type=positive,
        default=round(LIMIT.total_seconds() / 60),
        help="minutes for every run together; a run still going stops there, the rest do not start"
        f" (default {round(LIMIT.total_seconds() / 60)})",
    )
    parser.add_argument(
        "--top",
        type=positive,
        default=TOP,
        help=f"functions listed by own time (default {TOP})",
    )


def _prediction_days_arguments(parser: argparse.ArgumentParser) -> None:
    """``climate-prior``'s and ``normal-order``'s: the pre-validation's days."""
    from krx_quant.prevalidation import PREVALIDATION_DAYS

    parser.add_argument(
        "--days",
        type=positive,
        default=PREVALIDATION_DAYS,
        help=f"recorded trading days to predict (default: {PREVALIDATION_DAYS})",
    )


def _diagnose_arguments(parser: argparse.ArgumentParser) -> None:
    from krx_quant.diagnose import DEFAULT_DAYS

    parser.add_argument(
        "--days",
        type=positive,
        default=DEFAULT_DAYS,
        help=f"the latest N trading days with data (default {DEFAULT_DAYS})",
    )


def _signal_review_arguments(parser: argparse.ArgumentParser) -> None:
    from krx_quant.signal_review import DEFAULT_DAYS

    parser.add_argument(
        "--days",
        type=positive,
        default=DEFAULT_DAYS,
        help=f"the latest N days with predictions (default {DEFAULT_DAYS}, the leaderboard's 20d)",
    )
    parser.add_argument(
        "--from",
        dest="since",
        type=date.fromisoformat,
        help="instead, the first N days with predictions on or after this day, YYYY-MM-DD: a fixed window,"
        " the same days however late it runs (#206)",
    )


def _error_breakdown_arguments(parser: argparse.ArgumentParser) -> None:
    from krx_quant.error_breakdown import DEFAULT_ALGORITHM, DEFAULT_DAYS

    parser.add_argument(
        "--days",
        type=positive,
        default=DEFAULT_DAYS,
        help=f"the latest N days with predictions (default {DEFAULT_DAYS}, the leaderboard's 20d)",
    )
    parser.add_argument(
        "--algorithm", default=DEFAULT_ALGORITHM, help=f"the algorithm (default {DEFAULT_ALGORITHM})"
    )


def _nightly_hit_rates_arguments(parser: argparse.ArgumentParser) -> None:
    from krx_quant.nightly_hit_rates import HIT_RATE_NIGHTS

    parser.add_argument(
        "--nights",
        type=positive,
        default=HIT_RATE_NIGHTS,
        help=f"the latest N nights whose promotion lists carry the hit rates (default {HIT_RATE_NIGHTS})",
    )


def _daily_check_arguments(parser: argparse.ArgumentParser) -> None:
    from krx_quant.daily_check import DAYS

    parser.add_argument(
        "--days",
        type=positive,
        default=DAYS,
        help=f"the latest N trading days (default {DAYS})",
    )


def _inverse_replay(day: date | None) -> int:
    """The down calls replayed through the inverse ETF's recorded quotes (#606): counts, ticks and won."""
    from krx_quant.inverse_replay import inverse_replay

    report, code = inverse_replay(Settings.from_env(require_kis=False), datetime.now(KST), day)
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
    sub.add_parser(
        "sample-profile",
        help="make one recorded day's training samples from scratch, as a night does, and time each stage"
        " and the top functions (#512); reads only, into a temporary directory, no price",
        arguments=_sample_profile_arguments,
    ).set_defaults(run=lambda args: _sample_profile(args.day, args.algorithms, args.limit, args.top))
    sub.add_parser(
        "climate-prior",
        help="compare the climate baseline, shannon and shannon with each slot's class frequencies on"
        " the pre-validation's folds (#429; reads the training samples, replays nothing)",
        arguments=_prediction_days_arguments,
    ).set_defaults(run=lambda args: _climate_prior(args.days))
    sub.add_parser(
        "normal-order",
        help="compare newton, gauss, granger, markov and kalman with normal_forecast's targets taken as"
        " independent and in the touch label's order on the pre-validation's folds (#529; reads the"
        " training samples, replays nothing)",
        arguments=_prediction_days_arguments,
    ).set_defaults(run=lambda args: _normal_order(args.days))
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
    sub.add_parser(
        "diagnose",
        help="print the measurements the open decisions need; reads only, writes nothing",
        arguments=_diagnose_arguments,
    ).set_defaults(run=lambda args: _diagnose(args.days))
    sub.add_parser(
        "signal-review",
        help="print what choosing the Telegram signal settings needs (#206); reads only, writes nothing",
        arguments=_signal_review_arguments,
    ).set_defaults(run=lambda args: _signal_review(args.days, args.since))
    sub.add_parser(
        "error-breakdown",
        help="print where an algorithm errs (#431): confusion matrices and lift over the climate baseline"
        " by symbol, 30-minute slot, spread and volatility; exploratory, reads only, writes nothing",
        arguments=_error_breakdown_arguments,
    ).set_defaults(run=lambda args: _error_breakdown(args.days, args.algorithm))
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
    sub.add_parser(
        "nightly-hit-rates",
        help="print the champion's and the challenger's hit rates the nightly reports carry beside the"
        " log loss's swap (#437), by algorithm over the latest nights: mismatches, what a hit-rate-first"
        " rule would have chosen; reads the store only, no price",
        arguments=_nightly_hit_rates_arguments,
    ).set_defaults(run=lambda args: _nightly_hit_rates(args.nights))
    sub.add_parser(
        "daily-check",
        help="print what the daily kinds did on the latest trading days (#264); reads only, writes nothing",
        arguments=_daily_check_arguments,
    ).set_defaults(run=lambda args: _daily_check(args.days))
    structure_cmd = sub.add_parser(
        "structure-check",
        help="measure the research's market-structure claims on the stored bars and candles; reads only",
    )
    structure_cmd.add_argument(
        "--days",
        type=positive,
        default=None,
        help="the latest N trading days with one-minute bars (default: all)",
    )
    structure_cmd.set_defaults(run=lambda args: _structure_check(args.days))
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
