"""Prediction kinds (#179): what a predictor predicts at each bar close.

The engine predicts the direction (``direction``: up, flat or down, the touch target of
#160) and, beside it, other kinds of forecast: how far the mid-point moves (range, #177),
which barrier is reached first (#178). Every kind's core output is three probabilities, so
the Stats Engine's three-class statistics (accuracy, Brier score, calibration, lift, the
lift by trading day, the false discovery rate) hold for any of them, and so do the
lifecycle rules, judged within the kind against its own baselines.

A kind declares in one place (``Kind``):

- its name, and its three classes in order (the first, the middle and the third);
- its action classes: the calls precision and coverage count (direction: up and down);
- its answer function and answer version: the class of a bar close's four minutes, from
  the quotes the Market View followed through them (``Followed``), and, for days with
  one-minute bars only, a bar rule (``bar_answer``) to train on;
- whether its answer counts crossed quotes (``crossed``, #187): a touch's answer does (the
  barrier), a mid-point's does not (the range);
- for a kind with barriers (#183, the barrier), the rule of their width at a bar close
  (``width``): the engine fixes it there from the bars it knows, the same for every predictor;
  the answer reads it (``Followed.width``), the bar rule is given it, and every prediction keeps
  it (``kind_predictions.width_ticks``);
- its baselines, the predictions every algorithm of the kind must beat: some from what
  they know at the bar close, some from a table the nightly training refits on the kind's
  answers and the 08:00 swap installs (``TableBaseline``, #180);
- its extra output fields beside the three probabilities, and their check;
- its extra statistics, beside the three-class ones (with the kind's baselines' records,
  to compare against).

A kind has a horizon (``Kind.horizon``, #263): its windows run from the bar close for that
long. Four minutes, the touch target's, unless the kind says otherwise; the direction's is
always four. A longer one is answered at t + horizon, its quotes kept as long, and its last
bar close predicted is the one whose window ends by the closing auction.

A forecast's class is its most likely one; a tie goes to the middle class, then the
first (as the direction's goes to flat, then up).

The direction is registered as it always ran: its predictions, touch labels, baselines
and consensus keep their own tables and code (``predictions``, ``touches``,
``baselines.py``, ``consensus.py``). Every other kind's predictions and answers go to
``kind_predictions`` and ``kind_answers``. A kind's answer is made once per (kind, symbol,
bar close, answer version) at t + 4 minutes, and every predictor of the kind at that bar
close is scored on it.

Every kind's answer is made from the same quotes as the touch label (``touch.py``): the
integrated best quotes received from t up to t + 4 minutes, WebSocket ones only; a
one-sided quote is dropped; a crossed one (the best ask below the best bid) is kept for a
kind that counts it (``Kind.crossed``: the barrier, whose touches are trades one could have
made at each side's price, as the touch label's, #187) and dropped for one that does not
(the range, whose mid-points mean nothing across a crossing, as for the reference price);
a window that received a REST snapshot of the book, went sixty seconds without one of the
kind's quotes, or that the engine restarted in cannot be scored (``Followed.unscorable``),
whatever the answer function says.

The kinds are registered in ``kind_registry.KINDS``, in order: the direction first, then
the range (#180, ``ranges.py``), then the barrier (#183, ``barriers.py``). A new kind's
algorithms run after the direction's (``algorithms.KIND_ALGORITHMS``), so the CPU budget
skips them first.
"""

from __future__ import annotations

import math
import re
from bisect import bisect_left
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol, runtime_checkable

from krx_quant.cards import Card, Meaning
from krx_quant.reference import Bar
from krx_quant.touch import MISSING_MID, REST_QUOTES, SILENCE, SILENT, TARGET_VERSION, WINDOW

if TYPE_CHECKING:  # the algorithm contract builds on this module: its types only, and the features'
    from krx_quant.algorithms.contract import KindSample, Model
    from krx_quant.features import Snapshot, Width

DIRECTION = "direction"
# Why a kind's answer function gave no class for a window the shared rules could score.
NO_ANSWER = "no answer"
# The status of a kind baseline's prediction it did not make (as ``baselines.NO_PREVIOUS``).
NO_PREVIOUS = "no_previous"
# A kind's name and class names: they go in URLs and JSON keys (``p_<class>``).
_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


class Quote(NamedTuple):
    """A best quote received in a window: an integrated one from the WebSocket, not
    one-sided (#160); crossed (``crossed``) only in the window of a kind that counts it
    (#187)."""

    received_at: datetime
    bid: float
    ask: float
    bid_size: float | None  # from the book feed; None from a trade's quote
    ask_size: float | None

    @property
    def crossed(self) -> bool:
        """The best ask below the best bid: the two sides come from two venues (#187)."""
        return self.ask < self.bid


