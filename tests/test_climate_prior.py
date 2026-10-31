"""The climate prior experiment (#429): shannon's regression with each symbol and slot's class
frequencies as two inputs more, against shannon as it is and the climate baseline, on the folds of
shannon's pre-validation.

Input: made-up samples whose labels follow the time of day, and recorded days whose first half hour
swings both ways (each four minutes reach both targets: up) while the rest stands still. Output: the
prior each training sample and each fold's model are given, the models, the experiment's report and
lines, and the scheduled job.
"""

from __future__ import annotations

import json
import math
import re
from datetime import date, datetime, time, timedelta
from pathlib import Path

import numpy as np
import pytest

from krx_quant import climate_prior
from krx_quant.algorithms import Abstention, Model, NothingToLearn, NotPredicted, Sample
from krx_quant.algorithms.shannon import INPUTS, Shannon
from krx_quant.cli import main
from krx_quant.climate_prior import (
    PRIOR_INPUTS,
    PRIOR_STRENGTH,
    PriorTally,
    ShannonPrior,
    climate_prior_experiment,
    lines,
    prior_inputs,
    prior_of,
    regression,
    training_priors,
)
from krx_quant.config import Settings
from krx_quant.engine import Event
from krx_quant.features import Leaders
from krx_quant.feeds import KST
from krx_quant.feeds.base import kst_day
from krx_quant.market_calendar import calendar_store
from krx_quant.prevalidation import MIN_DAYS, _WalkForward
from krx_quant.pricing import Direction
from krx_quant.scheduled import CLIMATE_PRIOR, JOBS, Facts, JobRecord, Night
from krx_quant.stats import Interval
from krx_quant.training import finished_days
from tests.replay_events import at, book, trade
from tests.test_prevalidation import AFTER, DAYS
from tests.test_replay_cli import record
from tests.test_shannon import inputs, on_days

UP, FLAT, DOWN = Direction.UP, Direction.FLAT, Direction.DOWN
D1, D2, D3 = DAYS[:3]
VARIANT = ShannonPrior()


def sample(day: date, hhmm: str, outcome: Direction | None, symbol: str = "005930") -> Sample:
    """A bar close whose inputs say nothing (every one the same)."""
    return Sample(
        symbol, datetime.combine(day, time.fromisoformat(hhmm), KST), inputs(ofi_1=0.5), outcome, 0.0
    )


def a_day(day: date, opening: Direction = UP) -> list[Sample]:
    """Ten bar closes from 09:01 labelled ``opening``, ten from 10:01 flat."""
    return [sample(day, f"09:{m:02d}", opening) for m in range(1, 11)] + [
        sample(day, f"10:{m:02d}", FLAT) for m in range(1, 11)
    ]


def test_a_training_sample_s_prior_counts_only_the_window_s_days_before_its_own() -> None:
    window = [*a_day(D1), *a_day(D2), *a_day(D3)]

    priors = training_priors(window, window)

    assert priors[:20] == [(0.0, 0.0)] * 20, "the window's first day: nothing before it, the even split"
    # After the first day: the symbol's day 10 up, 10 flat, 0 down, with 30 pseudo-counts split evenly;
    # its 09:00 slot 10 up, with 30 in the day's frequencies.
    day = ((10 + 10) / 50, (10 + 10) / 50, 10 / 50)
    opening = (
        (10 + PRIOR_STRENGTH * day[0]) / (10 + PRIOR_STRENGTH),
        PRIOR_STRENGTH * day[1] / (10 + PRIOR_STRENGTH),
        PRIOR_STRENGTH * day[2] / (10 + PRIOR_STRENGTH),
    )
    assert opening == pytest.approx((0.55, 0.3, 0.15))
    assert priors[20] == pytest.approx(prior_inputs(opening)), "09:01 on the second day: the first day's"
    assert priors[20][0] > 0 > priors[30][0], "up likelier than flat at the open, not at 10:00"
    # A sample's own day's labels and the later days' never reach it.
    turned = [*a_day(D1), *a_day(D2, DOWN), *a_day(D3, DOWN)]
    assert training_priors(turned, turned)[:40] == priors[:40]
    assert training_priors(turned, turned)[40:] != priors[40:], "the third day counts the second's"
    # Nor do days outside the window: only what it is given is counted.
    assert training_priors(window[20:], window[20:])[:20] == [(0.0, 0.0)] * 20


