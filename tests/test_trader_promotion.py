"""The promotion of a challenger and the live gate (#400, #393 "정책과 자기 개선", "실전 관문"):
worked by hand, on paper records as the trader keeps them (``policy_days`` and its trades).

The t quantiles used (``stats._t_quantile``): 2.262157 at 0.975 with 9 degrees of freedom (ten
days, 95% two-sided), 2.933324 at 1 − 0.05 ÷ 6 with 9 (ten days, Bonferroni over three
challengers: 98.33% two-sided), 2.093024 at 0.975 with 19 (twenty days). Every P&L is made up.
"""

from __future__ import annotations

import math
import re
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

from krx_quant.feeds.base import KST
from krx_quant.trader import policy as policies
from krx_quant.trader import promotion
from krx_quant.trader.promotion import (
    FEW_DAYS,
    MARKET_OPEN,
    NOT_ABOVE,
    PolicyDay,
    Record,
    bonferroni,
    gate,
    judge,
    promote,
)
from krx_quant.trader.refit import admit_now, promote_now
from krx_quant.trader.store import DB_NAME, TraderStore
from tests.trader_doubles import (
    BARRIER,
    CALENDAR,
    Barrier,
    Day,
    Read,
    Repick,
    at,
    keep,
    policy_cell,
    quote,
)

# The weekdays from 09-01 (the calendar guesses they trade).
WEEKDAYS = [d for d in (date(2026, 9, 1) + timedelta(days=i) for i in range(60)) if d.weekday() < 5]


def record(
    version: int, nets: Sequence[float], champion: bool = False, stopped: Sequence[date] = ()
) -> Record:
    days = WEEKDAYS[: len(nets)]
    return Record(
        version,
        tuple(PolicyDay(d, n, int(n != 0), champion, d in stopped) for d, n in zip(days, nets, strict=True)),
    )


# Seven days the challenger made 2,000 won more than the champion and three it made 1,000 less:
# mean 1,100; deviations 900 (×7) and −2,100 (×3), their squares 18,900,000, the variance
# 18,900,000 ÷ 9 = 2,100,000, the standard error √(2,100,000 ÷ 10) = 458.2576.
CLOSE_CALL = [2_000.0] * 7 + [-1_000.0] * 3


def test_a_challenger_needs_ten_common_days() -> None:
    champion = record(1, [0.0] * 10, champion=True)
    nine = judge(1, {1: champion, 2: record(2, [1_000.0] * 9)}, [2])
    ten = judge(1, {1: champion, 2: record(2, [1_000.0] * 10)}, [2])

    assert (nine[0].days, nine[0].why, nine[0].passed) == (9, FEW_DAYS, False)
    assert (ten[0].days, ten[0].low, ten[0].passed) == (10, 1_000.0, True), (
        "no spread: the lower bound is the mean"
    )


def test_a_lower_bound_at_or_below_zero_does_not_promote() -> None:
    """Five days +3,000 and five −2,000: mean 500, deviations ±2,500, the variance 62,500,000 ÷ 9,
    the standard error 833.33; the lower bound 500 − 2.262157 × 833.33 = −1,385.13. A challenger
    exactly as good as the champion has a lower bound of 0: not above it."""
    champion = record(1, [0.0] * 10, champion=True)
    mixed = record(2, [3_000.0] * 5 + [-2_000.0] * 5)
    alike = record(3, [0.0] * 10)

    [lost] = judge(1, {1: champion, 2: mixed}, [2])
    [tied] = judge(1, {1: champion, 3: alike}, [3])

    assert lost.mean == 500
    assert lost.low == pytest.approx(500 - 2.262157 * math.sqrt(62_500_000 / 9 / 10), abs=0.01)
    assert lost.low == pytest.approx(-1_385.13, abs=0.01)
    assert (lost.why, tied.low, tied.why) == (NOT_ABOVE, 0.0, NOT_ABOVE)


def test_a_challenger_passing_alone_fails_once_bonferroni_counts_three() -> None:
    """One challenger: the lower bound at 95% is 1,100 − 2.262157 × 458.2576 = 63.35 > 0. Three
    challengers running: at 1 − 0.05 ÷ 3 (98.33%), 1,100 − 2.933324 × 458.2576 = −244.22."""
    champion = record(1, [0.0] * 10, champion=True)  # no trade: 0 a day
    kept = {1: champion, 2: record(2, CLOSE_CALL), 3: record(3, [0.0] * 3), 4: record(4, [])}

    [alone] = judge(1, kept, [2])
    corrected, *_ = judge(1, kept, [2, 3, 4])

    assert alone.mean == pytest.approx(1_100) and corrected.mean == pytest.approx(1_100)
    assert (alone.level, alone.passed) == (0.95, True)
    assert alone.low == pytest.approx(63.35, abs=0.01)
    assert corrected.level == pytest.approx(1 - 0.05 / 3) == bonferroni(3)
    assert corrected.low == pytest.approx(1_100 - 2.933324 * 458.2576, abs=0.01)
    assert corrected.low == pytest.approx(-244.22, abs=0.01)
    assert (corrected.passed, corrected.why) == (False, NOT_ABOVE)


