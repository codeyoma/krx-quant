"""The climate prior experiment (#429): shannon's regression with each symbol and slot's class
frequencies as two inputs more, against shannon as it was before and the climate baseline, on the
folds of shannon's pre-validation. Since #429's adoption (c) is shannon itself; the prior's helpers
are ``algorithms/priors.py``'s (``tests/test_priors.py``), and shannon's prior ``tests/test_shannon.py``'s.

Input: made-up samples whose labels follow the time of day, and recorded days whose first half hour
swings both ways (each four minutes reach both targets: up) while the rest stands still. Output: the
arms' models, the experiment's report and lines. Its scheduled job (``climate-prior-429``) ran on
2026-11-01 and left ``JOBS`` (#505).
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

from krx_quant import climate_prior
from krx_quant.algorithms import Abstention, NothingToLearn, NotPredicted
from krx_quant.algorithms.priors import PriorTally
from krx_quant.algorithms.shannon import INPUTS, Shannon, default_params, regression
from krx_quant.cli import main
from krx_quant.climate_prior import ShannonAlone, ShannonPrior, climate_prior_experiment, lines
from krx_quant.config import Settings
from krx_quant.engine import Event
from krx_quant.features import Leaders
from krx_quant.feeds.base import kst_day
from krx_quant.market_calendar import calendar_store
from krx_quant.prevalidation import _WalkForward
from krx_quant.pricing import Direction
from krx_quant.stats import Interval
from krx_quant.training import finished_days
from tests.prevalidation_doubles import AFTER, DAYS
from tests.replay_doubles import record
from tests.replay_events import at, book, trade
from tests.sample_doubles import a_day, inputs
from tests.test_shannon import on_days

UP, FLAT, DOWN = Direction.UP, Direction.FLAT, Direction.DOWN
D1, D2, D3 = DAYS[:3]


def test_c_is_shannon_now_under_the_experiment_s_name() -> None:
    window = [*a_day(D1), *a_day(D2), *a_day(D3)]

    ours, theirs = ShannonPrior().fit(window), Shannon().fit(window)

    assert ours.params == theirs.params
    assert ours.version.startswith("shannon-prior-20260916-")
    assert ours.version.removeprefix("shannon-prior-") == theirs.version.removeprefix("shannon-")
    assert ShannonPrior().default_model().version == "shannon-prior-default"


def test_b_is_shannon_before_the_prior_its_regression_on_its_eight_inputs_alone() -> None:
    # 240 samples on four days: the penalty is validated on the last two.
    scored = sorted(on_days([100, 100, 20, 20]), key=lambda s: (s.bar_close, s.symbol))
    raw = np.array([[np.nan if s.features[f] is None else s.features[f] for f in INPUTS] for s in scored])
    y = np.array([[float(s.outcome is c) for c in (UP, FLAT, DOWN)] for s in scored])

    model = ShannonAlone().fit(scored)

    assert model.version.startswith("shannon-20260904-")
    assert model.params.keys() == default_params(INPUTS).keys(), "no prior, no prior's input"
    ours = regression(raw, y, [kst_day(s.bar_close) for s in scored], INPUTS)
    assert ours["validation_days"] == 2.0
    assert {k: model.params[k] for k in ours} == ours
    assert set(model.params) - set(ours) == {"move.up", "move.flat", "move.down"}
    with pytest.raises(NothingToLearn, match="with the book"):
        ShannonAlone().fit([replace(s, features=inputs(depth_imbalance=None)) for s in scored])
    with pytest.raises(NotPredicted) as declined:
        ShannonAlone().predict(inputs(depth_imbalance=None), model)
    assert declined.value.why is Abstention.NO_BOOK


def test_where_the_prior_says_nothing_shannon_is_b() -> None:
    # One day: no day before any sample, the even split everywhere; the prior's inputs never vary.
    one_day = on_days([150])

    alone, shannon = ShannonAlone().fit(one_day), Shannon().fit(one_day)

    assert {k: shannon.params[k] for k in alone.params} == alone.params
    assert all(v == 0 for k, v in shannon.params.items() if k.startswith("w.") and "climate" in k)


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