@dataclass(frozen=True)
class Followed:
    """The four minutes from a bar close as the Market View followed them (#167, #179): what a
    kind's answer is made from, the same quotes the touch label is.

    ``anchor`` is the mid-point at the bar close (the reference price), ``tick`` and ``k``
    its tick size and flat band, ``bid_start`` and ``ask_start`` the best quote received
    last before the bar close. ``quotes`` are the usable best quotes received in
    [t, t + 4 minutes), in the order received, crossed ones among them for a kind that
    counts them (``Kind.crossed``, #187); ``trades`` the integrated trades' (time,
    price) there. ``unscorable`` says why the window cannot be scored (``touch.py``'s
    reasons), None if it can. ``cost`` is the round-trip cost at the reference price in ticks
    (``pricing.cost_ticks``: k is it rounded up), and ``width`` the kind's barrier width fixed
    at the bar close (``Kind.width``, #183); None without a reference price, or a kind without
    barriers."""

    symbol: str
    bar_close: datetime
    anchor: float | None
    tick: float | None
    k: int | None
    bid_start: float | None
    ask_start: float | None
    quotes: tuple[Quote, ...]
    trades: tuple[tuple[datetime, float], ...]
    unscorable: str | None
    cost: float | None = None
    width: Width | None = None
    horizon: timedelta = WINDOW  # the kind's (#263): the window is [t, t + horizon)

    @property
    def end(self) -> datetime:
        return self.bar_close + self.horizon


class QuoteLog:
    """A symbol's best quotes, REST book snapshots and trades as received, for the kinds'
    windows (#179): kept while a window may read them. A one-sided quote is dropped, as a
    touch window drops it (``touch.TouchWindow``); a crossed one is kept, and a window
    gives it only to a kind that counts it (``window``, #187).

    A window reads its minutes of the log by bisection while everything came in receipt order,
    as it does live and in a replay: the log holds the longest kind's window and more (an hour
    with the barrier's horizon ladder, #263), so scanning all of it for each window would cost
    that hour every time. Anything received out of order falls back to a scan."""

    def __init__(self) -> None:
        self._quotes: list[Quote] = []
        self._rests: list[datetime] = []
        self._trades: list[tuple[datetime, float]] = []
        self._ordered = True  # each list in the order of its received times

    def quote(
        self,
        received_at: datetime,
        bid: float | None,
        ask: float | None,
        bid_size: float | None,
        ask_size: float | None,
        rest: bool,
    ) -> None:
        if rest:
            self._ordered &= not self._rests or self._rests[-1] <= received_at
            self._rests.append(received_at)
            return
        if not bid or not ask or bid <= 0 or ask <= 0:
            return  # one-sided: dropped; a crossed one is kept for the kinds that count it
        self._ordered &= not self._quotes or self._quotes[-1].received_at <= received_at
        self._quotes.append(Quote(received_at, bid, ask, bid_size, ask_size))

    def trade(self, received_at: datetime, price: float | None) -> None:
        if price:
            self._ordered &= not self._trades or self._trades[-1][0] <= received_at
            self._trades.append((received_at, price))

    def forget(self, before: datetime) -> None:
        """Drop what was received before ``before``: no window kept reads it."""
        self._quotes = [q for q in self._quotes if q.received_at >= before]
        self._rests = [r for r in self._rests if r >= before]
        self._trades = [t for t in self._trades if t[0] >= before]

    def window(
        self,
        symbol: str,
        bar_close: datetime,
        anchor: float | None,
        tick: float | None,
        k: int | None,
        bid_start: float | None,
        ask_start: float | None,
        *,
        cost: float | None = None,
        width: Width | None = None,
        crossed: bool = False,
        horizon: timedelta = WINDOW,
    ) -> Followed:
        """The window from ``bar_close``, by the touch target's rules: the quotes received in
        [t, t + ``horizon``) (four minutes, or a kind's own, #263), the crossed ones only with
        ``crossed`` (a kind that counts them, #187); unscorable without a mid-point at t, with a
        REST snapshot in it, or with sixty seconds or more without one of those quotes (from t,
        between two, or up to the end). ``cost`` and ``width`` go with it as given (``Followed``).
        """
        start, end = bar_close, bar_close + horizon
        if self._ordered:
            got = self._quotes[
                bisect_left(self._quotes, start, key=_received) : bisect_left(
                    self._quotes, end, key=_received
                )
            ]
            rests = self._rests[bisect_left(self._rests, start) : bisect_left(self._rests, end)]
            traded = self._trades[
                bisect_left(self._trades, start, key=_at) : bisect_left(self._trades, end, key=_at)
            ]
        else:
            got = [q for q in self._quotes if start <= q.received_at < end]
            rests = [r for r in self._rests if start <= r < end]
            traded = [t for t in self._trades if start <= t[0] < end]
        quotes = tuple(q for q in got if crossed or not q.crossed)
        rest = bool(rests)
        trades = tuple(traded)
        silent = went_silent(start, end, [q.received_at for q in quotes])
        unscorable = MISSING_MID if anchor is None else REST_QUOTES if rest else SILENT if silent else None
        return Followed(
            symbol,
            bar_close,
            anchor,
            tick,
            k,
            bid_start,
            ask_start,
            quotes,
            trades,
            unscorable,
            cost,
            width,
            horizon,
        )


