"""Hit rates beside the nightly swap (#437).

Input: hand-made validated samples (two trading days, one recorded, one backfilled); nights trained
through a local Object Store with stand-ins whose forecasts are fixed (rising days: almost every
outcome is up), one whose log loss is lower and whose hit rate is lower, one the other way round;
promotion lists in a local store. Output: each report's ``hit_rate``, worked out by hand below; the
verdicts, still the log loss's; the Telegram summary's lines; the summary over nights, its command,
and the scheduled job that posts it to #437.
"""

from __future__ import annotations

import asyncio
import json
import math
import statistics
from collections.abc import Sequence
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from krx_quant.algorithms import Algorithm, Model, Sample
from krx_quant.algorithms.contract import KindSample
from krx_quant.baselines import Forecast
from krx_quant.cli import main
from krx_quant.clock import FakeClock
from krx_quant.config import Settings
from krx_quant.features import FeatureVector
from krx_quant.feeds import KST
from krx_quant.github import GitHub
from krx_quant.kinds import DIRECTION_KIND, Answer
from krx_quant.nightly import VALIDATION_DAYS, train_night
from krx_quant.nightly_hit_rates import (
    HIT_RATE_NIGHTS,
    KEEP,
    PROMOTE,
    SCANNED,
    compare,
    compared_nights,
    lines,
    nights_with_hit_rates,
)
from krx_quant.object_store import LocalObjectStore
from krx_quant.pricing import Direction
from krx_quant.ranges import RANGE
from krx_quant.scheduled import (
    FAILED,
    INTERRUPTED,
    JOBS,
    NIGHTLY_HIT_RATES,
    OK,
    STATE_FILE,
    JobRecord,
    JobState,
    Night,
    Scheduler,
)
from krx_quant.stats import t_quantile
from krx_quant.touch import TARGET_VERSION
from tests.algorithm_doubles import FrequencyAlgorithm, stand_in_card, stand_in_meanings
from tests.fake_github import REPO, TOKEN, fake_github
from tests.kind_doubles import TEST_KIND, DriftFrequency
from tests.nightly_doubles import DAYS, FEE, FINISHED, NIGHT, archive_bars, read
from tests.scheduled_doubles import AT, CALENDAR, COMMIT, FRIDAY, MONDAY, SATURDAY, Told, at, faking
from tests.test_nightly import night, outcomes

UP, FLAT, DOWN = Direction.UP, Direction.FLAT, Direction.DOWN
D1, D2 = date(2026, 10, 26), date(2026, 10, 27)
SYMBOL = "005930"
NO_FEATURES = FeatureVector({}, ())


def sample(day: date, minute: int, outcome: Direction) -> Sample:
    return Sample(SYMBOL, datetime.combine(day, time(9, minute), KST), NO_FEATURES, outcome, None)


def calls(samples: Sequence[Sample | KindSample], called: Sequence[str]) -> list[tuple[Any, float, str]]:
    return [(s, 0.5, c) for s, c in zip(samples, called, strict=True)]


# Day 1, recorded: the challenger right twice, the champion once (each pair's gain 1, 0, 0, 0).
# Day 2, backfilled: both right twice (gains 0, 1, -1), and a bar close the champion did not predict.
DAY1 = [sample(D1, 1, UP), sample(D1, 2, UP), sample(D1, 3, FLAT), sample(D1, 4, DOWN)]
DAY2 = [sample(D2, 1, DOWN), sample(D2, 2, UP), sample(D2, 3, FLAT)]
UNPAIRED = sample(D2, 4, UP)
CHALLENGER = calls([*DAY1, *DAY2, UNPAIRED], ["up", "flat", "flat", "up", "down", "up", "up", "up"])
CHAMPION = calls([*DAY1, *DAY2], ["flat", "flat", "flat", "flat", "down", "flat", "flat"])
REFERENCES = {D1: "mid", D2: "close"}