def test_a_day_the_champion_was_stopped_is_not_a_common_day() -> None:
    champion = record(1, [0.0] * 10, champion=True, stopped=[WEEKDAYS[3]])
    [judged] = judge(1, {1: champion, 2: record(2, [1_000.0] * 10)}, [2])

    assert (judged.days, judged.why) == (9, FEW_DAYS)


def test_the_live_gate_worked_by_hand_with_the_days_without_a_trade_at_zero() -> None:
    """Twenty days: twelve of +2,000, four of −1,000 and four without a trade (0). The mean is
    20,000 ÷ 20 = 1,000 (not 20,000 ÷ 16 = 1,250: the four count); deviations 1,000 (×12), −2,000
    (×4), −1,000 (×4), their squares 32,000,000, the variance 32,000,000 ÷ 19, the standard error
    √(32,000,000 ÷ 19 ÷ 20) = 290.191; the lower bound 1,000 − 2.093024 × 290.191 = 392.62 > 0."""
    nets = [2_000.0] * 12 + [-1_000.0] * 4 + [0.0] * 4

    passed = gate(7, record(7, nets, champion=True))
    short = gate(7, record(7, nets[:19], champion=True))
    losing = gate(7, record(7, [2_000.0] * 4 + [-1_000.0] * 16, champion=True))

    assert (passed.days, passed.mean) == (20, 1_000)
    assert passed.low == pytest.approx(1_000 - 2.093024 * math.sqrt(32_000_000 / 19 / 20), abs=0.01)
    assert passed.low == pytest.approx(392.62, abs=0.01)
    assert passed.passed
    assert (short.days, short.passed) == (19, False), "a positive bound, but not twenty days"
    assert (losing.days, losing.mean, losing.passed) == (20, -400, False)
    assert gate(None, None).as_json() == {
        "policy": None,
        "days": 0,
        "required": 20,
        "mean": None,
        "low": None,
        "level": 0.95,
        "passed": False,
    }


# The records as the trader keeps them.


COLUMNS = (
    *("mode", "symbol", "kind", "predictor", "prediction_id", "bar_close", "probability", "threshold"),
    *("anchor", "tick", "width_ticks", "target", "stop", "time_barrier", "decided_at", "entry_at"),
    *("entry_price", "quantity", "buy_fee", "exit_at", "exit_price", "exit_reason", "sell_fee", "tax"),
    *("cost", "gross", "net", "policy_id", "champion"),
)


def ran(state: Path, version: int, nets: Sequence[float], champion: bool = False) -> None:
    """``version`` ran on the first weekdays, one each of ``nets``: a trade of that net P&L on the
    days it is not 0, none on the others (as the trader writes them: a day's row and its trades)."""
    store = TraderStore(state / DB_NAME)
    try:
        for day, net in zip(WEEKDAYS, nets, strict=False):
            store.put_policy_day("paper", version, day, champion, False, at(f"{day} 10:00:00"))
            if net:
                entry = datetime.combine(day, time(10), KST)
                with store._db:
                    store._db.execute(
                        f"INSERT INTO trades ({', '.join(COLUMNS)}) VALUES ({', '.join('?' * len(COLUMNS))})",
                        (
                            *("paper", "005930", "barrier", "breiman", 1, entry.isoformat(), 0.7, 0.6),
                            *(70_050, 100, 2, 70_250, 69_850, (entry + timedelta(minutes=4)).isoformat()),
                            *(
                                entry.isoformat(),
                                (entry + timedelta(seconds=2)).isoformat(),
                                70_100,
                                10,
                                105.15,
                            ),
                            *(
                                (entry + timedelta(minutes=2)).isoformat(),
                                70_250,
                                "take_profit",
                                105.375,
                                1405,
                            ),
                            *(1615.525, net + 1615.525, net, version, int(champion)),
                        ),
                    )
    finally:
        store.close()


