"""The market-wide halt's notices and protection (#625, stage 3).

Input: the collector against the fake KIS, fed a breaker (the KOSPI 8.4% down held a minute), a
trade flagged halted, then the reopening auction's book and a minute of continuous trading; and the
Replay Harness with 2026-11-06's order of events (``tests.test_market_halt``). Output: the Telegram
messages, the gaps' reasons, the predictions the engine makes, the trader's paper fills and stall
notices, and the special windows its replay knows.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from krx_quant.collector import run_collector
from krx_quant.fake_kis import FakeKis
from krx_quant.feeds import KST
from krx_quant.special import REOPENED, Kind, Window
from krx_quant.special import Kind as SpecialKind
from krx_quant.trader import CAPITAL
from krx_quant.trader.book import TIME, Rules
from krx_quant.trader.cells import Kept, special_windows
from krx_quant.trader.quotes import BestQuote
from krx_quant.trader.replay import known
from tests.api_schema import check
from tests.collector_doubles import FIXED_NOW, TRADE_KEY, Clock, read, settings_for
from tests.kis_frames import book_frame, index_frame, trade_frame
from tests.replay_doubles import VI, busy_morning
from tests.replay_events import SYMBOL, api, at, get_json, run
from tests.test_calendar_health import health_checks
from tests.test_market_halt import halt_morning, market
from tests.test_resilience import with_telegram
from tests.test_trader_replay import legs, models, paper_legs, record_gap, replayed
from tests.test_trader_stall import BACK_SAID, BOUGHT, STALL_SAID, TEN, flowing, paper_day, said, stalls
from tests.trader_doubles import (
    BARRIER,
    FEE,
    Day,
    Read,
    Special,
    Tick,
    first_champion,
    pick,
    quote,
    record_quotes,
)
from tests.trader_doubles import at as tat
from tests.waiting import until

INDEX_KEY = ("H0UPCNT0", "0001")
HALT_NOTICE = "[알림] 시장 전체 매매 정지"
RESUME_NOTICE = "[알림] 시장 전체 매매 재개"


def hms(at: datetime) -> str:
    return at.astimezone(KST).strftime("%H%M%S")


def index_at(at: datetime, change: str = "-8.40") -> str:
    return index_frame({"bstp_cls_code": "0001", "bsop_hour": hms(at), "prdy_ctrt": change})


def trade_at(at: datetime, *, halted: bool = False, code: str = "0") -> str:
    return trade_frame(
        {
            "MKSC_SHRN_ISCD": "005930",
            "STCK_CNTG_HOUR": hms(at),
            "STCK_PRPR": "81300",
            "BSOP_DATE": "20261008",
            "TRHT_YN": "Y" if halted else "N",
            "HOUR_CLS_CODE": code,
        }
    )


def book_at(at: datetime, code: str) -> str:
    return book_frame(
        {
            "MKSC_SHRN_ISCD": "005930",
            "BSOP_HOUR": hms(at),
            "HOUR_CLS_CODE": code,
            "ASKP1": "81300",
            "BIDP1": "81200",
        }
    )


# --- The collector's notices.


def checked_after(checks: Any, done: int) -> bool:
    return len(checks.opened) > done


async def test_a_confirmed_halt_is_one_notice_and_its_reopening_another_with_no_feed_outage(
    kis: FakeKis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checks = health_checks(monkeypatch)
    kis.script[TRADE_KEY] = [trade_at(FIXED_NOW)]
    kis.script[INDEX_KEY] = [index_at(FIXED_NOW)]
    clock = Clock(FIXED_NOW)  # 09:30:16
    stop = asyncio.Event()
    settings = replace(
        with_telegram(kis, settings_for(kis, tmp_path)), index_codes=("0001",), health_check_seconds=0.05
    )
    task = asyncio.create_task(run_collector(settings, stop=stop, clock=clock))
    await kis.wait_for_subscription_count(TRADE_KEY, 1)
    await kis.round_trip()

    async def at_(when: datetime, *frames: str) -> None:
        clock.at = when
        for f in frames:
            await kis.push(f)
        await kis.round_trip()

    # The KOSPI held 8.4% down a minute: the breaker at 09:31:16; the trade flagged at 09:31:18.
    await at_(FIXED_NOW + timedelta(minutes=1), index_at(FIXED_NOW + timedelta(minutes=1)))
    await at_(FIXED_NOW + timedelta(minutes=1, seconds=2), trade_at(FIXED_NOW, halted=True))
    clock.at += timedelta(seconds=1)  # the engine knows what it received before now
    await until(lambda: any(m["text"].startswith(HALT_NOTICE) for m in kis.telegram_messages))
    # Five minutes without trades or books, the index going on: the trades' key goes stale.
    for minute in range(2, 8):
        when = FIXED_NOW + timedelta(minutes=minute)
        await at_(when, index_at(when))
        await until(partial(checked_after, checks, len(checks.opened)))  # a stale check at this minute
    assert any(TRADE_KEY in opened for opened in checks.opened)
    # The reopening auction's book, then continuous trading and a minute of fresh quotes and trades.
    when = FIXED_NOW + timedelta(minutes=8)
    await at_(when, book_at(when, "C"), index_at(when))
    when += timedelta(seconds=30)
    while when <= FIXED_NOW + timedelta(minutes=10):
        await at_(when, book_at(when, "0"), trade_at(when), index_at(when, "-6.00"))
        when += timedelta(seconds=5)
    await until(lambda: any(m["text"].startswith(RESUME_NOTICE) for m in kis.telegram_messages))
    stop.set()
    await asyncio.wait_for(task, 5)

    texts = [m["text"] for m in kis.telegram_messages]
    assert [t[: len(HALT_NOTICE)] for t in texts] == [HALT_NOTICE, RESUME_NOTICE], texts
    assert "09:30:16부터" in texts[0], "from the halted trade's time"
    reasons = {g["reason"] for g in read(tmp_path, "gaps", "H0UNCNT0")}
    assert reasons == {"stale: market halt"}, "an expected silence, apart from data lost"


# --- The engine predicts nothing in the halt.


async def test_no_prediction_in_the_halt_until_a_minute_of_fresh_quotes_after_the_reopening(
    tmp_path: Path,
) -> None:
    db = tmp_path / "engine.db"

    engine = await run(db, [*halt_morning(), *market()], until=at("09:50:00"))

    with sqlite3.connect(db) as conn:
        statuses = dict(
            conn.execute(
                "SELECT substr(bar_close, 12, 5), group_concat(DISTINCT status) FROM predictions"
                " GROUP BY bar_close"
            ).fetchall()
        )
        kinds = dict(
            conn.execute(
                "SELECT substr(bar_close, 12, 5), group_concat(DISTINCT status) FROM kind_predictions"
                " GROUP BY bar_close"
            ).fetchall()
        )
    # The halt from 09:03:32 to 09:35:10 (the reopening at 09:34:10 and a fresh minute).
    for close in ("09:04", "09:20", "09:34", "09:35"):
        assert statuses[close] == "halt", close
        assert kinds[close] == "halt", close
    assert "halt" not in statuses["09:03"] and "halt" not in statuses["09:36"]
    assert "halt" not in kinds["09:36"]
    async with api(engine) as client:
        shown = await get_json(client, f"/v1/symbols/{SYMBOL}/predictions?limit=1000")
        barrier = await get_json(
            client, f"/v1/symbols/{SYMBOL}/kinds/barrier/predictions?bar_close=2026-10-12T09:20:00%2B09:00"
        )
    check("predictions", shown)
    check("kind_predictions", barrier)
    assert {p["status"] for p in shown["predictions"] if p["bar_close"][11:16] == "09:20"} == {"halt"}


# --- The trader.

HALT = Window(Kind.MARKET_HALT, None, at("09:03:32"), at("09:35:10"), REOPENED)


def test_the_replay_knows_a_window_from_when_the_engine_learned_it() -> None:
    kept = Kept(Kind.VI, "005930", at("09:30:20"), at("09:32:40"), "dynamic", at("09:30:24"))
    before = Kept(Kind.SIDECAR, None, at("09:21:00"), at("09:26:00"))  # kept before #625: its start

    assert known([kept, before], at("09:30:22")) == [before]
    assert known([kept, before], at("09:30:24")) == [kept, before]


async def test_the_engine_keeps_when_it_first_learned_a_window_and_the_trader_reads_it(
    tmp_path: Path,
) -> None:
    # A VI seen on at 09:30:24 and released at 09:32:44: one row, learned at its release, first
    # learned at 09:30:24, which the trader's replay knows it from.
    db = tmp_path / "engine.db"
    await run(db, [*busy_morning(), *VI], until=at("09:40:00"))

    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        [window] = special_windows(conn, "005930", at("09:00:00"))
        [row] = conn.execute("SELECT learned_at, first_learned_at FROM special_windows").fetchall()
    assert (row["learned_at"], row["first_learned_at"]) == (
        "2026-10-12T09:32:44+09:00",
        "2026-10-12T09:30:24+09:00",
    )
    assert isinstance(window, Kept) and window.learned_at == at("09:30:24")
    assert known([window], at("09:30:23")) == []


# --- The trader's books through a halt (the example of ``tests/test_trader_stall.py``: bought at
# 10:00:02.5 at 70,100, its stop 69,850, its time barrier 10:04).


def halted_from(start: str, end: str) -> Read:
    """The engine's market halt from ``start`` to ``end``, read by the trader at ``start``."""
    return Read(start, [Special(start, end, SpecialKind.MARKET_HALT)])


