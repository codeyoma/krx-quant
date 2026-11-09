"""The scheduled jobs' machinery (#215; a module of its own since #500): what a job is (``Job``,
``Target``, ``Attachment``), what its condition is asked with (``Night``), how its facts are read
(``Reads``, from ``Sources``), the state file (``JobState``) and the scheduler that runs the due jobs
and posts their results (``Scheduler``). How they run is in the package's docstring
(``krx_quant.scheduled``); the jobs are in its family modules.

It knows no job nor experiment (#651): a job that asks what the engine knows brings the function
that reads it (``Job.facts``, a ``Reader``), in its family's module, and its condition gets what that
returned (``Night.facts``). The scheduler reads it when the job is first considered on a night, once
however many jobs ask the same reader, and again after a job that says so (``Job.refreshes``, #599:
the queue screen's order).
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
from typing import Any, Generic, Protocol, TypeVar, cast

from krx_quant.clock import SessionClock
from krx_quant.config import JOBS_UNTIL, Settings
from krx_quant.engine import PredictionEngine
from krx_quant.feeds.base import KST
from krx_quant.github import GitHub, GitHubError, comment_body
from krx_quant.market_calendar import MarketCalendar
from krx_quant.process_log import KRX_QUANT, Child, run_logged
from krx_quant.process_log import Ran as Ran  # a job's process, once it ended
from krx_quant.redaction import redact
from krx_quant.swap import NightTraining

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


F = TypeVar("F")  # what a job's condition asks: the facts its reader returns
T = TypeVar("T")


@dataclass(frozen=True)
class Sources:
    """What the jobs' facts are read from (#651): the settings, the market's calendar, the clock and
    the running engine (None where there is none: a test's scheduler)."""

    settings: Settings
    calendar: MarketCalendar
    clock: SessionClock
    engine: PredictionEngine | None = None

    @property
    def running(self) -> PredictionEngine:
        """The running engine; a reader that asks it cannot read without one."""
        if self.engine is None:
            raise LookupError("no running engine to read the jobs' facts from")
        return self.engine


class Reads:
    """The facts read for one turn of the jobs (#651): a night's, until a job that refreshes them
    (``Job.refreshes``) or the quiet night's turn; a session job's own. Each reader is read once, when
    the first job that asks it is considered: ``await reads(reader)``, which a reader also calls for
    the readers it builds on. One that raised raises again, unread."""

    def __init__(self, sources: Sources | None) -> None:
        self._sources = sources
        self._read: dict[Any, Any] = {}
        self._failed: dict[Any, BaseException] = {}

    @property
    def sources(self) -> Sources:
        if self._sources is None:
            raise LookupError("the scheduler has no sources to read the jobs' facts from")
        return self._sources

    async def __call__(self, reader: Reader[T]) -> T:
        if reader in self._failed:
            raise self._failed[reader]
        if reader not in self._read:
            try:
                self._read[reader] = await reader(self)
            except Exception as e:
                self._failed[reader] = e
                raise
        return cast(T, self._read[reader])


# What reads a job's facts (``Job.facts``): from the night's ``Reads``, its sources and the readers it
# builds on. The files are read in worker threads; the engine's database only on the event loop's (its
# connection belongs to it), in a few small queries.
Reader = Callable[[Reads], Awaitable[T]]


@dataclass(frozen=True)
class Night(Generic[F]):
    """What a job's condition is asked with."""

    day: date  # the night's day: from KRX_JOBS_AT on it to 05:30 the next morning; a session job's day
    trading_day: bool
    commit: str  # the code running
    record: JobRecord | None  # this job's last run; None if it never ran
    records: Mapping[str, JobRecord]  # every job's last run
    facts: F  # what its reader (``Job.facts``) read; None for a job without one

    @property
    def ran(self) -> bool:
        """This job has run to its end (ok or failed); a run cut by a restart has not."""
        return self.record is not None and self.record.outcome in (OK, FAILED)


@dataclass(frozen=True)
class Job(Generic[F]):
    """A scheduled job: when it runs (``due``, asked with what ``facts`` reads), what (``krx-quant``
    arguments) for how long at most, and where its result goes."""

    name: str  # unique: the job's key in the state file
    due: Callable[[Night[F]], bool]
    command: tuple[str, ...]
    timeout: timedelta
    targets: tuple[Target, ...]
    label: tuple[str, ...] = (READY_FOR_AGENT,)  # set on each issue once the comment is up
    unlabel: tuple[str, ...] = (READY_FOR_HUMAN, NEEDS_TRIAGE)  # taken off then
    attach: tuple[Attachment, ...] = ()  # files it writes, posted with its output
    # What its condition asks of what the engine and the server know (#651): read when the job is first
    # considered on a night, and handed to its conditions as ``Night.facts``. None: it asks nothing but
    # the night and the runs (``Night.facts`` is None).
    facts: Reader[F] | None = None
    # The later jobs this one gives way to (#376): None, or which of them. See ``Scheduler``.
    gives_way_to: Callable[[Job[Any]], bool] | None = None
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
    quiet: Callable[[Night[F]], bool] | None = None
    # Whether the night's facts are read again once it ran (#599: the queue screen sets the order the
    # pre-validations after it go by). False: the later jobs ask the facts read before it.
    refreshes: bool = False
    # The algorithm whose first pre-validation it runs (#599): the quiet night's turn asks those jobs,
    # among their places in the list, in the order ``order`` reads (the queue screen's). None: in the
    # list's.
    queued: str | None = None
    order: Reader[tuple[str, ...]] | None = None
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


def in_queue(jobs: Sequence[Job[Any]], queue: Sequence[str]) -> list[Job[Any]]:
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


def session_window(job: Job[Any], day: date) -> tuple[datetime, datetime]:
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
    second pre-validation) are asked it, and one due runs if it can still end by 05:30. A job's facts
    (``Job.facts``) are read when it is first considered, once a night for every job that asks the same
    reader (``Reads``), and again after a job that says so (``Job.refreshes``, #599) and for the quiet
    night's turn. A job that goes on by day across nights (``Job.resumes``, #643) starts when a day of
    it still ends by 05:30, is told that end (``--until``) and is stopped there at the latest.

    The session jobs (``Job.session``, #403) run apart from the nights: on each trading day, in the
    list's order, each from its time if it can still end by ``SESSION_UNTIL``, asked its condition
    with its facts read then (cheap ones: the live engine runs beside it). A collector started later in
    the session runs a job due at once; one started after it waits for the next trading day."""

    def __init__(
        self,
        jobs: Sequence[Job[Any]],
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
        sources: Sources | None = None,
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
        self._sources = sources
        self._runner = runner
        # Each run's end, a cut one's too, as it is kept (#614): the readiness line keeps them until a
        # line that told them went, as the state keeps a job's last run alone.
        self._ran = ran or (lambda name, record: None)

    @property
    def _night_jobs(self) -> list[Job[Any]]:
        return [j for j in self._jobs if j.session is None]

    @property
    def _session_jobs(self) -> list[Job[Any]]:
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
            due = await self._due(job, day, Reads(self._sources))
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

    def _job_named(self, name: str) -> Job[Any] | None:
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
        reads = Reads(self._sources)  # each job's facts, read when it is first considered
        queue = self._night_jobs
        gave_way: set[str] = set()  # the jobs that gave way tonight: once a night each
        while queue:
            job = queue.pop(0)
            if self._cut(job, day):
                continue  # cut by a restart tonight: the next night runs it
            due = await self._due(job, day, reads)
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
            elif job.name not in gave_way and (ahead := await self._gives_way(job, queue, day, reads)):
                gave_way.add(job.name)
                at = queue.index(ahead) + 1
                while at < len(queue) and queue[at].name in gave_way:
                    at += 1  # after those that gave way to it before: the list's order among them
                queue.insert(at, job)
            else:
                await self._run(job, day)
                if job.refreshes:
                    reads = Reads(self._sources)  # read again for the next jobs (#599)
        await self._quiet_turn(day)

    async def _quiet_turn(self, day: date) -> None:
        """The night of ``day`` once every job had its turn (#546): the jobs with a condition for a
        quiet night (``Job.quiet``) are asked it, in the list's order (those queued in the queue
        screen's, #599: ``in_queue``, read by ``Job.order``), on the facts read again (a run tonight may
        have changed them);
        one due runs if its time limit still ends by 05:30. One that would not is only logged: it had
        its turn, and waits for the next night's."""
        asked = [j for j in self._night_jobs if j.quiet is not None and not self._cut(j, day)]
        if not asked:
            return
        _, end = window(day, self._at)
        reads = Reads(self._sources)
        for job in in_queue(asked, await self._order(asked, reads)):
            if not await self._due(job, day, reads, quiet=True):
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

    def _cut(self, job: Job[Any], day: date) -> bool:
        """Whether a restart cut ``job``'s run on the night of ``day``."""
        record = self._state.jobs.get(job.name)
        return record is not None and record.outcome == INTERRUPTED and record.day == day

    async def _due(self, job: Job[Any], day: date, reads: Reads, *, quiet: bool = False) -> bool | None:
        """Whether ``job`` is due on the night of ``day``, asked now (``quiet``: by its condition on a
        quiet night, ``Job.quiet``) with its facts (``Job.facts``) as ``reads`` has them; None if its
        condition failed, or its facts could not be read."""
        facts = None
        if job.facts is not None:
            try:
                facts = await reads(job.facts)
            except Exception:
                log.exception("could not read what the condition of the scheduled job %s asks", job.name)
                return None
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

    def _waits_no_more(self, job: Job[Any]) -> None:
        """``job`` is not due: the nights it gave way while it waited count no more (#376)."""
        if self._state.gave_way.pop(job.name, None) is not None:
            self._state.save()

    async def _gives_way(
        self, job: Job[Any], queue: Sequence[Job[Any]], day: date, reads: Reads
    ) -> Job[Any] | None:
        """The job of ``queue`` (the rest of the night's, in turn) that ``job`` gives way to tonight
        (#376): the first it gives way to that is due and could start now, but not once ``job`` ran
        to its time limit. None if there is none (both fit), or if ``job`` gave way on
        ``GIVE_WAY_NIGHTS`` nights already: it runs first then."""
        if job.gives_way_to is None:
            return None
        _, end = window(day, self._at)
        now = self._clock.now()
        ended = now + job.timeout  # at the latest: its limit, not how long it last took
        ahead = None
        for later in queue:
            if (
                job.gives_way_to(later)
                and now <= end - later.needs < ended
                and not self._cut(later, day)
                and await self._due(later, day, reads)
            ):
                ahead = later
                break
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

    async def _order(self, jobs: Sequence[Job[Any]], reads: Reads) -> tuple[str, ...]:
        """The order the queued of ``jobs`` go by (``Job.order``, #599: the queue screen's), read by the
        first that has one; none (the list's) without, or when it cannot be read."""
        order = next((job.order for job in jobs if job.order is not None), None)
        if order is None:
            return ()
        try:
            return await reads(order)
        except Exception:
            log.exception("could not read the order of the queued scheduled jobs")
            return ()

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

    async def _run(self, job: Job[Any], day: date) -> None:
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

    def _verdict(self, job: Job[Any], ran: Ran, started: datetime) -> str | None:
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
        self, job: Job[Any], before: Mapping[str, tuple[int, int, int] | None]
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
        job: Job[Any],
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


def _calls(job: Job[Any], ran: Ran) -> bool:
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


def _when(job: Job[Any]) -> str:
    """When a job runs, as its lines say: at night, or in the session (#403)."""
    return "밤" if job.session is None else "장중"


def _what(job: Job[Any], record: JobRecord) -> str:
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


def _heading(job: Job[Any], record: JobRecord) -> str:
    commit = record.commit if record.commit == "unknown" else record.commit[:12]
    return (
        f"**예약 작업 `{job.name}`** · {record.day} {_when(job)} · {_what(job, record)} · 커밋 `{commit}` · "
        f"종료 코드 {_code(record.exit_code)} · {_duration(record.seconds)}"
    )


def _outcome_line(job: Job[Any], record: JobRecord, links: Sequence[str], why_not: str | None) -> str:
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


# The conditions a one-off job of any family asks most. No job in ``JOBS`` asks the first two now: those
# that did ran, posted their results and left it (#505). They stay for the next one-off job. The third
# is the retry rule of the jobs that run once and again after a failure on new code (#651).
def _first_trading_night(night: Night[Any]) -> bool:
    """Once, on the night of the first trading day it is asked on: the first after the
    deploy that brings the job."""
    return night.trading_day and not night.ran


def _first_night(night: Night[Any]) -> bool:
    """Once, on the first night it is asked on, trading or not."""
    return not night.ran


def again_on_new_code(night: Night[Any]) -> bool:
    """The retry rule of a job that runs once (#651): not run to its end yet (a run a restart cut has
    not); or failed, and the code running is other now."""
    record = night.record
    return not night.ran or (
        record is not None and record.outcome == FAILED and record.commit != night.commit
    )
