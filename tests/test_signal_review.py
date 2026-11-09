"""The signal-threshold review (#206): what choosing the Telegram signal settings needs.

Input: a store holding hand-made predictions, touch answers and final consensus (the grid
worked out by hand below), or what an engine on a fake clock stored over a morning with
signals on. Output: the review's text, and the signals each candidate rule gives, set
against those the engine sent and blocked by that rule.
"""

from __future__ import annotations

import itertools
import json
import logging
import re
from collections.abc import Sequence
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from krx_quant.algorithms import Model
from krx_quant.baselines import Forecast
from krx_quant.clock import FakeClock
from krx_quant.config import Settings
from krx_quant.consensus import EQUAL_VERSION, FINAL, pool
from krx_quant.engine import Event, PredictionEngine
from krx_quant.features import FeatureVector
from krx_quant.lifecycle import Status
from krx_quant.predictions import NewPrediction, PredictionStore, Score, read_only
from krx_quant.pricing import Direction
from krx_quant.replay import replay_into
from krx_quant.signal_review import (
    Band,
    BarClose,
    Comparison,
    _skill,
    day_ratio,
    day_ratio_difference,
    days_from,
    fixed_days,
    read,
    rebuilt_closes,
    replay,
    signal_review,
    stored_closes,
    verdict,
)
from krx_quant.signals import BLOCKED, SENT, SignalRule, Signals
from krx_quant.stats import Scored
from krx_quant.touch import TARGET_VERSION
from tests.algorithm_doubles import FixedAlgorithm
from tests.api_doubles import touch
from tests.replay_doubles import busy_morning
from tests.replay_events import CALENDAR, FEE, SYMBOL, at, vi

UP, FLAT, DOWN = Direction.UP, Direction.FLAT, Direction.DOWN
EARLIER, DAY = date(2026, 10, 12), date(2026, 10, 13)
SETTINGS = Settings("key", "secret", signal_thresholds=SignalRule(0.6, 2), target_version=TARGET_VERSION)


def on(day: date, hhmm: str) -> datetime:
    return datetime.combine(day, at(f"{hhmm}:00").timetz())


def f(p_up: float, p_flat: float, p_down: float) -> Forecast:
    return Forecast(p_up, p_flat, p_down, 0.0)


# Each bar close by hand: newton's and gauss's forecasts, the final consensus (their equal-weight
# mean, as the engine pools before any fit), why it is blocked, and the touch answer.
N1, G1 = f(0.84, 0.10, 0.06), f(0.62, 0.30, 0.08)  # both up: (0.73, 0.20, 0.07)
G2 = f(0.42, 0.50, 0.08)  # with N1, one up, one flat: (0.63, 0.30, 0.07)
N3, G3 = f(0.10, 0.20, 0.70), f(0.06, 0.30, 0.64)  # both down: (0.08, 0.25, 0.67)
N4, G4 = f(0.74, 0.20, 0.06), f(0.52, 0.40, 0.08)  # both up: (0.63, 0.30, 0.07)
G5 = f(0.28, 0.40, 0.32)  # with N3, one down, one flat: (0.19, 0.30, 0.51)
# (newton, gauss, blocked, answer) at 09:01-09:06: the consensus had members on DAY.
WITH_MEMBERS = [
    (N1, G1, "special", UP),
    (N1, G2, None, UP),
    (N3, G3, None, FLAT),
    (N4, G4, None, DOWN),
    (N3, G5, None, DOWN),
    (N1, G1, None, UP),
]
# The day before, it had none (``no_members``): no algorithm was official yet.
WITHOUT_MEMBERS = [(N1, G1, UP), (N3, G3, DOWN), (N1, G2, FLAT)]


def mean(newton: Forecast, gauss: Forecast) -> Forecast:
    """The two pooled with equal weights, as the engine pools them."""
    return pool({"newton": newton, "gauss": gauss}, {"newton": 0.5, "gauss": 0.5})


