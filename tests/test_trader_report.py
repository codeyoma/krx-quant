"""The trader's nightly report (#402, #393 "리포트와 에이전트"): what it says of a day and its night,
and when it calls an agent, on stores the trader and the nightly re-fit write as they do.

The example trade (``test_trader_paper.py``): the 10:00 barrier prediction buys 14 shares at 70,100
(target 70,250, stop 69,850); sold at the target, 1.5 ticks up, it pays both commissions (147.21 and
147.525 won) and the tax (1,967 won), 2,261.735 won in all, and nets 2,100 − 2,261.735 = −161.735 won.
Every price, P&L and hit rate is made up.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest

from krx_quant.config import Settings
from krx_quant.market_calendar import MarketCalendar
from krx_quant.predictions import DB_NAME as ENGINE_DB
from krx_quant.predictions import PredictionStore
from krx_quant.trader.report import (
    AGENT,
    REFIT_HEADING,
    calls_agent,
    last_of_week,
    read,
    reasons,
    render,
    report_day,
    run_night,
)
from krx_quant.trader.selection import choose
from krx_quant.trader.settings import TraderSettings
from krx_quant.trader.store import DB_NAME, TraderStore
from tests.test_trader_limits import BOUGHT, TEN, rules
from tests.test_trader_promotion import CLOSE_CALL, WEEKDAYS, day_ran, judged_at, setup
from tests.test_trader_run import EngineApi
from tests.trader_doubles import (
    BARRIER,
    CALENDAR,
    FEE,
    AskPause,
    AskRearm,
    Barrier,
    Day,
    Read,
    Tick,
    at,
    cost_map,
    cost_row,
    keep,
    policy_cell,
    predictor,
    quote,
)

WEDNESDAY, THURSDAY, FRIDAY = date(2026, 10, 14), date(2026, 10, 15), date(2026, 10, 16)
PRICE = re.compile(r"70[,.]?[0-9]{3}")  # any of the made-up prices


def reported(state: Path, now: str = "22:40:00", calendar: MarketCalendar = CALENDAR) -> str:
    """The report a night's run at ``now`` prints."""
    return render(read(state / DB_NAME, report_day(calendar, at(now)), calendar))


def agent_line(text: str) -> str:
    return next(line for line in text.splitlines() if line.startswith(AGENT))


def champion_and_challenger(state: Path) -> None:
    """Policy 1 the champion, policy 2 a challenger (θ 0.65), both kept the night before."""
    keep(state, at("2026-10-13 22:31:00"), policy_cell(BARRIER, 0.6))
    keep(state, at("2026-10-13 22:32:00"), policy_cell(BARRIER, 0.65))