def test_trained_on_a_single_day_it_learns_nothing_from_the_prior_of_that_day_s_own_labels() -> None:
    # The labels follow the slot alone: a prior that counted the day's own labels would tell them
    # all. Counted on the days before (none), it is the even split everywhere, and stays unweighted.
    model = VARIANT.fit(a_day(D1))

    assert [model.params[f"w.{c}.{f}"] for c in ("up", "flat", "down") for f in PRIOR_INPUTS] == [0.0] * 6
    assert model.params["prior.005930.09:00.up"] > 0.5, "the day is kept for the day after it"


def test_the_model_keeps_its_window_s_prior_and_predicts_with_it_alone() -> None:
    window = [*a_day(D1), *a_day(D2), *a_day(D3)]
    tally = PriorTally()
    tally.add(window)

    model = VARIANT.fit(window)

    kept = {k: v for k, v in model.params.items() if k.startswith("prior.")}
    assert kept == tally.table(), "every day of the window, in the model's own parameters"
    assert model.version.startswith("shannon-prior-20260916-")
    later = date(2026, 9, 17)
    opening, ten = sample(later, "09:05", None), sample(later, "10:05", None)
    at_open = VARIANT.predict(opening.features, model, opening.symbol, opening.bar_close)
    at_ten = VARIANT.predict(ten.features, model, ten.symbol, ten.bar_close)
    assert at_open.p_up > at_open.p_flat and at_ten.p_flat > at_ten.p_up, "the same inputs, the slot tells"
    stored = Model.from_json(model.to_json())
    assert VARIANT.predict(opening.features, stored, opening.symbol, opening.bar_close) == at_open
    # The prior is read from the model, nowhere else: the 10:00 slot's row in place of the open's.
    moved = {k: kept[k.replace("09:00", "10:00")] for k in kept if ".09:00." in k}
    swapped = Model(model.version, {**model.params, **moved})
    assert VARIANT.predict(opening.features, swapped, opening.symbol, opening.bar_close) == pytest.approx(
        at_ten
    )
    # A slot never seen takes its symbol's day; a symbol never seen, the even split.
    afternoon = datetime.combine(later, time(13, 5), KST)
    assert prior_of(model.params, "005930", afternoon) == tuple(
        kept[f"prior.005930.all.{c}"] for c in "up flat down".split()
    )
    assert prior_of(model.params, "000660", afternoon) == (1 / 3, 1 / 3, 1 / 3)


def test_without_the_book_or_a_label_it_learns_nothing_and_predicts_as_shannon_does() -> None:
    no_book = [Sample(s.symbol, s.bar_close, inputs(depth_imbalance=None), s.outcome, 0.0) for s in a_day(D1)]
    with pytest.raises(NothingToLearn, match="with the book"):
        VARIANT.fit(no_book)
    with pytest.raises(NotPredicted) as declined:
        VARIANT.predict(no_book[0].features, VARIANT.default_model(), "005930", no_book[0].bar_close)
    assert declined.value.why is Abstention.NO_BOOK
    s = a_day(D1)[0]
    default = VARIANT.predict(s.features, VARIANT.default_model(), s.symbol, s.bar_close)
    assert default == Shannon().predict(s.features, Shannon().default_model()), "every class as likely"