def add(
    store: PredictionStore,
    algorithm: str,
    t: datetime,
    forecast: Forecast | None,
    answer: Direction,
    *,
    version: str = "v1",
    status: str = "ok",
    blocked: str | None = None,
    pooled: tuple[str, ...] | None = None,
) -> None:
    """One stored prediction at ``t``, scored on ``answer`` once its four minutes are over."""
    added = store.add_all(
        [
            NewPrediction(
                SYMBOL,
                algorithm,
                version,
                t,
                t + timedelta(seconds=1),
                forecast,
                t + timedelta(minutes=4),
                81_250,
                81_250,
                2,
                status=status,
                blocked=blocked,
                pooled=pooled,
            )
        ]
    )
    for pending in added.pending:
        store.score_all(
            [(pending.prediction_id, Score(81_250, answer, answer, None))], t + timedelta(minutes=4)
        )


def hand_made(db: Path) -> None:
    store = PredictionStore(db)
    for name in ("newton", "gauss"):
        store.add_status(name, on(EARLIER, "08:00"), None, Status.ACTIVE, "test")
    store.record_models([("newton", "v1", TARGET_VERSION), ("gauss", "v1", TARGET_VERSION)])
    for i, (n, g, answer) in enumerate(WITHOUT_MEMBERS):
        t = on(EARLIER, f"09:0{i + 1}")
        add(store, "newton", t, n, answer)
        add(store, "gauss", t, g, answer)
        add(store, FINAL, t, None, answer, version=EQUAL_VERSION, status="no_members")
        store.add_touches([touch(SYMBOL, t, answer)])
    for i, (n, g, blocked, answer) in enumerate(WITH_MEMBERS):
        t = on(DAY, f"09:0{i + 1}")
        add(store, "newton", t, n, answer)
        add(store, "gauss", t, g, answer)
        add(
            store,
            FINAL,
            t,
            mean(n, g),
            answer,
            version=EQUAL_VERSION,
            blocked=blocked,
            pooled=("newton", "gauss"),
        )
        store.add_touches([touch(SYMBOL, t, answer)])
    store.close()


def section(text: str, number: int) -> list[str]:
    [found] = [p for p in text.split("\n\n") if p.startswith(f"{number}. ")]
    return [line.strip() for line in found.splitlines()[1:]]


def cell(lines: Sequence[str], probability: float | str, agree: int) -> str:
    shown = probability if isinstance(probability, str) else f"{probability:.2f}"
    head = f"확률 ≥ {shown} · 같은 방향 ≥ {agree}:"
    [found] = [line for line in lines if line.startswith(head)]
    return found


def test_each_candidate_s_signals_and_hit_rate_as_worked_out_by_hand(tmp_path: Path) -> None:
    hand_made(tmp_path / "engine.db")

    text = signal_review(tmp_path / "engine.db", SETTINGS, DAY)

    assert text.splitlines()[0] == "신호 기준 검토 2026-10-12 – 2026-10-13 (2일, 예측이 있는 날)"
    stored = section(text, 4)
    # 09:01 is blocked (a VI): it does not hold 09:02 back. 09:02 calls up until 09:06, so 09:04
    # is not called again, 09:06 is; 09:03 calls down until 09:07, 09:05 within it.
    assert cell(stored, 0.5, 1) == (
        "확률 ≥ 0.50 · 같은 방향 ≥ 1: 신호 3번 (하루 3.0번) · 막힘 1 · 상승 2번 적중 100.0% [34.2–100.0%]"
        " (거래일 — 1일) · 하락 1번 적중 0.0% [0.0–79.3%] · 합쳐 66.7% [20.8–93.9%] · 정답 없음 0"
        " · 닿음 손익 -1.20틱/번"
    )
    # With two agreeing, 09:02 (one up, one flat) is no signal: 09:04 is the up call.
    assert cell(stored, 0.5, 2).startswith(
        "확률 ≥ 0.50 · 같은 방향 ≥ 2: 신호 2번 (하루 2.0번) · 막힘 1 · 상승 1번 적중 0.0% [0.0–79.3%]"
        " (거래일 — 1일) · 하락 1번 적중 0.0% [0.0–79.3%] · 합쳐 0.0% [0.0–65.8%]"
    )
    assert cell(stored, 0.6, 2).endswith("(지금 설정)")
    # At 65%, 09:02 and 09:04 (63%) are not sure enough: 09:03 down, 09:06 up.
    assert " 신호 2번 (하루 2.0번) · 막힘 1 · 상승 1번 적중 100.0%" in cell(stored, 0.65, 1)
    assert " 신호 1번 (하루 1.0번) · 막힘 1 · 상승 1번 적중 100.0%" in cell(stored, 0.7, 2)
    assert " 신호 0번 (하루 0.0번) · 막힘 0 · 상승 0번 적중 — · 하락 0번 적중 — · 합쳐 —" in cell(
        stored, 0.8, 1
    )
    assert len([line for line in stored if line.startswith("확률")]) == 7 * 2  # 50-80% × 1-2 agreeing
    assert stored[0].startswith("최종 합의 6개(종목 × 봉 마감) · 2026-10-13 – 2026-10-13 1일 · 구성원 2–2개")
    assert stored[1] == "같은 봉 마감의 도달 정답: 상승 50.0% · 보합 16.7% · 하락 33.3% (6개)"
    assert stored[-1] == (
        "지금 설정(확률 ≥ 0.6 · 같은 방향 ≥ 2)을 다시 낸 것과 엔진이 이 규칙으로 남긴 신호: "
        "다시 낸 것 3, 남긴 것 없음(알림이 꺼져 있었거나 신호가 없었다)"
    )

    # Rebuilt with both official throughout: the day before adds 09:01 up (right) and 09:02
    # down (right); its 09:03 up is within 09:01's four minutes.
    rebuilt = section(text, 5)
    assert cell(rebuilt, 0.5, 1).startswith(
        "확률 ≥ 0.50 · 같은 방향 ≥ 1: 신호 5번 (하루 2.5번) · 막힘 1 · 상승 3번 적중 100.0% [43.9–100.0%]"
        " (거래일 [100.0–100.0%] 2일) · 하락 2번 적중 50.0% [9.5–90.5%] · 합쳐 80.0% [37.6–96.4%]"
    )
    assert rebuilt[0].startswith("최종 합의 9개(종목 × 봉 마감) · 2026-10-12 – 2026-10-13 2일")


