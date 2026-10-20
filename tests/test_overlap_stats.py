"""The statistics of windows of several sessions (#310): a daily kind whose window runs H
sessions opens one every trading day, so two days' windows share sessions and their gains are
not independent. The lifts by trading day take a Newey–West standard error at lag 2(H − 1),
never below the independent one, with about the independent windows' degrees of freedom; the
paired lifts and the shown intervals a Driscoll–Kraay one.

The calibration (#310, "보정이 맞는지 시험한다"): ten made-up years of three symbols whose daily
moves are independent day to day, five-session windows classed up, flat and down by a band that
leaves a third flat, the kind's three baselines, and predictions with no edge. The pre-validation
gate's rule (the weakest lift above 0 and the weakest lift by trading day's 95% lower bound above
0) is run 2,000 times: the corrected bound must pass no more than twice its nominal one-sided
2.5%, the uncorrected one (independent days, ``stats.t_test``) shows how often it would.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import date, datetime, timedelta
from statistics import NormalDist

import numpy as np
import pytest

from krx_quant.feeds import KST
from krx_quant.kind_stats import entry, entry_json, sessions_of, summarize
from krx_quant.kinds import Kind, KindScored
from krx_quant.stats import (
    day_lift,
    independent_days,
    lift,
    long_run_variance,
    overlap_day_lift,
    overlap_df,
    overlap_interval,
    overlap_lag,
    overlap_lift,
    overlap_mean,
    overlap_t_test,
    session_index,
    t_test,
    variance_ratio,
    wilson,
)
from tests.kind_doubles import drift_of

H = 5
DAY0 = date(2026, 1, 5)


def kind(overlap: int) -> Kind:
    return Kind(
        name="windows",
        title="test",
        classes=("up", "flat", "down"),
        class_titles=("a", "b", "c"),
        action=("up", "down"),
        answer_version=1,
        answer=drift_of,
        overlap=overlap,
    )


def at(day: int) -> datetime:
    return datetime.combine(DAY0 + timedelta(days=day), datetime.min.time(), KST).replace(hour=8, minute=50)


def scored(name: str, day: int, symbol: str, predicted: int, outcome: int) -> KindScored:
    p = tuple(1.0 if i == predicted else 0.0 for i in range(3))
    return KindScored(name, "v1", symbol, at(day), p, outcome, {}, {})  # type: ignore[arg-type]


def test_the_lag_degrees_of_freedom_and_independent_days_follow_the_sessions() -> None:
    assert overlap_lag(5) == 8 and overlap_lag(1) == 0
    assert independent_days(2_450, 5) == 490 and independent_days(19, 5) == 3
    assert independent_days(20, 1) == 20
    assert overlap_df(20, 5) == 3 and overlap_df(4, 5) == 1 and overlap_df(2_450, 5) == 489


def test_the_long_run_variance_pairs_days_by_their_distance_not_their_neighbours() -> None:
    # Values 1, −1, 2, 0 on sessions 0, 1, 3, 4 (session 2 blank), mean 0.5: z = .5, −1.5, 1.5, −.5.
    # γ0 = 5/4 ÷ … = 1.25; γ1 pairs (0,1) and (3,4): (−.75 − .75) ÷ 4 = −.375; γ2 pairs (1,3):
    # −2.25 ÷ 4 = −.5625 (and (0,2), (2,4) with the blank: 0). Lag 2: 1.25 + 2·(2/3)(−.375) +
    # 2·(1/3)(−.5625) = 1.25 − .5 − .375 = .375.
    gamma0, total = long_run_variance([1, -1, 2, 0], [0, 1, 3, 4], lag=2)
    assert gamma0 == pytest.approx(1.25)
    assert total == pytest.approx(0.375)
    # Taking the list's neighbours as lag 1 would give another value.
    assert long_run_variance([1, -1, 2, 0], [0, 1, 2, 3], lag=2)[1] != pytest.approx(0.375)
    with pytest.raises(ValueError):
        long_run_variance([1, 2], [3, 3], lag=2)


def test_a_positive_autocorrelation_widens_the_interval_and_a_negative_one_cannot_narrow_it() -> None:
    smooth = [math.sin(i / 6) + 0.1 for i in range(200)]  # neighbours alike: long-run variance up
    places = list(range(200))
    independent = t_test(smooth)
    corrected = overlap_t_test(smooth, places, H)
    assert independent is not None and corrected is not None
    assert corrected.value == pytest.approx(independent.value)
    assert corrected.low is not None and independent.low is not None
    assert corrected.low < independent.low
    ratio = variance_ratio(smooth, places, H)
    assert ratio is not None and ratio > 3
    alternating = [(-1.0) ** i + 0.05 for i in range(200)]  # neighbours opposite: long-run variance down
    floor = overlap_t_test(alternating, places, H)
    plain = t_test(alternating)
    assert floor is not None and plain is not None
    assert floor.value is not None and floor.low is not None
    assert plain.value is not None and plain.low is not None
    df = overlap_df(200, H)
    half = (floor.value - floor.low) / _t(0.975, df)
    assert half == pytest.approx((plain.value - plain.low) / _t(0.975, 199))  # the independent error
    ratio = variance_ratio(alternating, places, H)
    assert ratio is not None and ratio < 1


def _t(q: float, df: int) -> float:
    from krx_quant.stats import _t_quantile

    return _t_quantile(q, df)


def test_one_session_is_the_independent_statistics() -> None:
    values = [0.1, -0.2, 0.3, 0.05, 0.0, 0.2]
    assert overlap_t_test(values, list(range(6)), 1) == t_test(values)
    one, alg, base = kind(1), [], []
    for d in range(12):
        for s, symbol in enumerate(("005930", "000660", "009150")):
            outcome = (d + s) % 3
            alg.append(scored("alg", d, symbol, (d * 7 + s) % 3, outcome))
            base.append(scored("flat", d, symbol, 1, outcome))
    e = entry("alg", alg, {"flat": base}, one)
    assert e.lifts["flat"].lift == lift(alg, base)
    assert e.lifts["flat"].by_day == day_lift(alg, base)
    assert e.summary.accuracy == wilson(sum(r.correct for r in alg), len(alg))
    shown = entry_json(e)
    assert shown["overlap_sessions"] == 1 and shown["independent_days"] == 12


def test_the_driscoll_kraay_mean_sums_each_day_before_the_long_run_variance() -> None:
    # Two days on sessions 0 and 1: (1, 0) and (1, 1); mean 0.75. Day sums of (v − .75): −.5 and .5.
    # Their mean 0: γ0 = (.25 + .25) ÷ 2 = .25, γ1 = −.25 ÷ 2 = −.125; lag 8: .25 + 2·(8/9)(−.125)
    # < 0, floored at 0, so the independent error stands: √(.25 ÷ 3) ÷ 2 (sample sd √(1/3)·… ).
    by_day = {DAY0: [1.0, 0.0], DAY0 + timedelta(days=1): [1.0, 1.0]}
    found = overlap_mean(by_day, session_index(by_day), H)
    assert found is not None
    assert found.mean == pytest.approx(0.75) and found.n == 4 and found.days == 2
    independent = math.sqrt(sum((v - 0.75) ** 2 for v in (1, 0, 1, 1)) / 3) / 2
    assert found.error == pytest.approx(independent)
    # Correlated days (each day's values alike, the next day's too): Driscoll–Kraay above it.
    days = {DAY0 + timedelta(days=i): [float(i // 10 % 2)] * 3 for i in range(60)}
    found = overlap_mean(days, session_index(days), H)
    assert found is not None
    flat = [v for vs in days.values() for v in vs]
    mean = sum(flat) / len(flat)
    spread = math.sqrt(sum((v - mean) ** 2 for v in flat) / (len(flat) - 1))
    assert found.error > spread / math.sqrt(len(flat))
    shown = overlap_interval(days, session_index(days), H, ratio=True)
    assert shown is not None and shown.low is not None and shown.high is not None
    assert 0.0 <= shown.low < mean < shown.high <= 1.0 and shown.n == 180


def test_an_overlapping_kind_s_entry_takes_its_days_among_every_predictor_s() -> None:
    windows, alg, base = kind(H), [], []
    for d in range(40):
        for s, symbol in enumerate(("005930", "000660", "009150")):
            outcome = (d // 3 + s) % 3
            base.append(scored("flat", d, symbol, 1, outcome))
            if d % 7 != 3:  # the algorithm missed some days: blanks among the baseline's
                alg.append(scored("alg", d, symbol, (d // 4 + s) % 3, outcome))
    sessions = sessions_of(alg, {"flat": base})
    assert len(sessions) == 40
    e = entry("alg", alg, {"flat": base}, windows)
    assert e.lifts["flat"].by_day == overlap_day_lift(alg, base, sessions, H)
    assert e.lifts["flat"].lift == overlap_lift(alg, base, sessions, H)
    by_alg_days_only = overlap_day_lift(alg, base, session_index(r.bar_close.date() for r in alg), H)
    assert e.lifts["flat"].by_day != by_alg_days_only  # the missed days are blanks, not skipped
    summary = summarize(alg, windows, {"flat": base}, "alg")
    assert summary.overlap == H and summary.days == 40 - 6
    shown = entry_json(e)
    assert shown["overlap_sessions"] == H and shown["independent_days"] == (40 - 6) // H
    assert shown["accuracy"]["n"] == len(alg)


# The calibration (module doc).
YEARS_DAYS = 2_450  # trading days in ten years
FIRST = 750  # the walk-forward's first predicted day (``daily_training.WALK_MIN_TRAIN``)
SYMBOLS = 3
CLIMATE = 250  # otc5-climate's windows
RUNS = 2_000
BAND = NormalDist().inv_cdf(2 / 3) * math.sqrt(H)  # a third of the windows flat, in daily σ
UP, FLAT, DOWN = 0, 1, 2
Predict = Callable[[np.random.Generator, int], np.ndarray]


def _independent(rng: np.random.Generator, n: int) -> np.ndarray:
    """A class at random every day and symbol."""
    return rng.integers(0, 3, (n, SYMBOLS))


def _sticky(rng: np.random.Generator, n: int) -> np.ndarray:
    """A class at random that stays a while (a new draw on a tenth of the days): a model whose
    inputs move slowly. Its right and wrong days come in runs, as the windows' classes do."""
    draws = rng.integers(0, 3, (n, SYMBOLS))
    switch = rng.random((n, SYMBOLS)) < 0.1
    switch[0] = True
    last = np.maximum.accumulate(np.where(switch, np.arange(n)[:, None], 0), axis=0)
    out: np.ndarray = np.take_along_axis(draws, last, axis=0)
    return out