def _received(q: Quote) -> datetime:
    return q.received_at


def _at(trade: tuple[datetime, float]) -> datetime:
    return trade[0]


def went_silent(start: datetime, end: datetime, received: Sequence[datetime]) -> bool:
    """Whether sixty seconds or more went by without a quote in [``start``, ``end``): from the
    start to the first quote ``received``, between two, or from the last to the end."""
    last = start
    for at in received:
        if at - last >= SILENCE:
            return True
        last = at
    return end - last >= SILENCE


@dataclass(frozen=True)
class Answer:
    """A kind's answer for a bar close: its class (None: it could not be scored, ``unscorable``
    says why) and a summary of its path, as stored and served (JSON)."""

    label: str | None
    unscorable: str | None = None
    path: Mapping[str, Any] = field(default_factory=dict)


# A kind's answer from a window's quotes (called for every window, scorable or not; a
# window the shared rules cannot score gets no class whatever it says).
AnswerFunction = Callable[[Followed], Answer]


class BarAnswer(Protocol):
    """A kind's answer from one-minute bars, for days with no quotes (training only)."""

    def __call__(
        self, closing: Bar, after: Sequence[Bar], fee_rate: float, /, *, width: Width | None = None
    ) -> Answer | None:
        """The answer from the bar that ended at the bar close (``closing``), the bars of the
        four minutes after it (``after``, those with trades) and the fee rate; for a kind with
        barriers (#183), their width at the bar close from the bars up to it (``Kind.width``),
        None for one without. None without an answer."""
        ...


# A kind's barrier width at a bar close (#183), from what is known there (the day's bars and
# the previous trading day's, the reference price and k: ``features.Snapshot``); None without
# a reference price.
WidthRule = Callable[["Snapshot"], "Width | None"]


@dataclass(frozen=True)
class KindForecast:
    """A forecast of a kind: its three classes' probabilities, in the kind's order, and the
    kind's extra fields."""

    probabilities: tuple[float, float, float]
    fields: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class KindInputs:
    """What a kind's baseline knows at a bar close."""

    symbol: str
    bar_close: datetime
    mid: float | None  # the reference price at the bar close
    tick: float | None
    flat_ticks: int | None
    # The answer of the window that ended at this bar close (t − 4 minutes to t); None when
    # it could not be scored or was not followed (the day's first minutes, a restart).
    previous: Answer | None
    # The kind's answers of the symbol's windows over today, by class.
    day_classes: Mapping[str, int]


class KindBaseline(Protocol):
    name: str
    version: str
    card: Card

    def predict(self, inputs: KindInputs) -> KindForecast | None:
        """The forecast; None where it does not predict (status ``NO_PREVIOUS``)."""
        ...


@runtime_checkable
class TableBaseline(Protocol):
    """A kind baseline that predicts from a table (#180, ``range-climate``): a model like an
    algorithm's (``state/models/<name>.json``), which the nightly training refits every night
    on the kind's answers of the latest ``days`` training days and always promotes, and the
    08:00 swap installs, as the direction's climate table (``climate.py``). The engine binds
    the table it runs (``using``): the predictions carry the table's version."""

    name: str
    version: str  # the bound table's
    card: Card
    days: int  # the latest training days a table is fitted on

    def predict(self, inputs: KindInputs) -> KindForecast | None: ...

    def default_table(self) -> Model:
        """The table before any training."""
        ...

    def fit(self, samples: Sequence[KindSample]) -> Model:
        """The table of the kind's answers of ``samples`` (the defaults without any)."""
        ...

    def using(self, table: Model) -> TableBaseline:
        """This baseline predicting from ``table``."""
        ...