def test_the_two_models_are_compared_on_the_bar_closes_both_predicted() -> None:
    found = compare(CHALLENGER, CHAMPION, REFERENCES, DIRECTION_KIND, True)

    assert found is not None
    assert (found["samples"], found["days"]) == (7, 2), "the bar close the champion did not predict is out"
    assert found["challenger"] == {
        "hit_rate": 4 / 7,
        "coverage": 5 / 7,  # up and down calls
        "confusion": {
            "up": {"up": 2, "flat": 1, "down": 1},
            "flat": {"up": 1, "flat": 1, "down": 0},
            "down": {"up": 0, "flat": 0, "down": 1},
        },
    }
    assert found["champion"] == {
        "hit_rate": 3 / 7,
        "coverage": 1 / 7,
        "confusion": {
            "up": {"up": 0, "flat": 0, "down": 0},
            "flat": {"up": 3, "flat": 2, "down": 1},
            "down": {"up": 0, "flat": 0, "down": 1},
        },
    }
    assert found["difference"] == pytest.approx(1 / 7)
    # A trading day at a time: the days' mean gains 1/4 and 0, a Student t with one degree of freedom.
    half = t_quantile(0.975, 1) * statistics.stdev([0.25, 0.0]) / math.sqrt(2)
    assert found["by_trading_day"] == pytest.approx(
        {"days": 2, "mean": 0.125, "low": 0.125 - half, "high": 0.125 + half}
    )
    assert list(found["by_reference"]) == ["close", "mid"]
    mid, close = found["by_reference"]["mid"], found["by_reference"]["close"]
    assert (mid["samples"], mid["days"], mid["difference"]) == (4, 1, 0.25)
    assert (mid["challenger"]["hit_rate"], mid["champion"]["hit_rate"]) == (0.5, 0.25)
    assert mid["by_trading_day"] is None, "one day: no interval"
    assert (close["samples"], close["difference"]) == (3, 0.0)
    assert (found["log_loss_rule"], found["hit_rate_rule"], found["mismatch"]) == (PROMOTE, PROMOTE, False)


def test_a_promotion_whose_hit_rate_fell_is_a_mismatch_and_a_tie_keeps() -> None:
    swapped = compare(CHAMPION, CHALLENGER, REFERENCES, DIRECTION_KIND, True)
    assert swapped is not None and swapped["difference"] == pytest.approx(-1 / 7)
    assert (swapped["log_loss_rule"], swapped["hit_rate_rule"], swapped["mismatch"]) == (PROMOTE, KEEP, True)

    kept = compare(CHAMPION, CHALLENGER, REFERENCES, DIRECTION_KIND, False)
    assert kept is not None and (kept["log_loss_rule"], kept["mismatch"]) == (KEEP, False)

    tie = compare(CHAMPION, CHAMPION, REFERENCES, DIRECTION_KIND, True)
    assert tie is not None and tie["difference"] == 0.0
    assert (tie["hit_rate_rule"], tie["mismatch"]) == (KEEP, False), "no hit gained: the champion stays"

    assert compare(CHALLENGER, [], REFERENCES, DIRECTION_KIND, True) is None, "no pair"


def test_a_kind_s_models_are_compared_on_its_classes_and_action_classes() -> None:
    labels = ["small", "large", "medium"]
    samples = [
        KindSample(SYMBOL, datetime.combine(D1, time(9, i + 1), KST), NO_FEATURES, Answer(label), 1)
        for i, label in enumerate(labels)
    ]
    found = compare(
        calls(samples, ["small", "small", "medium"]), calls(samples, ["medium"] * 3), {}, RANGE, False
    )

    assert found is not None
    assert found["challenger"]["hit_rate"] == 2 / 3 and found["champion"]["hit_rate"] == 1 / 3
    assert found["challenger"]["coverage"] == 2 / 3, "the range's action classes: small and large"
    assert found["challenger"]["confusion"]["small"] == {"small": 1, "medium": 0, "large": 1}
    assert list(found["by_reference"]) == ["None"], "a day of no known reference, as the losses' groups"
    assert found["hit_rate_rule"] == PROMOTE and found["log_loss_rule"] == KEEP


# Through the night


