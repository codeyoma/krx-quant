"""Daily kinds (#264): predictions made once a trading day, at a fixed time before the open, of
a window that runs from the opening auction across the closing auction to the close.

The kinds of ``kinds.py`` predict at every bar close and are answered from the quotes of the
minutes after it (the Market View's windows). A daily kind has neither: it is predicted once a
trading day at its own time (``DailyKind.predict_at``, 08:50 KST), from what is known by then,
and answered once the day is over from the day's KRX auction prices. So it is registered apart
(``kind_registry.DAILY_KINDS``) and run apart (``DailyRunner``): the bar-close machinery, the
Market View and the 4-minute kinds never see it.

What it shares with the other kinds:

- its three classes, action classes, answer version and statistics (``Kind``: a daily kind is
  one, so ``kind_stats``' accuracy, Brier score, calibration, lifts and their FDR are its);
- its storage: its predictions go to ``kind_predictions`` (``bar_close`` is the prediction
  time, 08:50 of the day) and its answers to ``kind_answers``, one per (kind, symbol, day,
  answer version), every predictor of the day scored on it;
- the lifecycle rules, with its own days (``DailyKind.review_rules``): a day is one value per
  symbol, so a status is judged on 60, 120 and 180 trading days with an answer, never on the
  10 days of a bar-close kind's probation.

**Prediction.** On each trading day at ``predict_at`` the runner gives every daily kind's
baselines and its algorithms (``DailyAlgorithm``, admitted as any algorithm is: pre-validated)
the symbol's KRX day candles of the days before (``DailySource``), and stores what each says,
with the day's flat band (``kind_predictions.band``). An engine started after that time does not
predict the day: a prediction must be made from what was known at its time.

**Answer.** The day's answer is read from its KRX day candle (``candles.krx_days_path``: the
KRX market alone, so its open is the opening auction's and its close the closing auction's),
which the collector's history task stores after 20:30. The runner looks for it from
``ANSWER_FROM`` (20:35) every ``ANSWER_EVERY`` (10 minutes); a symbol whose candle is not there
waits, and is written unscorable (``NO_CANDLE``) once ``ANSWER_GIVE_UP`` (7 days) has passed. A
starting engine answers its predictions of the latest ``PENDING_DAYS`` days still without one
the same way. Training reads the same candles, so the labels it learns are the answers it is
scored on.

**Special days.** A circuit breaker or a sidecar on the day (``special_windows``: the market's,
learned live or filled from the recording, #236) marks its answer special: out of the default
statistics, as a window of the other kinds a special situation touches. A VI does not: it pauses
one symbol for two minutes of a day, and both ends are still the auctions' prices (the path
counts the day's VIs, ``vi``).
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time as timer
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from krx_quant.cards import Card, Meaning
from krx_quant.feeds.base import KST, kst_day
from krx_quant.kinds import NO_PREVIOUS, Answer, Kind, KindForecast
from krx_quant.lifecycle import Rules

if TYPE_CHECKING:
    from krx_quant.algorithms.contract import Model
    from krx_quant.features import FeatureVector
    from krx_quant.market_calendar import MarketCalendar
    from krx_quant.predictions import PredictionStore

log = logging.getLogger(__name__)

# A kind's name and class names: they go in URLs and JSON keys, as the bar-close kinds' do.
_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
# When a daily kind is predicted, KST, each trading day (#264).
PREDICT_AT = time(8, 50)
# When its answers are first looked for: the day's KRX day candle is stored after 20:30
# (``backfill.BARS_FINAL_AT``), by the collector's history task.
ANSWER_FROM = time(20, 35)
ANSWER_EVERY = timedelta(minutes=10)
# A day whose candle has not come by then is answered unscorable (``NO_CANDLE``).
ANSWER_GIVE_UP = timedelta(days=7)
# A starting engine answers its predictions of these latest days still without one.
PENDING_DAYS = 30
# Why a daily answer has no class.
NO_CANDLE = "no candle"  # the day's KRX day candle never came
NO_BAND = "no band"  # too few days before it to set the flat band
# The KRX days a prediction or an answer is given, at most: enough for the bands and the
# baselines' year (``open_to_close``), far less than the 40 years KIS keeps.
HISTORY_DAYS = 1_000

# The lifecycle of a daily kind's algorithms (#264): a trading day is one value per symbol, so
# a status is judged once 60 trading days have an answer, then at 120 and 180 (probation), and
# on the bench likewise; the level is the bar-close kinds' (98.3%, three looks).
DAILY_RULES = Rules(probation_days=(60, 120, 180), bench_days=(60, 120, 180), confidence=0.983)
# The latest days an active algorithm is benched on, and a benched one recovers on.
DAILY_RECENT_DAYS = 60
# The scored predictions, and the pairs with each baseline, a decision needs at least: the
# 60 days of one symbol (three symbols give 180 by the first review).
DAILY_MIN_SAMPLE = 60


@dataclass(frozen=True)
class DayPrices:
    """A symbol's KRX day (#264), from its KRX day candle: the opening auction's price, the
    day's high and low, the closing auction's price. Adjusted prices (``candles.py``): a past
    split divides them all alike, so a day's ratios hold."""

    day: date
    open: float
    high: float
    low: float
    close: float
    volume: int

    @classmethod
    def of(cls, row: Mapping[str, Any]) -> DayPrices | None:
        """A stored KRX day candle; None without an open and a close (no trade that day)."""
        opened, closed = row.get("open") or 0, row.get("close") or 0
        if opened <= 0 or closed <= 0:
            return None
        return cls(
            row["date"],
            float(opened),
            float(row.get("high") or max(opened, closed)),
            float(row.get("low") or min(opened, closed)),
            float(closed),
            int(row.get("volume") or 0),
        )

    @property
    def change(self) -> float:
        """The day's move from the opening auction to the closing auction: close / open − 1."""
        return self.close / self.open - 1