def _gate(rng: np.random.Generator, predict: Predict) -> dict[str, tuple[float, float, float, float]]:
    """One made-up ten years: each baseline's (paired lift, corrected 95% lower bound by trading
    day, uncorrected one, variance ratio) of a prediction with no edge."""
    market = rng.standard_normal(YEARS_DAYS)
    moves = math.sqrt(0.5) * market[:, None] + math.sqrt(0.5) * rng.standard_normal((YEARS_DAYS, SYMBOLS))
    summed = np.vstack([np.zeros(SYMBOLS), np.cumsum(moves, axis=0)])
    n = YEARS_DAYS - H + 1  # windows that ended
    change = summed[H : H + n] - summed[:n]
    answer = np.where(change > BAND, UP, np.where(change < -BAND, DOWN, FLAT))
    right = (predict(rng, n) == answer).astype(float)
    flat = (answer == FLAT).astype(float)
    persist = np.zeros_like(flat)  # the class of the window that ended the day before
    persist[H:] = answer[H:] == answer[:-H]
    counts = np.vstack([np.zeros((1, SYMBOLS, 3)), np.cumsum(np.eye(3)[answer], axis=0)])
    ended = np.maximum(np.arange(n) - H + 1, 0)  # windows begun by day − 5 have ended by 08:50
    shares = counts[ended] - counts[np.maximum(ended - CLIMATE, 0)] + 1
    most = shares.max(axis=2)
    climate_call = np.where(shares[:, :, FLAT] == most, FLAT, np.where(shares[:, :, UP] == most, UP, DOWN))
    climate = (climate_call == answer).astype(float)
    out = {}
    places = list(range(n - FIRST))
    for name, baseline in (("otc5-flat", flat), ("otc5-persist", persist), ("otc5-climate", climate)):
        gains = (right - baseline)[FIRST:]
        days = gains.mean(axis=1).tolist()
        corrected, independent = overlap_t_test(days, places, H), t_test(days)
        assert corrected is not None and independent is not None
        assert corrected.low is not None and independent.low is not None
        ratio = variance_ratio(days, places, H)
        out[name] = (float(gains.mean()), corrected.low, independent.low, ratio or 1.0)
    return out