def test_a_setting_with_more_places_has_a_row_of_its_own(tmp_path: Path) -> None:
    """Input: the setting 0.604 beside the candidate 0.60. Output: two rows, each labelled with
    its own probability; the setting's is the one marked."""
    hand_made(tmp_path / "engine.db")
    settings = replace(SETTINGS, signal_thresholds=SignalRule(0.604, 2))

    stored = section(signal_review(tmp_path / "engine.db", settings, DAY), 4)

    assert not cell(stored, 0.6, 2).endswith("(지금 설정)")
    assert cell(stored, "0.604", 2).endswith("(지금 설정)")


def test_a_final_consensus_that_pooled_a_model_of_another_target_is_left_out(tmp_path: Path) -> None:
    hand_made(tmp_path / "engine.db")
    store = PredictionStore(tmp_path / "engine.db")
    store.record_models([("newton", "old", 1)])  # trained on the target before #170
    t = on(DAY, "09:07")
    add(store, "newton", t, N1, UP, version="old")
    add(store, "gauss", t, G1, UP)
    add(store, FINAL, t, mean(N1, G1), UP, version=EQUAL_VERSION, pooled=("newton", "gauss"))
    store.close()

    stored = section(signal_review(tmp_path / "engine.db", SETTINGS, DAY), 4)

    assert stored[0] == "옛 정답 모델을 합쳤거나 구성원의 예측이 없는 최종 합의 1개(종목 × 봉 마감)는 뺐다"
    assert stored[1].startswith("최종 합의 6개(종목 × 봉 마감)")