class Constant:
    """Forecasts its model's three probabilities at every bar close; fitted, its model is
    ``trained``'s, whatever the samples."""

    training_days = 4

    def __init__(self, name: str, trained: dict[str, float]) -> None:
        self.name = name
        self.features: tuple[str, ...] = ("velocity",)
        self.card = stand_in_card(name, "fixed probabilities", ("velocity",), "nothing")
        self.param_meanings = stand_in_meanings("up", "flat", "down")
        self._trained = trained

    def default_model(self) -> Model:
        return Model(f"{self.name}-default", {"up": 1 / 3, "flat": 1 / 3, "down": 1 / 3})

    def fit(self, samples: Sequence[Sample]) -> Model:
        scored = [s for s in samples if s.outcome is not None]
        return Model.fitted(self.name, dict(self._trained), scored, 0.0)

    def predict(self, features: FeatureVector, model: Model) -> Forecast:
        return Forecast(model.params["up"], model.params["flat"], model.params["down"], 0.0)


# Almost every validation outcome of the rising days is up. Calling up, just, costs about ln 3 a bar
# close; calling flat by a hair with up close behind costs about ln 2: a lower log loss, and almost
# every call wrong.
CALLS_UP = {"up": 0.34, "flat": 0.33, "down": 0.33}
CALLS_FLAT = {"up": 0.49, "flat": 0.50, "down": 0.01}


def runs(root: Path, name: str, params: dict[str, float]) -> Model:
    """The server runs ``params`` for ``name``: the night's champion."""
    model = Model(f"{name}-20260917-aaaaaa", params)
    (root / "models" / name).mkdir(parents=True)
    (root / "models" / name / f"{model.version}.json").write_text(model.to_json())
    (root / "live.json").write_text(json.dumps({name: model.version}))
    return model


@pytest.fixture
def root(tmp_path: Path) -> Path:
    found = tmp_path / "store"
    for day in DAYS:
        archive_bars(found, day)
    return found


def shares(tmp_path: Path) -> dict[Direction, float]:
    """The validation outcomes' shares, of the samples the night read."""
    seen = [o for day in DAYS[-VALIDATION_DAYS:] for o in outcomes(tmp_path, day)]
    return {d: seen.count(d) / len(seen) for d in Direction}


def test_a_challenger_of_a_lower_log_loss_and_a_lower_hit_rate_is_promoted_and_flagged(
    root: Path, tmp_path: Path
) -> None:
    champion = runs(root, "learner", CALLS_UP)

    result = night(root, tmp_path, [Constant("learner", CALLS_FLAT)])

    report = read(root, f"reports/{NIGHT}/learner.json")
    loss, theirs = report["challenger"]["loss"], report["champion"]["loss"]
    assert loss < theirs, "the log loss prefers the challenger"
    assert (report["promoted"], report["forced"]) == (True, False), "promoted: the rule is unchanged"
    assert report["reason"] == f"{loss:.4f} < {theirs:.4f} ({champion.version})"
    assert result.promoted == {"learner": report["challenger"]["version"]}
    found = report["hit_rate"]
    happened = shares(tmp_path)
    assert found["samples"] == report["validation"]["paired"]["samples"]
    assert found["challenger"]["hit_rate"] == pytest.approx(happened[FLAT])
    assert found["champion"]["hit_rate"] == pytest.approx(happened[UP])
    assert found["challenger"]["coverage"] == 0.0 and found["champion"]["coverage"] == 1.0
    assert sum(found["challenger"]["confusion"]["flat"].values()) == found["samples"], "it calls flat only"
    assert found["difference"] < 0 and found["by_trading_day"]["days"] == VALIDATION_DAYS
    assert list(found["by_reference"]) == ["close"], "backfilled days only"
    assert (found["log_loss_rule"], found["hit_rate_rule"], found["mismatch"]) == (PROMOTE, KEEP, True)
    [line] = [x for x in result.summary().splitlines() if x.startswith("- learner")]
    assert line == (
        f"- learner 승격: {report['reason']} · 맞힘률 {happened[UP]:.1%} → {happened[FLAT]:.1%}"
        " (불일치: 로그 손실로는 승격, 맞힘률은 떨어짐)"
    )


def test_a_challenger_of_a_higher_hit_rate_and_a_higher_log_loss_is_kept(root: Path, tmp_path: Path) -> None:
    runs(root, "learner", CALLS_FLAT)

    result = night(root, tmp_path, [Constant("learner", CALLS_UP)])

    report = read(root, f"reports/{NIGHT}/learner.json")
    assert report["challenger"]["loss"] > report["champion"]["loss"]
    assert (report["promoted"], report["forced"]) == (False, False), "kept: the rule is unchanged"
    assert result.promoted == {}
    found = report["hit_rate"]
    assert found["difference"] > 0
    assert (found["log_loss_rule"], found["hit_rate_rule"], found["mismatch"]) == (KEEP, PROMOTE, False)
    [line] = [x for x in result.summary().splitlines() if x.startswith("- learner")]
    assert line.startswith("- learner 유지: ") and line.endswith(" (맞힘률 우선이면 승격)")


