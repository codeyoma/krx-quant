"""The trader's fill notice recorder (#484): the mock account's real-time fill notices kept as they
came in ``state/fill_notices.sqlite3``, beside the trading and never in it.

KIS is the fake one (``fake_kis.FakeKis``): each subscription's answer gives the key and IV its
connection's frames are encrypted with (``rotate_notice_keys``: new ones on each connection), and a
test pushes the frames (``push_notice``). The clock stands at 10:00 of a trading day, inside the
recording window. Every value is made up.
"""

from __future__ import annotations

import asyncio
import logging
import os
import stat
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

import aiohttp
import pytest

from krx_quant import kis
from krx_quant.fake_kis import NOTICE_IV, NOTICE_KEY, FakeKis, encrypt
from krx_quant.feeds.base import KST
from krx_quant.kis_orders import Account, AccountNo
from krx_quant.ratelimit import RateLimiter
from krx_quant.trader import fill_notices
from krx_quant.trader.fill_notices import (
    CAP,
    DB_NAME,
    KEEP_DAYS,
    MALFORMED,
    NO_KEY,
    QUEUE,
    SIZE,
    TR_ID,
    UNDECRYPTED,
    UNDIVIDED,
    Connected,
    FillNoticeRecorder,
    FillNoticeStore,
    Frame,
    Limits,
    Pace,
    parse_frame,
    read_recorded,
    recording_window,
)
from tests.trader_doubles import CALENDAR, at
from tests.waiting import until

KEY = "PStrader" + "n" * 28
SECRET = "Sn" + "k" * 60
HTS_ID = "notice7hts"
FAST = Pace(backoff=0.05, most=0.2, silence=5.0, heard=0.0, flush=3.0, close=0.2, look=0.02)
TEN = at("10:00:00")


def record(*changes: tuple[int, str], width: int = 23) -> list[str]:
    """A made-up record of ``width`` fields, an empty one among them, with ``changes`` (index, value)."""
    fields = [f"f{i}" for i in range(width)]
    fields[5] = ""
    for i, value in changes:
        fields[i] = value
    return fields


def sealed(text: str, count: int) -> str:
    """A fill notice frame of ``text`` as KIS sends it, encrypted with the fake's key and IV."""
    return f"1|{TR_ID}|{count:03d}|{encrypt(NOTICE_KEY, NOTICE_IV, text)}"


@pytest.fixture
async def fake() -> AsyncIterator[FakeKis]:
    found = FakeKis(app_key=KEY, app_secret=SECRET, rotate_notice_keys=True)
    await found.start()
    yield found
    await found.stop()


class Running:
    """A recorder on ``fake`` in its own task, as the trader starts it."""

    def __init__(
        self,
        fake: FakeKis,
        state: Path,
        session: aiohttp.ClientSession,
        *,
        clock: Callable[[], datetime] = lambda: TEN,
        hts_id: str | None = HTS_ID,
        limits: Limits | None = None,
        pace: Pace = FAST,
    ) -> None:
        async def approval() -> str:
            return await kis.issue_approval_key(session, fake.rest_url, KEY, SECRET)

        self.task = fill_notices.start(
            session, fake.ws_url, hts_id, approval, state, clock, CALENDAR.session, pace=pace, limits=limits
        )

    async def stop(self) -> None:
        await fill_notices.stop(self.task)
        assert self.task.done()


def recorded(state: Path) -> fill_notices.Recorded:
    found = read_recorded(state)
    assert found is not None
    return found


async def subscribed(fake: FakeKis, n: int) -> None:
    await until(lambda: len(fake.notice_keys_issued) >= n)


# A frame read


def test_a_frame_is_split_into_its_header_s_count_of_records_of_equal_width_empty_fields_kept() -> None:
    one, two = record((0, "a")), record((0, "b"))
    text = "^".join([*one, *two])
    frame = parse_frame(
        1, 7, TEN, f"1|{TR_ID}|002|{encrypt(NOTICE_KEY, NOTICE_IV, text)}", (NOTICE_KEY, NOTICE_IV)
    )

    assert (frame.why, frame.count, frame.tr_id, frame.number) == (None, 2, TR_ID, 7)
    assert frame.records == (tuple(one), tuple(two))
    assert frame.records[0][5] == "", "an empty field is kept"