def test_a_paper_book_fills_nothing_on_the_quotes_of_a_halt_and_exits_on_the_first_after_it(
    tmp_path: Path,
) -> None:
    """The halt known from 10:00:30 to 10:05:00; the auction's quotes in it bid below the stop and past
    the time barrier: the book sells on none of them, and on the first quote after the halt."""
    day = paper_day(tmp_path)

    [trade] = day.run(
        [
            *flowing("10:00:00", "10:00:02"),
            TEN,
            BOUGHT,
            *flowing("10:00:03", "10:00:29"),
            halted_from("10:00:30", "10:05:00"),
            *flowing("10:00:31", "10:04:59", bid=69_500, ask=69_400),
            quote("10:05:00", 69_900, 70_000),
        ]
    )

    assert trade["exit"] == {"at": "2026-10-14T10:05:00+09:00", "price": 69_900, "reason": TIME}


def test_a_stall_in_a_halt_the_engine_knows_tells_telegram_nothing(tmp_path: Path) -> None:
    """The halt known from 10:00:30; the last quote at 10:01:00 and none until 10:05:00: the stall is
    kept, and neither it nor the quotes' return is sent (the engine sends the halt's notices)."""
    day = paper_day(tmp_path)

    day.run(
        [
            *flowing("10:00:00", "10:00:29"),
            halted_from("10:00:30", "10:05:00"),
            *flowing("10:00:31", "10:01:00"),
            Tick("10:01:05.5"),
            Tick("10:01:31"),
            Tick("10:03:00"),
            quote("10:05:00", 70_000, 70_100),
        ]
    )

    [stall] = stalls(tmp_path)
    assert (stall["began_at"], stall["alerted_at"]) == ("2026-10-14T10:01:05.500000+09:00", None)
    assert day.notices == []