def test_a_day_s_trades_are_reported_with_each_policy_s_record_and_no_price(tmp_path: Path) -> None:
    """The champion and the challenger buy the 10:00 rise on the same quote and sell at the target;
    the 10:05 prediction is not a rise."""
    champion_and_challenger(tmp_path)
    Day(tmp_path, desk=True).run(
        [
            Read("10:00:02.3", [Barrier("10:00:00", p=(0.7, 0.2, 0.1))]),
            quote("10:00:02.5", 70_000, 70_100),
            quote("10:02:00", 70_250, 70_300),
            Read("10:05:02.3", [Barrier("10:05:00", p=(0.3, 0.4, 0.3))]),
            Tick("15:30:00"),
        ]
    )

    text = reported(tmp_path)

    lines = text.splitlines()
    assert lines[:2] == [
        "자동 매매 리포트 (#402) · 2026-10-14(수) · 모의(paper)",
        "에이전트 차례: 아님(밤의 기록만: 라벨을 바꾸지 않는다)",
    ]
    assert "그날 · 챔피언 정책 1" in lines
    assert (
        "1. 10:00:02 → 10:02:00 익절(take_profit) · barrier breiman(top) · 오름 0.70 ≥ θ 0.6 · "
        "p* 0.600, 하한 0.900 · 14주 · +1.5틱 · 비용 2,262원 · 순손익 -162원"
    ) in lines
    assert "읽은 예측(챔피언의 장부): 읽은 예측 2 · 산 것 1 · 사지 않은 것: not_up 1" in lines, (
        "the champion's book's decisions alone, not the challenger's"
    )
    assert "1. barrier breiman(top): 하한 0.900 − p* 0.600 = +0.300 · 300건 · θ 0.6 · 비율 1.00" in lines
    assert (
        "- 챔피언 정책 1: 그날 매매 1건, 순손익 -162원 · 기록 1거래일, 매매 1건, 순손익 -162원, "
        "날마다 평균 -162원, 하한 없음"
    ) in lines
    challenger = next(line for line in lines if line.startswith("- 도전 정책 2: "))
    assert "그날 매매 1건, 순손익 -162원" in challenger
    assert challenger.endswith(
        "챔피언과 함께 돈 거래일 1일, (도전 − 챔피언) 평균 +0원, 하한 없음(신뢰수준 95.00%) "
        "→ 날 수 부족(10일부터 판정)"
    )
    assert "- 바뀐 것 없음" in lines
    assert (
        "실전 관문: 챔피언 정책 1의 모의 기록 1일(이력 1거래일), 날마다 순손익 평균 -162원, "
        "다음 판정(1번째, 이력 20거래일째, α 1.520%)의 신뢰수준 96.96%로 지금 하한 없음 → 아직"
    ) in lines
    assert "실전 준비 (#408)" in lines, "what the live mode needs"
    assert "- 그날 닿은 한도: 없음" in lines and "- 정지: 없음" in lines
    assert lines[-1] == (
        "2026-10-14 · 매매 1건 · 순손익 -162원 · 그날 최저 -3,654원 · 누적 -162원 · 도는 중(running)"
    )
    assert not PRICE.search(text), "no price"
    assert not calls_agent(text)


def test_a_day_without_a_trade_says_why_no_cell_passed(tmp_path: Path) -> None:
    """The cost map passes no cell: the 4-minute barrier's breiman fails, its baseline has too few
    calls, the direction's consensus has no verdict, the other kinds no row."""
    body = cost_map(
        [
            cost_row(
                "barrier",
                top=predictor("breiman", 0.55, "fail"),
                baseline=predictor("barrier-persist", 0.61, "few", calls=40),
            ),
            cost_row("direction", consensus=predictor("consensus-final", 0.5, None)),
        ]
    )
    day = Day(tmp_path, selection=choose(body))
    trades = day.run([Read("10:00:02.3", [Barrier("10:00:00")]), quote("10:00:02.5", 70_000, 70_100)])
    assert trades == []

    text = reported(tmp_path)

    lines = text.splitlines()
    assert "매매 없음." in lines
    assert "매매할 칸 없음: 비용 지도에서 시장가 왕복으로 통과한 칸이 없다(no_pass)" in lines
    assert "- barrier top breiman: fail(하한 0.550, p* 0.630, 300건)" in lines
    assert "- barrier baseline barrier-persist: few(하한 0.610, p* 0.630, 40건)" in lines
    assert "- barrier_15m top -: no row" in lines
    assert (
        "칸 고르기: 09:00:00(그날 고르기 1번 가운데 장 마감 전 마지막) · "
        "비용 지도 as_of 2026-10-14T08:30:00+09:00" in lines
    )
    assert "정책별 모의 성적: 정책이 없다(첫 밤 정책 맞추기 전)" in lines
    assert "그날 · 챔피언 없음(첫 정책 전: 비용 지도의 칸을 기본값 θ와 λ로 산다)" in lines
    assert not calls_agent(text), "a day without a trade is a record"


def test_a_day_a_loss_limit_stopped_calls_an_agent(tmp_path: Path) -> None:
    """The daily limit at 0.5% (5,000 won): the stop's −6,448 won reaches it at 10:01:30
    (``test_trader_limits.py``); the 10:05 rise is skipped for it."""
    day = Day(tmp_path, rules=rules(daily=0.5))
    day.run([TEN, BOUGHT, quote("10:01:30", 69_800, 69_900), Read("10:05:02.3", [Barrier("10:05:00")])])

    text = reported(tmp_path)

    assert agent_line(text) == "에이전트 차례: 하루 손실 한도에 닿음(10:01:30)"
    lines = text.splitlines()
    assert any(
        line.startswith("1. 10:00:02 → 10:01:30 손절(stop_loss) · ") and "-3틱" in line for line in lines
    )
    assert (
        "- 그날 닿은 한도: 하루 손실 한도 10:01:30 · 손익 -6,448원(실현 -6,448원, 열린 포지션 +0원) · "
        "한도 5,000원 · 다음 거래일에 다시 돈다"
    ) in lines
    assert "사지 않은 것: daily_limit 1" in next(line for line in lines if line.startswith("읽은 예측("))
    assert "하루 손실 한도로 멈춤(daily_limit)" in next(line for line in lines if line.startswith("장부: "))
    assert calls_agent(text)


