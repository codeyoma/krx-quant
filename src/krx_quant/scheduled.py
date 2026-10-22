"""Scheduled jobs (#215): the server runs a ``krx-quant`` command when its time comes and
leaves the result as a comment on GitHub issues, where people and agents read it.

The jobs are listed here (``JOBS``), so what runs on the server, and when, is reviewed in
a PR; the pre-validations (#204) are built from the algorithm registries, one for each
algorithm registered after pre-validation existed. Each has a name (its key in the state
file), a condition asked every night (``Night``: the day, whether it trades, this job's
last run, every job's, and what the engine knows, ``Facts``), the command (``krx-quant``
arguments), a time limit, the issues its result goes to (each with the part of the output
it gets), the files it writes that go with the result (``Attachment``) and the labels it
sets.

Every night (trading or not) from ``KRX_JOBS_AT`` (22:30) to 05:30, once the nightly
training is over (``swap.NightTraining``), the jobs due run one at a time, in this list's
order, each in its own process, as the nightly training does; its whole output also goes to
``state/logs/jobs/<job>/<night>.log`` as it comes (``log_files.py``, #356). A job's condition is asked
when its turn comes, so it sees the runs of the jobs before it that night. One whose time
limit would pass 05:30 is not started that night: the 06:00 deploy and the 07:20 restart
stop the collector. A collector started inside the window (a restart at night) runs that
night's jobs at once.

The state (``state/scheduled.json``) keeps each job's last run: its night, outcome (``ok``,
``failed``, ``interrupted``; ``running`` while it runs), exit code, duration, commit and
comments, and the results not posted yet. A run the collector's stop cut (a deploy, a
restart, a crash) is ``interrupted``: it does not count as run, and runs again the next
night. A result is masked (``redaction.py``) and posted on each issue, which then gets
``ready-for-agent`` in place of ``ready-for-human`` and ``needs-triage``. Without
``KRX_GITHUB_TOKEN``, or when GitHub fails, the result waits in the state and is posted at
the next night or start. Telegram gets a line per outcome, with the comments' addresses.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from functools import partial
from pathlib import Path
from time import monotonic
from typing import Any, Protocol

import aiohttp

from krx_quant.algorithms import ALGORITHMS, DAILY_ALGORITHMS, KIND_ALGORITHMS, Algorithm, KindAlgorithm
from krx_quant.clock import SessionClock
from krx_quant.config import JOBS_UNTIL, Settings
from krx_quant.daily import DailyAlgorithm, DailyKind
from krx_quant.daily_features import pending_inputs
from krx_quant.engine import PredictionEngine
from krx_quant.feeds.base import KST
from krx_quant.github import GitHub, GitHubError, comment_body
from krx_quant.kind_registry import DAILY_KINDS, daily_kind_named
from krx_quant.log_files import JOBS as JOB_LOGS
from krx_quant.log_files import KRX_QUANT, night_file, root_of, run_logged
from krx_quant.log_files import Ran as Ran  # a job's process, once it ended
from krx_quant.market_calendar import MarketCalendar
from krx_quant.night_probe import evening, subscribed_nights
from krx_quant.notify import Alerts
from krx_quant.prevalidation import MIN_DAYS, REGISTERED_BEFORE, LastReport, last_report, report_path
from krx_quant.redaction import redact, secrets_of
from krx_quant.swap import NightTraining
from krx_quant.training import finished_days, is_recorded

log = logging.getLogger(__name__)

STATE_FILE = "scheduled.json"
OK, FAILED, INTERRUPTED, RUNNING = "ok", "failed", "interrupted", "running"
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
    """``state/scheduled.json``: each job's last run, and the results not posted yet."""

    def __init__(
        self, path: Path, jobs: dict[str, JobRecord] | None = None, unposted: list[Unposted] | None = None
    ) -> None:
        self.path = path
        self.jobs = jobs or {}
        self.unposted = unposted or []

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
        }
        tmp = self.path.with_name(f".{self.path.name}.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        tmp.replace(self.path)


@dataclass(frozen=True)
class Facts:
    """What the engine knows that a condition may ask, read once a night."""

    recorded_days: int = 0  # recorded days, as ``prevalidate`` counts them (MIN_DAYS, #204)
    waiting: tuple[str, ...] = ()  # the algorithms waiting for their pre-validation (#204)
    official: int = 0  # the consensus members: active algorithms on the engine's target (#206)
    # The latest pre-validation report of each registered algorithm that has one (#204).
    reports: Mapping[str, LastReport] = field(default_factory=dict)
    # The trading days of the week before the night whose night session (18:00–06:00) the
    # collector subscribed the KOSPI200 night futures for before it began (#264).
    night_futures: tuple[date, ...] = ()
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
    # Whether ``KRX_OPENAPI_KEY`` is set on this server (#305): the KRX Open API probe waits for it.
    krx_openapi: bool = False


@dataclass(frozen=True)
class Night:
    """What a job's condition is asked with."""

    day: date  # the night's day: from KRX_JOBS_AT on it to 05:30 the next morning
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


class Runner(Protocol):
    """Runs a job's ``krx-quant`` arguments within its time limit, its output also into its log
    file (``jobs/<job>/<night>.log``, #356; None: none)."""

    def __call__(
        self, args: Sequence[str], timeout: timedelta, log_file: Path | None = None
    ) -> Awaitable[Ran]: ...


Notify = Callable[[str], Awaitable[bool]]


async def run_command(
    args: Sequence[str],
    timeout: timedelta,
    log_file: Path | None = None,
    *,
    program: Sequence[str] = KRX_QUANT,
) -> Ran:
    """Run ``program`` with ``args`` and read its output, each line also into ``log_file``;
    killed at ``timeout`` (what it wrote by then is kept), and when the collector stops
    (cancelled)."""
    return await run_logged([*program, *args], timeout, log_file)


def window(day: date, at: time) -> tuple[datetime, datetime]:
    """The night of ``day``: from ``at`` (on ``day``, or after midnight the next day) to 05:30
    the next morning."""
    start = day if at >= JOBS_UNTIL else day + timedelta(days=1)
    return datetime.combine(start, at, KST), datetime.combine(day + timedelta(days=1), JOBS_UNTIL, KST)


def first_night(at: time, now: datetime) -> date:
    """The first night a scheduler started at ``now`` runs: one under way (a restart at
    night) at once, else the next."""
    today = now.astimezone(KST).date()
    for day in (today - timedelta(days=1), today):
        if now < window(day, at)[1]:
            return day
    return today + timedelta(days=1)


class Scheduler:
    """Runs the due jobs every night and posts their results."""

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
        runner: Runner = run_command,
        logs: Path | None = None,
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
        self._runner = runner
        self._logs = logs  # state/logs: each run's output goes to jobs/<job>/<night>.log (#356)

    async def run(self) -> None:
        """Every night's jobs, until cancelled; first what the last collector left."""
        await self.recover()
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

    async def recover(self) -> None:
        """A run the last collector left going was cut (a deploy, a restart, a crash): it runs
        again the next night. Results not posted yet are posted."""
        for name, record in self._state.jobs.items():
            if record.outcome == RUNNING:
                record.outcome = INTERRUPTED
                self._state.save()
                log.warning("scheduled job %s was cut on the night of %s: it runs again", name, record.day)
                await self._notify(
                    f"예약 작업 {name}({record.day} 밤)이 엔진 재시작으로 끊겼습니다. 다음 밤에 다시 돕니다."
                )
        await self._post_unposted()

    async def run_night(self, day: date) -> None:
        """The night of ``day``: the results earlier nights could not post, then each due job
        in turn, once the night's training is over."""
        _, end = window(day, self._at)
        await self._post_unposted()
        facts: Facts | None = None
        for job in self._jobs:
            record = self._state.jobs.get(job.name)
            if record is not None and record.outcome == INTERRUPTED and record.day == day:
                continue  # cut by a restart tonight: the next night runs it
            if facts is None:
                facts = await self._read_facts()
            night = Night(
                day, self._calendar.is_trading_day(day), self._commit, record, dict(self._state.jobs), facts
            )
            try:
                due = job.due(night)
            except Exception:
                log.exception("the condition of the scheduled job %s failed", job.name)
                continue
            if not due:
                continue
            latest = end - job.timeout  # the last start that ends by 05:30
            if not await self._training_done(latest):
                await self._notify(
                    f"예약 작업 {job.name}({day} 밤): 밤 학습이 {latest:%H:%M}까지 끝나지 않아 "
                    "다음 밤으로 미룹니다."
                )
            elif self._clock.now() > latest:
                await self._notify(
                    f"예약 작업 {job.name}({day} 밤): 지금 시작하면 제한 시간({_duration(job.timeout)})이 "
                    f"{JOBS_UNTIL:%H:%M}을 넘어 다음 밤으로 미룹니다."
                )
            else:
                await self._run(job, day)

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
        self._state.save()
        before = {a.path: _stamp(self._files / a.path) for a in job.attach}
        log.info("scheduled job %s: krx-quant %s", job.name, " ".join(job.command))
        began = monotonic()
        try:
            ran = await self._runner(
                job.command, job.timeout, night_file(self._logs, JOB_LOGS, day, job.name)
            )
        except Exception as e:
            log.exception("could not run the scheduled job %s", job.name)
            ran = Ran(None, "", f"could not be started: {type(e).__name__}: {e}")
        seconds = round(monotonic() - began, 1)
        outcome = OK if ran.exit_code == 0 and not ran.timed_out else FAILED
        record = JobRecord(day, outcome, started, self._commit, ran.exit_code, seconds, ran.timed_out)
        self._state.jobs[job.name] = record
        log.info("scheduled job %s: %s (exit %s) in %.1fs", job.name, outcome, ran.exit_code, seconds)
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
        return Unposted(job.name, record.day, target.issue, body, job.label, job.unlabel)

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
            line = f"예약 작업 {name}({day} 밤)의 결과를 이제 올렸습니다. 댓글: {' '.join(links)}"
            await self._notify(line)


def _stamp(path: Path) -> tuple[int, int, int] | None:
    """Which version of the file at ``path`` is there (inode, change time, size); None if none."""
    try:
        found = path.stat()
    except OSError:
        return None
    return found.st_ino, found.st_mtime_ns, found.st_size


def _heading(job: Job, record: JobRecord) -> str:
    code = "없음" if record.exit_code is None else str(record.exit_code)
    commit = record.commit if record.commit == "unknown" else record.commit[:12]
    return (
        f"**예약 작업 `{job.name}`** · {record.day} 밤 · 커밋 `{commit}` · 종료 코드 {code} · "
        f"{_duration(record.seconds)}"
    )


def _outcome_line(job: Job, record: JobRecord, links: Sequence[str], why_not: str | None) -> str:
    what = "성공" if record.outcome == OK else "실패"
    if record.timed_out:
        how = f"제한 시간 {_duration(job.timeout)}을 넘어 멈춤"
    elif record.exit_code is None:
        how = "시작하지 못함"
    else:
        how = f"종료 코드 {record.exit_code}"
    line = f"예약 작업 {job.name}({record.day} 밤): {what}, {how}, {_duration(record.seconds)}."
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
    nights = await asyncio.to_thread(subscribed_nights, settings.data_dir, calendar, evening(clock.now()))
    daily = sorted({f for a in engine.daily_registered for f in a.features})
    pending = await asyncio.to_thread(pending_inputs, daily, settings.state_dir, settings.symbols)
    answers = await asyncio.to_thread(pending_answers, settings, calendar, clock.now())
    simulating, simulating_daily = engine.to_simulate()  # the engine's database: on this thread
    return Facts(
        recorded,
        tuple(engine.waiting()),
        len(engine.members()),
        reports,
        tuple(nights),
        tuple(pending),
        simulating_daily=tuple(simulating_daily),
        simulating=tuple(simulating),
        pending_answers=tuple(answers),
        krx_openapi=settings.krx_openapi_key is not None,
    )


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


async def keep_jobs(
    settings: Settings,
    session: aiohttp.ClientSession,
    calendar: MarketCalendar,
    alerts: Alerts,
    clock: SessionClock,
    training: NightTraining,
    engine: PredictionEngine,
    jobs: Sequence[Job] | None = None,
) -> None:
    """The collector's scheduled jobs (``KRX_JOBS_AT``), until cancelled."""
    assert settings.jobs_at is not None
    github = GitHub(session, settings.github_token, settings.github_repo, settings.github_api_url)
    if not github.configured:
        log.warning("KRX_GITHUB_TOKEN is not set: the scheduled jobs keep their results on the server")

    scheduler = Scheduler(
        JOBS if jobs is None else jobs,
        settings.jobs_at,
        calendar,
        JobState.load(settings.state_dir / STATE_FILE),
        github,
        alerts.deliver,
        clock,
        commit=settings.commit,
        secrets=secrets_of(settings),
        training=training,
        facts=partial(read_facts, settings, calendar, clock, engine),
        logs=root_of(settings),
    )
    await scheduler.run()


# The jobs, run in this order. A new timed piece of server work is a new entry here (a PR),
# not an issue asking someone to remember a date (AGENTS.md).


def _first_trading_night(night: Night) -> bool:
    """Once, on the night of the first trading day it is asked on: the first after the
    deploy that brings the job."""
    return night.trading_day and not night.ran


# ① The diagnosis (#203, #187): after the engine with its section 9 (#213) is deployed, on
# the first trading day's night. Sections 7 and 8 decide BAR_C and record the barrier bar
# rule's agreement (#203); section 9 is the crossed quotes (#187). It reads the engine DB
# read-only, safe beside the running engine, one feed at a time; the hour is a bound, not
# an estimate. It ran on the night of 2026-10-16: BAR_C = 1 was confirmed and the barrier
# bar rule's agreement measured (#203), and #187 chose from section 9 to count crossed quotes
# in the touches (target version 3, barrier answer version 2).
DIAGNOSE = Job(
    name="diagnose-203-187",
    due=_first_trading_night,
    command=("diagnose", "--days", "20"),
    timeout=timedelta(hours=1),
    targets=(
        Target(203),
        Target(
            187,
            sections("진단", "9. 뒤집힌 호가(#187)"),
            "출력 중 머리말과 9절(뒤집힌 호가)만 싣는다. 전체 출력은 #203에 있다.",
        ),
    ),
)

# ② The signal-threshold review (#206): on the first night with an official algorithm (the
# first probation verdicts on the touch target, about ten trading days after it restarted
# probation, #170), once. Before, the consensus has no member and no signal goes out. The
# command reads the engine DB read-only, beside the running engine; the hour is a bound, not
# an estimate. Before the pre-validations: it is short, and they may take the rest of the night.
SIGNAL_REVIEW_ISSUE = 206


def _first_official_night(night: Night) -> bool:
    """Once, on the first night the consensus has a member (an official algorithm)."""
    return night.facts.official > 0 and not night.ran


SIGNAL_REVIEW = Job(
    name="signal-review-206",
    due=_first_official_night,
    command=("signal-review", "--days", "20"),
    timeout=timedelta(hours=1),
    targets=(
        Target(
            SIGNAL_REVIEW_ISSUE,
            note=(
                "에이전트가 이 출력으로 신호 기준의 추천값(`KRX_SIGNAL_MIN_PROBABILITY`, "
                "`KRX_SIGNAL_MIN_AGREE`)을 낸다. 사용자가 정하면 서버 `.env`의 두 값을 바꾸거나 기본값을 "
                "PR로 바꾼다."
            ),
        ),
    ),
)

# ③ The daily supply sources (#226): where KIS, and the Toss Open API when its client is set
# (#235), give the chart's daily supply indicators, asked once, on the first night after the
# deploy that brings the job (trading or not: the probe asks for the latest trading day and
# earlier ones). It only reads, spaced under each API's limit, with the cached access tokens
# (the collector's KIS one; Toss's one-per-client token, shared), and prints no market value,
# so its report can go on the public issue. KIS takes at most about 240 calls, Toss about 170,
# a few minutes; the KIS part stops at 15 minutes and the Toss part at 8 (each call at 15
# seconds), inside the half hour. Before the pre-validations, which may take the night.
SUPPLY_PROBE_ISSUE = 226


def _first_night(night: Night) -> bool:
    """Once, on the first night it is asked on, trading or not."""
    return not night.ran


SUPPLY_PROBE = Job(
    name="probe-supply-226",
    due=_first_night,
    command=("probe-supply",),
    timeout=timedelta(minutes=30),
    targets=(
        Target(
            SUPPLY_PROBE_ISSUE,
            note=(
                '에이전트가 이 출력으로 `docs/kis-feeds.md` "일별 수급 (#226)"과 `docs/toss-feeds.md`의 ⚠를 '
                "지우거나 고친다. 출력에는 시세 값이 없다(필드 이름, 행 수, 날짜, 표시만)."
            ),
        ),
    ),
)

# ④ The structure check (#270): the research's market-structure claims (``structure_check``)
# measured on the stored one-minute bars, day candles and KIS daily flows, once, on the first
# night after the deploy that brings the job (trading or not: it reads finished days only).
# It reads files and makes no call; its output holds no price, volume or flow value, so it can
# go on the public issue. A year of bars for three symbols is a few minutes; the half hour is a
# bound. Before the pre-validations, which may take the night.
STRUCTURE_CHECK_ISSUE = 270

STRUCTURE_CHECK = Job(
    name="structure-check-270",
    due=_first_night,
    command=("structure-check",),
    timeout=timedelta(minutes=30),
    targets=(
        Target(
            STRUCTURE_CHECK_ISSUE,
            note=(
                "에이전트가 이 출력으로 리서치 보고서(`research/reports/`)와 #262(채점), "
                "#264(하루 한 번 예측)에 반영할 것을 정리한다. 출력에는 가격·거래량·수급 값이 없다"
                "(비율, 기울기, 개수, 중앙값만)."
            ),
        ),
    ),
)

# The entry delay (#262): after the engine with the diagnosis's section 10 is deployed, on the
# first trading day's night, once: each predictor's skill against the class rates, and its up
# and down calls acted on 1, 5 and 30 seconds after the bar close, over the 20 days. The whole
# diagnosis runs (it is one command); only its header and section 10 go to #262. The hour is a
# bound, as the diagnosis's above. Before the pre-validations.
ENTRY_DELAY_ISSUE = 262

ENTRY_DELAY = Job(
    name="entry-delay-262",
    due=_first_trading_night,
    command=("diagnose", "--days", "20"),
    timeout=timedelta(hours=1),
    targets=(
        Target(
            ENTRY_DELAY_ISSUE,
            sections("진단", "10. 예측자별 실력과 진입 지연(#262"),
            "출력 중 머리말과 10절(예측자별 실력과 진입 지연)만 싣는다. 에이전트가 이것으로 #262의 진입 지연"
            " 민감도를 정리한다. 목표 도달과 닿음 손익은 봉 마감, +1초, +5초, +30초에 들어갔을 때다.",
        ),
    ),
)

# The daily kinds (#264): after the engine with open_to_close is deployed, on the first trading
# day's night, once: its 08:50 predictions and the answers written once the day's KRX day candle
# was stored (after 20:30), and that candle's open and close against the recorded KRX trades'
# opening and closing auction prints. It reads the engine DB read-only, the candles and the
# day's recording; it prints no price. Minutes at most; the half hour is a bound. Before the
# pre-validations, which may take the night.
DAILY_CHECK_ISSUE = 264

DAILY_CHECK = Job(
    name="daily-check-264",
    due=_first_trading_night,
    command=("daily-check", "--days", "1"),
    timeout=timedelta(minutes=30),
    targets=(
        Target(
            DAILY_CHECK_ISSUE,
            note=(
                "에이전트가 이 출력으로 첫 거래일의 08:50 예측과 채점이 돌았는지, KRX 일봉의 시가·종가가 "
                '녹화한 KRX 체결의 시가·종가 단일가와 같은지 확인하고 `docs/engine-api.md`의 "시가→종가"에 '
                "남긴다. 출력에는 가격이 없다(같음·다름과 개수만)."
            ),
        ),
    ),
)

# The structure check's turn of the month again (#270, #264): its first run counted KIS's 0 net
# buys (every day before NXT opened) as figures. They are none now (``supply.no_figure``), and
# section 4 heads each symbol with the days that have a foreign figure, the first and the last,
# which shows on the server's files where KIS's figures begin: the days Cox's flows have (#264).
# Once, on the first night after the deploy that brings it (trading or not); its header and
# section 4 go to #264. As the first run's, it reads files only; the half hour is a bound.
# Before the pre-validations.
STRUCTURE_SUPPLY = Job(
    name="structure-check-supply-264",
    due=_first_night,
    command=("structure-check",),
    timeout=timedelta(minutes=30),
    targets=(
        Target(
            DAILY_CHECK_ISSUE,
            sections("구조 실측", "4. 월말·월초"),
            "출력 중 머리말과 4절(월말·월초 × 전날 외국인 순매수)만 싣는다. 에이전트가 이것으로 KIS 외국인 "
            "순매수가 있는 첫날(NXT 개장 2025-03-04로 본다, #270)을 확인하고 `docs/kis-feeds.md`와 "
            "Cox의 수급 입력 설명에 남긴다. 출력에는 수급 값이 없다(개수, 날짜, 비율만).",
        ),
    ),
)

# KRX's net buys before NXT opened (#264, #297): the supply keeper asks the days before NXT of
# KIS's KRX investor query (``J``) once, at the collector's first start after the deploy that
# brings it (06:00); whether ``J`` has figures that far back only the server shows. Once, on the
# first night after that deploy (trading or not): ``probe-net-buys`` asks ``J`` and ``UN`` for
# 30 days to each of six days per symbol (36 calls for three, spaced as the supply probe's, a
# minute or so) and counts what the keeper stored. It prints counts, days and field looks, no
# market value, so it can go on the public issue. The half hour is a bound. Before the
# pre-validations: its count of the stored days before NXT says whether Cox's re-run (the flows'
# version 2, ``daily_features``) runs that night or waits for them (``Facts.pending_inputs``).
NET_BUYS_PROBE = Job(
    name="probe-net-buys-j-264",
    due=_first_night,
    command=("probe-net-buys",),
    timeout=timedelta(minutes=30),
    targets=(
        Target(
            DAILY_CHECK_ISSUE,
            note=(
                "에이전트가 이 출력으로 KIS `J`가 NXT 개장(2025-03-04) 전의 순매수를 주는지, 수집이 "
                '그 날들을 저장했는지 확인하고 `docs/kis-feeds.md` "투자자별 순매수"의 ⚠와 Cox의 수급 입력 '
                "설명에 남긴다. 저장됐으면 같은 밤 `prevalidate-cox`가 그것으로 돌고, 아니면 기다린다. "
                "출력에는 시세 값이 없다(행 수, 날짜, 개수, 필드 모양만)."
            ),
        ),
    ),
)

# What the KRX day candle's close is (#264): the 2026-10-19 daily check found it unlike the
# recorded closing auction print (its open alike), and KRX's after-market opened on 2026-09-14.
# Once, on the first night after the deploy that brings it (trading or not: KIS gives the past
# days): ``probe-close`` asks KIS for each symbol's KRX one-minute bars of the last year's days
# (two or three pages a day, about 1,600 calls for three symbols, spaced a quarter second: seven
# minutes or so) and counts, before and from that day, whether the candle's close is the closing
# auction bar's or the day's last bar's, and the recorded print's. It prints counts and bar times,
# no price, so it can go on the public issue. The half hour is a bound. Before the
# pre-validations.
CLOSE_PROBE = Job(
    name="probe-close-264",
    due=_first_night,
    command=("probe-close",),
    timeout=timedelta(minutes=30),
    targets=(
        Target(
            DAILY_CHECK_ISSUE,
            note=(
                "에이전트가 이 출력으로 KRX 일봉의 종가가 무엇인지(애프터마켓의 마지막 가격인지, "
                "15:30 종가 단일가인지) 애프터마켓(2026-09-14) 전과 뒤로 나눠 확인하고, "
                '`docs/engine-api.md` "시가→종가"와 `docs/kis-feeds.md`에 남긴다. 출력에는 가격이 없다'
                "(같음·다름과 개수, 봉 시각만)."
            ),
        ),
    ),
)

# The day candles asked again (#264): ``probe-close-264`` (2026-10-20) found the KRX day candle's
# close the closing auction's on every stored day but one per symbol, the one stored the same
# evening, while KIS's candle was still provisional. The collector now asks KIS again for each
# file's newest stored candles every run and replaced the stored ones KIS has otherwise once, at
# the first run after the deploy (``candles``). Once, on the first trading day's night after that
# deploy, so that evening's candle is stored provisional: ``probe-close`` again, which now also
# lists the days whose close is not the auction's, the newest stored days against KIS's candle as
# it is now, and the candles the collector replaced (the run and the fields). It prints counts,
# dates, bar times and field names, no price. The first run took 17 minutes; the 45 are a bound.
# Before the pre-validations.
CLOSE_REFRESH_PROBE = Job(
    name="probe-close-refresh-264",
    due=_first_trading_night,
    command=("probe-close",),
    timeout=timedelta(minutes=45),
    targets=(
        Target(
            DAILY_CHECK_ISSUE,
            note=(
                "에이전트가 이 출력으로 일봉 다시 받기(#264)가 됐는지 확인한다: 애프터마켓 뒤에 "
                "종가가 단일가 봉과 다른 날이 그날 저녁 저장한 날뿐인지, 그날 밤 KIS의 일봉이 아직 잠정인지, "
                "수집기가 바꾼 봉이 어느 실행(아침 시작 또는 20:30)에서 어느 필드였는지. "
                "그 결과를 `docs/kis-feeds.md` "
                '"기간별 시세"와 `docs/engine-api.md` "시가→종가"에 남긴다. 출력에는 가격이 없다'
                "(같음·다름과 개수, 날짜, 봉 시각, 필드 이름만)."
            ),
        ),
    ),
)

# The single-stock ETFs' net assets (#305): the closing flow's demand of a product is
# A·(L² − L)·r, with A its net assets on the previous trading day, and no KIS query for them was
# measured. Once, on the first trading day's night after the deploy that brings it, so the night
# shows whether that day's values are in by then: ``probe-nav`` asks KIS's ETF/ETN current price,
# its daily, product and minute NAV trends for each of the 16 products of ``products.PRODUCTS``
# (about 100 calls spaced half a second, a minute or so) and compares the net asset total with
# the listed units × NAV. It prints counts, dates, times and field looks, no market value, so it
# can go on the public issue. The half hour is a bound. Before the pre-validations.
NAV_PROBE_ISSUE = 305

NAV_PROBE = Job(
    name="probe-nav-305",
    due=_first_trading_night,
    command=("probe-nav",),
    timeout=timedelta(minutes=30),
    targets=(
        Target(
            NAV_PROBE_ISSUE,
            note=(
                "에이전트가 이 출력으로 상품마다 어느 조회가 순자산을 주는지, 지난날을 상장일"
                "(2026-05-27)까지 주는지, 그날 값이 언제 나오는지 정리해 `docs/kis-feeds.md` "
                '"ETF 순자산 (#305)"의 ⚠를 고치고, 순자산 출처를 정한다(KIS가 아닌 출처는 사용자가 정한다). '
                "그 전에는 순자산을 쓰지 않는다(2단계는 이 결과를 기다린다). 출력에는 시세 값이 없다"
                "(개수, 날짜, 시각, 필드 모양, 같음·다름만)."
            ),
        ),
    ),
)

# Cash dividends and the adjusted prices (#310): open_to_close_5d reads its window's two ends from
# KIS's adjusted KRX day candles; whether that adjustment takes cash dividends out, and whether
# KIS's rows flag the ex-dividend days, only KIS's answers show. Once, on the first night after the
# deploy that brings it (trading or not: KIS gives the past days): ``probe-dividends`` asks each
# symbol's ten years of KRX day candles adjusted and as traded (about 150 calls spaced half a
# second, a minute or two) and counts where their ratio steps, by size and month, with the steps'
# dates and the rows' ex-date flags. It prints counts, dates and flag values, no price, so it can go
# on the public issue. The half hour is a bound. Before the pre-validations.
DIVIDEND_PROBE_ISSUE = 310

DIVIDEND_PROBE = Job(
    name="probe-dividends-310",
    due=_first_night,
    command=("probe-dividends",),
    timeout=timedelta(minutes=30),
    targets=(
        Target(
            DIVIDEND_PROBE_ISSUE,
            note=(
                "에이전트가 이 출력으로 KIS 수정주가가 현금배당을 빼는지(배당락일마다 작은 계단이 있는지), "
                "KIS 일봉의 `flng_cls_code`가 배당락일을 알려 주는지 보고 `docs/kis-feeds.md`와 "
                '`docs/engine-api.md` "5거래일 시가→종가"의 배당에 남긴다. 빼지 않고 배당락 표시가 있으면 '
                "수집이 그 값을 KRX 일봉에 두고 정답 경로의 `ex_dividend`를 채우는 PR을 연다. 출력에는 "
                "가격이 없다(개수, 날짜, 필드 값, 계단 크기의 구간만)."
            ),
        ),
    ),
)

# The KRX Open API's ETF daily trading service (#305): KIS gives the products' net assets and
# listed units for today only (``probe-nav-305``), so the user chose KRX's ``etf_bydd_trd`` (ETF
# 일별매매정보) for the days before (2026-10-22). Its key is the user's to apply for; once it is in
# the server's ``.env`` (``KRX_OPENAPI_KEY``, ``Facts.krx_openapi``), once, on the first night
# (trading or not: KRX gives a day from 08:00 on the next business day): ``probe-krx-etf`` asks the
# day before the listing, the listing day, a day a week and the last five trading days (about 30
# calls spaced a second), and KIS's current price and daily NAV for each product (about 60 calls
# spaced half a second), a minute or two. It prints counts, dates, field names and looks, same or
# different, no market value (KRX's terms forbid giving the data to anyone else), so it can go on
# the public issue. It stops asking at 10 minutes; the quarter hour is a bound, kept short as it
# runs before the simulations and pre-validations. A night without the key it waits, harmlessly.
KRX_ETF_PROBE_ISSUE = 305


def _first_night_with_the_krx_key(night: Night) -> bool:
    """Once, on the first night ``KRX_OPENAPI_KEY`` is set, trading or not; again each later
    night while its last run failed (a key whose service is not approved yet answers 401), so
    fixing the application needs no edit of the state file."""
    if not night.facts.krx_openapi:
        return False
    record = night.record
    return not night.ran or (record is not None and record.outcome == FAILED and record.day < night.day)


KRX_ETF_PROBE = Job(
    name="probe-krx-etf-305",
    due=_first_night_with_the_krx_key,
    command=("probe-krx-etf",),
    timeout=timedelta(minutes=15),
    targets=(
        Target(
            KRX_ETF_PROBE_ISSUE,
            note=(
                "에이전트가 이 출력으로 `docs/krx-openapi.md`의 ⚠(주소와 인증, 필드와 모양, 종목코드, "
                "상장일부터의 행, 값이 나오는 때, 상장좌수가 날마다인지, 순자산총액의 단위, KIS와 같은지)를 "
                "실측으로 고친다. 401·403이면 인증키나 서비스 이용 신청을 사용자에게 묻는다. 그다음 "
                "상장일부터의 순자산 채우기(백필)와 날마다의 수집을 만든다. 출력에는 시세 값이 없다"
                "(개수, 날짜, 필드 이름과 모양, 같음·다름만)."
            ),
        ),
    ),
)

# The WebSocket's gaps day by day (#344): on 2026-10-21 the silence watchdog (#311) closed the
# connection at 15:30:33, no frame having come since 15:29:33, around the closing auction's print.
# Whether that silence comes at every close (the watchdog would then cut the connection there every
# day, and its 60 seconds need another look: the user's decision) only the recording shows. Once, on
# the first night after the deploy that brings it (trading or not: it reads the days already
# recorded): ``probe-gaps`` summarizes every recorded day's gaps and the closing auction window's
# frames, trades and bars. It reads files only and prints no market value, so it can go on the
# public issue. A minute or two; the quarter hour is a bound, kept short as it runs before the
# simulations and pre-validations, which must start by 00:30 (the Breimans by 23:30). It is listed
# after the other probes, so on a night they are due too it never holds back the results #314,
# #264, #305 and #310 wait on.
GAPS_PROBE_ISSUE = 344

GAPS_PROBE = Job(
    name="probe-gaps-344",
    due=_first_night,
    command=("probe-gaps",),
    timeout=timedelta(minutes=15),
    targets=(
        Target(
            GAPS_PROBE_ISSUE,
            note=(
                "에이전트가 이 출력으로 15:30 무렵의 침묵이 매일 종가 단일가 때 오는지(날마다 종가 단일가 "
                "구간의 가장 긴 침묵과 겹친 끊김), 그날 종가 단일가 체결과 1분봉 채움이 녹화에 있는지, "
                "끊김이 언제 무슨 이유로 몇 피드에 왔는지 #344에 정리한다. 60초 기준"
                "(`KRX_WS_SILENCE_SECONDS`)과 종가 단일가 구간의 처리를 바꿀지는 사용자가 정한다. "
                "출력에는 시세 값이 없다(개수, 시각, 코드, 피드 이름, 키만)."
            ),
        ),
    ),
)

# The closing flow (#305): when KIS moves its ETF current price to the new day (the net assets'
# retries, ``net_assets``), and which one-minute bar, the integrated or the KRX one, gives the
# closing auction value the recorded trades do (the denominator takes recorded prints only until
# then, ``closing_flow``), only the server shows. Once, on the first trading day's night after the
# deploy that brings it, so the morning's retries, the 15:20 row and the evening's days are in:
# ``check-closing-flow`` summarizes the net asset rows by day with when they first came, the 15:20
# rows, the days kept, and on each day with a recorded print both bars' values against it (the KRX
# bar asked of KIS, one call a symbol and day, half a second apart). It prints no market value.
# Seconds; the quarter hour is a bound. After the gaps probe.
CLOSING_FLOW_ISSUE = 305

CLOSING_FLOW_CHECK = Job(
    name="check-closing-flow-305",
    due=_first_trading_night,
    command=("check-closing-flow",),
    timeout=timedelta(minutes=15),
    targets=(
        Target(
            CLOSING_FLOW_ISSUE,
            note=(
                "에이전트가 이 출력으로 KIS가 ETF 순자산을 언제 다음 날로 넘기는지(날마다 처음 받은 때)와 "
                "순자산을 쌓기 시작한 첫날, 통합(UN)·KRX(J) 1분봉의 단일가 봉이 녹화한 종가 단일가 "
                '체결대금과 같은지 보고 `docs/kis-feeds.md` "ETF 순자산 (#305)"의 ⚠를 고친다. 녹화와 늘 같은 '
                "봉이 있으면 그 봉으로 녹화가 없는 날의 분모를 채우는 PR을 연다(`docs/engine-api.md` "
                '"closing-flow"의 ⚠). 출력에는 시세 값이 없다(개수, 날짜, 시각, 봉과 녹화의 체결대금 비율만).'
            ),
        ),
    ),
)

# The night futures (#264): the collector records the KOSPI200 night session's trades and book
# (18:00–06:00) from the deploy that brings it; what only the server shows is whether KIS
# streams them for this app key, under the contract code asked for, in the layout of KIS's
# example. Once, on the first night after a night the collector subscribed for from its start
# (the evening after the first trading day's night): ``probe-night`` reads that night's files,
# and the masters. It prints counts, times, field names and codes, no market value, so it can
# go on the public issue. One night's frames take seconds to read; the half hour is a bound.
# Before the pre-validations.
NIGHT_PROBE_ISSUE = 264


def _after_a_subscribed_night(night: Night) -> bool:
    """Once, on the first night after one the night futures were subscribed for from 18:00."""
    return not night.ran and any(day < night.day for day in night.facts.night_futures)


NIGHT_PROBE = Job(
    name="probe-night-264",
    due=_after_a_subscribed_night,
    command=("probe-night",),
    timeout=timedelta(minutes=30),
    targets=(
        Target(
            NIGHT_PROBE_ISSUE,
            note=(
                '에이전트가 이 출력으로 `docs/kis-feeds.md` "야간선물 (#264)"의 ⚠(종목코드 규칙, '
                "실제로 오는지, 필드 배치, 시각 형식)를 고친다. 출력에는 시세 값이 없다"
                "(개수, 시각, 필드 이름, 종목코드만)."
            ),
        ),
    ),
)

# The auctions' expected fill (#304): the pre-open briefing shows KRX's expected opening price
# from 08:30 to 09:00, read from the integrated book's ANTC_* fields, which the collector
# normalizes from the deploy that brings it. The raw layer kept every frame before that, so the
# days already recorded show whether the feed carries the price then, and whether it is KRX's
# auction's (against the KRX day candle's open and close, in ticks). Once, on the first trading
# day's night, so that day's normalized rows show the new columns too. It prints counts, times,
# codes and tick differences, no market value, so it can go on the public issue. Ten days of
# raw book frames take a few minutes to read; the half hour is a bound. Before the
# pre-validations.
# The result goes to #314 (the follow-up of #304, which closed with the screen): what to do with
# it is written there.
EXPECTED_OPEN_ISSUE = 314

EXPECTED_OPEN_PROBE = Job(
    name="probe-expected-open-314",
    due=_first_trading_night,
    command=("probe-expected-open",),
    timeout=timedelta(minutes=30),
    targets=(
        Target(
            EXPECTED_OPEN_ISSUE,
            note=(
                "에이전트가 이 출력으로 통합 호가의 예상 체결(`ANTC_*`)이 08:30–09:00에 오는지, KRX 시가 "
                '단일가의 것인지 보고 `docs/events.md` "호가"의 예상 체결 열 설명과 `docs/kis-feeds.md`에 '
                '남긴다. 장전 브리핑(#304)의 예상 시가는 그 결과를 따른다(이 이슈의 "에이전트가 할 일"). '
                "출력에는 시세 값이 없다(개수, 시각, 코드, 틱 차이만)."
            ),
        ),
    ),
)

# The same probe again, KRX's own (#314). The first run (the night of 2026-10-20) counted section
# 4's times and section 5's gaps over every venue code; for 009150 the opening auction's window
# also carried NXT's '2', so its gaps to the open may be NXT's. From the deploy that brings this,
# sections 4 and 5 are KRX's ('1') only and the other codes' times are shown apart. Once, on the
# first trading day's night after it, over the latest recorded days; the half hour as above.
EXPECTED_OPEN_KRX_PROBE = Job(
    name="probe-expected-open-krx-314",
    due=_first_trading_night,
    command=("probe-expected-open",),
    timeout=timedelta(minutes=30),
    targets=(
        Target(
            EXPECTED_OPEN_ISSUE,
            note=(
                "첫 출력(2026-10-20 밤)의 4·5절은 거래소 구분 코드와 상관없이 셌다. 이번 4·5절은 "
                "KRX(`1`) 것만 세고, 다른 코드의 시각은 괄호로 따로 보인다. 에이전트가 이 출력으로 "
                '009150의 시가 차이를 고치고 `docs/kis-feeds.md` "피드별 메모"의 "거르지 않은 값" 표시를 '
                "지운다(005930·000660은 그 구간에 `1`만 있었다). 종가 쪽 차이는 #342가 다시 받은 "
                "일봉과 견준 것이다. 그날 저녁 저장한 날만 잠정 종가이므로 그날을 빼고 "
                '"종가 쪽" 문단을 고친다. 출력에는 시세 값이 없다'
                "(개수, 시각, 코드, 틱 차이만)."
            ),
        ),
    ),
)

# ⑤ The pre-validations (#204): a new algorithm runs live once its pre-validation has passed
# (``prevalidation.py``). Once ``MIN_DAYS`` trading days are recorded, one waiting algorithm
# is pre-validated a night, at most (``prevalidation_tonight``): a run replays 20 days beside
# the live engine, on a 2 GB server with burst CPU. They go in this order, then any other
# waiting algorithm in the registry's. A verdict on the code and the target an algorithm runs
# now (passed, or refused) is not asked for again: a deploy that changes its code, or the
# target, asks again. A run that left no verdict on them (it crashed, or hit its time limit)
# runs again after a new deploy, once the algorithms not tried yet have had their turn. A run
# a restart cut runs again the next night (the scheduler).
PREVALIDATE_FIRST = ("mandelbrot", "breiman", "kalman", "shannon", "poisson")
PREVALIDATION_ISSUE = 204
# A run takes about 20 times a day's replay (20 days, the algorithm retrained before each);
# breiman retrains LightGBM each day and takes longest, and so does each Breiman of the
# barrier's horizon ladder (#263). Bounds, not estimates: room for the burst CPU running out.
# Each must start by 05:30 less its limit (00:30, the Breimans 23:30).
PREVALIDATION_TIMEOUT = timedelta(hours=5)
PREVALIDATION_TIMEOUTS = {
    name: timedelta(hours=6) for name in ("breiman", "breiman-15m", "breiman-30m", "breiman-60m")
}
# An algorithm's turn tonight: never tried (or cut by a restart), or tried again.
FRESH, AGAIN = "fresh", "again"


def prevalidation_job_name(algorithm: str) -> str:
    return f"prevalidate-{algorithm}"


def prevalidation_order(names: Sequence[str]) -> tuple[str, ...]:
    """The algorithms to pre-validate, in turn: those of ``PREVALIDATE_FIRST`` registered, then
    any other registered after pre-validation existed (``names``: the registries' order)."""
    new = [name for name in names if name not in REGISTERED_BEFORE]
    return (*(n for n in PREVALIDATE_FIRST if n in new), *(n for n in new if n not in PREVALIDATE_FIRST))


def prevalidation_turn(night: Night, algorithm: str) -> str | None:
    """Whether ``algorithm`` would be pre-validated tonight: FRESH, AGAIN (its last run left
    no verdict, on older code: it waits for the FRESH ones), or None (not tonight)."""
    facts = night.facts
    if facts.recorded_days < MIN_DAYS or algorithm not in facts.waiting:
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


def prevalidation_tonight(night: Night, order: Sequence[str]) -> str | None:
    """The algorithm pre-validated tonight, of ``order``: none if one was tonight already
    (whatever came of it); else the first FRESH, else the first AGAIN."""
    if any(
        (record := night.records.get(prevalidation_job_name(name))) is not None and record.day == night.day
        for name in order
    ):
        return None
    turns = [(name, prevalidation_turn(night, name)) for name in order]
    return next((n for n, t in turns if t == FRESH), None) or next((n for n, t in turns if t == AGAIN), None)


def _prevalidation_due(algorithm: str, order: tuple[str, ...]) -> Callable[[Night], bool]:
    def due(night: Night) -> bool:
        return prevalidation_tonight(night, order) == algorithm

    return due


def prevalidation_jobs(names: Sequence[str]) -> tuple[Job, ...]:
    """A pre-validation job for each algorithm of ``names`` (the registries) registered after
    pre-validation existed, in ``prevalidation_order``."""
    order = prevalidation_order(names)
    return tuple(
        Job(
            name=prevalidation_job_name(name),
            due=_prevalidation_due(name, order),
            command=("prevalidate", name),
            timeout=PREVALIDATION_TIMEOUTS.get(name, PREVALIDATION_TIMEOUT),
            targets=(Target(PREVALIDATION_ISSUE, note=_prevalidation_note(name)),),
            attach=(Attachment(f"리포트 state/{_report_of(name)}", _report_of(name)),),
        )
        for name in order
    )


def _prevalidation_note(algorithm: str) -> str:
    """The note on an algorithm's pre-validation result: what passing and refusing do, and
    what to do with what it measured besides the checks (``_DIAGNOSTICS_NOTES``)."""
    return " ".join(n for n in (_PREVALIDATION_NOTE, _DIAGNOSTICS_NOTES.get(algorithm)) if n)


# The daily kinds' algorithms' pre-validations (#264): a walk-forward over ten years of KRX day
# candles (``daily_training``), not a replay of the recorded days, so neither the recorded days'
# count nor the one-a-night turn of the bar-close ones holds them back: each runs on the first
# night it waits without a verdict on the code and inputs it runs (and again once a deploy changes
# them, or after a run that left no verdict, on newer code). While the data of one of its
# features is behind the feature's version on this server (#297: KRX's net buys before NXT, until
# the keeper has stored them), it waits: a run then would be judged on the old data. Minutes, not
# hours; the hour is a bound. Before the bar-close pre-validations, which may take the rest of the
# night. Its result goes to #204, with them, and to its daily kind's issue (``DAILY_KIND_ISSUES``:
# open_to_close's #264, open_to_close_5d's #310, whose review days wait for the variance ratio
# its first pre-validation measures).
DAILY_PREVALIDATION_TIMEOUT = timedelta(hours=1)
DAILY_KIND_ISSUES = {"open_to_close": DAILY_CHECK_ISSUE, "open_to_close_5d": 310}


def _daily_prevalidation_due(
    algorithm: str, features: Sequence[str] = (), kind: str | None = None
) -> Callable[[Night], bool]:
    of_kind = daily_kind_named(kind) if kind is not None else None

    def due(night: Night) -> bool:
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
) -> tuple[Job, ...]:
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
                command=("prevalidate", name),
                timeout=DAILY_PREVALIDATION_TIMEOUT,
                targets=(
                    Target(PREVALIDATION_ISSUE, note=note),
                    Target(DAILY_KIND_ISSUES.get(kind or "", DAILY_CHECK_ISSUE), note=note),
                ),
                attach=(Attachment(f"리포트 state/{_report_of(name)}", _report_of(name)),),
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
        "종류의 판정일은 이 실행이 잰 분산 배율(VR, 리포트의 `walk_forward.overlap`)로 규칙에 따라 정해 "
        "코드에 고정한다. 그때까지는 통과(`passed`)해도 수습에 오르지 않는다(`review_hold`). 에이전트가 "
        'VR과 규칙이 주는 판정일(`review_days`)로 판정일을 고정하는 PR을 연다(#310 "상태 판정"). '
        "거부(`refused`)되면 같은 코드로는 다시 돌지 않는다."
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
# (``Facts.pending_answers``, #264: ``open_to_close``'s closing auction prices).
SIMULATION_ISSUE = 298
DAILY_SIMULATION_TIMEOUT = timedelta(hours=1)


