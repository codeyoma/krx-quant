"""The daily kinds in the engine (#264): predicted once a trading day at 08:50 from the KRX days
before, answered once the day's KRX day candle is stored, judged by their own days."""

from __future__ import annotations

import random
import time as timer
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest

from krx_quant.algorithms import Model
from krx_quant.cards import Card, Meaning
from krx_quant.clock import FakeClock
from krx_quant.daily import (
    NO_CANDLE,
    DailyAlgorithm,
    DailyRegistrationError,
    DailySample,
    DayPrices,
    register_daily_algorithms,
)
from krx_quant.engine import Budget, PredictionEngine
from krx_quant.features import FeatureVector
from krx_quant.feeds.base import KST
from krx_quant.kind_registry import DAILY_KINDS
from krx_quant.kinds import DIRECTION_KIND, Answer, KindForecast
from krx_quant.lifecycle import Status
from krx_quant.market_calendar import MarketCalendar
from krx_quant.open_to_close import CLIMATE, FLAT_NAME, OPEN_TO_CLOSE, PERSIST, answers
from krx_quant.predictions import NewKindPrediction, PredictionStore
from krx_quant.replay import replay_into
from krx_quant.special import Kind as SpecialKind
from krx_quant.special import Window

SYMBOLS = ("005930", "000660")
FEE = 0.015 / 100
MONDAY = date(2026, 10, 12)
THURSDAY = date(2026, 10, 8)  # the trading day before: Friday the 9th is Hangul Day
CALENDAR = MarketCalendar({date(2026, 10, 9): False})


def at(day: date, hhmm: str) -> datetime:
    return datetime.combine(day, time.fromisoformat(hhmm), KST)


def history(last: date, n: int, seed: int) -> list[DayPrices]:
    """``n`` made-up KRX days ending on ``last``, on the calendar's trading days."""
    days: list[date] = []
    day = last
    while len(days) < n:
        if CALENDAR.is_trading_day(day):
            days.append(day)
        day -= timedelta(days=1)
    rng = random.Random(seed)
    out, price = [], 50_000.0
    for d in reversed(days):
        opened = price * (1 + rng.gauss(0, 0.01))
        close = opened * (1 + rng.gauss(0, 0.015))
        out.append(DayPrices(d, opened, max(opened, close), min(opened, close), close, 1_000))
        price = close
    return out


def the_day(prices: Sequence[DayPrices], day: date, change: float) -> DayPrices:
    opened = prices[-1].close
    return DayPrices(day, opened, opened * 1.03, opened * 0.97, opened * (1 + change), 1_000)


class Source:
    def __init__(self, days: Mapping[str, list[DayPrices]]) -> None:
        self.stored = {s: list(d) for s, d in days.items()}

    def days(self, symbol: str) -> list[DayPrices]:
        return list(self.stored.get(symbol, []))


def engine(
    db: Path,
    clock: FakeClock,
    source: Source,
    algorithms: Sequence[DailyAlgorithm] = (),
    budget: Budget | None = None,
) -> PredictionEngine:
    return PredictionEngine(
        PredictionStore(db),
        CALENDAR,
        SYMBOLS,
        FEE,
        clock,
        algorithms=(),
        kinds=(DIRECTION_KIND,),
        kind_algorithms=(),
        daily=source,
        daily_algorithms=algorithms,
        daily_features=features,
        daily_feature_names={"last_change"},
        budget=budget,
    )


def features(
    algorithm: DailyAlgorithm, symbol: str, day: date, past: Sequence[DayPrices], band: Any
) -> FeatureVector:
    return FeatureVector({"last_change": past[-1].change if past else None}, algorithm.features)


class Leaning:
    """A made-up daily algorithm: up after an up day, down after a down day."""

    training_days = 250
    param_meanings: Mapping[str, Meaning] = {}

    def __init__(
        self,
        name: str = "leaning",
        own: str = "last_change",
        *,
        fail: bool = False,
        sleep: float = 0.0,
        kind: str = "open_to_close",
    ) -> None:
        self.name = name
        self.kind = kind
        self.features: tuple[str, ...] = (own,)
        self.card = Card("Test", "test", "test", (own,), "test", "test", "test")
        self.fail = fail
        self.sleep = sleep

    def default_model(self) -> Model:
        return Model(f"{self.name}-v1", {}, target_version=1)

    def fit(self, samples: Sequence[DailySample]) -> Model:
        return self.default_model()

    def predict(self, features: FeatureVector, model: Model) -> KindForecast:
        if self.fail:
            raise RuntimeError("broken")
        if self.sleep:
            timer.sleep(self.sleep)
        last = features[self.features[0]]
        if last is None:
            return KindForecast((1 / 3, 1 / 3, 1 / 3))
        return KindForecast((0.6, 0.2, 0.2) if last > 0 else (0.2, 0.2, 0.6))