def test_a_cumulative_halt_holding_and_a_pause_are_reported(tmp_path: Path) -> None:
    """The cumulative limit at 0.4% (4,000 won), reached on the 14th by the open position at 69,900
    (−5,051 won), holds on the 15th until a re-arm; a pause asked for on the 15th holds until a
    resume."""
    day = Day(tmp_path, rules=rules(daily=1.0, cumulative=0.4))
    day.run([TEN, BOUGHT, quote("10:01:30", 69_900, 70_000), AskPause("2026-10-15 11:02:00", sell_now=False)])

    text = reported(tmp_path, "2026-10-15 22:40:00")

    lines = text.splitlines()
    assert (
        "- 누적 손실 한도로 멈춰 있다(2026-10-14에 닿음): 서버에서 `krx-quant trade rearm`을 할 때까지"
        in lines
    )
    assert "- 정지: 새 진입 정지(화면, 11:02:00부터): 서버에서 `krx-quant trade resume`을 할 때까지" in lines
    assert "- 그날과 그 밤의 요청: 11:02:00 정지(새 진입만)(화면)" in lines
    assert agent_line(text) == "에이전트 차례: 아님(밤의 기록만: 라벨을 바꾸지 않는다)", (
        "the halt was reached the day before: that night called"
    )
    assert "누적 손실 한도에 닿음(10:01:30)" in agent_line(reported(tmp_path, "2026-10-14 22:40:00"))


def test_a_cumulative_halt_re_armed_later_still_holds_in_the_reports_of_the_days_before(
    tmp_path: Path,
) -> None:
    """The halt of the 14th re-armed on the 16th at 09:00: the report of the 15th, run again after,
    shows it holding that night, as it did; the 16th's shows the re-arm instead (Codex's review)."""
    day = Day(tmp_path, rules=rules(daily=1.0, cumulative=0.4))
    day.run([TEN, BOUGHT, quote("10:01:30", 69_900, 70_000), AskRearm("2026-10-16 09:00:00")])
    assert day.asked and not isinstance(day.asked[0], str), "re-armed"
    held = "- 누적 손실 한도로 멈춰 있다(2026-10-14에 닿음): 서버에서 `krx-quant trade rearm`을 할 때까지"

    assert held in reported(tmp_path, "2026-10-15 22:40:00").splitlines()
    sixteenth = reported(tmp_path, "2026-10-16 22:40:00").splitlines()
    assert held not in sixteenth
    assert "- 그날과 그 밤의 요청: 09:00:00 누적 한도 다시 켜기(서버)" in sixteenth


def test_a_night_a_challenger_is_promoted_calls_an_agent(tmp_path: Path) -> None:
    """Policy 2 beats the champion on its tenth common day (``test_trader_promotion.py``: mean
    +1,100 won a day, lower bound +63 won) and is promoted that night."""
    setup(tmp_path, CLOSE_CALL)
    tenth = WEEKDAYS[9]
    assert judged_at(tmp_path, at(f"{tenth} 22:31:00")).promoted == 2

    text = reported(tmp_path, f"{tenth} 22:40:00")

    assert agent_line(text) == "에이전트 차례: 챔피언 교체(정책 1 → 정책 2)"
    lines = text.splitlines()
    assert lines[0] == f"자동 매매 리포트 (#402) · {tenth}(월) · 모의(paper)"
    change = next(line for line in lines if line.startswith("- 챔피언 교체: "))
    assert change.startswith(
        "- 챔피언 교체: 정책 1 → 정책 2(22:31:00). "
        "도전 정책 2이 챔피언 정책 1을 모의로 이겼다(#400): 함께 돈 거래일 10일"
    )
    challenger = next(line for line in lines if line.startswith("- 도전 정책 2: "))
    assert challenger.startswith("- 도전 정책 2: 그날 매매 1건, 순손익 -1,000원 · 기록 10거래일, 매매 10건, ")
    assert challenger.endswith(
        "챔피언과 함께 돈 거래일 10일, (도전 − 챔피언) 평균 +1,100원, 하한 +63원(신뢰수준 95.00%) → 통과"
    ), "the night's judgment, as it was made"
    assert any(
        line.startswith("실전 관문: 챔피언 정책 2의 모의 기록 10일(이력 10거래일)") for line in lines
    ), "the gate on the champion after the night"
    assert "그날 · 챔피언 정책 1" in lines, "the day's champion"
    the_night_after = reported(tmp_path, f"{WEEKDAYS[10]} 22:40:00")
    assert "챔피언 교체" not in agent_line(the_night_after), "that night only"


