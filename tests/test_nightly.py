"""Nightly training through a local Object Store: data in, models, reports and the
promotion list out.

Input: archived one-minute bars and recorded events in the store, the models the server
runs (``live.json``) and registered algorithms (stand-ins whose learning is known, or
Granger). Output: the store's objects after a training run.
"""

from __future__ import annotations

import asyncio
import json
import math
import random
import statistics
from collections.abc import Sequence
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from krx_quant.algorithms import ALGORITHMS, Algorithm, Model, Sample
from krx_quant.algorithms.granger import Granger
from krx_quant.algorithms.markov import Markov
from krx_quant.archive import archive
from krx_quant.backfill import BAR_SCHEMA, MINUTE_BARS_TR
from krx_quant.cli import main
from krx_quant.clock import FakeClock
from krx_quant.engine import Event, PredictionEngine
from krx_quant.features import Leaders
from krx_quant.feeds import KST
from krx_quant.macro import MACRO, REGIME_SCHEMA
from krx_quant.market_calendar import MarketCalendar
from krx_quant.model_shelf import Shelf
from krx_quant.nightly import VALIDATION_DAYS, train_night
from krx_quant.object_store import LocalObjectStore
from krx_quant.predictions import PredictionStore
from krx_quant.pricing import Direction
from krx_quant.recorder import ParquetRecorder
from krx_quant.swap import morning_swap, publish_live
from krx_quant.training import training_day
from tests.algorithm_doubles import FrequencyAlgorithm
from tests.replay_events import at, book, every, index_tick, trade
from tests.synthetic_bars import trending
from tests.test_replay import busy_morning
from tests.test_replay_cli import record

FEE = 0.015 / 100
# Eight trading days: the last three validate, the four before them train (the stand-ins
# train on 4), and the first is too old to be used.
DAYS = [d for d in (date(2026, 9, 14) + timedelta(days=i) for i in range(12)) if d.weekday() < 5][:8]
NIGHT = DAYS[-1]
FINISHED = datetime.combine(NIGHT, time(21, 40), KST)  # when the run writes its list


def archive_bars(root: Path, day: date, symbol: str = "005930", ticks_per_minute: float = 1.0) -> None:
    """A rising (or falling) day of bars, where the archive puts it."""
    rows = [
        {
            "symbol": symbol,
            "bar_time": b.start,
            "venue": "UN",
            "open": b.open,
            "high": b.high,
            "low": b.low,
            "close": b.close,
            "volume": b.volume,
            "cum_value": 0,
        }
        for b in trending(day, ticks_per_minute, seed=day.day)
    ]
    path = root / "bars" / f"date={day}" / f"feed={MINUTE_BARS_TR}" / f"symbol={symbol}" / "bars.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=BAR_SCHEMA), path)


def archive_events(root: Path, day: date, events: list[Event], work: Path) -> None:
    """A recorded day of ``events``, stored where the archive puts it."""
    data = work / f"recorded-{day}"
    record(data, sorted(events, key=lambda e: e[1]["received_at"]), day=day)
    asyncio.run(archive(data, LocalObjectStore(root)))


def read(root: Path, key: str) -> Any:
    return json.loads((root / key).read_text())


@pytest.fixture
def store_root(tmp_path: Path) -> Path:
    root = tmp_path / "store"
    for day in DAYS:
        archive_bars(root, day)
    return root


def night(
    store_root: Path,
    tmp_path: Path,
    algorithms: Sequence[Algorithm],
    leaders: Leaders | None = None,
    commit: str = "unknown",
) -> Any:
    return train_night(
        LocalObjectStore(store_root),
        tmp_path / "cache",
        symbols=("005930",),
        fee_rate=FEE,
        algorithms=algorithms,
        leaders=leaders,
        now=lambda: FINISHED,
        commit=commit,
    )


