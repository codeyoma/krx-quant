"""``krx-quant probe-nav`` (#305): where KIS gives the single-stock ETFs' net assets, asked through
the fake KIS.

Input: fake answers for KIS's ETF/ETN current price, its daily, product and minute NAV trends,
for the 16 products, shaped as KIS's examples describe them (``examples_llm/etfetn``): the daily
NAV from the listing day to today, newest first, 100 rows a call; one product's starting later,
one missing a day, one without today's row yet; the minute NAV refused with market ``E`` and
answered with ``J``; one product's current price refused, one's net asset total 0, one's multiple
without its sign. Every value is a sentinel number. Output: the printed report, which must carry
none of them.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from krx_quant import cli, nav_probe
from krx_quant.fake_kis import FakeKis
from krx_quant.feeds.base import KST
from krx_quant.nav_probe import DAILY, MAX_FAILED_IN_A_ROW, MINUTES, PRICE, TREND, probe_nav
from krx_quant.products import PRODUCTS
from tests.kis_rest import requests_for
from tests.test_collector import settings_for

NOW = datetime(2026, 10, 21, 23, 41, tzinfo=KST)
LATEST, PREVIOUS = date(2026, 10, 21), date(2026, 10, 20)
HOLIDAYS = {date(2026, 6, 3), date(2026, 10, 9)}
LATE_START = "0198B0"  # its daily NAV starts on 2026-06-15
GAP, GAP_DAY = "0194M0", date(2026, 8, 12)  # its daily NAV misses a trading day
NOT_YET = "0193T0"  # its daily NAV has no row for the latest day yet
REFUSED = "0198D0"  # the current price refuses it
ZERO_TOTAL = "0194N0"  # the current price's net asset total is 0
UNSIGNED = "0197X0"  # the current price's multiple comes without its sign

# Sentinels: the latest and previous NAV, the units, the totals in 억원 (3,170,000 × 48,271.93 is
# 1,530.2억; × the previous NAV 1,515.7억, not 1,530), a price.
NAV, NAV_BEFORE = "48271.93", "47815.06"
UNITS, CIRCULATING = "3170000", "2950000"
TOTAL, CIRCULATING_TOTAL = "1530", "1424"
PRICE_VALUE = "48300"
SENTINELS = (NAV, NAV_BEFORE, UNITS, CIRCULATING, TOTAL, CIRCULATING_TOTAL, PRICE_VALUE)


def trading_days(first: date, last: date) -> list[date]:
    days, day = [], first
    while day <= last:
        if day.weekday() < 5 and day not in HOLIDAYS:
            days.append(day)
        day += timedelta(days=1)
    return days


def write_calendar(state: Path) -> None:
    days = {}
    day = date(2025, 10, 1)
    while day <= date(2026, 12, 31):
        days[day.isoformat()] = day.weekday() < 5 and day not in HOLIDAYS
        day += timedelta(days=1)
    state.mkdir(parents=True, exist_ok=True)
    (state / "market_calendar.json").write_text(json.dumps({"refreshed_on": None, "open_days": days}))


def ok(**outputs: Any) -> dict[str, Any]:
    return {"rt_cd": "0", "msg_cd": "MCA00000", "msg1": "정상처리 되었습니다.", **outputs}


def refused(code: str, message: str) -> dict[str, Any]:
    return {"rt_cd": "1", "msg_cd": code, "msg1": message}


def nav_on(day: date) -> str:
    if day == LATEST:
        return NAV
    if day == PREVIOUS:
        return NAV_BEFORE
    return f"4{day:%m%d}.17"


def price(params: dict[str, str]) -> dict[str, Any]:
    code = params["FID_INPUT_ISCD"]
    if code == REFUSED:
        return refused("OPSQ0011", "종목 오류")
    product = next(p for p in PRODUCTS if p.code == code)
    multiple = abs(product.leverage) if code == UNSIGNED else product.leverage
    return ok(
        output={
            "stck_prpr": PRICE_VALUE,
            "etf_ntas_ttam": "0" if code == ZERO_TOTAL else TOTAL,
            "etf_crcl_ntas_ttam": CIRCULATING_TOTAL,
            "nav": NAV,
            "prdy_last_nav": NAV_BEFORE,
            "lstn_stcn": UNITS,
            "etf_crcl_stcn": CIRCULATING,
            "etf_trc_ert_mltp": f"{multiple:.2f}",
            "stck_lstn_date": "20260527",
        }
    )


def trend(params: dict[str, str]) -> dict[str, Any]:
    return ok(
        output1={"stck_prpr": PRICE_VALUE, "stck_prdy_clpr": PRICE_VALUE},
        output2={"nav": NAV, "prdy_clpr_nav": NAV_BEFORE},
    )


def daily(params: dict[str, str]) -> dict[str, Any]:
    code = params["FID_INPUT_ISCD"]
    first = datetime.strptime(params["FID_INPUT_DATE_1"], "%Y%m%d").date()
    last = datetime.strptime(params["FID_INPUT_DATE_2"], "%Y%m%d").date()
    start = date(2026, 6, 15) if code == LATE_START else date(2026, 5, 27)
    end = PREVIOUS if code == NOT_YET else LATEST
    days = [d for d in trading_days(max(first, start), min(last, end)) if not (code == GAP and d == GAP_DAY)]
    rows = [
        {"stck_bsop_date": f"{d:%Y%m%d}", "stck_clpr": PRICE_VALUE, "nav": nav_on(d), "dprt": "0.12"}
        for d in reversed(days)
    ]
    return ok(output=rows[:100])


def minutes(params: dict[str, str]) -> dict[str, Any]:
    if params["FID_COND_MRKT_DIV_CODE"] == "E":
        return refused("OPSQ0002", "시장 구분 오류")
    rows = [
        {"bsop_hour": f"15{m:02d}00", "nav": NAV, "stck_prpr": PRICE_VALUE}
        for m in range(30, 0, -1)  # 15:30 back to 15:01, newest first
    ]
    return ok(output=rows)


async def probed(kis: FakeKis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, int, int]:
    monkeypatch.setattr(nav_probe, "PACE", 0)
    kis.rest_responses.update({PRICE.tr: price, TREND.tr: trend, DAILY.tr: daily, MINUTES.tr: minutes})
    settings = replace(settings_for(kis, tmp_path), rest_max_per_second=1000)
    write_calendar(settings.state_dir)
    lines: list[str] = []
    calls, failed = await probe_nav(settings, NOW, lines.append)
    assert calls == len(kis.rest_requests)
    return "\n".join(lines), calls, failed


def section(report: str, number: int) -> str:
    """The report's section ``number`` (``N. ...`` to the next blank line)."""
    return next(p for p in report.split("\n\n") if p.startswith(f"{number}. "))