def table_baselines(kinds: Sequence[Kind]) -> list[tuple[Kind, TableBaseline]]:
    """The kinds' baselines that predict from a table, with their kind, in order."""
    return [(k, b) for k in kinds for b in k.baselines if isinstance(b, TableBaseline)]


@dataclass(frozen=True)
class KindScored:
    """One scored prediction of a kind, on its bar close's answer."""

    algorithm: str
    model_version: str
    symbol: str
    bar_close: datetime
    probabilities: tuple[float, float, float]
    outcome: int  # the answer's class, by its place in the kind's classes
    output: Mapping[str, float]  # the stored output: the probabilities and the kind's fields
    path: Mapping[str, Any]  # the answer's path

    @property
    def predicted(self) -> int:
        return most_likely(self.probabilities)

    @property
    def correct(self) -> bool:
        return self.predicted == self.outcome


def most_likely(probabilities: Sequence[float]) -> int:
    """The class a forecast predicts, by its place: the most likely; a tie goes to the middle
    class, then the first (``pricing.most_likely``'s flat, then up)."""
    best = max(probabilities)
    if probabilities[1] == best:
        return 1
    return 0 if probabilities[0] == best else 2


# The check of a kind's output (the probabilities and its fields, as stored): why it breaks
# the kind's contract, None if it keeps it.
OutputCheck = Callable[[Mapping[str, float]], str | None]
# A kind's statistics beside the three-class ones: of the predictor named, from its scored
# predictions and the kind's baselines' (by name, to compare against; #180).
ExtraStats = Callable[[str, Sequence[KindScored], Mapping[str, Sequence[KindScored]]], dict[str, Any]]


@dataclass(frozen=True, eq=False)
class Kind:
    """A kind of prediction (#179), as registered: everything the engine, the Stats Engine,
    the lifecycle, training and the Engine API need to know of it."""

    name: str
    title: str  # as the screen names it (Korean)
    classes: tuple[str, str, str]  # in order: the first, the middle, the third
    class_titles: tuple[str, str, str]  # as the screen names them
    action: tuple[str, ...]  # the classes precision and coverage count
    answer_version: int
    # The answer of a window (None: the direction, whose touch label is ``touch.py``'s).
    answer: AnswerFunction | None = None
    # The answer of a day with one-minute bars only; None: such days are not trained on.
    bar_answer: BarAnswer | None = None
    # The width of the kind's barriers at a bar close (#183); None: a kind without barriers.
    width: WidthRule | None = None
    # Whether its answer counts crossed quotes (#187): their best bid and best ask each a
    # price one could trade at (a touch); False drops them, as the reference price does (a
    # mid-point). It is part of the answer rule: changing it takes a new answer version.
    crossed: bool = False
    baselines: tuple[KindBaseline, ...] = ()
    # How long its windows run from the bar close (#263): the touch target's four minutes, or
    # longer for a kind of a longer horizon. Whole minutes; the direction's is four.
    horizon: timedelta = WINDOW
    # The output's fields beside the probabilities: what each is (its meaning and unit).
    fields: Mapping[str, Meaning] = field(default_factory=dict)
    check: OutputCheck | None = None
    stats: ExtraStats | None = None

    def index(self, label: str) -> int:
        """A class's place in the kind's order."""
        return self.classes.index(label)

    def p_keys(self) -> tuple[str, str, str]:
        """The output's keys of the three probabilities: ``p_<class>``."""
        first, middle, third = (f"p_{c}" for c in self.classes)
        return first, middle, third

    def output(self, forecast: KindForecast) -> dict[str, float]:
        """A forecast as stored and served: ``p_<class>`` for each class, then the fields."""
        return {**dict(zip(self.p_keys(), forecast.probabilities, strict=True)), **forecast.fields}

    def probabilities(self, output: Mapping[str, float]) -> tuple[float, float, float]:
        """The three probabilities of a stored output, in the kind's order."""
        first, middle, third = (float(output[k]) for k in self.p_keys())
        return first, middle, third

    def invalid(self, forecast: KindForecast) -> str | None:
        """Why ``forecast`` breaks the kind's contract (an ``error``), None if it keeps it: the
        probabilities a distribution, the kind's fields and no others, each finite, and the
        kind's own check."""
        p = forecast.probabilities
        if len(p) != 3 or not all(0 <= x <= 1 for x in p) or not math.isclose(sum(p), 1, abs_tol=1e-6):
            return f"probabilities {tuple(p)} are not a distribution"
        if set(forecast.fields) != set(self.fields):
            return f"fields {sorted(forecast.fields)}, not {sorted(self.fields)}"
        if not all(math.isfinite(v) for v in forecast.fields.values()):
            return f"fields {dict(forecast.fields)} are not all finite"
        return None if self.check is None else self.check(self.output(forecast))

    def answer_to(self, window: Followed) -> Answer:
        """The kind's answer of ``window``: its answer function's, unless the shared rules
        cannot score the window (``Followed.unscorable``), then no class and their reason."""
        if self.answer is None:
            raise ValueError(f"{self.name}: no answer function (the direction's is touch.py)")
        given = self.answer(window)
        if window.unscorable is not None:
            return Answer(None, window.unscorable, given.path)
        if given.label is None:
            return Answer(None, given.unscorable or NO_ANSWER, given.path)
        if given.label not in self.classes:
            raise ValueError(f"{self.name}: answered {given.label!r}, not one of {self.classes}")
        return Answer(given.label, None, given.path)

    def baseline_cards(self) -> list[tuple[str, Card]]:
        """Its baselines' names and cards (a daily kind's are its own, #264)."""
        return [(b.name, b.card) for b in self.baselines]

    def baseline_names(self) -> frozenset[str]:
        """Its baselines' names, the lifts' (a daily kind's are its own, #264)."""
        return frozenset(b.name for b in self.baselines)

    def as_dict(self, baselines: Sequence[tuple[str, Card]]) -> dict[str, Any]:
        """As ``GET /v1/kinds`` shows it, with its baselines' cards."""
        return {
            "name": self.name,
            "title": self.title,
            "classes": list(self.classes),
            "class_titles": list(self.class_titles),
            "action": list(self.action),
            "answer_version": self.answer_version,
            "horizon_minutes": int(self.horizon / timedelta(minutes=1)),  # #263
            "fields": {name: m.as_dict() for name, m in self.fields.items()},
            "baselines": [{"name": name, "card": card.as_dict()} for name, card in baselines],
            "daily": None,  # a kind predicted once a day says when (#264, ``daily.DailyKind``)
        }


