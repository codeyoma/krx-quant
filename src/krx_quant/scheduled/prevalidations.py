"""The pre-validation and simulation jobs (#204, #298, #454; a module of their own since #500),
built from the algorithm registries: one pre-validation for each algorithm registered after
pre-validation existed, one for each bar-close algorithm's changed code and one for each daily
algorithm, and the simulated probations. ``JOBS`` (``krx_quant.scheduled``) builds them from the
registries and lists them in the order they run.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from krx_quant.algorithms import ALGORITHMS, DAILY_ALGORITHMS, KIND_ALGORITHMS, Algorithm, KindAlgorithm
from krx_quant.config import Settings
from krx_quant.daily import DailyAlgorithm, DailyKind
from krx_quant.daily_features import pending_inputs
from krx_quant.kind_registry import DAILY_KINDS, daily_kind_named
from krx_quant.lifecycle import REGISTERED, Standing, Status
from krx_quant.market_calendar import MarketCalendar, first_whole_day
from krx_quant.predictions import Revision
from krx_quant.prevalidation import (
    MIN_DAYS,
    REGISTERED_BEFORE,
    LastReport,
    last_report,
    report_path,
    revision_report,
)
from krx_quant.queue_screen import QUEUE, queue_order
from krx_quant.scheduled.core import FAILED, OK, Attachment, Job, JobRecord, Night, Ran, Reads, Target
from krx_quant.training import finished_days, is_recorded

log = logging.getLogger("krx_quant.scheduled")

# What the pre-validations' and simulations' conditions ask (#651; before, fields of the one ``Facts``
# every job was asked with): read once a night, when the first of them is considered, and again after
# the queue screen (``Job.refreshes``, #599) and for the quiet night's turn.


@dataclass(frozen=True)
class Revising:
    """A running bar-close algorithm's changed code, waiting for its pre-validation (#454)."""

    boundary: datetime  # when the engine first ran it
    since: date  # its first whole trading day: the days it is judged on start there
    recorded_days: int  # the recorded days from ``since``, as ``prevalidate --revision`` counts them
    report: LastReport | None  # its latest report if made for this revision; None without one


@dataclass(frozen=True)
class PrevalidationFacts:
    """What the engine knows that a pre-validation's or a simulation's condition asks."""

    recorded_days: int = 0  # recorded days, as ``prevalidate`` counts them (MIN_DAYS, #204)
    waiting: tuple[str, ...] = ()  # the algorithms waiting for their pre-validation (#204)
    # The latest pre-validation report of each registered algorithm that has one (#204).
    reports: Mapping[str, LastReport] = field(default_factory=dict)
    # The daily features whose data on this server is behind their version (#297,
    # ``daily_features.pending_inputs``): a pre-validation reading them waits for it.
    pending_inputs: tuple[str, ...] = ()
    # The daily algorithms on a probation that waits for its simulation (#298,
    # ``PredictionEngine.to_simulate``).
    simulating_daily: tuple[str, ...] = ()
    # The daily kinds whose answers of the latest days are not all in on this server
    # (``DailyKind.pending``, #264: ``open_to_close``'s closing auction prices): their algorithms'
    # simulation and scheduled walk-forward wait for them.
    pending_answers: tuple[str, ...] = ()
    # The bar-close kinds' algorithms on a probation that waits for its simulation (#298).
    simulating: tuple[str, ...] = ()
    # The running bar-close algorithms whose changed code waits for its pre-validation (#454,
    # ``PredictionEngine.revising``), by name.
    revising: Mapping[str, Revising] = field(default_factory=dict)
    # The order of the bar-close pre-validations the latest queue screen set (#599, ``read_queue``):
    # none without a screen of the last few nights.
    queue: tuple[str, ...] = ()
    # The bar-close algorithms on a probation begun when their pre-validation passed before #681
    # (``lifecycle.REGISTERED``) whose report holds no promotion test: judged again by it (#681).
    rejudging: tuple[str, ...] = ()


def recorded_days(data_dir: Path, symbols: Sequence[str], now: datetime) -> int:
    """The recorded days ``prevalidate`` would replay, as it counts them."""
    return sum(1 for d in finished_days(data_dir, symbols, now) if is_recorded(data_dir, d, symbols))


def prevalidation_reports(
    state_dir: Path, algorithms: Sequence[Algorithm | KindAlgorithm | DailyAlgorithm], target_version: int
) -> dict[str, LastReport]:
    """The latest pre-validation report of each of ``algorithms`` that has one, as it stands for
    the code and target it runs now."""
    reports = {a.name: last_report(state_dir, a, target_version) for a in algorithms}
    return {name: report for name, report in reports.items() if report is not None}


def revising_facts(
    settings: Settings,
    calendar: MarketCalendar,
    now: datetime,
    revising: Sequence[tuple[Algorithm | KindAlgorithm, Revision]],
) -> dict[str, Revising]:
    """Each of ``revising`` (``PredictionEngine.revising``) as the revision jobs ask (#454): its first
    whole trading day, the recorded days from it and its report made for this revision."""
    if not revising:
        return {}
    recorded = [
        d
        for d in finished_days(settings.data_dir, settings.symbols, now)
        if is_recorded(settings.data_dir, d, settings.symbols)
    ]
    out = {}
    for algorithm, revision in revising:
        since = first_whole_day(calendar, revision.boundary)
        found = revision_report(settings.state_dir, algorithm, revision.boundary, settings.target_version)
        out[algorithm.name] = Revising(
            revision.boundary,
            since,
            sum(1 for d in recorded if d >= since),
            None if found is None else found[0],
        )
    return out


