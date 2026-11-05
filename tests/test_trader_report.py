"""The trader's nightly report (#402, #393 "리포트와 에이전트"): what it says of a day and its night,
and when it calls an agent, on stores the trader and the nightly re-fit write as they do.

The example trade (``test_trader_paper.py``): the 10:00 barrier prediction buys 14 shares at 70,100
(target 70,250, stop 69,850); sold at the target, 1.5 ticks up, it pays both commissions (147.21 and
147.525 won) and the tax (1,967 won), 2,261.735 won in all, and nets 2,100 − 2,261.735 = −161.735 won.
Every price, P&L and hit rate is made up.
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

import pytest

from krx_quant.barriers import BARRIER as BARRIER_KIND
from krx_quant.barriers import TIMEOUT, UP_FIRST
from krx_quant.config import Settings
from krx_quant.kinds import Answer
from krx_quant.market_calendar import MarketCalendar
from krx_quant.predictions import DB_NAME as ENGINE_DB
from krx_quant.predictions import PredictionStore
from krx_quant.trader import MOCK, PAPER, SYMBOL, review
from krx_quant.trader import policy as policies
from krx_quant.trader.paper import STOP_LOSS, TAKE_PROFIT, TIME
from krx_quant.trader.report import (
    AGENT,
    PRACTICE_TRADES,
    REFIT_HEADING,
    calls_agent,
    last_of_week,
    practice_trades,
    read,
    reasons,
    render,
    report_day,
    run_night,
    run_report,
)
from krx_quant.trader.selection import choose
from krx_quant.trader.settings import TraderSettings
from krx_quant.trader.store import DB_NAME, Entry, Exit, TraderStore
from tests.promotion_doubles import CLOSE_CALL, WEEKDAYS, day_ran, judged_at, setup
from tests.test_trader_limits import BOUGHT, TEN, rules
from tests.test_trader_promotion import begun
from tests.test_trader_risk import TRADER, loss, rise
from tests.trader_doubles import (
    BARRIER,
    CALENDAR,
    FEE,
    AskPause,
    AskRearm,
    Barrier,
    Day,
    EngineApi,
    Read,
    Tick,
    at,
    challenger,
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
    """Policy 1 the champion, policy 2 a challenger (θ 0.65, its cohort's alone, #473), both kept the
    night before."""
    keep(state, at("2026-10-13 22:31:00"), policy_cell(BARRIER, 0.6))
    challenger(state, at("2026-10-13 22:32:00"), policy_cell(BARRIER, 0.65))


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
    assert "1. barrier breiman(top): 하한 0.900 − p* 0.600 = +0.300 · 300건 · θ 0.6 · 손실 예산" in lines
    assert (
        "- 챔피언 정책 1: 그날 매매 1건, 순손익 -162원 · 기록 1거래일, 매매 1건, 순손익 -162원, "
        "날마다 평균 -162원, 하한 없음"
    ) in lines
    theirs = next(line for line in lines if line.startswith("- 도전 정책 2: "))
    assert "그날 매매 1건, 순손익 -162원" in theirs
    assert theirs.endswith(
        "챔피언 정책 1과 함께 돈 거래일 1일, (도전 − 챔피언) 평균 +0원, 하한 없음(신뢰수준 95.00%, 묶음 안 "
        "Bonferroni) · 지금은 날 수 부족(10일 미만이면 판정 불가) · 검토 날 2026-11-10 밤에 한 번 판정한다"
    ), "a figure until its cohort's review night (#473)"
    assert "- 바뀐 것 없음" in lines
    assert (
        "실전 관문: 챔피언 정책 1의 챔피언으로 돈 모의 기록 1일(이력 1거래일), 날마다 순손익 평균 -162원, "
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
    assert "그날 · 챔피언 없음(첫 정책 전: 연습 말고는 사지 않는다, #469)" in lines
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
    +1,100 won a day, lower bound +63 won), a review day (#469: the 20th weekday from 08-18), and is
    promoted that night."""
    begun(tmp_path, date(2026, 8, 18))
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
        "도전 정책 2이 챔피언 정책 1을 모의로 이겼다(#473): 주기 1 묶음의 검토 날 2026-09-14, "
        "함께 돈 거래일 10일"
    )
    assert (
        "- 묶음 판정 (#473): 주기 1(2026-08-18–2026-09-14) · 챔피언 정책 1과 견줌 · 정책 2 챔피언이 됨(하한 "
        "+63원, 10일)" in lines
    )
    theirs = next(line for line in lines if line.startswith("- 도전 정책 2: "))
    assert theirs.startswith("- 도전 정책 2: 그날 매매 1건, 순손익 -1,000원 · 기록 10거래일, 매매 10건, ")
    assert theirs.endswith(
        "챔피언 정책 1과 함께 돈 거래일 10일, (도전 − 챔피언) 평균 +1,100원, 하한 +63원(신뢰수준 95.00%, "
        "묶음 안 Bonferroni) · 그 밤 판정: 챔피언이 됨"
    ), "the night's judgment, as it was made"
    assert any(
        line.startswith("실전 관문: 챔피언 정책 2의 챔피언으로 돈 모의 기록 0일(이력 10거래일)")
        for line in lines
    ), "the gate on the champion after the night: its days as a challenger do not count"
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
            "챔피언 정책 1의 챔피언으로 돈 모의 기록 20일, 날마다 순손익 평균 +1,000원, "
            "하한 +321원(신뢰수준 96.96%)"
        )
        and line.endswith("→ 넘었다")
        for line in lines
    )
    assert any(
        line.startswith(
            "실전 관문: 챔피언 정책 1의 챔피언으로 돈 모의 기록 20일(이력 20거래일) → 넘었다(1번째 판정"
        )
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


def started(state: Path, mode: str = "paper") -> None:
    """The trader's start at 09:00 as ``run.py`` keeps it: its capital and risk rules (#471), the code's."""
    store = TraderStore(state / DB_NAME)
    try:
        store.add_run(
            at("09:00:00"),
            mode,
            {
                "capital": 1_000_000,
                "risk_per_trade_percent": 0.5,
                "loss_streak": 3,
                "cooldown_minutes": 30,
                "daily_buy_orders": 20,
            },
        )
    finally:
        store.close()


def test_a_day_s_execution_cost_and_risk_rules_are_reported_with_no_price(tmp_path: Path) -> None:
    """#475: each trade's legs and the day's means; the loss budget, the buy orders and the cooldown.
    The 10:00 rise is decided with the 10:00:01 quote's mid-point (69,950) known and bought at the ask
    70,100: 1.5 ticks adverse; the 10:10 and 10:20 ones with the 10:00:30 quote's (69,850): 2.5. Each
    is stopped on a quote whose bid 69,800 is half a tick under its mid-point. Three net losses in a row:
    the cooldown from the third's exit, 10:20:30, to 10:50:30, which skips the 10:30 rise."""
    started(tmp_path)
    Day(tmp_path, rules=TRADER).run(
        [quote("10:00:01", 69_900, 70_000), *loss("10:00"), *loss("10:10"), *loss("10:20"), rise("10:30")]
    )

    text = reported(tmp_path)

    lines = text.splitlines()
    executed = [line for line in lines if line.startswith("   실행: ")]
    assert executed == [
        f"   실행: 매수 +{ticks:.2f}틱(결정 때 중간가 대비) · 결정 뒤 주문 0.2초, 체결 0.2초 · "
        "매도(stop_loss) +0.50틱(결정 때 중간가 대비) · 결정 뒤 주문 0.0초, 체결 0.0초"
        for ticks in (1.5, 2.5, 2.5)
    ]
    assert (
        "실행 비용 (#475): 틱은 불리한 쪽이 +(매수는 결정 때 중간가보다 비싸게, 매도는 싸게 체결; 익절은 "
        "목표가 대비), 지연은 결정부터 · 시각은 체결한 호가를 받은 때다"
        "(모의 체결: collector가 받은 호가 시각)"
    ) in lines
    assert (
        "- 평균: 매수 +2.17틱, 결정 뒤 체결 0.2초(3건) · 손절·시간·정지·한도 매도 +0.50틱, 결정 뒤 체결 "
        "0.0초(3건)"
    ) in lines
    assert (
        "- 위험 규칙 (#471): 매매마다 손실 예산 5,000원(운용 상한 1,000,000원의 0.5%) · "
        "그날 매수 주문 3/20 · 순손실 3번 연달으면 30분 쉰다: 그날 쉼 1번(10:20:30–10:50:30) · "
        "그날 마지막 청산까지 연달은 순손실 3번"
    ) in lines
    assert "읽은 예측(챔피언의 장부): 읽은 예측 4 · 산 것 3 · 사지 않은 것: cooldown 1" in lines
    assert not PRICE.search(text), "no price"


def test_a_take_profit_s_execution_is_from_its_target_and_a_store_before_475_has_none(tmp_path: Path) -> None:
    """#475: the take-profit's leg from its target, the limit resting from the buy; a store whose trades
    were kept before #475 (no ``decided_mid`` column) reports its trades without the lines."""
    Day(tmp_path).run([TEN, BOUGHT, quote("10:02:00", 70_250, 70_300)])

    lines = reported(tmp_path).splitlines()

    [executed] = [line for line in lines if line.startswith("   실행: ")]
    assert executed == (
        "   실행: 매수 ?(결정 때 중간가 없음) · 결정 뒤 주문 0.2초, 체결 0.2초 · "
        "매도(take_profit) +0.00틱(목표가 대비: 매수 뒤 걸어 둔 지정가) · 매수 체결 뒤 주문 0.0초"
    ), "no quote before the decision: no mid-point known"
    assert "- 평균: 매수 없음, 결정 뒤 체결 0.2초(1건) · 익절 목표가 대비 +0.00틱(1건)" in lines

    old = tmp_path / "old"
    old.mkdir()
    Day(old).run([TEN, BOUGHT, quote("10:02:00", 70_250, 70_300)])
    db = sqlite3.connect(old / DB_NAME)
    with db:
        db.execute("ALTER TABLE trades DROP COLUMN decided_mid")
    db.close()
    text = reported(old)
    assert "실행" not in text and "1. 10:00:02 → 10:02:00 익절(take_profit)" in text
    assert "위험 규칙" not in text, "no start of the trader's that kept its risk rules"


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
    assert refit.lstrip("\n").startswith(
        "도전 정책 판정 (#473: 묶음을 검토 날 밤에 한 번)\n챔피언이 아직 없다"
    )
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


# The decision sources (#560): what each made of the day, and whether the calls it traded were right.


def kept(
    state: Path,
    prediction: int,
    entered: str,
    shares: int,
    fees: tuple[float, float, float],
    out: tuple[str, float, str] | None,
    *,
    mode: str = MOCK,
    policy: int | None = 1,
    champion: bool = True,
    shadow: bool = False,
    practice: bool = False,
    p_star: float = 0.63,
) -> None:
    """A trade of a book kept as the trader keeps one (``store.enter``, ``exit``): ``shares`` of the
    barrier's ``prediction`` bought at 70,100 at ``entered``, its buy's commission, sale's commission
    and tax ``fees``, sold as ``out`` says (when, at what, why) or still open."""
    store = TraderStore(state / DB_NAME)
    try:
        close = at(entered) - timedelta(seconds=2)
        trade = store.enter(
            0,
            Entry(
                mode=mode,
                symbol=SYMBOL,
                kind=BARRIER.kind,
                predictor=BARRIER.predictor,
                prediction_id=prediction,
                bar_close=close,
                probability=0.7,
                threshold=0.6,
                anchor=70_050,
                tick=100,
                width_ticks=2,
                target=70_250,
                stop=69_850,
                time_barrier=close + timedelta(minutes=4),
                decided_at=at(entered),
                entry_at=at(entered),
                entry_price=70_100,
                quantity=shares,
                buy_fee=fees[0],
                quote_at=at(entered),
                role="top",
                p_star=p_star,
                low=0.9,
                policy_id=policy,
                champion=champion,
                shadow=shadow,
                practice=practice,
            ),
        )
        if out is not None:
            hms, sold, why = out
            store.exit(trade, Exit(at(hms), sold, why, fees[1], fees[2], at(hms)))
    finally:
        store.close()


def four_sources(state: Path) -> None:
    """A mock day's four decision sources. The champion's book and its paper shadow buy the 10:00 rise
    as the module's example trade: 14 shares sold at the target, 1.5 ticks up (+21 ticks for the 14),
    2,261.735 won of costs, −161.735 won. The practice buys a share of the 10:10 rise, stopped 2 ticks
    down (costs 10.5 + 10.5 + 139.8 = 160.8 won: −360.8 won), and one of the 10:20 rise, still open.
    The challenger buys 10 shares of the 10:00 rise (p* 0.65), sold at its time barrier where it
    bought (costs 105.15 + 105.15 + 1,402 = 1,612.3 won), and of the 10:20 rise (p* 0.60), stopped 2
    ticks down (−2,000 won, costs 105.15 + 104.85 + 1,398 = 1,608 won). The engine answers the 10:00
    rise up first and the 10:10 one by its timeout; the 10:20 one is not answered yet."""
    champion_and_challenger(state)
    started(state, MOCK)
    engine = PredictionStore(state / ENGINE_DB)
    try:
        for close in ("10:00:00", "10:10:00", "10:20:00"):
            Barrier(close).write(engine)
        engine.add_kind_answers(
            BARRIER_KIND.name,
            BARRIER_KIND.answer_version,
            [
                (SYMBOL, at("10:00:00"), Answer(UP_FIRST), False),
                (SYMBOL, at("10:10:00"), Answer(TIMEOUT), False),
            ],
        )
    finally:
        engine.close()
    with closing(sqlite3.connect(state / ENGINE_DB)) as db:
        ids = [r[0] for r in db.execute("SELECT id FROM kind_predictions ORDER BY bar_close")]
    ten, ten_ten, ten_twenty = ids
    example = (147.21, 147.525, 1_967.0)
    kept(state, ten, "10:00:02", 14, example, ("10:02:00", 70_250, TAKE_PROFIT))
    kept(
        state,
        ten,
        "10:00:02",
        14,
        example,
        ("10:02:00", 70_250, TAKE_PROFIT),
        mode=PAPER,
        champion=False,
        shadow=True,
    )
    stopped, still_open = ("10:12:00", 69_900, STOP_LOSS), None
    kept(state, ten_ten, "10:10:02", 1, (10.5, 10.5, 139.8), stopped, policy=None, practice=True)
    kept(state, ten_twenty, "10:20:02", 1, (10.5, 0.0, 0.0), still_open, policy=None, practice=True)
    timed, stopped = ("10:04:00", 70_100, TIME), ("10:21:00", 69_900, STOP_LOSS)
    for prediction, entered, fees, out, p_star in [
        (ten, "10:00:02", (105.15, 105.15, 1_402.0), timed, 0.65),
        (ten_twenty, "10:20:02", (105.15, 104.85, 1_398.0), stopped, 0.60),
    ]:
        kept(state, prediction, entered, 10, fees, out, mode=PAPER, policy=2, champion=False, p_star=p_star)


SOURCES_HEAD = "출처 | 매매 | 체결 손익 | 비용 | 순손익 | 청산 | 맞음 | p*"


def test_each_decision_source_s_trades_are_tallied_from_the_stored_day(tmp_path: Path) -> None:
    """#560: a row each, in won and in ticks (each trade's, times its shares), the exits' shares and
    the calls traded against the engine's answers."""
    four_sources(tmp_path)

    lines = reported(tmp_path).splitlines()

    head = lines.index(SOURCES_HEAD)
    assert lines[head - 1].startswith("출처별 손익 (#560): 그날 진입한 매매, 손익은 그 가운데 청산한 것")
    example = (
        "+21.0틱 | 22.6틱 2,262원 | -1.6틱 -162원 | 목표 100%, 손절 0%, 시간 0% | 100%(상승 1·보합 0·하락 0)"
    )
    assert lines[head + 1 : head + 5] == [
        f"챔피언 정책 1 | 1건 | {example} | 0.630",
        "연습(KIS 모의) | 2건(열림 1) | -2.0틱 | 1.6틱 161원 | -3.6틱 -361원 | 목표 0%, 손절 100%, 시간 0% | "
        "0%(상승 0·보합 1·하락 0, 답 없음 1) | 0.630",
        f"그림자(챔피언 정책 1, 모의 체결) | 1건 | {example} | 0.630",
        "도전 정책 2 | 2건 | -20.0틱 | 32.2틱 3,220원 | -52.2틱 -5,220원 | 목표 0%, 손절 50%, 시간 50% | "
        "100%(상승 1·보합 0·하락 0, 답 없음 1) | 0.600–0.650",
    ]
    assert lines[head + 5] == "", "the table ends there"

    (tmp_path / ENGINE_DB).unlink()
    without = reported(tmp_path).splitlines()
    assert without[without.index(SOURCES_HEAD) + 1].endswith("| ?(엔진 DB를 읽지 못함) | 0.630")


def test_a_paper_day_s_sources_are_the_champion_s_book_and_each_challenger(tmp_path: Path) -> None:
    """In paper the champion's book is the paper book itself: no shadow, no practice; a source that
    bought nothing has its row all the same."""
    champion_and_challenger(tmp_path)
    Day(tmp_path, desk=True).run([TEN, BOUGHT])

    lines = reported(tmp_path).splitlines()

    head = lines.index(SOURCES_HEAD)
    assert [line.split(" | ")[:2] for line in lines[head + 1 : head + 3]] == [
        ["챔피언 정책 1", "1건(열림 1)"],
        ["도전 정책 2", "1건(열림 1)"],
    ]
    assert lines[head + 1].endswith("| - | - | - | - | 답 없음 1 | 0.600"), "open: no P&L, no answer yet"
    assert lines[head + 3] == ""


def test_the_practice_s_trades_are_folded_apart_and_the_report_says_their_sum(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#560: the report keeps the practice's sum; its trades a line each go to the night job's fold
    (``PRACTICE_TRADES``), or under the report when it is run by hand."""
    four_sources(tmp_path)
    settings = Settings(kis_app_key="", kis_app_secret="", state_dir=tmp_path, fee_rate=FEE)
    practised = "1. 10:10:02 → 10:12:00 손절(stop_loss) · barrier breiman(top) · 오름 0.70 ≥ θ 0.6"

    text = reported(tmp_path)

    assert practised not in text
    assert (
        "연습 매매 2건(KIS 모의, 챔피언 칸이 없던 때 1주씩: 승격·관문·모의 기록·손실 한도에는 세지 않고, "
        "계좌 손익·위험 규칙·실행 점검에는 센다), 순손익 합 -361원 · 매매별 줄은 맨 아래 `연습 매매별 줄`에 "
        "따로 둔다(#560)"
    ) in text.splitlines()
    assert "1. 10:00:02 → 10:02:00 익절(take_profit)" in text, "the champion's trades stay"
    folded = practice_trades(read(tmp_path / DB_NAME, WEDNESDAY, CALENDAR)).splitlines()
    assert folded[0] == "연습 매매별 줄 (#560) · 2026-10-14 · 2건(진입 순, 가격은 싣지 않는다)"
    assert folded[1].startswith(practised) and folded[2].startswith("   실행: "), "with its execution"
    assert folded[3].startswith("2. 10:20:02 → 열림(아직 청산하지 않았다)") and len(folded) == 5

    assert run_report(settings, at("22:40:00"), fold=tmp_path / PRACTICE_TRADES) == 0
    out = capsys.readouterr().out
    assert practised not in out and AGENT in out
    assert (tmp_path / PRACTICE_TRADES).read_text(encoding="utf-8").splitlines() == folded

    assert run_report(settings, at("22:40:00")) == 0
    by_hand = capsys.readouterr().out
    assert by_hand.rstrip("\n").endswith("\n".join(folded)), "by hand: printed under the report"
    assert not PRICE.search((tmp_path / PRACTICE_TRADES).read_text(encoding="utf-8")), "no price"


def test_the_second_line_keeps_the_form_calls_agent_reads(tmp_path: Path) -> None:
    """#560 adds sections below; the second line, which the job's label reads (``calls_agent``), is
    as it was: the agent's turn and its reasons, or none."""
    four_sources(tmp_path)

    wednesday = reported(tmp_path).splitlines()
    friday = reported(tmp_path, "2026-10-16 22:40:00").splitlines()

    assert wednesday[1] == "에이전트 차례: 아님(밤의 기록만: 라벨을 바꾸지 않는다)"
    assert friday[1] == "에이전트 차례: 이번 주 마지막 거래일(금요일)"
    assert [calls_agent("\n".join(lines)) for lines in (wednesday, friday)] == [False, True]
    assert [line for line in wednesday if line.startswith(AGENT)] == [wednesday[1]], "that line alone"


def kept_empty(state: Path, left_out: list[dict[str, object]]) -> int:
    """The champion kept on the night before the test day with no cell, as the re-fit keeps one whose
    every cell it left out (``policy.add``)."""
    db = policies.connect(state / DB_NAME)
    try:
        review.begin(db, at("2026-10-13 22:31:00"), CALENDAR.session)
        version, _ = policies.add(
            db, at("2026-10-13 22:31:00"), "밤 맞추기", [], left_out, {"grid": {"min_days": 20}}
        )
    finally:
        db.close()
    return version


def test_a_day_no_cell_traded_says_the_champion_had_none_and_why_its_fit_left_them_out(
    tmp_path: Path,
) -> None:
    """#560: the cost map passes no cell (``no_pass``), but since #480 that decides nothing: the empty
    champion does, and its fit's night left out every cell for too few recorded days."""
    days = [("direction", "consensus-final", "consensus", 18), ("barrier", "barrier-persist", "baseline", 13)]
    kept_empty(
        tmp_path,
        [{"kind": k, "predictor": p, "role": r, "why": "few_days", "recorded_days": d} for k, p, r, d in days]
        + [
            {"kind": "barrier", "predictor": "breiman", "role": "top", "why": "few_trades", "most_trades": 12}
        ],
    )
    failing = cost_map([cost_row("barrier", top=predictor("breiman", 0.55, "fail"))])
    Day(tmp_path, desk=True, picks=choose(failing).picks, selection=choose(failing)).run([TEN])

    lines = reported(tmp_path).splitlines()

    assert (
        "매매할 칸 없음: 챔피언 정책 1에 칸이 없다(빈 정책 · 고르기의 기록은 no_pass이나, #480 뒤로 비용 "
        "지도의 판정은 매매를 정하지 않는다)"
    ) in lines
    assert (
        "- 챔피언 정책 1: 2026-10-13 22:31에 맞춘 정책: 그 밤 맞추기가 칸을 남기지 않았다 · 뺀 칸 3: "
        "few_days 2(녹화한 날 13–18일, 20일 필요), few_trades 1(1주씩 잰 체결이 가장 많을 때 12건, 20건 필요)"
    ) in lines
    assert "비용 지도가 본 칸(그 판정은 #480 뒤로 매매를 정하지 않는다):" in lines
    assert "- barrier top breiman: fail(하한 0.550, p* 0.630, 300건)" in lines


def test_a_champion_s_cell_whose_predictor_is_gone_is_why_none_traded(tmp_path: Path) -> None:
    """The champion's one cell is gone (#469): on 10-13 the engine made barrier predictions of
    barrier-persist and none of breiman (``test_trader_review.py``). The pick says so."""
    keep(tmp_path, at("2026-10-13 22:31:00"), policy_cell(BARRIER))
    engine = PredictionStore(tmp_path / ENGINE_DB)
    Barrier("2026-10-13 10:00:00", predictor="barrier-persist").write(engine)
    engine.close()
    Day(tmp_path, desk=True, gone=True).run([TEN])

    lines = reported(tmp_path).splitlines()

    assert (
        "매매할 칸 없음: 챔피언 정책 1의 칸 1개가 그날 매매하지 않았다: barrier breiman(top) "
        "gone(not_in_policy)" in lines
    )


async def test_the_night_s_fit_that_kept_nothing_says_what_it_left_out(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The first night keeps the empty champion (the cost map's one cell never predicted: left out,
    #472); the second fits the same and keeps nothing: its report says so, with why it left the cell
    out (#560), where it said only the causes it might be."""
    PredictionStore(tmp_path / ENGINE_DB).close()
    settings = Settings(kis_app_key="", kis_app_secret="", state_dir=tmp_path, fee_rate=FEE)
    failing = cost_map([cost_row("barrier", top=predictor("breiman", 0.60, "fail"))])

    async with EngineApi([failing]).serving() as url:
        assert await run_night(settings, TraderSettings(engine_url=url), at("22:31:00")) == 0
        capsys.readouterr()
        assert await run_night(settings, TraderSettings(engine_url=url), at("2026-10-15 22:31:00")) == 0

    report, refit = capsys.readouterr().out.split(f"\n{REFIT_HEADING}\n")
    assert (
        "- 밤 정책 맞추기: 새 정책을 저장하지 않았다: 맞춘 값(넣은 칸 0개)이 챔피언 정책 1과 같다 · 뺀 칸 1: "
        "few_days 1(녹화한 날 0일, 20일 필요)"
    ) in report.splitlines()
    assert "새 정책 없음 · 맞춘 값이 챔피언 정책 1과 같아 저장하지 않았다" in refit
    assert (tmp_path / PRACTICE_TRADES).read_text(encoding="utf-8") == (
        "연습 매매별 줄 (#560) · 2026-10-15: 그날 연습 매매가 없다.\n"
    ), "written every night, for the job's fold"
