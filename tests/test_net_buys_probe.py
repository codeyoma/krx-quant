"""``krx-quant probe-net-buys`` (#264, #297): whether KIS's KRX investor query has the net buys
before NXT opened, and what the engine stored of them, asked through the fake KIS.

Input: a fake ``FHPTJ04160001`` answering the 30 weekdays to the day asked, newest first: ``J``
with figures on every day, ``UN`` with "0" before NXT opened (2025-03-04) and other figures
after, one market refused for one symbol and window; and stored KIS and Toss files. Every value
is a sentinel number (seven digits). Output: the printed report, which must carry none of them.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from krx_quant import cli, supply_probe
from krx_quant.fake_kis import FakeKis
from krx_quant.net_buys_probe import MAX_FAILED_IN_A_ROW, WINDOWS, probe_net_buys
from krx_quant.supply import INVESTORS, KIS, NET_MARKET, NXT_OPENED, TOSS, write_supply
from tests.test_collector import settings_for

TODAY = date(2026, 10, 20)
SYMBOLS = ("005930", "000660")
SENTINELS = ("7314159", "2718281", "1618033", "7314160", "2718282", "1618034", "4669201")
SAME_DAY = date(2026, 10, 14)  # a recent day J's foreign net buy equals UN's


def weekdays(last: date, cap: int = 30) -> list[date]:
    """The ``cap`` weekdays up to ``last``, newest first."""
    days: list[date] = []
    day = last
    while len(days) < cap:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    return days


def nets(market: str, day: date) -> tuple[str, str, str]:
    if market == "J":
        return "-7314159", "2718281", "-1618033"
    if day < NXT_OPENED:
        return "0", "0", "0"
    return "-7314160", "2718282", "-1618033" if day == SAME_DAY else "-1618034"


def investors(params: dict[str, str]) -> dict[str, Any]:
    market, symbol = params["FID_COND_MRKT_DIV_CODE"], params["FID_INPUT_ISCD"]
    last = datetime.strptime(params["FID_INPUT_DATE_1"], "%Y%m%d").date()
    if symbol == "000660" and market == "J" and last.year == 2016:
        return {"rt_cd": "1", "msg_cd": "OPSQ1234", "msg1": "조회 기간 오류", "output2": [{"x": "4669201"}]}
    rows = []
    for d in weekdays(last):
        individual, institution, foreign = nets(market, d)
        rows.append(
            {
                "stck_bsop_date": d.strftime("%Y%m%d"),
                "prsn_ntby_qty": individual,
                "orgn_ntby_qty": institution,
                "frgn_ntby_qty": foreign,
                "stck_clpr": "4669201",
            }
        )
    return {
        "rt_cd": "0",
        "msg_cd": "MCA00000",
        "msg1": "정상처리 되었습니다.",
        "output1": {},
        "output2": rows,
    }


def stored(state: Path) -> None:
    """005930's KIS file: 40 days before NXT from J (two without a figure), 10 from it from UN;
    Toss's individual and institution net buys on the days before, equal to KRX's on some."""
    before = sorted(weekdays(NXT_OPENED - timedelta(days=1), 40))
    after = sorted(weekdays(NXT_OPENED + timedelta(days=13), 10))
    kis = [
        {
            "date": d,
            "individual_net": None if i < 2 else -7_314_159,
            "institution_net": None if i < 2 else 2_718_281,
            "foreign_net": None if i < 2 else -1_618_033,
            NET_MARKET: "J",
        }
        for i, d in enumerate(before)
    ] + [
        {
            "date": d,
            "individual_net": -7_314_160,
            "institution_net": 0,
            "foreign_net": -1_618_034,
            NET_MARKET: "UN",
        }
        for d in after
    ]
    write_supply(state, "005930", KIS, kis)
    toss = [
        {"date": d, "individual_net": -7_314_159 if i % 4 else 1, "institution_net": 2_718_281}
        for i, d in enumerate(before)
    ]
    write_supply(state, "005930", TOSS, toss)


async def probed(kis: FakeKis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, int, int]:
    monkeypatch.setattr(supply_probe, "PACE", 0)
    kis.rest_responses[INVESTORS.tr] = investors
    settings = replace(settings_for(kis, tmp_path), symbols=SYMBOLS, rest_max_per_second=1000)
    stored(settings.state_dir)
    lines: list[str] = []
    calls, failed = await probe_net_buys(settings, TODAY, lines.append)
    assert calls == len(kis.rest_requests) == len(SYMBOLS) * len(WINDOWS) * 2
    return "\n".join(lines), calls, failed


async def test_each_window_s_answers_their_figures_and_j_against_un(
    kis: FakeKis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report, _, failed = await probed(kis, tmp_path, monkeypatch)
    old = sorted(weekdays(date(2016, 12, 30)))
    looks = "prsn_ntby_qty int±, orgn_ntby_qty int, frgn_ntby_qty int±"
    assert (
        f"  005930 2016(~2016-12-30) J: OK, 30행 {old[0]}..{old[-1]} · {looks} · 값 개인 30·기관 30·외국인 30"
        f" · 값 있는 날 {old[0]} – {old[-1]}" in report
    )
    zero = "prsn_ntby_qty zero, orgn_ntby_qty zero, frgn_ntby_qty zero"
    assert (
        f"  005930 2016(~2016-12-30) UN: OK, 30행 {old[0]}..{old[-1]} · {zero} · 값 개인 0·기관 0·외국인 0"
        " · 값 있는 날 없음" in report
    )
    # NXT's first month: UN's figures begin on its opening; J and UN on the days from it.
    march = sorted(weekdays(date(2025, 3, 31)))
    opened = [d for d in march if d >= NXT_OPENED]
    assert f"  005930 2025-03(~2025-03-31) UN: OK, 30행 {march[0]}..{march[-1]} · {looks}" in report
    assert (
        f"값 개인 {len(opened)}·기관 {len(opened)}·외국인 {len(opened)}"
        f" · 값 있는 날 {NXT_OPENED} – 2025-03-31" in report
    )
    assert (
        f"  005930 2025-03(~2025-03-31) J = UN(2025-03-04부터 같은 날 {len(opened)}일,"
        " 2025-03-04 – 2025-03-31): "
        f"개인 같음 0·다름 {len(opened)} · 기관 같음 0·다름 {len(opened)} · 외국인 같음 0·다름 {len(opened)}"
    ) in report
    assert "005930 2025-02(~2025-02-28) J = UN" not in report, "no day from NXT's opening"
    assert "  005930 최근(~2026-10-20) J = UN(2025-03-04부터 같은 날 30일" in report
    assert "외국인 같음 1·다름 29" in report  # SAME_DAY
    # A refusal by its code and message only.
    assert "  000660 2016(~2016-12-30) J: error HTTP 200 OPSQ1234 조회 기간 오류" in report
    assert failed == 1 and "KIS: 24번 조회, 1번 실패" in report


async def test_the_stored_days_by_era_market_and_figure_and_krx_against_toss(
    kis: FakeKis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report, _, _ = await probed(kis, tmp_path, monkeypatch)
    before = sorted(weekdays(NXT_OPENED - timedelta(days=1), 40))
    after = sorted(weekdays(NXT_OPENED + timedelta(days=13), 10))
    assert (
        f"  005930 NXT 개장 전 40일({before[0]} – {before[-1]}): 시장 J 40·UN 0"
        " · 값 개인 38·기관 38·외국인 38"
        f" · 값 있는 날 {before[2]} – {before[-1]}" in report
    )
    assert (
        f"  005930 NXT 개장부터 10일({after[0]} – {after[-1]}): 시장 J 0·UN 10 · 값 개인 10·기관 10·외국인 10"
        in report
    ), "a real 0 is a figure"
    # Toss's individual equals KRX's on 3 of every 4 days with both (from the third day).
    both = len(before) - 2
    equal = sum(1 for i in range(2, len(before)) if i % 4)
    assert (
        f"  005930 NXT 개장 전 KIS J와 토스: 개인 같은 날 {both}일 중 같음 {equal}·다름 {both - equal}"
        f" · 기관 같은 날 {both}일 중 같음 {both}·다름 0" in report
    )
    assert "  000660: 저장된 KIS 수급 없음" in report


async def test_the_report_carries_no_market_value(
    kis: FakeKis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report, _, _ = await probed(kis, tmp_path, monkeypatch)
    assert [s for s in SENTINELS if s in report] == []
    assert re.search(r"(?<![A-Za-z0-9])\d{7,}", report) is None, "no long number but in TR codes"


async def test_answers_without_the_row_list_are_failures_and_stop_the_probe(
    kis: FakeKis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#297 review: KIS's OK without the rows where they belong is no usable answer: a failed
    call, counted toward the cutoff, so that nothing usable exits 1."""
    monkeypatch.setattr(supply_probe, "PACE", 0)
    kis.rest_responses[INVESTORS.tr] = lambda params: {
        "rt_cd": "0",
        "msg_cd": "MCA00000",
        "output2": {"x": "1"},
    }
    settings = replace(settings_for(kis, tmp_path), symbols=SYMBOLS, rest_max_per_second=1000)
    lines: list[str] = []

    calls, failed = await probe_net_buys(settings, TODAY, lines.append)

    report = "\n".join(lines)
    assert calls == failed == MAX_FAILED_IN_A_ROW == len(kis.rest_requests)
    assert "  005930 2016(~2016-12-30) J: OK, 행 목록이 아니다(본문 키: msg_cd, output2, rt_cd)" in report
    assert f"묻지 않음: {MAX_FAILED_IN_A_ROW}번 잇달아 실패" in report
    assert f"KIS: {calls}번 조회, {failed}번 실패" in report


def test_the_command_exits_1_only_when_nothing_answered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # no .env
    monkeypatch.setenv("KIS_APP_KEY", "key")
    monkeypatch.setenv("KIS_APP_SECRET", "secret")
    answers = iter([(24, 3), (24, 24)])

    async def stand_in(settings: Any, today: date) -> tuple[int, int]:
        return next(answers)

    monkeypatch.setattr(cli, "probe_net_buys", stand_in)
    assert cli.main(["probe-net-buys"]) == 0
    assert cli.main(["probe-net-buys"]) == 1