def pending_answers(settings: Settings, calendar: MarketCalendar, now: datetime) -> list[str]:
    """The daily kinds whose answers of the latest days are not all in
    (``PrevalidationFacts.pending_answers``)."""
    out = []
    for kind in DAILY_KINDS:
        why = (
            None if kind.pending is None else kind.pending(settings.data_dir, calendar, settings.symbols, now)
        )
        if why is not None:
            log.info("%s: %s", kind.name, why)
            out.append(kind.name)
    return out


async def read_queue(reads: Reads) -> tuple[str, ...]:
    """The order of the bar-close pre-validations the latest queue screen set (#599): the quiet night's
    turn goes by it (``Job.order``)."""
    sources = reads.sources
    return await asyncio.to_thread(queue_order, sources.settings.state_dir, sources.clock.now())


async def read_prevalidation_facts(reads: Reads) -> PrevalidationFacts:
    """What the pre-validations' and simulations' conditions ask tonight: every recorded day's folder,
    every registered algorithm's report and a few small queries of the engine's database."""
    settings, calendar, clock = reads.sources.settings, reads.sources.calendar, reads.sources.clock
    engine = reads.sources.running
    recorded = await asyncio.to_thread(recorded_days, settings.data_dir, settings.symbols, clock.now())
    everyone: list[Algorithm | KindAlgorithm | DailyAlgorithm] = [
        *engine.registered,
        *engine.kind_registered,
        *engine.daily_registered,
    ]
    reports = await asyncio.to_thread(
        prevalidation_reports, settings.state_dir, everyone, settings.target_version
    )
    daily = sorted({f for a in engine.daily_registered for f in a.features})
    pending = await asyncio.to_thread(pending_inputs, daily, settings.state_dir, settings.symbols)
    answers = await asyncio.to_thread(pending_answers, settings, calendar, clock.now())
    simulating, simulating_daily = engine.to_simulate()  # the engine's database: on this thread
    revising = engine.revising()  # likewise (#454)
    rejudging = to_rejudge(engine.store.statuses(), [*engine.registered, *engine.kind_registered], reports)
    return PrevalidationFacts(
        recorded_days=recorded,
        waiting=tuple(engine.waiting()),
        reports=reports,
        pending_inputs=tuple(pending),
        simulating_daily=tuple(simulating_daily),
        pending_answers=tuple(answers),
        simulating=tuple(simulating),
        revising=await asyncio.to_thread(revising_facts, settings, calendar, clock.now(), revising),
        queue=await reads(read_queue),
        rejudging=rejudging,
    )


def to_rejudge(
    statuses: Mapping[str, Standing],
    bar_close: Sequence[Algorithm | KindAlgorithm],
    reports: Mapping[str, LastReport],
) -> tuple[str, ...]:
    """The bar-close algorithms of ``bar_close`` (the registries' order) on a probation begun when
    their pre-validation passed before #681 (``REGISTERED``), whose report of the code they run holds
    no promotion test (``LastReport.promotion``): their pre-validation judges them again (#681)."""
    return tuple(
        a.name
        for a in bar_close
        if a.name not in REGISTERED_BEFORE
        and (s := statuses.get(a.name)) is not None
        and s.status is Status.PROBATION
        and s.reason == REGISTERED
        and (r := reports.get(a.name)) is not None
        and r.current
        and r.promotion is None
    )


# A pre-validation's or a simulation's night.
Turn = Night[PrevalidationFacts]

# The daily kinds' issue, open_to_close's: its pre-validation and simulation post there too. The daily
# check (``daily-check-264``, ``probes``) named it until that job left ``JOBS`` (#505).
#
# > **바뀜** (2026-11-10, #688): #264 → #204 (``PREVALIDATION_ISSUE``, below). 이유: the user closes
# > #264 (2026-11-10); the pre-validations' issue stays open. A daily pre-validation of open_to_close
# > then posts once there (``daily_prevalidation_jobs`` drops a target named twice).
DAILY_CHECK_ISSUE = 204