def setup(state: Path, *challengers: Sequence[float]) -> None:
    """Policy 1 the champion, without a trade on the first ten weekdays; a challenger for each of
    ``challengers``, its nets on those days."""
    keep(state, at("2026-08-31 22:31:00"), policy_cell(BARRIER, 0.8))
    for n, _ in enumerate(challengers):
        keep(state, at("2026-08-31 22:32:00"), policy_cell(BARRIER, 0.6 - n / 100))
    ran(state, 1, [0.0] * 10, champion=True)
    for version, nets in enumerate(challengers, start=2):
        ran(state, version, nets)


def judged_at(state: Path, now: datetime) -> promotion.Promotion:
    db = policies.connect(state / DB_NAME)
    try:
        return promote(db, now, CALENDAR.session)
    finally:
        db.close()


def test_the_records_hold_a_day_without_a_trade_as_zero(tmp_path: Path) -> None:
    nets = [2_000.0] * 12 + [-1_000.0] * 4 + [0.0] * 4
    keep(tmp_path, at("2026-08-31 22:31:00"), policy_cell(BARRIER, 0.8))
    ran(tmp_path, 1, nets, champion=True)

    done = judged_at(tmp_path, at("2026-10-31 10:00:00"))  # a Saturday

    assert (done.gate.policy, done.gate.days, done.gate.mean) == (1, 20, 1_000)
    assert done.gate.low == pytest.approx(392.62, abs=0.01) and done.gate.passed


def test_the_champion_never_changes_during_the_market(tmp_path: Path) -> None:
    """Policy 2 beats the champion on ten days (+1,000 each). Judged at 10:00 of a trading day, or
    at 08:30, when the trader reads the day's champion, nothing is judged or written; at the end of
    the closing auction (15:30) it is promoted. The trader running that day keeps policy 1 until its
    next pick, from which the champion's book trades under policy 2."""
    setup(tmp_path, [1_000.0] * 10)

    for moment in ("08:30:00", "10:00:00", "15:29:59"):
        done = judged_at(tmp_path, at(moment))
        assert (done.deferred, done.judgments, done.promoted) == (MARKET_OPEN, (), None), moment
        assert champion_now(tmp_path) == 1
    day = Day(tmp_path, desk=True)
    [traded] = day.run(
        [
            Read("10:00:02.3", [Barrier("10:00:00", p=(0.85, 0.1, 0.05))]),
            quote("10:00:02.5", 70_000, 70_100),
            quote("10:02:00", 70_300, 70_400),
        ]
    )
    assert traded["policy"] == 1

    done = judged_at(tmp_path, at("15:30:00"))

    assert (done.deferred, done.promoted, champion_now(tmp_path)) == (None, 2, 2)
    after = Day(tmp_path, desk=True).run(
        [
            Repick("2026-10-15 08:30:00"),
            Read("2026-10-15 10:00:02.3", [Barrier("2026-10-15 10:00:00")]),
            quote("2026-10-15 10:00:02.5", 70_000, 70_100),
        ]
    )
    assert [(t["bar_close"][:10], t["policy"], t["champion"]) for t in after] == [
        ("2026-10-15", 2, True),
        ("2026-10-14", 1, True),
    ]


def champion_now(state: Path) -> int | None:
    with closing(sqlite3.connect(state / DB_NAME)) as db:
        return policies.champion_version(db)


def test_a_holiday_or_a_night_judges_and_keeps_the_promotion_with_its_figures(tmp_path: Path) -> None:
    setup(tmp_path, CLOSE_CALL)

    done = judged_at(tmp_path, at("2026-10-31 10:00:00"))  # a Saturday

    assert (done.promoted, [j.passed for j in done.judgments]) == (2, [True])
    found = policies.read_policies(tmp_path / DB_NAME, 10)
    assert [(p["version"], p["status"]) for p in found["policies"]] == [(2, "champion"), (1, "former")]
    change = found["champions"][0]
    assert change["version"] == 2
    assert change["reason"].startswith(
        "도전 정책 2이 챔피언 정책 1을 모의로 이겼다(#400): 함께 돈 거래일 10일"
    )
    assert change["detail"] == {
        "from": 1,
        "days": 10,
        "required": 10,
        "mean": pytest.approx(1_100),
        "low": pytest.approx(63.35, abs=0.01),
        "level": 0.95,
        "challengers": 1,
    }
    assert found["policies"][0]["paper"]["days"] == 10
    with closing(sqlite3.connect(tmp_path / DB_NAME)) as db:
        assert policies.challenger_versions(db) == [], "the former champion runs no more"


