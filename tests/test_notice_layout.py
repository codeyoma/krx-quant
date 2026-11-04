"""The fill notice layout's night (#484, #403): the recorded records joined to the one order of their
day they name, and measured column by column without a value; the job ``fill-notice-layout-403``.

The records are the fake KIS account's (``fake_kis.FakeAccount``, in a made-up 23-column layout that
is not the example's first 23), recorded by the trader's recorder while the mock book trades
(``tests/kis_doubles.py``'s ``MockDay``), or written here for a case. Every value is made up.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path

import aiohttp
import pytest

from krx_quant import kis
from krx_quant.clock import FakeClock
from krx_quant.config import Settings
from krx_quant.fake_kis import FakeAccount, FakeKis
from krx_quant.feeds.base import KST
from krx_quant.kis_orders import FILL_NOTICE_COLUMNS, SIDE_BUY, SIDE_SELL, Servers
from krx_quant.process_log import Ran
from krx_quant.scheduled import (
    FAILED,
    FILL_NOTICE_LAYOUT,
    INTERRUPTED,
    JOBS,
    OK,
    Facts,
    JobRecord,
    Night,
    fill_notice_layout_calls,
)
from krx_quant.trader import fill_notices
from krx_quant.trader.fill_notices import (
    TR_ID,
    Recorded,
    StoredConnection,
    StoredRecord,
    read_recorded,
)
from krx_quant.trader.notice_layout import (
    BUY,
    CANCEL,
    DONE,
    PARTIAL,
    SELL,
    UNREADABLE,
    Known,
    Order,
    candidates,
    fill_notice_layout,
    gaps,
    measure,
    render,
)
from tests.kis_doubles import BOUGHT, TEN, Fill, MockDay, kis_mock, settings_of
from tests.trader_doubles import CALENDAR, at, quote
from tests.waiting import until

__all__ = ["kis_mock"]  # a fixture

HTS_ID = "notice7hts"
ACCOUNT_DIGITS = "5012345601"  # tests.kis_doubles.ACCOUNT's
NAME = "홍길동"
# A made-up 23 of the example's 26 columns, not its first 23: the order's price last.
LAYOUT = tuple(c for c in FILL_NOTICE_COLUMNS if c not in ("ORD_COND_PRC", "ORD_EXG_GB", "POPUP_YN"))
DAY = date(2026, 10, 14)
NIGHT = at("23:00:00")
PACE = fill_notices.Pace(backoff=0.05, most=0.2, silence=5.0, heard=0.0, close=0.2, look=0.02)
KNOWN = Known(frozenset({ACCOUNT_DIGITS, ACCOUNT_DIGITS[:8]}), HTS_ID)


def told(
    number: str,
    side: str,
    quantity: int,
    price: int,
    *,
    filled: str = "1",
    correction: str = "0",
    original: str = "",
    shares: int | None = None,
    at_price: int = 0,
    hms: str = "100002",
) -> list[str]:
    """A record as the fake account tells it (``FakeAccount._tell``), in ``LAYOUT``: an order's
    acceptance (``filled`` 1, its quantity) or a fill (``filled`` 2, ``shares`` at ``at_price``)."""
    values = {
        "CUST_ID": HTS_ID,
        "ACNT_NO": ACCOUNT_DIGITS,
        "ODER_NO": number,
        "OODER_NO": original,
        "SELN_BYOV_CLS": side,
        "RCTF_CLS": correction,
        "ODER_KIND": "00",
        "ODER_COND": "0",
        "STCK_SHRN_ISCD": "005930",
        "CNTG_QTY": str(quantity if shares is None else shares),
        "CNTG_UNPR": str(at_price) if filled == "2" else "0",
        "STCK_CNTG_HOUR": hms,
        "RFUS_YN": "0",
        "CNTG_YN": filled,
        "ACPT_YN": "2" if correction == "2" else "1",
        "BRNC_NO": "01790",
        "ODER_QTY": str(quantity),
        "ACNT_NAME": NAME,
        "FILLER": "",
        "CRDT_CLS": "10",
        "CRDT_LOAN_DATE": "",
        "CNTG_ISNM40": "삼성전자",
        "ODER_PRC": str(price),
    }
    return [values[c] for c in LAYOUT]


def stored(fields: Sequence[str], n: int) -> StoredRecord:
    """A record kept, received when its time field says (``told``'s ``hms``)."""
    hms = fields[LAYOUT.index("STCK_CNTG_HOUR")]
    return StoredRecord(1, n, at(f"{hms[:2]}:{hms[2:4]}:{hms[4:]}"), DAY, TR_ID, 1, 0, tuple(fields))


BUY_NO, SELL_NO, REST_NO, CANCEL_NO, OTHER_NO = (
    "0000031401",
    "0000031402",
    "0000031403",
    "0000031404",
    "0000031405",
)
ORDERS = [
    # 3 bought in two fills, 2 at 70,050 and 1 at 70,150: 70,083.33 on average
    Order(BUY, BUY_NO, "", SIDE_BUY, "005930", 3, 70_100, 3, 210_250 / 3, "100002"),
    Order(SELL, SELL_NO, "", SIDE_SELL, "005930", 3, 70_300, 3, 70_300.0, "100200"),
    Order(SELL, REST_NO, "", SIDE_SELL, "005930", 3, 70_400, 0, 0.0, "100010"),
    Order(CANCEL, CANCEL_NO, REST_NO, SIDE_SELL, "005930", 3, 70_400, 0, 0.0, "100150"),
]


def records() -> list[StoredRecord]:
    found = [
        told(BUY_NO, SIDE_BUY, 3, 70_100),
        told(BUY_NO, SIDE_BUY, 3, 70_100, filled="2", shares=2, at_price=70_050),
        told(BUY_NO, SIDE_BUY, 3, 70_100, filled="2", shares=1, at_price=70_150),
        told(REST_NO, SIDE_SELL, 3, 70_400, hms="100010"),
        told(CANCEL_NO, SIDE_SELL, 3, 70_400, correction="2", original=REST_NO, hms="100150"),
        told(SELL_NO, SIDE_SELL, 3, 70_300, hms="100200"),
        told(SELL_NO, SIDE_SELL, 3, 70_300, filled="2", shares=3, at_price=70_300, hms="100200"),
    ]
    return [stored(f, n) for n, f in enumerate(found, 1)]


def column(name: str) -> int:
    return LAYOUT.index(name) + 1  # as the output numbers them


def line_of(text: str, name: str) -> str:
    [line] = [x for x in text.splitlines() if x.startswith(f"    칸 {column(name)}: ")]
    return line


# The join


def test_a_record_is_joined_to_the_one_order_of_its_day_it_names() -> None:
    assert candidates(told(BUY_NO, SIDE_BUY, 3, 70_100), ORDERS) == [ORDERS[0]]
    numbers = ["31401", "x"]
    assert candidates(numbers, ORDERS) == [ORDERS[0]], "leading zeros aside"


def test_a_cancel_s_record_naming_its_order_too_is_the_cancel_s() -> None:
    record = told(CANCEL_NO, SIDE_SELL, 3, 70_400, correction="2", original=REST_NO)
    assert candidates(record, ORDERS) == [ORDERS[3]]


def test_a_record_naming_two_orders_or_none_is_not_joined() -> None:
    assert len(candidates([BUY_NO, SELL_NO], ORDERS)) == 2, "ambiguous"
    assert candidates([OTHER_NO, "70100", "3"], ORDERS) == []
    short = Order(BUY, "0000000012", "", SIDE_BUY, "005930", 1, 70_100, 0, 0.0, "")
    assert candidates(["12", "1"], [short]) == [], "a one- or two-digit code is no order number"


# The measure


def test_each_column_is_measured_against_its_order_and_the_fills_by_order() -> None:
    layout = measure(Recorded(records()), {DAY: ORDERS}, KNOWN, CALENDAR)
    text = render(layout, DAY)

    assert layout.code == DONE and layout.missing == []
    assert (layout.ambiguous, layout.unjoined) == (0, 0)
    assert "  - 23칸 레코드 7개(묶인 것 7개, 체결된 주문 2건)." in text
    assert "주문번호 7/7" in line_of(text, "ODER_NO")
    assert "원주문번호 1/1" in line_of(text, "OODER_NO")
    assert "매수·매도 코드 7/7" in line_of(text, "SELN_BYOV_CLS")
    assert "주문 수량 7/7" in line_of(text, "ODER_QTY")
    assert "주문가 7/7" in line_of(text, "ODER_PRC")
    assert "계좌 7/7" in line_of(text, "ACNT_NO") and "HTS ID 7/7" in line_of(text, "CUST_ID")
    assert "종목 7/7" in line_of(text, "STCK_SHRN_ISCD")
    assert "받은 시각(5초 안) 7/7" in line_of(text, "STCK_CNTG_HOUR")
    # The acceptance tells the order's quantity too: the fills are the records of CNTG_YN's one value.
    quantity = line_of(text, "CNTG_QTY")
    assert f"체결 수량 합 2/2(칸 {column('CNTG_YN')}로 고른 레코드)" in quantity
    assert "주문 수량 5/7" in quantity
    price = line_of(text, "CNTG_UNPR")
    assert f"체결가(칸 {column('CNTG_QTY')}의 수량으로 가중 평균) 2/2" in price
    assert line_of(text, "ACNT_NAME").startswith(f"    칸 {column('ACNT_NAME')}: text · 3자 · 값 1가지")
    assert line_of(text, "FILLER") == f"    칸 {column('FILLER')}: empty · 값 1가지"
    assert "묶인 레코드 7개: 매수 3개(주문 1건), 매도 3개(주문 2건), 취소 1개(주문 1건)" in text
    for value in [
        BUY_NO,
        "31401",
        SELL_NO,
        CANCEL_NO,
        ACCOUNT_DIGITS,
        "50123456",
        HTS_ID,
        NAME,
        "70100",
        "70083",
    ]:
        assert value not in text, value
    assert "70,1" not in text and "70,0" not in text


def test_without_a_cancel_joined_the_run_measured_part() -> None:
    joined = [r for r in records() if CANCEL_NO not in r.fields]
    layout = measure(Recorded(joined), {DAY: ORDERS}, KNOWN, CALENDAR)
    assert (layout.code, layout.missing) == (PARTIAL, [CANCEL])
    assert "아직 묶이지 않은 종류: 취소. 새 레코드가 있는 다음 거래일 밤에 다시 돈다" in render(layout, DAY)


def test_an_ambiguous_record_is_counted_and_not_measured() -> None:
    both = stored([*told(BUY_NO, SIDE_BUY, 3, 70_100)[:-1], SELL_NO], 9)
    layout = measure(Recorded([*records(), both]), {DAY: ORDERS}, KNOWN, CALENDAR)
    assert (layout.ambiguous, sum(len(g.joined) for g in layout.groups.values())) == (1, 7)
    assert "모호(주문 둘 이상에 맞음) 1개, 묶이지 않음 0개" in render(layout, DAY)


def test_a_day_whose_inquiry_was_not_read_leaves_the_run_unreadable() -> None:
    no_cancel = [o for o in ORDERS if o.kind != CANCEL]
    layout = measure(
        Recorded(records()), {DAY: no_cancel}, KNOWN, CALENDAR, unread=[DAY], why_unread="orders: no answer"
    )
    assert (layout.code, layout.missing) == (UNREADABLE, [CANCEL]), "the cancel's record is its order's then"
    text = render(layout, DAY)
    assert "조회를 읽지 못한 날(장부로만 묶었다): 2026-10-14 (orders: no answer)" in text
    assert "조회를 읽지 못한 날이 있어 다음 거래일 밤에 다시 돈다" in text


def test_the_time_no_connection_was_subscribed_within_the_window_is_counted() -> None:
    """08:20–15:40: subscribed from 08:20 to 10:00, then from 10:05 until last heard at 15:00 (no end
    kept: the trader stopped hard), and a connection never subscribed: 5 + 40 minutes."""
    connections = [
        StoredConnection(1, DAY, at("08:20:00"), at("08:20:00"), at("09:59:00"), at("10:00:00"), "x"),
        StoredConnection(2, DAY, at("10:00:00"), None, None, at("10:01:00"), "y"),
        StoredConnection(3, DAY, at("10:05:00"), at("10:05:00"), at("15:00:00"), None, None),
    ]
    gap, windows = gaps(connections, CALENDAR)
    assert gap == timedelta(minutes=45) and windows == [(at("08:20:00"), at("15:40:00"))]


# The run, on what the recorder kept while the mock book traded


async def trade_and_record(
    fake: FakeKis, account: FakeAccount, state: Path, events: Sequence[object]
) -> list[StoredRecord]:
    """The mock book's day on ``fake`` with the recorder subscribed beside it; the records kept once
    every notice the account told is."""
    account.notice_columns = LAYOUT
    account.hts_id = HTS_ID
    pushed: list[int] = []
    tell = account._tell

    def counted(*args: object, **kwargs: object) -> None:
        pushed.append(1)
        tell(*args, **kwargs)  # type: ignore[arg-type]

    account._tell = counted  # type: ignore[method-assign]
    async with aiohttp.ClientSession() as session:

        async def approval() -> str:
            return await kis.issue_approval_key(session, fake.rest_url, fake.app_key, fake.app_secret)

        recording = fill_notices.start(
            session, fake.ws_url, HTS_ID, approval, state, lambda: at("10:00:00"), CALENDAR.session, pace=PACE
        )
        try:
            await until(lambda: fake.subscription_count((TR_ID, HTS_ID)) > 0)
            await MockDay(state, fake, account).run(events)  # type: ignore[arg-type]
            await until(lambda: len((read_recorded(state) or Recorded()).records) == len(pushed))
        finally:
            await fill_notices.stop(recording)
    found = read_recorded(state)
    assert found is not None
    return found.records


def night_settings(fake: FakeKis, state: Path) -> Settings:
    return replace(settings_of(fake, state), trader_kis_hts_id=HTS_ID)


@pytest.mark.timeout(60)
async def test_a_day_with_a_buy_a_sell_and_a_cancel_joined_ends_the_job(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The time barrier: the buy filled, its take-profit cancelled, the time's sell filled."""
    fake, account = kis_mock
    events = [TEN, BOUGHT, quote("10:03:59.5", 70_000, 70_100), quote("10:04:00.2", 70_100, 70_200)]
    kept = await trade_and_record(fake, account, tmp_path, events)

    code = await fill_notice_layout(
        night_settings(fake, tmp_path), NIGHT, servers=Servers(fake.rest_url, fake.ws_url)
    )
    text = capsys.readouterr().out

    assert code == DONE, text
    assert len(kept) == 6 and {len(r.fields) for r in kept} == {23}
    assert text.startswith("체결 통보 칸 재기 (#403, #484) · 2026-10-14(수) 밤\n")
    assert "  - 거래일 1일: 2026-10-14" in text
    assert "KIS 주문 체결 조회 1일 중 1일 읽음(주문 3건, 취소 1건)" in text
    assert "묶인 레코드 6개: 매수 2개(주문 1건), 매도 3개(주문 2건), 취소 1개(주문 1건)" in text
    assert "주문번호 6/6" in line_of(text, "ODER_NO") and "원주문번호 1/1" in line_of(text, "OODER_NO")
    assert "체결 수량 합 2/2" in line_of(text, "CNTG_QTY")
    assert "묶인 매수, 매도, 취소가 모두 있다: 이 작업은 다시 돌지 않는다" in text
    numbers = {o.number for o in account.orders}
    for value in [
        *numbers,
        *(n.lstrip("0") for n in numbers),
        ACCOUNT_DIGITS,
        HTS_ID,
        NAME,
        "70050",
        "70300",
    ]:
        assert value not in text, value


@pytest.mark.timeout(60)
async def test_a_day_without_a_cancel_measures_part(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake, account = kis_mock
    await trade_and_record(fake, account, tmp_path, [TEN, BOUGHT, Fill("10:02:00")])

    code = await fill_notice_layout(
        night_settings(fake, tmp_path), NIGHT, servers=Servers(fake.rest_url, fake.ws_url)
    )

    assert code == PARTIAL
    assert "아직 묶이지 않은 종류: 취소" in capsys.readouterr().out


async def test_without_the_records_or_the_trader_s_key_it_cannot_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = Settings(kis_app_key="", kis_app_secret="", state_dir=tmp_path, trader_kis_hts_id=HTS_ID)
    assert await fill_notice_layout(settings, NIGHT) == UNREADABLE
    assert "결과: 기록(state/fill_notices.sqlite3)이 없다" in capsys.readouterr().out

    store = fill_notices.FillNoticeStore(tmp_path / fill_notices.DB_NAME, fill_notices.Limits(), DAY)
    store.apply(
        [
            fill_notices.Frame(
                1, 1, at("10:00:02"), TR_ID, 1, 9, records=(tuple(told(BUY_NO, SIDE_BUY, 3, 70_100)),)
            )
        ]
    )
    store.close()
    assert await fill_notice_layout(settings, NIGHT) == UNREADABLE
    text = capsys.readouterr().out
    assert "조회를 읽지 못한 날(장부로만 묶었다): 2026-10-14 (KRX_TRADER_KIS_APP_KEY" in text
    assert "묶이지 않음 1개" in text


# The job


def night(when: date, record: JobRecord | None = None, days: tuple[date, ...] = (DAY,)) -> Night:
    return Night(when, CALENDAR.is_trading_day(when), "c" * 40, record, {}, Facts(fill_notice_days=days))


def ran(day: date, code: int) -> JobRecord:
    started = datetime.combine(day, time(22, 30), KST)
    return JobRecord(day, OK if code == 0 else FAILED, started, "c" * 40, code, 9.0)


def test_the_job_posts_to_403_and_calls_an_agent_on_its_last_run_or_a_failure() -> None:
    assert FILL_NOTICE_LAYOUT in JOBS and FILL_NOTICE_LAYOUT.session is None
    assert FILL_NOTICE_LAYOUT.name == "fill-notice-layout-403"
    assert FILL_NOTICE_LAYOUT.command == ("fill-notice-layout",)
    assert [t.issue for t in FILL_NOTICE_LAYOUT.targets] == [403]
    assert FILL_NOTICE_LAYOUT.label == ("ready-for-agent",)
    assert FILL_NOTICE_LAYOUT.timeout == timedelta(minutes=10)
    note = FILL_NOTICE_LAYOUT.targets[0].note
    assert "출력에는 값, 주문번호, 계좌번호, HTS ID, 가격이 없다" in note
    calls = [fill_notice_layout_calls(Ran(code, "", "")) for code in (DONE, PARTIAL, UNREADABLE, 1)]
    assert calls == [True, False, True, True]
    assert fill_notice_layout_calls(Ran(PARTIAL, "", "", timed_out=True)), "timed out"


def test_the_job_runs_on_a_trading_night_with_records_not_judged_until_every_kind_is_joined() -> None:
    due = FILL_NOTICE_LAYOUT.due
    thursday, friday, saturday, monday = (date(2026, 10, d) for d in (15, 16, 17, 19))
    assert due(night(DAY)) and not due(night(DAY, days=())), "records first"
    assert not due(night(saturday, days=(DAY, friday))), "trading nights only"
    assert not due(night(thursday, ran(DAY, DONE), (DAY, thursday))), "done: every kind joined"
    assert not due(night(thursday, ran(DAY, PARTIAL))), "no record after the last run"
    assert due(night(thursday, ran(DAY, PARTIAL), (DAY, thursday))), "a new day's records"
    assert not due(night(DAY, ran(DAY, PARTIAL), (DAY,))), "once a night"
    for code in (UNREADABLE, 1):
        assert due(night(friday, ran(thursday, code), (DAY,))), f"after {code}: the next trading night"
    assert due(night(monday, replace(ran(friday, PARTIAL), outcome=INTERRUPTED), (DAY,))), "cut: again"


async def test_the_night_s_facts_read_the_days_recorded(tmp_path: Path) -> None:
    from krx_quant.scheduled import read_facts
    from tests.daily_doubles import CALENDAR as DAILY_CALENDAR
    from tests.daily_doubles import NIGHT as DAILY_NIGHT
    from tests.daily_doubles import engine_on
    from tests.daily_doubles import settings as daily_settings

    s = daily_settings(tmp_path, signal=0.0, days=30)
    store = fill_notices.FillNoticeStore(s.state_dir / fill_notices.DB_NAME, fill_notices.Limits(), DAY)
    store.apply([fill_notices.Frame(1, 1, at("10:00:02"), TR_ID, 1, 9, why=fill_notices.NO_KEY)])
    store.close()
    clock = FakeClock(DAILY_NIGHT)
    e = engine_on(s, clock, [])
    try:
        facts = await read_facts(s, DAILY_CALENDAR, clock, e)
    finally:
        e.store.close()
    assert facts.fill_notice_days == (DAY,)