# ⑤ The pre-validations (#204): a new algorithm runs live once its pre-validation has passed
# (``prevalidation.py``). Once ``MIN_DAYS`` trading days are recorded, one waiting algorithm
# is pre-validated a night in its turn (``prevalidation_tonight``): a run replays 20 days beside
# the live engine, on a 2 GB server with burst CPU. On a quiet night a second one follows
# (#546, ``prevalidations_quiet``): once every job of the night had its turn, the first in turn whose
# time limit still ends by 05:30; never more than two. They go in this order, then any other
# waiting algorithm in the registry's. A verdict on the code and the target an algorithm runs
# now (passed, or refused) is not asked for again: a deploy that changes its code, or the
# target, asks again. A run that left no verdict on them (it crashed, or hit its time limit)
# runs again after a new deploy, once the algorithms not tried yet have had their turn. A run
# a restart cut runs again the next night (the scheduler).
PREVALIDATE_FIRST = ("mandelbrot", "breiman", "kalman", "shannon", "poisson")
PREVALIDATION_ISSUE = 204
# The bar-close pre-validations a night (#546): one in its turn, a second on a quiet night.
PREVALIDATIONS_A_NIGHT = 2
# A run takes about 20 times a day's replay (20 days, the algorithm retrained before each). The
# limits are the slowest run's pace over 20 days, rounded up with room to spare (#546; they were 5
# hours, 6 for the Breimans, set before any run). A direction algorithm's day took 6.5 minutes at
# most (neyman, 2026-11-01: 15 days in 1 h 37 min): 2 h 10 min for 20, a 3-hour limit. Another
# bar-close kind's algorithm replays its kind besides the direction (#263) and takes longest: 11.4
# minutes a day (mandelbrot, 2026-10-24: 10 days in 1 h 54 min), 10.9 (breiman-30m, 2026-11-04: 18
# days in 3 h 16 min): 3 h 48 min for 20, a limit of 4 h 30 min. Each must start by 05:30 less its
# limit (02:30; the other kinds' 01:00).
PREVALIDATION_TIMEOUT = timedelta(hours=3)
PREVALIDATION_TIMEOUTS = {a.name: timedelta(hours=4, minutes=30) for a in KIND_ALGORITHMS}
# An algorithm's turn tonight: never tried (or cut by a restart), or tried again; or on a probation
# judged again by the walk-forward replay (#681), before them.
FRESH, AGAIN, REJUDGE = "fresh", "again", "rejudge"


def prevalidation_job_name(algorithm: str) -> str:
    return f"prevalidate-{algorithm}"


def prevalidation_order(names: Sequence[str]) -> tuple[str, ...]:
    """The algorithms to pre-validate, in turn: those of ``PREVALIDATE_FIRST`` registered, then
    any other registered after pre-validation existed (``names``: the registries' order)."""
    new = [name for name in names if name not in REGISTERED_BEFORE]
    return (*(n for n in PREVALIDATE_FIRST if n in new), *(n for n in new if n not in PREVALIDATE_FIRST))


def prevalidation_turn(night: Turn, algorithm: str) -> str | None:
    """Whether ``algorithm`` would be pre-validated tonight: REJUDGE (on a probation its walk-forward
    replay judges again, #681: first), FRESH, AGAIN (its last run left no verdict, on older code: it
    waits for the FRESH ones), or None (not tonight)."""
    facts = night.facts
    if facts.recorded_days < MIN_DAYS:
        return None
    if algorithm in facts.rejudging:  # #681
        record = night.records.get(prevalidation_job_name(algorithm))
        if record is not None and record.outcome in (OK, FAILED) and record.commit == night.commit:
            return None  # it ran on this code and left no promotion test: another deploy may
        return REJUDGE
    if algorithm not in facts.waiting:
        return None
    report = facts.reports.get(algorithm)
    if report is not None and report.judged:
        return None  # it judged the code and target the algorithm runs now: it would again
    record = night.records.get(prevalidation_job_name(algorithm))
    if record is None or record.outcome not in (OK, FAILED):
        return FRESH  # never ran, or a restart cut it
    if (
        report is not None
        and not report.current
        and report.made_at is not None
        and report.made_at >= record.started_at
    ):
        return FRESH  # the run left its report, and the code or the target has changed since
    ended = record.started_at + timedelta(seconds=record.seconds or 0.0)
    if report is not None and report.made_at is not None and report.made_at > ended:
        # A run started after it (by hand, e.g. a look on fewer days) left the latest report,
        # without a verdict on this code: ask again for one.
        return FRESH
    if record.commit == night.commit:
        return None  # it ran on this code without a verdict: another deploy may give one
    return AGAIN  # it crashed or ran out of time on older code


def prevalidation_tonight(night: Turn, order: Sequence[str]) -> str | None:
    """The algorithm pre-validated tonight in its turn, of ``order``: none if one was tonight
    already (whatever came of it), a changed code's too (#454, ``revision_tonight``); else the first
    REJUDGE (#681), else the first FRESH, else the first AGAIN."""
    if _ran_tonight(night, order):
        return None
    turns = [(name, prevalidation_turn(night, name)) for name in order]
    return next(iter(_in_turn(turns)), None)


def prevalidations_quiet(night: Turn, order: Sequence[str]) -> tuple[str, ...]:
    """The algorithms of ``order`` that may be pre-validated second on a quiet night (#546, ``Job.quiet``:
    every job of the night had its turn), in turn: none unless exactly one bar-close pre-validation
    ran tonight (a changed code's too) to its end (one a restart cut may have taken the engine down
    with it); those FRESH, else those AGAIN. The scheduler runs the first whose time limit still
    ends by 05:30."""
    if not _second(night, order):
        return ()
    return _in_turn([(name, prevalidation_turn(night, name)) for name in order])


def _in_turn(turns: Sequence[tuple[str, str | None]]) -> tuple[str, ...]:
    """Of each name's turn, those REJUDGE (#681), else those FRESH, else those AGAIN."""
    for turn in (REJUDGE, FRESH, AGAIN):
        if found := tuple(n for n, t in turns if t == turn):
            return found
    return ()


