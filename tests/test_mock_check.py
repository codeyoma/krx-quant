"""The mock day's check (#405, its third acceptance criterion): ``krx-quant check-mock-trades``,
which compares a mock trading day's orders in the trader's ledger with KIS's day order inquiry,
order by order. Its scheduled job ``check-mock-trades-405`` ran on 2026-11-02 and left ``JOBS``
(#505).

The ledger is made by the mock book on the fake KIS (``tests/kis_doubles.py``'s ``MockDay``),
whose account the check then reads. Every value is made up.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

from krx_quant.fake_kis import FakeAccount, FakeKis
from krx_quant.feeds.base import KST
from krx_quant.kis_orders import Servers
from krx_quant.scheduled import FAILED
from krx_quant.trader import MOCK, PAPER
from krx_quant.trader.mock_check import (
    DIFFERENT,
    NO_ORDERS,
    PROBE_JOB,
    SAME,
    UNREADABLE,
    check_mock_trades,
    mock_days,
)
from krx_quant.trader.store import DB_NAME as TRADER_DB
from krx_quant.trader.store import TraderStore
from tests.kis_doubles import ACCOUNT, BOUGHT, TEN, MockDay, kis_mock, settings_of
from tests.scheduled_doubles import COMMIT, THURSDAY, at
from tests.trader_doubles import quote

__all__ = ["kis_mock"]  # the fixture, used here

DAY = date(2026, 10, 14)  # the trading day of ``tests/trader_doubles.py``


def test_the_mock_days_are_those_with_a_mock_ledger(tmp_path: Path) -> None:
    assert mock_days(tmp_path) == ()
    store = TraderStore(tmp_path / TRADER_DB)
    try:
        row: dict[str, Any] = {"realized": 0.0, "trades": 0, "worst": 0.0, "cumulative": 0.0}
        row["cumulative_worst"] = 0.0
        row |= {"daily_limit": 1.0, "cumulative_limit": 2.0, "status": "running", "halt": None}
        store.put_day(PAPER, date(2026, 10, 13), row, at(THURSDAY, "10:00"))
        store.put_day(MOCK, date(2026, 10, 15), row, at(THURSDAY, "10:00"))
        store.put_day(MOCK, date(2026, 10, 14), row, at(THURSDAY, "10:00"))
    finally:
        store.close()
    assert mock_days(tmp_path) == (date(2026, 10, 14), date(2026, 10, 15))


# The command


async def check(fake: FakeKis, state: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    evening = datetime(2026, 10, 14, 23, 0, tzinfo=KST)
    servers = Servers(fake.rest_url, fake.ws_url)
    code = await check_mock_trades(settings_of(fake, state), evening, DAY, servers=servers)
    return code, capsys.readouterr().out


async def test_a_mock_day_s_orders_are_the_same_in_the_ledger_and_on_kis(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake, account = kis_mock
    await MockDay(tmp_path, fake, account).run([TEN, BOUGHT, quote("10:01:30", 69_800, 69_900)])
    capsys.readouterr()

    code, out = await check(fake, tmp_path, capsys)

    assert code == SAME
    assert out.startswith("KIS 모의 매매 대조 (#405) · 2026-10-14(수) · 005930")
    assert "- 장부(state/trader.sqlite3, mock, 챔피언의 매매): 매매 1건, 주문 3건(매수 1, 매도 2)" in out
    assert "체결 매수 14주 · 매도 14주" in out
    assert "- KIS 주문 체결 조회: 주문 3건(매수 1, 매도 2), 취소 1건, 체결 매수 14주 · 매도 14주" in out
    assert "1. 매수(진입) 10:00:02 · 주문 14주 ↔ 14주 · 체결 14주 ↔ 14주 · 체결가 차이 +0.00틱: 같다" in out
    assert "2. 매도(익절) 10:00:02 · 주문 14주 ↔ 14주 · 체결 0주 ↔ 0주 · 체결가 차이 +0.00틱: 같다" in out
    assert "3. 매도(손절) 10:01:30 · 주문 14주 ↔ 14주 · 체결 14주 ↔ 14주 · 체결가 차이 +0.00틱: 같다" in out
    assert out.rstrip().endswith("결과: 같다: 장부의 주문 3건이 모두 KIS 주문 체결 조회와 같다")
    # No order number, account, key or price.
    for o in account.orders:
        assert o.number not in out and o.number.lstrip("0") not in out
    assert ACCOUNT.split("-")[0] not in out and fake.app_key not in out
    assert not re.search(r"\d{2},?\d{3}원|\b(69|70)[,]?\d{3}\b", out), "no price"


async def test_a_buy_of_which_nothing_filled_is_compared_as_the_ledger_keeps_it(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A buy KIS took and cancelled with nothing filled opened no trade: it is the ledger's all the
    same, not an order KIS has that the ledger lacks."""
    fake, account = kis_mock
    account.fills = False
    assert await MockDay(tmp_path, fake, account).run([TEN, BOUGHT]) == []
    capsys.readouterr()

    code, out = await check(fake, tmp_path, capsys)

    assert code == SAME
    assert "매매 0건, 주문 1건(매수 1, 매도 0)" in out
    assert "1. 매수(진입) 10:00:02 · 주문 14주 ↔ 14주 · 체결 0주 ↔ 0주 · 체결가 차이 +0.00틱: 같다" in out
    assert "장부에 없는 KIS의 주문\n- 없음" in out