# --- The paper book and the re-fit's replay through a halt: the same fills (``book.halt_spans``).


def paper_and_replay(state: Path, events: list[Any], gaps: Sequence[tuple[str, str]] = ()) -> tuple[Any, Any]:
    """The paper book's fills on ``events`` and the replay's (each adapter's, its nets checked:
    ``test_trader_replay.legs``) on the database it read, the same quotes
    and the recorded ``gaps`` of the book (the halt's silence)."""
    models(state)
    paper = Day(state, picks=[pick(BARRIER)], rules=Rules(fee_rate=FEE, capital=CAPITAL)).run(events)
    record_quotes(state / "data", [e for e in events if isinstance(e, BestQuote)])
    for start, end in gaps:
        record_gap(state / "data", tat(start), tat(end))
    _, trades, _ = replayed(state, [pick(BARRIER)])
    return paper_legs(list(reversed(paper))), legs(trades)


def test_the_replay_holds_a_trade_through_a_halt_as_the_paper_book(tmp_path: Path) -> None:
    """Bought at 10:00:02.5; the halt known from 10:00:30 to 10:05:00, the auction's quotes in it below
    the stop, the book's feed recorded silent in it: neither sells in the halt, both on 10:05:00's quote."""
    events = [
        *flowing("10:00:00", "10:00:02"),
        TEN,
        BOUGHT,
        *flowing("10:00:03", "10:00:29"),
        halted_from("10:00:30", "10:05:00"),
        *flowing("10:03:00", "10:04:59", bid=69_500, ask=69_600),
        quote("10:05:00", 69_900, 70_000),
    ]

    paper, replay_ = paper_and_replay(tmp_path, events, gaps=[("10:01:30", "10:03:00")])

    held = [(BARRIER, tat("10:00:02.5"), 70_100, 9, tat("10:05:00"), 69_900, TIME)]
    assert replay_ == held, "the replay's fills"
    assert paper == held, "the paper book's fills"