def screened_order(screened: Sequence[str], order: Sequence[str]) -> tuple[str, ...]:
    """``order`` (the registries' turn) as the latest queue screen set it (#599,
    ``PrevalidationFacts.queue``, the
    Engine API's ``queue``): those of it ``screened`` orders, in its order, then the others in
    ``order``'s (none screened: the registries' turn)."""
    ordered = [n for n in dict.fromkeys(screened) if n in order]
    return (*ordered, *(n for n in order if n not in ordered))


def _prevalidation_due(algorithm: str, order: tuple[str, ...]) -> Callable[[Turn], bool]:
    def due(night: Turn) -> bool:
        return prevalidation_tonight(night, screened_order(night.facts.queue, order)) == algorithm

    return due


def _prevalidation_quiet(algorithm: str, order: tuple[str, ...]) -> Callable[[Turn], bool]:
    def due(night: Turn) -> bool:
        return algorithm in prevalidations_quiet(night, screened_order(night.facts.queue, order))

    return due


def _ran_tonight(night: Turn, order: Sequence[str]) -> list[JobRecord]:
    """The bar-close pre-validations run tonight, whatever came of them: first ones (of ``order``)
    and changed code's (#454)."""
    first = [
        record
        for name in order
        if (record := night.records.get(prevalidation_job_name(name))) is not None and record.day == night.day
    ]
    revised = [
        record
        for name, record in night.records.items()
        if name.startswith(REVISION_JOB) and record.day == night.day
    ]
    return first + revised


def _second(night: Turn, order: Sequence[str]) -> bool:
    """Whether a quiet night may run a second bar-close pre-validation (#546): one ran tonight, to
    its end (ok or failed: not cut by a restart)."""
    ran = _ran_tonight(night, order)
    return len(ran) == PREVALIDATIONS_A_NIGHT - 1 and all(r.outcome in (OK, FAILED) for r in ran)


def prevalidation_jobs(names: Sequence[str]) -> tuple[Job[PrevalidationFacts], ...]:
    """A pre-validation job for each algorithm of ``names`` (the registries) registered after
    pre-validation existed, in ``prevalidation_order``."""
    order = prevalidation_order(names)
    return tuple(
        Job(
            name=prevalidation_job_name(name),
            due=_prevalidation_due(name, order),
            quiet=_prevalidation_quiet(name, order),
            facts=read_prevalidation_facts,
            command=("prevalidate", name),
            timeout=PREVALIDATION_TIMEOUTS.get(name, PREVALIDATION_TIMEOUT),
            targets=(Target(PREVALIDATION_ISSUE, note=_prevalidation_note(name)),),
            attach=(Attachment(f"리포트 state/{_report_of(name)}", _report_of(name)),),
            report=_report_of(name),
            queued=name,
            order=read_queue,
        )
        for name in order
    )


def _prevalidation_note(algorithm: str) -> str:
    """The note on an algorithm's pre-validation result: what passing and refusing do, and
    what to do with what it measured besides the checks (``_DIAGNOSTICS_NOTES``)."""
    return " ".join(n for n in (_PREVALIDATION_NOTE, _DIAGNOSTICS_NOTES.get(algorithm)) if n)


# The pre-validations of changed code (#454, ``revisions.py``): a running bar-close algorithm whose
# code changed is back on probation, and is pre-validated on the recorded days after the change only
# (``prevalidate <name> --revision``: from its first whole trading day), once ``MIN_DAYS`` of them are
# recorded. Every check, as a first pre-validation's. It shares the night's turns of the bar-close
# pre-validations (a run replays the same days beside the live engine: one in its turn, a second on
# a quiet night, #546) and comes first: its algorithm runs already, and its probation cannot make it
# active before it passed; the waiting ones run after it. In the registries' order among them (turing
# too: its exemption is its first revision's). Its verdict is not asked for again; a run that left
# none runs again after a deploy, after the ones not tried yet, as a first pre-validation does. Its
# result goes to #204.
REVISION_JOB = "prevalidate-revision-"


def revision_job_name(algorithm: str) -> str:
    return f"{REVISION_JOB}{algorithm}"


def revision_turn(night: Turn, algorithm: str) -> str | None:
    """Whether ``algorithm``'s changed code would be pre-validated tonight (#454): FRESH, AGAIN (its
    last run for this revision left no verdict, on older code: it waits for the FRESH ones), or None
    (not tonight: none waits, too few days after the change, or judged)."""
    revising = night.facts.revising.get(algorithm)
    if revising is None or revising.recorded_days < MIN_DAYS:
        return None
    report = revising.report
    if report is not None and report.judged:
        return None  # the engine keeps its verdict at its next bar close
    record = night.records.get(revision_job_name(algorithm))
    if record is None or record.outcome not in (OK, FAILED) or record.started_at < revising.boundary:
        return FRESH  # never ran for this revision, or a restart cut it
    if (
        report is not None
        and not report.current
        and report.made_at is not None
        and report.made_at >= record.started_at
    ):
        return FRESH  # the run left its report, and the target has changed since
    ended = record.started_at + timedelta(seconds=record.seconds or 0.0)
    if report is not None and report.made_at is not None and report.made_at > ended:
        return FRESH  # a look by hand since replaced its verdict
    if record.commit == night.commit:
        return None  # it ran on this code without a verdict: another deploy may give one
    return AGAIN


