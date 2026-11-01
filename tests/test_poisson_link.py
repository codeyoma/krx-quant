"""The asymmetric link experiment (#439): poisson's lean through a multinomial logistic of its own,
against poisson's ordered logistic and the climate baseline, on the folds of poisson's
pre-validation.

Input: made-up samples whose arrivals excite each other as a Hawkes process does, labelled up more
often than flat where the buys lean and flat elsewhere, and recorded days whose first half hour
swings both ways under buyers (each four minutes reach both targets: up) while the rest stands
still under sellers. Output: the links' calls, the tuning of the penalty and what it is fitted on,
the experiment's report and lines, and the scheduled job.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import astuple
from datetime import date, datetime, time, timedelta
from pathlib import Path

import numpy as np
import pytest

from krx_quant import poisson_link
from krx_quant.algorithms import NothingToLearn, Sample
from krx_quant.algorithms.forecasts import UNKNOWN
from krx_quant.algorithms.poisson import ARRIVALS, EXCITATIONS, Poisson
from krx_quant.algorithms.shannon import PENALTIES
from krx_quant.baselines import Forecast
from krx_quant.cli import main
from krx_quant.config import Settings
from krx_quant.engine import Event
from krx_quant.features import SIDES, FeatureVector, Leaders
from krx_quant.feeds import KST
from krx_quant.feeds.base import kst_day
from krx_quant.market_calendar import calendar_store
from krx_quant.poisson_link import AsymmetricLink, lean_of, lines, poisson_link_experiment, tuned
from krx_quant.prevalidation import MIN_DAYS, _WalkForward
from krx_quant.pricing import Direction, most_likely
from krx_quant.scheduled import JOBS, POISSON_LINK, Facts, JobRecord, Night
from krx_quant.stats import Interval
from krx_quant.training import finished_days
from tests.replay_events import at, book, trade
from tests.test_poisson import TRUE_INTENSITIES, vector
from tests.test_prevalidation import AFTER, DAYS
from tests.test_replay_cli import record

UP, FLAT, DOWN = Direction.UP, Direction.FLAT, Direction.DOWN
VARIANT = AsymmetricLink()
POISSON = Poisson()


def hawkes_days(days: Sequence[date], per_day: int = 60, seed: int = 0) -> list[Sample]:
    """Each day, ``per_day`` bar closes five minutes apart from 09:01, each with its excitations and
    the four minutes after it carrying the arrivals the true intensities drew (all in the fourth).
    Where the buys are expected to lean, three bar closes in five are up and two flat; elsewhere
    every one is flat. Down never comes: the data are asymmetric."""
    rng = np.random.default_rng(seed)
    out: list[Sample] = []
    leaning = 0
    for day in days:
        start = datetime.combine(day, time(9, 1), KST)
        for i in range(per_day):
            x = rng.exponential(1.0, size=len(EXCITATIONS))
            expected = {
                side: TRUE_INTENSITIES[side][0] + float(np.dot(TRUE_INTENSITIES[side][1:], x))
                for side in SIDES
            }
            drawn = {side: rng.poisson(expected[side]) for side in SIDES}
            if expected["buy"] > expected["sell"]:
                outcome = UP if leaning % 5 < 3 else FLAT
                leaning += 1
            else:
                outcome = FLAT
            t = start + timedelta(minutes=5 * i)
            excitations = vector(**dict(zip(EXCITATIONS, map(float, x), strict=True)))
            out.append(Sample("005930", t, excitations, outcome, 0.0))
            for step in range(1, 5):
                last = {
                    a: float(drawn[side]) if step == 4 else 0.0
                    for a, side in zip(ARRIVALS, SIDES, strict=True)
                }
                out.append(Sample("005930", t + timedelta(minutes=step), vector(**last), None, None))
    return out


Predict = Callable[[FeatureVector], Forecast]


def calls(predict: Predict, samples: Sequence[Sample]) -> list[tuple[Direction, Direction]]:
    """The class ``predict`` calls at each bar close with a label, and the class that came."""
    out = []
    for s in samples:
        if s.outcome is not None:
            f = predict(s.features)
            out.append((most_likely(f.p_up, f.p_flat, f.p_down), s.outcome))
    return out


def test_on_asymmetric_data_the_asymmetric_link_calls_up_and_the_ordered_one_calls_nothing() -> None:
    window, later = hawkes_days(DAYS[:5]), hawkes_days(DAYS[5:6], seed=1)
    ordered = POISSON.fit(window)

    asymmetric = VARIANT.fit(window, ordered)

    # The ordered logistic gives up at a lean what it gives down at its opposite: down never comes
    # where the sells lean, so it can only call flat.
    ordered_calls = calls(lambda f: POISSON.predict(f, ordered), later)
    asymmetric_calls = calls(lambda f: VARIANT.predict(f, asymmetric), later)
    assert Counter(c for c, _ in ordered_calls) == Counter({FLAT: 60})
    called = Counter(c for c, _ in asymmetric_calls)
    assert called[UP] > 0 and called[DOWN] == 0
    assert sum(c is o for c, o in asymmetric_calls) > sum(c is o for c, o in ordered_calls)
    # The same intensities, so the same lean at every bar close: only the link differs.
    for s in later:
        assert lean_of(asymmetric.params, s.features) == lean_of(ordered.params, s.features)
    p = asymmetric.params
    assert p["link.w.up"] - p["link.w.flat"] > 0, "up against flat follows the lean"
    assert p["link.bias.down"] < min(p["link.bias.up"], p["link.bias.flat"]), "down is rare"
    assert asymmetric.version.startswith("poisson-asymmetric-20260918-")


def test_the_penalty_is_tuned_on_the_latest_days_with_intensities_that_never_saw_their_arrivals() -> None:
    window = hawkes_days(DAYS[:5])
    held_out = DAYS[4]

    tuning = tuned(window)

    assert tuning.held == [held_out], "whole days, about a fifth of the samples"
    assert set(tuning.losses) == set(PENALTIES) and tuning.losses[tuning.penalty] == min(
        tuning.losses.values()
    )
    earlier = [s for s in window if kst_day(s.bar_close) < held_out]
    assert tuning.intensities is not None and tuning.intensities.params == POISSON.fit(earlier).params
    # The arrivals of the held-out day (the four minutes after each of its bar closes) never reach
    # the tuning: swapped, the same penalty is chosen on the same losses.
    swapped = [
        Sample(
            s.symbol,
            s.bar_close,
            vector(
                buy_arrivals_1=s.features["sell_arrivals_1"], sell_arrivals_1=s.features["buy_arrivals_1"]
            ),
            None,
            None,
        )
        if kst_day(s.bar_close) == held_out and s.outcome is None
        else s
        for s in window
    ]
    assert POISSON.fit(swapped).params != POISSON.fit(window).params, "the swap would show"
    again = tuned(swapped)
    assert (again.penalty, again.held, again.losses) == (tuning.penalty, tuning.held, tuning.losses)
    # The model is then fitted on the whole window with the intensities given, before the day it
    # predicts: the held-out day is the window's, never the day predicted.
    model = VARIANT.fit(window, POISSON.fit(window))
    assert model.params["link.penalty"] == tuning.penalty
    assert model.params["link.validation_from"] == model.params["link.validation_to"] == 20260918.0
    assert model.trained_until == DAYS[4]


def test_too_few_samples_or_days_take_the_middle_penalty_unvalidated() -> None:
    one_day = hawkes_days(DAYS[:1], per_day=70)

    tuning = tuned(one_day)

    assert (tuning.penalty, tuning.held, tuning.losses, tuning.intensities) == (PENALTIES[2], [], {}, None)
    model = VARIANT.fit(one_day, POISSON.fit(one_day))
    assert model.params["link.validation_days"] == 0.0


def test_before_any_training_it_forecasts_as_poisson_s_defaults_and_without_a_side_nothing() -> None:
    default = VARIANT.default_model()
    s = hawkes_days(DAYS[:1], per_day=1)[0]

    assert default.version == "poisson-asymmetric-default"
    assert astuple(VARIANT.predict(s.features, default)) == pytest.approx(
        astuple(POISSON.predict(s.features, POISSON.default_model()))
    )
    unknown = vector()
    assert VARIANT.predict(unknown, default) == POISSON.predict(unknown, POISSON.default_model()) == UNKNOWN
    with pytest.raises(NothingToLearn, match="trades' sides"):
        VARIANT.fit([Sample(s.symbol, s.bar_close, unknown, UP, 0.0)], POISSON.default_model())


def mornings(day: date) -> list[Event]:
    """09:00–09:30: the mid swings six ticks up and down every two minutes, at its centre at each
    bar close, so the four minutes from a bar close reach both targets (k = 2 ticks): up; buyers
    take nine for every one sold (but at 09:29, whose four minutes see only the swing's last fall:
    down). From 09:30 to 10:30 it stands still (flat) and sellers take nine for every one bought.
    A book with the same sizes, a trade of each side every ten seconds."""
    events: list[Event] = []
    start = when = at("09:00:00", day)
    while when <= at("10:30:00", day):
        opening = when < at("09:30:00", day)
        swing = round(6 * math.sin(2 * math.pi * (when - start).total_seconds() / 120))
        mid = 81_250 + 100 * (swing if opening else 0)
        events += [
            book(when, mid - 50, mid + 50, bid_qty=100, ask_qty=100, deeper=(50, 50)),
            trade(when + timedelta(seconds=1), mid + 50, 9 if opening else 1, side="buy"),
            trade(when + timedelta(seconds=2), mid - 50, 1 if opening else 9, side="sell"),
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
    caplog.set_level("INFO", logger="krx_quant.poisson_link")

    result = poisson_link_experiment(s, algorithms=[Poisson()], kind_algorithms=[], kinds=[], now=AFTER)

    assert result.days == days
    first, *later = result.folds
    assert first.models == {
        "climate": "baseline-climate-default",
        "poisson": "poisson-default",
        "asymmetric": "poisson-asymmetric-default",
    }, "nothing before the first day"
    assert first.link is None and first.ordered == {"slope": 1.0, "cut": 1.0}
    for fold, before in zip(later, days, strict=False):
        assert all(f"-{before:%Y%m%d}-" in v for v in fold.models.values()), "trained up to the day before"
        assert fold.link is not None and fold.link["penalty"] in PENALTIES
    n = {arm.n for arm in result.arms.values()}
    assert n == {sum(f.scored for f in result.folds)} and n != {0}, "every arm on the same bar closes"
    happened = result.arms["climate"].happened
    assert happened[UP] > 0 and happened[FLAT] > happened[UP] and happened[DOWN] == len(days)
    # The buyers lean in the opening half hour, the sellers after it; up comes only with the buyers.
    # The ordered logistic cannot call up without calling down where the sellers lean: always flat.
    ordered, asymmetric = result.arms["poisson"], result.arms["asymmetric"]
    assert ordered.predicted[UP] == ordered.predicted[DOWN] == 0
    assert value(asymmetric.recall[UP]) > 0.5 and value(asymmetric.accuracy) > value(ordered.accuracy)
    confusion = result.confusion["asymmetric"]
    assert sum(sum(row.values()) for row in confusion.values()) == asymmetric.n
    assert confusion[UP][UP] == asymmetric.predicted[UP] - confusion[UP][FLAT] - confusion[UP][DOWN] > 0
    over = result.asymmetric_over_poisson
    assert over.pairs.value is not None and over.pairs.value > 0 and over.by_day is not None
    assert result.better and result.lower_bound_up and result.rule
    # The leans: on the buyers' side up comes, on the sellers' never.
    leans = result.leans
    assert leans.n == asymmetric.n - sum(f.unknown for f in result.folds)
    assert sum(b.n for b in leans.bins) == leans.n
    lowest, highest = leans.bins[0], leans.bins[-1]
    assert lowest.high < 0 < highest.low and lowest.happened[UP] == 0 < highest.happened[UP]
    # The last day's (c) model: the window's days before it, never its own.
    available = finished_days(s.data_dir, s.symbols, AFTER)
    trainer = _WalkForward(s, Poisson(), available, calendar_store(s).calendar, Leaders.of(s))
    window = trainer.samples_before(days[-1])
    assert {kst_day(x.bar_close) for x in window} == set(days[:-1])
    assert result.last_model.params == VARIANT.fit(window, trainer.before(days[-1])).params
    # Its memory a day (#383), and the report.
    logged = [r.getMessage() for r in caplog.records if ": memory on " in r.getMessage()]
    assert [m.split(": ")[1] for m in logged] == [
        f"memory on {d} (day {i} of 6)" for i, d in enumerate(days, 1)
    ]
    saved = json.loads((s.state_dir / "experiments" / "poisson-link-439.json").read_text())
    assert saved["days"] == [d.isoformat() for d in days]
    assert saved["rule"] == {"better": True, "lower_bound_up": True, "met": True}
    assert set(saved["arms"]) == {"climate", "poisson", "asymmetric"}
    assert saved["arms"]["asymmetric"]["by_class"]["up"]["recall"]["value"] == value(asymmetric.recall[UP])
    assert saved["arms"]["asymmetric"]["confusion"]["up"]["up"] == confusion[UP][UP]
    assert saved["lift"]["over_climate"]["poisson"]["by_day"]["n"] == len(days)
    assert saved["folds"][1]["link"] == later[0].link and saved["folds"][0]["link"] is None
    assert saved["leans"]["n"] == leans.n and len(saved["leans"]["bins"]) == len(leans.bins)
    text = "\n".join(lines(result, Path("state/experiments/poisson-link-439.json")))
    assert text.startswith("poisson link experiment (#439): 6 recorded days, 2026-09-14..2026-09-21\n")
    assert "-> met: the ordered link held poisson back" in text
    assert re.search(r"\(c\) over \(b\) +\+0\.\d{4} \(", text)
    assert not re.search(r"\b8[01][0-9],?[0-9]{3}\b", text), "no price"


def test_the_command_says_when_no_day_was_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KRX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KRX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("KRX_SYMBOLS", "005930")

    assert main(["poisson-link", "--days", "3"]) == 1

    assert "no recorded day of 005930" in capsys.readouterr().err
    assert not (tmp_path / "state" / poisson_link.REPORT).exists()


def test_the_job_runs_once_on_the_first_night_with_ten_recorded_days_after_every_pre_validation() -> None:
    job = POISSON_LINK
    assert (job.name, job.command, job.timeout) == ("poisson-link-439", ("poisson-link",), timedelta(hours=3))
    assert [t.issue for t in job.targets] == [439] and "시세 값이 없다" in job.targets[0].note
    assert "저절로 바꾸지 않는다" in job.targets[0].note
    assert [a.path for a in job.attach] == ["experiments/poisson-link-439.json"]
    after = max(i for i, j in enumerate(JOBS) if j.name.startswith(("prevalidate-", "simulate-")))
    assert JOBS.index(job) > after, "last: it holds back no simulation nor pre-validation"
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
