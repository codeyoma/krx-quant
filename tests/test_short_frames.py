"""Frames short of fields (#381): kept raw only and counted for their feed and key, as a WARNING
for the member flow (KIS sends a few short ones at 08:00) and an ERROR for any other feed.

Input: frames pushed by the fake KIS server. Output: the raw and events layers, the log, and
``/v1/status``'s ``kis.feeds[].short_frames``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from krx_quant.clock import FakeClock
from krx_quant.collector import run_collector
from krx_quant.fake_kis import FakeKis
from krx_quant.feeds import MEMBER, TRADE
from krx_quant.feeds.base import KST
from krx_quant.health import FeedHealth
from krx_quant.recorder import ParquetRecorder
from tests.kis_frames import member_frame, trade_frame
from tests.test_collector import read, settings_for
from tests.test_live_predictions import free_port, poll

# 2026-10-08, as the reading helpers name the day: 08:00:01, the NXT pre-market's start.
MORNING = datetime(2026, 10, 8, 8, 0, 1, tzinfo=KST)
MEMBER_KEY = (MEMBER.tr_id, "005930")
TRADE_KEY = (TRADE.tr_id, "005930")


def full_member() -> str:
    return member_frame({"MKSC_SHRN_ISCD": "005930"})


def without(frame: str, *indexes: int) -> str:
    """``frame`` with the fields at ``indexes`` (0-based) left out: not one block, as on the
    server (probe-member-frames-381, 2026-10-29)."""
    head, payload = frame.rsplit("|", 1)
    fields = payload.split("^")
    return f"{head}|{'^'.join(f for i, f in enumerate(fields) if i not in indexes)}"


def feed_of(status: dict[str, Any], key: tuple[str, str]) -> dict[str, Any] | None:
    found = [f for f in status["kis"]["feeds"] if (f["feed"], f["key"]) == key]
    return found[0] if found else None


def short_of(status: dict[str, Any], key: tuple[str, str]) -> int | None:
    feed = feed_of(status, key)
    return None if feed is None else int(feed["short_frames"])


async def serve(kis: FakeKis, tmp_path: Path, ready: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
    """Run the collector until ``/v1/status`` is ``ready``; that status.

    A frame is recorded, counted and logged in one step of the event loop, so a status that
    shows it was received or counted comes after its log line.
    """
    port = free_port()
    settings = replace(settings_for(kis, tmp_path), api_port=port)
    stop = asyncio.Event()
    task = asyncio.create_task(run_collector(settings, stop=stop, clock=FakeClock(MORNING)))
    try:
        await kis.wait_for_subscriptions(set(kis.script))
        status: dict[str, Any] = await poll(f"http://127.0.0.1:{port}/v1/status", ready)
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)
    return status


def records_of(caplog: pytest.LogCaptureFixture, feed: str, level: int) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == level and feed in r.getMessage()]


async def test_a_member_frame_short_of_fields_is_kept_raw_counted_and_only_a_warning(
    kis: FakeKis, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    short = without(full_member(), 2, 3, 9, 26, 40, 77)  # 72 of 78
    kis.script[MEMBER_KEY] = [full_member(), full_member(), short]  # once it is counted, all came
    kis.script[TRADE_KEY] = [trade_frame({"MKSC_SHRN_ISCD": "005930"})]

    with caplog.at_level(logging.INFO):
        status = await serve(kis, tmp_path, lambda b: short_of(b, MEMBER_KEY) == 1)

    assert [r["message"] for r in read(tmp_path, "raw", MEMBER.tr_id)] == [
        full_member(),
        full_member(),
        short,
    ]
    assert len(read(tmp_path, "events", MEMBER.tr_id)) == 2, "the short frame's event is dropped"
    assert records_of(caplog, MEMBER.tr_id, logging.WARNING) == [
        "H0UNMBC0 frame short of fields kept raw only (005930, #381): 72 of 78"
    ]
    assert records_of(caplog, MEMBER.tr_id, logging.ERROR) == [], "the alert's level is not reached"
    assert short_of(status, TRADE_KEY) == 0


async def test_another_feed_s_short_frame_is_still_an_error_and_counted(
    kis: FakeKis, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    trade = trade_frame({"MKSC_SHRN_ISCD": "005930"})
    short_trade = without(trade, 5, 20)
    kis.script[TRADE_KEY] = [short_trade]
    kis.script[MEMBER_KEY] = [full_member()]

    with caplog.at_level(logging.INFO):
        status = await serve(kis, tmp_path, lambda b: short_of(b, TRADE_KEY) == 1)

    assert [r["message"] for r in read(tmp_path, "raw", TRADE.tr_id)] == [short_trade]
    assert not (tmp_path / "events" / "date=2026-10-08" / f"feed={TRADE.tr_id}").exists()
    assert records_of(caplog, TRADE.tr_id, logging.ERROR) == [
        "could not normalize H0UNCNT0 frame; kept raw only"
    ]
    assert records_of(caplog, TRADE.tr_id, logging.WARNING) == []
    assert short_of(status, MEMBER_KEY) == 0


async def test_a_member_frame_with_too_many_fields_is_still_an_error_and_not_counted(
    kis: FakeKis, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    long = f"{full_member()}^0"  # 79 of 78
    kis.script[MEMBER_KEY] = [long]

    def received(status: dict[str, Any]) -> bool:
        feed = feed_of(status, MEMBER_KEY)
        return feed is not None and feed["last_received_at"] is not None

    with caplog.at_level(logging.INFO):
        status = await serve(kis, tmp_path, received)

    assert [r["message"] for r in read(tmp_path, "raw", MEMBER.tr_id)] == [long]
    assert records_of(caplog, MEMBER.tr_id, logging.ERROR) == [
        "could not normalize H0UNMBC0 frame; kept raw only"
    ]
    assert records_of(caplog, MEMBER.tr_id, logging.WARNING) == []
    assert short_of(status, MEMBER_KEY) == 0


def test_the_count_is_today_s(tmp_path: Path) -> None:
    now = [MORNING]
    health = FeedHealth(ParquetRecorder(tmp_path, 10, 3600), lambda: now[0], cast(Any, None))
    health.received(MEMBER_KEY)
    health.short_frame(MEMBER_KEY)
    health.short_frame(MEMBER_KEY)
    health.received(TRADE_KEY)

    assert {(s.feed, s.short_frames) for s in health.snapshot()} == {(MEMBER.tr_id, 2), (TRADE.tr_id, 0)}

    now[0] = MORNING.replace(hour=23, minute=59, second=59)
    assert {(s.feed, s.short_frames) for s in health.snapshot()} == {(MEMBER.tr_id, 2), (TRADE.tr_id, 0)}

    now[0] = MORNING + timedelta(days=1)  # the next day, KST
    assert {(s.feed, s.short_frames) for s in health.snapshot()} == {(MEMBER.tr_id, 0), (TRADE.tr_id, 0)}
    health.short_frame(MEMBER_KEY)
    assert {(s.feed, s.short_frames) for s in health.snapshot()} == {(MEMBER.tr_id, 1), (TRADE.tr_id, 0)}
