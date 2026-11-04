"""The label-order experiment (#529): inside ``forecasts.label_order``, and only there,
``normal_forecast`` gives the touch target in the label's order; the experiment compares newton, gauss,
granger, markov and kalman as they run with the same trained and predicting in that order.

Input: forecasts of made-up moves and spreads, made-up samples and records, and recorded mornings
(``test_climate_prior``'s: the first half hour swings both ways, the rest stands still). Output: the
forecasts, each arm's records, the rule, the experiment's report and lines, and its scheduled job.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from krx_quant import normal_order
from krx_quant.algorithms.contract import Abstention, Model, NotPredicted, Sample
from krx_quant.algorithms.forecasts import forecasting, label_order, normal_forecast, ordered_forecast
from krx_quant.algorithms.gauss import Gauss
from krx_quant.algorithms.newton import Newton
from krx_quant.algorithms.shannon import Shannon
from krx_quant.baselines import Forecast
from krx_quant.cli import main
from krx_quant.climate_prior import PairedLift
from krx_quant.features import FeatureVector, Leaders, TouchTerms
from krx_quant.feeds.base import KST
from krx_quant.market_calendar import calendar_store
from krx_quant.normal_order import (
    INDEPENDENT,
    ORDERED,
    Comparison,
    _score,
    arm_report,
    lines,
    loss_gain_by_day,
    normal_order_experiment,
)
from krx_quant.prevalidation import _WalkForward
from krx_quant.pricing import Direction
from krx_quant.scheduled import (
    FAILED,
    JOBS,
    NEYMAN_VARIANTS,
    NORMAL_ORDER,
    SMT_DISCOVERY,
    Facts,
    JobRecord,
    Night,
)
from krx_quant.stats import Scored
from krx_quant.training import finished_days
from tests.prevalidation_doubles import AFTER, DAYS
from tests.test_climate_prior import settings

UP, FLAT, DOWN = Direction.UP, Direction.FLAT, Direction.DOWN
TOUCH = FeatureVector({}, (), TouchTerms(0.5))  # half a tick of spread at the bar close
ENDPOINT = FeatureVector({}, ())  # the four-minute move (target version 1)


def test_outside_label_order_the_targets_stay_independent_inside_they_follow_the_label() -> None:
    with forecasting(TOUCH):
        independent = normal_forecast(0.0, 2.0, 2.0)
        with label_order():
            ordered = normal_forecast(0.0, 2.0, 2.0)
            assert ordered == ordered_forecast(0.0, 2.0, 2.0)
        assert normal_forecast(0.0, 2.0, 2.0) == independent, "left on the way out"

    # k = 2 ticks, half a tick of spread, no drift, a spread of 2 ticks: the issue's numbers.
    assert independent.p_up == pytest.approx(ordered.p_up)
    assert round(independent.p_down, 3) == 0.167 and round(ordered.p_down, 3) == 0.211
    assert round(independent.p_flat, 3) == 0.622 and round(ordered.p_flat, 3) == 0.578
    with forecasting(ENDPOINT):
        endpoint = normal_forecast(0.3, 2.0, 1.0)
        with label_order():
            assert normal_forecast(0.3, 2.0, 1.0) == endpoint, "the four-minute move has no order"


def test_label_order_is_left_when_what_runs_inside_it_fails() -> None:
    with forecasting(TOUCH):
        independent = normal_forecast(0.0, 2.0, 1.0)
        with pytest.raises(RuntimeError), label_order():
            raise RuntimeError("a fit fails")
        assert normal_forecast(0.0, 2.0, 1.0) == independent


@pytest.mark.parametrize("move", [-2.0, -0.5, 0.0, 0.5, 2.0])
@pytest.mark.parametrize("spread", [0.3, 1.0, 2.0, 4.0])
@pytest.mark.parametrize("k", [1.0, 2.0, 3.0])
def test_the_label_s_order_never_gives_down_less_than_the_independent_targets(
    move: float, spread: float, k: float
) -> None:
    with forecasting(TOUCH):
        independent = normal_forecast(move, spread, k)
        with label_order():
            ordered = normal_forecast(move, spread, k)

    assert ordered.p_up == pytest.approx(independent.p_up, abs=1e-12)
    assert ordered.p_down >= independent.p_down - 1e-9, "a path that goes down reaches up less often"
    assert ordered.p_flat <= independent.p_flat + 1e-9
    assert ordered.p_up + ordered.p_flat + ordered.p_down == pytest.approx(1.0)
    assert ordered.expected_ticks == independent.expected_ticks == move


def newton_sample(minute: int, outcome: Direction | None, volatility: float = 2.0) -> Sample:
    values = {"velocity": 0.0, "acceleration": 0.0, "relative_volume": 1.0, "volatility": volatility}
    features = FeatureVector(values | {"flat_ticks": 1.0}, Newton.features, TouchTerms(0.5))
    bar_close = datetime(2026, 9, 14, 9, minute, tzinfo=KST)
    return Sample("005930", bar_close, features, outcome, 0.0)


class Declining:
    """Newton that declines the bar closes of one volatility, as kalman declines those without the book."""

    def __init__(self, volatility: float) -> None:
        self._newton, self._volatility = Newton(), volatility
        self.name, self.features = "newton", Newton.features

    def predict(self, features: FeatureVector, model: Model) -> Forecast:
        if features["volatility"] == self._volatility:
            raise NotPredicted(Abstention.NO_BOOK)
        return self._newton.predict(features, model)


def test_each_arm_forecasts_each_bar_close_as_it_would_alone_in_its_order() -> None:
    newton = Newton()
    model = newton.default_model()
    models = {INDEPENDENT: model, ORDERED: model}
    samples = [newton_sample(1, DOWN), newton_sample(2, None), newton_sample(3, UP, volatility=7.0)]
    records: dict[str, list[Scored]] = {INDEPENDENT: [], ORDERED: []}

    scored, not_predicted = _score(samples, Declining(7.0), models, records)  # type: ignore[arg-type]

    assert (scored, not_predicted) == (1, 1), "no label: not counted; declined: left out of both"
    (independent,), (ordered,) = records[INDEPENDENT], records[ORDERED]
    with forecasting(samples[0].features):
        alone = newton.predict(samples[0].features, model)
        with label_order():
            in_order = newton.predict(samples[0].features, model)
    assert independent.p_up == alone.p_up == ordered.p_up == in_order.p_up
    assert (independent.p_flat, independent.p_down) == (alone.p_flat, alone.p_down)
    assert (ordered.p_flat, ordered.p_down) == (in_order.p_flat, in_order.p_down)
    assert ordered.p_down > independent.p_down, "spread wide against the band: the order matters"
    assert (independent.algorithm, ordered.algorithm) == (INDEPENDENT, ORDERED)
    assert independent.outcome is ordered.outcome is DOWN


def records(arm: str, p: tuple[float, float, float], outcomes: Sequence[Direction]) -> list[Scored]:
    """One record a minute on two days, the first half of ``outcomes`` on each."""
    out = []
    for i, outcome in enumerate(outcomes):
        day = date(2026, 9, 14) + timedelta(days=i % 2)
        bar_close = datetime(day.year, day.month, day.day, 9, 1 + i // 2, tzinfo=KST)
        out.append(Scored(arm, f"{arm}-1", "005930", bar_close, *p, outcome))
    return out


def comparison(independent: list[Scored], ordered: list[Scored]) -> Comparison:
    return Comparison(
        "newton",
        [],
        {INDEPENDENT: arm_report(independent), ORDERED: arm_report(ordered)},
        PairedLift.of(ordered, independent),
        loss_gain_by_day(ordered, independent),
    )


def test_the_order_is_a_candidate_when_it_hits_more_and_its_log_loss_is_lower() -> None:
    outcomes = [DOWN, DOWN, DOWN, FLAT, DOWN, DOWN, FLAT, DOWN]
    flat = records(INDEPENDENT, (0.2, 0.5, 0.3), outcomes)  # calls flat
    down = records(ORDERED, (0.2, 0.3, 0.5), outcomes)  # calls down

    c = comparison(flat, down)

    assert c.lift.pairs.value == pytest.approx(4 / 8), "six downs won, two flats lost"
    assert c.better is True and c.lower_loss is True and c.candidate
    gain = c.loss_gain
    assert gain is not None and gain.n == 2 and gain.value > 0, "a trading day a value"
    report = c.arms[ORDERED]
    assert report.mean_probability == pytest.approx({UP: 0.2, FLAT: 0.3, DOWN: 0.5})
    assert report.loss_where[FLAT] is not None and report.loss_where[UP] is None, "no up happened"
    assert c.as_dict()["rule"] == {"better": True, "lower_loss": True, "candidate": True}

    reverse = comparison(down, flat)
    assert reverse.better is False and reverse.lower_loss is False and not reverse.candidate
    # More hits with a worse log loss: not a candidate.
    sure = records(ORDERED, (0.0, 0.0, 1.0), outcomes)
    mixed = comparison(flat, sure)
    assert mixed.better is True and mixed.lower_loss is False and not mixed.candidate


def test_without_a_pair_nothing_is_gained() -> None:
    assert loss_gain_by_day([], []) is None
    c = comparison([], [])
    assert c.better is None and c.lower_loss is None and not c.candidate


# Five recorded mornings, each made into samples by the Market View once for the experiment and again
# for the window checked by hand: a few seconds on a laptop, past the 30 s default on a loaded runner.
@pytest.mark.timeout(180)
def test_newton_and_gauss_are_compared_on_the_pre_validation_s_folds_shannon_is_not(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    days = DAYS[:5]
    s = settings(tmp_path, days)
    caplog.set_level("INFO", logger="krx_quant.normal_order")

    result = normal_order_experiment(
        s, algorithms=[Shannon(), Newton(), Gauss()], kind_algorithms=[], kinds=[], now=AFTER
    )

    assert result.days == days
    assert [c.algorithm for c in result.comparisons] == ["newton", "gauss"], "the registry's order"
    for c in result.comparisons:
        first, *later = c.folds
        assert first.models == {INDEPENDENT: f"{c.algorithm}-default", ORDERED: f"{c.algorithm}-default"}
        for fold, before in zip(later, days, strict=False):
            assert all(f"-{before:%Y%m%d}-" in v for v in fold.models.values()), "trained to the day before"
        n = {arm.stats.n for arm in c.arms.values()}
        assert n == {sum(f.scored for f in c.folds)} and n != {0}, "both arms on the same bar closes"
        assert c.lift.by_day is not None and c.lift.by_day.n == len(days)
    # The independent arm is the algorithm as its pre-validation trains it.
    available = finished_days(s.data_dir, s.symbols, AFTER)
    trainer = _WalkForward(s, Newton(), available, calendar_store(s).calendar, Leaders.of(s))
    newton = result.comparisons[0]
    assert newton.folds[-1].models[INDEPENDENT] == trainer.before(days[-1]).version
    logged = [r.getMessage() for r in caplog.records if "'s memory on " in r.getMessage()]
    assert [m.split(": ")[1] for m in logged[:5]] == [
        f"newton's memory on {d} (day {i} of 5)" for i, d in enumerate(days, 1)
    ]
    saved = json.loads((s.state_dir / "experiments" / "normal-order-529.json").read_text())
    assert saved["days"] == [d.isoformat() for d in days]
    assert [c["algorithm"] for c in saved["comparisons"]] == ["newton", "gauss"]
    by_class = saved["comparisons"][0]["arms"][ORDERED]["by_class"]["down"]
    assert set(by_class) == {"recall", "precision", "predicted", "happened", "mean_probability", "log_loss"}
    assert saved["candidates"] == result.candidates
    text = "\n".join(lines(result, Path("state/experiments/normal-order-529.json")))
    assert text.startswith(
        "normal_forecast label-order experiment (#529): 5 recorded days, 2026-09-14..2026-09-18\n"
    )
    assert re.search(r"^newton: \d+ bar closes both arms predicted", text, re.M)
    assert re.search(r"^  #529 rule: lift above 0: (yes|no|-); lower log loss: (yes|no|-) -> ", text, re.M)
    assert text.endswith("report: state/experiments/normal-order-529.json")
    assert not re.search(r"\b8[01][0-9],?[0-9]{3}\b", text), "no price"


def test_the_command_says_when_no_day_was_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KRX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KRX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("KRX_SYMBOLS", "005930")

    assert main(["normal-order", "--days", "3"]) == 1

    assert "no recorded day of 005930" in capsys.readouterr().err
    assert not (tmp_path / "state" / normal_order.REPORT).exists()


def test_the_job_runs_once_with_ten_recorded_days_and_again_after_a_failure_on_other_code() -> None:
    job = NORMAL_ORDER
    assert (job.name, job.command, job.timeout) == ("normal-order-529", ("normal-order",), timedelta(hours=4))
    assert [t.issue for t in job.targets] == [529] and "가격이 없다" in job.targets[0].note
    assert [a.path for a in job.attach] == ["experiments/normal-order-529.json"]
    assert JOBS.index(NEYMAN_VARIANTS) < JOBS.index(job) < JOBS.index(SMT_DISCOVERY)
    assert job.gives_way_to is None and job.session is None
    sunday = date(2026, 11, 8)
    started = datetime(2026, 11, 8, 23, 0, tzinfo=KST)
    ran = JobRecord(sunday, "ok", started, "abc", 0, 600.0)
    failed = JobRecord(sunday, FAILED, started, "abc", None, 14400.0, timed_out=True)

    def night(recorded: int, record: JobRecord | None = None, commit: str = "abc") -> Night:
        return Night(sunday + timedelta(days=1), False, commit, record, {}, Facts(recorded_days=recorded))

    assert not job.due(night(9)), "fewer than ten recorded days"
    assert job.due(night(10)), "the first night with ten, trading or not"
    assert not job.due(night(30, ran)), "once"
    assert not job.due(night(30, failed)), "a failure is not run again on the same code"
    assert job.due(night(30, failed, "def")), "but again once a deploy brings other code"
