"""The fill notice's columns 10 and 17 (#648): told apart on the recorded orders of more than one
share that something filled of, the notices lost while the recorder was disconnected counted, and
the night job ``fill-notice-columns-648`` due once ``APART`` such orders are recorded.

The records are in the mock server's 23 columns (``kis_orders.MOCK_FILL_NOTICE_COLUMNS``), written
here for a case or told by the fake KIS account while the mock book trades (``tests/kis_doubles.py``).
Every value is made up.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from contextlib import closing
from datetime import date
from pathlib import Path

import pytest

from krx_quant.fake_kis import FakeAccount, FakeKis
from krx_quant.kis_orders import MOCK_FILL_NOTICE_COLUMNS, SIDE_BUY, SIDE_SELL, Servers
from krx_quant.process_log import Ran
from krx_quant.scheduled import FILL_NOTICE_COLUMNS, JOBS, OK, JobRecord, Night
from krx_quant.trader import notice_columns
from krx_quant.trader.fill_notices import DB_NAME, TR_ID, Recorded, StoredConnection, StoredRecord
from krx_quant.trader.notice_columns import (
    APART,
    DONE,
    EXAMPLE,
    NEITHER,
    NOT_APART,
    PARTIAL,
    REVERSED,
    TOO_FEW,
    apart_orders,
    fill_notice_columns,
    missing_of,
    render,
    told_orders,
    verdict,
)
from krx_quant.trader.notice_layout import BUY, CANCEL, SELL, Gathered, Known, Order, measure
from tests.kis_doubles import BOUGHT, TEN, Fill, kis_mock
from tests.test_notice_layout import night_settings, trade_and_record
from tests.trader_doubles import CALENDAR, at, quote

__all__ = ["kis_mock"]  # a fixture

HTS_ID = "notice7hts"
ACCOUNT_DIGITS = "5012345601"
DAY = date(2026, 10, 14)
NIGHT = at("23:00:00")
KNOWN = Known(frozenset({ACCOUNT_DIGITS, ACCOUNT_DIGITS[:8]}), HTS_ID)
PRICE, FILL_PRICE = 70_100, 70_050
COLUMNS = MOCK_FILL_NOTICE_COLUMNS


def told(
    number: str,
    side: str,
    quantity: int,
    *,
    filled: str = "1",
    shares: int | None = None,
    hms: str = "100002",
    swap: bool = False,
) -> tuple[str, ...]:
    """A record in the mock server's 23: an acceptance (``filled`` 1) or a fill of ``shares``;
    ``swap`` puts the order's quantity in column 10 and the fill's in 17."""
    fill_qty = str(quantity if shares is None else shares)
    values = {
        "CUST_ID": HTS_ID,
        "ACNT_NO": ACCOUNT_DIGITS,
        "ODER_NO": number,
        "OODER_NO": "",
        "SELN_BYOV_CLS": side,
        "RCTF_CLS": "0",
        "ODER_KIND": "00",
        "ODER_COND": "0",
        "STCK_SHRN_ISCD": "005930",
        "CNTG_QTY": str(quantity) if swap else fill_qty,
        "CNTG_UNPR": str(FILL_PRICE) if filled == "2" else str(PRICE),
        "STCK_CNTG_HOUR": hms,
        "RFUS_YN": "0",
        "CNTG_YN": filled,
        "ACPT_YN": "1",
        "BRNC_NO": "01790",
        "ODER_QTY": fill_qty if swap else str(quantity),
        "ACNT_NAME": HTS_ID,
        "CNTG_ISNM": "삼전",
        "CRDT_CLS": "10",
        "CRDT_LOAN_DATE": "",
        "CNTG_ISNM40": "삼성전자",
        "ODER_PRC": str(PRICE),
    }
    return tuple(values[c] for c in COLUMNS)


def number(i: int) -> str:
    return f"{31400 + i:010d}"


def order(i: int, quantity: int = 3, filled: int = 3, hms: str = "100002") -> Order:
    return Order(BUY, number(i), "", SIDE_BUY, "005930", quantity, PRICE, filled, FILL_PRICE, hms)


def records_of(
    i: int, quantity: int = 3, fills: Sequence[int] = (2, 1), *, swap: bool = False
) -> list[tuple[str, ...]]:
    """An order's acceptance and its fills."""
    found = [told(number(i), SIDE_BUY, quantity, swap=swap)]
    found += [told(number(i), SIDE_BUY, quantity, filled="2", shares=s, swap=swap) for s in fills]
    return found


def gathered(
    orders: Sequence[Order],
    fields: Sequence[tuple[str, ...]],
    connections: Sequence[StoredConnection] = (),
) -> Gathered:
    kept = [StoredRecord(1, n, at("10:00:02"), DAY, TR_ID, 1, 0, f) for n, f in enumerate(fields, 1)]
    layout = measure(Recorded(kept, [], list(connections), []), {DAY: orders}, KNOWN, CALENDAR)
    return Gathered(DAY, layout, {DAY: orders}, CALENDAR)