def test_on_shannon_s_inputs_alone_its_regression_is_shannon_s() -> None:
    # 240 samples on four days: the penalty is validated on the last two.
    scored = sorted(on_days([100, 100, 20, 20]), key=lambda s: (s.bar_close, s.symbol))
    raw = np.array([[np.nan if s.features[f] is None else s.features[f] for f in INPUTS] for s in scored])
    y = np.array([[float(s.outcome is c) for c in (UP, FLAT, DOWN)] for s in scored])

    ours = regression(raw, y, [kst_day(s.bar_close) for s in scored], INPUTS)

    theirs = Shannon().fit(scored).params
    assert ours["validation_days"] == 2.0
    assert ours == pytest.approx({k: theirs[k] for k in ours}, abs=1e-12)
    assert set(theirs) - set(ours) == {"move.up", "move.flat", "move.down"}


def mornings(day: date) -> list[Event]:
    """09:00–09:30: the mid swings six ticks up and down every two minutes, at its centre at each
    bar close, so the four minutes from a bar close reach both targets (k = 2 ticks): up. From
    09:30 to 10:30 it stands still: flat. A book with the same sizes and a trade every ten seconds."""
    events: list[Event] = []
    start = when = at("09:00:00", day)
    while when <= at("10:30:00", day):
        swing = round(6 * math.sin(2 * math.pi * (when - start).total_seconds() / 120))
        mid = 81_250 + 100 * (swing if when < at("09:30:00", day) else 0)
        events += [
            book(when, mid - 50, mid + 50, bid_qty=100, ask_qty=100, deeper=(50, 50)),
            trade(when + timedelta(seconds=1), mid + 50, 3),
        ]
        when += timedelta(seconds=10)
    return events


def settings(tmp_path: Path, days: list[date]) -> Settings:
    data = tmp_path / "data"
    for day in days:
        record(data, mornings(day), day=day)
    return Settings(
        kis_app_key="",
        kis_app_secret="",
        symbols=("005930",),
        etf_symbols=(),
        index_codes=(),
        us_symbols=(),
        data_dir=data,
        state_dir=tmp_path / "state",
    )


def value(i: Interval | None) -> float:
    assert i is not None
    return i.value


