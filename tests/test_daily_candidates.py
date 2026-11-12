"""The daily kinds' candidates (#698): Hurst's momentum rule, Lorenz's analog days, Friedman's
trees and their five-session twins; their new features at 08:50, live and in training alike; their
purged split; and the walk-forward's clone check among the algorithms of a kind, on made-up
histories."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from krx_quant.algorithms import DAILY_ALGORITHMS, Model, load_models, save_model
from krx_quant.algorithms.cox import Cox
from krx_quant.algorithms.daily_window import MIN_SAMPLES, window
from krx_quant.algorithms.friedman import Friedman
from krx_quant.algorithms.friedman5d import Friedman5d
from krx_quant.algorithms.hurst import LOOKBACKS, Hurst
from krx_quant.algorithms.hurst5d import Hurst5d
from krx_quant.algorithms.lorenz import Lorenz
from krx_quant.algorithms.lorenz5d import Lorenz5d
from krx_quant.config import Settings
from krx_quant.daily import Band, DailyAlgorithm, DailySample, DayPrices
from krx_quant.daily_features import (
    DAILY_FEATURES,
    DailyFiles,
    DayContext,
    LiveFeatures,
    samples,
    vector,
)
from krx_quant.daily_training import prevalidate_daily
from krx_quant.features import FeatureVector
from krx_quant.feeds.base import KST
from krx_quant.kinds import Answer
from krx_quant.lifecycle import Status
from krx_quant.market_calendar import MarketCalendar
from krx_quant.open_to_close import CLASSES, OPEN_TO_CLOSE
from krx_quant.open_to_close_5d import OPEN_TO_CLOSE_5D
from krx_quant.predictions import DB_NAME, PredictionStore
from krx_quant.prevalidation import code_digest, report_path
from tests.daily_doubles import SYMBOLS, made_history

FEE = 0.015 / 100
LAST = date(2026, 10, 16)  # a Friday
NOW = datetime(2026, 10, 16, 22, 30, tzinfo=KST)
CALENDAR = MarketCalendar()
CANDIDATES: list[DailyAlgorithm] = [Hurst(), Lorenz(), Friedman(), Hurst5d(), Lorenz5d(), Friedman5d()]


def settings(tmp_path: Path, days: int, signal: float = 0.0) -> Settings:
    made_history(tmp_path / "data", tmp_path / "state", LAST, days, signal=signal)
    return Settings(
        kis_app_key="",
        kis_app_secret="",
        symbols=SYMBOLS,
        etf_symbols=(),
        index_codes=(),
        us_symbols=(),
        data_dir=tmp_path / "data",
        state_dir=tmp_path / "state",
    )


def test_three_candidates_of_other_families_for_each_daily_kind() -> None:
    by_kind: dict[str, list[str]] = {}
    for a in DAILY_ALGORITHMS:
        by_kind.setdefault(a.kind, []).append(a.name)
    assert by_kind == {
        "open_to_close": ["cox", "hurst", "lorenz", "friedman"],
        "open_to_close_5d": ["cox-5d", "hurst-5d", "lorenz-5d", "friedman-5d"],
    }
    for a in CANDIDATES:
        assert set(a.features) <= set(DAILY_FEATURES)
        card = a.card.as_dict()
        assert all(card[k] for k in ("scientist", "character", "logic", "training", "forecasting", "failure"))
    assert {a.card.scientist for a in CANDIDATES} == {
        "Harold Edwin Hurst",
        "Edward Lorenz",
        "Jerome H. Friedman",
    }
    assert set(Friedman.features) == set(DAILY_FEATURES), "the trees read every daily feature"
    # A twin's digest holds its parent's package and the shared window (``code_from``).
    assert code_digest(Hurst5d()) != code_digest(Hurst())


def ctx_of(
    history: list[DayPrices], flows: Mapping[date, Mapping[str, float | None]] | None = None
) -> DayContext:
    return DayContext(
        symbol="005930",
        day=history[-1].day + timedelta(days=1),
        history=history,
        labels=["up"] * len(history),
        previous_day=history[-1].day,
        band=Band(0.005, 0.01, 0.5),
        flow_days=flows or {},
    )


def test_the_new_features_by_hand() -> None:
    """A symbol whose close grows 1% a day but every tenth day (down 2%): its momenta over the
    realised volatility, its drawdown from the year's high, its last days' range over its usual,
    and its five days' flows."""
    start = date(2025, 1, 1)
    closes = [100.0]
    for i in range(1, 260):
        closes.append(closes[-1] * (0.98 if i % 10 == 0 else 1.01))
    history = [
        DayPrices(start + timedelta(days=i), c, c * (1.04 if i >= 255 else 1.01), c * 0.99, c, 1_000)
        for i, c in enumerate(closes)
    ]
    v = vector(("cc_5", "cc_20", "cc_60", "drawdown", "range_5"), ctx_of(history))
    changes = [math.log(b / a) for a, b in zip(closes[-61:-1], closes[-60:], strict=True)]
    rv = math.sqrt(sum(c * c for c in changes) / 60)
    for n in (5, 20, 60):
        assert v[f"cc_{n}"] == pytest.approx(math.log(closes[-1] / closes[-1 - n]) / (rv * math.sqrt(n)))
    assert v["drawdown"] == pytest.approx(0.0), "the last close is the year's high"
    usual = (sum((1.04 if i >= 255 else 1.01) - 0.99 for i in range(200, 260))) / 60
    assert v["range_5"] == pytest.approx(math.log(0.05 / usual))

    fell = [
        *history[:-1],
        DayPrices(history[-1].day, closes[-2], closes[-2], closes[-2] * 0.9, closes[-2] * 0.9, 1),
    ]
    assert vector(("drawdown",), ctx_of(fell))["drawdown"] == pytest.approx(math.log(0.9))
    kept = [
        *history[:-1],
        DayPrices(history[-1].day, closes[-2], closes[-2], closes[-2], closes[-2], 1, candle=False),
    ]
    assert vector(("cc_5", "drawdown"), ctx_of(kept)).as_dict() == {"cc_5": None, "drawdown": None}, (
        "a day kept from the auctions alone is not on the candles' scale"
    )
    short = vector(("cc_60", "drawdown"), ctx_of(history[-100:]))
    assert short["cc_60"] is not None and short["drawdown"] is None, "fewer than 250 days: no year's high"

    flows: dict[date, dict[str, float | None]] = {
        d.day: {"foreign": 0.01 * k, "institution": -0.02} for k, d in enumerate(history[-5:])
    }
    f = vector(("foreign_net_5", "institution_net_5"), ctx_of(history, flows))
    assert (f["foreign_net_5"], f["institution_net_5"]) == pytest.approx((0.02, -0.02))
    flows[history[-3].day] = {"foreign": None, "institution": -0.02}
    assert vector(("foreign_net_5",), ctx_of(history, flows))["foreign_net_5"] is None, (
        "one day without: none"
    )


