"""The promotion of a challenger and the live gate (#400, #393 "정책과 자기 개선", "실전 관문"; #408:
the gate's checkpoints; #473: a cohort judged once, on its review night): worked by hand, on paper
records as the trader keeps them (``policy_days`` and its trades), and the false-pass rates of the
gate and of the promotion simulated. ``tests/test_trader_cohort.py`` has the cohorts themselves.

The t quantiles used (``stats._t_quantile``): 2.262157 at 0.975 with 9 degrees of freedom (ten
days, 95% two-sided), 2.933324 at 1 − 0.05 ÷ 6 with 9 (ten days, Bonferroni over three
challengers: 98.33% two-sided); the gate's (#408) at 1 − α_k, α_k = 0.025 × 6 ÷ (π² k²): α_1 =
0.0151982, α_2 = 0.0037995, 2.339284 at 1 − α_1 and 2.985660 at 1 − α_2 with 19 degrees of freedom
(twenty days), 2.246666 and 2.815463 with 39 (forty days). Every P&L is made up.
"""

from __future__ import annotations

import math
import random
import re
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

from krx_quant.feeds.base import KST
from krx_quant.trader import policy as policies
from krx_quant.trader import promotion, review
from krx_quant.trader.cells import Cell
from krx_quant.trader.promotion import (
    BETWEEN_REVIEWS,
    CHECKPOINT_DAYS,
    FEW_DAYS,
    MARKET_OPEN,
    NOT_ABOVE,
    PolicyDay,
    Record,
    alpha,
    bonferroni,
    champion_days,
    checkpoint_level,
    checkpoints,
    gate_of,
    judge,
    lower_bound,
    no_gate,
    passes,
    pick,
    promote,
)
from krx_quant.trader.refit import form_now, promote_now
from krx_quant.trader.store import DB_NAME, TraderStore
from tests.trader_doubles import (
    BARRIER,
    CALENDAR,
    Barrier,
    Day,
    Read,
    Repick,
    at,
    enter,
    keep,
    policy_cell,
    quote,
)

FIFTEEN = Cell("barrier_15m", "barrier-persist-15m")

# The weekdays from 09-01 (the calendar guesses they trade). The tests' policies are kept from 08-31's
# night (``keep``), so the evaluation version's first cycle (#469) starts on the first of them: its
# review day is the 20th (09-28), the next the 40th.
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


def test_a_day_the_champion_was_paused_is_not_a_common_day() -> None:
    """``stopped``: a pause (or a failed order) held the champion that day (#473: not a loss limit,
    ``desk.Desk.record``). Nine common days: fewer than ten, undecided at the review."""
    champion = record(1, [0.0] * 10, champion=True, stopped=[WEEKDAYS[3]])
    [judged] = judge(1, {1: champion, 2: record(2, [1_000.0] * 10)}, [2])

    assert (judged.days, judged.why, judged.verdict) == (9, FEW_DAYS, "undecided")


def test_only_the_days_of_the_cohort_s_cycle_are_common() -> None:
    """#473: a cohort's member is judged on its cycle's days alone (``window``): never on more."""
    champion = record(1, [0.0] * 12, champion=True)
    challenger = record(2, [1_000.0] * 12)

    [judged] = judge(1, {1: champion, 2: challenger}, [2], (WEEKDAYS[2], WEEKDAYS[11]))

    assert (judged.days, judged.low) == (10, 1_000.0)


# Twenty days: twelve of +2,000, four of −1,000 and four without a trade (0).
GOOD = [2_000.0] * 12 + [-1_000.0] * 4 + [0.0] * 4