def predictions(store: PredictionStore) -> list[dict[str, Any]]:
    out = []
    for symbol in SYMBOLS:
        out += store.kind_recent(OPEN_TO_CLOSE.name, 1, symbol, 100)
    return sorted(out, key=lambda r: (r["bar_close"], r["symbol"], r["algorithm"]))


async def test_a_day_is_predicted_at_0850_and_answered_once_its_krx_day_candle_is_stored(
    tmp_path: Path,
) -> None:
    pasts = {s: history(THURSDAY, 700, seed=i) for i, s in enumerate(SYMBOLS)}
    source = Source(pasts)
    clock = FakeClock(at(MONDAY, "07:20"))
    e = engine(tmp_path / "e.sqlite3", clock, source)
    assert e.daily_kinds == DAILY_KINDS

    await replay_into(e, clock, [], until=at(MONDAY, "08:51"))
    made = predictions(e.store)
    assert {(r["symbol"], r["algorithm"]) for r in made} == {
        (s, b) for s in SYMBOLS for b in (FLAT_NAME, CLIMATE, PERSIST)
    }
    assert {r["bar_close"] for r in made} == {"2026-10-12T08:50:00+09:00"}
    assert {r["status"] for r in made} == {"ok"}
    for r in made:
        expected = OPEN_TO_CLOSE.band(pasts[r["symbol"]])  # type: ignore[misc]
        assert expected is not None and r["band"] == pytest.approx(expected.value)
        assert (r["flat_ticks"], r["width_ticks"], r["anchor"], r["tick"]) == (None, None, None, None)
    persisted = {r["symbol"]: r["output"] for r in made if r["algorithm"] == PERSIST}
    for symbol in SYMBOLS:
        thursday = answers(pasts[symbol], FEE)[-1].label
        assert persisted[symbol] == {f"p_{c}": float(c == thursday) for c in OPEN_TO_CLOSE.classes}

    # The day's candle is not stored at 20:35: no answer yet.
    await replay_into(e, clock, [], until=at(MONDAY, "20:40"))
    assert e.store.kind_answers(OPEN_TO_CLOSE.name) == []
    source.stored["005930"].append(the_day(pasts["005930"], MONDAY, 0.05))
    await replay_into(e, clock, [], until=at(MONDAY, "20:50"))
    [answered] = e.store.kind_answers(OPEN_TO_CLOSE.name)
    assert (answered["symbol"], answered["bar_close"], answered["class"]) == (
        "005930",
        "2026-10-12T08:50:00+09:00",
        "up",
    )
    assert answered["scored_at"] == "2026-10-12T20:45:00+09:00", "the first look after it was stored"
    assert answered["path"]["source"] == "krx_day_candle"
    assert answered["path"]["change"] == pytest.approx(0.05)
    assert (answered["path"]["vi"], answered["special"]) == (0, False)

    source.stored["000660"].append(the_day(pasts["000660"], MONDAY, 0.0))
    await replay_into(e, clock, [], until=at(MONDAY, "21:00"))
    flat = [a for a in e.store.kind_answers(OPEN_TO_CLOSE.name) if a["symbol"] == "000660"]
    assert [a["class"] for a in flat] == ["flat"]
    assert e.next_due() != at(MONDAY, "21:05"), "nothing waits: no more looking"
    scored = e.store.kind_scored(OPEN_TO_CLOSE.name, 1, [MONDAY], None, False)
    assert len(scored) == 6, "every predictor of both symbols, on the day's answer"