def test_a_model_that_learned_is_promoted_and_one_that_did_not_is_not(
    store_root: Path, tmp_path: Path
) -> None:
    learner = FrequencyAlgorithm("learner", "velocity")
    backwards = FrequencyAlgorithm("backwards", "volatility", inverted=True)

    night(store_root, tmp_path, [learner, backwards])

    promotions = read(store_root, f"promotions/{NIGHT}.json")
    assert promotions["day"] == str(NIGHT)
    assert promotions["finalized_at"].startswith(f"{NIGHT}T21:40")
    [learned] = [v for k, v in promotions["promoted"].items() if k == "learner"]
    assert list(promotions["promoted"]) == ["learner"], "the backwards model lost to the default"
    assert read(store_root, f"models/learner/{learned}.json")["version"] == learned

    report = read(store_root, f"reports/{NIGHT}/learner.json")
    assert report["champion"]["version"] == "learner-default"
    assert report["challenger"]["version"] == learned
    assert report["challenger"]["loss"] < report["champion"]["loss"]
    assert report["validation_days"] == [str(d) for d in DAYS[-VALIDATION_DAYS:]]
    assert report["training_days"] == [str(d) for d in DAYS[-VALIDATION_DAYS - 4 : -VALIDATION_DAYS]]
    assert report["promoted"] is True
    lost = read(store_root, f"reports/{NIGHT}/backwards.json")
    assert lost["promoted"] is False and lost["challenger"]["loss"] > lost["champion"]["loss"]


def test_the_champion_is_the_model_the_server_runs(store_root: Path, tmp_path: Path) -> None:
    # The days turned: the last three fell. The server already runs a model that expects
    # falls; one learned on the rising days before cannot beat it.
    for day in DAYS[-VALIDATION_DAYS:]:
        archive_bars(store_root, day, ticks_per_minute=-1.0)
    falls = Model("learner-20260917-aaaaaa", {"up": 0.05, "flat": 0.15, "down": 0.8})
    (store_root / "models" / "learner").mkdir(parents=True)
    (store_root / "models" / "learner" / f"{falls.version}.json").write_text(falls.to_json())
    (store_root / "live.json").write_text(json.dumps({"learner": falls.version}))

    night(store_root, tmp_path, [FrequencyAlgorithm("learner", "velocity")])

    report = read(store_root, f"reports/{NIGHT}/learner.json")
    assert report["champion"]["version"] == falls.version
    assert report["challenger"]["loss"] > report["champion"]["loss"]
    assert read(store_root, f"promotions/{NIGHT}.json")["promoted"] == {}, "the live model stays"


def outcomes(tmp_path: Path, day: date) -> list[Direction]:
    """The validation outcomes of ``day``, from the samples training reads (the cache the
    night filled)."""
    found = training_day(
        tmp_path / "cache",
        day,
        MarketCalendar(dict.fromkeys(DAYS, True)),
        ("005930",),
        ["velocity"],
        FEE,
        Leaders(),
    )
    assert found is not None
    return [s.outcome for s in found.samples if s.outcome is not None]


def test_the_report_has_the_losses_by_day_and_the_paired_difference(store_root: Path, tmp_path: Path) -> None:
    night(store_root, tmp_path, [FrequencyAlgorithm("learner", "velocity")], commit="0123abc")

    report = read(store_root, f"reports/{NIGHT}/learner.json")
    params = read(store_root, f"models/learner/{report['challenger']['version']}.json")["params"]
    # The stand-in's forecast: its class frequencies, each at least 0.01, summing to one.
    floored = {d: max(params[d.value], 0.01) for d in Direction}
    forecast = {d: p / sum(floored.values()) for d, p in floored.items()}
    by_day, differences = {}, []
    for day in DAYS[-VALIDATION_DAYS:]:
        seen = outcomes(tmp_path, day)
        challenger = -sum(math.log(forecast[o]) for o in seen) / len(seen)
        by_day[str(day)] = {"samples": len(seen), "challenger": challenger, "champion": math.log(3)}
        differences.append((len(seen), challenger - math.log(3)))  # the default: a third each

    validation = report["validation"]
    assert list(validation["by_day"]) == list(by_day)
    for shown, row in by_day.items():
        assert validation["by_day"][shown] == pytest.approx(row)
    n = sum(k for k, _ in differences)
    assert validation["paired"] == pytest.approx(
        {
            "samples": n,
            "mean_difference": sum(k * d for k, d in differences) / n,
            "day_standard_error": statistics.stdev(d for _, d in differences) / math.sqrt(VALIDATION_DAYS),
        }
    )
    assert list(validation["by_reference"]) == ["close"], "backfilled days only"
    assert validation["by_reference"]["close"] == pytest.approx(
        {"samples": n, "challenger": report["challenger"]["loss"], "champion": math.log(3)}
    )
    assert report["champion"]["trained_on_validation"] is False
    assert report["commit"] == "0123abc"
    assert read(store_root, f"promotions/{NIGHT}.json")["promoted"] == {
        "learner": report["challenger"]["version"]
    }


