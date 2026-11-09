"""``krx-quant inverse-replay`` (#606): the down calls replayed through the inverse ETF's recorded quotes.

Input: an engine database as the engine writes it (``tests/trader_doubles.py``'s ``Barrier`` and
``Direction``, displayed 2 s after their bar close) and a data directory as the collector writes it:
005930's integrated book (``record_quotes``), the ETF's KRX book as events or raw frames, the gaps.
Output: each call's trade or why it has none, the report's lines, the exit code and the scheduled job.

The 4-minute barrier throughout unless said: the reference 70,050 won, tick 100, w 2 (the lower barrier
L 69,850, the upper U 70,250), its bar close 10:00, displayed 10:00:02, its time barrier 10:04. Every
price is made up.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from krx_quant import cli
from krx_quant.archive import PRUNED
from krx_quant.config import Settings
from krx_quant.feeds import BOOK, KRX_BOOK, RAW_SCHEMA
from krx_quant.feeds.base import KST
from krx_quant.health import GAP_SCHEMA
from krx_quant.inverse_replay import (
    AGENT_LINE,
    BEFORE_ENTRY,
    BLOCKED,
    DAYS,
    DOWN_BARRIER,
    EXPIRY,
    GAP,
    NO_DAYS,
    NO_ENTRY_QUOTE,
    NO_EXIT_QUOTE,
    NO_RECORDING,
    PAST_LAST_EXIT,
    SPECIAL,
    TARGET_REACHED,
    UP_BARRIER,
    Replay,
    replay_days,
    report,
)
from krx_quant.market_calendar import MarketCalendar
from krx_quant.predictions import DB_NAME, PredictionStore, read_only
from krx_quant.process_log import Ran
from krx_quant.raw_days import RawDays
from krx_quant.recorder import ParquetRecorder
from krx_quant.scheduled import FAILED, JOBS, OK, JobRecord, Night
from krx_quant.scheduled.research import INVERSE_REPLAY, INVERSE_REPLAY_ISSUE, inverse_replay_calls
from krx_quant.trader import SYMBOL
from krx_quant.trader.cells import Cell
from krx_quant.trader.quotes import BestQuote
from tests.kis_frames import krx_book_frame
from tests.trader_doubles import CALENDAR, DAY, FEE, Barrier, Direction, Special, at, quote, record_quotes

TODAY = date(*DAY)  # a Wednesday: 09:00 to 15:30
ETF = "0193L0"
FALL = (0.2, 0.3, 0.5)  # a barrier prediction whose most likely class is down_first
DOWN = (0.1, 0.2, 0.7)  # a direction's down call
# The ETF's book: (received, bid, ask, its time code), "0" continuous trading.
EtfRow = tuple[str, int, int, str]
OPEN_BOOK: list[EtfRow] = [("10:00:03", 9000, 9005, "0")]
# 005930 standing above L at the entry.
STANDING = [quote("10:00:01", 70_000, 70_100)]


def predictions(state: Path, *shown: Barrier | Direction | Special) -> None:
    store = PredictionStore(state / DB_NAME)
    try:
        for s in shown:
            s.write(store)
    finally:
        store.close()


def record_etf(data: Path, rows: Sequence[EtfRow], *, raw: bool = False) -> None:
    """The ETF's book as the collector records it: its best-quote events, or (``raw``) its frames."""
    recorder = ParquetRecorder(data, flush_rows=1_000, flush_seconds=3600)
    for hms, bid, ask, cls in rows:
        received = at(hms)
        day = received.date()
        if raw:
            frame = krx_book_frame(
                {
                    "MKSC_SHRN_ISCD": ETF,
                    "BSOP_HOUR": received.strftime("%H%M%S"),
                    "HOUR_CLS_CODE": cls,
                    "ASKP1": str(ask),
                    "BIDP1": str(bid),
                }
            )
            row = {"received_at": received.astimezone(UTC), "message": frame}
            recorder.record("raw", day, KRX_BOOK.tr_id, ETF, row, RAW_SCHEMA)
            continue
        event = {
            "symbol": ETF,
            "event_time": received.replace(microsecond=0),
            "received_at": received.astimezone(UTC),
            "hour_cls": cls,
            "ask_px_1": ask,
            "ask_qty_1": 10,
            "bid_px_1": bid,
            "bid_qty_1": 10,
        }
        recorder.record("events", day, KRX_BOOK.tr_id, ETF, event, KRX_BOOK.schema)
    recorder.close()