def test_the_night_the_live_gate_passes_calls_an_agent_and_not_the_nights_after(tmp_path: Path) -> None:
    """The champion's record passes the gate at the trader's first checkpoint, its twentieth day
    (``test_trader_promotion.py``: mean 1,000 won a day, lower bound +321 won at α_1), judged by that
    night's job before its report; the nineteenth night has no checkpoint, the twenty-first shows it
    passed the evening before (#408)."""
    keep(tmp_path, at("2026-08-31 22:31:00"), policy_cell(BARRIER, 0.8))
    texts = []
    for n, net in enumerate([2_000.0] * 12 + [-1_000.0] * 4 + [0.0] * 5):
        day_ran(tmp_path, 1, WEEKDAYS[n], net, champion=True)
        judged_at(tmp_path, at(f"{WEEKDAYS[n]} 22:31:00"))
        texts.append(reported(tmp_path, f"{WEEKDAYS[n]} 22:40:00"))
    nineteenth, twentieth, twenty_first = texts[18:]

    assert "실전 관문" not in agent_line(nineteenth)
    assert "1번째, 이력 20거래일째" in next(
        line for line in nineteenth.splitlines() if line.startswith("실전 관문: ")
    )
    assert agent_line(twentieth) == "에이전트 차례: 실전 관문을 넘음(챔피언 정책 1)"
    lines = twentieth.splitlines()
    assert any(
        line.startswith(
            f"실전 관문 판정 1번째(모의 이력 20거래일째, {WEEKDAYS[19]}, α 1.520%): "
            "챔피언 정책 1의 모의 기록 20일, 날마다 순손익 평균 +1,000원, 하한 +321원(신뢰수준 96.96%)"
        )
        and line.endswith("→ 넘었다")
        for line in lines
    )
    assert any(
        line.startswith("실전 관문: 챔피언 정책 1의 모의 기록 20일(이력 20거래일) → 넘었다(1번째 판정")
        and "그 밤 처음 넘었다" in line
        for line in lines
    )
    assert "실전 관문" not in agent_line(twenty_first), "passed the evening before already"
    assert "넘었다" in next(line for line in twenty_first.splitlines() if line.startswith("실전 관문: "))


def test_the_week_s_last_trading_day_calls_an_agent(tmp_path: Path) -> None:
    """Friday's night calls; with Friday closed, Thursday's does; a Wednesday's does not."""
    friday_closed = MarketCalendar({FRIDAY: False})
    assert [last_of_week(CALENDAR, d) for d in (WEDNESDAY, THURSDAY, FRIDAY)] == [False, False, True]
    assert last_of_week(friday_closed, THURSDAY)

    assert (
        agent_line(reported(tmp_path, "2026-10-16 22:40:00"))
        == "에이전트 차례: 이번 주 마지막 거래일(금요일)"
    )
    thursday = reported(tmp_path, "2026-10-15 22:40:00", friday_closed)
    assert agent_line(thursday) == "에이전트 차례: 이번 주 마지막 거래일(목요일)"
    assert not calls_agent(reported(tmp_path, "2026-10-14 22:40:00"))
    assert "trader의 저장소(state/trader.sqlite3)가 없다" in thursday, "no store: the trader never ran"