def test_the_settings_the_leaderboard_and_the_official_algorithms_records(tmp_path: Path) -> None:
    hand_made(tmp_path / "engine.db")

    text = signal_review(tmp_path / "engine.db", replace(SETTINGS, min_sample=5), DAY)

    assert section(text, 1) == [
        "지금 설정: KRX_SIGNAL_ALERT off · KRX_SIGNAL_MIN_PROBABILITY 0.6 · KRX_SIGNAL_MIN_AGREE 2",
        "기간 중 엔진이 시작할 때의 신호 규칙: 기록 없음",
        "엔진이 남긴 신호: 보냄 0 · 막힘 0",
        "지금 정식 알고리즘(합의 구성원): newton, gauss",
    ]
    board = section(text, 2)
    # newton called up 6 times (4 right) and down 3 times (2 right), never flat.
    [newton] = [line for line in board if " newton [정식] " in line]
    assert "newton [정식] 채점 9 · 정답률 66.7% [35.4–87.9%] · 상승 예측 6 적중 66.7% [30.0–90.3%]" in newton
    assert "보합 예측 0 적중 — · 하락 예측 3 적중 66.7% [20.8–93.9%]" in newton
    # #262: its skill against the class rates, after the classes.
    assert re.search(
        r"하락 예측 3 적중 [^·]+ · 기대 정답률 \d+\.\d% · kappa [+-]\d\.\d{3} "
        r"\(거래일 (—|[+-]\d\.\d{3} \[[^]]+\] \d+일)\) · Gerrity ([+-]\d\.\d{3}|—) · ",
        newton,
    ), newton
    assert any(line.startswith("기준 baseline-flat 채점 0") for line in board)
    [since] = [line for line in section(text, 3) if line.startswith("newton:")]
    assert since.startswith("newton: 2026-10-12 08:00부터 정식 · 2일 채점 9 · 정답률 66.7%")


def test_the_skill_reads_against_the_class_rates_and_by_trading_day() -> None:
    # #262: right on every bar close of two days (an up, a flat and a down each day).
    sure = {UP: (1.0, 0.0, 0.0), FLAT: (0.0, 1.0, 0.0), DOWN: (0.0, 0.0, 1.0)}

    def scored(day: date, minute: int, d: Direction) -> Scored:
        return Scored("newton", "v1", SYMBOL, on(day, f"09:0{minute}"), *sure[d], d)

    records = [scored(day, i + 1, d) for day in (EARLIER, DAY) for i, d in enumerate((UP, FLAT, DOWN))]
    assert _skill(records) == (
        "기대 정답률 33.3% · kappa +1.000 (거래일 +1.000 [+1.000, +1.000] 2일) · Gerrity +1.000"
    )
    assert _skill(records[:3]) == "기대 정답률 33.3% · kappa +1.000 (거래일 —) · Gerrity +1.000"  # one day
    assert _skill([]) == "실력 —"


# By trading day (#206, 2026-11-03): each day a cluster


def test_a_hit_rate_by_trading_day_as_worked_out_by_hand() -> None:
    """Five days of four up signals each, hit 3, 1, 2, 4 and 0 times: 10 of 20, 0.5. Each day's
    residual h − 0.5 n is 1, −1, 0, 2, −2, their squares adding up to 10; the variance is
    5/4 × 10 / 20² = 0.03125, the standard error 0.176777, and Student's t at 4 degrees of
    freedom 2.776445: 0.5 ± 0.490811."""
    days = [date(2026, 11, 4) + timedelta(days=i) for i in range(5)]
    found = day_ratio(dict(zip(days, [(3, 4), (1, 4), (2, 4), (4, 4), (0, 4)], strict=True)))

    assert found is not None and (found.value, found.n) == (0.5, 5)
    assert found.low == pytest.approx(0.009189, abs=1e-6) and found.high == pytest.approx(0.990811, abs=1e-6)
    # The ratio of the sums, not the days' mean rate: 2 of 2 and 0 of 8 are 2 of 10.
    uneven = day_ratio({days[0]: (2, 2), days[1]: (0, 8)})
    assert uneven is not None and uneven.value == pytest.approx(0.2)
    one = day_ratio({days[0]: (2, 3)})
    assert one is not None and (one.low, one.high, one.n) == (None, None, 1)  # one day tells no spread
    assert day_ratio({}) is None and day_ratio({days[0]: (0, 0)}) is None