def test_of_several_passing_the_greatest_lower_bound_is_promoted(tmp_path: Path) -> None:
    setup(tmp_path, [1_000.0] * 10, [3_000.0] * 9 + [2_000.0], CLOSE_CALL)

    done = judged_at(tmp_path, at("22:31:00"))

    assert [(j.challenger, j.passed) for j in done.judgments] == [(2, True), (3, True), (4, False)]
    assert done.judgments[0].level == pytest.approx(1 - 0.05 / 3)
    assert done.promoted == 3


def test_three_slots_the_newest_waiting_first_and_a_dropped_one_never_back(tmp_path: Path) -> None:
    """Policies 2, 3 and 4 take the slots; 5 and 6 wait. Policy 4 promoted, the newest waiting (6)
    takes its slot; policy 2 dropped, 5 takes its. A dropped one never runs again."""
    for night in range(6):
        keep(tmp_path, at("22:31:00") + timedelta(days=night), policy_cell(BARRIER, 0.4 + night / 100))

    with closing(sqlite3.connect(tmp_path / DB_NAME)) as db:
        assert (policies.challenger_versions(db), policies.waiting_versions(db)) == ([2, 3, 4], [6, 5])
        assert policies.crown(db, at("2026-10-20 15:31:00"), 4, "test", {})
        assert policies.challenger_versions(db) == [2, 3]
        assert policies.admit(db, at("2026-10-20 22:40:00")) == [6]
        policies.drop(db, at("2026-10-21 22:40:00"), [2])
        assert policies.admit(db, at("2026-10-21 22:41:00")) == [5]
        assert (policies.challenger_versions(db), policies.waiting_versions(db)) == ([3, 5, 6], [])
        assert policies.admit(db, at("2026-10-22 22:41:00")) == [], "no slot free"
    found = {
        p["version"]: (p["status"], p["admitted_at"], p["dropped_at"])
        for p in policies.read_policies(tmp_path / DB_NAME, 10)["policies"]
    }
    assert found == {
        6: ("challenger", "2026-10-20T22:40:00+09:00", None),
        5: ("challenger", "2026-10-21T22:41:00+09:00", None),
        4: ("champion", "2026-10-17T22:31:00+09:00", None),
        3: ("challenger", "2026-10-16T22:31:00+09:00", None),
        2: ("dropped", "2026-10-15T22:31:00+09:00", "2026-10-21T22:40:00+09:00"),
        1: ("former", None, None),
    }


def day_ran(state: Path, version: int, day: date, net: float, champion: bool) -> None:
    """``version`` ran on ``day`` with a trade of ``net`` (none when 0)."""
    store = TraderStore(state / DB_NAME)
    try:
        store.put_policy_day("paper", version, day, champion, False, datetime.combine(day, time(10), KST))
    finally:
        store.close()
    if net:
        ran_trade(state, version, day, net, champion)


def ran_trade(state: Path, version: int, day: date, net: float, champion: bool) -> None:
    store = TraderStore(state / DB_NAME)
    try:
        entry = datetime.combine(day, time(10), KST)
        with store._db:
            store._db.execute(
                f"INSERT INTO trades ({', '.join(COLUMNS)}) VALUES ({', '.join('?' * len(COLUMNS))})",
                (
                    *("paper", "005930", "barrier", "breiman", 1, entry.isoformat(), 0.7, 0.6),
                    *(70_050, 100, 2, 70_250, 69_850, (entry + timedelta(minutes=4)).isoformat()),
                    *(entry.isoformat(), (entry + timedelta(seconds=2)).isoformat(), 70_100, 10, 105.15),
                    *((entry + timedelta(minutes=2)).isoformat(), 70_250, "take_profit", 105.375, 1405),
                    *(1615.525, net + 1615.525, net, version, int(champion)),
                ),
            )
    finally:
        store.close()


def test_a_challenger_reaches_its_tenth_day_and_is_promoted_while_a_fit_comes_every_night(
    tmp_path: Path,
) -> None:
    """Policy 1 the champion (no trade), policy 2 a challenger making 1,000 won a day. Every night a
    new fit comes: 3 and 4 take the last slots, the later ones wait. Policy 2 is judged from its
    tenth day and promoted that night; that night's fit takes its slot."""
    keep(tmp_path, at("2026-08-31 22:31:00"), policy_cell(BARRIER, 0.8))
    keep(tmp_path, at("2026-08-31 22:32:00"), policy_cell(BARRIER, 0.6))
    promoted_on: list[tuple[int, int | None]] = []
    for n, day in enumerate(WEEKDAYS[:10]):
        db = policies.connect(tmp_path / DB_NAME)
        try:
            champion, running = policies.champion_version(db), policies.running_versions(db, day)
        finally:
            db.close()
        assert champion is not None
        day_ran(tmp_path, champion, day, 0.0, champion=True)
        for version in running:
            day_ran(tmp_path, version, day, 1_000.0 if version == 2 else 0.0, champion=False)
        night = datetime.combine(day, time(22, 31), KST)
        done = judged_at(tmp_path, night)
        promoted_on.append((n + 1, done.promoted))
        keep(tmp_path, night + timedelta(minutes=1), policy_cell(BARRIER, 0.5 - n / 100))

    assert promoted_on == [(n, None) for n in range(1, 10)] + [(10, 2)]
    with closing(sqlite3.connect(tmp_path / DB_NAME)) as db:
        assert policies.champion_version(db) == 2
        assert policies.challenger_versions(db) == [3, 4, 12], "tonight's fit takes the freed slot"
        assert policies.waiting_versions(db) == [11, 10, 9, 8, 7, 6, 5]