def revision_tonight(night: Turn, order: Sequence[str]) -> str | None:
    """The algorithm of ``order`` whose changed code is pre-validated tonight in its turn (#454):
    none if a bar-close pre-validation (a first one or a changed code's) ran tonight; else the first
    FRESH, else the first AGAIN."""
    if _ran_tonight(night, order):
        return None
    turns = [(name, revision_turn(night, name)) for name in order]
    return next((n for n, t in turns if t == FRESH), None) or next((n for n, t in turns if t == AGAIN), None)


def revisions_quiet(night: Turn, order: Sequence[str]) -> tuple[str, ...]:
    """The algorithms of ``order`` whose changed code may be pre-validated second on a quiet night
    (#546), in turn, as ``prevalidations_quiet``; listed before the first ones, they come first."""
    if not _second(night, order):
        return ()
    return _in_turn([(name, revision_turn(night, name)) for name in order])


def _revision_due(algorithm: str, order: tuple[str, ...]) -> Callable[[Turn], bool]:
    def due(night: Turn) -> bool:
        return revision_tonight(night, order) == algorithm

    return due


def _revision_quiet(algorithm: str, order: tuple[str, ...]) -> Callable[[Turn], bool]:
    def due(night: Turn) -> bool:
        return algorithm in revisions_quiet(night, order)

    return due


def revision_jobs(names: Sequence[str]) -> tuple[Job[PrevalidationFacts], ...]:
    """A job pre-validating the changed code of each bar-close algorithm of ``names`` (the
    registries' order), those registered before pre-validation too (#454)."""
    order = tuple(names)
    return tuple(
        Job(
            name=revision_job_name(name),
            due=_revision_due(name, order),
            quiet=_revision_quiet(name, order),
            facts=read_prevalidation_facts,
            command=("prevalidate", name, "--revision"),
            timeout=PREVALIDATION_TIMEOUTS.get(name, PREVALIDATION_TIMEOUT),
            targets=(Target(PREVALIDATION_ISSUE, note=_revision_note(name)),),
            attach=(Attachment(f"리포트 state/{_report_of(name)}", _report_of(name)),),
            report=_report_of(name),
        )
        for name in order
    )


_REVISION_NOTE = (
    "코드가 바뀐 돌고 있는 알고리즘의 사전 검증(#454): 코드가 바뀐 뒤 첫 온전한 거래일부터의 녹화일만 "
    "재생했다(검사는 처음 사전 검증과 같다). 거부(`refused`)되면 엔진이 다음 봉 마감에 은퇴시키고 다음 15:40 "
    "점검에 한 번 알린다. 통과(`passed`)만으로는 정식이 되지 않는다: 수습은 이어지고, 실시간 수습의 판정일 "
    "판정도 통과해야 정식이다. 같은 코드로는 다시 돌지 않는다."
)


def _revision_note(algorithm: str) -> str:
    return " ".join(n for n in (_REVISION_NOTE, _DIAGNOSTICS_NOTES.get(algorithm)) if n)


# The daily kinds' algorithms' pre-validations (#264): a walk-forward over ten years of KRX day
# candles (``daily_training``), not a replay of the recorded days, so neither the recorded days'
# count nor the one-a-night turn of the bar-close ones holds them back: each runs on the first
# night it waits without a verdict on the code and inputs it runs (and again once a deploy changes
# them, or after a run that left no verdict, on newer code). While the data of one of its
# features is behind the feature's version on this server (#297: KRX's net buys before NXT, until
# the keeper has stored them), it waits: a run then would be judged on the old data. Minutes, not
# hours; the hour is a bound. Before the bar-close pre-validations, which may take the rest of the
# night. Its result goes to #204, with them, and to its daily kind's issue (``DAILY_KIND_ISSUES``:
# open_to_close's ``DAILY_CHECK_ISSUE``, #204 too since #688, so once there; open_to_close_5d's #310,
# whose review days were fixed from the variance ratio its first pre-validation measured: a later
# algorithm's report measures it again).
DAILY_PREVALIDATION_TIMEOUT = timedelta(hours=1)
DAILY_KIND_ISSUES = {"open_to_close": DAILY_CHECK_ISSUE, "open_to_close_5d": 310}


def _daily_prevalidation_due(
    algorithm: str, features: Sequence[str] = (), kind: str | None = None
) -> Callable[[Turn], bool]:
    of_kind = daily_kind_named(kind) if kind is not None else None

    def due(night: Turn) -> bool:
        facts = night.facts
        if algorithm not in facts.waiting:
            return False
        if of_kind is not None and not of_kind.runs:
            return False  # its kind awaits a probe's result (#310): it is not pre-validated
        if set(features) & set(facts.pending_inputs):
            return False  # its data is not all in yet (#297): it would be judged on the old
        if kind in facts.pending_answers:
            return False  # its kind's latest answers are not all in (#264): fewer days than there are
        report = facts.reports.get(algorithm)
        if report is not None and report.judged:
            return False  # it judged the code the algorithm runs now
        record = night.records.get(prevalidation_job_name(algorithm))
        if record is None or record.outcome not in (OK, FAILED):
            return True  # never ran, or a restart cut it
        if report is not None and not report.current and report.made_at is not None:
            if report.made_at >= record.started_at:
                return True  # the run left its report, and the code has changed since
        return record.commit != night.commit  # it ran without a verdict: again on new code

    return due