def many(n: int, *, swap: bool = False, fills: Sequence[int] = (2, 1)) -> Gathered:
    return gathered(
        [order(i) for i in range(n)],
        [f for i in range(n) for f in records_of(i, fills=fills, swap=swap)],
    )


def line_of(text: str, column: int) -> str:
    [line] = [x for x in text.splitlines() if x.startswith(f"    칸 {column}(")]
    return line


def assert_no_value(text: str, numbers: Sequence[str]) -> None:
    for value in [
        *numbers,
        *(n.lstrip("0") for n in numbers),
        ACCOUNT_DIGITS,
        ACCOUNT_DIGITS[:8],
        HTS_ID,
        str(PRICE),
        str(FILL_PRICE),
        f"{PRICE:,}",
    ]:
        assert value not in text, value


# The verdict


def test_the_example_s_reading_holds_when_17_is_the_order_s_and_10_sums_to_the_fills() -> None:
    """20 orders of 3 shares, each filled 2 then 1: the fills' column 10 differs from 17."""
    run = many(APART)
    told_ = told_orders(run.layout)
    assert len(told_) == APART and verdict(told_) == EXAMPLE

    text = render(run)
    assert "레코드가 묶인 것 20건(매수 20건) · 체결된 것 20건" in text
    assert "그 레코드 60개: 접수(칸 14가 1) 20개, 체결(칸 14가 2) 40개" in text
    assert "칸 10과 17이 다른 레코드 40개" in text
    assert line_of(text, 10) == "    칸 10(`CNTG_QTY`): 주문 수량 20/60 · 접수 20/20 · 체결 합 20/20"
    assert line_of(text, 17) == "    칸 17(`ODER_QTY`): 주문 수량 60/60 · 접수 20/20 · 체결 합 0/20"
    assert "판정: 가려졌다, 예제대로다" in text
    assert_no_value(text, [number(i) for i in range(APART)])


def test_the_columns_read_the_other_way_round_are_told() -> None:
    assert verdict(told_orders(many(APART, swap=True).layout)) == REVERSED


def test_orders_filled_at_once_do_not_tell_the_columns_apart() -> None:
    run = many(APART, fills=(3,))
    assert verdict(told_orders(run.layout)) == NOT_APART
    assert "칸 10과 17이 다른 레코드 0개" in render(run)


def test_a_column_off_on_one_order_is_neither_reading() -> None:
    run = gathered(
        [order(i) for i in range(APART)],
        [f for i in range(APART) for f in records_of(i, fills=(2, 1) if i else (2, 2))],
    )
    assert verdict(told_orders(run.layout)) == NEITHER


def test_fewer_orders_than_the_job_waits_for_are_too_few() -> None:
    assert verdict(told_orders(many(APART - 1).layout)) == TOO_FEW


def test_orders_of_one_share_and_cancels_do_not_tell_the_columns() -> None:
    """The practice's one-share orders (#531's 108 records) and a cancel are not measured."""
    one = order(0, quantity=1, filled=1)
    cancel = Order(CANCEL, number(1), number(2), SIDE_SELL, "005930", 3, PRICE, 0, 0.0, "100100")
    resting = Order(SELL, number(2), "", SIDE_SELL, "005930", 3, PRICE, 0, 0.0, "100000")
    run = gathered(
        [one, cancel, resting],
        [
            *records_of(0, quantity=1, fills=(1,)),
            told(number(1), SIDE_SELL, 3),
            told(number(2), SIDE_SELL, 3),
        ],
    )
    assert told_orders(run.layout) == []
    assert "그날들의 주문 중 0건" in render(run)


# The disconnections


def test_an_order_placed_while_no_connection_was_subscribed_is_counted_when_its_notice_is_missing() -> None:
    """Subscribed 08:20–10:00 and 10:05–15:40: the order of 10:02 has no record (lost in the gap),
    the order of 11:00 has none either (not in a gap), the order of 10:03 has its records."""
    connections = [
        StoredConnection(1, DAY, at("08:20:00"), at("08:20:00"), at("09:59:00"), at("10:00:00"), "x"),
        StoredConnection(2, DAY, at("10:05:00"), at("10:05:00"), at("15:40:00"), at("15:40:00"), None),
    ]
    run = gathered(
        [order(0, hms="100200"), order(1, hms="110000"), order(2, hms="100300")],
        records_of(2),
        connections,
    )
    missing = missing_of(run)
    assert (missing.orders, missing.unnoticed, missing.during, missing.unknown, missing.placed_during) == (
        3,
        2,
        1,
        0,
        2,
    )
    text = render(run)
    assert "기록 창 안에서 구독되지 않은 때 1번, 모두 5분 0초" in text
    assert "묶인 레코드가 없는 것 2건: 구독되지 않은 때 낸 것 1건, 시각을 모르는 것 0건" in text