def test_a_challenger_not_promoted_by_its_twentieth_day_is_dropped_and_the_newest_waiting_enters(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Policy 2 no better than the champion for twenty common days: dropped; 3 and 4, five days
    each, stay. At the night's end the newest waiting policy (6) takes the slot, not 5."""
    keep(tmp_path, at("2026-08-31 22:31:00"), policy_cell(BARRIER, 0.8))
    for n in range(5):
        keep(tmp_path, at("2026-08-31 22:32:00"), policy_cell(BARRIER, 0.6 - n / 100))
    ran(tmp_path, 1, [0.0] * 20, champion=True)
    ran(tmp_path, 2, [0.0] * 20)
    ran(tmp_path, 3, [0.0] * 5)
    ran(tmp_path, 4, [0.0] * 5)

    done = judged_at(tmp_path, at("22:31:00"))
    admit_now(tmp_path / DB_NAME, at("22:35:00"))

    assert [(j.challenger, j.days, j.why, j.retires) for j in done.judgments] == [
        (2, 20, NOT_ABOVE, True),
        (3, 5, FEW_DAYS, False),
        (4, 5, FEW_DAYS, False),
    ]
    assert (done.promoted, done.dropped) == (None, (2,))
    with closing(sqlite3.connect(tmp_path / DB_NAME)) as db:
        assert (policies.challenger_versions(db), policies.waiting_versions(db)) == ([3, 4, 6], [5])
    assert "빈 자리에 들어간 정책(다음 거래일부터 돈다): 정책 6" in capsys.readouterr().out
    assert "내린 도전 정책(20일 동안 이기지 못함, 자리를 비운다): 정책 2" in promotion.report(done)


def test_a_challenger_runs_on_the_days_after_the_pick_it_was_kept_before(tmp_path: Path) -> None:
    keep(tmp_path, at("2026-10-13 22:31:00"), policy_cell(BARRIER))  # the champion
    keep(tmp_path, at("2026-10-13 22:31:00"), policy_cell(BARRIER))
    keep(tmp_path, at("08:29:59"), policy_cell(BARRIER))
    keep(tmp_path, at("08:30:00"), policy_cell(BARRIER))

    with closing(sqlite3.connect(tmp_path / DB_NAME)) as db:
        assert policies.running_versions(db, date(2026, 10, 14)) == [2, 3]
        assert policies.running_versions(db, date(2026, 10, 15)) == [2, 3, 4]


def test_the_night_s_report_has_no_price(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    setup(tmp_path, CLOSE_CALL, [0.0] * 3, [])

    promote_now(tmp_path / DB_NAME, CALENDAR.session, at("22:31:00"))

    out = capsys.readouterr().out
    assert out.splitlines()[:6] == [
        "도전 정책 판정 (#400)",
        "챔피언 정책 1 · 도전 정책 3개 · 하한의 신뢰수준 98.33%(Bonferroni) · 함께 돈 거래일 10일부터 판정, "
        "20일까지 못 이기면 내린다",
        "- 정책 2: 함께 돈 거래일 10일, 날마다 (도전 − 챔피언) 순손익 평균 +1,100원, 하한 -244원 → 하한 ≤ 0",
        "- 정책 3: 함께 돈 거래일 3일, 날마다 (도전 − 챔피언) 순손익 평균 +0원, 하한 +0원 → 날 수 부족",
        "- 정책 4: 함께 돈 거래일 0일, 날마다 (도전 − 챔피언) 순손익 평균 없음, 하한 없음 → 날 수 부족",
        "→ 챔피언은 그대로다.",
    ]
    assert (
        "실전 관문: 챔피언 정책 1의 모의 기록 10/20거래일, 날마다 순손익 평균 +0원, 하한 +0원 → 아직" in out
    )
    assert not re.search(r"70[,.]?[0-9]{3}", out), "no price"