def daily_prevalidation_jobs(
    names: Sequence[str],
    features: Mapping[str, Sequence[str]] | None = None,
    kinds: Mapping[str, str] | None = None,
) -> tuple[Job[PrevalidationFacts], ...]:
    """A pre-validation job for each daily algorithm of ``names`` (#264), waiting for its
    ``features``' data and its kind's answers (``kinds``: algorithm -> kind), posting to #204 and
    its kind's issue (``DAILY_KIND_ISSUES``)."""
    out = []
    for name in names:
        kind = (kinds or {}).get(name)
        note = _DAILY_PREVALIDATION_NOTES.get(kind or "", _DAILY_PREVALIDATION_NOTE)
        out.append(
            Job(
                name=prevalidation_job_name(name),
                due=_daily_prevalidation_due(name, (features or {}).get(name, ()), kind),
                facts=read_prevalidation_facts,
                command=("prevalidate", name),
                timeout=DAILY_PREVALIDATION_TIMEOUT,
                targets=tuple(
                    Target(issue, note=note)
                    for issue in dict.fromkeys(
                        (PREVALIDATION_ISSUE, DAILY_KIND_ISSUES.get(kind or "", DAILY_CHECK_ISSUE))
                    )
                ),
                attach=(Attachment(f"리포트 state/{_report_of(name)}", _report_of(name)),),
                report=_report_of(name),
            )
        )
    return tuple(out)


_DAILY_PREVALIDATION_NOTE = (
    "하루 한 번 예측 종류의 알고리즘(#264)의 사전 검증: KRX 일봉 10년의 walk-forward다. 통과(`passed`)하면 "
    "그 밤 `simulate-daily-298`이 수습에 올리고 최근 20거래일을 시뮬레이션해 바로 판정한다(#298, 판정은 "
    "20·40·60일째). 거부(`refused`)되면 같은 코드로는 다시 돌지 않는다."
)
_DAILY_PREVALIDATION_NOTES = {
    "open_to_close_5d": (
        "5거래일 창 `open_to_close_5d`(#310)의 알고리즘의 사전 검증: KRX 일봉 10년의 walk-forward다. 이 "
        "종류의 판정일은 `cox-5d`의 첫 사전 검증(2026-10-23 밤)이 잰 분산 배율(VR 2.220)로 규칙에 따라 "
        "60·120·180일로 고정했다. 통과(`passed`)하면 그 밤 `simulate-daily-298`이 수습에 올리고 끝난 최근 "
        "창 60개를 시뮬레이션해 바로 판정한다(#298). 리포트의 `walk_forward.overlap`이 이 실행의 VR과 규칙이 "
        "주는 판정일(`review_days`)을 다시 잰다. 출력의 `review days of open_to_close_5d` 줄이 고정한 "
        '판정일보다 길다고 하면 에이전트가 판정일을 다시 정하는 이슈를 연다(#310 "상태 판정"의 "다음 '
        '알고리즘"). 거부(`refused`)되면 같은 코드로는 다시 돌지 않는다.'
    ),
}


# The daily kinds' simulated probations (#298, ``simulation.py``): a daily algorithm starts on
# probation with its latest 20 trading days replayed (refitted before each, as its nights would),
# judged at once. The job runs right after the daily pre-validations: on the night one passes, it
# enrolls the algorithm on probation (the engine would at its next start) and simulates it, so it
# is active by the next 08:50 if it passes. It also runs on a night a daily probation waits for it
# (begun by the engine, or a run cut short). Once a night; a run that failed waits for new code (or
# a new pass), as a pre-validation that left no verdict does. Cox fits in seconds: 20 fits and ten
# years of samples are minutes; the hour is a bound. Its result goes to #298 and #264. It waits for
# the algorithms of a kind whose answers of the latest days are not all in on this server
# (``PrevalidationFacts.pending_answers``, #264: ``open_to_close``'s closing auction prices).
# It gives way to a bar-close pre-validation as the bar-close simulation does (below, #376). It never
# holds back the daily pre-validations: they run before it (a pass enrolls that night), take about a
# minute, are not one a night, and start by 04:30. Judged by its hour, it gives way only when it
# would start after 00:00 with another bar-close kind's algorithm due (last start 01:00), or after
# 01:30 with a direction one (02:30; #546: before, 22:30 and 23:30).
SIMULATION_ISSUE = 298
DAILY_SIMULATION_TIMEOUT = timedelta(hours=1)


def _a_prevalidation(job: Job[Any]) -> bool:
    """A pre-validation's job: the simulations give way to it (#376)."""
    return job.command[:1] == ("prevalidate",)


def _daily_simulation_due(
    algorithms: Sequence[DailyAlgorithm], kinds: Sequence[DailyKind] = DAILY_KINDS
) -> Callable[[Turn], bool]:
    held = {k.name for k in kinds if k.review_hold is not None}

    def due(night: Turn) -> bool:
        # Not for an algorithm whose kind's latest answers are not all in on this server (#264):
        # the days replayed would not be the latest; nor for one whose kind's review days are not
        # fixed yet (#310, ``DailyKind.review_hold``): it is not enrolled.
        names = [
            a.name for a in algorithms if a.kind not in night.facts.pending_answers and a.kind not in held
        ]
        if not names:
            return False
        record = night.record
        if record is not None and record.day == night.day:
            return False  # once a night
        passed = any(
            (r := night.records.get(prevalidation_job_name(name))) is not None
            and r.day == night.day
            and r.outcome == OK
            for name in names
        )
        if passed:
            return True  # a daily algorithm passed tonight: enrolled and simulated at once
        if not set(night.facts.simulating_daily) & set(names):
            return False
        return not (record is not None and record.outcome == FAILED and record.commit == night.commit)

    return due