def record_gap(data: Path, feed: str, key: str, start: datetime, end: datetime | None) -> None:
    recorder = ParquetRecorder(data, flush_rows=1_000, flush_seconds=3600)
    row = {"feed": feed, "symbol": key, "start": start, "end": end, "reason": "disconnected: test"}
    recorder.record("gaps", start.date(), feed, key, row, GAP_SCHEMA)
    recorder.close()


def replayed(state: Path, days: Sequence[date] = (TODAY,), calendar: MarketCalendar = CALENDAR) -> Replay:
    data = state / "data"
    with read_only(state / DB_NAME) as db, RawDays(data) as raw:
        assert db is not None
        return replay_days(db, data, raw, calendar, days, FEE)


def one_day(
    tmp_path: Path,
    stock: Sequence[BestQuote],
    etf: Sequence[EtfRow],
    *shown: Barrier | Direction | Special,
    raw: bool = False,
) -> Replay:
    predictions(tmp_path, *(shown or (Barrier("10:00:00", FALL),)))
    record_quotes(tmp_path / "data", [*STANDING, *stock])
    record_etf(tmp_path / "data", etf, raw=raw)
    return replayed(tmp_path)


# --- the trade -----------------------------------------------------------------------------------


def test_a_down_call_buys_at_the_first_ask_and_sells_at_the_bid_once_the_stock_reaches_l(
    tmp_path: Path,
) -> None:
    done = one_day(
        tmp_path,
        [quote("10:00:30", 69_900, 70_000), quote("10:01:00", 69_800, 69_850)],  # its ask at L
        [*OPEN_BOOK, ("10:00:58", 9040, 9045, "0")],
    )

    [trade] = done.all.trades
    assert trade.exit == DOWN_BARRIER
    # Bought at 9,005, sold at 9,040 (the record standing 2 s before), the commission on both sides.
    assert trade.net == pytest.approx(9040 - 9005 - FEE * (9005 + 9040))
    assert trade.net_ticks == pytest.approx(trade.net / 5)
    assert trade.spread_ticks == 1
    assert list(done.cells) == [Cell("barrier", "breiman")]
    assert done.kinds["barrier"].trades == [trade]


def test_the_stock_reaching_u_sells_on_the_up_barrier_and_the_worse_one_wins_a_crossed_quote(
    tmp_path: Path,
) -> None:
    done = one_day(
        tmp_path,
        [quote("10:01:00", 70_250, 69_850)],  # crossed: its bid at U, its ask at L
        [*OPEN_BOOK, ("10:01:00", 8990, 8995, "0")],
    )

    [trade] = done.all.trades
    assert trade.exit == UP_BARRIER
    assert trade.net == pytest.approx(8990 - 9005 - FEE * (9005 + 8990))


def test_without_a_barrier_it_sells_at_the_time_barrier_on_the_record_standing_then(tmp_path: Path) -> None:
    done = one_day(
        tmp_path,
        [quote("10:02:00", 70_000, 70_100)],
        [*OPEN_BOOK, ("10:03:57", 9010, 9015, "0"), ("10:04:01", 9100, 9105, "0")],
    )

    [trade] = done.all.trades
    assert trade.exit == EXPIRY
    assert trade.net == pytest.approx(9010 - 9005 - FEE * (9005 + 9010)), "the record of 10:03:57, 3 s old"