def test_a_difference_of_hit_rates_by_trading_day_as_worked_out_by_hand() -> None:
    """Below, 1, 1, 2, 0, 1 of four a day (0.25); above, 3, 2, 4, 3, 3 of four (0.75): +0.5.
    Each day's residuals over their totals (20 each) are 0, 0, 1, −1, 0 and 0, −1, 1, 0, 0
    twentieths; above's less below's, 0, −1, 0, 1, 0 twentieths, square to 2/400. The variance
    is 5/4 × 0.005 = 0.00625, the standard error 0.0790569: +0.5 ± 2.776445 × 0.0790569."""
    days = [date(2026, 11, 4) + timedelta(days=i) for i in range(5)]
    below = dict(zip(days, [(1, 4), (1, 4), (2, 4), (0, 4), (1, 4)], strict=True))
    above = dict(zip(days, [(3, 4), (2, 4), (4, 4), (3, 4), (3, 4)], strict=True))

    found = day_ratio_difference(below, above)

    assert found is not None and found.value == pytest.approx(0.5) and found.n == 5
    assert found.low == pytest.approx(0.280503, abs=1e-6) and found.high == pytest.approx(0.719497, abs=1e-6)
    # A day with signals on one side only is a cluster all the same.
    lone = day_ratio_difference({days[0]: (1, 2)}, {days[1]: (2, 2)})
    assert lone is not None and lone.value == pytest.approx(0.5) and lone.n == 2
    assert day_ratio_difference({}, above) is None


# Six trading days of 18 bar closes five minutes apart (no move held back as running): the final
# consensus at 0.65, 0.72 and 0.80 in turn, newton and gauss alike, both up. How many of each band's
# six a day touched up; the rest stayed flat.
BAND_DAYS = [date(2026, 11, d) for d in (4, 5, 6, 9, 10, 11)]
BAND_HITS = {0.65: (2, 4, 3, 3, 2, 4), 0.72: (4, 4, 3, 5, 4, 4), 0.8: (6, 5, 6, 5, 6, 6)}


def banded(db: Path) -> None:
    store = PredictionStore(db)
    for name in ("newton", "gauss"):
        store.add_status(name, on(BAND_DAYS[0], "08:00"), None, Status.ACTIVE, "test")
    store.record_models([("newton", "v1", TARGET_VERSION), ("gauss", "v1", TARGET_VERSION)])
    for d, day in enumerate(BAND_DAYS):
        for i in range(18):
            p = list(BAND_HITS)[i % 3]
            answer = UP if i // 3 < BAND_HITS[p][d] else FLAT
            t = on(day, "09:00") + timedelta(minutes=5 * i)
            both = f(p, 0.9 - p, 0.1)
            add(store, "newton", t, both, answer)
            add(store, "gauss", t, both, answer)
            add(store, FINAL, t, mean(both, both), answer, version=EQUAL_VERSION, pooled=("newton", "gauss"))
            store.add_touches([touch(SYMBOL, t, answer)])
    store.close()


def band_lines(lines: Sequence[str]) -> list[str]:
    start = lines.index(next(line for line in lines if line.startswith("구간 비교")))
    return list(lines[start + 1 : start + 7])


