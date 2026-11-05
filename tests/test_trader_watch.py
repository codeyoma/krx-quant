"""The trader's silence (#542): told once when the trader stops writing its state during the
session, sooner while it holds a position, quiet out of the session, and told back once it has
written again for a minute; not told again by a restart of the collector.

Input: the trader's state (``snapshot``) in its store as a test writes it, the collector's clock,
the market calendar. Output: the alerts sent and the silence kept in the state folder.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from krx_quant.clock import FakeClock
from krx_quant.collector import run_collector
from krx_quant.fake_kis import FakeKis
from krx_quant.feeds.base import KST
from krx_quant.market_calendar import MarketCalendar
from krx_quant.notify import Alerts
from krx_quant.trader.store import DB_NAME, TraderStore
from krx_quant.trader_watch import (
    HOLDING_SECONDS,
    RECOVER_SECONDS,
    SILENT_SECONDS,
    STATE_FILE,
    Beat,
    TraderWatch,
    alerted,
    keep_trader_watched,
    last_beat,
)
from tests.collector_doubles import settings_for
from tests.waiting import until

DAY = date(2026, 11, 5)  # a Thursday
CALENDAR = MarketCalendar({DAY: True, date(2026, 11, 6): True, date(2026, 12, 25): False})


def at(hhmmss: str, day: date = DAY) -> datetime:
    return datetime.combine(day, datetime.strptime(hhmmss, "%H:%M:%S").time(), KST)


def beat(hhmmss: str, trade: int | None = None, day: date = DAY) -> Beat:
    return Beat(at(hhmmss, day), "mock", trade is not None, trade)


class Sent(Alerts):
    """Alerts without Telegram, keeping what they would send."""

    def __init__(self) -> None:
        super().__init__(cast(Any, None), None, None, "http://127.0.0.1:9")
        self.sent: list[str] = []

    def _send(self, text: str) -> None:
        self.sent.append(text)


def watching(tmp_path: Path, alerts: Alerts) -> TraderWatch:
    return TraderWatch(tmp_path / DB_NAME, CALENDAR, alerts, tmp_path)


def test_a_trader_silent_in_the_session_is_told_once_past_a_minute(tmp_path: Path) -> None:
    alerts = Sent()
    watch = watching(tmp_path, alerts)
    last = beat("10:00:05")

    watch.check(at("10:01:05"), last)
    assert alerts.sent == [], "60 s is not past the minute"
    watch.check(at("10:01:07"), last)
    watch.check(at("10:05:00"), last)

    assert alerts.sent == [
        "[장애] 자동 매매(mock): trader가 1분 2초 동안 상태를 쓰지 않았습니다"
        "(마지막 10:00:05, 열린 포지션 없음). trader가 멈췄으면 손절가와 시간 장벽을 아무도 보지 않습니다."
        " 서버에서 `docker compose ps trader`와 `docker compose logs --tail 50 trader`를 봅니다"
        '(docs/operations.md 3장 "trader 침묵 (#542)").'
    ], "once"
    assert SILENT_SECONDS == 60


def test_a_trader_holding_a_position_is_told_sooner(tmp_path: Path) -> None:
    alerts = Sent()
    watch = watching(tmp_path, alerts)
    last = beat("10:00:05", trade=12)

    watch.check(at("10:00:25"), last)
    assert alerts.sent == [], "20 s is not past the limit"
    watch.check(at("10:00:26"), last)

    assert len(alerts.sent) == 1
    assert "21초 동안 상태를 쓰지 않았습니다(마지막 10:00:05, 열린 포지션 있음: 매매 12)" in alerts.sent[0]
    assert HOLDING_SECONDS == 20


def test_a_trader_writing_its_state_is_not_told(tmp_path: Path) -> None:
    alerts = Sent()
    watch = watching(tmp_path, alerts)

    for second in range(0, 300, 5):  # its heartbeat, every 5 s
        now = at("10:00:00") + timedelta(seconds=second)
        watch.check(now, Beat(now - timedelta(seconds=4), "paper", True, 3))

    assert alerts.sent == []


@pytest.mark.parametrize(
    ("now", "why"),
    [
        (at("10:30:00", date(2026, 12, 25)), "a holiday (Christmas, a Friday)"),
        (at("12:00:00", date(2026, 11, 8)), "a Sunday"),
        (at("06:00:30"), "the 06:00 deploy"),
        (at("07:20:30"), "the collector's 07:20 restart"),
        (at("08:59:59"), "before the open"),
        (at("15:30:00"), "the closing auction is over"),
        (at("20:00:00"), "the evening"),
    ],
)
def test_out_of_the_session_a_silent_trader_is_not_told(tmp_path: Path, now: datetime, why: str) -> None:
    alerts = Sent()
    watch = watching(tmp_path, alerts)

    watch.check(now, Beat(now - timedelta(hours=2), "mock", True, 4))

    assert alerts.sent == [], why
    assert alerted(tmp_path) is None


def test_a_trader_silent_since_before_the_open_is_told_at_the_open(tmp_path: Path) -> None:
    alerts = Sent()
    watch = watching(tmp_path, alerts)
    last = beat("07:40:00")

    watch.check(at("08:59:59"), last)
    watch.check(at("09:00:00"), last)

    assert len(alerts.sent) == 1
    assert (
        "trader가 1시간 20분 동안 상태를 쓰지 않았습니다(마지막 07:40:00, 열린 포지션 없음)" in alerts.sent[0]
    )


def test_a_silence_from_an_earlier_day_says_the_day(tmp_path: Path) -> None:
    alerts = Sent()
    watch = watching(tmp_path, alerts)

    watch.check(at("09:00:05", date(2026, 11, 6)), beat("15:02:11"))

    assert "(마지막 11-05 15:02:11, 열린 포지션 없음)" in alerts.sent[0]


def test_the_trader_back_is_told_once_it_has_written_for_a_minute(tmp_path: Path) -> None:
    alerts = Sent()
    watch = watching(tmp_path, alerts)
    watch.check(at("10:01:10"), beat("10:00:05"))
    assert len(alerts.sent) == 1

    back = at("10:04:17")
    for second in range(0, 60, 5):  # writing again, but not for a minute yet
        watch.check(
            back + timedelta(seconds=second + 1), Beat(back + timedelta(seconds=second), "mock", False, None)
        )
    assert len(alerts.sent) == 1, "not back before a minute of writes"
    assert RECOVER_SECONDS == 60

    watch.check(back + timedelta(seconds=61), Beat(back + timedelta(seconds=60), "mock", False, None))
    watch.check(back + timedelta(seconds=66), Beat(back + timedelta(seconds=65), "mock", False, None))

    assert alerts.sent[1:] == [
        "[복구] 자동 매매(mock): trader가 다시 상태를 씁니다(10:04:17부터, 멈춘 동안 4분 12초)."
    ]
    assert alerted(tmp_path) is None
    watch.check(at("10:09:00"), beat("10:07:50"))
    assert len(alerts.sent) == 3, "a new silence is told again"
    assert alerts.sent[2].startswith("[장애] 자동 매매(mock): trader가 1분 10초 동안")


def test_a_trader_that_writes_once_and_dies_again_stays_told_once(tmp_path: Path) -> None:
    """Docker starting a trader that crashes after its first write, again and again, about a minute
    apart: neither a recovery each time it writes nor a new alert each time it is silent again."""
    alerts = Sent()
    watch = watching(tmp_path, alerts)
    watch.check(at("10:01:10"), beat("10:00:05"))

    for start in ("10:02:10", "10:03:12", "10:04:14", "10:05:16"):
        written = beat(start)
        for second in (2, 10, 20, 40, 61):
            watch.check(written.at + timedelta(seconds=second), written)

    assert len(alerts.sent) == 1, alerts.sent


def test_the_trader_back_is_told_out_of_the_session_too(tmp_path: Path) -> None:
    alerts = Sent()
    watch = watching(tmp_path, alerts)
    watch.check(at("15:10:00"), beat("15:00:00", trade=8))

    morning = at("06:01:03", date(2026, 11, 6))  # the 06:00 deploy starts it again
    watch.check(morning + timedelta(seconds=2), Beat(morning, "mock", False, None))
    watch.check(morning + timedelta(seconds=63), Beat(morning + timedelta(seconds=61), "mock", False, None))

    assert alerts.sent[1] == (
        "[복구] 자동 매매(mock): trader가 다시 상태를 씁니다(06:01:03부터, 멈춘 동안 15시간 1분)."
    )


def test_a_restart_of_the_collector_does_not_tell_the_same_silence_again_and_tells_its_end(
    tmp_path: Path,
) -> None:
    first = Sent()
    watching(tmp_path, first).check(at("10:01:10"), beat("10:00:05"))
    assert alerted(tmp_path) == at("10:00:05")
    assert (tmp_path / STATE_FILE).exists()

    again = Sent()
    watch = watching(tmp_path, again)  # the collector started again, the trader still silent
    watch.check(at("10:30:00"), beat("10:00:05"))
    assert again.sent == []

    back = at("10:40:00")
    watch.check(back + timedelta(seconds=1), Beat(back, "mock", False, None))
    watch.check(back + timedelta(seconds=61), Beat(back + timedelta(seconds=60), "mock", False, None))
    assert again.sent == [
        "[복구] 자동 매매(mock): trader가 다시 상태를 씁니다(10:40:00부터, 멈춘 동안 39분 55초)."
    ]


def test_an_unreadable_state_file_is_no_silence_alerted(tmp_path: Path) -> None:
    (tmp_path / STATE_FILE).write_text("{not json")
    assert alerted(tmp_path) is None
    (tmp_path / STATE_FILE).write_text('{"silent_since": null}')
    assert alerted(tmp_path) is None


def test_nothing_is_told_without_a_store_or_a_snapshot(tmp_path: Path) -> None:
    path = tmp_path / DB_NAME
    assert last_beat(path) is None, "no store: a server without a trader"
    TraderStore(path).close()
    assert last_beat(path) is None, "a store with no state written yet"

    alerts = Sent()
    watching(tmp_path, alerts).check(at("11:00:00"), None)
    assert alerts.sent == []


def test_the_state_is_read_from_the_trader_s_store(tmp_path: Path) -> None:
    store = TraderStore(tmp_path / DB_NAME)
    try:
        store.put_snapshot(at("10:00:05"), {"mode": "mock", "status": "running", "position": None})
        assert last_beat(store.path) == Beat(at("10:00:05"), "mock", False, None)
        store.put_snapshot(at("10:00:06"), {"mode": "paper", "position": {"trade_id": 7, "quantity": 3}})
        assert last_beat(store.path) == Beat(at("10:00:06"), "paper", True, 7)
    finally:
        store.close()


async def test_the_watch_reads_the_store_and_tells_the_silence(tmp_path: Path) -> None:
    store = TraderStore(tmp_path / DB_NAME)
    store.put_snapshot(at("10:00:05"), {"mode": "mock", "position": {"trade_id": 2}})
    store.close()
    alerts = Sent()
    clock = [at("10:00:10")]
    reads: list[datetime] = []

    class Counted(TraderWatch):
        def check(self, now: datetime, last: Beat | None) -> None:
            reads.append(now)
            super().check(now, last)

    watch = Counted(tmp_path / DB_NAME, CALENDAR, alerts, tmp_path)
    task = asyncio.create_task(keep_trader_watched(watch, lambda: clock[0], every=0))
    try:
        await until(lambda: len(reads) >= 2)
        assert alerts.sent == [], "10:00:10 is 5 s after it wrote"
        clock[0] = at("10:00:30")
        await until(lambda: bool(alerts.sent))
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert (
        "trader가 25초 동안 상태를 쓰지 않았습니다(마지막 10:00:05, 열린 포지션 있음: 매매 2)"
        in alerts.sent[0]
    )


async def test_a_store_that_cannot_be_read_is_logged_once_and_read_again(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / DB_NAME
    path.write_text("not a database")
    alerts = Sent()
    reads: list[datetime] = []

    class Counted(TraderWatch):
        def check(self, now: datetime, last: Beat | None) -> None:
            reads.append(now)
            super().check(now, last)

    turns = [0]  # the watch asks the time once a turn, before it reads

    def clock() -> datetime:
        turns[0] += 1
        return at("10:00:00")

    watch = Counted(path, CALENDAR, alerts, tmp_path)
    caplog.set_level(logging.INFO, logger="krx_quant.trader_watch")
    task = asyncio.create_task(keep_trader_watched(watch, clock, every=0))
    try:
        await until(lambda: turns[0] >= 4)  # three reads failed, at least
        assert reads == []
        path.unlink()
        store = TraderStore(path)
        store.put_snapshot(at("09:59:59"), {"mode": "paper", "position": None})
        store.close()
        await until(lambda: len(reads) >= 2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    said = [(r.levelname, r.getMessage()) for r in caplog.records]
    assert [s for s in said if s[0] == "WARNING"] == [
        (
            "WARNING",
            f"could not read the trader's state in {path} (file is not a database): read again every 0s",
        )
    ], "once while it fails"
    assert said.count(("INFO", "the trader's state is read again")) == 1
    assert alerts.sent == []


async def test_the_collector_tells_a_trader_silent_in_the_session(
    kis: FakeKis, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The live collector watches the trader's store in its state folder (#542)."""
    settings = settings_for(kis, tmp_path)
    store = TraderStore(settings.state_dir / DB_NAME)
    store.put_snapshot(at("09:58:00"), {"mode": "paper", "position": None})
    store.close()
    caplog.set_level(logging.WARNING, logger="krx_quant.notify")
    stop = asyncio.Event()
    task = asyncio.create_task(run_collector(settings, stop=stop, clock=FakeClock(at("10:00:00"))))
    try:
        await until(
            lambda: any("상태를 쓰지 않았습니다" in r.getMessage() for r in caplog.records), timeout=10
        )
    finally:
        stop.set()
        await asyncio.wait_for(task, 10)

    told = [r.getMessage() for r in caplog.records if "상태를 쓰지 않았습니다" in r.getMessage()]
    assert told[0].startswith("alert: [장애] 자동 매매(paper): trader가 2분 0초 동안"), told
    assert alerted(settings.state_dir) == at("09:58:00")
