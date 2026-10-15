"""``krx-quant probe-supply`` (#226): where KIS gives the daily supply indicators, asked.

Input: a fake KIS answering the seven queries the survey found, the way their KIS examples
describe them (rows newest first, a cap per call, a refused market, an ETF without lending
data, a gap in short selling, a continuation header). Every value it gives is a sentinel
number. Output: the printed report, which must carry none of them.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from krx_quant import cli, supply_probe
from krx_quant.fake_kis import FakeKis, RestAnswer
from krx_quant.kis import KisError
from krx_quant.probe_fields import flag
from krx_quant.supply_probe import (
    CREDIT,
    DAILY_PRICE,
    INVESTOR,
    INVESTOR_DAILY,
    LOAN,
    PRICE,
    SHORT_SALE,
    kis_error,
    parse,
    probe_supply,
    status,
)
from tests.test_collector import settings_for

TODAY = date(2026, 10, 15)
SHORT_FIRST = date(2021, 1, 4)
SHORT_BAN = (date(2023, 11, 6), date(2025, 3, 28))  # no rows: a gap longer than a window
INVESTOR_FIRST = date(2015, 1, 2)
LOAN_FIRST = date(2021, 6, 1)
CREDIT_FIRST = date(2025, 1, 2)
ETF = "133690"
# Every number the fake gives: none may appear in the report.
SENTINELS = (
    *("271828", "7314159", "2718281", "2718282", "14142135"),
    *("46692016", "98765432", "61.8033", "1.6180", "47.28"),
)


def ymd(text: str) -> date:
    return datetime.strptime(text, "%Y%m%d").date()


def weekdays(first: date, last: date, cap: int) -> list[date]:
    """The weekdays from ``last`` back to ``first``, newest first, ``cap`` at most."""
    days: list[date] = []
    day = last
    while day >= first and len(days) < cap:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    return days


def ok(**parts: Any) -> dict[str, Any]:
    return {"rt_cd": "0", "msg_cd": "MCA00000", "msg1": "정상처리 되었습니다.", **parts}


def refused(code: str, message: str) -> dict[str, Any]:
    return {"rt_cd": "1", "msg_cd": code, "msg1": message, "output": [{"stck_prpr": "271828"}]}


def short_sale(params: dict[str, str]) -> RestAnswer:
    market = params["FID_COND_MRKT_DIV_CODE"]
    if market == "UN":
        return refused("OPSQ1234", "시장 구분 오류")
    start, end = ymd(params["FID_INPUT_DATE_1"]), min(ymd(params["FID_INPUT_DATE_2"]), TODAY)
    days = [] if market == "NX" else weekdays(max(start, SHORT_FIRST), end, 400)
    days = [d for d in days if not SHORT_BAN[0] <= d <= SHORT_BAN[1]][:100]
    rows = [
        {
            "stck_bsop_date": f"{d:%Y%m%d}",
            "stck_clpr": "271828",
            "ssts_cntg_qty": "7314159",
            "ssts_vol_rlim": "1.6180",
            "acml_ssts_cntg_qty_rlim": "0.00",
            "ssts_tr_pbmn": "",
        }
        for d in days
    ]
    return ok(output1={"stck_prpr": "271828", "acml_vol": "98765432"}, output2=rows)


def investor_daily(params: dict[str, str]) -> RestAnswer:
    market = params["FID_COND_MRKT_DIV_CODE"]
    days = weekdays(INVESTOR_FIRST, min(ymd(params["FID_INPUT_DATE_1"]), TODAY), 30)
    if params.get("_tr_cont") == "N" and days:  # the next page by continuation
        days = weekdays(INVESTOR_FIRST, days[-1] - timedelta(days=1), 30)
    rows = [
        {
            "stck_bsop_date": f"{d:%Y%m%d}",
            "stck_clpr": "271828",
            "prsn_ntby_qty": "-2718282" if market == "UN" else "-2718281",
            "orgn_ntby_qty": "14142135",
            "frgn_ntby_qty": "-7314159",
            "frgn_reg_ntby_qty": "-7314158",
            "prsn_ntby_tr_pbmn": "",
        }
        for d in days
    ]
    more = "M" if days and days[-1] > INVESTOR_FIRST else "D"
    return ok(output1={"stck_prpr": "271828"}, output2=rows), {"tr_cont": more}


def investor(params: dict[str, str]) -> RestAnswer:
    if params["FID_COND_MRKT_DIV_CODE"] == "UN":
        return refused("OPSQ1234", "시장 구분 오류")
    rows = [
        {
            "stck_bsop_date": f"{d:%Y%m%d}",
            "prsn_ntby_qty": "-2718281",
            "orgn_ntby_qty": "14142135",
            "frgn_ntby_qty": "-7314159",
        }
        for d in weekdays(INVESTOR_FIRST, TODAY, 30)
    ]
    return ok(output=rows)


def daily_price(params: dict[str, str]) -> RestAnswer:
    rows = [
        {
            "stck_bsop_date": f"{d:%Y%m%d}",
            "stck_clpr": "271828",
            "hts_frgn_ehrt": "47.28",
            "frgn_ntby_qty": "-7314159",
        }
        for d in weekdays(INVESTOR_FIRST, TODAY, 30)
    ]
    return ok(output=rows)


def price(params: dict[str, str]) -> RestAnswer:
    # 46,692,016 of 98,765,432 shares held by foreigners: 47.28%.
    return ok(
        output={
            "stck_prpr": "271828",
            "frgn_hldn_qty": "46692016",
            "lstn_stcn": "98765432",
            "hts_frgn_ehrt": "47.28",
        }
    )


def loan(params: dict[str, str]) -> RestAnswer:
    if params["MKSC_SHRN_ISCD"] == ETF:
        return refused("OPSQ5678", "대상 종목이 아닙니다")
    start, end = ymd(params["START_DATE"]), min(ymd(params["END_DATE"]), TODAY)
    rows = [
        {
            "bsop_date": f"{d:%Y%m%d}",
            "rmnd_stcn": "14142135",
            "rmnd_amt": "98765432",
            "prdy_rmnd_vrss": "-2718281",
        }
        for d in weekdays(max(start, LOAN_FIRST), end, 100)
    ]
    return ok(output1=rows)


def credit_balance(deal: date) -> str:
    """A credit balance that changes from day to day."""
    return str(14142135 + deal.toordinal() % 5)


def credit(params: dict[str, str]) -> RestAnswer:
    # A deal settles two days later; the rows are those settled by the day asked.
    deals = weekdays(CREDIT_FIRST, min(ymd(params["FID_INPUT_DATE_1"]), TODAY) - timedelta(days=2), 30)
    rows = [
        {
            "deal_date": f"{d:%Y%m%d}",
            "stlm_date": f"{d + timedelta(days=2):%Y%m%d}",
            "whol_loan_rmnd_stcn": credit_balance(d),
            "whol_loan_rmnd_rate": "1.6180",
            "whol_stln_rmnd_stcn": "0",
        }
        for d in deals
    ]
    return ok(output=rows)


def answering(kis: FakeKis) -> None:
    for source, respond in (
        (SHORT_SALE, short_sale),
        (INVESTOR_DAILY, investor_daily),
        (INVESTOR, investor),
        (DAILY_PRICE, daily_price),
        (PRICE, price),
        (LOAN, loan),
        (CREDIT, credit),
    ):
        kis.rest_responses[source.tr] = respond


async def probed(
    kis: FakeKis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **changes: Any
) -> tuple[str, int, int]:
    monkeypatch.setattr(supply_probe, "PACE", 0)
    answering(kis)
    settings = replace(
        settings_for(kis, tmp_path),
        symbols=("005930", "000660"),
        etf_symbols=(ETF,),
        rest_max_per_second=1000,
        **changes,
    )
    lines: list[str] = []
    calls, failed = await probe_supply(settings, TODAY, lines.append)
    if "toss_client_id" not in changes:
        assert calls == len(kis.rest_requests)
    return "\n".join(lines), calls, failed


async def test_each_source_s_latest_day_a_year_back_and_fields(
    kis: FakeKis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report, _, failed = await probed(kis, tmp_path, monkeypatch)
    assert (
        "[1] FHPST04830000 국내주식 공매도 일별추이: GET /uapi/domestic-stock/v1/quotations/daily-short-sale"
        in report
    )
    assert (
        "    005930 J: OK, 100 rows (output2), 2026-05-29..2026-10-15, newest first"
        " · ssts_cntg_qty int, ssts_vol_rlim dec" in report
    )
    assert "    005930 UN: error HTTP 200 OPSQ1234 시장 구분 오류" in report  # a market KIS refuses
    assert "    005930 NX: OK, 0 rows (output2)" in report
    assert "    000660 J: OK, 100 rows" in report and f"    {ETF} J: OK, 100 rows" in report
    # The fields, flagged: no value.
    assert (
        "  output2 fields (005930 J): int: ssts_cntg_qty, stck_clpr · dec: ssts_vol_rlim"
        " · date: stck_bsop_date · zero: acml_ssts_cntg_qty_rlim · empty: ssts_tr_pbmn" in report
    )
    assert "  beside them, output1 fields: int: acml_vol, stck_prpr" in report
    assert (
        "  a year back (asked to 2025-10-15):\n    005930 J: OK, 100 rows (output2), 2025-05-29..2025-10-15"
        in report
    )
    # Investors: integrated, KRX and NXT; signed net buys; the continuation header.
    assert (
        "    005930 UN: OK, 30 rows (output2), 2026-09-04..2026-10-15, newest first, tr_cont M"
        " · prsn_ntby_qty int±, orgn_ntby_qty int, frgn_ntby_qty int±, frgn_reg_ntby_qty int±" in report
    )
    # Today's queries take no date.
    assert "[3] FHKST01010900 주식현재가 투자자" in report
    assert "  takes no date: a past day cannot be asked" in report
    assert (
        "    005930 UN: OK, one object (output) · frgn_hldn_qty int, hts_frgn_ehrt dec, lstn_stcn int"
        in report
    )
    # Lending: an ETF refused. Credit: settlement days paged, trading days beside them.
    assert f"    {ETF} 3: error HTTP 200 OPSQ5678 대상 종목이 아닙니다" in report
    assert (
        "    005930 J: OK, 30 rows (output), 2026-09-04..2026-10-15, newest first,"
        " deal_date 2026-09-02..2026-10-13" in report
    )
    assert failed == report.count(": error ")
    assert f"calls, {failed} failed" in report


async def test_the_history_pages_back_crosses_a_gap_and_bisects(
    kis: FakeKis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report, _, _ = await probed(kis, tmp_path, monkeypatch)
    # Short selling: 100 a call, a gap of a year and a half, then back to its first day.
    assert "  history of 005930 J (paging back from 2026-10-15, at most 25 calls):" in report
    assert "    a gap: no rows from 2023-11-04 to 2025-03-30" in report
    assert "rows in 18 calls (at most 100 a call), oldest 2021-01-04: nothing older" in report
    # Investors: 30 a call run out of pages, then the bisection finds the first day.
    assert "    750 rows in 25 calls (at most 30 a call), back to 2023-12-01: still going" in report
    assert "    oldest by bisection: 2015-01-02 (" in report
    assert (
        "    the first page said tr_cont M; the next (tr_cont N): OK, 30 rows (output2),"
        " 2026-07-24..2026-09-03, newest first, tr_cont M, older rows" in report
    )
    # Lending and credit.
    assert "oldest 2021-06-01: nothing older" in report
    assert "oldest 2025-01-04: nothing older" in report  # the first deal's settlement day


async def test_the_sources_are_cross_checked_by_counts_only(
    kis: FakeKis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report, _, _ = await probed(kis, tmp_path, monkeypatch)
    assert (
        "  FHPTJ04160001 J vs FHKST01010900 J: 30 common days (2026-09-04..2026-10-15); equal on: "
        "prsn_ntby_qty 30, orgn_ntby_qty 30, frgn_ntby_qty 30" in report
    )
    assert "  FHPTJ04160001 UN vs FHPTJ04160001 J: 30 common days" in report
    assert "prsn_ntby_qty 0, orgn_ntby_qty 30, frgn_ntby_qty 30" in report
    assert (
        "  FHKST01010100 UN: frgn_hldn_qty / lstn_stcn x 100 = hts_frgn_ehrt (within 0.01 points)" in report
    )


async def test_the_report_carries_no_market_value(
    kis: FakeKis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report, _, _ = await probed(kis, tmp_path, monkeypatch)
    assert [s for s in SENTINELS if s in report] == []
    # No long number at all, but in TR codes (symbols have six digits).
    assert re.search(r"(?<![A-Za-z0-9])\d{7,}", report) is None
    assert (
        "Toss (secondary; foreign holdings primary, #235)\n"
        "  skipped: TOSS_CLIENT_ID/TOSS_CLIENT_SECRET not set" in report
    )


async def test_the_cached_token_is_used_until_it_expires(
    kis: FakeKis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Within the collector's refresh margin, the probe still uses the cached token: it never
    issues one while the collector's is valid."""
    kis.tokens_issued.append("the-collector-s-token")
    cache = tmp_path / "state" / "kis_token.json"
    cache.parent.mkdir(parents=True)
    cache.write_text(
        json.dumps(
            {
                "key_id": hashlib.sha256(kis.app_key.encode()).hexdigest()[:16],
                "token": "the-collector-s-token",
                "expires": (datetime.now().astimezone() + timedelta(minutes=30)).isoformat(),
                "issued_at": 0.0,
            }
        )
    )
    await probed(kis, tmp_path, monkeypatch)
    assert kis.tokens_issued == ["the-collector-s-token"]
    assert {r[3]["_auth"] for r in kis.rest_requests} == {"Bearer the-collector-s-token"}