async def test_a_circuit_breaker_marks_the_day_special_and_a_vi_is_counted(tmp_path: Path) -> None:
    pasts = {s: history(THURSDAY, 300, seed=i) for i, s in enumerate(SYMBOLS)}
    source = Source(pasts)
    clock = FakeClock(at(MONDAY, "08:00"))
    e = engine(tmp_path / "e.sqlite3", clock, source)
    await replay_into(e, clock, [], until=at(MONDAY, "09:00"))
    e.store.keep_special(
        Window(SpecialKind.VI, "005930", at(MONDAY, "10:00"), at(MONDAY, "10:02")), at(MONDAY, "10:00")
    )
    for s in SYMBOLS:
        source.stored[s].append(the_day(pasts[s], MONDAY, 0.01))
    await replay_into(e, clock, [], until=at(MONDAY, "20:36"))
    answered = {a["symbol"]: a for a in e.store.kind_answers(OPEN_TO_CLOSE.name)}
    assert (answered["005930"]["path"]["vi"], answered["005930"]["special"]) == (1, False), (
        "a VI is not special"
    )

    clock2 = FakeClock(at(date(2026, 10, 13), "08:00"))
    e.store.keep_special(
        Window(
            SpecialKind.CIRCUIT_BREAKER,
            None,
            at(date(2026, 10, 13), "10:00"),
            at(date(2026, 10, 13), "10:30"),
        ),
        at(date(2026, 10, 13), "10:00"),
    )
    e.store.close()
    e2 = engine(tmp_path / "e.sqlite3", clock2, source)
    await replay_into(e2, clock2, [], until=at(date(2026, 10, 13), "09:00"))
    for s in SYMBOLS:
        source.stored[s].append(the_day(source.stored[s], date(2026, 10, 13), -0.01))
    await replay_into(e2, clock2, [], until=at(date(2026, 10, 13), "20:36"))
    tuesday = [
        a for a in e2.store.kind_answers(OPEN_TO_CLOSE.name) if a["bar_close"].startswith("2026-10-13")
    ]
    assert [a["special"] for a in tuesday] == [True, True], "the market's breaker: every symbol's day"


async def test_an_engine_started_after_0850_does_not_predict_the_day_and_a_restart_answers_what_waits(
    tmp_path: Path,
) -> None:
    pasts = {s: history(THURSDAY, 300, seed=i) for i, s in enumerate(SYMBOLS)}
    source = Source(pasts)
    late = FakeClock(at(MONDAY, "08:51"))
    e = engine(tmp_path / "late.sqlite3", late, source)
    await replay_into(e, late, [], until=at(MONDAY, "21:00"))
    assert predictions(e.store) == []

    clock = FakeClock(at(MONDAY, "07:20"))
    first = engine(tmp_path / "e.sqlite3", clock, source)
    await replay_into(first, clock, [], until=at(MONDAY, "12:00"))
    first.store.close()
    for s in SYMBOLS:
        source.stored[s].append(the_day(pasts[s], MONDAY, -0.04))
    restarted = FakeClock(at(date(2026, 10, 13), "07:20"))  # the next morning's restart
    second = engine(tmp_path / "e.sqlite3", restarted, source)
    assert second._daily is not None and second._daily.next_due() == at(date(2026, 10, 13), "07:20"), (
        "the waiting day is looked at at once"
    )
    await replay_into(second, restarted, [], until=at(date(2026, 10, 13), "07:21"))
    answered = second.store.kind_answers(OPEN_TO_CLOSE.name)
    assert [(a["symbol"], a["class"]) for a in answered] == [("000660", "down"), ("005930", "down")]
    assert {a["scored_at"] for a in answered} == {"2026-10-13T07:20:00+09:00"}


async def test_a_day_whose_candle_never_comes_is_given_up_after_seven_days(tmp_path: Path) -> None:
    source = Source({s: history(THURSDAY, 300, seed=i) for i, s in enumerate(SYMBOLS)})
    clock = FakeClock(at(MONDAY, "08:00"))
    e = engine(tmp_path / "e.sqlite3", clock, source)
    await replay_into(e, clock, [], until=at(MONDAY, "09:00"))
    e.store.close()
    later = FakeClock(at(date(2026, 10, 19), "20:34"))
    e2 = engine(tmp_path / "e.sqlite3", later, source)
    await replay_into(e2, later, [], until=at(date(2026, 10, 19), "20:36"))
    answered = e2.store.kind_answers(OPEN_TO_CLOSE.name)
    monday = [a for a in answered if a["bar_close"].startswith("2026-10-12")]
    assert [(a["class"], a["unscorable"]) for a in monday] == [(None, NO_CANDLE)] * 2