class DailySource(Protocol):
    """Where the runner reads the symbols' KRX days."""

    def days(self, symbol: str) -> list[DayPrices]:
        """Every stored KRX day of ``symbol``, oldest first."""
        ...


class StoredDays:
    """The KRX day candles the collector stores (``candles.read_krx_days``), read again only
    when the file changed."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self._read: dict[str, tuple[tuple[int, int], list[DayPrices]]] = {}

    def days(self, symbol: str) -> list[DayPrices]:
        from krx_quant.candles import krx_days_path, read_krx_days  # it reads the candles' files

        path = krx_days_path(self.data_dir, symbol)
        try:
            stat = path.stat()
        except FileNotFoundError:
            return []
        stamp = (stat.st_mtime_ns, stat.st_size)
        cached = self._read.get(symbol)
        if cached is None or cached[0] != stamp:
            found = [d for r in read_krx_days(self.data_dir, symbol) if (d := DayPrices.of(r)) is not None]
            cached = self._read[symbol] = (stamp, found)
        return cached[1]


@dataclass(frozen=True)
class Band:
    """A daily kind's flat band for a day (#264), set from the days before it only."""

    value: float  # |close / open − 1| at most this is flat
    sigma: float  # the volatility it scales (``open_to_close``)
    scale: float  # the multiple of it


@dataclass(frozen=True)
class DailyInputs:
    """What a daily baseline knows at the prediction time: the symbol's answers of the days
    before, by the kind's rule (``DailyKind.answers``), oldest first, and the day's band."""

    symbol: str
    day: date
    at: datetime
    past: tuple[tuple[date, str | None], ...]  # (day, class), None for a day without one
    band: Band | None
    previous_day: date | None = None  # the trading day before ``day``, by the calendar


class DailyBaseline(Protocol):
    name: str
    version: str
    card: Card

    def predict(self, inputs: DailyInputs) -> KindForecast | None:
        """The forecast; None where it does not predict (status ``NO_PREVIOUS``)."""
        ...


