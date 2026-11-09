"""The scheduled jobs' machinery (#215; a module of its own since #500): what a job is (``Job``,
``Target``, ``Attachment``), what its condition is asked with (``Night``, and ``Facts``: read by
``read_facts`` at night and by ``session_facts`` in the session), the state file (``JobState``) and
the scheduler that runs the due jobs and posts their results (``Scheduler``). How they run is in the
package's docstring (``krx_quant.scheduled``); the jobs are in its family modules.

A condition that needs more of what the engine knows adds a field to ``Facts`` and reads it in
``read_facts`` (or ``session_facts``), here: every job's facts are read in one place, once a night,
and again after a job that says so (``Job.refreshes``, #599: the queue screen's order).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import signal
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from time import monotonic
from typing import Any, Protocol

from krx_quant.algorithms import Algorithm, KindAlgorithm
from krx_quant.clock import SessionClock
from krx_quant.config import JOBS_UNTIL, Settings
from krx_quant.daily import DailyAlgorithm
from krx_quant.daily_features import pending_inputs
from krx_quant.engine import PredictionEngine
from krx_quant.feeds.base import KST
from krx_quant.github import GitHub, GitHubError, comment_body
from krx_quant.holdout import days_in_window
from krx_quant.kind_registry import DAILY_KINDS
from krx_quant.market_calendar import MarketCalendar, first_whole_day
from krx_quant.neyman_backfill import Progress as NeymanBackfill
from krx_quant.neyman_backfill import progress as neyman_backfill_progress
from krx_quant.neyman_variants import Progress as NeymanVariants
from krx_quant.neyman_variants import progress as neyman_variants_progress
from krx_quant.night_book import complete_nights as night_book_nights
from krx_quant.nightly_hit_rates import nights_with_hit_rates
from krx_quant.predictions import DB_NAME, Revision
from krx_quant.prevalidation import LastReport, last_report, revision_report
from krx_quant.process_log import KRX_QUANT, Child, run_logged
from krx_quant.process_log import Ran as Ran  # a job's process, once it ended
from krx_quant.queue_screen import queue_order
from krx_quant.redaction import redact
from krx_quant.signal_review import fixed_days
from krx_quant.smt_experiment import days_in_holdout as smt_days_in_holdout
from krx_quant.state_backup import last_backup
from krx_quant.swap import NightTraining
from krx_quant.trader.mock_check import trader_in_mock
from krx_quant.trader.notice_columns import apart_orders
from krx_quant.training import finished_days, is_recorded

# The package's logger: the lines name ``krx_quant.scheduled``, as before the split (#500).
log = logging.getLogger("krx_quant.scheduled")

STATE_FILE = "scheduled.json"
OK, FAILED, INTERRUPTED, RUNNING = "ok", "failed", "interrupted", "running"
# A pre-validation's verdict (#440), as its report and its output say it.
PASSED, REFUSED = "passed", "refused"
READY_FOR_AGENT, READY_FOR_HUMAN, NEEDS_TRIAGE = "ready-for-agent", "ready-for-human", "needs-triage"
# A job's arguments follow ``KRX_QUANT``, as the nightly training's do.

# Which part of a job's output an issue gets: None if it is not in the output.
Part = Callable[[str], str | None]


def whole(output: str) -> str:
    return output


def sections(*titles: str) -> Part:
    """The paragraphs of the output (separated by a blank line, as ``diagnose`` prints its
    sections) whose first line starts with one of ``titles``, in the output's order; None
    unless each title starts one."""

    def part(output: str) -> str | None:
        found = [p.strip("\n") for p in output.split("\n\n") if p.strip("\n").startswith(titles)]
        firsts = [p.splitlines()[0] for p in found]
        if not all(any(first.startswith(t) for first in firsts) for t in titles):
            return None
        return "\n\n".join(found)

    return part


@dataclass(frozen=True)
class Target:
    """An issue a job's result goes to: which part of the output, and a line saying what it is."""

    issue: int
    part: Part = whole
    note: str = ""


@dataclass(frozen=True)
class Attachment:
    """A file a job writes, folded under its output (``<details>``) on each of its issues once
    it has ended, masked like the rest. Only if this run wrote it (it is new or changed since
    the start): else a line says it is not there."""

    title: str  # the fold's summary
    path: str  # under the state directory (the state file's), e.g. prevalidations/kalman.json


@dataclass
class JobRecord:
    """A job's last run."""

    day: date  # the night it ran on
    outcome: str  # OK, FAILED, INTERRUPTED, or RUNNING while it runs
    started_at: datetime
    commit: str  # the code that ran it
    exit_code: int | None = None  # None: it could not be started
    seconds: float | None = None
    timed_out: bool = False  # stopped at its time limit (then FAILED)
    comments: dict[int, str] = field(default_factory=dict)  # issue -> the result's comment
    # What the run judged (#440, ``Job.report``): PASSED (OK), REFUSED (FAILED: its exit code 1, as the
    # conditions have always read it), or None: a job without a verdict, or a run that left none.
    verdict: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "day": self.day.isoformat(),
            "outcome": self.outcome,
            "started_at": self.started_at.isoformat(),
            "commit": self.commit,
            "exit_code": self.exit_code,
            "seconds": self.seconds,
            "timed_out": self.timed_out,
            "comments": {str(issue): url for issue, url in self.comments.items()},
            "verdict": self.verdict,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> JobRecord:
        return cls(
            day=date.fromisoformat(data["day"]),
            outcome=str(data["outcome"]),
            started_at=datetime.fromisoformat(data["started_at"]),
            commit=str(data.get("commit", "unknown")),
            exit_code=None if data.get("exit_code") is None else int(data["exit_code"]),
            seconds=None if data.get("seconds") is None else float(data["seconds"]),
            timed_out=bool(data.get("timed_out", False)),
            comments={int(issue): str(url) for issue, url in data.get("comments", {}).items()},
            verdict=None if data.get("verdict") is None else str(data["verdict"]),  # none before #440
        )