def test_the_live_gate_worked_by_hand_with_the_days_without_a_trade_at_zero() -> None:
    """``GOOD``'s mean is 20,000 ÷ 20 = 1,000 (not 20,000 ÷ 16 = 1,250: the four count); deviations
    1,000 (×12), −2,000 (×4), −1,000 (×4), their squares 32,000,000, the variance 32,000,000 ÷ 19, the
    standard error √(32,000,000 ÷ 19 ÷ 20) = 290.191. At the first checkpoint (α_1 = 0.0151982, the
    two-sided level 0.969604) the lower bound is 1,000 − 2.339284 × 290.191 = 321.16 > 0; at the
    second (α_2), 1,000 − 2.985660 × 290.191 = 133.59."""
    error = math.sqrt(32_000_000 / 19 / 20)

    assert alpha(1) == pytest.approx(0.025 * 6 / math.pi**2) == pytest.approx(0.0151982, abs=1e-7)
    assert checkpoint_level(1) == pytest.approx(1 - 2 * 0.0151982, abs=1e-7)
    assert lower_bound(GOOD, checkpoint_level(1)) == pytest.approx(1_000 - 2.339284 * error, abs=0.01)
    assert lower_bound(GOOD, checkpoint_level(1)) == pytest.approx(321.16, abs=0.01)
    assert lower_bound(GOOD, checkpoint_level(2)) == pytest.approx(1_000 - 2.985660 * error, abs=0.01)
    assert passes(GOOD, 1) and passes(GOOD, 2)
    assert not passes(GOOD[:19], 1), "a positive bound, but not twenty days"
    assert not passes([2_000.0] * 4 + [-1_000.0] * 16, 1), "a mean of -400"
    assert no_gate().as_json() == {
        "policy": None,
        "days": 0,
        "required": 20,
        "mean": None,
        "low": None,
        "level": pytest.approx(checkpoint_level(1)),
        "passed": False,
        "history": 0,
        "checkpoint": {
            "k": 1,
            "day": 20,
            "every": 20,
            "alpha": pytest.approx(alpha(1)),
            "level": pytest.approx(checkpoint_level(1)),
        },
        "passed_at": None,
        "stressed": False,
        "last": None,
    }


def test_the_gate_spends_at_most_two_and_a_half_percent_over_every_checkpoint() -> None:
    """Σ α_k = 0.025 × 6 ÷ π² × Σ 1 ÷ k² = 0.025 (Σ 1 ÷ k² = π² ÷ 6): the checkpoints never spend
    more, however long the trader runs."""
    spent = [sum(alpha(k) for k in range(1, n + 1)) for n in (1, 12, 50, 10_000)]

    assert spent == sorted(spent) and all(a < 0.025 for a in spent)
    assert spent[-1] == pytest.approx(0.025, abs=1e-5)
    assert alpha(2) == pytest.approx(alpha(1) / 4)


def t4(rng: random.Random) -> float:
    """A Student t with four degrees of freedom: a normal over √(χ²₄ ÷ 4), χ²₄ = −2 ln(U₁U₂)."""
    z = rng.gauss(0.0, 1.0)
    return z / math.sqrt(-2.0 * math.log(rng.random() * rng.random()) / 4)