def test_a_stale_record_at_the_exit_sells_on_the_first_after_it_within_five_seconds(tmp_path: Path) -> None:
    done = one_day(tmp_path, [], [*OPEN_BOOK, ("10:03:50", 9010, 9015, "0"), ("10:04:04", 9020, 9025, "0")])

    [trade] = done.all.trades
    assert trade.net == pytest.approx(9020 - 9005 - FEE * (9005 + 9020))


@pytest.mark.parametrize(
    ("etf", "why"),
    [
        ([("10:00:08", 9000, 9005, "0")], "the first record after the display came 6 s after it"),
        ([("10:00:03", 9000, 9005, "B")], "an auction's expected price, not continuous trading"),
        ([("10:00:03", 0, 9005, "0")], "no bid"),
        ([("10:00:03", 9005, 9005, "0")], "locked"),
        ([("10:00:01", 9000, 9005, "0")], "received before the display"),
    ],
)
def test_no_usable_ask_within_five_seconds_of_the_display_buys_nothing(
    tmp_path: Path, etf: list[EtfRow], why: str
) -> None:
    done = one_day(tmp_path, [], [*etf, ("10:03:59", 9010, 9015, "0")])

    assert done.all.trades == [], why
    assert done.all.left_out == {NO_ENTRY_QUOTE: 1}


def test_no_usable_bid_within_five_seconds_of_the_exit_leaves_the_call_out(tmp_path: Path) -> None:
    done = one_day(tmp_path, [], [*OPEN_BOOK, ("10:03:50", 9010, 9015, "0"), ("10:04:06", 9020, 9025, "0")])

    assert done.all.trades == []
    assert done.all.left_out == {NO_EXIT_QUOTE: 1}


def test_a_stock_already_at_l_when_the_share_is_bought_buys_nothing(tmp_path: Path) -> None:
    done = one_day(tmp_path, [quote("10:00:02.5", 69_800, 69_850)], OPEN_BOOK)

    assert done.all.left_out == {TARGET_REACHED: 1}


@pytest.mark.parametrize(
    ("feed", "key"),
    [(KRX_BOOK.tr_id, ETF), (BOOK.tr_id, SYMBOL)],
)
def test_a_gap_of_the_etf_s_book_or_the_stock_s_in_the_window_leaves_the_call_out(
    tmp_path: Path, feed: str, key: str
) -> None:
    record_gap(tmp_path / "data", feed, key, at("10:02:00"), at("10:02:10"))
    record_gap(tmp_path / "data", feed, "000660", at("10:00:00"), None)  # another key's: no matter

    done = one_day(tmp_path, [], [*OPEN_BOOK, ("10:03:59", 9010, 9015, "0")])

    assert done.all.left_out == {GAP: 1}


def test_a_day_without_the_etf_s_quotes_leaves_its_calls_out(tmp_path: Path) -> None:
    predictions(tmp_path, Barrier("10:00:00", FALL))
    record_quotes(tmp_path / "data", STANDING)

    done = replayed(tmp_path)

    assert done.all.left_out == {NO_RECORDING: 1}


def test_a_raw_day_pruned_and_not_read_back_leaves_its_calls_out(tmp_path: Path) -> None:
    # The archive pruned it on the server (#607) and no Object Store is set to read it back from.
    partition = tmp_path / "data" / "raw" / f"date={TODAY}" / f"feed={KRX_BOOK.tr_id}" / f"symbol={ETF}"
    partition.mkdir(parents=True)
    (partition / PRUNED).write_text("{}")
    predictions(tmp_path, Barrier("10:00:00", FALL))
    record_quotes(tmp_path / "data", STANDING)

    done = replayed(tmp_path)

    assert done.all.left_out == {NO_RECORDING: 1}