# A daily kind's answers of its days (oldest first, one each), each set from the days before
# it only, with the fee rate for the research P&L's costs in the path.
DayAnswers = Callable[[Sequence[DayPrices], float], list[Answer]]
# A daily kind's band of the day after ``days`` (oldest first), from them only.
DayBand = Callable[[Sequence[DayPrices]], "Band | None"]


@dataclass(frozen=True)
class DailySample:
    """One symbol's day of a daily kind's training data (#264): what was known at the prediction
    time, and the day's answer (None without a class: too few days for the band)."""

    symbol: str
    day: date
    features: FeatureVector
    answer: Answer | None
    answer_version: int

    @property
    def label(self) -> str | None:
        return None if self.answer is None else self.answer.label


class DailyAlgorithm(Protocol):
    """An algorithm of a daily kind (#264): the contract of the other kinds' algorithms
    (``algorithms.contract.KindAlgorithm``), on a day's features and the kind's answers."""

    name: str
    kind: str  # a daily kind's name
    card: Card
    features: tuple[str, ...]  # of ``daily_features.DAILY_FEATURES``
    training_days: int  # the latest trading days it trains on
    param_meanings: Mapping[str, Meaning]

    def default_model(self) -> Model: ...

    def fit(self, samples: Sequence[DailySample]) -> Model: ...

    def predict(self, features: FeatureVector, model: Model) -> KindForecast: ...


@dataclass(frozen=True, eq=False)
class DailyKind(Kind):
    """A kind predicted once a trading day (#264): its time, its window (from the opening
    auction to the close), its rule of a day's answer and band, its baselines and the
    lifecycle's days of it. The ``Kind`` fields it has are a kind's (classes, action, answer
    version, statistics); its bar-close fields (``answer``, ``bar_answer``, ``width``,
    ``baselines``, ``horizon``) are unused."""

    predict_at: time = PREDICT_AT
    window: tuple[str, str] = ("open", "close")  # the auction it starts at, the one it ends at
    window_titles: tuple[str, str] = ("시가 단일가", "종가 단일가")
    daily_baselines: tuple[DailyBaseline, ...] = ()
    answers: DayAnswers | None = None
    band: DayBand | None = None
    review_rules: Rules = DAILY_RULES
    recent_days: int = DAILY_RECENT_DAYS
    min_sample: int = DAILY_MIN_SAMPLE

    def baseline_cards(self) -> list[tuple[str, Card]]:
        return [(b.name, b.card) for b in self.daily_baselines]

    def baseline_names(self) -> frozenset[str]:
        return frozenset(b.name for b in self.daily_baselines)

    def predicted_at(self, day: date) -> datetime:
        """The prediction time of ``day``: ``bar_close`` of its predictions."""
        return datetime.combine(day, self.predict_at, KST)

    def answer_of(self, days: Sequence[DayPrices], fee_rate: float) -> Answer:
        """The answer of the last of ``days`` (oldest first), from the ones before it."""
        if self.answers is None:
            raise ValueError(f"{self.name}: no answer rule")
        return self.answers(days, fee_rate)[-1]

    def as_dict(self, baselines: Sequence[tuple[str, Card]]) -> dict[str, Any]:
        """As ``GET /v1/kinds`` shows it: a kind's fields, no horizon (it has no bar close), and
        its day (``daily``)."""
        out = super().as_dict(baselines)
        out["horizon_minutes"] = None
        out["daily"] = {
            "predict_at": self.predict_at.strftime("%H:%M"),
            "from": self.window[0],
            "to": self.window[1],
            "from_title": self.window_titles[0],
            "to_title": self.window_titles[1],
            "answer_from": ANSWER_FROM.strftime("%H:%M"),
            "probation_days": list(self.review_rules.probation_days),
            "bench_days": list(self.review_rules.bench_days),
            "recent_days": self.recent_days,
        }
        return out


class DailyRegistrationError(ValueError):
    pass