def test_the_new_features_are_the_same_live_and_in_training(tmp_path: Path) -> None:
    data, state = tmp_path / "data", tmp_path / "state"
    made_history(data, state, LAST, 700, signal=1.0)
    files = DailyFiles(data, state)
    friedman = Friedman()
    trained = [s for s in samples(OPEN_TO_CLOSE, files, SYMBOLS, friedman.features, FEE) if s.day == LAST]
    live = LiveFeatures(OPEN_TO_CLOSE, data, state, CALENDAR, SYMBOLS, FEE)
    for sample in trained:
        history = [d for d in files.days(sample.symbol) if d.day < LAST]
        got = live(friedman, sample.symbol, LAST, history, OPEN_TO_CLOSE.band_after(history)).as_dict()
        assert got == pytest.approx(sample.features.as_dict(), nan_ok=True), sample.symbol
        for name in ("cc_5", "cc_20", "cc_60", "drawdown", "range_5", "foreign_net_5", "institution_net_5"):
            assert sample.features[name] is not None, name


def made(labels: list[str], values: list[float | None], name: str, sessions: int = 1) -> list[DailySample]:
    """One symbol's samples on consecutive weekdays, each with ``name``'s value and its class, its
    window ending ``sessions`` weekdays on (the day itself for one)."""
    days: list[date] = []
    day = date(2016, 1, 4)
    while len(days) < len(labels) + sessions:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return [
        DailySample(
            "005930", d, FeatureVector({name: value}, (name,)), Answer(label), 1, days[i + sessions - 1]
        )
        for i, (d, label, value) in enumerate(zip(days, labels, values, strict=False))
    ]