async def test_the_current_price_s_fields_and_the_product_list_s_check(
    kis: FakeKis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report, _, _ = await probed(kis, tmp_path, monkeypatch)
    assert (
        "상품: 005930 +2x 현물+선물형 0193W0, 0195R0, 0194M0, 0192M0, 0193K0"
        " · 005930 +2x 선물형 0194N0, 0198B0 · 005930 -2x 선물형 0193L0"
        " · 000660 +2x 현물+선물형 0193T0, 0195S0, 0194T0, 0192L0, 0197W0"
        " · 000660 +2x 선물형 0194R0, 0198D0 · 000660 -2x 선물형 0197X0" in report
    )
    a = section(report, 1)
    assert a.startswith("1. (a) FHPST02400000 ETF/ETN 현재가 (J)")
    assert (
        "  0193W0, 0195R0, 0194M0, 0192M0, 0193K0, 0198B0, 0193L0, 0193T0, 0195S0, 0194T0, 0192L0, 0197W0,"
        " 0194R0, 0197X0 (14종): OK · etf_ntas_ttam int, etf_crcl_ntas_ttam int, nav dec, prdy_last_nav dec,"
        " lstn_stcn int, etf_crcl_stcn int" in a
    )
    assert "  0198D0: error HTTP 200 OPSQ0011 종목 오류" in a
    assert "  0194N0: OK · etf_ntas_ttam zero," in a
    assert (
        "  값 있음(16종 중): etf_ntas_ttam 14 · etf_crcl_ntas_ttam 15 · nav 15 · prdy_last_nav 15"
        " · lstn_stcn 15 · etf_crcl_stcn 15" in a
    )
    assert (
        "  상품 목록 확인: etf_trc_ert_mltp = 배율 L 같음 14 · 다름 1 · 견줄 것 없음 1"
        " · stck_lstn_date = 상장일 같음 15 · 다름 0 · 견줄 것 없음 1" in a
    )
    assert "  다른 상품: 0197X0(배율)" in a
    assert "  필드(0193W0): int: " in a and "date: stck_lstn_date" in a


async def test_how_far_back_the_daily_nav_goes(
    kis: FakeKis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report, _, _ = await probed(kis, tmp_path, monkeypatch)
    b = section(report, 2)
    listed = trading_days(date(2026, 5, 27), LATEST)
    assert len(listed) > 100, "two pages"
    assert (
        f"(13종): {len(listed)}행(3번 조회) 2026-05-27..2026-10-21, newest first"
        f" · nav dec · nav 값 있는 날 {len(listed)}(첫날 2026-05-27) · 상장일 2026-05-27까지 감"
        " · 상장일부터 빠진 거래일 0 · 상장 전 행 0" in b
    )
    late = trading_days(date(2026, 6, 15), LATEST)
    assert f"  0198B0: {len(late)}행(2번 조회) 2026-06-15..2026-10-21" in b
    assert "· 상장일까지 못 감 · 상장일부터 빠진 거래일 12(2026-05-27, 2026-05-28," in b
    assert f"  0194M0: {len(listed) - 1}행" in b and "상장일부터 빠진 거래일 1(2026-08-12)" in b
    assert f"  0193T0: {len(listed) - 1}행(3번 조회) 2026-05-27..2026-10-20" in b
    assert "  모두: 상장일까지 감 15 · 상장일까지 못 감 1" in b
    requests = requests_for(kis, DAILY.tr)
    assert {r["FID_INPUT_DATE_1"] for r in requests} == {"20260501"}, "asked from before the listing"


async def test_when_the_latest_value_is_in_as_far_as_the_night_shows(
    kis: FakeKis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report, _, _ = await probed(kis, tmp_path, monkeypatch)
    assert "3. (b) FHPST02440000 NAV 비교추이(종목) (J)" in report
    assert "  16종 모두: OK · output1 2개 필드 · output2 2개 필드 · nav dec, prdy_clpr_nav dec" in report
    assert "4. (b) FHPST02440100 NAV 비교추이(분) (시장 E, 안 되면 J;" in report
    assert (
        "  16종 모두: J OK, 30행 · bsop_hour 15:01..15:30 · nav dec"
        " (먼저 E error HTTP 200 OPSQ0002 시장 구분 오류)" in report
    )
    timing = section(report, 5)
    assert timing.startswith(
        "5. 그날 값이 언제 나오나(2026-10-21 23:41 KST에 본 것, 마지막 거래일 2026-10-21, 그 전 2026-10-20)"
    )
    assert "  (b) 일별의 가장 최근 행: 마지막 거래일 15 · 그 전 거래일 1" in timing
    # NOT_YET has no row of the latest day: its nav equals none; REFUSED gave no current price.
    assert "  (a) nav = (b) 일별 nav의 날: 마지막 거래일 14 · 어느 날과도 다름 1 · 견줄 것 없음 1" in timing
    assert "  (a) prdy_last_nav = (b) 일별 nav의 날: 그 전 거래일 15 · 견줄 것 없음 1" in timing
    assert "  (b) 종목 prdy_clpr_nav = (b) 일별 nav의 날: 그 전 거래일 16" in timing
    assert "  (b) 분별 마지막 시각: 15:30 16" in timing
    assert "두 번째 관찰이 필요하다" in timing


async def test_the_net_asset_total_against_the_units_times_the_nav(
    kis: FakeKis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report, _, _ = await probed(kis, tmp_path, monkeypatch)
    c = section(report, 6)
    assert "  etf_ntas_ttam = lstn_stcn × nav: 원 0 · 천원 0 · 백만원 0 · 억원 14 · 견줄 것 없음 2" in c
    assert (
        "  etf_ntas_ttam = lstn_stcn × prdy_last_nav: 원 0 · 천원 0 · 백만원 0 · 억원 0 · 다름 14"
        " · 견줄 것 없음 2" in c
    )
    assert (
        "  etf_crcl_ntas_ttam = etf_crcl_stcn × nav: 원 0 · 천원 0 · 백만원 0 · 억원 15 · 견줄 것 없음 1" in c
    )
    assert "상장 주수는 (a)의 오늘 값뿐이다" in c


async def test_the_report_carries_no_market_value(
    kis: FakeKis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report, calls, failed = await probed(kis, tmp_path, monkeypatch)
    assert [s for s in SENTINELS if s in report] == []
    bare = re.sub(r"\d{4}-\d{2}-\d{2}|\d{2}:\d{2}|FHPST\d{8}|[A-Z]{4}\d{4}|0\d{3}[A-Z]0|\d{6}", "", report)
    assert not re.search(r"\d{4,}", bare), "no value in the output: counts, dates, times, codes only"
    # The 16 minute calls with E and the one refused current price failed; the rest answered.
    assert failed == 17 and f"KIS: {calls}번 조회, 17번 실패" in report


async def test_answers_without_what_was_asked_for_are_failures_and_stop_the_probe(
    kis: FakeKis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """KIS's OK without the object or rows where they belong is no usable answer (#297)."""
    monkeypatch.setattr(nav_probe, "PACE", 0)
    for tr in (PRICE.tr, TREND.tr, DAILY.tr, MINUTES.tr):
        kis.rest_responses[tr] = lambda params: ok(output="x")
    settings = replace(settings_for(kis, tmp_path), rest_max_per_second=1000)
    lines: list[str] = []

    calls, failed = await probe_nav(settings, NOW, lines.append)

    report = "\n".join(lines)
    assert calls == failed == MAX_FAILED_IN_A_ROW == len(kis.rest_requests)
    # Two products' five calls each: the rest is not asked.
    assert "  0193W0, 0195R0 (2종): OK, 읽을 것이 없다(본문 키: msg1, msg_cd, output, rt_cd)" in report
    assert f"묻지 않음: {MAX_FAILED_IN_A_ROW}번 잇달아 실패" in report
    assert "빠진 거래일" not in report and "  모두: 답 없음 16" in report


def test_the_command_exits_1_only_when_nothing_answered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # no .env
    monkeypatch.setenv("KIS_APP_KEY", "key")
    monkeypatch.setenv("KIS_APP_SECRET", "secret")
    answers = iter([(100, 17), (10, 10)])

    async def stand_in(settings: Any, now: datetime) -> tuple[int, int]:
        assert now.tzinfo is not None
        return next(answers)

    monkeypatch.setattr(cli, "probe_nav", stand_in)
    assert cli.main(["probe-nav"]) == 0
    assert cli.main(["probe-nav"]) == 1