# Six recorded mornings, each made into samples by the Market View twice (the experiment, then the
# window checked by hand): a few seconds on a laptop, past the 30 s default on a loaded CI runner.
@pytest.mark.timeout(120)
def test_the_three_arms_are_compared_on_the_same_bar_closes_of_the_pre_validation_s_folds(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    days = DAYS[:6]
    s = settings(tmp_path, days)
    caplog.set_level("INFO", logger="krx_quant.climate_prior")

    result = climate_prior_experiment(s, algorithms=[Shannon()], kind_algorithms=[], kinds=[], now=AFTER)

    assert result.days == days
    first, *later = result.folds
    assert first.models == {
        "climate": "baseline-climate-default",
        "shannon": "shannon-default",
        "shannon_prior": "shannon-prior-default",
    }, "nothing before the first day"
    for fold, before in zip(later, days, strict=False):
        assert all(f"-{before:%Y%m%d}-" in v for v in fold.models.values()), "trained up to the day before"
    n = {arm.n for arm in result.arms.values()}
    assert n == {sum(f.scored for f in result.folds)} and n != {0}, "every arm on the same bar closes"
    happened = result.arms["climate"].happened
    assert happened[UP] > 0 and happened[FLAT] > happened[UP], "the open's half hour up, the rest flat"
    # Shannon cannot tell the open from the rest: always flat. The prior can, as the climate does.
    shannon, prior = result.arms["shannon"], result.arms["shannon_prior"]
    assert shannon.predicted[UP] == 0 and value(shannon.recall[UP]) == 0
    assert value(prior.accuracy) > value(shannon.accuracy)
    assert value(prior.recall[UP]) > 0.5 and value(prior.coverage) > 0
    over = result.prior_over_shannon
    assert over.pairs.value is not None and over.pairs.value > 0 and over.by_day is not None
    assert over.by_day.n == len(days)
    assert result.better and result.lower_bound_up and result.rule
    # The last day's (c) model holds its window's prior: the days before it, never its own.
    available = finished_days(s.data_dir, s.symbols, AFTER)
    trainer = _WalkForward(s, Shannon(), available, calendar_store(s).calendar, Leaders.of(s))
    window = trainer.samples_before(days[-1])
    assert {kst_day(x.bar_close) for x in window} == set(days[:-1])
    tally = PriorTally()
    tally.add(window)
    kept = {k: v for k, v in result.last_model.params.items() if k.startswith("prior.")}
    assert kept == tally.table()
    tally.add(trainer.samples_on(days[-1]))
    assert kept != tally.table(), "with the day it predicts, it would be another prior"
    assert result.last_model.params["w.up.climate_up"] > 0, "it learned to follow the prior"
    # Its memory a day (#383), and the report.
    logged = [r.getMessage() for r in caplog.records if ": memory on " in r.getMessage()]
    assert [m.split(": ")[1] for m in logged] == [
        f"memory on {d} (day {i} of 6)" for i, d in enumerate(days, 1)
    ]
    saved = json.loads((s.state_dir / "experiments" / "climate-prior-429.json").read_text())
    assert saved["days"] == [d.isoformat() for d in days]
    assert saved["rule"] == {"better": True, "lower_bound_up": True, "met": True}
    assert set(saved["arms"]) == {"climate", "shannon", "shannon_prior"}
    assert saved["arms"]["shannon_prior"]["by_class"]["up"]["recall"]["value"] == value(prior.recall[UP])
    assert saved["lift"]["over_climate"]["shannon"]["by_day"]["n"] == len(days)
    text = "\n".join(lines(result, Path("state/experiments/climate-prior-429.json")))
    assert text.startswith("climate prior experiment (#429): 6 recorded days, 2026-09-14..2026-09-21\n")
    assert "-> met: shannon would take the prior" in text
    assert re.search(r"\(c\) over \(b\) +\+0\.\d{4} \(", text)
    assert not re.search(r"\b8[01][0-9],?[0-9]{3}\b", text), "no price"


def test_the_command_says_when_no_day_was_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KRX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KRX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("KRX_SYMBOLS", "005930")

    assert main(["climate-prior", "--days", "3"]) == 1

    assert "no recorded day of 005930" in capsys.readouterr().err
    assert not (tmp_path / "state" / climate_prior.REPORT).exists()


def test_the_job_runs_once_on_the_first_night_with_ten_recorded_days_after_every_pre_validation() -> None:
    job = CLIMATE_PRIOR
    assert (job.name, job.command, job.timeout) == (
        "climate-prior-429",
        ("climate-prior",),
        timedelta(hours=3),
    )
    assert [t.issue for t in job.targets] == [429] and "시세 값이 없다" in job.targets[0].note
    assert [a.path for a in job.attach] == ["experiments/climate-prior-429.json"]
    assert JOBS[-1] is job, "last: it holds back no simulation nor pre-validation"
    assert job.gives_way_to is None and job.session is None
    saturday = date(2026, 10, 31)
    started = datetime(2026, 10, 31, 23, 0, tzinfo=KST)
    ran = JobRecord(saturday, "ok", started, "abc", 0, 600.0)
    cut = JobRecord(saturday, "interrupted", started, "abc")
    failed = JobRecord(saturday, "failed", started, "abc", None, 10800.0, timed_out=True)

    def night(recorded: int, record: JobRecord | None = None, commit: str = "abc") -> Night:
        return Night(saturday + timedelta(days=1), False, commit, record, {}, Facts(recorded_days=recorded))

    assert not job.due(night(MIN_DAYS - 1)), "too few recorded days"
    assert job.due(night(MIN_DAYS)), "trading or not"
    assert not job.due(night(15, ran)), "once"
    assert not job.due(night(15, ran, "def")), "once, whatever the code"
    assert job.due(night(15, cut)), "again after a restart cut it"
    assert not job.due(night(15, failed)), "a failure is not run again on the same code"
    assert job.due(night(15, failed, "def")), "but again once a deploy brings other code"
