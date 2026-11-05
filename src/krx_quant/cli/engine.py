"""The engine's commands (#578): the collector, which runs the engine, and its algorithms' models and
standing: replay, rescore, fit, pre-validate, train, simulate, roll back, retire."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from krx_quant.cli.core import Commands, positive
from krx_quant.config import Settings, SettingsError
from krx_quant.feeds.base import KST

if TYPE_CHECKING:
    from krx_quant.daily import DailyKind
    from krx_quant.lifecycle import Status


async def _collect() -> int:
    from krx_quant.collector import run_collector

    settings = Settings.from_env()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows: Ctrl+C raises KeyboardInterrupt instead
            pass
    logging.getLogger("krx_quant.cli").info(  # the line's logger as before the families (#578)
        "recording %s into %s", ",".join(settings.symbols), settings.data_dir.resolve()
    )
    await run_collector(settings, stop=stop)
    return 0


def _replay(day: date, db: Path | None) -> int:
    from krx_quant.replay import replay_day

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
    from krx_quant.rescore import EngineRunning, rescore

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
    from krx_quant.daily_training import daily_algorithm, fit_daily
    from krx_quant.training import fit_algorithm

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
    from krx_quant.daily_training import daily_algorithm, prevalidate_daily
    from krx_quant.prevalidation import PrevalidationRunning, report_path

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
    from krx_quant.nightly import log_summary, run_training

    try:
        settings = Settings.from_env(require_kis=False)
        night = asyncio.run(run_training(settings, day))
    except SettingsError:
        raise  # a configuration error, reported as one (exit 2)
    except (FileNotFoundError, ValueError) as e:
        print(e, file=sys.stderr)
        return 1
    log_summary(night)
    return 0


def _prevalidate(name: str, days: int, revision: bool = False) -> int:
    from krx_quant.algorithms import RegistrationError
    from krx_quant.daily_training import daily_algorithm
    from krx_quant.market_calendar import calendar_store, first_whole_day
    from krx_quant.predictions import DB_NAME, REVISION_CHANGED, latest_revisions, read_only
    from krx_quant.prevalidation import REGISTERED_BEFORE, PrevalidationRunning, prevalidate, report_path

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


def _simulate(which: str) -> int:
    """Simulate the probations that wait for it (#298), print what came of them; exit 1 if one
    could not run."""
    from krx_quant.algorithms import RegistrationError
    from krx_quant.kind_registry import DAILY_KINDS
    from krx_quant.simulation import DAILY, SimulationRunning, report, simulate_bar_close, simulate_daily

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
    from krx_quant.algorithms import ALGORITHMS, KIND_ALGORITHMS
    from krx_quant.kind_registry import kind_named
    from krx_quant.model_shelf import Shelf

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
    from krx_quant.lifecycle import Status

    return _set_status(name, Status.RETIRED, "retired by command", allowed=lambda s: s is not Status.RETIRED)


def _unretire(name: str) -> int:
    """Put a retired algorithm back on probation."""
    from krx_quant.lifecycle import Status

    return _set_status(name, Status.PROBATION, "unretired by command", allowed=lambda s: s is Status.RETIRED)


def _set_status(name: str, to: Status, reason: str, *, allowed: Callable[[Status], bool]) -> int:
    from krx_quant.predictions import DB_NAME, PredictionStore

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


def _prevalidate_arguments(parser: argparse.ArgumentParser) -> None:
    from krx_quant.prevalidation import PREVALIDATION_DAYS

    parser.add_argument("algorithm", help="e.g. kalman")
    parser.add_argument(
        "--days",
        type=positive,
        default=PREVALIDATION_DAYS,
        help=f"recorded trading days to replay (default: {PREVALIDATION_DAYS})",
    )
    parser.add_argument(
        "--revision",
        action="store_true",
        help="judge the running algorithm's changed code on the recorded days after the change (#454)",
    )


def _simulate_arguments(parser: argparse.ArgumentParser) -> None:
    from krx_quant.simulation import BAR_CLOSE_COMMAND, DAILY

    parser.add_argument(
        "which",
        choices=[DAILY, BAR_CLOSE_COMMAND],
        help="daily: the daily kinds' algorithms; bar-close: the direction's and the other kinds'"
        " (the latest recorded days, 10 to 20)",
    )


def add(sub: Commands) -> None:
    """The engine's commands, each with its handler (``cli._parser``)."""
    sub.add_parser("collect", help="record KIS real-time feeds until interrupted").set_defaults(
        run=lambda args: asyncio.run(_collect())
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
    replay_cmd.set_defaults(run=lambda args: _replay(args.day, args.db))
    sub.add_parser(
        "rescore",
        help="label every recorded day's bar closes again with the touch target (stop the engine first)",
    ).set_defaults(run=lambda args: _rescore())
    fit_cmd = sub.add_parser(
        "fit", help="train an algorithm on the latest recorded or backfilled days and save its model"
    )
    fit_cmd.add_argument("algorithm", help="e.g. newton")
    fit_cmd.add_argument(
        "--days", type=positive, help="trading days to train on (default: the algorithm's own)"
    )
    fit_cmd.set_defaults(run=lambda args: _fit(args.algorithm, args.days))
    sub.add_parser(
        "prevalidate",
        help="replay a new algorithm on the latest recorded days; the engine starts it only if it passes",
        arguments=_prevalidate_arguments,
    ).set_defaults(run=lambda args: _prevalidate(args.algorithm, args.days, args.revision))
    train_cmd = sub.add_parser(
        "train", help="train every algorithm on KRX_OBJECT_STORE's latest days and store the promotion list"
    )
    train_cmd.add_argument(
        "--day", type=date.fromisoformat, help="the night to train, YYYY-MM-DD (default: latest)"
    )
    train_cmd.set_defaults(run=lambda args: _train(args.day))
    sub.add_parser(
        "simulate",
        help="replay the latest trading days as the probation of each algorithm that waits for it (#298),"
        " and judge it at once (safe beside the engine)",
        arguments=_simulate_arguments,
    ).set_defaults(run=lambda args: _simulate(args.which))
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