def test_the_losses_by_reference_price_split_recorded_and_backfilled_days(
    store_root: Path, tmp_path: Path
) -> None:
    # The last two validation days were recorded (mornings only); the first is backfilled.
    for day in DAYS[-2:]:
        archive_events(store_root, day, busy_morning(day), tmp_path)

    night(store_root, tmp_path, [FrequencyAlgorithm("learner", "velocity")])

    validation = read(store_root, f"reports/{NIGHT}/learner.json")["validation"]
    by_day = validation["by_day"]
    for kind, days in (("close", DAYS[-3:-2]), ("mid", DAYS[-2:])):
        rows = [by_day[str(d)] for d in days]
        n = sum(r["samples"] for r in rows)
        expected = {
            "samples": n,
            "challenger": sum(r["samples"] * r["challenger"] for r in rows) / n,
            "champion": sum(r["samples"] * r["champion"] for r in rows) / n,
        }
        assert validation["by_reference"][kind] == pytest.approx(expected)
    assert by_day[str(DAYS[-1])]["samples"] == 56, "09:01–09:56 of a recorded morning"
    # Days of unequal size: the standard error is the sample-weighted mean's, clustered by day.
    sized = [(r["samples"], r["challenger"] - r["champion"]) for r in by_day.values()]
    n = sum(k for k, _ in sized)
    mean = sum(k * d for k, d in sized) / n
    clustered = math.sqrt(1.5 * sum((k * (d - mean)) ** 2 for k, d in sized)) / n
    assert validation["paired"]["mean_difference"] == pytest.approx(mean)
    assert validation["paired"]["day_standard_error"] == pytest.approx(clustered)


def test_a_champion_fitted_by_hand_on_the_validation_days_is_reported(
    store_root: Path, tmp_path: Path
) -> None:
    learner = FrequencyAlgorithm("learner", "velocity")
    validation = DAYS[-VALIDATION_DAYS:]
    calendar = MarketCalendar(dict.fromkeys(DAYS, True))
    night(store_root, tmp_path / "first", [learner])  # fills a cache to fit from
    samples = [
        s
        for d in validation
        if (
            found := training_day(
                tmp_path / "first" / "cache", d, calendar, ("005930",), ["velocity"], FEE, Leaders()
            )
        )
        for s in found.samples
    ]
    by_hand = learner.fit(samples)  # as `krx-quant fit` would, up to the latest day
    assert by_hand.trained_until == validation[-1]
    (store_root / "models" / "learner" / f"{by_hand.version}.json").write_text(by_hand.to_json())
    (store_root / "live.json").write_text(json.dumps({"learner": by_hand.version}))
    for stale in (store_root / "reports", store_root / "promotions"):
        for path in stale.rglob("*.json"):
            path.unlink()

    night(store_root, tmp_path, [learner])

    report = read(store_root, f"reports/{NIGHT}/learner.json")
    assert report["champion"]["version"] == by_hand.version
    assert report["champion"]["trained_on_validation"] is True
    assert "trained on these days" in report["reason"]


def test_a_checkpoint_an_older_version_wrote_is_trained_again(store_root: Path, tmp_path: Path) -> None:
    learner = FrequencyAlgorithm("learner", "velocity")
    older = {
        "algorithm": "learner",
        "day": str(NIGHT),
        "champion": None,
        "challenger": None,
        "promoted": False,
    }
    (store_root / "reports" / str(NIGHT)).mkdir(parents=True)
    (store_root / "reports" / str(NIGHT) / "learner.json").write_text(
        json.dumps({**older, "reason": "older"})
    )

    night(store_root, tmp_path, [learner], commit="0123abc")

    report = read(store_root, f"reports/{NIGHT}/learner.json")
    assert len(learner.fitted_on) == 1
    assert report["commit"] == "0123abc" and "validation" in report


class Reclaimed(RuntimeError):
    """The Spot instance went away mid-night."""


class CrashingOnce(FrequencyAlgorithm):
    def __init__(self, name: str, feature: str) -> None:
        super().__init__(name, feature)
        self.crash = True

    def fit(self, samples: Sequence[Sample]) -> Model:
        if self.crash:
            raise Reclaimed
        return super().fit(samples)