def test_the_verdict_is_the_log_loss_s_whatever_the_hit_rates(root: Path, tmp_path: Path) -> None:
    """Input: stand-ins the two rules agree and part on. Output: each is promoted exactly when its
    log loss is lower; the hit-rate rule is reported beside it."""
    algorithms: list[Algorithm] = [
        FrequencyAlgorithm("learner", "velocity"),
        FrequencyAlgorithm("backwards", "volatility", inverted=True),
        Constant("flat", CALLS_FLAT),
        Constant("up", CALLS_UP),
    ]

    result = night(root, tmp_path, algorithms)

    rules = {}
    for a in algorithms:
        report = read(root, f"reports/{NIGHT}/{a.name}.json")
        better = report["challenger"]["loss"] < report["champion"]["loss"]
        assert report["promoted"] is better and report["forced"] is False, a.name
        assert report["hit_rate"]["log_loss_rule"] == (PROMOTE if better else KEEP)
        rules[a.name] = (report["hit_rate"]["log_loss_rule"], report["hit_rate"]["hit_rate_rule"])
    assert rules == {
        "learner": (PROMOTE, PROMOTE),  # it calls up; the defaults' tie calls flat
        "backwards": (KEEP, KEEP),
        "flat": (PROMOTE, KEEP),  # ln 2 against the defaults' ln 3, calling flat as their tie does
        "up": (PROMOTE, PROMOTE),
    }
    assert sorted(result.promoted) == ["flat", "learner", "up"]


def test_a_kind_s_report_compares_its_classes(tmp_path: Path) -> None:
    root = tmp_path / "store"
    for day in DAYS:
        archive_bars(root, day)

    train_night(
        LocalObjectStore(root),
        tmp_path / "cache",
        symbols=(SYMBOL,),
        fee_rate=FEE,
        algorithms=[],
        now=lambda: FINISHED,
        target_version=TARGET_VERSION,
        kind_algorithms=[DriftFrequency("counter", "velocity")],
        kinds=(TEST_KIND,),
    )

    found = read(root, f"reports/{NIGHT}/counter.json")["hit_rate"]
    assert list(found["challenger"]["confusion"]) == list(TEST_KIND.classes)
    assert found["samples"] == sum(n for row in found["champion"]["confusion"].values() for n in row.values())


# Over nights


def compared(difference: float, promoted: bool, *, days: int = 3) -> dict[str, Any]:
    """A report's ``hit_rate`` of ``difference``: champion 0.40, challenger 0.40 + difference."""
    by_day = {"days": days, "mean": difference, "low": difference - 0.01, "high": difference + 0.005}
    row = {
        "samples": 100,
        "days": days,
        "challenger": {"hit_rate": 0.40 + difference, "coverage": 0.2, "confusion": {}},
        "champion": {"hit_rate": 0.40, "coverage": 0.1, "confusion": {}},
        "difference": difference,
        "by_trading_day": by_day,
    }
    return {
        **row,
        "by_reference": {"mid": row},
        "log_loss_rule": PROMOTE if promoted else KEEP,
        "hit_rate_rule": PROMOTE if difference > 0 else KEEP,
        "mismatch": promoted and difference < 0,
    }


def store_nights(root: Path, nights: dict[date, list[dict[str, Any]]]) -> None:
    for day, reports in nights.items():
        path = root / "promotions" / f"{day}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"day": str(day), "finalized_at": "", "promoted": {}, "reports": reports}))


def nights_of(first: date, count: int) -> list[date]:
    days: list[date] = []
    day = first
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


# Two nights before the deploy (no hit rates), then twelve: newton compared every night (promoted on
# four, its hit rate fell on two of them), mandelbrot's range on one, a climate table never.
OLD, NEW = nights_of(date(2026, 10, 29), 2), nights_of(date(2026, 11, 2), 12)