def register_daily_kinds(kinds: Sequence[DailyKind], taken: Sequence[Kind] = ()) -> tuple[DailyKind, ...]:
    """Check the daily kinds in order (``kinds.register_kinds``' rules for a name, classes and
    action classes), against the bar-close kinds ``taken``. A kind's name and its baselines'
    names share one set with every name before them, the bar-close kinds' and their baselines'
    included, so no name stands for two things (a kind and a baseline, in either order); one
    without an answer or a band rule is refused too."""
    names = {k.name for k in taken} | {name for k in taken for name in k.baseline_names()}
    for k in kinds:
        if not _NAME.match(k.name) or k.name in names:
            raise DailyRegistrationError(f"{k.name!r}: taken, or not a lowercase name")
        names.add(k.name)
        if (
            len(set(k.classes)) != 3
            or not all(_NAME.match(c) for c in k.classes)
            or not k.action
            or not set(k.action) <= set(k.classes)
        ):
            raise DailyRegistrationError(f"{k.name}: classes {k.classes}, action {k.action}")
        if k.answers is None or k.band is None:
            raise DailyRegistrationError(f"{k.name}: no answer or band rule")
        for b in k.daily_baselines:
            if b.name in names:
                raise DailyRegistrationError(f"{k.name}: baseline {b.name} is taken")
            names.add(b.name)
    return tuple(kinds)


def history_before(days: Sequence[DayPrices], day: date, limit: int = HISTORY_DAYS) -> list[DayPrices]:
    """The latest ``limit`` of ``days`` (oldest first) before ``day``."""
    before = [d for d in days if d.day < day]
    return before[-limit:]


@dataclass(frozen=True)
class DailyStored:
    """What the runner stored, for the engine to tell its listeners: a daily kind's
    predictions displayed (``ids`` by predictor) or its answers written (``keys``)."""

    kind: str
    at: datetime
    ids: Mapping[str, list[int]] = field(default_factory=dict)
    keys: list[tuple[str, datetime]] = field(default_factory=list)


# Extra inputs of a daily algorithm's features beyond the KRX days, as of the prediction time
# (``daily_features``): the engine's (the pre-market trades it received) and the stored days'.
Features = Callable[["DailyAlgorithm", str, date, Sequence[DayPrices], "Band | None"], "FeatureVector"]
Running = Callable[[], Sequence[tuple["DailyAlgorithm", "Model"]]]
Told = Callable[[DailyStored], None]