@pytest.mark.parametrize(
    ("predict", "markedly"), [(_sticky, 2.0), (_independent, 1.0)], ids=["sticky", "independent"]
)
def test_a_prediction_with_no_edge_passes_the_corrected_gate_no_more_than_twice_its_level(
    predict: Predict, markedly: float
) -> None:
    rng = np.random.default_rng(310)
    gate = {"corrected": 0, "independent": 0}
    single = {name: [0, 0] for name in ("otc5-flat", "otc5-persist", "otc5-climate")}
    ratios = []
    for _ in range(RUNS):
        found = _gate(rng, predict)
        weakest = min(v[0] for v in found.values())
        gate["corrected"] += weakest > 0 and min(v[1] for v in found.values()) > 0
        gate["independent"] += weakest > 0 and min(v[2] for v in found.values()) > 0
        for name, (_, corrected, independent, _) in found.items():
            single[name][0] += corrected > 0
            single[name][1] += independent > 0
        ratios.append(max(v[3] for v in found.values()))
    rates = {k: v / RUNS for k, v in gate.items()}
    by_baseline = {k: (v[0] / RUNS, v[1] / RUNS) for k, v in single.items()}
    print(f"{predict.__name__}: gate {rates}, by baseline (corrected, independent) {by_baseline}")
    print(f"{predict.__name__}: largest variance ratio, mean over runs {sum(ratios) / RUNS:.2f}")
    # The gate (every baseline beaten) and each baseline's bound alone: at most 5%.
    assert rates["corrected"] <= 0.05
    assert all(corrected <= 0.05 for corrected, _ in by_baseline.values())
    # Uncorrected, each baseline's bound passes more often: for the sticky predictions, whose
    # right and wrong days run with the windows' classes, about 9–13% (more than twice).
    assert all(independent > markedly * corrected for corrected, independent in by_baseline.values())
    assert rates["independent"] > rates["corrected"]


