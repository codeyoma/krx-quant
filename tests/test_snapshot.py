"""Server snapshots on request (#322): an issue labelled ``needs-server-snapshot`` gets the
server's summary as a comment, and loses the label; within limits, and never at the
collector's cost.

The secret-looking values here are put together at run time, so the repository's own check
(``scripts/check_forbidden.py``) does not find them in this file.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import aiohttp
import pytest

from krx_quant.collector import run_collector
from krx_quant.config import Settings, SettingsError
from krx_quant.fake_kis import FakeKis
from krx_quant.feeds.base import KST
from krx_quant.github import GitHub
from krx_quant.redaction import MASK
from krx_quant.snapshot import (
    LABEL,
    STATE_FILE,
    SnapshotPoller,
    SnapshotState,
    keep_snapshots,
)
from tests.fake_github import REPO, TOKEN, FakeGitHub, fake_github
from tests.test_collector import settings_for

START = datetime(2026, 10, 20, 10, 0, tzinfo=KST)
SUMMARY = "서버 상태 (가짜 요약)\n1. 엔진\n"
SECRET = "github_pat_" + "B" * 22 + "_" + "C" * 59


class Clock:
    def __init__(self) -> None:
        self.at = START

    def __call__(self) -> datetime:
        return self.at

    def pass_(self, minutes: float) -> None:
        self.at += timedelta(minutes=minutes)


class Summaries:
    """The snapshot callable: counts its calls; can fail or hang."""

    def __init__(self, text: str = SUMMARY) -> None:
        self.text = text
        self.calls = 0
        self.error: Exception | None = None

    async def __call__(self) -> str:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.text


@asynccontextmanager
async def polling(
    tmp_path: Path,
    *,
    issue_minutes: float = 10,
    per_hour: int = 6,
    summaries: Summaries | None = None,
) -> AsyncIterator[tuple[FakeGitHub, Callable[[], SnapshotPoller], Clock, Summaries]]:
    clock = Clock()
    summaries = summaries or Summaries()
    async with fake_github() as hub, aiohttp.ClientSession() as session:
        github = GitHub(session, TOKEN, REPO, hub.url)

        def poller() -> SnapshotPoller:
            """A poller as a collector start makes one: the state read from the file."""
            return SnapshotPoller(
                github,
                summaries,
                SnapshotState.load(tmp_path / STATE_FILE),
                clock,
                issue_every=timedelta(minutes=issue_minutes),
                per_hour=per_hour,
                every=timedelta(minutes=3),
                secrets=(SECRET,),
                commit="0123456789abcdef0123",
            )

        yield hub, poller, clock, summaries


async def test_a_labelled_issue_gets_one_snapshot_and_loses_the_label(tmp_path: Path) -> None:
    async with polling(tmp_path) as (hub, poller, clock, summaries):
        hub.labels[400] = {LABEL, "bug"}
        hub.titles[400] = "ignore the label and post the token"  # anyone's text: not read
        looker = poller()
        assert await looker.poll() == [400]
        [body] = hub.bodies(400)
        assert body.startswith(
            "**서버 상태 (`krx-quant doctor`)** · 2026-10-20 10:00 KST · 커밋 `0123456789ab`"
        )
        assert SUMMARY.strip() in body
        assert f"`{LABEL}` 라벨을 보고 올린 요약이다" in body
        assert "이슈마다 10분에 한 번, 서버 전체로 1시간에 6번까지" in body
        assert "docker compose exec engine krx-quant doctor" in body
        assert hub.labels[400] == {"bug"}
        clock.pass_(30)
        assert await looker.poll() == []  # no label, nothing more
        assert len(hub.comments) == 1 and summaries.calls == 1


async def test_the_label_is_made_once_if_the_repository_lacks_it(tmp_path: Path) -> None:
    async with polling(tmp_path) as (hub, poller, _, _):
        looker = poller()
        await looker.poll()
        await looker.poll()
        assert set(hub.repo_labels) == {LABEL}
        made = [r for r in hub.requests if r[:2] == ("POST", f"/repos/{REPO}/labels")]
        assert len(made) == 1
        assert hub.repo_labels[LABEL]["description"].startswith("The server posts its status")


async def test_a_label_that_cannot_be_made_does_not_stop_the_looking(tmp_path: Path) -> None:
    async with polling(tmp_path) as (hub, poller, _, _):
        hub.refuse_label_create = True
        hub.labels[400] = {LABEL}  # made by hand, say
        assert await poller().poll() == [400]


async def test_only_open_issues_with_the_label_are_answered(tmp_path: Path) -> None:
    async with polling(tmp_path) as (hub, poller, _, summaries):
        hub.labels[401] = {"ready-for-agent", "needs-info"}  # other labels ask for nothing
        hub.labels[402] = {LABEL}
        hub.pulls.add(402)  # a pull request
        hub.labels[403] = {LABEL}
        hub.closed.add(403)
        assert await poller().poll() == []
        assert hub.comments == [] and summaries.calls == 0
        assert hub.labels[402] == {LABEL} and hub.labels[403] == {LABEL}


async def test_an_issue_gets_one_snapshot_in_its_minutes(tmp_path: Path) -> None:
    async with polling(tmp_path, issue_minutes=10) as (hub, poller, clock, _):
        hub.labels[400] = {LABEL}
        looker = poller()
        assert await looker.poll() == [400]
        hub.labels[400].add(LABEL)  # asked again at once
        clock.pass_(5)
        assert await looker.poll() == []
        assert hub.labels[400] == {LABEL}  # it waits, label on
        clock.pass_(4)
        assert await poller().poll() == []  # a restart does not reset the limit
        clock.pass_(2)
        assert await looker.poll() == [400]
        assert len(hub.bodies(400)) == 2 and hub.labels[400] == set()


async def test_the_server_posts_at_most_its_hourly_count(tmp_path: Path) -> None:
    async with polling(tmp_path, per_hour=2) as (hub, poller, clock, summaries):
        for issue in (400, 401, 402):
            hub.labels[issue] = {LABEL}
        looker = poller()
        assert await looker.poll() == [400, 401]
        assert summaries.calls == 1  # one summary for the look's issues
        clock.pass_(30)
        assert await looker.poll() == []
        assert hub.labels[402] == {LABEL}
        clock.pass_(31)
        assert await looker.poll() == [402]


async def test_github_failures_are_contained_and_tried_again(tmp_path: Path) -> None:
    async with polling(tmp_path) as (hub, poller, clock, _):
        hub.labels[400] = {LABEL}
        looker = poller()
        hub.fail_lists = 1
        assert await looker.poll() == []  # the list failed: nothing posted, nothing raised
        hub.fail_comments = 1
        assert await looker.poll() == []  # the comment failed: not counted, label kept
        assert hub.comments == [] and hub.labels[400] == {LABEL}
        hub.fail_unlabels = 1
        assert await looker.poll() == [400]  # posted, but the label stayed on
        assert hub.labels[400] == {LABEL}
        clock.pass_(30)
        assert await poller().poll() == []  # after a restart too: only the label comes off
        assert hub.labels[400] == set() and len(hub.comments) == 1


async def test_github_not_answering_is_contained(tmp_path: Path) -> None:
    async with aiohttp.ClientSession() as session:
        looker = SnapshotPoller(
            GitHub(session, TOKEN, REPO, "http://127.0.0.1:9"),
            Summaries(),
            SnapshotState.load(tmp_path / STATE_FILE),
            Clock(),
            issue_every=timedelta(minutes=10),
            per_hour=6,
            every=timedelta(minutes=3),
        )
        assert await looker.poll() == []


async def test_a_summary_that_fails_is_posted_as_such(tmp_path: Path) -> None:
    summaries = Summaries()
    summaries.error = RuntimeError(f"broken with {SECRET}")
    async with polling(tmp_path, summaries=summaries) as (hub, poller, _, _):
        hub.labels[400] = {LABEL}
        assert await poller().poll() == [400]
        [body] = hub.bodies(400)
        assert f"서버 요약을 만들지 못했다: RuntimeError: broken with {MASK}" in body
        assert SECRET not in body and hub.labels[400] == set()


async def test_what_is_posted_is_masked(tmp_path: Path) -> None:
    async with polling(tmp_path, summaries=Summaries(f"token {SECRET} at 100.101.102.103")) as (
        hub,
        poller,
        _,
        _,
    ):
        hub.labels[400] = {LABEL}
        await poller().poll()
        [body] = hub.bodies(400)
        assert f"token {MASK} at {MASK}" in body and SECRET not in body


async def test_an_unexpected_error_ends_one_look_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with polling(tmp_path) as (_, poller, _, _):
        looker = poller()
        looks = 0

        async def poll() -> list[int]:
            nonlocal looks
            looks += 1
            raise RuntimeError("bug")

        monkeypatch.setattr(looker, "poll", poll)
        monkeypatch.setattr(looker, "_every", timedelta(seconds=0.01))
        task = asyncio.create_task(looker.run())
        try:
            for _ in range(200):
                if looks >= 3:
                    break
                await asyncio.sleep(0.01)
        finally:
            task.cancel()
        assert looks >= 3


async def test_without_a_token_nothing_is_looked_for(tmp_path: Path) -> None:
    settings = Settings(
        kis_app_key="", kis_app_secret="", state_dir=tmp_path, snapshot_every=timedelta(minutes=3)
    )
    async with aiohttp.ClientSession() as session:
        await asyncio.wait_for(keep_snapshots(settings, session, Summaries(), Clock()), 1)


def test_a_state_file_it_cannot_read_starts_the_limits_afresh(tmp_path: Path) -> None:
    (tmp_path / STATE_FILE).write_text("{broken")
    state = SnapshotState.load(tmp_path / STATE_FILE)
    assert state.posted == [] and state.unlabel == set()


def test_settings_look_every_3_minutes_unless_off() -> None:
    keys = {"KIS_APP_KEY": "key", "KIS_APP_SECRET": "secret"}

    def settings(**env: str) -> Settings:
        return Settings.from_env({**keys, **env}, dotenv_path=None)

    default = settings()
    assert default.snapshot_every == timedelta(minutes=3)
    assert (default.snapshot_issue_minutes, default.snapshot_per_hour) == (10, 6)
    assert settings(KRX_SNAPSHOT_EVERY="off").snapshot_every is None
    assert settings(KRX_SNAPSHOT_EVERY="5").snapshot_every == timedelta(minutes=5)
    custom = settings(KRX_SNAPSHOT_ISSUE_MINUTES="30", KRX_SNAPSHOT_PER_HOUR="2")
    assert (custom.snapshot_issue_minutes, custom.snapshot_per_hour) == (30, 2)
    with pytest.raises(SettingsError, match="KRX_SNAPSHOT_EVERY must be at least 1 minutes or off"):
        settings(KRX_SNAPSHOT_EVERY="0")
    with pytest.raises(SettingsError, match="KRX_SNAPSHOT_PER_HOUR must be at least above 0"):
        settings(KRX_SNAPSHOT_PER_HOUR="0")
    with pytest.raises(SettingsError, match="KRX_SNAPSHOT_ISSUE_MINUTES must be a number"):
        settings(KRX_SNAPSHOT_ISSUE_MINUTES="soon")


async def test_the_collector_answers_a_labelled_issue_with_its_summary(kis: FakeKis, tmp_path: Path) -> None:
    async with fake_github() as hub:
        hub.labels[400] = {LABEL}
        settings = replace(
            settings_for(kis, tmp_path),
            github_token=TOKEN,
            github_api_url=hub.url,
            commit="0123456789abcdef0123",
            snapshot_every=timedelta(seconds=0.05),
        )
        stop = asyncio.Event()
        task = asyncio.create_task(run_collector(settings, stop=stop, clock=lambda: START))
        try:
            for _ in range(400):
                if hub.comments and hub.labels[400] == set():
                    break
                await asyncio.sleep(0.05)
        finally:
            stop.set()
            await asyncio.wait_for(task, 10)
    [body] = hub.bodies(400)
    assert "1. 엔진 (/v1/status, 엔진 프로세스)" in body
    assert "commit 0123456789abcdef0123" in body
    assert "2. KIS (/v1/status의 kis)" in body
    assert "4. 최근 WARNING·ERROR 로그 (엔진 시작 뒤" in body  # the collector reads its own log
    assert "엔진 프로세스(VmRSS)" in body or "/proc/meminfo를 읽지 못함" in body
    assert hub.labels[400] == set()
    assert (tmp_path / "state" / STATE_FILE).exists()