class DailyRunner:
    """The daily kinds in the engine (#264): each trading day's predictions at their time, and
    the answers once the day's KRX day candles are stored (module doc)."""

    def __init__(
        self,
        kinds: Sequence[DailyKind],
        store: PredictionStore,
        calendar: MarketCalendar,
        symbols: Sequence[str],
        fee_rate: float,
        source: DailySource,
        now: Callable[[], datetime],
        *,
        tell: Told | None = None,
        running: Running | None = None,
        features: Features | None = None,
        seconds: float = 10.0,
    ) -> None:
        self.kinds = tuple(kinds)
        self.store = store
        self.calendar = calendar
        self.symbols = tuple(symbols)
        self.fee_rate = fee_rate
        self.source = source
        self._now = now  # the engine's clock: the display time
        self._tell = tell or (lambda _: None)
        self._running = running or (lambda: ())
        self._features = features
        self._seconds = seconds  # an algorithm's time limit on a day's symbols
        started_at = now()
        # The last prediction time handled: a start after a day's time skips that day.
        self._predicted_through = started_at
        # (kind, symbol, day) predicted and not answered yet: answered from ANSWER_FROM on.
        self._pending: set[tuple[str, str, date]] = set()
        since = kst_day(started_at) - timedelta(days=PENDING_DAYS)
        for k in self.kinds:
            for symbol, day in store.daily_unanswered(k.name, k.answer_version, since):
                self._pending.add((k.name, symbol, day))
        self._next_look = self._first_look(started_at, at_once=True) if self._pending else None

    def kind_named(self, name: str) -> DailyKind | None:
        return next((k for k in self.kinds if k.name == name), None)

    def next_due(self) -> datetime | None:
        """The next prediction time, or the next look for answers when any wait."""
        due = [t for t in (self._next_prediction(), self._next_look) if t is not None]
        return min(due) if due else None

    def _next_prediction(self) -> datetime | None:
        """The first prediction time of a daily kind after the last one handled."""
        after = self._predicted_through
        day = kst_day(after)
        for _ in range(14):
            if self.calendar.is_trading_day(day):
                times = [k.predicted_at(day) for k in self.kinds if k.predicted_at(day) > after]
                if times:
                    return min(times)
            day += timedelta(days=1)
        return None

    def _first_look(self, after: datetime, *, at_once: bool = False) -> datetime:
        """The first look for answers from ``after``: the next ``ANSWER_EVERY`` step (``after``
        itself ``at_once``, as a starting engine looks), never before the earliest waiting day's
        ``ANSWER_FROM``."""
        earliest = min(day for _, _, day in self._pending)
        start = datetime.combine(earliest, ANSWER_FROM, KST)
        if after < start:
            return start
        if at_once:
            return after
        steps = math.floor((after - start) / ANSWER_EVERY) + 1
        return start + steps * ANSWER_EVERY

    async def run(self, due: datetime) -> None:
        """What is due at ``due``: the day's predictions, then the answers that can be made."""
        if due == self._next_prediction():
            self._predicted_through = due
            await self.predict(due)
        if self._next_look is not None and due >= self._next_look:
            await self.answer(due)

    async def predict(self, at: datetime) -> None:
        """Every daily kind due at ``at``: its baselines' and its algorithms' predictions of every
        symbol, stored at once, each with the day's band."""
        day = kst_day(at)
        for kind in [k for k in self.kinds if k.predicted_at(day) == at]:
            try:
                await self._predict_kind(kind, day, at)
            except Exception:
                log.exception("%s: its predictions failed on %s", kind.name, day)

    async def _predict_kind(self, kind: DailyKind, day: date, at: datetime) -> None:
        histories = await asyncio.to_thread(self._histories, day)
        bands = {s: _band(kind, h) for s, h in histories.items()}
        past = await asyncio.to_thread(self._past, kind, histories)
        made: list[tuple[str, str, Mapping[str, KindForecast | None], str]] = []
        previous = self.calendar.previous_trading_day(day)
        for baseline in kind.daily_baselines:
            forecasts: dict[str, KindForecast | None] = {}
            for symbol in self.symbols:
                inputs = DailyInputs(symbol, day, at, past[symbol], bands[symbol], previous)
                forecast = baseline.predict(inputs)
                if forecast is not None and (why := kind.invalid(forecast)) is not None:
                    raise ValueError(f"{baseline.name}: {why}")
                forecasts[symbol] = forecast
            made += _by_status(baseline.name, baseline.version, forecasts)
        for algorithm, model in self._running():
            if algorithm.kind != kind.name:
                continue
            made += await self._algorithm(kind, algorithm, model, day, histories, bands)
        self._record(kind, at, made, bands)
        for symbol in self.symbols:
            self._pending.add((kind.name, symbol, day))
        look = datetime.combine(day, ANSWER_FROM, KST)
        self._next_look = look if self._next_look is None else min(self._next_look, look)

    async def _algorithm(
        self,
        kind: DailyKind,
        algorithm: DailyAlgorithm,
        model: Model,
        day: date,
        histories: Mapping[str, list[DayPrices]],
        bands: Mapping[str, Band | None],
    ) -> list[tuple[str, str, Mapping[str, KindForecast | None], str]]:
        """``algorithm``'s forecasts of every symbol, in a worker thread, within its time limit:
        ``timed_out`` past it, ``error`` for an exception or a forecast that breaks the kind's
        contract (every symbol alike, as an algorithm's bar close)."""
        features = self._features
        if features is None:
            return [(algorithm.name, model.version, dict.fromkeys(self.symbols), "error")]

        def forecasts() -> dict[str, KindForecast | None]:
            out: dict[str, KindForecast | None] = {}
            for symbol in self.symbols:
                vector = features(algorithm, symbol, day, histories[symbol], bands[symbol])
                out[symbol] = forecast = algorithm.predict(vector, model)
                if (why := kind.invalid(forecast)) is not None:
                    raise ValueError(why)
            return out

        started = timer.monotonic()
        try:
            found = await asyncio.wait_for(asyncio.to_thread(forecasts), self._seconds)
        except TimeoutError:
            log.warning("%s: over its %gs on %s", algorithm.name, self._seconds, day)
            return [(algorithm.name, model.version, dict.fromkeys(self.symbols), "timed_out")]
        except Exception:
            log.exception("%s: its daily forecast failed on %s", algorithm.name, day)
            return [(algorithm.name, model.version, dict.fromkeys(self.symbols), "error")]
        log.info("%s predicted %s in %.3fs", algorithm.name, day, timer.monotonic() - started)
        return [(algorithm.name, model.version, found, "ok")]

    def _histories(self, day: date) -> dict[str, list[DayPrices]]:
        return {s: history_before(self.source.days(s), day) for s in self.symbols}

    def _past(
        self, kind: DailyKind, histories: Mapping[str, list[DayPrices]]
    ) -> dict[str, tuple[tuple[date, str | None], ...]]:
        """Each symbol's answers of the days before, by the kind's rule."""
        assert kind.answers is not None
        out: dict[str, tuple[tuple[date, str | None], ...]] = {}
        for symbol, history in histories.items():
            answers = kind.answers(history, self.fee_rate) if history else []
            out[symbol] = tuple((d.day, a.label) for d, a in zip(history, answers, strict=True))
        return out

    def _record(
        self,
        kind: DailyKind,
        at: datetime,
        made: Sequence[tuple[str, str, Mapping[str, KindForecast | None], str]],
        bands: Mapping[str, Band | None],
    ) -> None:
        from krx_quant.predictions import NewKindPrediction  # the store's types

        displayed_at = self._now()
        predictions = [
            NewKindPrediction(
                symbol=symbol,
                algorithm=name,
                model_version=version,
                bar_close=at,
                displayed_at=displayed_at,
                output=None if forecast is None else kind.output(forecast),
                status=status,
                band=None if (band := bands.get(symbol)) is None else band.value,
            )
            for name, version, forecasts, status in made
            for symbol, forecast in forecasts.items()
        ]
        if predictions:
            ids = self.store.add_kind_predictions(kind.name, predictions)
            self._tell(DailyStored(kind.name, at, ids=ids))

    async def answer(self, now: datetime) -> None:
        """The answers of the days waiting whose KRX day candle is stored, and of those given up
        on; then when to look again."""
        waiting = sorted(self._pending, key=lambda p: (p[2], p[0], p[1]))
        days = await asyncio.to_thread(lambda: {s: self.source.days(s) for s in {p[1] for p in waiting}})
        by_kind: dict[str, list[tuple[str, datetime, Answer, bool]]] = {}
        for name, symbol, day in waiting:
            kind = self.kind_named(name)
            if kind is None:
                self._pending.discard((name, symbol, day))
                continue
            if now < datetime.combine(day, ANSWER_FROM, KST):
                continue
            found = [d for d in days[symbol] if d.day <= day]
            if found and found[-1].day == day:
                answer = kind.answer_of(found[-HISTORY_DAYS:], self.fee_rate)
                answer = _with_vis(answer, self.store, symbol, day)
            elif now >= datetime.combine(day, ANSWER_FROM, KST) + ANSWER_GIVE_UP:
                answer = Answer(None, NO_CANDLE, {})
            else:
                continue
            special = self._special_day(day)
            by_kind.setdefault(name, []).append((symbol, kind.predicted_at(day), answer, special))
            self._pending.discard((name, symbol, day))
        for name, answers in by_kind.items():
            kind = self.kind_named(name)
            assert kind is not None
            written = self.store.add_kind_answers(name, kind.answer_version, answers, scored_at=now)
            if written:
                self._tell(DailyStored(name, now, keys=written))
        self._next_look = self._first_look(now) if self._pending else None

    def _special_day(self, day: date) -> bool:
        """Whether a circuit breaker or a sidecar (a window of the whole market) touched ``day``."""
        start = datetime.combine(day, time(), KST)
        windows = self.store.special_windows(None, start, start + timedelta(days=1))
        return any(w["symbol"] is None for w in windows)