def false_passes(paths: int, horizon: int, seed: int, nightly: bool = False) -> float:
    """The share of ``paths`` champions without an edge (each trading day's net P&L 1,000 won × t4,
    mean 0, a fixed seed) that the gate passes within ``horizon`` days: at the checkpoints (#408), or
    looked at every night from day 20 at 95% (the gate before #408)."""
    rng = random.Random(seed)
    passed = 0
    for _ in range(paths):
        nets = [1_000.0 * t4(rng) for _ in range(horizon)]
        if nightly:
            lows = (lower_bound(nets[:d], 0.95) for d in range(20, horizon + 1))
            passed += any(low is not None and low > 0 for low in lows)
        else:
            looks = range(1, horizon // CHECKPOINT_DAYS + 1)
            passed += any(passes(nets[: k * CHECKPOINT_DAYS], k) for k in looks)
    return passed / paths


def test_without_an_edge_the_checkpoints_pass_by_chance_at_most_two_and_a_half_percent() -> None:
    """#408's decision, checked by simulation (fixed seeds, the engine's lower bound): heavy-tailed
    daily P&L (t4) and no edge. The checkpoints with α spent as 1/k² pass 1.8% of such champions by
    day 250, 1.6% by day 500 and 1.9% by day 1,000: at most 2.5% at any horizon. Looked at every night
    at 95%, as the gate was before, 13% passed by day 120 and 15% by day 250."""
    assert false_passes(4_000, 250, seed=408) <= 0.025
    assert false_passes(2_000, 500, seed=409) <= 0.025
    assert false_passes(1_000, 1_000, seed=410) <= 0.025
    assert false_passes(500, 120, seed=408, nightly=True) > 0.10, "the nightly looks it replaces"


def lifetime_false_passes(paths: int, horizon: int, seed: int) -> tuple[float, float]:
    """The shares of ``paths`` traders whose policies all have no edge (each policy's trading days'
    net P&L normal, mean 0, deviation 1,000 won, independent, a fixed seed) that the gate passes
    within ``horizon`` days, the promotion and the checkpoints run together as the night's job runs
    them (``promote``): first testing the champion on its days as the champion (``champion_days``),
    then on its whole record (the gate before #425's review). A cohort of three challengers runs
    beside the champion for each review cycle of 20 days (#473); on the cycle's last day the gate
    tests the champion of that day, then ``judge`` and ``pick`` judge the cohort once on its 20 days
    and crown one, and a new cohort of three enters. The promotion never reads the gate, so both
    gates are tested on the same traders."""
    rng = random.Random(seed)
    days = [date(2026, 1, 1) + timedelta(days=t) for t in range(horizon)]
    slots = 3
    own = whole = 0
    for _ in range(paths):
        ran: dict[int, list[PolicyDay]] = {1: []}
        champion, newest = 1, 1
        running: list[int] = []
        passed = [False, False]  # on its days as the champion, on its whole record
        for n, day in enumerate(days, 1):
            if (n - 1) % CHECKPOINT_DAYS == 0:  # a cycle's first day: a new cohort
                running = list(range(newest + 1, newest + 1 + slots))
                newest += slots
                ran |= {v: [] for v in running}
            for version in (champion, *running):
                ran[version].append(PolicyDay(day, rng.gauss(0.0, 1_000.0), 1, version == champion, False))
            if n % CHECKPOINT_DAYS == 0:
                record = Record(champion, tuple(ran[champion]))
                for i, tested in enumerate((champion_days(record), record.days)):
                    passed[i] = passed[i] or passes([d.net for d in tested], n // CHECKPOINT_DAYS)
                if all(passed):
                    break
                # The review night: the cohort on its cycle's 20 days, once.
                kept = {v: Record(v, tuple(ran[v])) for v in running}
                kept[champion] = Record(champion, tuple(ran[champion][-CHECKPOINT_DAYS:]))
                best = pick(judge(champion, kept, running))
                champion = champion if best is None else best.challenger
        own += passed[0]
        whole += passed[1]
    return own / paths, whole / paths


@pytest.mark.timeout(300)
def test_without_an_edge_promotion_and_checkpoints_together_pass_at_most_two_and_a_half_percent() -> None:
    """#425's review: a promoted champion's days as a challenger are the ones ``judge`` picked it
    on, so the gate tests only its days as the champion. A cohort of three zero-edge challengers a
    review cycle against the champion (#473), judged once on its review night, and the checkpoints,
    over a trader's year (250 trading days, 3,000 traders, a fixed seed): 2.1% pass by chance on the
    champion's own days, at most 2.5% (the sampling error ±0.25%), and 2.6% on its whole record; over
    16,000 other traders 1.8% and 2.45%. (With #400's nightly looks from the tenth common day: 1.9%
    and 2.6%, and 2.0% and 2.7%.)"""
    own, whole = lifetime_false_passes(3_000, 250, seed=425)
    assert own <= 0.025
    assert whole >= own, "the whole record holds the days the cohort's judgment picked it on"


def false_promotions(paths: int, seed: int, nightly: bool = False) -> float:
    """The share of ``paths`` review cycles in which a cohort of three zero-edge challengers (each
    day's (challenger − champion) net P&L 1,000 won × t4, mean 0, a fixed seed) has one promoted:
    judged once on its 20 days (#473), or looked at every night from the tenth common day to the
    20th (#400's rule, the Bonferroni over the three alike)."""
    rng = random.Random(seed)
    level = bonferroni(3)
    promoted = 0
    for _ in range(paths):
        diffs = [[1_000.0 * t4(rng) for _ in range(20)] for _ in range(3)]
        looks = range(10, 21) if nightly else (20,)
        lows = (lower_bound(d[:n], level) for n in looks for d in diffs)
        promoted += any(low is not None and low > 0 for low in lows)
    return promoted / paths


def test_judged_once_a_cohort_without_an_edge_is_promoted_at_most_two_and_a_half_percent() -> None:
    """#473 (spec #468 5: Bonferroni within the cohort, no repeated looks): a cohort of three
    zero-edge challengers judged once on its review night has one promoted by chance in at most
    2.5% of the cycles (each side 0.05 ÷ 6, three of them: 2.5%): 2.2% of 10,000 cycles of
    heavy-tailed t4 days (a fixed seed; 1.9–2.1% of 10,000 with three others). Looked at every night
    from the tenth day to the 20th, as #400 judged, 6.3% were."""
    assert false_promotions(10_000, seed=473) <= 0.025
    assert false_promotions(10_000, seed=473, nightly=True) > 0.05, "the repeated looks it replaces"


# The records as the trader keeps them.


COLUMNS = (
    *("mode", "symbol", "kind", "predictor", "prediction_id", "bar_close", "probability", "threshold"),
    *("anchor", "tick", "width_ticks", "target", "stop", "time_barrier", "decided_at", "entry_at"),
    *("entry_price", "quantity", "buy_fee", "exit_at", "exit_price", "exit_reason", "sell_fee", "tax"),
    *("cost", "gross", "net", "policy_id", "champion"),
)


def ran(
    state: Path, version: int, nets: Sequence[float], champion: bool = False, days: Sequence[date] = WEEKDAYS
) -> None:
    """``version`` ran on the first of ``days`` (the weekdays from 09-01), one each of ``nets``: a
    trade of that net P&L on the days it is not 0, none on the others (as the trader writes them: a
    day's row and its trades)."""
    store = TraderStore(state / DB_NAME)
    try:
        for day, net in zip(days, nets, strict=False):
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


def setup(state: Path, *challengers: Sequence[float], days: Sequence[date] = WEEKDAYS) -> None:
    """Policy 1 the champion, without a trade on the first ten of ``days`` (the weekdays from 09-01);
    a challenger for each of ``challengers``, its nets on those days, all entered the night before
    the first as the cohort of the review cycle in force then (#473, ``enter``)."""
    night = datetime.combine(days[0] - timedelta(days=1), time(22, 31), KST)
    keep(state, night, policy_cell(BARRIER, 0.8))
    versions = [
        keep(state, night + timedelta(minutes=1), policy_cell(BARRIER, 0.6 - n / 100))
        for n, _ in enumerate(challengers)
    ]
    enter(state, night + timedelta(minutes=2), *versions)
    ran(state, 1, [0.0] * 10, champion=True, days=days)
    for version, nets in enumerate(challengers, start=2):
        ran(state, version, nets, days=days)


def begun(state: Path, first: date) -> None:
    """The evaluation version (#469) begun so that its first cycle starts on ``first``: its review
    days are the 20th, 40th … weekdays from it (the calendar guesses the weekdays trade)."""
    db = policies.connect(state / DB_NAME)
    try:
        assert review.begin(db, datetime.combine(first, time(8), KST), CALENDAR.session).first_day == first
    finally:
        db.close()


def judged_at(state: Path, now: datetime) -> promotion.Promotion:
    db = policies.connect(state / DB_NAME)
    try:
        return promote(db, now, CALENDAR.session)
    finally:
        db.close()


def test_the_records_hold_a_day_without_a_trade_as_zero(tmp_path: Path) -> None:
    """The champion's twenty days are the trader's paper history's first checkpoint: judged that
    night, at α_1, on its days without a trade as 0."""
    keep(tmp_path, at("2026-08-31 22:31:00"), policy_cell(BARRIER, 0.8))
    ran(tmp_path, 1, GOOD, champion=True)

    done = judged_at(tmp_path, at("2026-10-31 10:00:00"))  # a Saturday

    [first] = done.checked
    assert (first.k, first.day, first.policy, first.days, first.mean) == (1, WEEKDAYS[19], 1, 20, 1_000)
    assert first.low == pytest.approx(321.16, abs=0.01) and first.passed
    assert (done.gate.policy, done.gate.days, done.gate.passed, done.gate.passed_at) == (1, 20, True, first)
    assert done.gate.next == 2, "the next checkpoint is the second, at the trader's fortieth day"


def test_the_champion_never_changes_during_the_market(tmp_path: Path) -> None:
    """Policy 2 beats the champion on ten days (+1,000 each) of the cycle from 09-17, whose review
    day is the test day (10-14, its 20th weekday, #469). Judged at 10:00 of it, or at 08:30, when the
    trader reads the day's champion, nothing is judged or written; at the end of the closing auction
    (15:30) it is promoted. The trader running that day keeps policy 1 until its next pick, from which
    the champion's book trades under policy 2."""
    begun(tmp_path, date(2026, 9, 17))
    setup(tmp_path, [1_000.0] * 10, days=[d for d in LONG if d >= date(2026, 9, 30)])

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
    """The review day a Friday (09-25, the 20th weekday from 08-31, #469): the Saturday after it is
    its night, and judges."""
    begun(tmp_path, date(2026, 8, 31))
    setup(tmp_path, CLOSE_CALL)

    done = judged_at(tmp_path, at("2026-09-26 10:00:00"))  # a Saturday

    assert (done.promoted, [j.passed for j in done.judgments]) == (2, [True])
    found = policies.read_policies(tmp_path / DB_NAME, 10)
    assert [(p["version"], p["status"]) for p in found["policies"]] == [(2, "champion"), (1, "former")]
    change = found["champions"][0]
    assert change["version"] == 2
    assert change["reason"].startswith(
        "도전 정책 2이 챔피언 정책 1을 모의로 이겼다(#473): 주기 1 묶음의 검토 날 2026-09-25, "
        "함께 돈 거래일 10일"
    )
    assert change["detail"] == {
        "from": 1,
        "days": 10,
        "required": 10,
        "mean": pytest.approx(1_100),
        "low": pytest.approx(63.35, abs=0.01),
        "level": 0.95,
        "challengers": 1,
        "evaluation": 2,
        "cycle": 1,
    }
    assert found["policies"][0]["paper"]["days"] == 10
    with closing(sqlite3.connect(tmp_path / DB_NAME)) as db:
        assert policies.challenger_versions(db) == [], "the former champion runs no more"


def test_of_several_passing_the_greatest_lower_bound_is_promoted(tmp_path: Path) -> None:
    setup(tmp_path, [1_000.0] * 10, [3_000.0] * 9 + [2_000.0], CLOSE_CALL)

    done = judged_at(tmp_path, night(WEEKDAYS[19]))  # the first review day (#469)

    assert [(j.challenger, j.passed) for j in done.judgments] == [(2, True), (3, True), (4, False)]
    assert done.judgments[0].level == pytest.approx(1 - 0.05 / 3)
    assert done.promoted == 3


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


def test_twenty_nights_of_fits_wait_and_the_review_night_judges_the_cohort_once(tmp_path: Path) -> None:
    """Policy 1 the champion (no trade); policies 2 and 3 enter as the cohort of the cycle from 09-01
    (#473), policy 2 making 1,000 won a day. Every night a new fit comes and waits: nothing judges,
    crowns, drops or enters during the cycle, though policy 2 beats the champion from its tenth day,
    and the champion's cells, their order and θ stay as they were (#469). The review night (its 20th
    weekday) judges the cohort once: policy 2 is crowned, policy 3 dropped; at the night's end the
    three newest fits enter as the next cycle's cohort, the older ones dropped (#473)."""
    keep(tmp_path, at("2026-08-31 22:31:00"), policy_cell(BARRIER, 0.8), policy_cell(FIFTEEN, 0.7))
    keep(tmp_path, at("2026-08-31 22:32:00"), policy_cell(BARRIER, 0.6))
    keep(tmp_path, at("2026-08-31 22:32:30"), policy_cell(BARRIER, 0.59))
    enter(tmp_path, at("2026-08-31 22:33:00"), 3, 2)
    frozen = values_now(tmp_path)
    nights: list[tuple[int, int | None, str | None]] = []
    for n, day in enumerate(WEEKDAYS[:20]):
        db = policies.connect(tmp_path / DB_NAME)
        try:
            champion, running = policies.champion_version(db), policies.running_versions(db, day)
        finally:
            db.close()
        assert (champion, running) == (1, [2, 3]), f"day {n + 1}: the cohort of the cycle"
        day_ran(tmp_path, 1, day, 0.0, champion=True)
        for version in running:
            day_ran(tmp_path, version, day, 1_000.0 if version == 2 else 0.0, champion=False)
        night = datetime.combine(day, time(22, 31), KST)
        done = judged_at(tmp_path, night)
        nights.append((n + 1, done.promoted, done.deferred))
        if done.promoted is None:
            assert values_now(tmp_path) == frozen, f"night {n + 1}: the champion's version is frozen"
            assert done.verdict is None and done.dropped == ()
        keep(tmp_path, night + timedelta(minutes=1), policy_cell(BARRIER, 0.5 - n / 100))
        form_now(tmp_path / DB_NAME, CALENDAR.session, night + timedelta(minutes=2))

    assert nights == [(n, None, BETWEEN_REVIEWS) for n in range(1, 20)] + [(20, 2, None)]
    assert done.verdict is not None and (done.verdict.outcome, done.dropped) == ("promoted", (3,))
    with closing(sqlite3.connect(tmp_path / DB_NAME)) as db:
        assert policies.champion_version(db) == 2
        assert policies.challenger_versions(db) == [21, 22, 23], "the next cycle's cohort: the newest three"
        assert policies.waiting_versions(db) == [], "the older fits are dropped: newer ones replaced them"
        assert policies.running_versions(db, WEEKDAYS[20]) == [21, 22, 23]


def values_now(state: Path) -> tuple[int | None, tuple[tuple[str, str, float], ...]]:
    """The champion's version and values (its cells by name, their order and θ)."""
    db = policies.connect(state / DB_NAME)
    try:
        found = policies.champion(db)
    finally:
        db.close()
    return (None, ()) if found is None else (found.version, policies.values(found.cells))


def test_every_member_not_crowned_is_dropped_and_one_short_of_days_is_undecided(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Policy 2 no better than the champion on twenty days (``not_above``); 3 and 4 ran five days each
    (the trader down, say): undecided (#473: 판정 불가). The review night drops all three; the night
    before judged none."""
    setup(tmp_path, [0.0] * 20, [0.0] * 5, [0.0] * 5)
    ran(tmp_path, 1, [0.0] * 20, champion=True)

    early = judged_at(tmp_path, night(WEEKDAYS[18]))  # not between reviews (#469, #473)
    done = judged_at(tmp_path, night(WEEKDAYS[19]))  # the review night

    assert (early.deferred, early.judgments, early.dropped, early.verdict) == (BETWEEN_REVIEWS, (), (), None)
    assert [(j.challenger, j.days, j.why, j.verdict) for j in done.judgments] == [
        (2, 20, NOT_ABOVE, "not_above"),
        (3, 5, FEW_DAYS, "undecided"),
        (4, 5, FEW_DAYS, "undecided"),
    ]
    assert (done.promoted, done.dropped) == (None, (2, 3, 4))
    with closing(sqlite3.connect(tmp_path / DB_NAME)) as db:
        assert policies.challenger_versions(db) == []
    text = promotion.report(done)
    assert (
        "- 정책 3: 함께 돈 거래일 5일, 날마다 (도전 − 챔피언) 순손익 평균 +0원, 하한 +0원 → 판정 불가" in text
    )
    assert "내린 도전 정책(묶음에서 뽑히지 않음, 다시 돌지 않는다): 정책 2, 정책 3, 정책 4" in text


def test_the_night_s_report_has_no_price(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    setup(tmp_path, CLOSE_CALL, [0.0] * 3, [])

    promote_now(tmp_path / DB_NAME, CALENDAR.session, night(WEEKDAYS[18]))
    between = capsys.readouterr().out
    promote_now(tmp_path / DB_NAME, CALENDAR.session, night(WEEKDAYS[19]))

    assert between.splitlines()[:2] == [
        "도전 정책 판정 (#473: 묶음을 검토 날 밤에 한 번)",
        "검토 날이 아니라 판정하지 않았다(#469): 도전 정책은 묶음으로 검토 날(20거래일마다) 밤에 한 번 "
        "판정하고, 챔피언은 그때만 바뀐다. 다음 검토 날 2026-09-28.",
    ]
    out = capsys.readouterr().out
    assert out.splitlines()[:7] == [
        "도전 정책 판정 (#473: 묶음을 검토 날 밤에 한 번)",
        "검토 날 2026-09-28(#469) · 주기 1 묶음(2026-09-01–2026-09-28) · 챔피언 정책 1 · 도전 정책 3개 · "
        "하한의 신뢰수준 98.33%(묶음 안 Bonferroni) · 함께 돈 거래일 10일 미만이면 판정 불가",
        "- 정책 2: 함께 돈 거래일 10일, 날마다 (도전 − 챔피언) 순손익 평균 +1,100원, 하한 -244원 → 하한 ≤ 0",
        "- 정책 3: 함께 돈 거래일 3일, 날마다 (도전 − 챔피언) 순손익 평균 +0원, 하한 +0원 "
        "→ 판정 불가(함께 돈 거래일 10일 미만)",
        "- 정책 4: 함께 돈 거래일 0일, 날마다 (도전 − 챔피언) 순손익 평균 없음, 하한 없음 "
        "→ 판정 불가(함께 돈 거래일 10일 미만)",
        "→ 챔피언은 그대로다.",
        "내린 도전 정책(묶음에서 뽑히지 않음, 다시 돌지 않는다): 정책 2, 정책 3, 정책 4",
    ]
    assert (
        "실전 관문: 챔피언 정책 1의 챔피언으로 돈 모의 기록 10일(이력 10거래일), 날마다 순손익 평균 +0원, "
        "다음 판정(1번째, 이력 20거래일째, α 1.520%)의 신뢰수준 96.96%로 지금 하한 +0원 → 아직" in out
    )
    assert not re.search(r"70[,.]?[0-9]{3}", out + between), "no price"


# The live gate's checkpoints on the store (#408).

# Weekdays enough for three checkpoints (the calendar guesses they trade).
LONG = [d for d in (date(2026, 9, 1) + timedelta(days=i) for i in range(100)) if d.weekday() < 5]


def night(day: date) -> datetime:
    return datetime.combine(day, time(22, 31), KST)


def test_a_checkpoint_is_judged_once_and_a_champion_that_passed_stays_passed(tmp_path: Path) -> None:
    """Policy 1 passes at the first checkpoint (``GOOD``: its lower bound 321.16 at α_1). Twenty
    losing days later the second checkpoint fails (−3,000 a day), but the gate stays passed for it. A
    trade of the first twenty days kept later changes no checkpoint: each is judged once. With one
    extra adverse tick a leg (each trade 10 shares of a 100-won tick: 1,000 won a leg, 2,000 a round
    trip) the +2,000 days make 0 and the −1,000 days −3,000: no stressed pass."""
    keep(tmp_path, at("2026-08-31 22:31:00"), policy_cell(BARRIER, 0.8))
    for day, net in zip(LONG[:20], GOOD, strict=True):
        day_ran(tmp_path, 1, day, net, champion=True)
    first = judged_at(tmp_path, night(LONG[19]))
    for day in LONG[20:40]:
        day_ran(tmp_path, 1, day, -3_000.0, champion=True)
    second = judged_at(tmp_path, night(LONG[39]))
    ran_trade(tmp_path, 1, LONG[3], -50_000.0, champion=True)  # kept after its checkpoint
    third = judged_at(tmp_path, night(LONG[40]))

    [one] = first.checked
    assert (one.k, one.policy, one.days, one.passed) == (1, 1, 20, True)
    assert one.low == pytest.approx(321.16, abs=0.01)
    assert one.stressed_low is not None and one.stressed_low < 0 and not one.stressed_passed
    assert PolicyDay(LONG[0], 2_000.0, 1, True, False, 1_000.0).stressed() == 0.0
    [two] = second.checked
    assert (two.k, two.day, two.policy, two.days, two.passed) == (2, LONG[39], 1, 40, False)
    assert two.level == pytest.approx(checkpoint_level(2))
    passed = second.gate.passed_at
    assert second.gate.passed and passed is not None and (passed.k, passed.low) == (1, one.low)
    assert third.checked == (), "nothing due: each checkpoint is judged once"
    with closing(sqlite3.connect(tmp_path / DB_NAME)) as db:
        kept = checkpoints(db)
    assert [(c.k, c.low) for c in kept] == [(1, pytest.approx(321.16, abs=0.01)), (2, two.low)]


# Policy 2's twenty days as the champion: 12 of +2,000 and 8 of −1,000. The mean is 16,000 ÷ 20 = 800;
# deviations 1,200 (×12) and −1,800 (×8), their squares 43,200,000, the variance ÷ 19, the standard
# error √(43,200,000 ÷ 19 ÷ 20) = 337.17. At α_1 its lower bound is 800 − 2.339284 × 337.17 = 11.3 > 0;
# at α_2, 800 − 2.985660 × 337.17 = −206.7.
CLOSE = ([2_000.0] * 6 + [-1_000.0] * 4) * 2
# Its twenty days as a challenger, the ones it was promoted on: 18 of +2,000 and 2 of −1,000.
WON_ON = ([2_000.0] * 9 + [-1_000.0]) * 2


def test_the_alpha_spent_counts_the_trader_s_checkpoints_across_a_change_of_the_champion(
    tmp_path: Path,
) -> None:
    """Policy 1 is the champion of the first twenty days (no trade: it fails the first checkpoint);
    policy 2, a challenger beside it, is crowned that night and is the champion of the next twenty.
    The second checkpoint judges policy 2 on its twenty days as the champion at α_2, the trader's
    second: below 0, so it does not pass, though at a fresh α_1 it would. Its days as a challenger,
    which it was promoted on, never count: with them its forty days would pass at α_2. It must pass
    at a later checkpoint."""
    keep(tmp_path, at("2026-08-31 22:31:00"), policy_cell(BARRIER, 0.8))
    enter(
        tmp_path,
        at("2026-08-31 22:33:00"),
        keep(tmp_path, at("2026-08-31 22:32:00"), policy_cell(BARRIER, 0.6)),
    )
    for day, net in zip(LONG[:20], WON_ON, strict=True):
        day_ran(tmp_path, 1, day, 0.0, champion=True)
        day_ran(tmp_path, 2, day, net, champion=False)
    first = judged_at(tmp_path, night(LONG[19]))
    assert first.promoted == 2, "it beat the champion on those days"
    for day, net in zip(LONG[20:40], CLOSE, strict=True):
        day_ran(tmp_path, 2, day, net, champion=True)
    second = judged_at(tmp_path, night(LONG[39]))

    assert [(c.k, c.policy, c.passed) for c in first.checked] == [(1, 1, False)]
    [two] = second.checked
    assert (two.k, two.policy, two.days, two.mean) == (2, 2, 20, 800)
    assert two.low == pytest.approx(800 - 2.985660 * math.sqrt(43_200_000 / 19 / 20), abs=0.01)
    assert two.low == pytest.approx(-206.7, abs=0.1) and not two.passed
    assert lower_bound(CLOSE, checkpoint_level(1)) == pytest.approx(11.3, abs=0.1)
    assert passes(CLOSE, 1), "a fresh α for the new champion would have passed it"
    assert passes(WON_ON + CLOSE, 2), "its days as a challenger would have passed it"
    with closing(sqlite3.connect(tmp_path / DB_NAME)) as db:
        gate = gate_of(db, 2)
        before = gate_of(db, 2, LONG[19], night(LONG[19]))
    assert (gate.passed, gate.history, gate.next, gate.next_day) == (False, 40, 3, 60)
    assert gate.level == pytest.approx(checkpoint_level(3))
    assert (before.days, before.history, before.next) == (0, 20, 2), "the gate as it stood that night"
