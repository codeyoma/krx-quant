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
order, each in its own process, as the nightly training does. A job's condition is asked
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
import sys
from asyncio.subprocess import DEVNULL, PIPE
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from time import monotonic
from typing import Any

import aiohttp

from krx_quant.algorithms import ALGORITHMS, KIND_ALGORITHMS, Algorithm, KindAlgorithm
from krx_quant.clock import SessionClock
from krx_quant.config import JOBS_UNTIL, Settings
from krx_quant.engine import PredictionEngine
from krx_quant.feeds.base import KST
from krx_quant.github import GitHub, GitHubError, comment_body
from krx_quant.market_calendar import MarketCalendar
from krx_quant.notify import Alerts
from krx_quant.prevalidation import MIN_DAYS, REGISTERED_BEFORE, LastReport, last_report, report_path
from krx_quant.redaction import redact, secrets_of
from krx_quant.swap import NightTraining
from krx_quant.training import finished_days, is_recorded

log = logging.getLogger(__name__)

STATE_FILE = "scheduled.json"
OK, FAILED, INTERRUPTED, RUNNING = "ok", "failed", "interrupted", "running"
READY_FOR_AGENT, READY_FOR_HUMAN, NEEDS_TRIAGE = "ready-for-agent", "ready-for-human", "needs-triage"
# A job's arguments follow this, as the nightly training's do.
KRX_QUANT = (sys.executable, "-m", "krx_quant.cli")
# How long a killed job's output is still read for (a process it started may hold the pipe).
_DRAIN_SECONDS = 5.0

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


@dataclass(frozen=True)
class Ran:
    """A job's process, once it ended."""

    exit_code: int | None  # None: it could not be started
    stdout: str
    stderr: str
    timed_out: bool = False


Runner = Callable[[Sequence[str], timedelta], Awaitable[Ran]]
Notify = Callable[[str], Awaitable[bool]]


async def run_command(args: Sequence[str], timeout: timedelta, program: Sequence[str] = KRX_QUANT) -> Ran:
    """Run ``program`` with ``args`` and read its output; killed at ``timeout`` (what it wrote
    by then is kept), and when the collector stops (cancelled)."""
    process = await asyncio.create_subprocess_exec(*program, *args, stdin=DEVNULL, stdout=PIPE, stderr=PIPE)
    assert process.stdout is not None and process.stderr is not None
    out, err = bytearray(), bytearray()
    readers = [
        asyncio.create_task(_drain(process.stdout, out)),
        asyncio.create_task(_drain(process.stderr, err)),
    ]
    # The process's end is timed too: one that closes its output early still stops at the limit.
    ended = asyncio.create_task(process.wait())
    try:
        _, pending = await asyncio.wait([*readers, ended], timeout=timeout.total_seconds())
        if pending:
            _kill(process)
            await asyncio.wait(readers, timeout=_DRAIN_SECONDS)
        code = await ended
    except BaseException:  # cancelled: the job stops with the collector
        _kill(process)
        raise
    finally:
        for task in (*readers, ended):
            task.cancel()
    return Ran(code, out.decode(errors="replace"), err.decode(errors="replace"), timed_out=bool(pending))


async def _drain(stream: asyncio.StreamReader, sink: bytearray) -> None:
    while chunk := await stream.read(1 << 16):
        sink += chunk


def _kill(process: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError):
        process.kill()


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
            ran = await self._runner(job.command, job.timeout)
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
    state_dir: Path, algorithms: Sequence[Algorithm | KindAlgorithm], target_version: int
) -> dict[str, LastReport]:
    """The latest pre-validation report of each of ``algorithms`` that has one, as it stands for
    the code and target it runs now."""
    reports = {a.name: last_report(state_dir, a, target_version) for a in algorithms}
    return {name: report for name, report in reports.items() if report is not None}


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

    async def facts() -> Facts:
        recorded = await asyncio.to_thread(recorded_days, settings.data_dir, settings.symbols, clock.now())
        everyone: list[Algorithm | KindAlgorithm] = [*engine.registered, *engine.kind_registered]
        reports = await asyncio.to_thread(
            prevalidation_reports, settings.state_dir, everyone, settings.target_version
        )
        return Facts(recorded, tuple(engine.waiting()), len(engine.members()), reports)

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
        facts=facts,
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
# an estimate.
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

# ② The pre-validations (#204): a new algorithm runs live once its pre-validation has passed
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
# breiman retrains LightGBM each day and takes longest. Bounds, not estimates: room for the
# burst CPU running out. Each must start by 05:30 less its limit (00:30, breiman 23:30).
PREVALIDATION_TIMEOUT = timedelta(hours=5)
PREVALIDATION_TIMEOUTS = {"breiman": timedelta(hours=6)}
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
            targets=(Target(PREVALIDATION_ISSUE, note=_PREVALIDATION_NOTE),),
            attach=(Attachment(f"리포트 state/{_report_of(name)}", _report_of(name)),),
        )
        for name in order
    )


_PREVALIDATION_NOTE = (
    "통과(`passed`)하면 엔진이 다음 봉 마감부터 수습으로 돌린다. 거부(`refused`)되면 같은 코드·정답 "
    "버전으로는 다시 돌지 않고, 고친 코드가 배포되면 다시 돈다."
)


def _report_of(algorithm: str) -> str:
    """The report's path under the state directory."""
    return report_path(Path(), algorithm).as_posix()


JOBS: tuple[Job, ...] = (
    DIAGNOSE,
    *prevalidation_jobs([*(a.name for a in ALGORITHMS), *(a.name for a in KIND_ALGORITHMS)]),
)