def test_a_frame_of_another_width_is_split_by_its_count_alone() -> None:
    """Not read as KIS's example's 26 columns, nor as its first 23: a record is as wide as its frame's
    fields over its count."""
    for width in (23, 26, 9):
        fields = record(width=width)
        plain = parse_frame(1, 1, TEN, f"0|{TR_ID}|001|{'^'.join(fields)}", None)
        assert plain.records == (tuple(fields),)


def test_a_frame_its_count_does_not_divide_is_kept_apart_with_its_fields() -> None:
    text = "^".join(record(width=47))
    frame = parse_frame(
        1, 1, TEN, f"1|{TR_ID}|002|{encrypt(NOTICE_KEY, NOTICE_IV, text)}", (NOTICE_KEY, NOTICE_IV)
    )

    assert (frame.why, frame.records, frame.count) == (UNDIVIDED, (), 2)
    assert frame.fields == tuple(record(width=47))


def test_a_frame_not_decrypted_or_not_of_the_form_is_kept_apart_without_fields() -> None:
    frame = sealed("a^b", 1)
    other = ("f" * 32, "g" * 16)
    assert [parse_frame(1, 1, TEN, frame, k).why for k in (None, other)] == [NO_KEY, UNDECRYPTED]
    assert parse_frame(1, 1, TEN, f"1|{TR_ID}|001|not base64!", (NOTICE_KEY, NOTICE_IV)).why == UNDECRYPTED
    for text in ("1|H0STCNI9", f"2|{TR_ID}|001|a", f"1|{TR_ID}|x|a"):
        assert parse_frame(1, 1, TEN, text, None).why == MALFORMED
    assert parse_frame(1, 1, TEN, frame, None).fields is None


def test_a_trading_day_s_window_runs_from_before_krx_takes_orders_to_after_the_close() -> None:
    session = CALENDAR.session(date(2026, 10, 14))
    assert session is not None
    assert recording_window(session) == (at("08:20:00"), at("15:40:00"))


# The recorder


async def test_each_record_is_kept_as_it_came_and_a_reconnect_reads_with_its_new_key_and_iv(
    fake: FakeKis, tmp_path: Path
) -> None:
    async with aiohttp.ClientSession() as session:
        running = Running(fake, tmp_path, session)
        try:
            await subscribed(fake, 1)
            one = record((2, "0000031401"))
            two = [record((2, "0000031402")), record((2, "0000031403"))]
            wide = record((2, "0000031404"), width=26)
            await fake.push_notice([one])
            await fake.push_notice(two)
            await fake.push_notice("^".join(record(width=47)), count=2)
            await fake.push_notice([wide])
            await until(lambda: len(recorded(tmp_path).records) == 4 and len(recorded(tmp_path).odd) == 1)
            files = [p for p in tmp_path.iterdir() if p.name.startswith(DB_NAME)]
            assert {p.name for p in files} == {DB_NAME, f"{DB_NAME}-wal", f"{DB_NAME}-shm"}
            assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in files), "the account and HTS ID"

            await fake.close_connections()  # KIS drops it: the next connection's answer gives a new key
            await subscribed(fake, 2)
            again = record((2, "0000031405"))
            await fake.push_notice([again])
            await until(lambda: len(recorded(tmp_path).records) == 5)
        finally:
            await running.stop()

    kept = recorded(tmp_path)
    assert [(r.connection, r.frame, r.count, r.position) for r in kept.records] == [
        (1, 1, 1, 0),
        (1, 2, 2, 0),
        (1, 2, 2, 1),
        (1, 4, 1, 0),
        (2, 1, 1, 0),
    ]
    assert [list(r.fields) for r in kept.records] == [one, *two, wide, again]
    assert {(r.tr_id, r.day, r.received_at) for r in kept.records} == {(TR_ID, TEN.date(), TEN)}
    [odd] = kept.odd
    assert (odd.why, odd.count, odd.fields) == (UNDIVIDED, 2, tuple(record(width=47)))
    assert fake.approvals_issued == ["fake-approval-key-1"], "the approval key used again"
    first, second = kept.connections
    assert (first.number, first.subscribed_at, first.ended_at, first.why) == (
        1,
        TEN,
        TEN,
        "KIS closed the connection",
    )
    assert (second.number, second.subscribed_at) == (2, TEN)
    stored = b"".join(p.read_bytes() for p in tmp_path.iterdir() if p.name.startswith(DB_NAME))
    for secret in [*(k for pair in fake.notice_keys_issued for k in pair), *fake.approvals_issued]:
        assert secret.encode() not in stored, "no key, IV or approval key is kept"
    assert b"SUBSCRIBE SUCCESS" not in stored, "nor the subscription's answer"