class KindRegistrationError(ValueError):
    pass


def register_kinds(kinds: Sequence[Kind]) -> tuple[Kind, ...]:
    """Check ``kinds`` in order; raises KindRegistrationError on the first refused: a name
    taken or not URL-safe, classes not three different URL-safe names, action classes
    not among them, a field named like a probability, a kind besides the direction
    without an answer function, a baseline's name taken by another."""
    names: set[str] = set()
    baselines: set[str] = set()
    for k in kinds:
        if not _NAME.match(k.name) or k.name in names:
            raise KindRegistrationError(f"{k.name!r}: taken, or not a lowercase name")
        names.add(k.name)
        if len(set(k.classes)) != 3 or not all(_NAME.match(c) for c in k.classes):
            raise KindRegistrationError(f"{k.name}: its classes {k.classes} are not three lowercase names")
        if not k.action or not set(k.action) <= set(k.classes):
            raise KindRegistrationError(f"{k.name}: action classes {k.action} not among {k.classes}")
        clash = sorted(set(k.fields) & set(k.p_keys()))
        if clash:
            raise KindRegistrationError(f"{k.name}: fields {', '.join(clash)} are named like probabilities")
        if k.name != DIRECTION and k.answer is None:
            raise KindRegistrationError(f"{k.name}: no answer function")
        minutes = k.horizon / timedelta(minutes=1)
        if minutes != int(minutes) or k.horizon < WINDOW or (k.name == DIRECTION and k.horizon != WINDOW):
            raise KindRegistrationError(
                f"{k.name}: its horizon {k.horizon} is not whole minutes of four or more"
            )
        for b in k.baselines:
            if b.name in baselines:
                raise KindRegistrationError(f"{k.name}: baseline {b.name} is taken")
            baselines.add(b.name)
    return tuple(kinds)


# The direction, as it always ran (#160, #170): its answer is the touch label (``touch.py``,
# the ``touches`` table), its baselines ``baselines.baselines_for``. The touch window counts
# crossed quotes itself (#187): ``crossed`` says so here, and nothing reads it.
DIRECTION_KIND = Kind(
    name=DIRECTION,
    title="방향",
    classes=("up", "flat", "down"),
    class_titles=("상승", "보합", "하락"),
    action=("up", "down"),
    answer_version=TARGET_VERSION,
    crossed=True,
    fields={
        "expected_ticks": Meaning(
            "4분 뒤 중간가 변화의 기대값. 세 확률과 함께 낸다.",
            "틱",
            "봉 마감에 낸다.",
        )
    },
)

# Every kind the engine predicts is registered in ``kind_registry.KINDS`` (#180): a kind's own
# module (``ranges.py``) builds on this one.