# The job


def night(facts: int, record: JobRecord | None = None, day: date = date(2026, 11, 19)) -> Night[int]:
    return Night(day, CALENDAR.is_trading_day(day), "c0ffee", record, {}, facts)


def test_the_job_waits_for_enough_orders_and_runs_once() -> None:
    assert FILL_NOTICE_COLUMNS in JOBS and FILL_NOTICE_COLUMNS.command == ("fill-notice-columns",)
    assert [t.issue for t in FILL_NOTICE_COLUMNS.targets] == [648]
    due = FILL_NOTICE_COLUMNS.due
    assert not due(night(APART - 1))
    assert due(night(APART))
    assert not due(night(APART, day=date(2026, 11, 21)))  # a Saturday
    thursday = date(2026, 11, 19)
    ran = JobRecord(thursday, OK, at("22:30:00"), "c0ffee", 0, 30.0)
    assert not due(night(APART, ran, date(2026, 11, 20)))
    failed = JobRecord(thursday, "failed", at("22:30:00"), "c0ffee", 4, 30.0)
    assert not due(night(APART, failed, thursday))
    assert due(night(APART, failed, date(2026, 11, 20)))
    # Too few orders joined to a notice (3): again the next trading night, posted without a label.
    too_few = JobRecord(thursday, "failed", at("22:30:00"), "c0ffee", PARTIAL, 30.0)
    assert due(night(APART, too_few, date(2026, 11, 20)))
    label = FILL_NOTICE_COLUMNS.label_if
    assert label is not None
    assert not label(Ran(PARTIAL, "", "")) and label(Ran(DONE, "", "")) and label(Ran(4, "", ""))


# The run, on what the recorder kept while the mock book traded


@pytest.mark.timeout(60)
async def test_a_partly_filled_take_profit_tells_the_columns_and_no_value_is_shown(
    kis_mock: tuple[FakeKis, FakeAccount],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The buy filled whole, its take-profit filled 5 shares, the time's sell the rest: three orders
    of more than one share, the take-profit's fill telling 5 against its quantity."""
    fake, account = kis_mock
    events = [
        TEN,
        BOUGHT,
        Fill("10:02:00", 5),
        quote("10:03:59.5", 70_000, 70_100),
        quote("10:04:00.2", 70_100, 70_200),
    ]
    await trade_and_record(fake, account, tmp_path, events, columns=tuple(MOCK_FILL_NOTICE_COLUMNS))
    assert apart_orders(tmp_path) == 3
    monkeypatch.setattr(notice_columns, "APART", 3)

    settings, servers = night_settings(fake, tmp_path), Servers(fake.rest_url, fake.ws_url)
    code = await fill_notice_columns(settings, NIGHT, servers=servers)
    text = capsys.readouterr().out

    assert code == DONE, text
    assert text.startswith("체결 통보 칸 10과 17 가르기 (#648) · 2026-10-14(수) 밤\n")
    assert "  - 거래일 1일: 2026-10-14\n" in text
    assert "그날들의 주문 중 3건(그중 일부만 체결된 것 1건)" in text
    assert "레코드가 묶인 것 3건(매수 1건, 매도 2건) · 체결된 것 3건" in text
    assert "    칸 10(`CNTG_QTY`): 주문 수량 5/6 · 접수 3/3 · 체결 합 3/3" in text
    assert "    칸 17(`ODER_QTY`): 주문 수량 6/6 · 접수 3/3 · 체결 합 2/3" in text
    assert "칸 10과 17이 다른 레코드 1개" in text
    assert "판정: 가려졌다, 예제대로다" in text, text
    numbers = [o.number for o in account.orders]
    assert_no_value(text, numbers)
    for o in account.orders:
        if o.price:  # a cancel's is 0
            assert str(o.price) not in text and f"{o.price:,}" not in text

    # The ledger counts the orders, but fewer are joined to a notice than the job waits for: 3, and
    # the job runs again.
    monkeypatch.setattr(notice_columns, "APART", 4)
    assert await fill_notice_columns(settings, NIGHT, servers=servers) == PARTIAL
    assert "판정: 가르기에 모자란다" in capsys.readouterr().out

    # A day the recorder asked for connections on but kept no record of: its orders are read, and
    # each counts as missing its notice.
    with closing(sqlite3.connect(tmp_path / DB_NAME)) as db, db:
        db.execute("DELETE FROM records")
    assert await fill_notice_columns(settings, NIGHT, servers=servers) == PARTIAL
    text = capsys.readouterr().out
    assert "  - 거래일 1일: 2026-10-14\n" in text
    assert "그날들의 주문 4건(취소 포함) 중 묶인 레코드가 없는 것 4건" in text


def test_without_the_files_no_order_is_counted(tmp_path: Path) -> None:
    assert apart_orders(tmp_path) == 0