def test_hurst_picks_the_lookback_that_tells_the_classes_apart_and_reads_its_tercile() -> None:
    """The day goes against the last month's move (cc_20 above 1: down; below −1: up); the week's
    and the quarter's tell nothing."""
    import random

    rng = random.Random(698)
    rows = []
    for _ in range(1_500):
        x = rng.uniform(-3, 3)
        lean = "down" if x > 1 else "up" if x < -1 else rng.choice(CLASSES)
        rows.append(
            (x, lean if rng.random() < 0.8 else rng.choice(CLASSES), rng.uniform(-3, 3), rng.uniform(-3, 3))
        )
    found = []
    for (x, _label, five, sixty), base in zip(
        rows, made([r[1] for r in rows], [r[0] for r in rows], "cc_20"), strict=True
    ):
        found.append(
            DailySample(
                base.symbol,
                base.day,
                FeatureVector({"cc_5": five, "cc_20": x, "cc_60": sixty}, LOOKBACKS),
                base.answer,
                1,
                base.ends,
            )
        )
    hurst = Hurst()
    model = hurst.fit(found)
    assert model.params["lookback"] == 20 and model.target_version == OPEN_TO_CLOSE.answer_version
    assert set(model.params) <= set(hurst.param_meanings)
    assert model.params["p.high.down"] > 0.6 and model.params["p.low.up"] > 0.6, "reversal, both ways"
    high = hurst.predict(FeatureVector({"cc_5": 0.0, "cc_20": 2.5, "cc_60": 0.0}, LOOKBACKS), model)
    assert high.probabilities == pytest.approx(tuple(model.params[f"p.high.{c}"] for c in CLASSES))
    unknown = hurst.predict(FeatureVector({"cc_5": 0.0, "cc_20": None, "cc_60": 0.0}, LOOKBACKS), model)
    assert unknown.probabilities == pytest.approx(tuple(model.params[f"p.missing.{c}"] for c in CLASSES))
    assert hurst.predict(found[0].features, hurst.default_model()).probabilities == (1 / 3, 1 / 3, 1 / 3)


@pytest.mark.parametrize("algorithm", [Lorenz(), Friedman()], ids=lambda a: a.name)
def test_lorenz_and_friedman_learn_the_planted_lean_and_round_trip(
    tmp_path: Path, algorithm: DailyAlgorithm
) -> None:
    """The made-up day leans against the night's SOX move: fitted on the days before the last 200,
    each beats a third on them, and its saved model predicts the same."""
    made_history(tmp_path / "data", tmp_path / "state", LAST, 900, signal=1.0)
    found = samples(
        OPEN_TO_CLOSE, DailyFiles(tmp_path / "data", tmp_path / "state"), SYMBOLS, algorithm.features, FEE
    )
    days = sorted({s.day for s in found})
    cut = days[-200]
    model = algorithm.fit([s for s in found if s.day < cut])
    assert (
        model.version.startswith(f"{algorithm.name}-")
        and model.target_version == OPEN_TO_CLOSE.answer_version
    )
    assert model.blob is not None and set(model.params) <= set(algorithm.param_meanings)
    assert model.params["validation_loss"] < math.log(3), "held out, better than a third each"
    held = [s for s in found if s.day >= cut and s.label is not None]
    hits = sum(
        CLASSES[max(range(3), key=lambda i: algorithm.predict(s.features, model).probabilities[i])] == s.label
        for s in held
    )
    assert hits / len(held) > 0.45, "out of sample, the lean is learned"
    save_model(tmp_path / "state", algorithm.name, model)
    [loaded] = load_models(tmp_path / "state", [algorithm]).values()
    assert algorithm.predict(
        held[-1].features, Model.from_json(loaded.to_json())
    ).probabilities == pytest.approx(algorithm.predict(held[-1].features, model).probabilities)
    assert algorithm.predict(held[-1].features, algorithm.default_model()).probabilities == (
        1 / 3,
        1 / 3,
        1 / 3,
    )


def test_the_window_leaves_out_the_days_whose_windows_end_in_the_held_out_part() -> None:
    labels = [CLASSES[i % 3] for i in range(400)]
    one = window(made(labels, [0.0] * 400, "cc_5"), 1_000)
    assert one.early.sum() + one.late.sum() == 400, "one session: every day in one part"
    five = window(made(labels, [0.0] * 400, "cc_5", sessions=5), 1_000)
    left = [
        s.day for s, e, held in zip(five.samples, five.early, five.late, strict=True) if not e and not held
    ]
    assert left == five.days[five.days.index(five.cut) - 4 : five.days.index(five.cut)], (
        "the four days before"
    )
    from krx_quant.algorithms.contract import NothingToLearn

    with pytest.raises(NothingToLearn):
        window(made(labels[: MIN_SAMPLES - 1], [0.0] * (MIN_SAMPLES - 1), "cc_5"), 1_000)


