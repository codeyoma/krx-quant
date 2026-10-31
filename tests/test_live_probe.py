"""The live account's read by inquiry calls alone (#408, ``krx-quant probe-live-account``): against a
fake KIS live account (``fake_kis.FakeAccount(live=True)``, its codes and messages made up), never
KIS. It sends no order, checks every field the trader's broker reads, keeps its result for the live
mode and prints no key, account or value. Every key and account is made up."""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from krx_quant import kis_orders
from krx_quant.fake_kis import FakeAccount, FakeKis, FakeOrder
from krx_quant.kis_orders import Servers
from krx_quant.live_probe import DIFFERENT, NO_KEYS, PASSED, REFUSED, UNREADABLE, probe_live_account
from krx_quant.scheduled import JOBS, LIVE_PROBE, Facts, JobRecord, Night
from krx_quant.trader import live
from krx_quant.trader.store import DB_NAME
from tests.test_trader_live import LIVE_ACCOUNT, LIVE_KEY, LIVE_SECRET, TODAY, live_settings
from tests.trader_doubles import CALENDAR, at

NIGHT = at("22:40:00")  # the evening of the test day, a trading day


@pytest.fixture
async def kis_live() -> AsyncIterator[tuple[FakeKis, FakeAccount]]:
    fake = FakeKis(app_key=LIVE_KEY, app_secret=LIVE_SECRET)
    await fake.start()
    account = FakeAccount(
        fake, TODAY, account=LIVE_ACCOUNT.replace("-", ""), live=True, improvement=0.0, cash=1_000_000
    ).install()
    account.clock = lambda: NIGHT
    yield fake, account
    await fake.stop()


async def probed(fake: FakeKis, state: Path, **changes: Any) -> tuple[int, str]:
    lines: list[str] = []
    code = await probe_live_account(
        live_settings(state, **changes),
        NIGHT,
        calendar=CALENDAR,
        servers=Servers(fake.rest_url, fake.ws_url),
        out=lines.append,
    )
    return code, "\n".join(lines)


def kept(state: Path) -> list[tuple[int, str]]:
    with closing(sqlite3.connect(state / DB_NAME)) as db:
        return db.execute("SELECT passed, at FROM live_probes ORDER BY id").fetchall()


def check_of(state: Path) -> live.Check:
    with closing(sqlite3.connect(state / DB_NAME)) as db:
        return live.probe_check(db)