async def test_kis_refusing_everything_stops_the_probe(
    kis: FakeKis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(supply_probe, "PACE", 0)
    settings = replace(settings_for(kis, tmp_path), etf_symbols=(ETF,), rest_max_per_second=1000)
    lines: list[str] = []
    calls, failed = await probe_supply(settings, TODAY, lines.append)
    assert calls == failed == 20 == len(kis.rest_requests)
    assert "not asked: 20 calls in a row failed" in "\n".join(lines)


def test_values_are_flagged_by_their_look() -> None:
    assert flag("ssts_cntg_qty", ["12", "0", "3"]) == "int"
    assert flag("frgn_ntby_qty", ["12", "-3"]) == "int±"
    assert flag("hts_frgn_ehrt", ["51.23", "0.00"]) == "dec"
    assert flag("prdy_ctrt", ["-1.5"]) == "dec±"
    assert flag("ssts_vol_rlim", ["0", "0.00", "-0"]) == "zero"
    assert flag("ssts_tr_pbmn", ["", None, " "]) == "empty"
    assert flag("rprs_mrkt_kor_name", ["KOSPI"]) == "text"
    assert flag("stck_bsop_date", ["20261015"]) == "date"


def test_an_answer_of_another_shape_is_reported_by_its_keys() -> None:
    body = {"rt_cd": "0", "output": {"ssts_cntg_qty": "7314159"}}
    assert (
        status(SHORT_SALE, parse(SHORT_SALE, body, "")) == "OK, but not a row list (body keys: output, rt_cd)"
    )
    listed = {"rt_cd": "0", "output": [{"frgn_hldn_qty": "46692016"}]}  # an object wrapped in a list
    assert status(PRICE, parse(PRICE, listed, "")) == "OK, one object (output)"
    assert status(PRICE, parse(PRICE, {"rt_cd": "0"}, "")) == "OK, but not an object (body keys: rt_cd)"


def test_a_refusal_is_reported_by_its_code_and_message_only() -> None:
    error = KisError(
        "FHPST04830000 {'FID_INPUT_ISCD': '005930'}: HTTP 500 "
        "{'rt_cd': '1', 'msg_cd': 'EGW00201', 'msg1': '초당 거래건수를 초과하였습니다.', "
        "'output': [{'x': '7314159'}]}"
    )
    assert kis_error(error) == "error HTTP 500 EGW00201 초당 거래건수를 초과하였습니다."
    assert kis_error(KisError("something else")) == "error (no code)"


def test_the_command_exits_1_only_when_nothing_answered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # no .env
    monkeypatch.setenv("KIS_APP_KEY", "key")
    monkeypatch.setenv("KIS_APP_SECRET", "secret")
    answers = iter([(30, 4), (20, 20)])

    async def stand_in(settings: Any, today: date) -> tuple[int, int]:
        return next(answers)

    monkeypatch.setattr(cli, "probe_supply", stand_in)
    assert cli.main(["probe-supply"]) == 0
    assert cli.main(["probe-supply"]) == 1