def test_a_twin_learns_its_kind_s_answers() -> None:
    labels = [CLASSES[i % 3] for i in range(900)]
    found = [
        DailySample(
            s.symbol,
            s.day,
            FeatureVector({f: 0.1 * (i % 7) for f in LOOKBACKS}, LOOKBACKS),
            s.answer,
            1,
            s.ends,
        )
        for i, s in enumerate(made(labels, [0.0] * 900, "cc_5", sessions=5))
    ]
    model = Hurst5d().fit(found)
    assert model.version.startswith("hurst-5d-") and model.target_version == OPEN_TO_CLOSE_5D.answer_version


@pytest.mark.timeout(420)  # eight walk-forwards of 1,300 made-up days
def test_a_candidate_that_moves_with_one_its_kind_runs_is_refused_as_a_clone(tmp_path: Path) -> None:
    """On a planted lean every model of the kind learns, Friedman's walk-forward moves with Cox's.
    Cox waiting for its own verdict is not walked beside it, nor on probation without a report that
    stands; once the engine runs Cox (on probation, passed on its code), Friedman is refused as its
    clone; once Cox's pre-validation refused the code it has
    (and so the engine does not run it), it is left out again."""
    s = settings(tmp_path, 1_300, signal=0.6)
    both: list[DailyAlgorithm] = [Cox(), Friedman()]
    alone = prevalidate_daily(s, "friedman", algorithms=both)
    assert alone.correlations == {} and {c.name: c for c in alone.checks}["correlation"].passed, (
        "Cox has neither a status nor a pass: not one the engine runs"
    )

    store = PredictionStore(s.state_dir / DB_NAME)
    store.add_status("cox", NOW, None, Status.PROBATION, "registered")
    store.close()
    assert prevalidate_daily(s, "friedman", algorithms=both).correlations == {}, (
        "on probation without a standing report: the engine does not run it (#703 review)"
    )
    assert prevalidate_daily(s, "cox", algorithms=[Cox()]).passed
    cox_report = report_path(s.state_dir, "cox")
    passing = json.loads(cox_report.read_text())
    cox_report.write_text(json.dumps({**passing, "code": "0" * 16}))
    assert prevalidate_daily(s, "friedman", algorithms=both).correlations == {}, (
        "on probation, its pass of another code: not admitted, not beside"
    )
    cox_report.write_text(json.dumps(passing))
    result = prevalidate_daily(s, "friedman", algorithms=both)
    checks = {c.name: c for c in result.checks}
    assert set(result.correlations) == {"cox"} and result.correlations["cox"] > 0.9
    assert not checks["correlation"].passed and "highest with cox" in checks["correlation"].detail
    assert not result.passed
    saved = json.loads(report_path(s.state_dir, "friedman").read_text())
    assert saved["correlations"] == result.correlations

    path = report_path(s.state_dir, "cox")
    report = {**saved, "algorithm": "cox", "code": code_digest(Cox()), "passed": False}
    report["inputs"] = {f: DAILY_FEATURES[f].version for f in Cox.features if DAILY_FEATURES[f].version != 1}
    path.write_text(json.dumps(report))
    again = prevalidate_daily(s, "friedman", algorithms=both)
    assert again.correlations == {}, (
        "refused on its code, on probation (a daily one runs only while admitted)"
    )


@pytest.mark.timeout(240)  # three walk-forwards of 1,300 made-up days
def test_a_retired_algorithm_is_not_walked_beside_even_with_a_passing_report(tmp_path: Path) -> None:
    """Cox passed its pre-validation, then was retired: the engine no longer runs it, so a
    candidate that moves with it is not refused for that (#703 review)."""
    s = settings(tmp_path, 1_300, signal=0.6)
    both: list[DailyAlgorithm] = [Cox(), Friedman()]
    passed = prevalidate_daily(s, "cox", algorithms=[Cox()])
    assert passed.passed, passed.checks
    assert set(prevalidate_daily(s, "friedman", algorithms=both).correlations) == {"cox"}, "passed: beside"
    store = PredictionStore(s.state_dir / DB_NAME)
    store.add_status("cox", NOW, None, Status.RETIRED, "test")
    store.close()
    assert prevalidate_daily(s, "friedman", algorithms=both).correlations == {}, "retired: not beside"