def stored_nights(root: Path) -> None:
    nights: dict[date, list[dict[str, Any]]] = {d: [{"algorithm": "newton", "promoted": False}] for d in OLD}
    for i, day in enumerate(NEW):
        newton = compared(
            [-0.02, 0.01, -0.03, 0.0, 0.02, 0.0, 0.01, 0.0, -0.01, 0.03, 0.0, 0.01][i],
            promoted=i in (2, 4, 8, 9),
        )
        reports = [
            {"algorithm": "newton", "promoted": i in (2, 4, 8, 9), "hit_rate": newton},
            {"algorithm": "baseline-climate", "promoted": True},
        ]
        if i == 11:
            reports.append(
                {"algorithm": "mandelbrot", "kind": "range", "forced": True, "hit_rate": compared(0.05, True)}
            )
        nights[day] = reports
    store_nights(root, nights)


def test_the_latest_nights_with_hit_rates_are_read_oldest_first(tmp_path: Path) -> None:
    stored_nights(tmp_path)
    store = LocalObjectStore(tmp_path)

    found = compared_nights(store, HIT_RATE_NIGHTS)

    assert [day for day, _ in found] == NEW[-HIT_RATE_NIGHTS:]
    assert [r["algorithm"] for r in found[-1][1]] == ["newton", "mandelbrot"], "the reports with hit rates"
    assert [day for day, _ in compared_nights(store, 20)] == NEW, "the nights before the deploy have none"
    assert len(compared_nights(store, 20, scanned=5)) == 5, "of the latest lists read"


def test_the_condition_counts_the_latest_lists_and_survives_a_store_it_cannot_read(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    assert SCANNED == 2 * HIT_RATE_NIGHTS
    assert nights_with_hit_rates(Settings("key", "secret")) == 0, "no store"
    settings = Settings("key", "secret", object_store=str(tmp_path))
    store_nights(tmp_path, {d: [{"algorithm": "newton", "hit_rate": compared(0.0, False)}] for d in NEW[:9]})
    assert nights_with_hit_rates(settings) == 9
    store_nights(tmp_path, {NEW[9]: [{"algorithm": "newton", "hit_rate": compared(0.0, False)}]})
    assert nights_with_hit_rates(settings) == HIT_RATE_NIGHTS
    (tmp_path / "promotions" / f"{NEW[10]}.json").write_text("{not json")
    assert nights_with_hit_rates(settings) == 0
    assert "could not count the nights with hit rates" in caplog.text


def test_the_summary_counts_each_algorithm_s_mismatches_and_parted_rules(tmp_path: Path) -> None:
    stored_nights(tmp_path)

    out = lines(compared_nights(LocalObjectStore(tmp_path), HIT_RATE_NIGHTS), HIT_RATE_NIGHTS)

    text = "\n".join(out)
    assert out[0] == f"nightly hit rates (#437): 10 of the 10 nights asked for, {NEW[2]}..{NEW[-1]}"
    # newton on the latest ten: differences -0.03 (promoted), 0, +0.02 (promoted), 0, +0.01, 0,
    # -0.01 (promoted), +0.03 (promoted), 0, +0.01.
    assert (
        "all algorithms: 11 comparisons; the log loss promoted 5, of which the hit rate fell on 2 (mismatch);"
        " the hit-rate rule would have promoted 5; the rules parted on 4"
    ) in text
    newton = out[out.index("") + 1 :][out[out.index("") + 1 :].index("") + 1 :]
    assert newton[0] == (
        "newton (direction): 10 nights, 1000 pairs; the log loss promoted 4, of which the hit rate fell on 2"
        " (mismatch; 2 with the trading-day interval below 0); the hit-rate rule would have promoted 4;"
        " the rules parted on 4; forced 0"
    )
    assert newton[1] == (
        "  hit rate (each night's on its pairs, the nights' mean): champion 0.4000, challenger 0.4030;"
        " difference +0.0030, on the nights the log loss promoted +0.0025"
    )
    assert newton[2] == "  coverage: champion 0.1000, challenger 0.2000"
    assert newton[3] == "  difference by reference price: mid +0.0030 (10 nights, 1000 pairs)"
    assert newton[4] == (
        f"  mismatch {NEW[2]}: -0.0300 (by trading day -0.0300, 95% -0.0400..-0.0250, 3 days)"
    )
    assert newton[5].startswith(f"  mismatch {NEW[8]}: -0.0100")
    assert "mandelbrot (range): 1 nights, 100 pairs; the log loss promoted 1" in text and "forced 1" in text
    assert "baseline-climate" not in text, "never compared"
    assert lines([], 10) == ["nightly hit rates (#437): no night's promotion list carries the hit rates yet"]


def test_the_command_prints_the_summary_from_the_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)  # no .env
    monkeypatch.delenv("KRX_OBJECT_STORE", raising=False)
    assert main(["nightly-hit-rates"]) == 1
    assert "KRX_OBJECT_STORE is not set" in capsys.readouterr().err

    monkeypatch.setenv("KRX_OBJECT_STORE", str(tmp_path / "store"))
    assert main(["nightly-hit-rates"]) == 1, "no night carries them yet"
    assert "no night's promotion list carries the hit rates yet" in capsys.readouterr().out

    stored_nights(tmp_path / "store")
    assert main(["nightly-hit-rates", "--nights", "3"]) == 0
    assert capsys.readouterr().out.startswith(
        f"nightly hit rates (#437): 3 of the 3 nights asked for, {NEW[-3]}..{NEW[-1]}\n"
    )