async def test_a_kis_order_the_ledger_lacks_is_a_difference_unless_the_probe_placed_it(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake, account = kis_mock
    await MockDay(tmp_path, fake, account).run([TEN, BOUGHT])
    account._order({"PDNO": "005930", "ORD_QTY": "1", "ORD_UNPR": str(account.ask)})  # placed on KIS alone
    account.orders[-1].at = "100005"
    capsys.readouterr()

    code, out = await check(fake, tmp_path, capsys)
    assert code == DIFFERENT
    assert "장부에 없는 KIS의 주문\n- 10:00:05 매수 1주, 체결 1주" in out
    assert "결과: 다르다" in out

    probe: dict[str, Any] = {
        "day": DAY.isoformat(),
        "outcome": FAILED,
        "started_at": "2026-10-14T10:00:00+09:00",
    }
    probe |= {"commit": COMMIT, "exit_code": 5, "seconds": 47.0}
    (tmp_path / "scheduled.json").write_text(json.dumps({"jobs": {PROBE_JOB: probe}}))
    code, out = await check(fake, tmp_path, capsys)
    assert code == SAME
    assert f"주문 프로브(#403, {PROBE_JOB})가 돈 동안의 주문 1건은 프로브의 것이라 세지 않는다" in out


async def test_a_ledger_order_kis_does_not_show_is_a_difference(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake, account = kis_mock
    await MockDay(tmp_path, fake, account).run([TEN, BOUGHT])
    account.orders = [o for o in account.orders if o.side == "02"]  # the take-profit sell gone from KIS
    capsys.readouterr()

    code, out = await check(fake, tmp_path, capsys)

    assert code == DIFFERENT
    assert "KIS에 없는 장부의 주문\n- 매도(익절) 10:00:02 14주, 체결 0주" in out


async def test_a_day_without_orders_has_nothing_to_compare(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake, account = kis_mock
    await MockDay(tmp_path, fake, account).run([quote("10:00:00", 70_000, 70_100)])
    capsys.readouterr()

    code, out = await check(fake, tmp_path, capsys)

    assert code == NO_ORDERS and "견줄 주문이 없다" in out


async def test_without_the_trader_s_store_the_check_says_so(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake, _ = kis_mock

    code, out = await check(fake, tmp_path, capsys)

    assert code == UNREADABLE and "trader의 저장소(state/trader.sqlite3)가 없다" in out
    assert fake.rest_requests == [], "KIS is not asked"
