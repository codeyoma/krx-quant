"""``krx-quant probe-krx-etf`` (#305): what the KRX Open API's ETF daily trading service gives for
the single-stock ETFs, asked through a fake KRX (``tests/fake_krx_openapi.py``) and the fake KIS;
and the scheduled job that waits for the key.

Input: the fake service answers each day with two other ETFs and 15 of the 16 products (0198D0
never; 0194M0 not on its listing day), as the service page describes it: ``OutBlock_1``, every
value a string, ``-`` for none, the volume with thousands commas. 0193W0's listed units step up on
2026-10-20. The latest trading day (2026-10-22, asked that night) is not in yet: an empty answer.
KIS answers as its examples describe (``tests/test_nav_probe.py``): its daily NAV the same as
KRX's but for 0195R0 on the listing day, its net asset total in 억원, 0197X0's off. Every value is
a sentinel. Output: the printed report, which must carry none of them, nor the key.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from krx_quant import cli, krx_etf_probe
from krx_quant.clock import FakeClock
from krx_quant.config import Settings
from krx_quant.fake_kis import FakeKis
from krx_quant.feeds.base import KST
from krx_quant.github import GitHub
from krx_quant.krx_etf_probe import DOCUMENTED, MAX_FAILED_IN_A_ROW, probe_krx_etf
from krx_quant.nav_probe import DAILY, PRICE
from krx_quant.products import PRODUCTS
from krx_quant.redaction import secrets_of
from krx_quant.scheduled import (
    FAILED,
    GAPS_PROBE,
    INTERRUPTED,
    JOBS,
    KRX_ETF_PROBE,
    NAV_PROBE,
    OK,
    STATE_FILE,
    Facts,
    JobRecord,
    JobState,
    Night,
    Scheduler,
)
from tests.fake_krx_openapi import KEY, PATH, FakeKrx, fake_krx
from tests.test_collector import settings_for
from tests.test_nav_probe import ok, trading_days, write_calendar
from tests.test_scheduled import AT, CALENDAR, COMMIT, FRIDAY, MONDAY, SATURDAY, SUNDAY, Runs, Told, at

NOW = datetime(2026, 10, 22, 23, 40, tzinfo=KST)
LISTED, LATEST, PREVIOUS = date(2026, 5, 27), date(2026, 10, 22), date(2026, 10, 21)
BEFORE = date(2026, 5, 26)  # the trading day before the listing
NEVER = "0198D0"  # KRX has no row of it
LATE = "0194M0"  # KRX has no row of it on the listing day
STEPPED, STEP_DAY = "0193W0", date(2026, 10, 20)  # its listed units step up that day
KIS_NAV_OFF = "0195R0"  # KIS's daily NAV differs from KRX's on the listing day
KIS_TOTAL_OFF = "0197X0"  # KIS's net asset total is off

# Sentinels: KRX's NAV, units before and after the step, close; KIS's NAV of today, its NAV off.
NAV, NAV_TODAY, NAV_OFF = "48271.93", "48410.55", "47013.29"
SHARES, SHARES_AFTER = 3170000, 3370000
CLOSE = 48300
VOLUME = "1,234,567"
SENTINELS = (NAV, NAV_TODAY, NAV_OFF, str(SHARES), str(SHARES_AFTER), str(CLOSE), "1234567", VOLUME)


def total(shares: int) -> int:
    return round(shares * float(NAV))


def shares_on(code: str, day: date) -> int:
    return SHARES_AFTER if code == STEPPED and day >= STEP_DAY else SHARES


def krx_row(code: str, day: date, name: str) -> dict[str, Any]:
    """A row as the service page lists its fields: strings, ``-`` for none."""
    shares = shares_on(code, day)
    return {
        "BAS_DD": f"{day:%Y%m%d}",
        "ISU_CD": code,
        "ISU_NM": name,
        "TDD_CLSPRC": str(CLOSE),
        "CMPPREVDD_PRC": "-150",
        "FLUC_RT": "-0.31",
        "NAV": NAV,
        "TDD_OPNPRC": "48500",
        "TDD_HGPRC": "48600",
        "TDD_LWPRC": "48100",
        "ACC_TRDVOL": VOLUME,
        "ACC_TRDVAL": "59630000000",
        "MKTCAP": str(CLOSE * shares),
        "INVSTASST_NETASST_TOTAMT": str(total(shares)),
        "LIST_SHRS": str(shares),
        "IDX_IND_NM": "테스트 지수",
        "OBJ_STKPRC_IDX": "-",
        "CMPPREVDD_IDX": "-",
        "FLUC_RT_IDX": "-",
    }


def krx_day(day: date) -> list[dict[str, Any]] | int:
    if day >= LATEST:
        return []  # not in before 08:00 the next business day
    rows = [krx_row("069500", day, "다른 ETF 하나"), krx_row("133690", day, "다른 ETF 둘")]
    if day < LISTED:
        return rows
    return rows + [
        krx_row(p.code, day, f"테스트 상품 {p.code}")
        for p in PRODUCTS
        if p.code != NEVER and not (p.code == LATE and day == LISTED)
    ]


def kis_price(params: dict[str, str]) -> dict[str, Any]:
    code = params["FID_INPUT_ISCD"]
    shares = shares_on(code, LATEST)
    in_eok = "1600" if code == KIS_TOTAL_OFF else str(round(total(shares) / 1e8))
    return ok(
        output={
            "etf_ntas_ttam": in_eok,
            "lstn_stcn": str(shares),
            "nav": NAV_TODAY,
            "prdy_last_nav": NAV,
            "stck_prpr": str(CLOSE),
        }
    )


def kis_daily(params: dict[str, str]) -> dict[str, Any]:
    code = params["FID_INPUT_ISCD"]
    first = datetime.strptime(params["FID_INPUT_DATE_1"], "%Y%m%d").date()
    last = datetime.strptime(params["FID_INPUT_DATE_2"], "%Y%m%d").date()
    days = trading_days(max(first, LISTED), min(last, LATEST))

    def nav(day: date) -> str:
        if day == LATEST:
            return NAV_TODAY
        return NAV_OFF if code == KIS_NAV_OFF and day == LISTED else NAV

    rows = [{"stck_bsop_date": f"{d:%Y%m%d}", "nav": nav(d), "stck_clpr": str(CLOSE)} for d in reversed(days)]
    return ok(output=rows[:100])


def asked() -> list[date]:
    """The days the probe should ask: the day before the listing, the listing day, each week's first
    trading day after it, the last five trading days."""
    days = trading_days(LISTED, LATEST)
    weeks: dict[tuple[int, int], date] = {}
    for d in days:
        weeks.setdefault((d.isocalendar().year, d.isocalendar().week), d)
    return sorted({BEFORE, LISTED, *weeks.values(), *days[-5:]})


def settings_with(kis: FakeKis, krx: FakeKrx, tmp_path: Path, key: str | None = KEY) -> Settings:
    settings = replace(
        settings_for(kis, tmp_path), rest_max_per_second=1000, krx_openapi_key=key, krx_openapi_url=krx.url
    )
    write_calendar(settings.state_dir)
    return settings


async def probed(
    kis: FakeKis,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rows: Callable[[date], list[dict[str, Any]] | int] = krx_day,
    key: str | None = KEY,
) -> tuple[str, int, int, FakeKrx]:
    monkeypatch.setattr(krx_etf_probe, "PACE", 0)
    monkeypatch.setattr(krx_etf_probe, "KIS_PACE", 0)
    kis.rest_responses.update({PRICE.tr: kis_price, DAILY.tr: kis_daily})
    async with fake_krx(rows) as krx:
        lines: list[str] = []
        calls, with_rows = await probe_krx_etf(settings_with(kis, krx, tmp_path, key), NOW, lines.append)
    assert calls == len(krx.requests)
    return "\n".join(lines), calls, with_rows, krx


def section(report: str, number: int) -> str:
    """The report's section ``number`` (``N. ...`` to the next blank line)."""
    return next(p for p in report.split("\n\n") if p.startswith(f"{number}. "))