def test_bands_by_trading_day_and_the_rule_s_branch_on_a_synthetic_store(tmp_path: Path) -> None:
    """t at 5 degrees of freedom is 2.570582; each variance below is 6/5 × the days' squared
    residuals (h − p n) over the band's total squared.

    - [0.60, 0.70): 18 of 36, 0.5; residuals −1, 1, 0, 0, −1, 1: 0.5 ± 2.570582 × 0.060858.
    - ≥ 0.70 (0.72 and 0.80): 58 of 72; residuals ⅓, −⅔, −⅔, ⅓, ⅓, ⅓: ± 2.570582 × 0.017568.
    - [0.70, 0.75): 24 of 36; residuals 0, 0, −1, 1, 0, 0: ± 2.570582 × 0.043033.
    - ≥ 0.75: 34 of 36; residuals ⅓, −⅔, ⅓, −⅔, ⅓, ⅓: ± 2.570582 × 0.035136, cut at 100%.
    - Both differences, +0.305556 and +0.277778, have the variance 6/5 × 1/243: ± 0.180642. So ≥ 0.75
      beats [0.70, 0.75) by more than 0 at its lower bound (+0.097136), with six a day: the rule moves
      to 0.75.

    The touch P&L (k = 2, a tick of 100 won, a cost of 81,250 × 0.23% = 1.86875 ticks): a hit
    +1.5 ticks less it, a miss (flat) −1 less it: −0.36875 and −2.86875."""
    banded(tmp_path / "engine.db")

    settings = replace(SETTINGS, signal_thresholds=SignalRule(0.7, 2))
    text = signal_review(tmp_path / "engine.db", settings, BAND_DAYS[-1])

    rebuilt = section(text, 5)
    assert band_lines(rebuilt) == [
        "[0.60, 0.70): 상승 신호 36번 (하루 6.0번) · 채점 36번 6일 · 적중 50.0% [34.4–65.6%]"
        " · 닿음 손익 -1.62틱/번",
        "≥ 0.70: 상승 신호 72번 (하루 12.0번) · 채점 72번 6일 · 적중 80.6% [76.0–85.1%]"
        " · 닿음 손익 -0.85틱/번",
        "≥ 0.70 − [0.60, 0.70): +30.6%p [+12.5%p, +48.6%p] 6일",
        "[0.70, 0.75): 상승 신호 36번 (하루 6.0번) · 채점 36번 6일 · 적중 66.7% [55.6–77.7%]"
        " · 닿음 손익 -1.20틱/번",
        "≥ 0.75: 상승 신호 36번 (하루 6.0번) · 채점 36번 6일 · 적중 94.4% [85.4–100.0%]"
        " · 닿음 손익 -0.51틱/번",
        "≥ 0.75 − [0.70, 0.75): +27.8%p [+9.7%p, +45.8%p] 6일",
    ]
    assert band_lines(section(text, 4)) == band_lines(rebuilt)  # official throughout: the same consensus
    # The threshold rows carry the same interval by trading day for their up signals.
    assert "상승 72번 적중 80.6% [70.0–88.0%] (거래일 [76.0–85.1%] 6일)" in cell(rebuilt, 0.7, 2)
    assert cell(rebuilt, 0.7, 2).endswith("(지금 설정)")
    verdict = section(text, 6)
    assert (
        verdict[-2] == "판정: 0.75로 올린다(≥ 0.75 − [0.70, 0.75)의 하한 +9.7%p > 0 · ≥ 0.75 하루 6.0번 ≥ 5)"
    )
    assert "알림(KRX_SIGNAL_ALERT)은 판정과 상관없이 끈 채로 둔다" in verdict[-1]


def test_a_band_short_of_signals_or_days_is_not_judged(tmp_path: Path) -> None:
    """The hand-made store has a signal or two a band: each comparison is 판정 불가, with its
    counts, so neither branch is met and the rule keeps 0.70."""
    hand_made(tmp_path / "engine.db")

    text = signal_review(tmp_path / "engine.db", SETTINGS, DAY)

    lines = band_lines(section(text, 5))
    assert lines[0].startswith("[0.60, 0.70): 상승 신호 ") and lines[0].endswith(
        "판정 불가(채점 30번·5일 미만)"
    )
    assert lines[2] == "≥ 0.70 − [0.60, 0.70): 판정 불가"
    assert section(text, 6)[-2] == "판정: 0.70 유지(0.75 갈래: 판정 불가 · 0.60 갈래: 판정 불가)"


def band(low: float, high: float | None, per_day: Sequence[tuple[int, int]], days: int = 20) -> Band:
    """A band whose up signals were all scored: (hits, scored) on each of the first trading days."""
    by_day = {date(2026, 11, 4) + timedelta(days=i): c for i, c in enumerate(per_day)}
    return Band(low, high, sum(n for _, n in per_day), days, by_day, None)


def test_the_rule_s_branches_from_the_top() -> None:
    even = [(3, 6)] * 20  # 0.5 every day: no spread
    better = [(5, 6)] * 20  # 0.833
    keep_low = Comparison(band(0.6, 0.7, even), band(0.7, None, better))  # ≥ 0.70 better: no way back
    up = Comparison(band(0.7, 0.75, even), band(0.75, None, better))  # ≥ 0.75 better at its bound
    flat = Comparison(band(0.7, 0.75, even), band(0.75, None, even))
    back = Comparison(band(0.6, 0.7, better), band(0.7, None, even))  # [0.60, 0.70) not worse

    assert verdict(keep_low, up)[0] == 0.75
    assert verdict(back, up)[0] == 0.75  # both met: the first branch is taken
    assert verdict(back, flat)[0] == 0.6
    assert verdict(Comparison(band(0.6, 0.7, even), band(0.7, None, even)), flat)[0] == 0.6  # 0 is not worse
    assert verdict(keep_low, flat)[0] == 0.7
    # Five a day at least: 100 signals on 20 days is five.
    few = Comparison(band(0.7, 0.75, even), band(0.75, None, [(4, 5)] * 20))
    assert verdict(keep_low, few)[0] == 0.75
    fewer = Comparison(band(0.7, 0.75, even), band(0.75, None, [(4, 5)] * 20, days=21))
    assert verdict(keep_low, fewer)[0] == 0.7
    # A band of fewer than 30 scored signals, or on fewer than five days, is not judged.
    short = Comparison(band(0.6, 0.7, [(5, 6)] * 4), band(0.7, None, even))  # 24 signals, 4 days
    assert short.difference is None and verdict(short, flat)[0] == 0.7
    four_days = Comparison(band(0.7, 0.75, even), band(0.75, None, [(9, 10)] * 4, days=4))
    assert four_days.difference is None and verdict(keep_low, four_days)[0] == 0.7