async def test_it_reads_the_live_account_by_inquiry_calls_alone_and_passes(
    kis_live: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """The price, the balance, the orderable cash and the day's order inquiry, each on the live
    server's TR IDs; no order sent. Every field the broker reads is there as it reads it: passed, kept,
    and the live mode's check passes. Nothing held and no order that day: those rows are not seen,
    and checked on the first live day instead."""
    fake, _ = kis_live

    code, text = await probed(fake, tmp_path)

    assert code == PASSED
    assert fake.posts_received == [], "no order, no cancel"
    asked = {tr for _, tr, _, _ in fake.rest_requests}
    assert asked == {
        kis_orders.PRICE,
        kis_orders.BALANCE.live,
        kis_orders.ORDERABLE.live,
        kis_orders.FILLS.live,
    }
    assert "주문 TR은 보내지 않는다" in text
    assert "잔고의 합계(output2) 1행: trader가 읽는 dnca_tot_amt(정수)" in text
    assert "주문 체결 조회의 행(output1): 행 없음 → 보지 못함(통과: 첫 실전 날에 본다)" in text
    assert text.splitlines()[-1].startswith("결과: 같다")
    for secret in (LIVE_KEY, LIVE_SECRET, LIVE_ACCOUNT, "7012345601", "70123456", "1,000,000", "1000000"):
        assert secret not in text, "no key, account or value"
    assert [passed for passed, _ in kept(tmp_path)] == [1]
    assert check_of(tmp_path).passed


async def test_an_order_of_the_day_has_its_row_s_fields_checked(
    kis_live: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = kis_live
    account.orders.append(FakeOrder("99001", kis_orders.SIDE_BUY, "005930", 1, 61_000, "100000", day=TODAY))

    code, text = await probed(fake, tmp_path)

    assert code == PASSED
    assert "주문 체결 조회의 행(output1) 1행: trader가 읽는 odno(글자)" in text and "avg_prvs(숫자)" in text


async def test_a_field_the_broker_reads_missing_or_not_a_number_fails_and_keeps_the_live_mode_locked(
    kis_live: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = kis_live
    balance = account._balance

    def short(params: dict[str, str]) -> Any:
        answer: Any = balance(params)
        body, headers = answer
        totals: dict[str, Any] = body["output2"][0]
        totals.pop("tot_evlu_amt")
        totals["prvs_rcdl_excc_amt"] = "-"
        return body, headers

    fake.rest_responses[kis_orders.BALANCE.live] = short

    code, text = await probed(fake, tmp_path)

    assert code == DIFFERENT
    assert "없는 것: tot_evlu_amt · 꼴이 다른 것: prvs_rcdl_excc_amt → 다르다" in text
    assert [passed for passed, _ in kept(tmp_path)] == [0]
    found = check_of(tmp_path)
    assert not found.passed and "통과하지 못했다" in found.said


async def test_an_answer_without_its_data_is_unreadable(
    kis_live: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, _ = kis_live
    fake.rest_responses[kis_orders.ORDERABLE.live] = lambda params: {
        "rt_cd": "1",
        "msg_cd": "FK9",
        "msg1": "x",
    }

    code, text = await probed(fake, tmp_path)

    assert code == UNREADABLE
    assert "KIS가 데이터로 답하지 않았다" in text and not check_of(tmp_path).passed


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"trader_live_kis_app_key": None}, NO_KEYS),
        ({"trader_live_kis_account": None}, NO_KEYS),
        ({"trader_live_kis_app_key": "PScollector" + "c" * 25}, REFUSED),  # the collector's key
        ({"trader_live_kis_account": "50123456-01"}, REFUSED),  # the mock account
    ],
)
async def test_without_the_live_key_or_with_another_s_it_sends_nothing(
    kis_live: tuple[FakeKis, FakeAccount], tmp_path: Path, change: dict[str, Any], code: int
) -> None:
    fake, _ = kis_live

    assert (await probed(fake, tmp_path, **change))[0] == code

    assert fake.rest_requests == [] and fake.posts_received == [] and fake.tokens_issued == []
    assert not (tmp_path / DB_NAME).exists(), "nothing kept"


def night(day: date, facts: Facts, record: JobRecord | None = None) -> Night:
    return Night(day, CALENDAR.is_trading_day(day), "abc", record, {}, facts)


def test_the_job_runs_every_trading_night_once_the_live_key_is_set_until_a_run_passes() -> None:
    assert LIVE_PROBE in JOBS
    assert (LIVE_PROBE.name, LIVE_PROBE.command) == ("probe-live-account-408", ("probe-live-account",))
    assert LIVE_PROBE.timeout == timedelta(minutes=5) and LIVE_PROBE.session is None
    assert [t.issue for t in LIVE_PROBE.targets] == [417]
    assert "주문은 하나도 보내지 않는다" in LIVE_PROBE.targets[0].note
    keyed, unkeyed = Facts(trader_live_kis=True), Facts()
    wednesday, saturday = date(2026, 10, 14), date(2026, 10, 17)
    started = datetime(2026, 10, 14, 22, 40)
    failed = JobRecord(wednesday, "failed", started, "abc", DIFFERENT, 3.0, False)
    passed = JobRecord(wednesday, "ok", started, "abc", PASSED, 3.0, False)

    assert not LIVE_PROBE.due(night(wednesday, unkeyed)), "without the live key"
    assert not LIVE_PROBE.due(night(saturday, keyed)), "a trading night"
    assert LIVE_PROBE.due(night(wednesday, keyed))
    assert not LIVE_PROBE.due(night(wednesday, keyed, failed)), "once a night"
    assert LIVE_PROBE.due(night(date(2026, 10, 15), keyed, failed)), "again the next trading night"
    assert not LIVE_PROBE.due(night(date(2026, 10, 15), keyed, passed)), "done once passed"