def test_no_time_exit_by_the_clock_in_a_halt(tmp_path: Path) -> None:
    """The last quote at 10:03:58, the halt known from 10:03:59 to 10:06:00 over the 10:04 barrier: the
    clock sells nothing in the halt (at 10:05, the trade is still open); once the first quote after it
    comes, both the book and the replay sell by the time barrier on the quote before it (10:03:58's
    bid, the last one of continuous trading)."""
    before: list[Any] = [
        *flowing("10:00:00", "10:00:02"),
        TEN,
        BOUGHT,
        *flowing("10:00:03", "10:03:58"),
        halted_from("10:03:59", "10:06:00"),
        Tick("10:04:01"),
        Tick("10:05:00"),
    ]
    first_champion(tmp_path / "held")
    [held] = Day(tmp_path / "held", picks=[pick(BARRIER)], rules=Rules(fee_rate=FEE, capital=CAPITAL)).run(
        before
    )
    assert held["exit"] is None, "held through the halt"

    paper, replay_ = paper_and_replay(tmp_path / "both", [*before, quote("10:06:00", 69_900, 70_000)])

    sold = [(BARRIER, tat("10:00:02.5"), 70_100, 9, tat("10:04:00"), 70_000, TIME)]
    assert replay_ == sold, "the replay's fills"
    assert paper == sold, "the paper book's fills"


def test_a_stall_whose_silence_began_just_before_the_halt_is_the_halts(tmp_path: Path) -> None:
    """The last quote at 10:00:29, the halt known from 10:00:30: the silence began a second before the
    halt (within ``stall.HALT_LEAD``), so it is the halt's: nothing sent."""
    day = paper_day(tmp_path)

    day.run(
        [
            *flowing("10:00:00", "10:00:29"),
            halted_from("10:00:30", "10:05:00"),
            Tick("10:00:34.5"),
            Tick("10:01:00"),
            Tick("10:03:00"),
            quote("10:05:00", 70_000, 70_100),
        ]
    )

    assert day.notices == []


def test_a_stall_begun_well_before_the_halt_is_sent_as_its_own(tmp_path: Path) -> None:
    """The last quote at 10:00:20, the halt known from 10:00:30: the quotes had stopped ten seconds
    before it (more than ``stall.HALT_LEAD``): a stall of its own, sent at 30 s and on the quotes' return."""
    day = paper_day(tmp_path)

    day.run(
        [
            *flowing("10:00:00", "10:00:20"),
            Tick("10:00:25.5"),
            halted_from("10:00:30", "10:05:00"),
            Tick("10:00:50"),
            Tick("10:03:00"),
            quote("10:05:00", 70_000, 70_100),
        ]
    )

    assert said(day.notices) == [STALL_SAID, BACK_SAID]