def product_rows() -> int:
    """KRX's product rows over the days asked: 15 products a day from the listing, 14 on it."""
    return sum(14 if d == LISTED else 15 for d in asked() if LISTED <= d < LATEST)


async def test_each_day_s_answer_its_fields_and_the_codes(
    kis: FakeKis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report, calls, with_rows, krx = await probed(kis, tmp_path, monkeypatch)
    days = asked()
    assert calls == len(days) and with_rows == len(days) - 1
    assert [datetime.strptime(q["basDd"], "%Y%m%d").date() for _, q, _ in krx.requests] == days
    assert f"물은 날 {len(days)}일: 상장 전 거래일 2026-05-26 · 상장일 2026-05-27" in report
    assert "마지막 5거래일 2026-10-16..2026-10-22" in report
    rows = product_rows()
    one = section(report, 1)
    assert f"  행 있음 {len(days) - 1}일 · 빈 답(행 0) 1일(2026-10-22) · 실패 0일" in one
    assert f"  본문 키: OutBlock_1 {len(days)}" in one
    assert "  행 수(그날 답한 모든 ETF): 가장 적은 날 2 · 가장 많은 날 17" in one
    assert f"  상품 행의 BAS_DD = 물은 날: 같음 {rows}" in one
    two = section(report, 2)
    assert two.startswith(f"2. 필드 (상품 행 {rows}개)")
    assert "  서비스 페이지의 19개와: 답에 있음 19 · 답에 없음 0 · 페이지에 없는 필드 0" in two
    assert "int: ACC_TRDVAL, ACC_TRDVOL, BAS_DD, INVSTASST_NETASST_TOTAMT, LIST_SHRS, MKTCAP," in two
    assert "dec: NAV · dec±: FLUC_RT" in two and "empty: CMPPREVDD_IDX, FLUC_RT_IDX, OBJ_STKPRC_IDX" in two
    assert f"  천 단위 쉼표가 있는 필드(행 수): ACC_TRDVOL {rows}" in two
    assert f'  "-"인 필드(행 수): OBJ_STKPRC_IDX {rows} · CMPPREVDD_IDX {rows} · FLUC_RT_IDX {rows}' in two
    wanted = (
        f"NAV {rows} · INVSTASST_NETASST_TOTAMT {rows} · LIST_SHRS {rows} · MKTCAP {rows} · TDD_CLSPRC {rows}"
    )
    assert f"  값 있음({rows}행 중): {wanted}" in two
    three = section(report, 3)
    assert "  찾은 곳(상품 수): ISU_CD 6자리 그대로 15" in three
    assert "  찾지 못한 상품: 0198D0 (1종)" in three
    assert "  같은 날 같은 코드의 두 번째 행: 0" in three


async def test_the_rows_from_the_listing_day_and_when_a_day_is_in(
    kis: FakeKis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report, _, _, _ = await probed(kis, tmp_path, monkeypatch)
    after = len([d for d in asked() if LISTED <= d < LATEST])
    four = section(report, 4)
    assert four.startswith(f"4. 상장일부터 행이 있나 (상장일과 그 뒤 물은 거래일 중 행이 온 {after}일)")
    assert f"(14종): {after}/{after}일" in four
    assert f"  0194M0: {after - 1}/{after}일 · 없는 날 2026-05-27 · 상장일 행 없음" in four
    assert f"  0198D0: 0/{after}일 · 없는 날 2026-05-27, " in four and four.endswith(
        "  상장 전 거래일(2026-05-26): 상품 행 0종"
    )
    five = section(report, 5)
    assert five.startswith(
        "5. 그날 값이 언제 나오나(2026-10-22 23:40 KST에 물은 것, 마지막 거래일 2026-10-22, 그 전 2026-10-21)"
    )
    assert "  마지막 거래일 2026-10-22: 빈 답(행 0)" in five
    assert "  그 전 거래일 2026-10-21: 행 있음 · 상품 15종" in five


async def test_the_listed_units_day_to_day_and_the_net_asset_total(
    kis: FakeKis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report, _, _, _ = await probed(kis, tmp_path, monkeypatch)
    rows = product_rows()
    six = section(report, 6)
    # Of the days with rows, only the last four (2026-10-16, 19, 20, 21) are next trading days: three
    # pairs for each of the 15 products; 0193W0's units step between 2026-10-19 and 10-20.
    assert "  이웃한 거래일: 짝 45 · 바뀜 1 · 같음 44 · 견줄 것 없음 0" in six
    assert "  한 번이라도 바뀐 상품: 1종(0193W0)" in six
    assert (
        f"  MKTCAP ÷ TDD_CLSPRC = LIST_SHRS (상품 행): 정확히 같음 {rows} · 0.5% 안 0 · 다름 0"
        " · 견줄 것 없음 0" in six
    )
    seven = section(report, 7)
    assert f"  원 {rows} · 천원 0 · 백만원 0 · 억원 0 · 다름 0 · 견줄 것 없음 0" in seven


async def test_the_same_days_against_kis(
    kis: FakeKis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report, _, _, _ = await probed(kis, tmp_path, monkeypatch)
    rows = product_rows()
    eight = section(report, 8)
    assert (
        f"  FHPST02440200 nav = KRX NAV(물은 날마다, 소수 둘째 자리의 반 안): 같음 {rows - 1} · 다름 1"
        in eight
    )
    assert "  다른 곳: 0195R0 2026-05-27" in eight
    assert "  FHPST02400000(물은 때의 값)과 KRX 그 전 거래일 2026-10-21:" in eight
    assert (
        "    etf_ntas_ttam = INVSTASST_NETASST_TOTAMT: 원 0 · 천원 0 · 백만원 0 · 억원 14 · 다름 1"
        " · 견줄 것 없음 1" in eight
    )
    assert "    lstn_stcn = LIST_SHRS: 같음 15 · 다름 0 · 견줄 것 없음 1" in eight
    assert "    prdy_last_nav = NAV: 같음 15 · 다름 0 · 견줄 것 없음 1" in eight
    assert "    nav = NAV: 같음 0 · 다름 15 · 견줄 것 없음 1" in eight
    assert "  FHPST02400000(물은 때의 값)과 KRX 마지막 거래일 2026-10-22: KRX 상품 행 없음" in eight
    kis_calls = len(kis.rest_requests)
    assert report.endswith(
        f"KRX: {len(asked())}번 조회, 0번 실패, 행이 있는 날 {len(asked()) - 1}"
        f" · KIS: {kis_calls}번 조회, 0번 실패"
    )


async def test_the_report_carries_no_market_value_and_the_key_goes_in_the_header_only(
    kis: FakeKis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report, _, _, krx = await probed(kis, tmp_path, monkeypatch)
    assert [s for s in SENTINELS if s in report] == [] and KEY not in report
    bare = re.sub(r"\d{4}-\d{2}-\d{2}|\d{2}:\d{2}|FHPST\d{8}|0\d{3}[A-Z]0|\d{6}", "", report)
    assert not re.search(r"\d{4,}", bare), "no value in the output: counts, dates, times, codes only"
    assert {path for path, _, _ in krx.requests} == {PATH}
    assert all(set(query) == {"basDd"} and header == KEY for _, query, header in krx.requests), (
        "the key in the header, never in the address"
    )


async def test_a_refused_key_is_said_plainly_and_asked_three_times(
    kis: FakeKis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report, calls, with_rows, _ = await probed(kis, tmp_path, monkeypatch, key="krx-openapi-key-not-approved")
    assert (calls, with_rows) == (MAX_FAILED_IN_A_ROW, 0)
    days = asked()
    assert (
        "  실패 3일(2026-05-26, 2026-05-27, 2026-06-01): HTTP 401 (KRX FAQ: 인증키만 받고 이 서비스를"
        " 이용 신청·승인받지 않았거나, 갱신 전 인증키, 오타·공백), 본문: respMsg Unauthorized API Call,"
        " respCode 401" in report
    )
    assert f"  실패 {len(days) - 3}일(2026-06-08, " in report and "): 묻지 않음: 3번 잇달아 실패" in report
    assert "  행 있음 0일 · 빈 답(행 0) 0일 · 실패" in report
    assert "8. KIS와 같은 날: 건너뜀(KRX에 상품 행이 없다)" in report
    assert "krx-openapi-key-not-approved" not in report
    assert kis.rest_requests == [], "KIS is not asked when KRX gave nothing"
    assert report.endswith("KRX: 3번 조회, 3번 실패, 행이 있는 날 0")


async def test_empty_answers_and_other_refusals_are_said_as_they_are(
    kis: FakeKis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report, calls, with_rows, _ = await probed(kis, tmp_path, monkeypatch, rows=lambda day: [])
    days = asked()
    assert (calls, with_rows) == (len(days), 0), "an empty answer is an answer: every day is asked"
    assert (
        f"  행 있음 0일 · 빈 답(행 0) {len(days)}일"
        "(2026-05-26, 2026-05-27, 2026-06-01, 2026-06-08, 2026-06-15 외)" in report
    )
    assert "2. 필드 (상품 행이 없어 모든 행 0개)\n  행이 없다" in report
    assert (
        "  마지막 거래일 2026-10-22: 빈 답(행 0)" in report
        and "  그 전 거래일 2026-10-21: 빈 답(행 0)" in report
    )

    def busy(day: date) -> list[dict[str, Any]] | int:
        return 429 if day == LISTED else krx_day(day)

    kis.rest_requests.clear()
    report, _, with_rows, _ = await probed(kis, tmp_path / "busy", monkeypatch, rows=busy)
    assert with_rows == len(days) - 2
    assert (
        "  실패 1일(2026-05-27): HTTP 429 (KRX FAQ: 인증키의 하루 한도 10,000번을 넘었다, 자정에 초기화)"
        in report
    )
    assert "· 상장일 답 없음" in report, "the listing day did not answer: no product is missing from it"


async def test_without_the_key_nothing_is_asked(
    kis: FakeKis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report, calls, with_rows, krx = await probed(kis, tmp_path, monkeypatch, key=None)
    assert (calls, with_rows) == (0, 0) and krx.requests == [] and kis.rest_requests == []
    assert report.endswith(
        '멈춤: KRX_OPENAPI_KEY가 없다(서버 .env에 넣는다, docs/operations.md "KRX Open API (#305)")'
    )


def test_the_service_page_s_fields_are_listed() -> None:
    assert len(DOCUMENTED) == len(set(DOCUMENTED)) == 19
    assert {
        "ISU_CD",
        "NAV",
        "MKTCAP",
        "TDD_CLSPRC",
        "INVSTASST_NETASST_TOTAMT",
        "LIST_SHRS",
        "BAS_DD",
    } <= set(DOCUMENTED)


def test_the_command_needs_no_kis_key_and_exits_1_when_no_day_had_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # no .env
    monkeypatch.delenv("KIS_APP_KEY", raising=False)
    monkeypatch.delenv("KIS_APP_SECRET", raising=False)
    monkeypatch.setenv("KRX_OPENAPI_KEY", KEY)
    answers = iter([(30, 29), (3, 0)])

    async def stand_in(settings: Settings, now: datetime) -> tuple[int, int]:
        assert settings.krx_openapi_key == KEY and now.tzinfo is not None
        return next(answers)

    monkeypatch.setattr(cli, "probe_krx_etf", stand_in)
    assert cli.main(["probe-krx-etf"]) == 0
    assert cli.main(["probe-krx-etf"]) == 1


# The setting


def test_the_key_is_optional_secret_and_masked(tmp_path: Path) -> None:
    unset = Settings.from_env({"KIS_APP_KEY": "k", "KIS_APP_SECRET": "s", "KRX_OPENAPI_KEY": " "}, None)
    assert unset.krx_openapi_key is None and unset.krx_openapi_url == "https://data-dbg.krx.co.kr"
    env = {
        "KIS_APP_KEY": "k",
        "KIS_APP_SECRET": "s",
        "KRX_OPENAPI_KEY": KEY,
        "KRX_OPENAPI_URL": "http://x.test/",
    }
    settings = Settings.from_env(env, None)
    assert settings.krx_openapi_key == KEY and settings.krx_openapi_url == "http://x.test"
    assert KEY in secrets_of(settings) and KEY not in repr(settings)


# The scheduled job


def night(day: date, facts: Facts, record: JobRecord | None = None) -> Night:
    return Night(day, CALENDAR.is_trading_day(day), COMMIT, record, {}, facts)


def test_the_job_waits_for_the_key_then_runs_once_any_night() -> None:
    assert KRX_ETF_PROBE.name == "probe-krx-etf-305"
    assert KRX_ETF_PROBE.command == ("probe-krx-etf",)
    assert KRX_ETF_PROBE.timeout == timedelta(minutes=15), "short: the simulations and pre-validations follow"
    assert [t.issue for t in KRX_ETF_PROBE.targets] == [305]
    assert KRX_ETF_PROBE.label == ("ready-for-agent",)
    assert JOBS.index(NAV_PROBE) < JOBS.index(KRX_ETF_PROBE) < JOBS.index(GAPS_PROBE)
    first = next(i for i, j in enumerate(JOBS) if j.name.startswith(("prevalidate-", "simulate-")))
    assert JOBS.index(KRX_ETF_PROBE) < first, "before the simulations and pre-validations"
    keyed = Facts(krx_openapi=True)
    assert not KRX_ETF_PROBE.due(night(FRIDAY, Facts())), "no key: it waits"
    assert KRX_ETF_PROBE.due(night(FRIDAY, keyed)) and KRX_ETF_PROBE.due(night(SATURDAY, keyed))
    ran = JobRecord(FRIDAY, OK, at(FRIDAY, "22:30"), COMMIT, 0, 60.0)
    assert not KRX_ETF_PROBE.due(night(MONDAY, keyed, ran))
    assert not KRX_ETF_PROBE.due(night(MONDAY, keyed, replace(ran, outcome=FAILED, exit_code=1)))
    assert KRX_ETF_PROBE.due(night(MONDAY, keyed, replace(ran, outcome=INTERRUPTED)))
    output = "KRX Open API ETF 일별매매정보 확인 (#305) 2026-10-22 23:40 KST\n\n1. 날마다의 답"
    assert KRX_ETF_PROBE.targets[0].part(output) == output, "the whole output"
    assert "시세 값이 없다" in KRX_ETF_PROBE.targets[0].note


async def test_the_scheduler_runs_it_on_the_first_night_with_the_key_and_not_again(tmp_path: Path) -> None:
    clock = FakeClock(at(FRIDAY, "23:00"))
    runs = Runs(clock)
    state = JobState.load(tmp_path / STATE_FILE)
    key_set = {FRIDAY: False, SATURDAY: True, SUNDAY: True}
    tonight = [FRIDAY]

    async def facts() -> Facts:
        return Facts(krx_openapi=key_set[tonight[0]])

    async with aiohttp.ClientSession() as session:
        scheduler = Scheduler(
            [KRX_ETF_PROBE],
            AT,
            CALENDAR,
            state,
            GitHub(session, None, "codeyoma/krx-quant", "http://127.0.0.1:9"),
            Told(),
            clock,
            commit=COMMIT,
            facts=facts,
            runner=runs,
        )
        for day in (FRIDAY, SATURDAY, SUNDAY):
            tonight[0] = day
            await scheduler.run_night(day)
    assert runs.started == [("probe-krx-etf", at(FRIDAY, "23:00"))], "once, on the night the key was set"
    assert state.jobs["probe-krx-etf-305"].day == SATURDAY


async def test_the_nightly_facts_say_whether_the_key_is_set(tmp_path: Path) -> None:
    from krx_quant.scheduled import read_facts
    from tests.test_simulation_daily import CALENDAR as DAILY_CALENDAR
    from tests.test_simulation_daily import NIGHT, engine_on
    from tests.test_simulation_daily import settings as daily_settings

    s = daily_settings(tmp_path, signal=0.0, days=30)
    clock = FakeClock(NIGHT)
    for key, said in ((None, False), (KEY, True)):
        e = engine_on(s, clock, [])
        try:
            facts = await read_facts(replace(s, krx_openapi_key=key), DAILY_CALENDAR, clock, e)
        finally:
            e.store.close()
        assert facts.krx_openapi is said