DAILY_SIMULATION = Job(
    name="simulate-daily-298",
    due=_daily_simulation_due(DAILY_ALGORITHMS),
    facts=read_prevalidation_facts,
    command=("simulate", "daily"),
    timeout=DAILY_SIMULATION_TIMEOUT,
    gives_way_to=_a_prevalidation,
    targets=(
        Target(
            SIMULATION_ISSUE,
            note=(
                "하루 한 번 예측 종류의 수습 시뮬레이션(#298): 그 종류의 첫 판정일만큼의 최근 거래일"
                "(`open_to_close`는 20일, `open_to_close_5d`는 끝난 창 60개, #310)을 그 알고리즘이 수습으로 "
                "돌았다고 치고 다시 재생했다(날마다 그 전날까지로 다시 학습). 그 판정을 바로 했다. 통과하면 "
                "지금부터 정식이고, 못 하면 수습이 "
                "이어진다(다음 판정은 그 둘째 판정일). 출력에는 시세 값이 없다(날짜, 개수, lift만)."
            ),
        ),
        Target(
            DAILY_CHECK_ISSUE,
            note="수습 시뮬레이션(#298)의 결과. 자세한 것은 #298.",
        ),
    ),
)


# The bar-close kinds' simulated probations (#298, ``simulation.py``): the direction's, the range's
# and the barriers' algorithms start on probation with the latest recorded days replayed (10 to 20:
# as many as there are), each trained once before them, judged at once. Due on a night a bar-close
# probation waits for it (a pre-validation passed, the engine enrolled it at the next bar close; a
# new target's restart) once 10 days are recorded (``MIN_DAYS``: with fewer there is nothing to
# simulate and the probation goes on live). The algorithms waiting are replayed together, in one
# run: the replay of a day's events through the Market View, which they share, is most of the cost,
# and live the engine runs them all at each bar close anyway; one run a night also leaves the rest of
# the night to a pre-validation. It is listed before the pre-validations (#298: it can make
# algorithms already running official at once), but gives way to the night's pre-validation when,
# run first to its limit, it would carry that one past its last start (02:30, the other kinds' 01:00,
# #546): the pre-validation runs first and the simulation after it, that night if it can still start
# by 00:30, else the next (#376, the decision of 2026-10-26). From 22:30 (``KRX_JOBS_AT``) its limit
# fits before no pre-validation's 3 or 4½ hours, so on a night both are due the pre-validation goes
# first.
# Before, the simulation the night after each pass pushed the next pre-validation back a night (8
# waited on 2026-10-26), and an algorithm not pre-validated does not run at all, while a simulation
# put off only judges a night later: its probation's live days count on. After it gave way on two nights
# (``GIVE_WAY_NIGHTS``) it runs first. A run that failed waits for new code, as a pre-validation that
# left no verdict does. The limit is a pre-validation's: up to 20 days' replay (without its 20
# retrainings), plus the burst CPU running out; it starts by 00:30. Its result goes to #298 and
# #204.
SIMULATION_TIMEOUT = timedelta(hours=5)


def _bar_close_simulation_due(night: Turn) -> bool:
    facts = night.facts
    if facts.recorded_days < MIN_DAYS or not facts.simulating:
        return False
    record = night.record
    if record is None or record.outcome not in (OK, FAILED):
        return True  # never ran, or a restart cut it
    if record.day == night.day:
        return False  # once a night
    return not (record.outcome == FAILED and record.commit == night.commit)


BAR_CLOSE_SIMULATION = Job(
    name="simulate-bar-close-298",
    due=_bar_close_simulation_due,
    facts=read_prevalidation_facts,
    command=("simulate", "bar-close"),
    timeout=SIMULATION_TIMEOUT,
    gives_way_to=_a_prevalidation,
    targets=(
        Target(
            SIMULATION_ISSUE,
            note=(
                "4분(봉 마감) 종류의 수습 시뮬레이션(#298): 녹화된 최근 거래일(10–20일)을 그 알고리즘들이 "
                "수습으로 돌았다고 치고 다시 재생했다(첫날 전까지로 한 번 학습). 그 판정을 바로 했다. "
                "통과하면 지금부터 정식이고, 못 하면 수습이 이어진다(다음 판정일까지 실시간 날이 더해진다). "
                "출력에는 시세 값이 없다(날짜, 개수, lift만)."
            ),
        ),
        Target(PREVALIDATION_ISSUE, note="수습 시뮬레이션(#298)의 결과. 자세한 것은 #298."),
    ),
)