@dataclass(eq=False)
class Unposted:
    """A result not on its issue yet: the comment (masked already), then the labels."""

    job: str
    day: date
    issue: int
    body: str
    label: tuple[str, ...]
    unlabel: tuple[str, ...]
    comment: str | None = None  # the comment's address once it is up; the labels may still wait

    def as_dict(self) -> dict[str, Any]:
        return {
            "job": self.job,
            "day": self.day.isoformat(),
            "issue": self.issue,
            "body": self.body,
            "label": list(self.label),
            "unlabel": list(self.unlabel),
            "comment": self.comment,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Unposted:
        return cls(
            job=str(data["job"]),
            day=date.fromisoformat(data["day"]),
            issue=int(data["issue"]),
            body=str(data["body"]),
            label=tuple(data.get("label", ())),
            unlabel=tuple(data.get("unlabel", ())),
            comment=data.get("comment"),
        )


class JobState:
    """``state/scheduled.json``: each job's last run, the nights a job gave way since it last ran
    (#376), and the results not posted yet."""

    def __init__(
        self,
        path: Path,
        jobs: dict[str, JobRecord] | None = None,
        unposted: list[Unposted] | None = None,
        gave_way: dict[str, list[date]] | None = None,
    ) -> None:
        self.path = path
        self.jobs = jobs or {}
        self.unposted = unposted or []
        # job -> the nights it gave way (``Job.gives_way_to``) and did not run, since it last ran
        # and while it stayed due: the starvation guard counts them (``GIVE_WAY_NIGHTS``).
        self.gave_way = gave_way or {}

    @classmethod
    def load(cls, path: Path) -> JobState:
        """The state as saved; empty if there is none. One that cannot be read is set aside
        (``.bad``) and the jobs start afresh: a job may then run once more."""
        try:
            data = json.loads(path.read_text())
            return cls(
                path,
                {name: JobRecord.from_dict(r) for name, r in data.get("jobs", {}).items()},
                [Unposted.from_dict(u) for u in data.get("unposted", [])],
                {
                    name: [date.fromisoformat(d) for d in nights]
                    for name, nights in data.get("gave_way", {}).items()
                },
            )
        except FileNotFoundError:
            return cls(path)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            log.error("cannot read %s (%s): set aside, the scheduled jobs start afresh", path, e)
            with contextlib.suppress(OSError):
                path.replace(path.with_name(path.name + ".bad"))
            return cls(path)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "jobs": {name: record.as_dict() for name, record in self.jobs.items()},
            "unposted": [u.as_dict() for u in self.unposted],
            "gave_way": {name: [d.isoformat() for d in nights] for name, nights in self.gave_way.items()},
        }
        tmp = self.path.with_name(f".{self.path.name}.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        tmp.replace(self.path)


@dataclass(frozen=True)
class Revising:
    """A running bar-close algorithm's changed code, waiting for its pre-validation (#454)."""

    boundary: datetime  # when the engine first ran it
    since: date  # its first whole trading day: the days it is judged on start there
    recorded_days: int  # the recorded days from ``since``, as ``prevalidate --revision`` counts them
    report: LastReport | None  # its latest report if made for this revision; None without one


@dataclass(frozen=True)
class Facts:
    """What the engine knows that a condition may ask, read once a night."""

    recorded_days: int = 0  # recorded days, as ``prevalidate`` counts them (MIN_DAYS, #204)
    waiting: tuple[str, ...] = ()  # the algorithms waiting for their pre-validation (#204)
    official: int = 0  # the consensus members: active algorithms on the engine's target (#206)
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
    # Whether the trader's KIS key, secret and account are set (``Settings.trader_kis``, #403): the
    # order probe's session job waited for them once it had said they were not (it left ``JOBS``,
    # #484); kept for a session job that orders on the mock account.
    trader_kis: bool = False
    # Whether the trader's live key, secret and account are set (``Settings.trader_live_kis``, #408):
    # the live account's read waits for them.
    trader_live_kis: bool = False
    # Whether the trader last started in mock mode (#405, ``mock_check.trader_in_mock``): it may hold
    # 005930 and order on the mock account in the session, so the order probe's session job did not
    # run then (it left ``JOBS``, #484); kept for a session job that orders on the mock account.
    trader_mock: bool = False
    # How many of the latest nights' promotion lists carry the hit rates beside the swap (#437,
    # ``nightly_hit_rates.nights_with_hit_rates``, up to ``HIT_RATE_NIGHTS``): their summary waits for
    # them.
    hit_rate_nights: int = 0
    # How many days of turing's holdout window are in the engine DB (#447, ``holdout.days_in_window``,
    # up to ``holdout.DAYS``): its judging waits for the window to be full.
    holdout_days: int = 0
    # Where neyman's variants experiment stands (#455, ``neyman_variants.progress``, read from its
    # report and the day folders): its window's N once discovered, whether it was judged, and the
    # recorded days after 2026-11-02 that can be in the window.
    neyman_variants: NeymanVariants = field(default_factory=NeymanVariants)
    # Whether the backfill's verdict on neyman's variants is in (#532, ``neyman_backfill.progress``,
    # read from its report): it goes on by day across nights until it is (#643).
    neyman_backfill: NeymanBackfill = field(default_factory=NeymanBackfill)
    # How many of the first ``SIGNAL_REVIEW_FIXED_DAYS`` days with predictions from
    # ``SIGNAL_REVIEW_FROM`` are in the engine DB (#206, ``signal_review.fixed_days``): the signal
    # review's fixed window waits for them all.
    signal_review_days: int = 0
    # How many days of the SMT experiment's holdout are in (#523, ``smt_experiment.days_in_holdout``, up to
    # ``smt_experiment.DAYS``, read from the day folders and the engine DB read-only): its verdict waits
    # for them all.
    smt_holdout_days: int = 0
    # The day of the last backup of the server's state this server made (#576, ``state_backup.MARK``):
    # the restore drill waits for one.
    state_backup: date | None = None
    # How many complete nights the night futures' book's measure found (#605, ``night_book.complete_nights``,
    # read from its state file): the measure stops at ``night_book.NIGHTS``.
    night_book_nights: int = 0
    # The order of the bar-close pre-validations the latest queue screen set (#599,
    # ``queue_screen.queue_order``): none without a screen of the last few nights.
    queue: tuple[str, ...] = ()
    # How many of the mock trader's orders of more than one share that something filled of are on
    # the days the fill notice recorder kept (#648, ``notice_columns.apart_orders``): telling the
    # notice's columns 10 and 17 apart waits for ``notice_columns.APART`` of them.
    fill_notice_apart: int = 0


@dataclass(frozen=True)
class Night:
    """What a job's condition is asked with."""

    day: date  # the night's day: from KRX_JOBS_AT on it to 05:30 the next morning; a session job's day
    trading_day: bool
    commit: str  # the code running
    record: JobRecord | None  # this job's last run; None if it never ran
    records: Mapping[str, JobRecord]  # every job's last run
    facts: Facts

    @property
    def ran(self) -> bool:
        """This job has run to its end (ok or failed); a run cut by a restart has not."""
        return self.record is not None and self.record.outcome in (OK, FAILED)


@dataclass(frozen=True)
class Job:
    """A scheduled job: when it runs (``due``), what (``krx-quant`` arguments) for how long
    at most, and where its result goes."""

    name: str  # unique: the job's key in the state file
    due: Callable[[Night], bool]
    command: tuple[str, ...]
    timeout: timedelta
    targets: tuple[Target, ...]
    label: tuple[str, ...] = (READY_FOR_AGENT,)  # set on each issue once the comment is up
    unlabel: tuple[str, ...] = (READY_FOR_HUMAN, NEEDS_TRIAGE)  # taken off then
    attach: tuple[Attachment, ...] = ()  # files it writes, posted with its output
    # The later jobs this one gives way to (#376): None, or which of them. See ``Scheduler``.
    gives_way_to: Callable[[Job], bool] | None = None
    # A session job (#403): the time (KST) it runs from on a trading day, not at night; its time
    # limit must end by ``SESSION_UNTIL``. None: a night's job.
    session: time | None = None
    # Whether a run calls an agent (#402): the labels change (``label``, ``unlabel``) only on the
    # runs it says so of; None: on every run.
    label_if: Callable[[Ran], bool] | None = None
    # The report whose verdict the run's exit code gives (#440, a pre-validation's): a path under the
    # state directory. Exit 0 passes; exit 1 refuses only if the run wrote it whole, made since it
    # started (``_refused_since``): else it is an error, which exits 1 too. None: no verdict.
    report: str | None = None
    # Its condition on a quiet night (#546, a second pre-validation's): asked again once every job of
    # the night had its turn, in the list's order; one due then runs if its time limit still ends by
    # 05:30. None: asked once a night, in its turn.
    quiet: Callable[[Night], bool] | None = None
    # Whether the night's facts are read again once it ran (#599: the queue screen sets the order the
    # pre-validations after it go by). False: the later jobs ask the facts read before it.
    refreshes: bool = False
    # The algorithm whose first pre-validation it runs (#599): the quiet night's turn asks those jobs,
    # among their places in the list, in the queue screen's order (``Facts.queue``). None: in the list's.
    queued: str | None = None
    # A night's job that goes on by day across nights (#643): the time one of its days takes at most.
    # It starts when that still ends by 05:30, not its whole limit; its command is told the night's end
    # (``--until``) and starts no day it could not end by then, keeps each day it ended, and the next
    # night's run goes on from there (its condition asks it again while days are left). It is stopped
    # at 05:30 at the latest (its limit at most). None: it starts only when its whole limit ends by 05:30.
    resumes: timedelta | None = None

    @property
    def needs(self) -> timedelta:
        """What must still fit before 05:30 for it to start: a day (``resumes``), else its limit."""
        return self.resumes if self.resumes is not None else self.timeout


# A job that gave way on this many nights since it last ran gives way no more: it runs first (#376).
GIVE_WAY_NIGHTS = 2


def in_queue(jobs: Sequence[Job], queue: Sequence[str]) -> list[Job]:
    """``jobs`` in their order, but those queued (``Job.queued``, #599) in ``queue``'s order among their
    own places; one ``queue`` does not name after those it does, in its place's order."""
    places = [i for i, job in enumerate(jobs) if job.queued is not None]
    rank = {name: i for i, name in enumerate(queue)}
    queued = sorted((jobs[i] for i in places), key=lambda job: rank.get(job.queued or "", len(rank)))
    out = list(jobs)
    for i, job in zip(places, queued, strict=True):
        out[i] = job
    return out


class Runner(Protocol):
    """Runs a job's ``krx-quant`` arguments within its time limit, its output also to Loki as
    ``child`` labels it (#358)."""

    def __call__(
        self, args: Sequence[str], timeout: timedelta, child: Child | None = None
    ) -> Awaitable[Ran]: ...


Notify = Callable[[str], Awaitable[bool]]


async def run_command(
    args: Sequence[str],
    timeout: timedelta,
    child: Child | None = None,
    *,
    program: Sequence[str] = KRX_QUANT,
) -> Ran:
    """Run ``program`` with ``args`` and read its output, each line also to Loki as ``child``;
    killed at ``timeout`` (what it wrote by then is kept), and when the collector stops
    (cancelled)."""
    return await run_logged([*program, *args], timeout, child=child)


def window(day: date, at: time) -> tuple[datetime, datetime]:
    """The night of ``day``: from ``at`` (on ``day``, or after midnight the next day) to 05:30
    the next morning."""
    start = day if at >= JOBS_UNTIL else day + timedelta(days=1)
    return datetime.combine(start, at, KST), datetime.combine(day + timedelta(days=1), JOBS_UNTIL, KST)


# A session job ends by this time on its trading day (#403): before the trader's last exit (every
# position is closed by 15:15, #393) and the closing auction from 15:20.
SESSION_UNTIL = time(15, 15)


def session_window(job: Job, day: date) -> tuple[datetime, datetime]:
    """A session job's window on ``day``: from its time to ``SESSION_UNTIL``."""
    assert job.session is not None
    return datetime.combine(day, job.session, KST), datetime.combine(day, SESSION_UNTIL, KST)


def first_night(at: time, now: datetime) -> date:
    """The first night a scheduler started at ``now`` runs: one under way (a restart at
    night) at once, else the next."""
    today = now.astimezone(KST).date()
    for day in (today - timedelta(days=1), today):
        if now < window(day, at)[1]:
            return day
    return today + timedelta(days=1)


class Scheduler:
    """Runs the due jobs every night and posts their results.

    The jobs run in the list's order, but for one that gives way (``Job.gives_way_to``, #376):
    when it could start, it gives way to the first later job of those that is due and could start
    now but, were this one to run first, could not by its last start. Whether it could is judged by
    this one's time limit, not by how long it last took: its run's length depends on the recorded
    days and the algorithms replayed, which grow from one run to the next (the bar-close simulation
    took 1 h 24 min on 2026-10-23 and 2 h 22 min on 2026-10-25). It then runs after that job, that
    night if it can still start by its own last start, else the next night. Once a night; and once it
    gave way on ``GIVE_WAY_NIGHTS`` nights since it last ran (while it stayed due) it runs first.
    Once every job had its turn, those with a condition for a quiet night (``Job.quiet``, #546: a
    second pre-validation) are asked it, and one due runs if it can still end by 05:30. The facts are
    read when the first job's turn comes, and again after a job that says so (``Job.refreshes``, #599).
    A job that goes on by day across nights (``Job.resumes``, #643) starts when a day of it still ends
    by 05:30, is told that end (``--until``) and is stopped there at the latest.

    The session jobs (``Job.session``, #403) run apart from the nights: on each trading day, in the
    list's order, each from its time if it can still end by ``SESSION_UNTIL``, asked its condition
    with ``session_facts``. A collector started later in the session runs a job due at once; one
    started after it waits for the next trading day."""

    def __init__(
        self,
        jobs: Sequence[Job],
        at: time,
        calendar: MarketCalendar,
        state: JobState,
        github: GitHub,
        notify: Notify,
        clock: SessionClock,
        *,
        commit: str = "unknown",
        secrets: Sequence[str] = (),
        training: NightTraining | None = None,
        facts: Callable[[], Awaitable[Facts]] | None = None,
        session_facts: Callable[[], Awaitable[Facts]] | None = None,
        runner: Runner = run_command,
        ran: Callable[[str, JobRecord], None] | None = None,
    ) -> None:
        self._jobs = jobs
        self._at = at
        self._calendar = calendar
        self._state = state
        self._github = github
        self._notify = notify
        self._clock = clock
        self._commit = commit
        self._secrets = tuple(secrets)
        self._training = training or NightTraining()
        self._facts = facts
        self._session_facts = session_facts
        self._runner = runner
        # Each run's end, a cut one's too, as it is kept (#614): the readiness line keeps them until a
        # line that told them went, as the state keeps a job's last run alone.
        self._ran = ran or (lambda name, record: None)

    @property
    def _night_jobs(self) -> list[Job]:
        return [j for j in self._jobs if j.session is None]

    @property
    def _session_jobs(self) -> list[Job]:
        return [j for j in self._jobs if j.session is not None]

    async def run(self) -> None:
        """Every night's jobs and every trading day's session jobs, until cancelled; first what the
        last collector left."""
        await self.recover()
        await asyncio.gather(self._nights(), self._sessions())

    async def _nights(self) -> None:
        day = first_night(self._at, self._clock.now())
        while True:
            start, end = window(day, self._at)
            await self._clock.sleep_until(start)
            if self._clock.now() < end:
                try:
                    await self.run_night(day)
                except Exception:
                    log.exception("the scheduled jobs of the night of %s failed", day)
            day += timedelta(days=1)

    async def _sessions(self) -> None:
        """Each trading day's session jobs, from today's on."""
        if not self._session_jobs:
            return
        day = self._clock.now().astimezone(KST).date()
        while True:
            if self._calendar.is_trading_day(day):
                try:
                    await self.run_session(day)
                except Exception:
                    log.exception("the scheduled session jobs of %s failed", day)
            day += timedelta(days=1)
            await self._clock.sleep_until(datetime.combine(day, time(0), KST))

    async def run_session(self, day: date) -> None:
        """The session jobs of the trading day ``day``, each from its time on, in the list's order:
        one due runs if it can end by ``SESSION_UNTIL``, else it waits for the next trading day.
        The results earlier runs could not post wait for the next night or start, as the night's do:
        posted from here too, at midnight, they could go up twice beside a night's job posting them."""
        for job in self._session_jobs:
            start, end = session_window(job, day)
            await self._clock.sleep_until(start)
            if self._clock.now() >= end or self._cut(job, day):
                continue  # the session is over, or a restart cut its run today: the next trading day
            due = self._due(job, day, await self._read_session_facts())
            if not due:
                if due is False:
                    self._waits_no_more(job)
                continue
            if self._clock.now() > end - job.timeout:
                await self._notify(
                    f"예약 작업 {job.name}({day} 장중): 지금 시작하면 제한 시간({_duration(job.timeout)})이 "
                    f"{SESSION_UNTIL:%H:%M}을 넘어 다음 거래일로 미룹니다."
                )
                continue
            await self._run(job, day)

    async def _read_session_facts(self) -> Facts:
        if self._session_facts is None:
            return Facts()
        try:
            return await self._session_facts()
        except Exception:
            log.exception("could not read what the scheduled session jobs' conditions ask")
            return Facts()

    def _job_named(self, name: str) -> Job | None:
        return next((j for j in self._jobs if j.name == name), None)

    async def recover(self) -> None:
        """A run the last collector left going was cut (a deploy, a restart, a crash): it runs
        again the next night. Results not posted yet are posted."""
        for name, record in self._state.jobs.items():
            if record.outcome == RUNNING:
                record.outcome = INTERRUPTED
                self._state.save()
                self._ran(name, record)
                log.warning("scheduled job %s was cut on the night of %s: it runs again", name, record.day)
                job = self._job_named(name)
                if job is not None and job.session is not None:
                    line = (
                        f"예약 작업 {name}({record.day} 장중)이 엔진 재시작으로 끊겼습니다. "
                        "다음 거래일 장중에 다시 돕니다."
                    )
                else:
                    line = (
                        f"예약 작업 {name}({record.day} 밤)이 엔진 재시작으로 끊겼습니다. "
                        "다음 밤에 다시 돕니다."
                    )
                await self._notify(line)
        await self._post_unposted()

    async def run_night(self, day: date) -> None:
        """The night of ``day``: the results earlier nights could not post, then each due job
        in turn, once the night's training is over; a job that gives way after the job it gives
        way to; then the quiet night's turn (``_quiet_turn``, #546)."""
        _, end = window(day, self._at)
        await self._post_unposted()
        facts: Facts | None = None
        queue = self._night_jobs
        gave_way: set[str] = set()  # the jobs that gave way tonight: once a night each
        while queue:
            job = queue.pop(0)
            if self._cut(job, day):
                continue  # cut by a restart tonight: the next night runs it
            if facts is None:
                facts = await self._read_facts()
            due = self._due(job, day, facts)
            if not due:
                if due is False:
                    self._waits_no_more(job)
                continue
            latest = end - job.needs  # the last start that ends by 05:30 (a day of one that resumes)
            if not await self._training_done(latest):
                await self._notify(
                    f"예약 작업 {job.name}({day} 밤): 밤 학습이 {latest:%H:%M}까지 끝나지 않아 "
                    "다음 밤으로 미룹니다."
                )
            elif self._clock.now() > latest:
                if job.resumes is not None:
                    limit = f"하루치({_duration(job.resumes)})도"
                else:
                    limit = f"제한 시간({_duration(job.timeout)})이"
                await self._notify(
                    f"예약 작업 {job.name}({day} 밤): 지금 시작하면 {limit} "
                    f"{JOBS_UNTIL:%H:%M}을 넘어 다음 밤으로 미룹니다."
                )
            elif job.name not in gave_way and (ahead := await self._gives_way(job, queue, day, facts)):
                gave_way.add(job.name)
                at = queue.index(ahead) + 1
                while at < len(queue) and queue[at].name in gave_way:
                    at += 1  # after those that gave way to it before: the list's order among them
                queue.insert(at, job)
            else:
                await self._run(job, day)
                if job.refreshes:
                    facts = None  # read again for the next job (#599)
        await self._quiet_turn(day)

    async def _quiet_turn(self, day: date) -> None:
        """The night of ``day`` once every job had its turn (#546): the jobs with a condition for a
        quiet night (``Job.quiet``) are asked it, in the list's order (those queued in the queue
        screen's, #599: ``in_queue``), on the facts read again (a run tonight may have changed them);
        one due runs if its time limit still ends by 05:30. One that would not is only logged: it had
        its turn, and waits for the next night's."""
        asked = [j for j in self._night_jobs if j.quiet is not None and not self._cut(j, day)]
        if not asked:
            return
        _, end = window(day, self._at)
        facts = await self._read_facts()
        for job in in_queue(asked, facts.queue):
            if not self._due(job, day, facts, quiet=True):
                continue
            latest = end - job.needs
            if self._clock.now() > latest or not await self._training_done(latest):
                log.info(
                    "scheduled job %s: due on the quiet night of %s, but its time limit (%s) would pass %s",
                    job.name,
                    day,
                    job.timeout,
                    f"{JOBS_UNTIL:%H:%M}",
                )
                continue
            await self._run(job, day)

    def _cut(self, job: Job, day: date) -> bool:
        """Whether a restart cut ``job``'s run on the night of ``day``."""
        record = self._state.jobs.get(job.name)
        return record is not None and record.outcome == INTERRUPTED and record.day == day

    def _due(self, job: Job, day: date, facts: Facts, *, quiet: bool = False) -> bool | None:
        """Whether ``job`` is due on the night of ``day``, asked now (``quiet``: by its condition on a
        quiet night, ``Job.quiet``); None if its condition failed."""
        record = self._state.jobs.get(job.name)
        night = Night(
            day, self._calendar.is_trading_day(day), self._commit, record, dict(self._state.jobs), facts
        )
        condition = job.quiet if quiet and job.quiet is not None else job.due
        try:
            return condition(night)
        except Exception:
            log.exception("the condition of the scheduled job %s failed", job.name)
            return None

    def _waits_no_more(self, job: Job) -> None:
        """``job`` is not due: the nights it gave way while it waited count no more (#376)."""
        if self._state.gave_way.pop(job.name, None) is not None:
            self._state.save()

    async def _gives_way(self, job: Job, queue: Sequence[Job], day: date, facts: Facts) -> Job | None:
        """The job of ``queue`` (the rest of the night's, in turn) that ``job`` gives way to tonight
        (#376): the first it gives way to that is due and could start now, but not once ``job`` ran
        to its time limit. None if there is none (both fit), or if ``job`` gave way on
        ``GIVE_WAY_NIGHTS`` nights already: it runs first then."""
        if job.gives_way_to is None:
            return None
        _, end = window(day, self._at)
        now = self._clock.now()
        ended = now + job.timeout  # at the latest: its limit, not how long it last took
        ahead = next(
            (
                later
                for later in queue
                if job.gives_way_to(later)
                and now <= end - later.needs < ended
                and not self._cut(later, day)
                and self._due(later, day, facts)
            ),
            None,
        )
        if ahead is None:
            return None
        nights = [n for n in self._state.gave_way.get(job.name, []) if n != day]
        last_start = end - ahead.needs
        if len(nights) >= GIVE_WAY_NIGHTS:
            log.info(
                "scheduled job %s gave way on %d nights since it last ran (%s): it runs before %s",
                job.name,
                len(nights),
                ", ".join(n.isoformat() for n in nights),
                ahead.name,
            )
            await self._notify(
                f"예약 작업 {job.name}({day} 밤): 사전 검증에 이미 {len(nights)}밤 자리를 내줘 이번에는 "
                f"먼저 돕니다. {ahead.name}은 마지막 시작 시각({last_start:%H:%M})까지 시작하지 못하면 "
                "다음 밤으로 미뤄집니다."
            )
            return None
        self._state.gave_way[job.name] = [*nights, day]
        self._state.save()
        log.info(
            "scheduled job %s gives way to %s: run first to its limit (%s), it would carry it past its "
            "last start, %s",
            job.name,
            ahead.name,
            job.timeout,
            last_start.strftime("%H:%M"),
        )
        await self._notify(
            f"예약 작업 {job.name}({day} 밤): 먼저 돌아 제한 시간({_duration(job.timeout)})을 다 쓰면 "
            f"{ahead.name}의 마지막 시작 시각({last_start:%H:%M})을 넘기므로 그 뒤로 미룹니다. "
            f"그 뒤 {end - job.timeout:%H:%M}까지 시작할 수 있으면 그 밤에, 아니면 다음 밤에 돕니다. "
            f"자리를 내준 밤은 {len(nights) + 1}/{GIVE_WAY_NIGHTS}이고, {GIVE_WAY_NIGHTS}밤을 내준 뒤에는 "
            "먼저 돕니다."
        )
        return ahead

    async def _read_facts(self) -> Facts:
        if self._facts is None:
            return Facts()
        try:
            return await self._facts()
        except Exception:
            log.exception("could not read what the scheduled jobs' conditions ask")
            return Facts()

    async def _training_done(self, latest: datetime) -> bool:
        """Wait while the night's training runs or is due; whether it was over by ``latest``."""
        while (until := self._training.busy_until(self._clock.now())) is not None:
            if self._clock.now() >= latest:
                return False
            log.info("scheduled jobs wait for the nightly training (until %s at most)", min(until, latest))
            changed = asyncio.ensure_future(self._training.changed())
            timer = asyncio.ensure_future(self._clock.sleep_until(min(until, latest)))
            try:
                await asyncio.wait({changed, timer}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                changed.cancel()
                timer.cancel()
        return True

    async def _run(self, job: Job, day: date) -> None:
        started = self._clock.now()
        # Left RUNNING if the collector stops meanwhile: the next start calls it interrupted.
        self._state.jobs[job.name] = JobRecord(day, RUNNING, started, self._commit)
        self._state.gave_way.pop(job.name, None)  # it runs: the nights it gave way count no more
        self._state.save()
        before = {a.path: _stamp(self._files / a.path) for a in job.attach}
        args, limit = job.command, job.timeout
        if job.resumes is not None and job.session is None:
            # By day across nights (#643): told the night's end, and stopped there at the latest.
            _, end = window(day, self._at)
            args = (*job.command, "--until", end.isoformat())
            limit = max(min(job.timeout, end - started), timedelta(0))
        log.info("scheduled job %s: krx-quant %s", job.name, " ".join(args))
        began = monotonic()
        try:
            ran = await self._runner(
                args,
                limit,
                Child.job(job.name, day),
            )
        except Exception as e:
            log.exception("could not run the scheduled job %s", job.name)
            ran = Ran(None, "", f"could not be started: {type(e).__name__}: {e}")
        seconds = round(monotonic() - began, 1)
        outcome = OK if ran.exit_code == 0 and not ran.timed_out else FAILED
        record = JobRecord(
            day,
            outcome,
            started,
            self._commit,
            ran.exit_code,
            seconds,
            ran.timed_out,
            verdict=self._verdict(job, ran, started),
        )
        self._state.jobs[job.name] = record
        self._ran(job.name, record)
        log.info(
            "scheduled job %s: %s (exit %s) in %.1fs",
            job.name,
            record.verdict or outcome,
            ran.exit_code,
            seconds,
        )
        attached, absent = self._attachments(job, before)
        results = [self._result(job, target, record, ran, attached, absent) for target in job.targets]
        self._state.unposted.extend(results)
        self._state.save()
        links, why_not = await self._post(results)
        await self._notify(_outcome_line(job, record, links, why_not))

    @property
    def _files(self) -> Path:
        """Where the jobs' attachments are: the state directory, the state file's."""
        return self._state.path.parent

    def _verdict(self, job: Job, ran: Ran, started: datetime) -> str | None:
        """What ``job``'s run ``ran``, started at ``started``, judged (#440): PASSED on exit 0, REFUSED
        on exit 1 with a refusal of its own in ``Job.report``; None for a job without a report, and
        for a run that judged nothing (it raised, ran out of time, was killed, or wrote no report)."""
        if job.report is None or ran.timed_out:
            return None
        if ran.exit_code == 0:
            return PASSED
        if ran.exit_code == 1 and _refused_since(self._files / job.report, started):
            return REFUSED
        return None

    def _attachments(
        self, job: Job, before: Mapping[str, tuple[int, int, int] | None]
    ) -> tuple[list[tuple[str, str]], list[str]]:
        """The files of ``job`` this run wrote, each (title, masked text); and a line for each
        it did not (one left by an earlier run would pass for this run's)."""
        attached: list[tuple[str, str]] = []
        absent: list[str] = []
        for attachment in job.attach:
            path = self._files / attachment.path
            stamp = _stamp(path)
            if stamp is None or stamp == before.get(attachment.path):
                absent.append(f"`state/{attachment.path}`은 이번 실행이 쓰지 않아 붙이지 않는다.")
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                absent.append(f"`state/{attachment.path}`을 읽지 못해 붙이지 않는다({type(e).__name__}).")
                continue
            attached.append((attachment.title, redact(text, self._secrets)))
        return attached, absent

    def _result(
        self,
        job: Job,
        target: Target,
        record: JobRecord,
        ran: Ran,
        attached: Sequence[tuple[str, str]] = (),
        absent: Sequence[str] = (),
    ) -> Unposted:
        notes = [f"다시 돌리기: `docker compose run --rm engine {' '.join(job.command)}`"]
        part = target.part(ran.stdout)
        if part is None:
            part = ran.stdout
            notes.append("출력에서 이 이슈에 올릴 부분을 찾지 못해 출력 전체를 싣는다.")
        elif target.note:
            notes.append(target.note)
        if ran.timed_out:
            notes.append(f"제한 시간 {_duration(job.timeout)}을 넘어 멈췄다. 그때까지의 출력이다.")
        notes.extend(absent)
        details = list(attached)
        if ran.stderr.strip():
            details.append(("stderr", redact(ran.stderr, self._secrets)))
        body = comment_body(_heading(job, record), redact(part, self._secrets), notes=notes, details=details)
        label, unlabel = (job.label, job.unlabel) if _calls(job, ran) else ((), ())
        return Unposted(job.name, record.day, target.issue, body, label, unlabel)

    async def _post(self, results: Sequence[Unposted]) -> tuple[list[str], str | None]:
        """Post ``results`` (the comment, then the labels): the comments' addresses, and why
        some are not up (they stay in the state)."""
        if not self._github.configured:
            return [], "KRX_GITHUB_TOKEN이 없음"
        links: list[str] = []
        failure: str | None = None
        for result in results:
            try:
                url = result.comment or await self._github.comment(result.issue, result.body)
                if result.comment is None:
                    result.comment = url
                    self._state.save()  # not posted twice if the labels fail
                await self._github.relabel(result.issue, result.label, result.unlabel)
            except GitHubError as e:
                log.error("could not post %s's result on #%s: %s", result.job, result.issue, e)
                failure = failure or redact(str(e), self._secrets)
                continue
            self._state.unposted = [u for u in self._state.unposted if u is not result]
            record = self._state.jobs.get(result.job)
            if record is not None and record.day == result.day:
                record.comments[result.issue] = url
            self._state.save()
            links.append(url)
        return links, failure

    async def _post_unposted(self) -> None:
        """Post what earlier nights could not, and say what went up."""
        if not self._state.unposted or not self._github.configured:
            return
        waiting = list(self._state.unposted)
        await self._post(waiting)
        left = {id(u) for u in self._state.unposted}
        posted: dict[tuple[str, date], list[str]] = {}
        for result in waiting:
            if id(result) not in left and result.comment is not None:
                posted.setdefault((result.job, result.day), []).append(result.comment)
        for (name, day), links in posted.items():
            job = self._job_named(name)
            when = _when(job) if job is not None else "밤"
            line = f"예약 작업 {name}({day} {when})의 결과를 이제 올렸습니다. 댓글: {' '.join(links)}"
            await self._notify(line)


def _calls(job: Job, ran: Ran) -> bool:
    """Whether ``job``'s run ``ran`` changes its issues' labels (``Job.label_if``); a condition that
    fails says yes: someone looks."""
    if job.label_if is None:
        return True
    try:
        return job.label_if(ran)
    except Exception:
        log.exception("the label condition of the scheduled job %s failed: labelled all the same", job.name)
        return True


def _refused_since(path: Path, started: datetime) -> bool:
    """Whether ``path`` holds a whole report that refused (``passed`` false), made (its
    ``prevalidated_at``) at ``started`` or later: the run's own, not one an earlier run left."""
    try:
        report = json.loads(path.read_text())
        made = datetime.fromisoformat(str(report["prevalidated_at"])) if isinstance(report, dict) else None
    except (OSError, ValueError, KeyError):
        return False
    if made is None or made.tzinfo is None:
        return False
    return report.get("passed") is False and made >= started


def _stamp(path: Path) -> tuple[int, int, int] | None:
    """Which version of the file at ``path`` is there (inode, change time, size); None if none."""
    try:
        found = path.stat()
    except OSError:
        return None
    return found.st_ino, found.st_mtime_ns, found.st_size


def _when(job: Job) -> str:
    """When a job runs, as its lines say: at night, or in the session (#403)."""
    return "밤" if job.session is None else "장중"


def _what(job: Job, record: JobRecord) -> str:
    """What came of a run, as its comment and its line say it (#440): 성공 (통과 for a pre-validation
    that passed), 거부 (it judged to the end and refused: not a failure, though it exits 1), and the
    failures, by how they ended: 시간 초과, 신호로 죽음 (the kernel's out-of-memory killer sends
    SIGKILL), and any other exit code, or none (it could not be started): 예외 for a job with a
    verdict (``Job.report``: it ends so only when it raised before its verdict), 실패 for another (its
    codes are its own: the order probe's 3 says its keys are not set, #403)."""
    if record.verdict == PASSED:
        return "통과"
    if record.verdict == REFUSED:
        return "거부"
    if record.outcome == OK:
        return "성공"
    if record.timed_out:
        return "시간 초과"
    if record.exit_code is not None and record.exit_code < 0:
        return "신호로 죽음"
    return "예외" if job.report is not None else "실패"


def _code(exit_code: int | None) -> str:
    """An exit code as the lines show it: ``-9(SIGKILL)`` for a signal, ``없음`` if none."""
    if exit_code is None:
        return "없음"
    if exit_code < 0:
        with contextlib.suppress(ValueError):
            return f"{exit_code}({signal.Signals(-exit_code).name})"
    return str(exit_code)


def _heading(job: Job, record: JobRecord) -> str:
    commit = record.commit if record.commit == "unknown" else record.commit[:12]
    return (
        f"**예약 작업 `{job.name}`** · {record.day} {_when(job)} · {_what(job, record)} · 커밋 `{commit}` · "
        f"종료 코드 {_code(record.exit_code)} · {_duration(record.seconds)}"
    )


def _outcome_line(job: Job, record: JobRecord, links: Sequence[str], why_not: str | None) -> str:
    if record.timed_out:
        how = f"제한 시간 {_duration(job.timeout)}을 넘어 멈춤"
    elif record.exit_code is None:
        how = "시작하지 못함"
    else:
        how = f"종료 코드 {_code(record.exit_code)}"
    line = (
        f"예약 작업 {job.name}({record.day} {_when(job)}): {_what(job, record)}, {how}, "
        f"{_duration(record.seconds)}."
    )
    if links:
        line += f" 댓글: {' '.join(links)}"
    if why_not:
        line += (
            f" GitHub에 올리지 못했습니다({why_not}). 결과는 서버의 state/{STATE_FILE}에 두고 "
            "다음 밤이나 엔진이 다시 뜰 때 올립니다."
        )
    return line


def _duration(span: float | timedelta | None) -> str:
    total = round(span.total_seconds() if isinstance(span, timedelta) else (span or 0.0))
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f"{hours}시간" + (f" {minutes}분" if minutes else "")
    if minutes:
        return f"{minutes}분" + (f" {seconds}초" if seconds else "")
    return f"{seconds}초"


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


# The signal review's fixed window (#206, ``research.SIGNAL_REVIEW_FIXED``): the days
# ``Facts.signal_review_days`` counts, read here by ``read_facts``.
SIGNAL_REVIEW_FROM = date(2026, 11, 4)
SIGNAL_REVIEW_FIXED_DAYS = 20


async def read_facts(
    settings: Settings, calendar: MarketCalendar, clock: SessionClock, engine: PredictionEngine
) -> Facts:
    """What the jobs' conditions ask tonight (``Facts``). The files are read in worker threads; the
    engine's database only on the event loop's (its connection belongs to it), and those reads are
    a few small queries."""
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
    return Facts(
        recorded_days=recorded,
        waiting=tuple(engine.waiting()),
        official=len(engine.members()),
        reports=reports,
        pending_inputs=tuple(pending),
        simulating_daily=tuple(simulating_daily),
        simulating=tuple(simulating),
        revising=await asyncio.to_thread(revising_facts, settings, calendar, clock.now(), revising),
        pending_answers=tuple(answers),
        trader_kis=settings.trader_kis,
        trader_live_kis=settings.trader_live_kis,
        hit_rate_nights=await asyncio.to_thread(nights_with_hit_rates, settings),
        holdout_days=await asyncio.to_thread(days_in_window, settings.state_dir / DB_NAME),
        neyman_variants=await asyncio.to_thread(neyman_variants_progress, settings, clock.now()),
        neyman_backfill=await asyncio.to_thread(neyman_backfill_progress, settings),
        signal_review_days=await asyncio.to_thread(
            fixed_days, settings.state_dir / DB_NAME, SIGNAL_REVIEW_FROM, SIGNAL_REVIEW_FIXED_DAYS
        ),
        smt_holdout_days=await asyncio.to_thread(smt_days_in_holdout, settings, calendar, clock.now()),
        state_backup=await asyncio.to_thread(last_backup, settings.state_dir),
        night_book_nights=await asyncio.to_thread(night_book_nights, settings.state_dir),
        queue=await asyncio.to_thread(queue_order, settings.state_dir, clock.now()),
        fill_notice_apart=await asyncio.to_thread(apart_orders, settings.state_dir),
    )


async def session_facts(settings: Settings) -> Facts:
    """What the session jobs' conditions ask (#403): the settings, and the mode the trader last
    started in (#405, one small read of its store), nothing else beside the live engine in the
    market's hours."""
    mock = await asyncio.to_thread(trader_in_mock, settings.state_dir)
    return Facts(trader_kis=settings.trader_kis, trader_mock=mock)


def pending_answers(settings: Settings, calendar: MarketCalendar, now: datetime) -> list[str]:
    """The daily kinds whose answers of the latest days are not all in (``Facts.pending_answers``)."""
    out = []
    for kind in DAILY_KINDS:
        why = (
            None if kind.pending is None else kind.pending(settings.data_dir, calendar, settings.symbols, now)
        )
        if why is not None:
            log.info("%s: %s", kind.name, why)
            out.append(kind.name)
    return out


# The conditions a one-off job of any family asks most. No job in ``JOBS`` asks them now: those that
# did ran, posted their results and left it (#505). They stay for the next one-off job.
def _first_trading_night(night: Night) -> bool:
    """Once, on the night of the first trading day it is asked on: the first after the
    deploy that brings the job."""
    return night.trading_day and not night.ran


def _first_night(night: Night) -> bool:
    """Once, on the first night it is asked on, trading or not."""
    return not night.ran