# A fixed window (``--from``, #206): the first N days with predictions from a day


def test_a_window_from_a_day_is_its_first_days_with_predictions(tmp_path: Path) -> None:
    banded(tmp_path / "engine.db")
    hand_made(tmp_path / "engine.db")  # 2026-10-12 and 10-13, before the window

    with read_only(tmp_path / "engine.db") as conn:
        assert conn is not None
        assert days_from(conn, date(2026, 11, 5), 3) == BAND_DAYS[1:4]  # the first three: 11-05, 06, 09
        assert days_from(conn, date(2026, 11, 7), 20) == BAND_DAYS[3:]  # fewer while it fills
        assert days_from(conn, date(2026, 11, 4), 20, until=date(2026, 11, 9)) == BAND_DAYS[:4]
        assert days_from(conn, date(2026, 11, 12), 20) == []
    assert fixed_days(tmp_path / "engine.db", date(2026, 11, 5), 3) == 3
    assert fixed_days(tmp_path / "engine.db", date(2026, 10, 12), 20) == 8
    assert (
        fixed_days(tmp_path / "none.db", date(2026, 11, 4), 20) == 0 and not (tmp_path / "none.db").exists()
    )

    text = signal_review(tmp_path / "engine.db", SETTINGS, date(2026, 11, 30), 3, since=date(2026, 11, 5))
    assert (
        text.splitlines()[0] == "신호 기준 검토 2026-11-05 – 2026-11-09 (3일, 2026-11-05부터 예측이 있는 날)"
    )
    # Without it, the latest days: 11-09 to 11-11.
    latest = signal_review(tmp_path / "engine.db", SETTINGS, date(2026, 11, 30), 3)
    assert latest.splitlines()[0] == "신호 기준 검토 2026-11-09 – 2026-11-11 (3일, 예측이 있는 날)"


