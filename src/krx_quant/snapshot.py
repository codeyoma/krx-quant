"""Server snapshots on request (#322): an issue labelled ``needs-server-snapshot`` gets the
server's summary (``krx-quant doctor``, ``doctor.py``) as a comment, and loses the label.

An agent cannot reach the server; it reads issues. So the collector looks every
``KRX_SNAPSHOT_EVERY`` (3 minutes) for the open issues of ``KRX_GITHUB_REPO`` that carry the
label, posts the summary on each, masked (``redaction.py``), with the scheduled jobs' GitHub
client (``github.py``, ``KRX_GITHUB_TOKEN``), then takes the label off. Only the label asks:
an issue's title, body and comments are anyone's and are never read, and no other label does
anything. Pull requests are left out. Only people who may triage the repository can label.

Limits: an issue gets at most one snapshot in ``KRX_SNAPSHOT_ISSUE_MINUTES`` (10), the server
at most ``KRX_SNAPSHOT_PER_HOUR`` (6) in any hour; an issue over either keeps its label and is
served once it may be. When posts were made is kept (``state/snapshots.json``), so a restart
does not reset the limits, with the issues whose comment is up but whose label could not be
taken off: the next look only takes it off, without a second comment. The label is made at
the first look if the repository lacks it.

Nothing here stops the collector: a GitHub that refuses or does not answer is logged and
looked at again next time, a summary that fails or takes over a minute is posted as such,
and an unexpected error ends that look only.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timedelta
from pathlib import Path

import aiohttp

from krx_quant.config import Settings
from krx_quant.feeds.base import KST
from krx_quant.github import GitHub, GitHubError, comment_body
from krx_quant.redaction import redact, secrets_of

log = logging.getLogger(__name__)

LABEL = "needs-server-snapshot"
LABEL_COLOR = "0e8a16"
LABEL_DESCRIPTION = "The server posts its status (krx-quant doctor) here in a few minutes, then removes this"
STATE_FILE = "snapshots.json"
HOUR = timedelta(hours=1)
SNAPSHOT_SECONDS = 60.0  # a summary taking longer is given up, and said so
LIST_LIMIT = 20  # issues read a look; more wait for the next

Snapshot = Callable[[], Awaitable[str]]


class SnapshotState:
    """``state/snapshots.json``: when each snapshot was posted, and the issues whose label is
    still to come off."""

    def __init__(
        self, path: Path, posted: list[tuple[int, datetime]] | None = None, unlabel: set[int] | None = None
    ) -> None:
        self.path = path
        self.posted = posted or []
        self.unlabel = unlabel or set()

    @classmethod
    def load(cls, path: Path) -> SnapshotState:
        """As saved; empty if there is none or it cannot be read (the limits then start afresh)."""
        try:
            data = json.loads(path.read_text())
            posted = [(int(p["issue"]), datetime.fromisoformat(p["at"])) for p in data.get("posted", [])]
            return cls(path, posted, {int(n) for n in data.get("unlabel", [])})
        except FileNotFoundError:
            return cls(path)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            log.error("cannot read %s (%s): the server snapshots' limits start afresh", path, e)
            return cls(path)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "posted": [{"issue": issue, "at": at.isoformat()} for issue, at in self.posted],
            "unlabel": sorted(self.unlabel),
        }
        tmp = self.path.with_name(f".{self.path.name}.tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self.path)

    def last(self, issue: int) -> datetime | None:
        return max((at for n, at in self.posted if n == issue), default=None)

    def since(self, at: datetime) -> int:
        return sum(1 for _, posted in self.posted if posted > at)

    def forget_before(self, at: datetime) -> None:
        self.posted = [(n, posted) for n, posted in self.posted if posted > at]


class SnapshotPoller:
    """Looks for the labelled issues and answers each with a snapshot, within the limits."""

    def __init__(
        self,
        github: GitHub,
        snapshot: Snapshot,
        state: SnapshotState,
        now: Callable[[], datetime],
        *,
        issue_every: timedelta,
        per_hour: int,
        every: timedelta,
        secrets: Sequence[str] = (),
        commit: str = "unknown",
    ) -> None:
        self._github = github
        self._snapshot = snapshot
        self._state = state
        self._now = now
        self._issue_every = issue_every
        self._per_hour = per_hour
        self._every = every
        self._secrets = tuple(secrets)
        self._commit = commit
        self._label_ready = False
        self._label_warned = False
        self._failing = False

    async def run(self) -> None:
        """A look every ``every``, until cancelled; the first one ``every`` after the start."""
        while True:
            await asyncio.sleep(self._every.total_seconds())
            try:
                await self.poll()
            except Exception:
                log.exception("the server snapshot look failed")

    async def poll(self) -> list[int]:
        """One look: the issues a snapshot was posted on."""
        await self._make_label()
        try:
            issues = await self._github.labelled(LABEL, LIST_LIMIT)
        except GitHubError as e:
            self._failed(e)
            return []
        if self._failing:
            self._failing = False
            log.info("GitHub answers again: server snapshots on request go on")
        now = self._now()
        self._state.forget_before(now - max(HOUR, self._issue_every))
        if self._state.unlabel - set(issues):  # the label came off some other way
            self._state.unlabel &= set(issues)
            self._state.save()
        posted: list[int] = []
        report: str | None = None
        for issue in issues:
            if issue in self._state.unlabel:
                await self._take_label_off(issue)  # its comment is up already
                continue
            last = self._state.last(issue)
            if last is not None and now - last < self._issue_every:
                log.info(
                    "server snapshot on #%s waits until %s (one in %s)",
                    issue,
                    last + self._issue_every,
                    self._issue_every,
                )
                continue
            if self._state.since(now - HOUR) >= self._per_hour:
                log.info("server snapshots: %d in the last hour already; the rest wait", self._per_hour)
                break
            if report is None:
                report = await self._report()
            try:
                url = await self._github.comment(issue, self._body(now, report))
            except GitHubError as e:
                self._failed(e)
                break
            self._state.posted.append((issue, now))
            self._state.unlabel.add(issue)
            self._state.save()  # counted, and not posted twice if the label stays on
            log.info("server snapshot posted on #%s: %s", issue, url)
            posted.append(issue)
            await self._take_label_off(issue)
        return posted

    async def _make_label(self) -> None:
        if self._label_ready:
            return
        try:
            made = await self._github.ensure_label(LABEL, LABEL_COLOR, LABEL_DESCRIPTION)
        except GitHubError as e:
            if not self._label_warned:
                self._label_warned = True
                log.warning(
                    "could not make sure the %s label exists (%s): make it once by hand (docs/operations.md)",
                    LABEL,
                    redact(str(e), self._secrets),
                )
            return
        self._label_ready = True
        if made:
            log.info("made the %s label", LABEL)

    async def _take_label_off(self, issue: int) -> None:
        try:
            await self._github.relabel(issue, (), (LABEL,))
        except GitHubError as e:
            log.warning("server snapshot on #%s: the label stays on (%s); taken off next time", issue, e)
            return
        self._state.unlabel.discard(issue)
        self._state.save()

    async def _report(self) -> str:
        try:
            text = await asyncio.wait_for(self._snapshot(), SNAPSHOT_SECONDS)
        except Exception as e:
            log.exception("could not make the server snapshot")
            text = f"서버 요약을 만들지 못했다: {type(e).__name__}: {e}"
        return redact(text, self._secrets)

    def _body(self, now: datetime, report: str) -> str:
        commit = self._commit if self._commit == "unknown" else self._commit[:12]
        minutes = round(self._issue_every.total_seconds() / 60)
        every = round(self._every.total_seconds() / 60)
        heading = (
            f"**서버 상태 (`krx-quant doctor`)** · {now.astimezone(KST):%Y-%m-%d %H:%M} KST · 커밋 `{commit}`"
        )
        notes = [
            f"`{LABEL}` 라벨을 보고 올린 요약이다. 라벨은 뗐다. 다시 보려면 라벨을 다시 붙인다"
            f"(서버가 {every}분마다 본다. 이슈마다 {minutes}분에 한 번, 서버 전체로 1시간에 "
            f"{self._per_hour}번까지).",
            "서버에서 직접 보려면: `docker compose exec engine krx-quant doctor`",
        ]
        return comment_body(heading, report, notes=notes)

    def _failed(self, e: GitHubError) -> None:
        if not self._failing:
            self._failing = True
            log.warning(
                "server snapshots: GitHub failed (%s); looked at again next time",
                redact(str(e), self._secrets),
            )


async def keep_snapshots(
    settings: Settings, session: aiohttp.ClientSession, snapshot: Snapshot, now: Callable[[], datetime]
) -> None:
    """The collector's look for snapshot requests (``KRX_SNAPSHOT_EVERY``), until cancelled."""
    assert settings.snapshot_every is not None
    github = GitHub(session, settings.github_token, settings.github_repo, settings.github_api_url)
    if not github.configured:
        log.warning("KRX_GITHUB_TOKEN is not set: no server snapshots on request (%s)", LABEL)
        return
    poller = SnapshotPoller(
        github,
        snapshot,
        SnapshotState.load(settings.state_dir / STATE_FILE),
        now,
        issue_every=timedelta(minutes=settings.snapshot_issue_minutes),
        per_hour=settings.snapshot_per_hour,
        every=settings.snapshot_every,
        secrets=secrets_of(settings),
        commit=settings.commit,
    )
    await poller.run()