async def test_the_approval_key_goes_through_the_broker_s_limiter_after_the_calls_waiting(
    fake: FakeKis,
) -> None:
    asked: list[bool] = []

    class Spy(RateLimiter):
        async def acquire(self, *, last: bool = False) -> float:
            asked.append(last)
            return await super().acquire(last=last)

    async with kis.rest_session() as session:

        async def token() -> str:
            return "token"

        account = Account(
            session, fake.rest_url, KEY, SECRET, AccountNo("50123456", "01"), token, mock=True, limiter=Spy(1)
        )
        assert await account.approval_key() == "fake-approval-key-1"

    assert asked == [True], "the broker's own limiter, last"
    assert account.answers == [], "not kept with the calls' answers"


async def test_a_refused_approval_key_is_asked_again_after_a_backoff(
    fake: FakeKis, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    fake.approval_refusals = 1
    caplog.set_level(logging.WARNING)
    async with aiohttp.ClientSession() as session:
        running = Running(fake, tmp_path, session)
        try:
            await subscribed(fake, 1)
        finally:
            await running.stop()

    assert fake.approvals_issued == ["fake-approval-key-1"]
    assert any("WebSocket ended (KisError: approval key request failed (403)" in m for m in caplog.messages)
    first, second = recorded(tmp_path).connections
    assert first.subscribed_at is None and (first.why or "").startswith("KisError")
    assert second.subscribed_at == TEN


async def test_an_approval_key_kis_no_longer_takes_is_asked_anew(fake: FakeKis, tmp_path: Path) -> None:
    async with aiohttp.ClientSession() as session:
        running = Running(fake, tmp_path, session)
        try:
            await subscribed(fake, 1)
            fake.approvals_issued.clear()  # KIS forgot it: the next subscription is OPSP0011
            await fake.close_connections()
            await subscribed(fake, 2)
        finally:
            await running.stop()

    assert fake.approvals_issued == ["fake-approval-key-1"], "asked anew"
    whys = [c.why for c in recorded(tmp_path).connections]
    assert "KIS refused the subscription (OPSP0011)" in whys


async def test_a_silent_connection_is_made_again(fake: FakeKis, tmp_path: Path) -> None:
    async with aiohttp.ClientSession() as session:
        running = Running(fake, tmp_path, session, pace=replace(FAST, silence=0.3))
        try:
            await subscribed(fake, 1)
            fake.go_silent()
            await subscribed(fake, 2)
        finally:
            await running.stop()

    assert recorded(tmp_path).connections[0].why == "no frame for 0.3s"


async def test_a_pingpong_is_answered(fake: FakeKis, tmp_path: Path) -> None:
    async with aiohttp.ClientSession() as session:
        running = Running(fake, tmp_path, session)
        try:
            await subscribed(fake, 1)
            await fake.round_trip()
        finally:
            await running.stop()


async def test_what_is_over_a_bound_is_not_kept_and_counted(fake: FakeKis, tmp_path: Path) -> None:
    """A frame over ``Limits.frame`` characters, the records and odd frames over a day's."""
    limits = Limits(frame=400, records=2, odd=1)
    async with aiohttp.ClientSession() as session:
        running = Running(fake, tmp_path, session, limits=limits)
        try:
            await subscribed(fake, 1)
            await fake.push_notice([record(width=40)] * 3)  # over 400 characters: 3 records
            for n in range(3):
                await fake.push_notice([record((2, f"00000{n}"))])
            for _ in range(2):
                await fake.push_notice("a^b^c", count=2)
            await until(lambda: len(recorded(tmp_path).drops) == 3)
        finally:
            await running.stop()

    kept = recorded(tmp_path)
    assert (len(kept.records), len(kept.odd)) == (2, 1)
    assert sorted((d.what, d.frames, d.records) for d in kept.drops) == [
        (CAP, 1, 1),
        (CAP, 1, 2),
        (SIZE, 1, 3),
    ]


async def test_a_frame_beyond_the_queue_is_not_kept_and_counted(tmp_path: Path) -> None:
    recorder = FillNoticeRecorder(
        aiohttp.ClientSession(),
        "ws://127.0.0.1:1",
        HTS_ID,
        lambda: asyncio.sleep(0, "key"),
        tmp_path / DB_NAME,
        lambda: TEN,
        CALENDAR.session,
        limits=Limits(queue=1),
    )
    try:
        recorder._frame(1, 1, TEN, f"0|{TR_ID}|001|a^b", None)
        recorder._frame(1, 2, TEN, f"0|{TR_ID}|002|a^b^c^d", None)
        assert recorder._drops == [(TEN, QUEUE, 1, 2)]
    finally:
        await recorder._session.close()


def test_rows_older_than_thirty_days_are_deleted(tmp_path: Path) -> None:
    today = date(2026, 11, 20)
    store = FillNoticeStore(tmp_path / DB_NAME, Limits(), date(2026, 10, 1))
    try:
        for days in (KEEP_DAYS + 1, KEEP_DAYS, 0):
            when = datetime.combine(today - timedelta(days=days), TEN.time(), KST)
            store.apply(
                [
                    Connected(100 - days, when),
                    Frame(100 - days, 1, when, TR_ID, 1, 3, records=(("a", "b"),)),
                    Frame(100 - days, 2, when, TR_ID, 2, 3, why=UNDIVIDED, fields=("a",)),
                ],
                [(when, QUEUE, 1, 1)],
            )
        store.prune(today)
    finally:
        store.close()

    kept = recorded(tmp_path)
    left = [today - timedelta(days=KEEP_DAYS), today]
    assert [r.day for r in kept.records] == left and [o.day for o in kept.odd] == left
    assert [c.day for c in kept.connections] == left and [d.day for d in kept.drops] == left


def test_a_store_made_with_another_mode_is_made_600(tmp_path: Path) -> None:
    path = tmp_path / DB_NAME
    path.touch(mode=0o644)
    os.chmod(path, 0o644)
    FillNoticeStore(path, Limits(), TEN.date()).close()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


async def test_without_an_hts_id_it_says_so_once_and_records_nothing(
    fake: FakeKis, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    async with aiohttp.ClientSession() as session:
        running = Running(fake, tmp_path, session, hts_id=None)
        await asyncio.wait_for(running.task, 5)

    assert [m for m in caplog.messages if "KRX_TRADER_KIS_HTS_ID is not set" in m]
    assert fake.ws_connections == 0 and not (tmp_path / DB_NAME).exists()


async def test_a_store_that_cannot_be_opened_ends_the_recorder_alone(
    fake: FakeKis, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / DB_NAME).mkdir()
    caplog.set_level(logging.WARNING)
    async with aiohttp.ClientSession() as session:
        running = Running(fake, tmp_path, session)
        await asyncio.wait_for(running.task, 5)

    assert running.task.exception() is None
    assert any("so the fill notices are not recorded" in m for m in caplog.messages)
    assert fake.ws_connections == 0


async def test_outside_the_window_there_is_no_connection_until_it_opens(
    fake: FakeKis, tmp_path: Path
) -> None:
    """The clock at 15:41 of a Friday: the next window is Monday's, 08:20. Once the clock is there,
    the recorder connects."""
    clock = [at("2026-10-16 15:41:00")]
    seen: list[datetime] = []

    def now() -> datetime:
        seen.append(clock[0])
        return clock[0]

    async with aiohttp.ClientSession() as session:
        running = Running(fake, tmp_path, session, clock=now)
        try:
            await until(lambda: len(seen) >= 3)  # it looked at the clock and waits
            assert fake.ws_connections == 0
            clock[0] = at("2026-10-19 08:20:00")
            await subscribed(fake, 1)
        finally:
            await running.stop()