@pytest.mark.parametrize(
    ("now", "day"),
    [
        ("2026-10-16 22:40:00", FRIDAY),  # the night's evening
        ("2026-10-17 03:00:00", FRIDAY),  # after midnight: still Friday's night
        ("2026-10-19 08:29:59", FRIDAY),  # Monday before its pick
        ("2026-10-19 08:30:00", date(2026, 10, 19)),
        ("2026-10-15 05:00:00", WEDNESDAY),
    ],
)
def test_the_day_reported_is_the_one_whose_night_it_is(now: str, day: date) -> None:
    assert report_day(CALENDAR, at(now)) == day


def test_the_night_s_failed_re_fit_calls_an_agent() -> None:
    made = read(Path("/nonexistent") / DB_NAME, WEDNESDAY, CALENDAR, refit=1)
    assert reasons(made) == ["밤 정책 맞추기 실패(종료 코드 1)"]
    assert calls_agent(render(made))


def test_no_account_nor_order_number_is_in_the_report(tmp_path: Path) -> None:
    """A broker's order number on the trade's orders (as KIS gives one in mock and live) and an
    account in the run's settings: neither is read into the report; the capital is."""
    account, order = "50123456-01", "0000817263"
    Day(tmp_path).run([TEN, BOUGHT, quote("10:02:00", 70_250, 70_300)])
    store = TraderStore(tmp_path / DB_NAME)
    try:
        store.add_run(at("09:00:00"), "paper", {"capital": 1_000_000, "account": account})
        with store._db:
            store._db.execute("UPDATE orders SET broker_id = ?", (order,))
    finally:
        store.close()

    text = reported(tmp_path)

    assert "운용 상한 1,000,000원" in text
    for secret in (account, account[:8], order, order.lstrip("0")):
        assert secret not in text
    assert not PRICE.search(text)


async def test_the_scheduled_job_s_night_reports_above_the_re_fit_s_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``refit-policy --report``: the re-fit first (no cell passes: the first policy, empty, is the
    champion), then the day's report printed above its output."""
    PredictionStore(tmp_path / ENGINE_DB).close()
    settings = Settings(kis_app_key="", kis_app_secret="", state_dir=tmp_path, fee_rate=FEE)
    failing = cost_map([cost_row("barrier", top=predictor("breiman", 0.60, "fail"))])

    async with EngineApi([failing]).serving() as url:
        code = await run_night(settings, TraderSettings(engine_url=url), at("22:31:00"))

    assert code == 0
    out = capsys.readouterr().out
    report, refit = out.split(f"\n{REFIT_HEADING}\n")
    assert report.splitlines()[:2] == [
        "자동 매매 리포트 (#402) · 2026-10-14(수) · 모의(paper)",
        "에이전트 차례: 첫 챔피언(정책 1)",
    ]
    assert "- 첫 챔피언: 정책 1(22:31:00). 처음 맞춘 정책(#399)" in report
    assert "- 맞춘 정책 1(챔피언): 칸 없음 (정책 전체 모의 0건, 순손익 +0원)" in report
    assert refit.lstrip("\n").startswith("도전 정책 판정 (#400)\n챔피언이 아직 없다")
    assert "정책 맞추기 (#399): 정책 1 · 챔피언(처음 맞춘 정책)" in refit


async def test_a_re_fit_that_fails_is_reported_and_its_exit_code_kept(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = Settings(kis_app_key="", kis_app_secret="", state_dir=tmp_path, fee_rate=FEE)

    async with EngineApi([503]).serving() as url:
        code = await run_night(settings, TraderSettings(engine_url=url), at("22:31:00"))

    assert code == 1
    out = capsys.readouterr().out
    report, refit = out.split(f"\n{REFIT_HEADING}\n")
    assert agent_line(report) == "에이전트 차례: 밤 정책 맞추기 실패(종료 코드 1)"
    assert "비용 지도를 읽지 못해 정책을 만들지 않았다" in refit


def test_the_report_reads_the_store_read_only(tmp_path: Path) -> None:
    champion_and_challenger(tmp_path)
    Day(tmp_path, desk=True).run([TEN, BOUGHT])
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.name.startswith(DB_NAME)}

    reported(tmp_path)

    assert {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.name.startswith(DB_NAME)} == before