async def test_a_daily_algorithm_predicts_at_0850_and_its_failures_are_kept(tmp_path: Path) -> None:
    source = Source({s: history(THURSDAY, 300, seed=i) for i, s in enumerate(SYMBOLS)})
    clock = FakeClock(at(MONDAY, "08:00"))
    algorithms: list[DailyAlgorithm] = [
        Leaning(),
        Leaning("broken", "x", fail=True),
        Leaning("slow", "y", sleep=0.3),
    ]
    e = PredictionEngine(
        PredictionStore(tmp_path / "e.sqlite3"),
        CALENDAR,
        SYMBOLS,
        FEE,
        clock,
        algorithms=(),
        kinds=(DIRECTION_KIND,),
        kind_algorithms=(),
        daily=source,
        daily_algorithms=algorithms,
        daily_features=lambda a, s, d, p, b: FeatureVector({f: p[-1].change for f in a.features}, a.features),
        daily_feature_names={"last_change", "x", "y"},
        budget=Budget(per_algorithm=0.1, cpu=30),
    )
    await replay_into(e, clock, [], until=at(MONDAY, "08:51"))
    by_algorithm = {(r["algorithm"], r["symbol"]): r for r in predictions(e.store)}
    for s in SYMBOLS:
        assert by_algorithm[("leaning", s)]["status"] == "ok"
        assert by_algorithm[("leaning", s)]["model_version"] == "leaning-v1"
        assert by_algorithm[("broken", s)]["status"] == "error"
        assert by_algorithm[("slow", s)]["status"] == "timed_out"
    assert e.store.statuses()["leaning"].status is Status.PROBATION
    assert e.kind_of_algorithm("leaning") is OPEN_TO_CLOSE


def test_daily_algorithms_are_checked_as_the_other_kinds_are() -> None:
    a: DailyAlgorithm = Leaning()
    b: DailyAlgorithm = Leaning("other")
    with pytest.raises(DailyRegistrationError, match="unknown features"):
        register_daily_algorithms([a], DAILY_KINDS, features=())
    with pytest.raises(DailyRegistrationError, match="already registered"):
        register_daily_algorithms([a], DAILY_KINDS, taken={"leaning"}, features={"last_change"})
    with pytest.raises(DailyRegistrationError, match="the same features"):
        register_daily_algorithms([a, b], DAILY_KINDS, features={"last_change"})
    other: DailyAlgorithm = Leaning("elsewhere", kind="range")
    with pytest.raises(DailyRegistrationError, match="not a registered daily kind"):
        register_daily_algorithms([other], DAILY_KINDS, features={"last_change"})


def add_day(store: PredictionStore, day: date, rng: random.Random) -> None:
    """A made-up day: ``leaning`` right on every symbol, the baselines always flat."""
    made, answered = [], []
    for symbol in SYMBOLS:
        label = rng.choice(OPEN_TO_CLOSE.classes)
        right = {f"p_{c}": float(c == label) for c in OPEN_TO_CLOSE.classes}
        flat = {"p_up": 0.0, "p_flat": 1.0, "p_down": 0.0}
        for name, version, output in (
            ("leaning", "leaning-v1", right),
            (FLAT_NAME, "v1", flat),
            (CLIMATE, "v1", flat),
            (PERSIST, "v1", flat),
        ):
            made.append(
                NewKindPrediction(
                    symbol, name, version, OPEN_TO_CLOSE.predicted_at(day), at(day, "08:50"), output
                )
            )
        answered.append((symbol, OPEN_TO_CLOSE.predicted_at(day), Answer(label, None, {}), False))
    store.add_kind_predictions(OPEN_TO_CLOSE.name, made)
    store.add_kind_answers(OPEN_TO_CLOSE.name, 1, answered, scored_at=at(day, "20:35"))


async def test_a_daily_algorithm_is_judged_on_its_60th_day_with_an_answer(tmp_path: Path) -> None:
    """The first probation review of a daily kind's algorithm is on its 60th answered day,
    against the kind's three baselines, by trading day at 98.3% (#264): not on the 59th."""
    store = PredictionStore(tmp_path / "e.sqlite3")
    first = date(2026, 6, 1)
    days = [d for d in (first + timedelta(days=i) for i in range(120)) if CALENDAR.is_trading_day(d)][:61]
    store.add_status("leaning", at(first, "07:00"), None, Status.PROBATION, "registered")
    store.record_models([("leaning", "leaning-v1", 1)])
    rng = random.Random(5)
    for day in days[:59]:
        add_day(store, day, rng)
    clock = FakeClock(at(days[59], "15:39"))
    e = engine(tmp_path / "e.sqlite3", clock, Source({}), algorithms=[Leaning()])
    assert e.store.kind_answered_days(OPEN_TO_CLOSE.name, 1, days[59], 100)[:2] == [days[58], days[57]]
    await e._review(at(days[59], "15:40"))
    assert e.store.statuses()["leaning"].status is Status.PROBATION, "59 days with an answer: not judged yet"

    add_day(e.store, days[59], rng)
    clock.set(at(days[60], "15:39"))
    await e._review(at(days[60], "15:40"))
    changed = e.store.status_history("leaning")[-1]
    assert changed["status"] == "active"
    assert changed["reason"].startswith("60 days on probation; weakest lift low")
    stages = await e.stages()
    assert "leaning" not in stages, "active: no stage"