def test_a_gap_left_open_on_a_raw_only_day_ends_at_the_etf_s_next_record(tmp_path: Path) -> None:
    # The collector stopped with the ETF's book gone since 09:50 (no end kept); the day has its raw
    # frames alone, the first at 09:59:00: the gap ended then, before the 10:00 call's window.
    record_gap(tmp_path / "data", KRX_BOOK.tr_id, ETF, at("09:50:00"), None)
    rows: list[EtfRow] = [("09:59:00", 9000, 9005, "0"), *OPEN_BOOK, ("10:03:59", 9010, 9015, "0")]

    done = one_day(tmp_path, [], rows, raw=True)

    assert done.all.left_out == {}
    assert len(done.all.trades) == 1


def test_a_day_recorded_raw_only_is_read_from_its_frames(tmp_path: Path) -> None:
    rows: list[EtfRow] = [*OPEN_BOOK, ("10:00:58", 9040, 9045, "0")]
    done = one_day(tmp_path, [quote("10:01:00", 69_800, 69_850)], rows, raw=True)

    [trade] = done.all.trades
    assert trade.exit == DOWN_BARRIER
    assert trade.net == pytest.approx(9040 - 9005 - FEE * (9005 + 9040))


# --- the calls ------------------------------------------------------------------------------------


def test_only_explicit_down_calls_are_replayed_and_the_entry_rules_leave_some_out(tmp_path: Path) -> None:
    done = one_day(
        tmp_path,
        [],
        [*OPEN_BOOK, ("10:03:59", 9010, 9015, "0")],
        Barrier("10:00:00", FALL),
        Barrier("10:00:00", (0.7, 0.2, 0.1), predictor="barrier-persist"),  # a rise: not a call
        Barrier("10:00:00", (0.3, 0.4, 0.3), predictor="barrier-climate"),  # a timeout: not a call
        Direction("10:00:00", DOWN),
        Direction("10:00:00", (0.3, 0.5, 0.2), predictor="baseline-persist"),  # flat: not_up, not down
        Direction("10:10:00", DOWN, blocked="no_reference"),
        Direction("10:20:00", DOWN, blocked="special"),
        Barrier("09:03:00", FALL),
        Barrier("15:12:00", FALL),
    )

    assert done.all.calls == 6
    assert len(done.all.trades) == 2
    assert done.all.left_out == {BLOCKED: 1, SPECIAL: 1, BEFORE_ENTRY: 1, PAST_LAST_EXIT: 1}
    assert {c.kind for c in done.cells} == {"barrier", "direction"}
    assert {c.predictor for c in done.cells} == {"breiman", "consensus-final"}


def test_a_special_window_touching_the_call_s_window_leaves_it_out(tmp_path: Path) -> None:
    done = one_day(
        tmp_path,
        [],
        OPEN_BOOK,
        Barrier("10:00:00", FALL),
        Special("10:03:00", "10:05:00"),
    )

    assert done.all.left_out == {SPECIAL: 1}


# --- the report and the job --------------------------------------------------------------------------


def settings(tmp_path: Path) -> Settings:
    return Settings("key", "secret", data_dir=tmp_path / "data", state_dir=tmp_path, fee_rate=FEE)


def test_the_report_shows_counts_ticks_and_won_and_no_price(tmp_path: Path) -> None:
    one_day(
        tmp_path,
        [quote("10:01:00", 69_800, 69_850)],
        [*OPEN_BOOK, ("10:00:58", 9040, 9045, "0")],
        Barrier("10:00:00", FALL),
        Direction("10:10:00", DOWN, blocked="no_reference"),
    )
    with RawDays(tmp_path / "data") as raw:
        out, code = report(settings(tmp_path), CALENDAR, TODAY, raw)

    assert code == 0
    assert "2026-10-14(수): 아래 호출 2 · 재생 1 · 순손익 합 +32.3원" in out
    assert "아래 호출 2 · 재생 1 · 뺀 것: 엔진이 막음 1" in out
    assert "청산: 아래 장벽 1 · 위 장벽 0 · 만기 0" in out
    assert "  barrier · breiman" in out
    for price in ("9000", "9005", "9040", "9,005", "70000", "70,050", "69850", "69,850"):
        assert price not in out
    assert AGENT_LINE not in out