# The scheduled job


def test_the_job_runs_once_on_the_first_night_ten_lists_carry_the_hit_rates() -> None:
    job = NIGHTLY_HIT_RATES
    assert (job.name, job.command, job.timeout) == (
        "nightly-hit-rates-437",
        ("nightly-hit-rates", "--nights", "10"),
        timedelta(minutes=15),
    )
    assert [t.issue for t in job.targets] == [437] and job.label == ("ready-for-agent",)
    note = job.targets[0].note
    assert "교체 규칙" in note and "사용자가 정한다" in note and "가격과 시세 값이 없다" in note
    first = next(i for i, j in enumerate(JOBS) if j.name.startswith(("prevalidate-", "simulate-")))
    assert JOBS.index(job) < first, "before the simulations and pre-validations"
    assert job.gives_way_to is None and job.session is None
    ran = JobRecord(SATURDAY, OK, at(SATURDAY, "22:30"), COMMIT, 0, 5.0)

    def asked(day: date, nights: int, record: JobRecord | None = None, commit: str = COMMIT) -> bool:
        return job.due(Night(day, CALENDAR.is_trading_day(day), commit, record, {}, nights))

    assert not asked(FRIDAY, HIT_RATE_NIGHTS - 1), "too few nights"
    assert asked(FRIDAY, HIT_RATE_NIGHTS) and asked(SATURDAY, HIT_RATE_NIGHTS), "trading or not"
    assert not asked(MONDAY, HIT_RATE_NIGHTS, ran), "once"
    assert asked(MONDAY, HIT_RATE_NIGHTS, replace(ran, outcome=INTERRUPTED)), "again after a restart cut it"
    failed = replace(ran, outcome=FAILED, exit_code=1)
    assert not asked(MONDAY, HIT_RATE_NIGHTS, failed), "a failure is not run again on the same code"
    assert asked(MONDAY, HIT_RATE_NIGHTS, failed, "fedcba"), "but again once a deploy brings other code"


async def test_the_job_runs_the_real_command_and_posts_to_437(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # no .env
    monkeypatch.setenv("KRX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KRX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("KRX_OBJECT_STORE", str(tmp_path / "store"))
    stored_nights(tmp_path / "store")
    state = JobState.load(tmp_path / STATE_FILE)

    async with fake_github() as hub, aiohttp.ClientSession() as session:
        scheduler = Scheduler(
            faking([NIGHTLY_HIT_RATES], {NIGHTLY_HIT_RATES.facts: HIT_RATE_NIGHTS}),
            AT,
            CALENDAR,
            state,
            GitHub(session, TOKEN, REPO, hub.url),
            Told(),
            FakeClock(at(FRIDAY, "23:00")),
            commit=COMMIT,
        )
        await asyncio.wait_for(scheduler.run_night(FRIDAY), 25)
    [body] = hub.bodies(437)
    assert f"nightly hit rates (#437): 10 of the 10 nights asked for, {NEW[2]}..{NEW[-1]}" in body
    assert "사용자가 정한다" in body
    assert state.jobs["nightly-hit-rates-437"].exit_code == 0