_PREVALIDATION_NOTE = (
    "검사 다섯을 넘으면 같은 walk-forward 재생으로 승격을 판정한다(#681, 리포트의 `promotion`): 수습 판정과 "
    "같은 통계(가장 약한 기준 대비 거래일 단위 lift의 하한 > 0)를 그 날들의 시험 예산으로 나눈 "
    "수준에서 본다. "
    "통과하면 그 밤 바로 정식(`active`)이고 엔진이 다음 봉 마감부터 돌린다. 못 하면 거부(`refused`)다. "
    "판정할 수 없으면(채점한 날 10일 미만, 그 날들의 예산 소진, 채점 부족) 실시간 날로 판정하는 수습"
    "(`forward`)이다. 이미 수습인 알고리즘을 다시 판정한 것이면 못 해도 수습에 남는다. 거부되면 같은 "
    "코드·정답 버전으로는 다시 돌지 않고, 고친 코드가 배포되면 다시 돈다."
)
# What an agent does with the diagnostics a candidate's report carries (#283,
# ``contract.Diagnostics``): measured besides the checks, never judged.
_DIAGNOSTICS_NOTES = {
    "neyman": (
        "출력의 `neyman diagnostics` 줄과 리포트의 `diagnostics`(#283)는 판정에 쓰지 않는 측정이다: "
        "1차의 상승·하락 예측이 2차 뒤 반대쪽으로 뒤집힌 수와 그 정밀도(뒤집히지 않은 예측의 정밀도와 함께), "
        "상승·하락 동률, 1차 확률의 표본 밖·표본 안·실시간 분포. 에이전트가 이 숫자를 #283에 요약한다. "
        "사용자가 그것으로 #283의 모델 변경 순서를 정한다."
    ),
}


def _report_of(algorithm: str) -> str:
    """The report's path under the state directory."""
    return report_path(Path(), algorithm).as_posix()


# The queue screen (#599, ``queue_screen.py``): each night a pre-validation would run, the bar-close
# algorithms waiting are screened by their walk-forward folds (minutes each, no replay) and the order
# of their pre-validations is set from it: a seat by score and a seat by arrival by turns, those passed
# over on three nights first. It orders only: every one waiting is still pre-validated, and judged by
# its checks. Due on a night at least two waiting have a turn (``prevalidation_turn``: the order of one
# is no question), once ``MIN_DAYS`` are recorded; once a night. Before the changed code's and the
# first pre-validations, which go by its order that night (``Job.refreshes``: the facts are read again
# after it). It gives way to a pre-validation as the simulations do (#376): run first to its limit, it
# would carry that one past its last start; it then runs after it, before the quiet night's second,
# which goes by tonight's order (the quiet turn reads the facts again), and that night's first goes by
# the order of the last screen. A screen more than four days old is not gone by (``QUEUE_KEPT_DAYS``).
# The limit: its budget for the candidates' processes (an hour; the issue's estimate was 36 to 54
# minutes for nine), one candidate's limit (10 minutes) and room. Its result goes to #204, which gets
# ``ready-for-agent`` only when the screen failed.
QUEUE_SCREEN_TIMEOUT = timedelta(hours=1, minutes=15)


def _queue_screen_due(order: tuple[str, ...]) -> Callable[[Turn], bool]:
    def due(night: Turn) -> bool:
        if night.facts.recorded_days < MIN_DAYS:
            return False
        record = night.record
        if record is not None and record.day == night.day and record.outcome in (OK, FAILED):
            return False  # once a night
        # Of the waiting ones only: one judged again (#681) is no candidate it orders.
        return sum(prevalidation_turn(night, name) in (FRESH, AGAIN) for name in order) >= 2

    return due


def _screen_failed(ran: Ran) -> bool:
    """The screen calls an agent only when it failed: its order is the night's routine."""
    return ran.exit_code != 0 or ran.timed_out


_QUEUE_SCREEN_NOTE = (
    "사전 검증 대기열 훑기(#599): 기다리는 봉 마감 알고리즘마다 사전 검증과 같은 날들을 walk-forward로 "
    "예측해(재생 없이) 기준 예측마다 거래일 단위 lift를 내고, 그 가운데 가장 약한 것을 점수로 그 밤부터의 "
    "사전 검증 순서를 정했다. 점수 자리와 먼저 온 자리가 번갈아 오고, 세 밤 밀린 알고리즘은 맨 앞이다. "
    "순서만 정한다: 기다리는 알고리즘은 모두 사전 검증을 받고, 통과·거부는 검사 다섯이 정한다. 점수를 "
    "모르는 알고리즘(시간·메모리 한도, 오류)은 먼저 온 자리를 지킨다. 밤마다 같은 날로 다시 훑은 점수는 "
    "서로 독립된 증거가 아니다. 출력에는 시세 값이 없다(lift, 개수, 날짜만)."
)

QUEUE_SCREEN = Job(
    name="screen-queue-599",
    due=_queue_screen_due(
        prevalidation_order([*(a.name for a in ALGORITHMS), *(a.name for a in KIND_ALGORITHMS)])
    ),
    facts=read_prevalidation_facts,
    command=("screen-queue",),
    timeout=QUEUE_SCREEN_TIMEOUT,
    targets=(Target(PREVALIDATION_ISSUE, note=_QUEUE_SCREEN_NOTE),),
    attach=(Attachment(f"대기열 state/{QUEUE}", QUEUE),),
    gives_way_to=_a_prevalidation,
    label_if=_screen_failed,
    refreshes=True,
)