def recorded(tmp_path: Path, days: Sequence[date]) -> None:
    for day in days:
        (tmp_path / "data" / "events" / f"date={day}" / f"feed={KRX_BOOK.tr_id}" / f"symbol={ETF}").mkdir(
            parents=True
        )


# The trading days up to the test day, latest first.
TRADING = [d for d in (TODAY - timedelta(days=n) for n in range(40)) if CALENDAR.session(d) is not None]


def test_the_first_report_with_twenty_recorded_days_calls_an_agent_once(tmp_path: Path) -> None:
    predictions(tmp_path)
    recorded(tmp_path, TRADING[:DAYS])
    with RawDays(tmp_path / "data") as raw:
        before, _ = report(settings(tmp_path), CALENDAR, TRADING[1], raw)
        out, code = report(settings(tmp_path), CALENDAR, TODAY, raw)
        again, _ = report(settings(tmp_path), CALENDAR, TODAY, raw)

    assert code == 0
    assert f"{DAYS}거래일(기록된 날 모두 {DAYS}일)" in out
    assert AGENT_LINE not in before, "19 days"
    assert AGENT_LINE in out
    assert AGENT_LINE not in again, "told once"


def test_a_night_missed_at_twenty_days_calls_an_agent_on_the_next_report(tmp_path: Path) -> None:
    # The job did not run (or failed) the night the twentieth day came: the next report says it.
    predictions(tmp_path)
    recorded(tmp_path, TRADING[: DAYS + 1])
    with RawDays(tmp_path / "data") as raw:
        out, _ = report(settings(tmp_path), CALENDAR, TODAY, raw)

    assert f"{DAYS}거래일(기록된 날 모두 {DAYS + 1}일)" in out
    assert AGENT_LINE in out


def test_no_recorded_day_exits_3(tmp_path: Path) -> None:
    predictions(tmp_path)
    with RawDays(tmp_path / "data") as raw:
        out, code = report(settings(tmp_path), CALENDAR, TODAY, raw)

    assert code == NO_DAYS == 3
    assert "호가가 기록된 거래일이 없다" in out


def night(day: date, record: JobRecord | None = None) -> Night[None]:
    return Night(day, CALENDAR.is_trading_day(day), "new", record, {}, None)


def ran(day: date, outcome: str = OK) -> JobRecord:
    return JobRecord(day, outcome, datetime(day.year, day.month, day.day, 22, 30, tzinfo=KST), "new", 0)


def test_the_job_runs_every_trading_night_once_and_posts_to_the_issue() -> None:
    due = INVERSE_REPLAY.due
    assert due(night(TODAY))
    assert not due(night(TODAY, ran(TODAY)))
    assert due(night(TODAY + timedelta(days=1), ran(TODAY)))
    assert not due(night(TODAY, ran(TODAY, FAILED))), "a failed run waits for the next trading night"
    assert not due(night(date(2026, 10, 17))), "a Saturday"
    assert INVERSE_REPLAY in JOBS
    assert callable(cli._parser().parse_args(list(INVERSE_REPLAY.command)).run)
    assert cli._parser().parse_args(["inverse-replay", "--day", "2026-10-14"]).day == TODAY
    assert [t.issue for t in INVERSE_REPLAY.targets] == [INVERSE_REPLAY_ISSUE] == [606]


def test_the_job_labels_the_issue_only_when_it_fails_or_calls_an_agent() -> None:
    assert not inverse_replay_calls(Ran(0, "인버스 ETF 재생 (#606)", ""))
    assert inverse_replay_calls(Ran(0, f"...\n{AGENT_LINE} 기록된 날이 20거래일이 됐다.", ""))
    assert inverse_replay_calls(Ran(3, "", ""))
    assert inverse_replay_calls(Ran(0, "", "", timed_out=True))
