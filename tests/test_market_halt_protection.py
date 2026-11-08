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
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from krx_quant.collector import run_collector
from krx_quant.fake_kis import FakeKis
from krx_quant.feeds import KST
from krx_quant.special import AUCTION, HALTED, REOPENED, Kind, Window
from krx_quant.trader.cells import Kept
from krx_quant.trader.replay import known
from tests.collector_doubles import FIXED_NOW, TRADE_KEY, Clock, read, settings_for
from tests.kis_frames import book_frame, index_frame, trade_frame
from tests.replay_events import at, run
from tests.test_market_halt import halt_morning, market
from tests.test_resilience import with_telegram
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


async def test_a_confirmed_halt_is_one_notice_and_its_reopening_another_with_no_feed_outage(
    kis: FakeKis, tmp_path: Path
) -> None:
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
    await until(lambda: any(m["text"].startswith(HALT_NOTICE) for m in kis.telegram_messages))
    # Five minutes without trades or books, the index going on.
    for minute in range(2, 8):
        when = FIXED_NOW + timedelta(minutes=minute)
        await at_(when, index_at(when))
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

    await run(db, [*halt_morning(), *market()], until=at("09:50:00"))

    with sqlite3.connect(db) as conn:
        statuses = dict(
            conn.execute(
                "SELECT substr(bar_close, 12, 5), group_concat(DISTINCT status) FROM predictions GROUP BY bar_close"
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


# --- The trader.

HALT = Window(Kind.MARKET_HALT, None, at("09:03:32"), at("09:35:10"), REOPENED)


def test_the_replay_knows_a_window_from_when_the_engine_learned_it() -> None:
    kept = Kept(Kind.VI, "005930", at("09:30:20"), at("09:32:40"), "dynamic", at("09:30:24"))
    before = Kept(Kind.SIDECAR, None, at("09:21:00"), at("09:26:00"))  # kept before #625: its start

    assert known([kept, before], at("09:30:22")) == [before]
    assert known([kept, before], at("09:30:24")) == [kept, before]


def test_the_phases_are_named_as_the_docs_say() -> None:
    assert (HALTED, AUCTION, REOPENED) == ("정지", "단일가", "재개")