def test_the_window_s_count_is_zero_without_a_readable_db(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    empty = tmp_path / "empty.db"
    empty.write_bytes(b"")
    with caplog.at_level(logging.ERROR):
        assert fixed_days(empty, date(2026, 11, 4), 20) == 0  # no table: logged, not raised
    assert "signal review" in caplog.text


def test_without_a_database_only_the_settings(tmp_path: Path) -> None:
    on_ = replace(SETTINGS, signal_rule=SignalRule(0.6, 2))
    text = signal_review(tmp_path / "none.db", on_, DAY)

    assert text.splitlines()[:3] == [
        "신호 기준 검토: 데이터 없음",
        "",
        "1. 신호 설정과 엔진이 남긴 신호",
    ]
    assert "KRX_SIGNAL_ALERT on" in text and not (tmp_path / "none.db").exists()


# Against the engine


class Cycling(FixedAlgorithm):
    """Predicts ``forecasts`` in turn, one a bar close."""

    def __init__(self, name: str, feature: str, forecasts: Sequence[Forecast]) -> None:
        super().__init__(name, features=(feature,), unique=(feature,))
        self._turns = itertools.cycle(forecasts)

    def predict(self, features: FeatureVector, model: Model) -> Forecast:
        return next(self._turns)


def members() -> list[Cycling]:
    """Three members whose consensus wanders over the thresholds."""
    return [
        Cycling("m0", "velocity", [f(0.7, 0.2, 0.1), f(0.3, 0.4, 0.3), f(0.55, 0.3, 0.15), f(0.1, 0.2, 0.7)]),
        Cycling(
            "m1", "acceleration", [f(0.8, 0.1, 0.1), f(0.6, 0.3, 0.1), f(0.1, 0.3, 0.6), f(0.2, 0.2, 0.6), N4]
        ),
        Cycling("m2", "relative_volume", [f(0.3, 0.4, 0.3), f(0.75, 0.15, 0.1), f(0.05, 0.25, 0.7)]),
    ]


# A VI learned a second after the 09:05 bar close (displayed 3 seconds after): it blocks
# 09:05's call, and 09:06-09:07 are not predicted.
MORNING: list[Event] = sorted(
    [*busy_morning(end="09:30:30"), vi(at("09:05:02"), "09:05:01", "09:07:30")],
    key=lambda e: e[1]["received_at"],
)
RULES = [SignalRule(0.5, 1), SignalRule(0.5, 3), SignalRule(0.55, 2), SignalRule(0.6, 1), SignalRule(0.7, 2)]


async def morning(db: Path, rule: SignalRule) -> None:
    """The engine over the morning with ``rule``'s signals on, the members official."""
    store = PredictionStore(db)
    algorithms = members()
    for a in algorithms:
        store.add_status(a.name, at("08:00:00"), None, Status.ACTIVE, "test")
    store.mark_target(TARGET_VERSION, at("08:00:00"))
    clock = FakeClock(at("09:00:00"))
    engine = PredictionEngine(
        store,
        CALENDAR,
        (SYMBOL,),
        FEE,
        clock,
        algorithms=algorithms,
        # Models of the touch target: only those count, in the review as in the engine (#170).
        models={a.name: replace(a.default_model(), target_version=TARGET_VERSION) for a in algorithms},
        signals=Signals(rule, lambda _: None),
        target_version=TARGET_VERSION,
    )
    await replay_into(engine, clock, MORNING, until=at("09:30:30"), display_delay=timedelta(seconds=3))
    store.close()


def kept(db: Path) -> set[tuple[str, str, str, str | None]]:
    """The signals the engine sent and blocked: (symbol, bar close, move, why blocked)."""
    store = PredictionStore(db)
    try:
        runs = [*store.runs(SENT, at("00:00:00")), *store.runs(BLOCKED, at("00:00:00"))]
    finally:
        store.close()
    details = [json.loads(r.detail) for r in runs]
    return {(d["symbol"], d["bar_close"], d["direction"], d["blocked"]) for d in details}


def judged(closes: Sequence[BarClose], rule: SignalRule) -> set[tuple[str, str, str, str | None]]:
    """The signals ``rule`` judges over ``closes``, as ``kept`` reads the engine's."""
    return {
        (
            j.signal.symbol,
            j.signal.bar_close.isoformat(),
            j.signal.direction.value,
            None if j.blocked is None else j.blocked.value,
        )
        for j in replay(closes, rule)
    }


async def test_each_candidate_gives_the_signals_the_engine_sends_by_it(tmp_path: Path) -> None:
    for i, rule in enumerate(RULES):
        await morning(tmp_path / f"{i}.db", rule)
    settings = Settings("key", "secret")

    with read_only(tmp_path / "0.db") as conn:
        assert conn is not None
        data = read(conn, settings, at("00:00:00").date(), 20)
    closes, left_out = stored_closes(data.finals, data.members)
    assert left_out == 0 and len(closes) == 28  # 09:01-09:30 but 09:06-09:07, in the VI
    # The members were official all morning, before any fit: rebuilt, the consensus is the same.
    rebuilt = rebuilt_closes(data.finals, data.members, ["m0", "m1", "m2"], data.weights)

    for i, rule in enumerate(RULES):
        engine_s = kept(tmp_path / f"{i}.db")
        assert judged(closes, rule) == engine_s, rule
        assert judged(rebuilt, rule) == engine_s, rule
        assert ("005930", "2026-10-12T09:05:00+09:00", "up", "special") in engine_s  # the VI blocks it
    assert len({frozenset(kept(tmp_path / f"{i}.db")) for i in range(len(RULES))}) == len(RULES)

    # The review run by the settings in force sets them beside the signals the engine kept.
    for i, rule in enumerate(RULES):
        text = signal_review(
            tmp_path / f"{i}.db", replace(settings, signal_rule=rule, signal_thresholds=rule), DAY
        )
        assert section(text, 4)[-1].endswith(" · 같다"), rule
