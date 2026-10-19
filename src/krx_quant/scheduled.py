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
from functools import partial
from pathlib import Path
from time import monotonic
from typing import Any

import aiohttp

from krx_quant.algorithms import ALGORITHMS, DAILY_ALGORITHMS, KIND_ALGORITHMS, Algorithm, KindAlgorithm
from krx_quant.clock import SessionClock
from krx_quant.config import JOBS_UNTIL, Settings
from krx_quant.daily import DailyAlgorithm
from krx_quant.daily_features import pending_inputs
from krx_quant.engine import PredictionEngine
from krx_quant.feeds.base import KST
from krx_quant.github import GitHub, GitHubError, comment_body
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
    # The trading days of the week before the night whose night session (18:00–06:00) the
    # collector subscribed the KOSPI200 night futures for before it began (#264).
    night_futures: tuple[date, ...] = ()
    # The daily features whose data on this server is behind their version (#297,
    # ``daily_features.pending_inputs``): a pre-validation reading them waits for it.
    pending_inputs: tuple[str, ...] = ()
    # The daily algorithms on a probation that waits for its simulation (#298,
    # ``PredictionEngine.to_simulate``).
    simulating_daily: tuple[str, ...] = ()
    # The bar-close kinds' algorithms on a probation that waits for its simulation (#298).
    simulating: tuple[str, ...] = ()


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
    )


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
EXPECTED_OPEN_ISSUE = 304

EXPECTED_OPEN_PROBE = Job(
    name="probe-expected-open-304",
    due=_first_trading_night,
    command=("probe-expected-open",),
    timeout=timedelta(minutes=30),
    targets=(
        Target(
            EXPECTED_OPEN_ISSUE,
            note=(
                "에이전트가 이 출력으로 통합 호가의 예상 체결(`ANTC_*`)이 08:30–09:00에 오는지, KRX 시가 "
                '단일가의 것인지 보고 `docs/events.md` "호가"의 예상 체결 열 설명과 `docs/kis-feeds.md`에 '
                "남긴다. 장전 브리핑(#304)의 예상 시가는 그 결과를 따른다. 출력에는 시세 값이 없다"
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
            targets=(Target(PREVALIDATION_ISSUE, note=_PREVALIDATION_NOTE),),
            attach=(Attachment(f"리포트 state/{_report_of(name)}", _report_of(name)),),
        )
        for name in order
    )


# The daily kinds' algorithms' pre-validations (#264): a walk-forward over ten years of KRX day
# candles (``daily_training``), not a replay of the recorded days, so neither the recorded days'
# count nor the one-a-night turn of the bar-close ones holds them back: each runs on the first
# night it waits without a verdict on the code and inputs it runs (and again once a deploy changes
# them, or after a run that left no verdict, on newer code). While the data of one of its
# features is behind the feature's version on this server (#297: KRX's net buys before NXT, until
# the keeper has stored them), it waits: a run then would be judged on the old data. Minutes, not
# hours; the hour is a bound. Before the bar-close pre-validations, which may take the rest of the
# night. Its result goes to #204, with them, and to #264, the daily kind's issue.
DAILY_PREVALIDATION_TIMEOUT = timedelta(hours=1)


def _daily_prevalidation_due(algorithm: str, features: Sequence[str] = ()) -> Callable[[Night], bool]:
    def due(night: Night) -> bool:
        facts = night.facts
        if algorithm not in facts.waiting:
            return False
        if set(features) & set(facts.pending_inputs):
            return False  # its data is not all in yet (#297): it would be judged on the old
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
    names: Sequence[str], features: Mapping[str, Sequence[str]] | None = None
) -> tuple[Job, ...]:
    """A pre-validation job for each daily algorithm of ``names`` (#264)."""
    return tuple(
        Job(
            name=prevalidation_job_name(name),
            due=_daily_prevalidation_due(name, (features or {}).get(name, ())),
            command=("prevalidate", name),
            timeout=DAILY_PREVALIDATION_TIMEOUT,
            targets=(
                Target(PREVALIDATION_ISSUE, note=_DAILY_PREVALIDATION_NOTE),
                Target(DAILY_CHECK_ISSUE, note=_DAILY_PREVALIDATION_NOTE),
            ),
            attach=(Attachment(f"리포트 state/{_report_of(name)}", _report_of(name)),),
        )
        for name in names
    )


_DAILY_PREVALIDATION_NOTE = (
    "하루 한 번 예측 종류의 알고리즘(#264)의 사전 검증: KRX 일봉 10년의 walk-forward다. 통과(`passed`)하면 "
    "그 밤 `simulate-daily-298`이 수습에 올리고 최근 20거래일을 시뮬레이션해 바로 판정한다(#298, 판정은 "
    "20·40·60일째). 거부(`refused`)되면 같은 코드로는 다시 돌지 않는다."
)


# The daily kinds' simulated probations (#298, ``simulation.py``): a daily algorithm starts on
# probation with its latest 20 trading days replayed (refitted before each, as its nights would),
# judged at once. The job runs right after the daily pre-validations: on the night one passes, it
# enrolls the algorithm on probation (the engine would at its next start) and simulates it, so it
# is active by the next 08:50 if it passes. It also runs on a night a daily probation waits for it
# (begun by the engine, or a run cut short). Once a night; a run that failed waits for new code (or
# a new pass), as a pre-validation that left no verdict does. Cox fits in seconds: 20 fits and ten
# years of samples are minutes; the hour is a bound. Its result goes to #298 and #264.
SIMULATION_ISSUE = 298
DAILY_SIMULATION_TIMEOUT = timedelta(hours=1)


def _daily_simulation_due(names: Sequence[str]) -> Callable[[Night], bool]:
    def due(night: Night) -> bool:
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
        if not night.facts.simulating_daily:
            return False
        return not (record is not None and record.outcome == FAILED and record.commit == night.commit)

    return due


DAILY_SIMULATION = Job(
    name="simulate-daily-298",
    due=_daily_simulation_due([a.name for a in DAILY_ALGORITHMS]),
    command=("simulate", "daily"),
    timeout=DAILY_SIMULATION_TIMEOUT,
    targets=(
        Target(
            SIMULATION_ISSUE,
            note=(
                "하루 한 번 예측 종류의 수습 시뮬레이션(#298): 최근 20거래일을 그 알고리즘이 수습으로 "
                "돌았다고 치고 다시 재생했다(날마다 그 전날까지로 다시 학습). 그 판정을 바로 했다. 통과하면 "
                "지금부터 정식이고, 못 하면 수습이 이어진다(다음 판정 40일째). 출력에는 시세 값이 없다"
                "(날짜, 개수, lift만)."
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
    DAILY_CHECK,
    STRUCTURE_SUPPLY,
    NET_BUYS_PROBE,
    *daily_prevalidation_jobs(
        [a.name for a in DAILY_ALGORITHMS], {a.name: a.features for a in DAILY_ALGORITHMS}
    ),
    DAILY_SIMULATION,
    BAR_CLOSE_SIMULATION,
    *prevalidation_jobs([*(a.name for a in ALGORITHMS), *(a.name for a in KIND_ALGORITHMS)]),
)