def test_only_a_daily_kind_s_windows_may_run_several_sessions() -> None:
    from dataclasses import replace

    from krx_quant.daily import DailyRegistrationError, register_daily_kinds
    from krx_quant.kinds import KindRegistrationError, register_kinds
    from krx_quant.open_to_close import OPEN_TO_CLOSE

    with pytest.raises(KindRegistrationError, match="overlap"):
        register_kinds([kind(H)])
    assert register_kinds([kind(1)])
    with pytest.raises(DailyRegistrationError, match="overlap"):
        register_daily_kinds([replace(OPEN_TO_CLOSE, overlap=0)])
    assert register_daily_kinds([replace(OPEN_TO_CLOSE, overlap=H)])


def test_a_day_left_out_of_the_statistics_stays_a_session_between_its_neighbours() -> None:
    """A special or unscorable day is left out for every predictor (#310 review): placed among the
    calendar's sessions, the days on either side keep their distance of two, not one."""
    from krx_quant.kind_stats import kind_scoring
    from krx_quant.market_calendar import MarketCalendar

    windows, alg, base, rows = kind(H), [], [], []
    left_out = 20
    for d in range(40):
        if d == left_out:
            continue
        for s, symbol in enumerate(("005930", "000660", "009150")):
            outcome = (d // 3 + s) % 3
            base.append(scored("flat", d, symbol, 1, outcome))
            alg.append(scored("alg", d, symbol, (d // 4 + s) % 3, outcome))
    calendar = MarketCalendar({(DAY0 + timedelta(days=d)): True for d in range(40)})
    every_session = session_index(DAY0 + timedelta(days=d) for d in range(40))
    assert sessions_of(alg, {"flat": base}, calendar) == every_session
    e = entry("alg", alg, {"flat": base}, windows, calendar=calendar)
    assert e.lifts["flat"].by_day == overlap_day_lift(alg, base, every_session, H)
    assert e.lifts["flat"].lift == overlap_lift(alg, base, every_session, H)
    closed_up = entry("alg", alg, {"flat": base}, windows)  # without the calendar: neighbours
    assert closed_up.lifts["flat"].by_day != e.lifts["flat"].by_day
    for r in [*alg, *base]:
        rows.append(
            {
                "algorithm": r.algorithm,
                "model_version": "v1",
                "symbol": r.symbol,
                "bar_close": r.bar_close.isoformat(),
                "output": dict(zip(windows.p_keys(), r.probabilities, strict=True)),
                "answer_class": windows.classes[r.outcome],
                "answer_path": {},
            }
        )
    judged = kind_scoring(windows, calendar).measure("alg", rows, {"flat"}, 0.0, 0.95)
    assert judged.weakest_by_day == e.lifts["flat"].by_day  # the lifecycle's too


def test_an_overlapping_kind_s_own_p_and_l_has_driscoll_kraay_intervals() -> None:
    """``open_to_close``'s research P&L as a kind of five-session windows (#310 review): its per-trade
    interval is the Driscoll–Kraay one over the days, wider than the independent one when
    neighbouring days' trades move together."""
    from dataclasses import replace

    from krx_quant.open_to_close import OPEN_TO_CLOSE, call_profit
    from krx_quant.stats import DayOverlap, interval_json, mean_interval

    five = replace(OPEN_TO_CLOSE, overlap=H)
    records = []
    for d in range(60):
        change = 0.01 * math.sin(d / 5)  # neighbouring days alike
        for symbol in ("005930", "000660", "009150"):
            records.append(
                KindScored(
                    "alg",
                    "v1",
                    symbol,
                    at(d),
                    (1.0, 0.0, 0.0),
                    0,
                    {},
                    {"change": change, "fee_rate": 0.00015},
                )
            )
    extra = summarize(records, five, {}, "alg").extra
    assert extra is not None
    dated = [(r.bar_close.date(), call_profit("up", r.path)) for r in records]
    places = session_index(day for day, _ in dated)
    overlapping = DayOverlap(H, places).mean_interval([(d, v) for d, v in dated if v is not None])
    assert extra["long"]["per_trade"] == interval_json(overlapping)
    independent = mean_interval([v for _, v in dated if v is not None])
    assert overlapping is not None and independent is not None
    assert overlapping.low is not None and independent.low is not None
    assert overlapping.low < independent.low
    one = summarize(records, OPEN_TO_CLOSE, {}, "alg").extra
    assert one is not None and one["long"]["per_trade"] == interval_json(independent)  # one session