def _band(kind: DailyKind, history: Sequence[DayPrices]) -> Band | None:
    assert kind.band is not None
    return kind.band(history) if history else None


def _with_vis(answer: Answer, store: PredictionStore, symbol: str, day: date) -> Answer:
    """``answer`` with the day's VIs of ``symbol`` counted in its path (``vi``)."""
    start = datetime.combine(day, time(), KST)
    vis = [w for w in store.special_windows(symbol, start, start + timedelta(days=1)) if w["kind"] == "vi"]
    return Answer(answer.label, answer.unscorable, {**answer.path, "vi": len(vis)})


def _by_status(
    name: str, version: str, forecasts: Mapping[str, KindForecast | None]
) -> list[tuple[str, str, Mapping[str, KindForecast | None], str]]:
    """A baseline's forecasts by status: those it made (``ok``), those it did not (``NO_PREVIOUS``)."""
    made = {s: f for s, f in forecasts.items() if f is not None}
    missing: dict[str, KindForecast | None] = {s: None for s, f in forecasts.items() if f is None}
    out: list[tuple[str, str, Mapping[str, KindForecast | None], str]] = []
    if made:
        out.append((name, version, made, "ok"))
    if missing:
        out.append((name, version, missing, NO_PREVIOUS))
    return out


def register_daily_algorithms(
    algorithms: Sequence[DailyAlgorithm],
    kinds: Sequence[DailyKind],
    *,
    taken: Collection[str] = (),
    features: Collection[str] = (),
) -> tuple[DailyAlgorithm, ...]:
    """Check the daily kinds' algorithms in order (#264), as ``algorithms.register_kind_algorithms``
    does the other kinds': a kind not among ``kinds``, a name ``taken`` (every other predictor's)
    or used twice, a feature not among ``features`` (``daily_features.DAILY_FEATURES``), and,
    within its kind, no feature of its own, its own ones not among its features, the feature
    set of an earlier one, or its own ones used by an earlier one."""
    known = {k.name for k in kinds}
    names = set(taken)
    earlier: dict[str, list[DailyAlgorithm]] = {}
    for a in algorithms:
        if a.kind not in known:
            raise DailyRegistrationError(f"{a.name}: kind {a.kind!r} is not a registered daily kind")
        if a.name in names:
            raise DailyRegistrationError(f"{a.name}: already registered")
        names.add(a.name)
        unknown = sorted(set(a.features) - set(features))
        if unknown:
            raise DailyRegistrationError(f"{a.name}: unknown features {', '.join(unknown)}")
        own = set(a.card.unique_features)
        if not own:
            raise DailyRegistrationError(f"{a.name}: no feature of its own")
        if not own <= set(a.features):
            raise DailyRegistrationError(
                f"{a.name}: unique features {sorted(own - set(a.features))} not declared"
            )
        for r in earlier.get(a.kind, []):
            if set(r.features) == set(a.features):
                raise DailyRegistrationError(f"{a.name}: the same features as {r.name}")
            if own & set(r.features):
                raise DailyRegistrationError(
                    f"{a.name}: its own features {sorted(own & set(r.features))} used by {r.name}"
                )
        earlier.setdefault(a.kind, []).append(a)
    return tuple(algorithms)