def test_a_night_cut_short_resumes_after_the_algorithms_it_finished(store_root: Path, tmp_path: Path) -> None:
    learner, second = FrequencyAlgorithm("learner", "velocity"), CrashingOnce("second", "volatility")

    with pytest.raises(Reclaimed):
        night(store_root, tmp_path, [learner, second])
    assert (store_root / f"reports/{NIGHT}/learner.json").exists()
    assert not (store_root / f"promotions/{NIGHT}.json").exists(), "not final until every algorithm is done"

    second.crash = False
    night(store_root, tmp_path, [learner, second])

    assert len(learner.fitted_on) == 1, "the finished algorithm is not trained again"
    assert len(second.fitted_on) == 1
    promotions = read(store_root, f"promotions/{NIGHT}.json")
    assert [r["algorithm"] for r in promotions["reports"]] == ["learner", "second"]
    assert night(store_root, tmp_path, [learner, second]).promoted == promotions["promoted"], (
        "a finished night"
    )
    assert len(learner.fitted_on) == 1


def test_the_train_command_trains_every_registered_algorithm_from_the_store(
    store_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KRX_OBJECT_STORE", str(store_root))
    monkeypatch.setenv("KRX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("KRX_SYMBOLS", "005930")
    monkeypatch.setenv("KRX_COMMIT", "0123abc")

    assert main(["train"]) == 0, capsys.readouterr().err

    promotions = read(store_root, f"promotions/{NIGHT}.json")
    assert [r["algorithm"] for r in promotions["reports"]] == [a.name for a in ALGORITHMS]
    assert {r["commit"] for r in promotions["reports"]} == {"0123abc"}, "the deploy's, as the engine's (#85)"
    assert f"{NIGHT} 밤 학습" in capsys.readouterr().out


async def test_a_promoted_model_runs_from_the_morning_and_defends_the_next_night(
    store_root: Path, tmp_path: Path
) -> None:
    learner = FrequencyAlgorithm("learner", "velocity")
    store = LocalObjectStore(store_root)
    first, morning = DAYS[-2], DAYS[-1]
    promoted = train_night(
        store,
        tmp_path / "cache",
        symbols=("005930",),
        fee_rate=FEE,
        algorithms=[learner],
        now=lambda: datetime.combine(first, time(21, 40), KST),
        day=first,
    ).promoted["learner"]
    calendar = MarketCalendar(dict.fromkeys(DAYS, True))
    engine = PredictionEngine(
        PredictionStore(tmp_path / "engine.db"),
        calendar,
        ("005930",),
        FEE,
        FakeClock(FINISHED),
        algorithms=[learner],
    )
    shelf = Shelf(tmp_path / "state")

    await morning_swap(store, shelf, engine, today=morning, at=datetime.combine(morning, time(8), KST))
    publish_live(store, shelf, engine.algorithms)
    night(store_root, tmp_path, [learner])

    assert engine.models["learner"].version == promoted
    assert read(store_root, f"reports/{NIGHT}/learner.json")["champion"]["version"] == promoted


def test_recorded_days_train_on_mid_points_and_the_report_says_which_days(
    store_root: Path, tmp_path: Path
) -> None:
    # The last training day and the validation days were recorded, the mornings only.
    for day in DAYS[-4:]:
        archive_events(store_root, day, busy_morning(day), tmp_path)
    learner = FrequencyAlgorithm("learner", "velocity")

    result = night(store_root, tmp_path, [learner])

    report = read(store_root, f"reports/{NIGHT}/learner.json")
    assert report["reference_prices"] == {
        **{str(d): "close" for d in DAYS[-7:-4]},
        **{str(d): "mid" for d in DAYS[-4:]},
    }
    # 09:01–09:56 of each recorded morning: the last book, at 09:59:48, counts for a minute.
    assert report["samples"]["validation"] == 3 * 56
    assert "녹화 중간가 4일, 백필 종가 3일" in result.summary()


def test_cached_days_no_night_needs_any_more_are_removed(store_root: Path, tmp_path: Path) -> None:
    stale = tmp_path / "cache" / "events" / "date=2026-01-02" / f"feed={MINUTE_BARS_TR}"
    stale.mkdir(parents=True)

    night(store_root, tmp_path, [FrequencyAlgorithm("learner", "velocity")])

    assert not stale.parent.exists()
    cached = sorted(p.name for p in (tmp_path / "cache" / "bars").glob("date=*"))
    assert cached == [f"date={d}" for d in DAYS[-7:]], "the four training and three validation days"


def test_a_day_with_one_symbols_bars_and_anothers_trades_is_not_trained_on(tmp_path: Path) -> None:
    # 000660's backfill failed on one day, which has only its recording; 005930 the opposite.
    root, symbols = tmp_path / "store", ("005930", "000660")
    mixed = DAYS[-4]
    for day in DAYS:
        archive_bars(root, day)
        if day != mixed:
            archive_bars(root, day, symbol="000660")
    archive_events(root, mixed, [trade(at("09:00:01", mixed), 200_000, symbol="000660")], tmp_path)

    def train(day: date | None = None) -> Any:
        return train_night(
            LocalObjectStore(root),
            tmp_path / "cache",
            symbols=symbols,
            fee_rate=FEE,
            algorithms=[FrequencyAlgorithm("learner", "velocity")],
            now=lambda: FINISHED,
            day=day,
        )

    with pytest.raises(FileNotFoundError, match="bars missing: 000660; trades missing: 005930"):
        train(mixed)
    train()
    report = read(root, f"reports/{NIGHT}/learner.json")
    assert str(mixed) not in report["training_days"]
    assert report["training_days"] == [str(d) for d in DAYS[:4]]


def leading_index_day(day: date) -> list[Event]:
    """A morning where the stock follows the electrical and electronics index: the index
    jumps half-way through each minute, and the stock the same way twenty seconds into the
    next.
    """
    steps = [random.Random(day.toordinal() * 100 + m).choice([-2, -1, 0, 1, 2]) for m in range(91)]

    def jumps(when: datetime, offset: timedelta) -> int:
        """The sum of the steps that happened by ``when``, each ``offset`` into its minute."""
        minutes = int((when - at("09:00:00", day) - offset).total_seconds() // 60) + 1
        return sum(steps[: max(0, minutes)])

    def stock(when: datetime) -> int:
        return 81_250 + 100 * jumps(when, timedelta(seconds=80))

    start, end = at("09:00:00", day), at("10:30:00", day)
    return [
        *every(start, end, 5, lambda w: index_tick(w, "0013", 3000 + 0.5 * jumps(w, timedelta(seconds=30)))),
        *every(start, end, 5, lambda w: book(w, stock(w) - 50, stock(w) + 50)),
        *every(start + timedelta(seconds=2), end, 10, lambda w: trade(w, stock(w) + 50)),
    ]


def test_granger_learns_from_the_indices_on_recorded_days(tmp_path: Path) -> None:
    root = tmp_path / "store"
    for day in DAYS:
        archive_events(root, day, leading_index_day(day), tmp_path)

    result = night(root, tmp_path, [Granger()], leaders=Leaders(indices=("0013",)))

    report = read(root, f"reports/{NIGHT}/granger.json")
    assert set(report["reference_prices"].values()) == {"mid"}, "recorded days, without any backfill"
    challenger = read(root, f"models/granger/{report['challenger']['version']}.json")
    assert challenger["params"]["lead_indices_1"] > 0
    assert result.promoted == {"granger": challenger["version"]}


def archive_regime(root: Path, day: date, regime: str, work: Path) -> None:
    """The day's macro regime as the collector records it at 07:30, archived."""
    data = work / f"macro-{day}"
    parquet = ParquetRecorder(data, flush_rows=1000, flush_seconds=3600)
    row = {
        "day": day,
        "recorded_at": datetime.combine(day, time(7, 30), KST),
        "regime": regime,
        "reasons": "[]",
        "failed": "{}",
        "skipped": "[]",
    }
    parquet.record(MACRO, day, "regime", "market", row, REGIME_SCHEMA)
    parquet.close()
    asyncio.run(archive(data, LocalObjectStore(root)))


def test_markov_learns_each_macro_regime_s_transitions_from_its_own_days(
    store_root: Path, tmp_path: Path
) -> None:
    for day in DAYS[::2]:
        archive_regime(store_root, day, "risk_on", tmp_path)

    night(store_root, tmp_path, [Markov()])

    report = read(store_root, f"reports/{NIGHT}/markov.json")
    params = read(store_root, f"models/markov/{report['challenger']['version']}.json")["params"]

    def rows(name: str) -> list[float]:
        return [round(params[f"{name}.{i}.{j}"], 9) for i in range(4) for j in range(4)]

    assert rows("shock") == rows("default"), "no shock day: the default transitions"
    assert rows("risk_on") != rows("default"), "learned from the risk_on days"