def _daily_simulation_due(
    algorithms: Sequence[DailyAlgorithm], kinds: Sequence[DailyKind] = DAILY_KINDS
) -> Callable[[Night], bool]:
    held = {k.name for k in kinds if k.review_hold is not None}

    def due(night: Night) -> bool:
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
    command=("simulate", "daily"),
    timeout=DAILY_SIMULATION_TIMEOUT,
    targets=(
        Target(
            SIMULATION_ISSUE,
            note=(
                "하루 한 번 예측 종류의 수습 시뮬레이션(#298): 그 종류의 첫 판정일만큼의 최근 거래일"
                "(`open_to_close`는 20일)을 그 알고리즘이 수습으로 돌았다고 치고 다시 재생했다(날마다 그 "
                "전날까지로 다시 학습). 그 판정을 바로 했다. 통과하면 지금부터 정식이고, 못 하면 수습이 "
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
# the night to a pre-validation. It goes before the pre-validations: it can make algorithms already
# running official at once, and a pre-validation that would not end by 05:30 waits a night. A run
# that failed waits for new code, as a pre-validation that left no verdict does. The limit is a
# pre-validation's: up to 20 days' replay (without its 20 retrainings), plus the burst CPU running
# out; it starts by 00:30. Its result goes to #298 and #204.
SIMULATION_TIMEOUT = timedelta(hours=5)


def _bar_close_simulation_due(night: Night) -> bool:
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
    command=("simulate", "bar-close"),
    timeout=SIMULATION_TIMEOUT,
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
    "통과(`passed`)하면 엔진이 다음 봉 마감부터 수습으로 돌리고, 수습이 시작된 그 밤 "
    "`simulate-bar-close-298`이 최근 녹화일로 수습을 시뮬레이션해 바로 판정한다(#298). "
    "거부(`refused`)되면 같은 코드·정답 버전으로는 다시 돌지 않고, 고친 코드가 배포되면 다시 돈다."
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


JOBS: tuple[Job, ...] = (
    DIAGNOSE,
    SIGNAL_REVIEW,
    SUPPLY_PROBE,
    STRUCTURE_CHECK,
    ENTRY_DELAY,
    NIGHT_PROBE,
    EXPECTED_OPEN_PROBE,
    EXPECTED_OPEN_KRX_PROBE,
    DAILY_CHECK,
    STRUCTURE_SUPPLY,
    NET_BUYS_PROBE,
    CLOSE_PROBE,
    CLOSE_REFRESH_PROBE,
    NAV_PROBE,
    DIVIDEND_PROBE,
    KRX_ETF_PROBE,
    GAPS_PROBE,
    CLOSING_FLOW_CHECK,
    *daily_prevalidation_jobs(
        [a.name for a in DAILY_ALGORITHMS],
        {a.name: a.features for a in DAILY_ALGORITHMS},
        {a.name: a.kind for a in DAILY_ALGORITHMS},
    ),
    DAILY_SIMULATION,
    BAR_CLOSE_SIMULATION,
    *prevalidation_jobs([*(a.name for a in ALGORITHMS), *(a.name for a in KIND_ALGORITHMS)]),
)
