"""The prediction engine: events in, predictions and scores out, on the Session Clock.

Live and replay drive the same two calls. Events go to ``on_event`` as they are received;
``run_due`` handles every bar close and scoring whose time has come on the clock. At a
bar close in the prediction window, from what was received before it:

- the baselines predict every symbol, at once;
- the Feature Store computes every declared feature once per symbol, and each algorithm
  predicts every symbol from its own features, in a worker thread so recording never
  waits. Each algorithm's predictions are displayed when it finishes. One that takes
  longer than its time budget is skipped for that bar close (``timed_out``), and so is
  each later bar close until it finishes. A symbol an algorithm declines for missing data
  (``NotPredicted``) is stored with the reason as its status (``no_book``), unscored.
  When the algorithms' expected work exceeds the CPU budget, the last registered are
  skipped first and the skip is recorded (``runs``); each skip halves the skipped one's
  expected time, so it is soon run (and timed) again.

Four minutes later each bar close's touch label is recorded (``touches``), what every
prediction is scored on (#170); the four-minute move's classes on bases A and B are still
written beside it (``outcomes``), unread, so an engine rolled back to before runs on.

Two consensus predictions follow (``consensus.py``): the fast one from the active
algorithms done 2 seconds after they started, the final one once they are all done.

After each day's close (+10 minutes) the algorithms' statuses are reviewed by the
lifecycle rules (``lifecycle.py``), the day's predictions lost to the time and CPU budgets
are told once, a retired algorithm is no longer judged (it runs on in shadow, below), and the
consensus weights are refitted. Where each algorithm on probation or on the bench stands
toward its next review (``stages``, #165) is worked out by the same rules, as of that review.

The reviews and the weights count only the predictions of models trained on the touch
target (#170): the engine keeps which target each model it runs learned
(``model_targets``). The first time it opens a database with a target version it has not
met (the direction's ``touch.TARGET_VERSION``, or a kind's answer version past its first,
#187) it restarts every algorithm of it not retired on probation
(``lifecycle.target_changed``, once: a ``target`` run marks it), and an algorithm's
probation counts from the first trading day it predicted with a model of that version
since: the day the morning swap installed it, nothing before, nothing while it waits.

Live, a new algorithm is enrolled only once ``admits`` lets it (its pre-validation passed,
``prevalidation.py``): from the next bar close it starts on probation. Until then it is not listed
or reviewed, and predicts in shadow alone (below); the first review it waits at tells it once for
the code, target and inputs it waits on (``runs``: ``prevalidation_waiting``, #508), saying that the
night's scheduled jobs pre-validate it by themselves: no restart tells it again until one of them
changes. One whose latest report refused the code and target it runs now (``refused``) is not
waiting: the first review after the refusal tells it once (``runs``: ``prevalidation_refused``), and
no restart tells it again until its code or target changes and it waits again (#310).
An algorithm the engine has already enrolled always runs, but for a daily kind's on probation
(#264): it runs only while ``admits`` lets it, so a new answer version, code or inputs have its
walk-forward run again before it predicts again (for its probation: in shadow it goes on).

Every registered algorithm predicts (#696): one the engine does not run by its pre-validation or its
status (waiting, refused, retired, a daily one kept out or held) predicts in shadow, its rows marked
``shadow`` (``lifecycle.SHADOW``) where an enrolled one's carry its status
(``predictions.lifecycle``). At a bar close the shadow ones start once the verdicts are made, so
nothing the consensus, the signals and the verdicts wait for waits on them, and the CPU budget skips
them first; a daily kind's run after its enrolled ones. Each has its time limit, as any algorithm.
No review, stage or probation counts a shadow row (``predictions.NOT_SHADOW``); the leaderboard and
the statistics show them. Only a daily kind not run yet (``DailyKind.awaiting``) predicts nothing.

Live, a running bar-close algorithm whose code changed is judged again (#454, ``revisions.py``):
at each start the engine records the code each one runs (``revisions``); one whose code is not
the revision it ran before (nor a reviewed refactor of it) goes back on probation once
(``lifecycle.CODE_CHANGED``), retired ones aside. That probation counts from its first whole trading
day, is never simulated, and becomes active only once the pre-validation of the new code (a
scheduled job, on the recorded days after the change) has passed too; a refusal retires it at the
next bar close, told once at the next review. The first start of this records each one's code as it
runs and changes no status.

The symbols' bars and reference prices, and the bars of the series that may lead them
(``features.Leaders``: futures, indices, US daytime trades, ETFs), come from the Market
View (``market_view.py``) as they stood at the bar close. Training on a recorded day goes
through the same view.

At each bar close it predicts, once the baselines' predictions are stored, the engine judges the
SMT divergence of three pairs (#520, ``smt.py``) from the same bars, and keeps the episodes that
begin there (``smt_events``, source ``live``). It runs on the event loop and reads a few dozen bars;
a failure is logged as a warning and the bar close's predictions go on.

Special situations (``special.py``): a symbol in a VI at a bar close is not predicted
(``vi``), and a prediction whose four minutes a VI, circuit breaker or sidecar touches is
special, marked when it is made or as soon as the situation is learned. Each window learned
is kept in ``special_windows`` (#236), its later versions over it (a VI's release).

Live, each bar close watches the event loop it runs on (``watch_loop``, #538, ``loop_watch.py``):
that loop also reads KIS and streams the best quotes to the trader. A bar close that keeps it over
a second without a turn logs one WARNING with how long, which of its steps ran then and how much
garbage collection (#621). Live, each bar close also ends with a garbage collection that freezes
what is alive, so that Python's full collections no longer walk the session's quote logs
(``garbage``, ``garbage.py``). Once its next work is over ``IDLE`` away after a bar close (the
session's last answers), and after each daily review, the live engine rests (#623,
``Garbage.rest``): the Market View lets go of what the session kept and the next one does not read
(``MarketView.let_go``), the frozen are released, the memory freed goes back to the system, and
one INFO line says what went and what the process holds.

The prediction window is continuous trading, from the first bar close after the open
until the last bar close whose target (t + 4 minutes) is still in continuous trading:
09:01–15:16 on a normal day.

With the touch target (``target_version`` 3, 2 before #187; #169) the algorithms' features carry its
terms at the bar close (half the spread there), so the shared normal forecast gives the
chances of reaching the targets; the baselines are the touch target's (``TOUCH_BASELINES``):
the last four minutes' direction is the label of the touch window that just ended, read
from the Market View (not predicted, ``no_previous``, when it could not be scored), the
random one draws in the day's touch labels' proportions, and the climate baseline calls
its table's direction (``climate.py``), a model the morning swap installs.

Beside the direction the engine predicts the other kinds registered (#179, ``kinds.py``),
each at the same bar closes from the same Market View: a kind's baselines predict every
symbol at once, just before the algorithms start (#538: while they run, the baselines' store
calls waited for the algorithms' threads to let go of the GIL), and its algorithms run with the
direction's, in the same worker threads, under the same time limit and CPU budget,
registered after them (the budget skips them first). A kind's predictions go to
``kind_predictions``, each with the bar close's k it was made with (#200), its mid-point and tick
size (``anchor``, ``tick``, #210: the reference its answer is measured from) and, for a kind with
barriers (#183), their width fixed there (``width_ticks``); at t + 4 minutes
each window's answer, by the kind's answer function on the quotes the touch label reads, goes
to ``kind_answers`` (special when a VI, circuit breaker or sidecar known at its bar close, or
learned since, touches it) and scores every predictor of the kind there. Each answer is made
once, in a worker thread (#538): a longer kind's window holds up to an hour of quotes, and the
baselines' last window over is that answer. The kinds have no
consensus and no signals. Each daily review also judges each kind's algorithms against that
kind's baselines, within the kind. A kind that fails (its answer function, a baseline) is
logged and never holds up the direction. A kind baseline that predicts from a table (the
range's climate, #180, ``kinds.TableBaseline``) runs the table the engine holds for it
(``kind_tables``): its defaults, the one given at start, then whichever the morning swap
installs, as the direction's climate table.

The daily kinds (#264, ``daily.py``: ``open_to_close``) are run apart from the bar closes, by a
``daily.DailyRunner``: once a trading day at their time (08:50) their baselines and admitted
algorithms predict every symbol from the KRX days before, and once the day's KRX day candles
are stored (after 20:30) each day's answer is written. Each daily review judges a daily kind's
algorithms by its own days (``DailyKind.review_rules``: 20, 40 and 60 days with an answer).
An engine given no source of the KRX days (``daily``: replays, tests) runs no daily kind.

A probation that begins for a new algorithm or a new target can be simulated (#298,
``simulation.py``, by a scheduled job): the latest trading days replayed as if the algorithm had
been on probation, kept apart from the live predictions (``sim_kind_predictions``). The reviews
and the stages count those days first, then the live days after them (``lifecycle.Simulated``);
the leaderboard, the statistics and the consensus weights read the live predictions only.
"""

from __future__ import annotations

import asyncio
import contextlib
import heapq
import json
import logging
import math
import time as timer
import weakref
from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from functools import partial
from typing import TYPE_CHECKING, Any, TypeVar

from krx_quant.algorithms import (
    ALGORITHMS,
    KIND_ALGORITHMS,
    Abstention,
    Algorithm,
    KindAlgorithm,
    Model,
    NotPredicted,
    PredictionContext,
    forecast_of,
    register,
    register_kind_algorithms,
)
from krx_quant.algorithms.forecasts import forecasting
from krx_quant.barriers import CLASSES as BARRIER_CLASSES
from krx_quant.baselines import NO_PREVIOUS, TOUCH_BASELINES, Baseline, Forecast, Inputs
from krx_quant.climate import CLIMATE
from krx_quant.climate import DEFAULT as NO_CLIMATE
from krx_quant.climate import direction as climate_direction
from krx_quant.clock import SessionClock
from krx_quant.config import MAX_REVIEW_DAY, Settings
from krx_quant.consensus import FAST, FINAL, NAMES, NO_WEIGHTS, SPANS, Timing, Weights, pool
from krx_quant.consensus import fit as fit_weights
from krx_quant.daily import (
    DailyAlgorithm,
    DailyKind,
    DailyRunner,
    DailySource,
    DailyStored,
    register_daily_algorithms,
)
from krx_quant.daily import Features as DailyFeatures
from krx_quant.features import (
    REGIME_CODES,
    FeatureStore,
    FeatureVector,
    Leaders,
    PreviousDays,
    TouchTerms,
    Value,
)
from krx_quant.feeds import (
    BOOK,
    BOOK_SNAPSHOT,
    FOREIGN_ESTIMATE,
    INDEX,
    INDEX_REST,
    KOSPI200_FUTURES,
    KRX_TRADE,
    MEMBER,
    MEMBER_REST,
    NXT_TRADE,
    OPTION,
    PROGRAM,
    TRADE,
    US_TRADE,
    USD_FUTURES,
    VI_STATUS,
)
from krx_quant.feeds.base import KST, LEVELS
from krx_quant.garbage import IDLE, REST_RUN, Garbage
from krx_quant.kind_registry import DAILY_KINDS, KINDS
from krx_quant.kind_stats import kind_scoring
from krx_quant.kinds import (
    DIRECTION,
    DIRECTION_KIND,
    Answer,
    KindBaseline,
    KindForecast,
    KindInputs,
    TableBaseline,
    table_baselines,
)
from krx_quant.kinds import NO_PREVIOUS as KIND_NO_PREVIOUS
from krx_quant.kinds import Kind as PredictionKind
from krx_quant.lifecycle import (
    CODE_CHANGED,
    CODE_PASSED,
    CODE_REFUSED,
    REGISTERED,
    SHADOW,
    Review,
    Rules,
    Simulated,
    Stage,
    Standing,
    Status,
    review,
    target_changed,
)
from krx_quant.lifecycle import stages as stages_now
from krx_quant.loop_watch import LOOP_HELD, LoopWatch, held_detail, held_text
from krx_quant.market_calendar import MarketCalendar, first_whole_day
from krx_quant.market_view import (
    FUTURES_SERIES,
    HORIZON,
    INDEX_FEEDS,
    LOOKAHEAD_DAYS,
    BarsAtClose,
    KindWindow,
    MarketView,
    next_bar_close,
)
from krx_quant.predictions import (
    REVISION_CHANGED,
    REVISION_FIRST,
    REVISION_PASSED,
    REVISION_REFUSED,
    NewKindPrediction,
    NewPrediction,
    Pending,
    PredictionStore,
    Revision,
    Score,
)
from krx_quant.pricing import Direction, classify, flat_ticks, most_likely, tick_size
from krx_quant.reference import Bar
from krx_quant.signals import SENT, Blocked, Judged, SignalRule, Signals
from krx_quant.smt import LIVE as SMT_LIVE
from krx_quant.smt import Detector as SmtDetector
from krx_quant.special import HALT_STATUS, KOSPI, Kind, SpecialSituations, Window, in_halt, in_vi, touching
from krx_quant.stats import DEFAULT_MIN_SAMPLE
from krx_quant.touch import RESTARTED, TARGET_VERSION, WINDOW
from krx_quant.verdict import VERDICT_VERSION, Held, Horizon, Market, Member, Side, verdict

if TYPE_CHECKING:  # the pre-validation replays through this module: its type only
    from krx_quant.prevalidation import Refusal
    from krx_quant.revisions import RevisionGate

log = logging.getLogger(__name__)

Event = tuple[str, dict[str, Any]]  # (feed TR ID, normalized row)
_F = TypeVar("_F")  # a forecast: the direction's or another kind's (#179)
Notify = Callable[[str], Awaitable[bool]]  # send an alert; whether it went


@dataclass(frozen=True)
class PredictionsStored:
    """Predictions just stored: displayed now."""

    bar_close: datetime
    ids: Mapping[str, list[int]]  # by predictor


@dataclass(frozen=True)
class OutcomesScored:
    """Outcomes just scored."""

    ids: list[int]  # their predictions'


@dataclass(frozen=True)
class KindPredictionsStored:
    """Predictions of a kind besides the direction just stored (#179): displayed now."""

    kind: str
    bar_close: datetime
    ids: Mapping[str, list[int]]  # by predictor


@dataclass(frozen=True)
class KindAnswered:
    """A kind's answers just stored (#179), by (symbol, bar close): what its predictions there
    are scored on."""

    kind: str
    keys: list[tuple[str, datetime]]


Change = PredictionsStored | OutcomesScored | KindPredictionsStored | KindAnswered
Listener = Callable[[Change], None]  # told of each, as it happens (the stream)
# The trader's open positions at a time, by symbol (#676, ``trader.store.read_held``): read off the
# event loop at each bar close's verdict.
HeldNow = Callable[[datetime], Mapping[str, Held]]
# Whether the engine may start a new algorithm (``prevalidation.admits``).
Admits = Callable[[Algorithm | KindAlgorithm | DailyAlgorithm], bool]
# Whether its latest pre-validation refused the code and target it runs now (``prevalidation.refusals``).
Refused = Callable[[Algorithm | KindAlgorithm | DailyAlgorithm], "Refusal | None"]
# A day's macro regime as decided before a time (the day's open); None unknown.
Regimes = Callable[[date, datetime], str | None]

# The ``runs`` kind of a pre-validation refusal told (#310): its detail is what was refused.
REFUSED_RUN = "prevalidation_refused"
# The ``runs`` kind of a wait for a pre-validation told (#508): its detail is what that
# pre-validation would judge (``prevalidation.judging``: the code, target and inputs), as one key.
WAITING_RUN = "prevalidation_waiting"
# The ``runs`` kind of a refused revision told (#454): its detail is the revision's id.
REVISION_REFUSED_RUN = "revision_refused"
# The ``runs`` kind of a review day of a probation whose new code waits for its pre-validation
# (#454): its detail is JSON, the revision's id, the days counted, whether it passed and why.
REVISION_REVIEW_RUN = "revision_review"
# Each day's algorithm review runs this long after the close, when everything is scored.
REVIEW_AFTER_CLOSE = timedelta(minutes=10)
# How far back a starting engine looks for a kind's predictions left without their answer.
_KIND_PENDING = timedelta(days=7)
# The bar close's steps on the event loop, as a WARNING names what held it (#538, ``loop_watch``).
SCORING = "scoring"
ANSWERS = "the kinds' answers"
INPUTS = "the inputs"
RECORDS = "records"
SMT_STEP = "SMT"
KIND_BASELINES = "the kinds' baselines"
CONSENSUS = "consensus"
VERDICT = "the verdicts"  # each symbol's at the bar close, its members done (#676, ``verdict.py``)
GARBAGE = "garbage collection"  # the live engine's after each bar close (#621, ``garbage.py``)
SQLITE = "SQLite writes"  # the store's write transactions, inside the steps that store (#637)
# Days with predictions a review reads back over: the longest rule counts 60 on the bench,
# and a status can be older than that.
_REVIEW_DAYS = MAX_REVIEW_DAY  # prediction days a review reads back

_BOOK_COLUMNS = (
    "symbol",
    "received_at",
    "ask_px_1",
    "bid_px_1",
    *(f"{side}_qty_{i}" for side in ("ask", "bid") for i in LEVELS),
)
# The feeds the engine reads, and the columns it uses from each.
ENGINE_FEEDS: dict[str, tuple[str, ...]] = {
    # Integrated trades make the bars; their best quotes are integrated quotes too, and their
    # aggressor side counts (Shannon's). Their halt flag and time code tell a market halt and its
    # reopening (#625, ``special.py``).
    TRADE.tr_id: (
        "symbol",
        "event_time",
        "received_at",
        "price",
        "qty",
        "ask1",
        "bid1",
        "side",
        "halted",
        "hour_cls",
    ),
    # The book's sizes make the microprice (Kalman's), the order flow imbalance and the
    # depth imbalance (Shannon's); its time code tells a halt's reopening auction (#625).
    BOOK.tr_id: (*_BOOK_COLUMNS, "hour_cls"),
    # The REST book snapshot replaces the integrated book when it is cut by the session limit.
    BOOK_SNAPSHOT.tr_id: _BOOK_COLUMNS,
    # KRX-only trades carry KRX's best quotes, for the 30 seconds before NXT opens; the
    # ETFs' make their bars.
    KRX_TRADE.tr_id: ("symbol", "event_time", "received_at", "price", "qty", "ask1", "bid1"),
    # NXT's trades, for its share of the volume (Shannon's).
    NXT_TRADE.tr_id: ("symbol", "received_at", "qty"),
    # A symbol's VIs: a bar close inside one is not predicted; a window touching one is special.
    # Their type is kept with the window (#236).
    VI_STATUS.tr_id: ("symbol", "event_time", "received_at", "end_time", "active", "kind"),
    # The KOSPI index and KOSPI200 futures, for circuit breakers and sidecars; with the other
    # indices, USD futures and US daytime trades, they lead.
    INDEX.tr_id: ("symbol", "event_time", "received_at", "value", "change_pct"),
    INDEX_REST.tr_id: ("symbol", "event_time", "received_at", "value", "change_pct"),
    KOSPI200_FUTURES.tr_id: ("symbol", "event_time", "received_at", "price", "qty", "change_pct"),
    USD_FUTURES.tr_id: ("symbol", "event_time", "received_at", "price", "qty"),
    US_TRADE.tr_id: ("symbol", "event_time", "received_at", "price", "qty"),
    # The symbols' flows, for Turing and Shannon: program trading, foreign member firms
    # (live or by REST), and brokers' estimates of investor buying.
    PROGRAM.tr_id: ("symbol", "received_at", "net_value"),
    MEMBER.tr_id: ("symbol", "received_at", "foreign_net_qty"),
    MEMBER_REST.tr_id: ("symbol", "received_at", "foreign_net_qty"),
    FOREIGN_ESTIMATE.tr_id: ("symbol", "received_at", "slot", "foreign_net_qty", "institution_net_qty"),
    # The options near the money, for their at-the-money implied volatility (Markov's).
    OPTION.tr_id: ("symbol", "received_at", "implied_vol", "delta"),
}


# The special situations that block a final consensus (``_blocked``): a VI, a circuit breaker and a
# market halt (#625); not a sidecar, which halts program trading alone.
_BLOCKING = (Kind.VI, Kind.CIRCUIT_BREAKER, Kind.MARKET_HALT)


def partitions(feed: str, symbols: Sequence[str], leaders: Leaders) -> list[str] | None:
    """The partitions of ``feed`` the engine reads; None for every one (a futures feed's
    single front month, whichever it was that day, and the options near the money that day).
    """
    if feed in FUTURES_SERIES or feed == OPTION.tr_id:
        return None
    if feed in INDEX_FEEDS:
        return sorted({KOSPI, *leaders.indices})
    if feed == US_TRADE.tr_id:
        return list(leaders.us)
    if feed == KRX_TRADE.tr_id:
        return [*symbols, *leaders.etfs]
    return list(symbols)


@dataclass(frozen=True)
class Budget:
    per_algorithm: float = 10.0  # seconds one algorithm may take for a bar close, all symbols
    cpu: float = 30.0  # seconds of expected algorithm work per bar close, all together

    @classmethod
    def of(cls, settings: Settings) -> Budget:
        return cls(settings.algorithm_seconds, settings.cpu_budget_seconds)


def _start_of(day: date) -> datetime:
    """Midnight starting ``day``, KST."""
    return datetime.combine(day, time(), KST)


# How much one bar close's measured time moves an algorithm's expected time.
_COST_WEIGHT = 0.3


@dataclass(frozen=True)
class CpuUse:
    """The CPU budget now, for the status."""

    budget: Budget
    expected: Mapping[str, float]  # seconds per bar close of each algorithm not retired, once timed
    skipped_today: int  # algorithm runs skipped for the budget today

    @property
    def expected_seconds(self) -> float:
        return sum(self.expected.values())

    @property
    def usage(self) -> float | None:
        """The expected work as a share of the budget; over 1, the last registered are skipped."""
        return self.expected_seconds / self.budget.cpu if self.budget.cpu > 0 else None


@dataclass(frozen=True)
class _AtClose:
    """What is fixed for a symbol at a bar close."""

    mid: float | None
    features: Mapping[str, Value]
    touch: TouchTerms | None = None  # the touch target's terms (target versions 2 and up)


def _marked_as(kind: PredictionKind) -> str | None:
    """How a ``target`` run names ``kind`` (#187): the direction's carries no name, as #170's."""
    return None if kind.name == DIRECTION else kind.name


class PredictionEngine:
    def __init__(
        self,
        store: PredictionStore,
        calendar: MarketCalendar,
        symbols: Sequence[str],
        fee_rate: float,
        clock: SessionClock,
        *,
        algorithms: Sequence[Algorithm] = ALGORITHMS,
        models: Mapping[str, Model] | None = None,
        budget: Budget | None = None,
        feature_store: FeatureStore | None = None,
        min_sample: int = DEFAULT_MIN_SAMPLE,
        notify: Notify | None = None,
        consensus_timing: Timing | None = None,
        leaders: Leaders | None = None,
        signals: Signals | None = None,
        signal_rule: SignalRule | None = None,
        regimes: Regimes | None = None,
        admits: Admits | None = None,
        refused: Refused | None = None,
        revisions: RevisionGate | None = None,
        commit: str = "unknown",
        review_rules: Rules | None = None,
        target_version: int,  # no default: the settings' (KRX_TARGET_VERSION, #647)
        climate: Model | None = None,
        kinds: Sequence[PredictionKind] = KINDS,
        kind_algorithms: Sequence[KindAlgorithm] = KIND_ALGORITHMS,
        previous: PreviousDays | None = None,
        daily: DailySource | None = None,
        daily_kinds: Sequence[DailyKind] = DAILY_KINDS,
        daily_algorithms: Sequence[DailyAlgorithm] = (),
        daily_features: DailyFeatures | None = None,
        daily_feature_names: Collection[str] = (),
        watch_loop: bool = False,
        garbage: Garbage | None = None,
        held: HeldNow | None = None,
    ) -> None:
        self.store = store
        self.commit = commit  # the code it runs (the deploy's KRX_COMMIT)
        self.started_at = clock.now()
        self.review_rules = review_rules or Rules()  # when statuses are judged (#87)
        self.calendar = calendar
        self.symbols = tuple(symbols)
        self.fee_rate = fee_rate
        self.clock = clock
        self.registered = register(algorithms)
        # What is learned and forecast (KRX_TARGET_VERSION, #169), and the baselines of it.
        self.target_version = target_version
        self.baselines: tuple[Baseline, ...] = TOUCH_BASELINES
        self.baseline_names = frozenset(b.name for b in self.baselines)
        self.climate = climate or NO_CLIMATE  # the climate baseline's table (the touch target)
        # The kinds predicted besides the direction (#179), in order, and their algorithms.
        self.kinds: tuple[PredictionKind, ...] = tuple(k for k in kinds if k.name != DIRECTION)
        clash = {b.name for k in self.kinds for b in k.baselines} & (
            {a.name for a in self.registered} | self.baseline_names | set(NAMES)
        )
        if clash:
            raise ValueError(
                f"kind baselines named like the direction's predictors: {', '.join(sorted(clash))}"
            )
        self.kind_registered = register_kind_algorithms(
            kind_algorithms,
            (DIRECTION_KIND, *self.kinds),
            taken={a.name for a in self.registered}
            | self.baseline_names
            | set(NAMES)
            | {b.name for k in self.kinds for b in k.baselines},
        )
        # The daily kinds (#264), run only with a source of the KRX days, and their algorithms.
        self.daily_kinds: tuple[DailyKind, ...] = tuple(daily_kinds) if daily is not None else ()
        taken: set[str] = (
            {a.name for a in self.registered}
            | {a.name for a in self.kind_registered}
            | self.baseline_names
            | set(NAMES)
            | {b.name for k in self.kinds for b in k.baselines}
        )
        daily_clash = {b.name for k in self.daily_kinds for b in k.daily_baselines} & taken
        if daily_clash:
            raise ValueError(f"daily baselines named like other predictors: {', '.join(sorted(daily_clash))}")
        self.daily_registered = register_daily_algorithms(
            daily_algorithms if self.daily_kinds else (),
            self.daily_kinds,
            taken=taken | {b.name for k in self.daily_kinds for b in k.daily_baselines},
            features=daily_feature_names,
        )
        self.daily_algorithms: tuple[DailyAlgorithm, ...] = ()  # those admitted, as the others
        given = models or {}
        everyone: list[Algorithm | KindAlgorithm | DailyAlgorithm] = [
            *self.registered,
            *self.kind_registered,
            *self.daily_registered,
        ]
        self.models = {a.name: given.get(a.name) or a.default_model() for a in everyone}
        # The tables the kinds' table baselines predict from (#180), by name: given, else the
        # defaults; a swap installs the night's (``switch_models``).
        self.kind_tables: dict[str, Model] = {
            b.name: given.get(b.name) or b.default_table() for _, b in table_baselines(self.kinds)
        }
        self._admits = admits  # without: every registered algorithm runs (replay, tests)
        self._refused = refused  # without: none refused, those not admitted wait (replay, tests)
        # The code each running bar-close algorithm runs (#454); without: none kept (replay, tests).
        self._revisions = revisions
        self._revision_of: dict[str, Revision] = {}  # each one's latest revision, as stored
        self.algorithms: tuple[Algorithm, ...] = ()  # the registered ones that run, in order
        self.kind_algorithms: tuple[KindAlgorithm, ...] = ()  # likewise, of the other kinds
        # Every registered bar-close algorithm's features: those run only in shadow are run too (#696).
        self._feature_names = sorted({f for a in self._bar_close() for f in a.features})
        # What each algorithm's rows carry (#696, ``predictions.lifecycle``): its status, or shadow.
        self._lifecycle: dict[str, str] = {}
        self._told_held: set[str] = set()  # held for their review days, already alerted (#310)
        self._budget = budget or Budget()
        self._features = feature_store or FeatureStore()
        self._cost: dict[str, float] = {}  # expected seconds per bar close, by algorithm
        self._busy: set[str] = set()  # algorithms still working past their budget
        # The bar close's event loop, watched live (#538): the one going on, if any.
        self._watch_loop = watch_loop
        self._watch: LoopWatch | None = None
        # The store's writes timed as a step of the bar close; its checkpoints off the loop (#637).
        # Through a weak reference: the store keeps no hold on the engine, which goes with its
        # last reference (each replayed day's before the next, #383).
        engine = weakref.ref(self)
        store.time_writes(lambda: contextlib.nullcontext() if (e := engine()) is None else e._step(SQLITE))
        store.checkpoint_aside()
        # The live engine's garbage collection after each bar close (#621); without: Python's own.
        self.garbage = garbage
        # The trader's positions, for a verdict's SELL (#676); without: nothing held (replay, tests).
        self._held = held
        # Each barrier member's evidence (#676) by (kind, algorithm): counted at start and at each
        # daily review, never during the session (a few hundred milliseconds over 40 days).
        self._evidence: dict[tuple[str, str], int] = {}
        # The kinds' answers made at the last scoring, by (kind, symbol, bar close): the kinds'
        # baselines read the window just over from them (#538).
        self._answered: dict[tuple[str, str, datetime], Answer] = {}
        self._special = SpecialSituations(calendar)
        self.leaders = leaders or Leaders()
        self._last_tick = clock.now()
        # The latest bar close this engine predicted (#687): None until it predicts one. Whether this
        # process ran a session that day, for the restart after the close (``close_restart.py``).
        self.predicted_at: datetime | None = None
        # Each symbol's previous trading day's ranges (#181, ``range_prev_day``): without, unknown.
        self._market = MarketView(
            calendar,
            symbols,
            self.leaders,
            self._last_tick,
            self.fee_rate,
            kinds=self.kinds,
            previous=previous,
        )
        # The SMT of each bar close predicted (#520), from the view's bars there.
        self._smt = SmtDetector(calendar, SMT_LIVE)
        # Outcomes waiting for their target time: (target_at, prediction_id, pending).
        self._pending: list[tuple[datetime, int, Pending]] = []
        for p in store.pending():
            heapq.heappush(self._pending, (p.target_at, p.prediction_id, p))
        # The kinds' predictions made before a restart without their answer (#179): (target
        # time, kind, symbol, bar close). Their windows were not followed through: answered
        # so (``restarted``) at their target time, as the touch labels are.
        self._kind_pending: list[tuple[datetime, str, str, datetime]] = []
        if self.kinds:
            versions = {k.name: k.answer_version for k in self.kinds}
            horizons = {k.name: k.horizon for k in self.kinds}  # each kind's (#263)
            for kind, symbol, bar_close in store.kind_unanswered(versions, self._last_tick - _KIND_PENDING):
                heapq.heappush(self._kind_pending, (bar_close + horizons[kind], kind, symbol, bar_close))
        self.min_sample = min_sample
        self.consensus_timing = consensus_timing or Timing()
        self.weights: Weights = store.latest_weights() or NO_WEIGHTS
        self.notify = notify  # where alerts go (Telegram, live)
        self.training: dict[str, Any] | None = None  # the last night's training, as the swap saw it
        # Each algorithm's report of that night, as stored (``nightly``), by algorithm (#165).
        self.night_reports: dict[str, dict[str, Any]] = {}
        # The statuses' stages and what they were computed for (``stages``).
        self._stages: tuple[Any, dict[str, Stage]] | None = None
        self.listeners: list[Listener] = []
        self.signals = signals  # Telegram signal alerts, off without
        # The signal settings in force, whether the alerts are on or off: the status serves them,
        # and the screen's signal alerts follow them unless the user set their own (#617).
        self.signal_rule = signal_rule or SignalRule()
        self._signals_resumed = False  # from the signals sent before a restart, at the first final
        self._regimes = regimes  # without: no macro regime, and nothing to say about it
        self._regime_days: dict[date, str | None] = {}
        # Reviews pick up where the last one left off: one missed while down runs at start.
        self._reviewed_through = store.last_run("review") or self._last_tick
        # The latest review over (#687): set once ``_review`` has returned, never while it runs.
        self._review_done = self._reviewed_through
        self._daily: DailyRunner | None = None
        self._daily_events: Callable[[str, dict[str, Any]], None] | None = None
        # A daily kind awaiting a probe's result (#310, ``DailyKind.awaiting``) is listed, not run.
        running_kinds = [k for k in self.daily_kinds if k.runs]
        if running_kinds:
            self._daily_events = getattr(daily_features, "on_event", None)
            self._daily = DailyRunner(
                running_kinds,
                store,
                calendar,
                symbols,
                fee_rate,
                daily,  # type: ignore[arg-type]  # not None with daily kinds
                clock.now,
                tell=self._tell_daily,
                running=self._running_daily,
                features=daily_features,
                seconds=self._budget.per_algorithm,
                lifecycle=self._lifecycle.get,
            )
        self._restart_on_target()
        self._check_revisions()
        self._admit()
        self._record_models(self.models)
        self._count_evidence()

    def _restart_on_target(self) -> None:
        """Once per target version (#170, #187): when the statuses first go by the direction's
        target version (``touch.TARGET_VERSION``), every direction algorithm with a status
        other than retired restarts on probation, its predictions so far (the old target's)
        never judged again; likewise a kind's algorithms when its answer version, past the
        first, is new. A name no longer registered goes with the direction, as before #187."""
        targets = [
            (DIRECTION_KIND, TARGET_VERSION),
            *((k, k.answer_version) for k in self.kinds if k.answer_version > 1),
        ]
        due = [(k, v) for k, v in targets if not self.store.target_marked(v, _marked_as(k))]
        if not due:
            return
        now = self.clock.now()
        standings = self.store.statuses()
        for kind, version in due:
            reason = target_changed(kind.name, version)
            for name, standing in standings.items():
                if (
                    standing.status is Status.RETIRED
                    or (self.kind_of_algorithm(name) or DIRECTION_KIND).name != kind.name
                ):
                    continue
                self.store.add_status(name, now, standing.status, Status.PROBATION, reason)
                log.info("%s: %s -> probation (%s)", name, standing.status.value, reason)
            self.store.mark_target(version, now, _marked_as(kind))

    def _check_revisions(self) -> None:
        """At start (#454): record the code each enrolled bar-close algorithm runs, and put one whose
        code changed since its last revision back on probation (``revisions.py``). One without a
        revision yet (the first start of this, one never recorded) has its code recorded as its
        first revision, its status as it is; a retired one with a revision is left as it was."""
        if self._revisions is None:
            return
        standings = self.store.statuses()
        self._revision_of = self.store.revisions()
        now = self.clock.now()
        for a in self._bar_close():
            standing = standings.get(a.name)
            if standing is None:
                continue  # not enrolled: its first revision is recorded when it is (``_admit``)
            last = self._revision_of.get(a.name)
            if last is not None and standing.status is Status.RETIRED:
                continue  # never revived: a later unretire and start find its code changed then
            try:
                self._revise(a, last, now, standing.status)
            except Exception:  # its code unreadable, say: it runs on as before, and the engine starts
                log.exception("%s: its code could not be recorded; its status stays as it is", a.name)

    def _revise(
        self, algorithm: Algorithm | KindAlgorithm, last: Revision | None, now: datetime, status: Status
    ) -> None:
        """Record the code ``algorithm`` runs against its ``last`` revision (#454); ``status`` is its
        status as read."""
        assert self._revisions is not None
        name, code = algorithm.name, self._revisions.code(algorithm)
        keep = partial(self.store.add_revision, name, code.digest, code.target_version, code.inputs, now)
        if last is None:
            revision = keep(boundary=now, state=REVISION_FIRST)
            log.info("%s: runs code %s, recorded as its first revision", name, code.digest)
        elif last.code == code.digest:
            return
        elif (same := self._revisions.refactor(name, last.code, code.digest)) is not None:
            revision = keep(
                boundary=last.boundary,
                state=last.state,
                mapped_from=last.code,
                verdict=last.verdict,
                judged_at=last.judged_at,
                detail=same.evidence,
            )
            log.info("%s: code %s -> %s, the same revision (%s)", name, last.code, code.digest, same.evidence)
        else:
            revision = keep(boundary=now, state=REVISION_CHANGED, restart=status)
            log.info(
                "%s: code %s -> %s: %s -> probation (%s)",
                name,
                last.code,
                code.digest,
                status.value,
                CODE_CHANGED,
            )
        self._revision_of[name] = revision

    def _judge_revisions(self) -> None:
        """Keep the verdict of each changed revision's pre-validation once its report is in (#454):
        a pass makes it active if its latest probation review day passed while it waited
        (``_held_review``), else lets its next review day do so; a refusal retires it (told at the
        next review)."""
        if self._revisions is None:
            return
        pending = [r for r in self._revision_of.values() if r.pending]
        if not pending:
            return
        standings = self.store.statuses()
        for revision in pending:
            name = revision.algorithm
            algorithm = self._bar_close_named(name)
            try:
                verdict = None if algorithm is None else self._revisions.verdict(algorithm, revision.boundary)
            except Exception:  # read again at the next bar close
                log.exception("%s: the verdict of its new code could not be read", name)
                continue
            if verdict is None:
                continue
            now = self.clock.now()
            standing = standings.get(name)
            if verdict.passed:
                passed = None if standing is None else self._held_review(revision, standing)
                promote = None
                if passed is not None:
                    promote = (Status.PROBATION, Status.ACTIVE, f"{passed}; {CODE_PASSED}")
                self._revision_of[name] = self.store.judge_revision(
                    revision, REVISION_PASSED, now, None, change=promote
                )
                log.info("%s: its new code %s passed its pre-validation", name, revision.code)
                if promote is not None:
                    log.info("%s: probation -> active (%s)", name, promote[2])
                continue
            retire = None
            if standing is not None and standing.status is not Status.RETIRED:
                retire = (standing.status, Status.RETIRED, f"{CODE_REFUSED}: {verdict.why}")
            self._revision_of[name] = self.store.judge_revision(
                revision, REVISION_REFUSED, now, verdict.why, change=retire
            )
            log.info("%s: -> retired (%s: %s)", name, CODE_REFUSED, verdict.why)

    def _held_review(self, revision: Revision, standing: Standing) -> str | None:
        """The reason of the review day ``revision``'s algorithm passed live while its new code
        waited for its pre-validation (#454: of the revision begun at its boundary, a reviewed
        refactor's row too), if the latest review day of its probation now
        (``standing``: the stint, since a new target's restart begins another) passed; None if it is
        not on probation, no review day of this stint was reached, or the latest did not pass
        (``_review`` keeps each: ``REVISION_REVIEW_RUN``)."""
        if standing.status is not Status.PROBATION:
            return None
        detail = self.store.last_detail(REVISION_REVIEW_RUN, revision.algorithm)
        try:
            kept = json.loads(detail or "null")
        except ValueError:
            return None
        if (
            not isinstance(kept, dict)
            or kept.get("boundary") != revision.boundary.astimezone(KST).isoformat()
            or kept.get("stint") != standing.since.astimezone(KST).isoformat()
            or not kept.get("passed")
        ):
            return None
        return str(kept.get("reason") or "")

    def _review_day(self, name: str, standing: Standing, day: date) -> int | None:
        """The days ``name``'s probation counts at ``day``'s review if that is one of its review days
        (``Rules.probation_days``), as the review counts them (live days: a held revision is never
        simulated); None otherwise."""
        kind = self.kind_of_algorithm(name)
        if kind is None or kind is DIRECTION_KIND:
            days = self.store.prediction_days(day, _REVIEW_DAYS)
            first = self._counted_from(name, standing)
        else:
            days = self.store.kind_prediction_days(kind.name, day, _REVIEW_DAYS)
            first = self._kind_counted_from(kind, name, standing)
        counted = 0 if first is None else sum(1 for d in days if d >= first)
        return counted if counted in self.review_rules.probation_days else None

    def _bar_close(self) -> list[Algorithm | KindAlgorithm]:
        """The registered bar-close algorithms: the direction's, then the other kinds'."""
        return [*self.registered, *self.kind_registered]

    def _bar_close_named(self, name: str) -> Algorithm | KindAlgorithm | None:
        return next((a for a in self._bar_close() if a.name == name), None)

    def revising(self) -> list[tuple[Algorithm | KindAlgorithm, Revision]]:
        """The bar-close algorithms not retired whose changed code waits for its pre-validation
        (#454), in registration order, each with its revision: what the scheduled job runs."""
        retired = _with_status(self.store.statuses(), Status.RETIRED)
        return [
            (a, r)
            for a in self._bar_close()
            if (r := self._revision_of.get(a.name)) is not None and r.pending and a.name not in retired
        ]

    def _record_models(self, models: Mapping[str, Model]) -> None:
        """Keep the target each of ``models`` was trained on, for the reviews (#170)."""
        self.store.record_models([(name, m.version, m.target_version) for name, m in models.items()])

    def _admit(self) -> None:
        """Run the algorithms enrolled before and the new ones admitted; a new one starts on
        probation, a new bar-close one with its code as its first revision (#454). The verdicts of
        changed revisions' pre-validations are kept first (``_judge_revisions``)."""
        self._judge_revisions()
        known = self.store.statuses()
        admitted = [a for a in self.registered if a.name in known or self._admits is None or self._admits(a)]
        kind_admitted = [
            a for a in self.kind_registered if a.name in known or self._admits is None or self._admits(a)
        ]
        # A daily algorithm on probation runs only while its pre-validation stands for the code,
        # the answer version and the inputs it runs now (#264): its walk-forward takes a minute,
        # so a change (``open_to_close``'s answer version 2) has it judged again first, that
        # night. One judged live already (active, on the bench) runs on, as any algorithm does.
        # One of a kind not run yet (#310, ``DailyKind.awaiting``) is not run either, nor one of a
        # kind whose review days are not fixed yet (``DailyKind.review_hold``), its pre-validation
        # passed or not: its probation would be judged on other days.
        daily_admitted = [
            a
            for a in self.daily_registered
            if self._kind_runs(a)
            and not self._review_held(a)
            and (
                (a.name in known and known[a.name].status is not Status.PROBATION)
                or self._admits is None
                or self._admits(a)
            )
        ]
        if (
            tuple(admitted) == self.algorithms
            and tuple(kind_admitted) == self.kind_algorithms
            and tuple(daily_admitted) == self.daily_algorithms
        ):
            return
        running: list[Algorithm | KindAlgorithm] = [*admitted, *kind_admitted]
        for a in running:
            standing = known.get(a.name)
            if standing is None:
                self.store.add_status(a.name, self.clock.now(), None, Status.PROBATION, REGISTERED)
                self._tell_admitted(a)
            # A new one, or one its pre-validation enrolled while the engine ran (#681: made active
            # by its walk-forward replay, or on a probation judged forward): its code is its first
            # revision.
            if self._revisions is not None and a.name not in self._revision_of:
                try:
                    self._revise(
                        a, None, self.clock.now(), Status.PROBATION if standing is None else standing.status
                    )
                except Exception:  # recorded at the next start, as one never recorded
                    log.exception("%s: its code could not be recorded", a.name)
        for d in daily_admitted:
            if d.name not in known:
                self.store.add_status(d.name, self.clock.now(), None, Status.PROBATION, REGISTERED)
                self._tell_admitted(d)
        self.algorithms = tuple(admitted)
        self.kind_algorithms = tuple(kind_admitted)
        self.daily_algorithms = tuple(daily_admitted)

    def _tell_admitted(self, algorithm: Algorithm | KindAlgorithm | DailyAlgorithm) -> None:
        if self._admits is not None:  # let in by the gate: told once, for Loki (#456)
            log.info(
                "%s: admitted on probation through the pre-validation gate (code %s)",
                algorithm.name,
                _code(algorithm),
            )

    def _kind_runs(self, algorithm: DailyAlgorithm) -> bool:
        kind = self.daily_kind_named(algorithm.kind)
        return kind is not None and kind.runs

    def _review_held(self, algorithm: DailyAlgorithm) -> bool:
        kind = self.daily_kind_named(algorithm.kind)
        return kind is not None and kind.review_hold is not None

    def waiting(self) -> list[str]:
        """The registered algorithms not run yet: their pre-validation has not passed, nor
        refused the code and target they run now (what the scheduled pre-validations run, and the
        alert names). Not one of a daily kind not run yet (#310, ``DailyKind.awaiting``): it is not
        pre-validated before its kind runs."""
        return [a.name for a in self._waiting()]

    def _waiting(self) -> list[Algorithm | KindAlgorithm | DailyAlgorithm]:
        return [a for a in self._unvalidated() if self._refusal(a) is None]

    def _waits_on(self, algorithm: Algorithm | KindAlgorithm | DailyAlgorithm) -> str:
        """What the pre-validation it waits for would judge (``prevalidation.judging``), as one key:
        the wait is told once for each (#508). Never in the way of the review: ``unknown`` when its
        code cannot be read."""
        from krx_quant.prevalidation import judging  # the pre-validation replays through this module

        try:
            return json.dumps(judging(algorithm, self.target_version, self.kinds), sort_keys=True)
        except (OSError, ValueError):
            return "unknown"

    def refused(self) -> dict[str, Refusal]:
        """The registered algorithms not run because their latest pre-validation refused the code
        and target they run now (``prevalidation.refusals``), each with its refusal: not waiting,
        as no pre-validation is due until either changes (#310)."""
        return {a.name: r for a in self._unvalidated() if (r := self._refusal(a)) is not None}

    def _unvalidated(self) -> list[Algorithm | KindAlgorithm | DailyAlgorithm]:
        """The registered algorithms not run for want of a pre-validation passed: waiting or
        refused."""
        return [
            *(a for a in self.registered if a not in self.algorithms),
            *(a for a in self.kind_registered if a not in self.kind_algorithms),
            *(
                a
                for a in self.daily_registered
                if a not in self.daily_algorithms and self._kind_runs(a) and not self._passed_and_held(a)
            ),
        ]

    def _refusal(self, algorithm: Algorithm | KindAlgorithm | DailyAlgorithm) -> Refusal | None:
        return None if self._refused is None else self._refused(algorithm)

    def held(self) -> list[str]:
        """The daily algorithms whose pre-validation passed but whose kind's review days are not
        fixed yet (#310, ``DailyKind.review_hold``): held out of probation, not waiting for a
        pre-validation (the scheduler will not run it again on the same code)."""
        return [
            a.name
            for a in self.daily_registered
            if a not in self.daily_algorithms and self._kind_runs(a) and self._passed_and_held(a)
        ]

    def _passed_and_held(self, algorithm: DailyAlgorithm) -> bool:
        return self._review_held(algorithm) and (self._admits is None or self._admits(algorithm))

    def to_simulate(self) -> tuple[list[str], list[str]]:
        """The algorithms on a probation that waits for its simulation (#298,
        ``PredictionStore.to_simulate``): the bar-close kinds', then the daily kinds' that run
        (a daily one on probation waits for its pre-validation otherwise, #264)."""
        bar_close = [a.name for a in self.registered] + [a.name for a in self.kind_registered]
        daily = [a.name for a in self.daily_algorithms]
        return self.store.to_simulate(bar_close, bar_close=True), self.store.to_simulate(daily)

    def simulated(self, kind: PredictionKind) -> dict[str, Simulated]:
        """The algorithms of ``kind`` with a simulated probation (#298), as the reviews and the
        stages count its days (``PredictionStore.simulated``)."""
        return self.store.simulated(kind.name, kind.answer_version)

    def daily_kind_named(self, name: str) -> DailyKind | None:
        """A daily kind the engine predicts (#264)."""
        return next((k for k in self.daily_kinds if k.name == name), None)

    def _running_daily(self) -> list[tuple[DailyAlgorithm, Model]]:
        """The daily algorithms that predict now, each with its model: those admitted (``_admit``)
        and not retired, then the others of a kind that runs, in shadow (#696)."""
        self._admit()
        standings = self.store.statuses()
        retired = _with_status(standings, Status.RETIRED)
        running = [a for a in self.daily_algorithms if a.name not in retired]
        shadow = [a for a in self.daily_registered if a not in running and self._kind_runs(a)]
        self._mark(standings, running, shadow)
        return [(a, self.models[a.name]) for a in [*running, *shadow]]

    def _in_shadow(self, retired: Collection[str]) -> tuple[list[Algorithm], list[KindAlgorithm]]:
        """The bar-close algorithms run only in shadow (#696), in registration order: registered, and
        not run by their pre-validation (waiting, refused) or by their status (retired)."""
        return (
            [a for a in self.registered if a not in self.algorithms or a.name in retired],
            [a for a in self.kind_registered if a not in self.kind_algorithms or a.name in retired],
        )

    def _mark(
        self,
        standings: Mapping[str, Standing],
        running: Iterable[Algorithm | KindAlgorithm | DailyAlgorithm],
        shadow: Iterable[Algorithm | KindAlgorithm | DailyAlgorithm],
    ) -> None:
        """Keep what the rows of ``running`` and ``shadow`` carry (#696): each one run its status (a
        new one starts on probation), each one in shadow ``SHADOW``."""
        for a in running:
            standing = standings.get(a.name)
            self._lifecycle[a.name] = (Status.PROBATION if standing is None else standing.status).value
        for a in shadow:
            self._lifecycle[a.name] = SHADOW

    def _tell_daily(self, stored: DailyStored) -> None:
        """The daily runner's predictions and answers, told as the other kinds' are (#264)."""
        if stored.ids:
            self._tell(KindPredictionsStored(stored.kind, stored.at, stored.ids))
        if stored.keys:
            self._tell(KindAnswered(stored.kind, stored.keys))

    def kind_named(self, name: str) -> PredictionKind | None:
        """A kind the engine predicts, the direction (#179) and the daily kinds (#264) included."""
        if name == DIRECTION:
            return DIRECTION_KIND
        return next((k for k in self.kinds if k.name == name), None) or self.daily_kind_named(name)

    def kind_of_algorithm(self, name: str) -> PredictionKind | None:
        """The kind a registered algorithm predicts; None for a name not registered."""
        if any(a.name == name for a in self.registered):
            return DIRECTION_KIND
        found = next((a for a in self.kind_registered if a.name == name), None)
        if found is not None:
            return self.kind_named(found.kind)
        daily = next((a for a in self.daily_registered if a.name == name), None)
        return None if daily is None else self.daily_kind_named(daily.kind)

    def on_event(self, feed: str, row: dict[str, Any]) -> None:
        self._market.on_event(feed, row)
        for window in self._special.event(feed, row, self.symbols):
            self._mark_special(window, row["received_at"])
        if self._daily_events is not None:  # the daily features' pre-market trades (#264)
            self._daily_events(feed, row)

    def _mark_special(self, window: Window, learned_at: datetime) -> None:
        """A special window just learned: the predictions it touches are special, scored or not,
        and it is kept (#236)."""
        log.info("special situation: %s", window)
        self.store.mark_special(
            window.symbol, window.start, window.end, HORIZON, {k.name: k.horizon for k in self.kinds}
        )
        self.store.keep_special(window, learned_at)

    def next_due(self) -> datetime | None:
        """When ``run_due`` next has work: a bar close to predict, an outcome to score, or a review."""
        candidates = [
            next_bar_close(self.calendar, self._last_tick),
            self._next_review(self._reviewed_through),
        ]
        if self._pending:
            candidates.append(self._pending[0][0])
        if self._kind_pending:
            candidates.append(self._kind_pending[0][0])
        if self._daily is not None:  # a daily kind's prediction or answers (#264)
            candidates.append(self._daily.next_due())
        due = [c for c in candidates if c is not None]
        return min(due) if due else None

    async def run_due(self) -> None:
        """Score, predict and review everything due by now, in time order."""
        now = self.clock.now()
        reviewed = False
        while (due := self.next_due()) is not None and due <= now:
            self._watch = LoopWatch() if self._watch_loop else None
            predicted = False
            try:
                await self._score_until(due)
                if due == next_bar_close(self.calendar, self._last_tick):
                    self._last_tick = self.predicted_at = due
                    predicted = True
                    await self._predict(due)
                if due == self._next_review(self._reviewed_through):
                    self._reviewed_through = due
                    await self._review(due)
                    self._review_done = due
                    reviewed = True
                if self._daily is not None and due == self._daily.next_due():
                    await self._daily.run(due)
                if predicted and self.garbage is not None:
                    with self._step(GARBAGE):
                        self.garbage.settle()
            finally:
                self._tell_held(due, predicted)
            # Once this run's writes are in, the log copied into the database in a thread (#637).
            self.store.checkpoint_soon()
        if self.garbage is not None:  # out of the session, its memory handed back (#621, #623)
            ahead = self.next_due()
            if reviewed or ahead is None or ahead - self.clock.now() > IDLE:
                rested = self.garbage.rest(self._let_go, self._market.held, reviewed=reviewed)
                if rested is not None:  # for the night's report, which cannot read Loki
                    self.store.add_run(REST_RUN, self.clock.now(), None, json.dumps(rested))

    @property
    def reviewed_through(self) -> datetime:
        """The time of the latest daily review over, by this engine or before its start (#687): a
        review still running is not (``_reviewed_through`` moves when it starts)."""
        return self._review_done

    def _let_go(self) -> Mapping[str, int]:
        """Drop from the Market View what the next session's bar closes do not read (#623,
        ``MarketView.let_go``), and say how many of each went: nothing while that session opens
        within ``IDLE`` (the pre-market's events are few), or none is in sight."""
        ahead = next_bar_close(self.calendar, self._last_tick)
        session = None if ahead is None else self.calendar.session(ahead.astimezone(KST).date())
        if session is None or session.open - self.clock.now() <= IDLE:
            return {}
        return self._market.let_go(session.open, {k.horizon for k in self.kinds})

    def _step(self, name: str) -> contextlib.AbstractContextManager[None]:
        """Time a synchronous step of the bar close on the event loop, when it is watched (#538)."""
        return contextlib.nullcontext() if self._watch is None else self._watch.step(name)

    def _tell_held(self, due: datetime, predicted: bool) -> None:
        """End the bar close's watch; a WARNING when it held the event loop too long (#538)."""
        watch, self._watch = self._watch, None
        if watch is None:
            return
        watch.stop()
        held = watch.held()
        if held is not None:
            what = f"{'bar close' if predicted else 'run'} {due.astimezone(KST):%Y-%m-%d %H:%M}"
            log.warning("%s", held_text(what, *held))
            # For the night's report of the bar closes' times (#589), which cannot read Loki.
            self.store.add_run(LOOP_HELD, due, None, held_detail(*held))

    def bars(self, symbol: str) -> list[Bar]:
        return self._market.bars(symbol, self.clock.now())

    def last_bar(self, symbol: str) -> Bar | None:
        """The symbol's latest bar today, the one being made if its minute goes on (#110)."""
        return self._market.last_bar(symbol, self.clock.now())

    def mid(self, symbol: str) -> float | None:
        return self._market.mid(symbol, self.clock.now())

    def mid_at(self, symbol: str, at: datetime) -> float | None:
        """The reference price just before ``at`` in continuous trading (the quotes of the last
        minutes are kept: the closing flow's minute and its 15:20, #305)."""
        return self._market.mid(symbol, at)

    def latest_lead(self, series: str) -> tuple[float, datetime] | None:
        """A leading series' latest trade today (e.g. the USD futures): price and time."""
        return self._market.latest_lead(series, self.clock.now())

    def switch_models(self, models: Mapping[str, Model]) -> bool:
        """Run ``models`` from the next bar close (a swap, a fit, a rollback); whether any changed.
        On the touch target (#170) an algorithm running a model of it does not go back to one
        of another target (the defaults included): that model would forecast other outcomes,
        and its days would count toward a review that does not look at them."""
        changed = False
        for name, model in models.items():
            if name in self.models and self.models[name] != model:
                if self._on_target(self.models[name], name) and not self._on_target(model, name):
                    log.warning(
                        "%s keeps %s: %s was not trained on target %d",
                        name,
                        self.models[name].version,
                        model.version,
                        self._target_of(name),
                    )
                    continue
                log.info("%s now runs %s", name, model.version)
                self.models[name] = model
                self._record_models({name: model})
                changed = True
            elif name == CLIMATE and name in self.baseline_names and self.climate != model:
                log.info("%s now runs %s", name, model.version)
                self.climate = model
                changed = True
            elif name in self.kind_tables and self.kind_tables[name] != model:  # a kind's (#180)
                log.info("%s now runs %s", name, model.version)
                self.kind_tables[name] = model
                changed = True
        return changed

    def members(self) -> list[str]:
        """The consensus members now: the active algorithms running a model of the engine's
        target (#170; one that is not sits out), in registration order."""
        active = _with_status(self.store.statuses(), Status.ACTIVE)
        return [a.name for a in self.algorithms if a.name in active and self._on_target(self.models[a.name])]

    def _on_target(self, model: Model, name: str | None = None) -> bool:
        """Whether ``model`` was trained on the target the engine scores (before #170 any is);
        of a kind's algorithm ``name``, on the kind's answer version (#179), likewise."""
        target = self._target_of(name)
        return target < 2 or (model.target_version or 1) == target

    def _target_of(self, name: str | None) -> int:
        """The target the models of algorithm ``name`` learn: a kind's answer version, else the
        engine's target version."""
        kind = None if name is None else self.kind_of_algorithm(name)
        return self.target_version if kind is None or kind is DIRECTION_KIND else kind.answer_version

    def cpu_use(self) -> CpuUse:
        """The CPU budget now: the algorithms that run, as timed so far, and today's skips."""
        # Every bar-close algorithm: those in shadow run too (#696).
        expected = {a.name: self._cost[a.name] for a in self._bar_close() if a.name in self._cost}
        midnight = _start_of(self.clock.now().astimezone(KST).date())
        return CpuUse(self._budget, expected, self.store.count_runs("cpu_budget", midnight))

    def market_halt(self) -> Window | None:
        """The latest market halt known now, begun by now (#625), as last learned: on while now is
        before its end. The collector sends one notice for it in place of the feeds' outages, and one
        when it is over."""
        now = self.clock.now()
        halts = [w for w in self._special.known(before=now) if w.kind is Kind.MARKET_HALT and w.start <= now]
        return max(halts, key=lambda w: w.start, default=None)

    def special_today(self) -> list[Window]:
        """The special situations known now that touch today, in time order."""
        now = self.clock.now()
        today = now.astimezone(KST).date()
        windows = [w for w in self._special.known(before=now) if w.end.astimezone(KST).date() >= today]
        return sorted(windows, key=lambda w: (w.start, w.kind.value, w.symbol or ""))

    def _next_review(self, after: datetime) -> datetime | None:
        """The first daily review strictly after ``after``."""
        day = after.astimezone(KST).date()
        for _ in range(LOOKAHEAD_DAYS):
            session = self.calendar.session(day)
            if session is not None and session.close + REVIEW_AFTER_CLOSE > after:
                return session.close + REVIEW_AFTER_CLOSE
            day += timedelta(days=1)
        return None

    async def _review(self, at: datetime) -> None:
        """Change statuses by the lifecycle rules, alert retirement candidates, and tell the
        predictions the budgets cost that day. An algorithm whose changed code has not passed its
        pre-validation stays on probation whatever its review (#454)."""
        self._judge_revisions()
        standings = self.store.statuses()
        alerted = {
            name
            for name, s in standings.items()
            if s.status in (Status.PROBATION, Status.BENCH)
            and self.store.ran_since("retire_candidate", name, s.since)
        }
        result = await asyncio.to_thread(
            review,
            [a.name for a in self.algorithms],
            standings,
            {name: self._counted_from(name, s) for name, s in standings.items()},
            self.store.prediction_days(at.astimezone(KST).date(), _REVIEW_DAYS),
            self.store.reviewed,  # the fields it reads (#487)
            min_sample=self.min_sample,
            fee_rate=self.fee_rate,
            alerted=alerted,
            rules=self.review_rules,
            baselines=self.baseline_names,
            simulated=self.simulated(DIRECTION_KIND),
        )
        # Each kind's algorithms, within the kind, against its baselines (#179).
        for kind in self.kinds:
            of_kind = [a.name for a in self.kind_algorithms if a.kind == kind.name]
            if not of_kind:
                continue
            judged = await asyncio.to_thread(
                review,
                of_kind,
                standings,
                {a: self._kind_counted_from(kind, a, standings[a]) for a in of_kind if a in standings},
                self.store.kind_prediction_days(kind.name, at.astimezone(KST).date(), _REVIEW_DAYS),
                partial(self._kind_rows, kind),
                min_sample=self.min_sample,
                fee_rate=self.fee_rate,
                alerted=alerted,
                rules=self.review_rules,
                baselines=frozenset(b.name for b in kind.baselines),
                scoring=kind_scoring(kind),
                simulated=self.simulated(kind),
            )
            result = Review(result.changes + judged.changes, result.candidates + judged.candidates)
        # Each daily kind's algorithms, by its own days (#264): those with an answer.
        for daily in self.daily_kinds:
            of_daily = [a.name for a in self.daily_algorithms if a.kind == daily.name]
            if not of_daily:
                continue
            judged = await asyncio.to_thread(
                review,
                of_daily,
                standings,
                {a: self._kind_counted_from(daily, a, standings[a]) for a in of_daily if a in standings},
                self._daily_days(daily, at.astimezone(KST).date()),
                partial(self._kind_rows, daily),
                min_sample=daily.min_sample,
                fee_rate=self.fee_rate,
                alerted=alerted,
                rules=daily.review_rules,
                baselines=daily.baseline_names(),
                scoring=kind_scoring(daily, self.calendar),
                recent=daily.recent_days,
                simulated=self.simulated(daily),
            )
            result = Review(result.changes + judged.changes, result.candidates + judged.candidates)
        # A probation whose new code has not passed its pre-validation (#454) is not made active; what
        # each of its review days decided is kept, and the pass makes it active if the latest passed
        # (``_judge_revisions``): a later review day that does not pass undoes an earlier one.
        held = {r.algorithm: r for r in self._revision_of.values() if r.held}
        promoted = {
            c.algorithm: c for c in result.changes if c.algorithm in held and c.status is Status.ACTIVE
        }
        changes = [c for c in result.changes if c.algorithm not in promoted]
        day = at.astimezone(KST).date()
        for name, revision in held.items():
            standing = standings.get(name)
            if standing is None or standing.status is not Status.PROBATION:
                continue
            counted = self._review_day(name, standing, day)
            if counted is None:
                continue
            c = promoted.get(name)
            kept = {
                "revision": revision.id,
                # The revision's, kept by a reviewed refactor's row too: what the pass matches.
                "boundary": revision.boundary.astimezone(KST).isoformat(),
                "stint": standing.since.astimezone(
                    KST
                ).isoformat(),  # its probation's: a restart begins another
                "days": counted,
                "passed": c is not None,
                "reason": c and c.reason,
            }
            self.store.add_run(REVISION_REVIEW_RUN, at, name, json.dumps(kept))
            log.info(
                "%s: day %d of its probation %s; it stays on probation until the pre-validation of its"
                " new code passes",
                name,
                counted,
                f"passed ({c.reason})" if c is not None else "did not pass",
            )
        for c in changes:
            # Only from the status reviewed: a retire command may have landed meanwhile.
            if self.store.change_status(
                c.algorithm, at, c.previous, c.status, c.reason, simulated=c.simulated
            ):
                log.info("%s: %s -> %s (%s)", c.algorithm, c.previous.value, c.status.value, c.reason)
            else:
                log.info("%s: %s not applied, its status changed meanwhile", c.algorithm, c.status.value)
        for candidate in result.candidates:
            name = candidate.algorithm
            stuck = (
                f"수습 {candidate.days}거래일 동안 정식이 되지 못했습니다"
                if candidate.status is Status.PROBATION
                else f"벤치에서 {candidate.days}거래일 동안 회복하지 못했습니다"
            )
            text = f"{name} 은퇴 후보: {stuck}. 은퇴시키려면 서버에서 krx-quant retire {name}"
            if await self._deliver(text):  # else asked again at the next review
                self.store.add_run("retire_candidate", at, name, text)
        # A wait for a pre-validation, once for the code, target and inputs it waits on (#508): kept
        # in ``runs``, so a restart does not tell it again; a new code, target or inputs does.
        untold = {
            a.name: (a, key)
            for a in self._waiting()
            if self.store.last_detail(WAITING_RUN, a.name) != (key := self._waits_on(a))
        }
        if untold:
            if await self._deliver(_waiting_text([a for a, _ in untold.values()], self.daily_registered)):
                for name, (_, key) in untold.items():  # else told at the next review
                    self.store.add_run(WAITING_RUN, at, name, key)
        # A refusal of the code and target it runs now, once (#310): kept in ``runs``, so a
        # restart does not tell it again; a new code or target makes it wait again.
        for name, refusal in self.refused().items():
            if self.store.last_detail(REFUSED_RUN, name) == refusal.judged:
                continue
            text = (
                f"사전 검증 거부: {name} ({refusal.why}). 수습에 들지 않고 그림자로만 예측하며, 같은 "
                f"코드와 정답으로는 다시 검증하지 않습니다. 리포트는 서버의 state/prevalidations/{name}.json"
            )
            if await self._deliver(text):  # else told at the next review
                self.store.add_run(REFUSED_RUN, at, name, refusal.judged)
        # A revision its pre-validation refused, once (#454): kept in ``runs`` by its id.
        for revision in self._revision_of.values():
            name = revision.algorithm
            if revision.verdict != REVISION_REFUSED:
                continue
            if self.store.last_detail(REVISION_REFUSED_RUN, name) == str(revision.id):
                continue
            text = (
                f"코드 변경 사전 검증 거부: {name} ({revision.detail}). 코드가 바뀐 뒤"
                f"({revision.boundary.astimezone(KST):%Y-%m-%d %H:%M})의 녹화일로 다시 판정해 거부되어 "
                f"은퇴했습니다. 리포트는 서버의 state/prevalidations/{name}.json"
            )
            if await self._deliver(text):  # else told at the next review
                self.store.add_run(REVISION_REFUSED_RUN, at, name, str(revision.id))
        unsaid = [name for name in self.held() if name not in self._told_held]
        if unsaid:
            kinds = sorted(
                {
                    k.name
                    for a in self.daily_registered
                    if a.name in unsaid
                    if (k := self.daily_kind_named(a.kind))
                }
            )
            text = (
                f"판정일 보류: {', '.join(unsaid)}. 사전 검증은 통과했지만 {', '.join(kinds)}의 "
                "판정일을 실측한 분산 배율로 코드에 고정하기 전이라 수습에 오르지 않습니다(#310). "
                "리포트의 walk_forward.overlap으로 판정일을 고정하는 PR이 필요합니다."
            )
            if await self._deliver(text):
                self._told_held.update(unsaid)
        await self._report_budgets(at)
        await self._refit_weights(at)
        self._count_evidence()  # the verdicts' members' evidence, with the day's answers (#676)
        # Last: a review that failed part way is run again after a restart.
        self.store.add_run("review", at, None, f"{len(changes)} status changes")

    async def _report_budgets(self, at: datetime) -> None:
        """Tell the reviewed day's predictions the time and CPU budgets cost, if any: an
        algorithm too slow for its limit, or the algorithms together too much for the
        server. Once a day, a review run again after a restart included (``budget_report``)."""
        day = at.astimezone(KST).date()
        midnight, next_day = _start_of(day), _start_of(day + timedelta(days=1))
        if self.store.count_runs("budget_report", midnight, until=next_day):
            return
        timed_out = self.store.timeouts_by_algorithm(midnight, next_day)
        if self.kinds:  # the kinds' algorithms too (#179), after the direction's
            timed_out = {**timed_out, **self.store.kind_timeouts_by_algorithm(midnight, next_day)}
        skipped = self.store.runs_by_algorithm("cpu_budget", midnight, next_day)
        if not timed_out and not skipped:
            return

        def counts(by_algorithm: Mapping[str, int]) -> str:
            return ", ".join(f"{name} {n}회" for name, n in by_algorithm.items())

        lines = [f"{day} 예산 때문에 예측이 빠진 알고리즘이 있습니다."]
        if timed_out:
            lines.append(
                f"시간 초과(봉 마감 한 번에 {self._budget.per_algorithm:g}초 넘김): {counts(timed_out)}."
            )
        if skipped:
            lines.append(f"CPU 예산({self._budget.cpu:g}초)으로 건너뜀: {counts(skipped)}.")
        lines.append("알고리즘을 가볍게 하거나 서버를 늘리세요. 걸리는 시간은 /v1/status의 cpu에 있습니다.")
        text = " ".join(lines)
        if await self._deliver(text):
            self.store.add_run("budget_report", at, None, text)

    async def _refit_weights(self, at: datetime) -> None:
        """The consensus weights of the members active after the review."""
        members = self.members()
        days = self.store.prediction_days(at.astimezone(KST).date(), max(SPANS))
        # The members' rows only, none without a member (#487): the weights read no other's.
        rows = await asyncio.to_thread(self.store.reviewed, days, False, members)
        self.weights = await asyncio.to_thread(fit_weights, rows, members, days, at)
        self.store.save_weights(self.weights)
        log.info("consensus weights %s for %s", self.weights.version, ", ".join(members) or "no member")

    def _first_counted(self, since: datetime) -> date:
        """The first day a status that began at ``since`` counts for: its day, unless it
        began after that day's review.
        """
        day = since.astimezone(KST).date()
        session = self.calendar.session(day)
        if session is not None and since >= session.close + REVIEW_AFTER_CLOSE:
            return day + timedelta(days=1)
        return day

    def _first_day(self, standing: Standing) -> date:
        """The first day ``standing`` counts for (``_first_counted``); on a probation for new code
        (#454, ``CODE_CHANGED``), its first whole trading day: the revision's predictions only."""
        if standing.status is Status.PROBATION and standing.reason == CODE_CHANGED:
            return first_whole_day(self.calendar, standing.since)
        return self._first_counted(standing.since)

    def _counted_from(self, name: str, standing: Standing) -> date | None:
        """The first day ``name``'s status counts for (``_first_day``); on probation or as
        a clone, no earlier than the first trading day it predicted with a model trained on the
        touch target since (#170): the day the morning swap (or a fit) installed one. None
        while it has not: a training that failed or a holiday counts nothing."""
        first = self._first_day(standing)
        if standing.status not in (Status.PROBATION, Status.CLONE):
            return first
        started = self.store.first_on_target(name, standing.since, TARGET_VERSION)
        return None if started is None else max(first, started.astimezone(KST).date())

    def _kind_counted_from(self, kind: PredictionKind, name: str, standing: Standing) -> date | None:
        """``_counted_from`` of a kind's algorithm (#179): on probation or as a clone, from the
        first trading day it predicted with a model trained on the kind's answers (its answer
        version); its defaults' days are not counted, as a new direction algorithm's are not."""
        first = self._first_day(standing)
        if standing.status not in (Status.PROBATION, Status.CLONE):
            return first
        started = self.store.first_kind_on_target(name, standing.since, kind.answer_version)
        return None if started is None else max(first, started.astimezone(KST).date())

    def _kind_rows(self, kind: PredictionKind, chosen: Sequence[date], special: bool) -> list[dict[str, Any]]:
        """``kind``'s scored predictions made on ``chosen`` days, as the lifecycle reads them
        (``kind_reviewed``, #487)."""
        return self.store.kind_reviewed(kind.name, kind.answer_version, chosen, special)

    def _daily_days(self, kind: DailyKind, until: date) -> list[date]:
        """The days a daily kind's lifecycle counts up to ``until`` (#264): those with an answer,
        latest first, as many as its longest rule reads back."""
        rules = kind.review_rules
        longest = max(rules.max_probation, rules.max_bench, kind.recent_days)
        return self.store.kind_answered_days(kind.name, kind.answer_version, until, longest + 20)

    def next_review_day(self) -> date | None:
        """The day of the next daily review: the days before it have been reviewed, their bar closes
        all scored (``stages``; the days each algorithm's series counts, #561)."""
        upcoming = self._next_review(self._reviewed_through)
        return None if upcoming is None else upcoming.astimezone(KST).date()

    async def stages(self) -> dict[str, Stage]:
        """Where each algorithm on probation or on the bench stands toward its next review
        (#165, ``lifecycle.stages``), as of the latest review: the days counted as the review
        counts them, the weakest lift by trading day it judges by, and the day the next review
        falls on if the engine predicts every trading day. Read off the event loop, and again
        only once a review has run or a status changed: the days reviewed do not change.
        """
        standings = self.store.statuses()
        next_day = self.next_review_day()
        key = (
            sorted((name, s.status.value, s.since) for name, s in standings.items()),
            next_day,
            self.store.simulation_mark(),  # a simulation kept since (#298)
        )
        if self._stages is not None and self._stages[0] == key:
            return self._stages[1]
        today = self.clock.now().astimezone(KST).date()
        days = self.store.prediction_days(today, _REVIEW_DAYS)
        reviewed = [d for d in days if next_day is None or d < next_day]
        found = await asyncio.to_thread(
            stages_now,
            [a.name for a in self.algorithms],
            standings,
            {name: self._counted_from(name, s) for name, s in standings.items()},
            reviewed,
            self.store.reviewed,  # the fields it reads (#487)
            min_sample=self.min_sample,
            fee_rate=self.fee_rate,
            rules=self.review_rules,
            baselines=self.baseline_names,  # the target's set, as the review judges by (#169)
            simulated=self.simulated(DIRECTION_KIND),
        )
        for kind in self.kinds:  # each kind's algorithms, as its review judges them (#179)
            names = [a.name for a in self.kind_algorithms if a.kind == kind.name]
            if not names:
                continue
            kind_days = self.store.kind_prediction_days(kind.name, today, _REVIEW_DAYS)
            found |= await asyncio.to_thread(
                stages_now,
                names,
                standings,
                {
                    name: self._kind_counted_from(kind, name, standings[name])
                    for name in names
                    if name in standings
                },
                [d for d in kind_days if next_day is None or d < next_day],
                partial(self._kind_rows, kind),
                min_sample=self.min_sample,
                fee_rate=self.fee_rate,
                rules=self.review_rules,
                baselines=frozenset(b.name for b in kind.baselines),
                scoring=kind_scoring(kind),
                simulated=self.simulated(kind),
            )
        for daily in self.daily_kinds:  # each daily kind's algorithms, by its days (#264)
            names = [a.name for a in self.daily_algorithms if a.kind == daily.name]
            if not names:
                continue
            found |= await asyncio.to_thread(
                stages_now,
                names,
                standings,
                {
                    name: self._kind_counted_from(daily, name, standings[name])
                    for name in names
                    if name in standings
                },
                self._daily_days(daily, today),
                partial(self._kind_rows, daily),
                min_sample=daily.min_sample,
                fee_rate=self.fee_rate,
                rules=daily.review_rules,
                baselines=daily.baseline_names(),
                scoring=kind_scoring(daily, self.calendar),
                recent=daily.recent_days,
                simulated=self.simulated(daily),
            )
        dated = {name: replace(s, next_review_on=self._review_on(next_day, s)) for name, s in found.items()}
        self._stages = (key, dated)
        return dated

    def _review_on(self, next_day: date | None, stage: Stage) -> date | None:
        """The trading day ``stage``'s next review falls on, counted from ``next_day``, the next
        daily review's, which counts one day more than the latest; None without either, or
        while nothing is counted yet (waiting for a model of the touch target, #170, and no day
        simulated, #298)."""
        if next_day is None or stage.next_review is None:
            return None
        if stage.counted_from is None and not stage.simulated:
            return None
        day = next_day
        for _ in range(stage.next_review - stage.days - 1):
            day += timedelta(days=1)
            while not self.calendar.is_trading_day(day):
                day += timedelta(days=1)
        return day

    async def _deliver(self, text: str) -> bool:
        """Send an alert; whether it went (or there is nowhere to send it)."""
        if self.notify is None:
            log.warning("alert: %s", text)
            return True
        return await self.notify(text)

    async def _predict(self, bar_close: datetime) -> None:
        with self._step(INPUTS):
            self._admit()
            day_start = datetime.combine(bar_close.date(), time(), KST)
            at_close: dict[str, _AtClose] = {}
            frozen = self._market.at_close(bar_close)
            standings = self.store.statuses()  # read each time: retiring is a command
            retired, active = _with_status(standings, Status.RETIRED), _with_status(standings, Status.ACTIVE)
            running = [a for a in self.algorithms if a.name not in retired]
            # A kind whose window from here would end after the closing auction (#263) predicts nothing.
            session = self.calendar.session(bar_close.astimezone(KST).date())
            late = {
                k.name
                for k in self.kinds
                if session is not None and bar_close + k.horizon > session.closing_auction
            }
            kind_running = [a for a in self.kind_algorithms if a.name not in retired and a.kind not in late]
            # The others in shadow (#696): after the verdicts, the first the CPU budget skips.
            shadow, kind_shadow = self._in_shadow(retired)
            kind_shadow = [a for a in kind_shadow if a.kind not in late]
            self._mark(standings, [*running, *kind_running], [*shadow, *kind_shadow])
            members = [a.name for a in running if a.name in active]
            known = self._special.known(before=bar_close)
            regime = self._regime(bar_close)
            # A market halt (#625) stops every symbol, as a VI stops its own.
            halted = in_halt(known, bar_close) is not None
            stopped = (
                list(self.symbols) if halted else [s for s in self.symbols if in_vi(known, s, bar_close)]
            )
            not_made = HALT_STATUS if halted else "vi"
            if stopped:
                self._record_not_predicted(bar_close, stopped, not_made, [*running, *shadow])
            made_by: dict[tuple[str, str, str], dict[str, Forecast | None]] = {}
            for symbol in self.symbols:
                if symbol in stopped:
                    continue
                snapshot = self._market.snapshot(frozen, symbol, self.fee_rate, regime)
                mid = snapshot.price
                touch = TouchTerms.at(self._market.quote_at(symbol, bar_close), snapshot.tick)
                at_close[symbol] = _AtClose(mid, self._features.compute(snapshot, self._feature_names), touch)
                inputs = Inputs(
                    symbol=symbol,
                    bar_close=bar_close,
                    mid=mid,
                    mid_before=self._market.mid(symbol, bar_close - HORIZON),
                    flat_ticks=snapshot.flat_ticks,
                    tick=snapshot.tick,
                    # The windows over by now: their bar closes four minutes back or more.
                    day_classes=self.store.day_touches(symbol, day_start, bar_close - HORIZON),
                    previous=self._market.touch(symbol, bar_close - HORIZON).direction,
                    climate=climate_direction(self.climate, symbol, bar_close),
                )
                for b in self.baselines:
                    forecast = b.predict(inputs)
                    status = "ok" if forecast is not None else NO_PREVIOUS
                    made_by.setdefault((b.name, self._version(b), status), {})[symbol] = forecast
        with self._step(RECORDS):
            self._record(
                bar_close,
                at_close,
                [(name, version, made, status) for (name, version, status), made in made_by.items()],
            )
        with self._step(SMT_STEP):
            self._judge_smt(frozen)
        # The kinds' baselines before the algorithms start (#538): they read the answers made at
        # this bar close's scoring and take milliseconds, while beside the algorithms' threads each
        # of their store calls waited for the GIL, which those threads hold as they compute.
        with self._step(KIND_BASELINES):
            self._predict_kinds(bar_close, at_close, stopped, [*kind_running, *kind_shadow], late, not_made)
        if not at_close:
            return
        # The kinds' algorithms after the direction's: the budget skips them first (#179), and those
        # in shadow before them (#696).
        chosen = {
            a.name
            for a in self._within_cpu_budget(bar_close, [*running, *kind_running, *shadow, *kind_shadow])
        }
        runs = {
            a.name: asyncio.ensure_future(self._run(a, bar_close, at_close))
            for a in running
            if a.name in chosen
        }
        kind_runs = [
            asyncio.ensure_future(self._run_kind(a, bar_close, at_close))
            for a in kind_running
            if a.name in chosen
        ]
        mine = [runs[m] for m in members if m in runs]
        try:
            # The fast consensus: the members done within a moment of starting.
            if mine:
                await asyncio.wait(mine, timeout=self.consensus_timing.fast_seconds)
            with self._step(CONSENSUS):
                self._record_consensus(FAST, bar_close, at_close, members, _finished(mine))
            # The final one: every member done (each has its time budget), not the others.
            if mine:
                await asyncio.wait(mine)
            with self._step(CONSENSUS):
                final = self._record_consensus(FINAL, bar_close, at_close, members, _finished(mine))
            await asyncio.gather(*runs.values(), *kind_runs, return_exceptions=True)
        except BaseException:
            for task in [*runs.values(), *kind_runs]:  # shutting down: none may write after the store closes
                task.cancel()
            raise
        # Every algorithm done: each symbol's verdict from what they said (#676).
        direction = [r.result() for r in runs.values() if not r.cancelled() and r.exception() is None]
        kinds = [r.result() for r in kind_runs if not r.cancelled() and r.exception() is None]
        await self._judge(bar_close, at_close, standings, direction, kinds, final, late)
        # Those in shadow once the verdicts are made (#696): nothing the consensus, the signals and the
        # verdicts wait for waits on them, nor shares the CPU with them.
        shadows = [
            *(asyncio.ensure_future(self._run(a, bar_close, at_close)) for a in shadow if a.name in chosen),
            *(
                asyncio.ensure_future(self._run_kind(a, bar_close, at_close))
                for a in kind_shadow
                if a.name in chosen
            ),
        ]
        try:
            await asyncio.gather(*shadows, return_exceptions=True)
        except BaseException:
            for task in shadows:  # shutting down: none may write after the store closes
                task.cancel()
            raise

    async def _judge(
        self,
        bar_close: datetime,
        at_close: Mapping[str, _AtClose],
        standings: Mapping[str, Standing],
        direction: Sequence[tuple[str, Mapping[str, Forecast | None]]],
        kinds: Sequence[tuple[str, str, Mapping[str, KindForecast | None]]],
        final: Mapping[str, Forecast],
        late: Collection[str],
    ) -> None:
        """Each predicted symbol's verdict at ``bar_close`` (#676, ``verdict.py``), from what every
        algorithm said there: the barrier kinds' (not ``late``) for its horizons, the direction's and
        the other kinds' beside it; stored and logged with its time. A failure is logged, and the
        engine goes on."""
        started = timer.perf_counter()
        try:
            held: Mapping[str, Held] = {}
            if self._held is not None:
                try:
                    held = await asyncio.to_thread(self._held, self.clock.now())
                except Exception:
                    log.warning("the trader's positions not read at %s", bar_close, exc_info=True)
            with self._step(VERDICT):
                now = self.clock.now()
                status = {name: s.status.value for name, s in standings.items()}
                evidence = self._evidence
                rows = []
                for symbol, c in at_close.items():
                    horizons: list[Horizon] = []
                    side = [
                        Side(name, DIRECTION, status.get(name), _direction_output(f))
                        for name, made in direction
                        if (f := made.get(symbol)) is not None
                    ]
                    if (pooled := final.get(symbol)) is not None:
                        side.append(Side(FINAL, DIRECTION, None, _direction_output(pooled)))
                    for kind in self.kinds:
                        made_here = [
                            (n, kf)
                            for n, k, m in kinds
                            if k == kind.name and (kf := m.get(symbol)) is not None
                        ]
                        if kind.classes != BARRIER_CLASSES:
                            side += [
                                Side(n, kind.name, status.get(n), kind.output(kf)) for n, kf in made_here
                            ]
                            continue
                        if kind.name in late:
                            continue
                        width = self._market.kind_width(kind, symbol, bar_close)
                        horizons.append(
                            Horizon(
                                kind.name,
                                round(kind.horizon.total_seconds() / 60),
                                None if width is None else width.ticks,
                                [
                                    Member(
                                        n,
                                        kind.name,
                                        status.get(n),
                                        evidence.get((kind.name, n), 0),
                                        kf.probabilities,
                                    )
                                    for n, kf in made_here
                                ],
                            )
                        )
                    quote = self._latest_quote(symbol, bar_close)
                    bid, ask = quote if quote is not None else (None, None)
                    found = verdict(
                        symbol,
                        bar_close,
                        horizons,
                        Market(c.mid, bid, ask, self.fee_rate),
                        held.get(symbol),
                        side,
                    )
                    rows.append((symbol, bar_close, VERDICT_VERSION, found.action, found.as_json()))
                self.store.add_verdicts(rows, now)
            told = ", ".join(f"{r[0]} {r[3]}" for r in rows)
            log.info(
                "verdicts at %s in %.1f ms: %s",
                f"{bar_close.astimezone(KST):%H:%M}",
                (timer.perf_counter() - started) * 1000,
                told,
            )
        except Exception:
            log.exception("verdicts not made at %s", bar_close)

    def _latest_quote(self, symbol: str, bar_close: datetime) -> tuple[float, float] | None:
        """``symbol``'s best quote (bid, ask) last received, crossed ones aside (#676: what an order now
        trades at): the last of its touch window's quotes so far (a few seconds of them, the members
        just done), else the one standing at the bar close; None without either."""
        window = self._market.followed(symbol, bar_close)
        if window is not None and window.quotes:
            last = window.quotes[-1]
            return last.bid, last.ask
        return self._market.quote_at(symbol, bar_close)

    def _count_evidence(self) -> None:
        """Count each barrier algorithm's evidence (#676): its predictions answered, by (kind,
        algorithm). At start and after each daily review: over 40 days it takes a few hundred
        milliseconds, kept off the session's bar closes."""
        counted: dict[tuple[str, str], int] = {}
        for kind in self.kinds:
            if kind.classes != BARRIER_CLASSES:
                continue
            names = [a.name for a in self.kind_registered if a.kind == kind.name]
            found = self.store.kind_evidence(kind.name, kind.answer_version, names)
            counted |= {(kind.name, name): n for name, n in found.items()}
        self._evidence = counted

    def _judge_smt(self, frozen: BarsAtClose) -> None:
        """Keep the SMT episodes that begin at this bar close (#520). A failure is logged, and the
        bar close's predictions go on."""
        try:
            found = self._smt.at_close(frozen.close, {**frozen.leaders, **frozen.symbols})
            if found:
                self.store.add_smt_events(found, self.clock.now())
        except Exception:
            log.warning("SMT not judged at %s", frozen.close, exc_info=True)

    def _predict_kinds(
        self,
        bar_close: datetime,
        at_close: Mapping[str, _AtClose],
        stopped: Sequence[str],
        running: Sequence[KindAlgorithm],
        late: Collection[str] = (),
        not_made: str = "vi",
    ) -> None:
        """Each kind's baselines' predictions of every symbol, at once, and every predictor's
        empty prediction of the symbols ``stopped`` (#179), status ``not_made`` (in a VI, or every
        symbol in a market halt, #625); none of a kind in ``late`` (its window would end after the
        closing auction, #263). A kind that fails is logged: the other kinds and the direction go
        on."""
        day_start = datetime.combine(bar_close.date(), time(), KST)
        for kind in self.kinds:
            if kind.name in late:
                continue
            try:
                baselines = self._kind_baselines(kind)
                made_by: dict[tuple[str, str, str], dict[str, KindForecast | None]] = {}
                for symbol, c in at_close.items():
                    # The last window over by now, its own horizon back (#263): answered at this
                    # bar close's scoring (#538), else (not followed) answered so here.
                    over = bar_close - kind.horizon
                    previous = self._answered.get((kind.name, symbol, over))
                    if previous is None:
                        previous = self._market.kind_answer(kind, symbol, over)
                    inputs = KindInputs(
                        symbol=symbol,
                        bar_close=bar_close,
                        mid=c.mid,
                        tick=tick_size(c.mid) if c.mid is not None else None,
                        flat_ticks=self._close_k(symbol, bar_close, at_close),
                        previous=previous if previous.label is not None else None,
                        # The windows over by now: their bar closes the kind's horizon back or more.
                        day_classes=self.store.day_kind_classes(
                            kind.name, kind.answer_version, symbol, day_start, bar_close - kind.horizon
                        ),
                    )
                    for b in baselines:
                        forecast = b.predict(inputs)
                        if forecast is not None and (why := kind.invalid(forecast)) is not None:
                            raise ValueError(f"{b.name} at {bar_close}: {why}")
                        status = "ok" if forecast is not None else KIND_NO_PREVIOUS
                        made_by.setdefault((b.name, b.version, status), {})[symbol] = forecast
                made = [(name, version, f, status) for (name, version, status), f in made_by.items()]
                if stopped:
                    nothing: dict[str, KindForecast | None] = dict.fromkeys(stopped)
                    made += [(b.name, b.version, nothing, not_made) for b in baselines]
                    made += [
                        (a.name, self.models[a.name].version, nothing, not_made)
                        for a in running
                        if a.kind == kind.name
                    ]
                self._record_kind(kind, bar_close, at_close, made)
            except Exception:
                log.exception("%s: its baselines failed at %s", kind.name, bar_close)

    def _kind_baselines(self, kind: PredictionKind) -> list[KindBaseline]:
        """``kind``'s baselines as they predict now: one of a table with the table the engine
        runs (#180), its predictions carrying the table's version."""
        return [
            b.using(self.kind_tables[b.name]) if isinstance(b, TableBaseline) else b for b in kind.baselines
        ]

    def _close_mid(self, symbol: str, bar_close: datetime, at_close: Mapping[str, _AtClose]) -> float | None:
        """``symbol``'s mid-point at the bar close, as the kinds' predictors are given it
        (``KindInputs.mid``) and their answers are measured from (the window's anchor); the
        reference price there for a symbol not predicted (a VI). None without one."""
        c = at_close.get(symbol)
        return c.mid if c is not None else self._market.mid(symbol, bar_close)

    def _close_k(self, symbol: str, bar_close: datetime, at_close: Mapping[str, _AtClose]) -> int | None:
        """The flat band k at ``symbol``'s mid-point at the bar close (``_close_mid``), as the
        kinds' predictors are given it (``KindInputs.flat_ticks``) and their answer counts it.
        None without a mid-point."""
        mid = self._close_mid(symbol, bar_close, at_close)
        return flat_ticks(mid, self.fee_rate) if mid is not None else None

    def _record_kind(
        self,
        kind: PredictionKind,
        bar_close: datetime,
        at_close: Mapping[str, _AtClose],
        made: Sequence[tuple[str, str, Mapping[str, KindForecast | None], str]],
    ) -> None:
        """Store a kind's (name, model version, forecasts by symbol, status) entries as
        displayed now (#179), each with its bar close's k (#200): the one the forecast was made
        with, kept as the price moves on before the answer; its mid-point and tick size there
        (#210), the reference its answer is measured from; and the kind's barrier width there
        (#183), the one its answer is made with."""
        displayed_at = self.clock.now()
        symbols = {symbol for _, _, forecasts, _ in made for symbol in forecasts}
        mids = {symbol: self._close_mid(symbol, bar_close, at_close) for symbol in symbols}
        ks = {symbol: None if mid is None else flat_ticks(mid, self.fee_rate) for symbol, mid in mids.items()}
        widths = {symbol: self._market.kind_width(kind, symbol, bar_close) for symbol in symbols}
        predictions = [
            NewKindPrediction(
                symbol=symbol,
                algorithm=name,
                model_version=version,
                bar_close=bar_close,
                displayed_at=displayed_at,
                output=None if forecast is None else kind.output(forecast),
                status=status,
                flat_ticks=ks[symbol],
                width_ticks=None if (width := widths[symbol]) is None else width.ticks,
                anchor=mids[symbol],
                tick=None if (mid := mids[symbol]) is None else tick_size(mid),
                lifecycle=self._lifecycle.get(name),
            )
            for name, version, forecasts, status in made
            for symbol, forecast in forecasts.items()
        ]
        if predictions:
            ids = self.store.add_kind_predictions(kind.name, predictions)
            self._tell(KindPredictionsStored(kind.name, bar_close, ids))

    def _regime(self, bar_close: datetime) -> str | None:
        """The day's macro regime, read once a day. A day without one, or with one this
        engine does not know, is recorded (``runs``) when an algorithm reads it."""
        day = bar_close.astimezone(KST).date()
        if self._regimes is None:
            return None
        if day not in self._regime_days:
            session = self.calendar.session(day)
            opened = session.open if session is not None else bar_close
            read = self._regimes(day, opened)
            regime = self._regime_days[day] = read if read in REGIME_CODES else None
            midnight = datetime.combine(day, time(), KST)
            if regime is None and "macro_regime" in self._feature_names:
                if not self.store.count_runs("no_regime", midnight):  # once, restarts too
                    detail = f"no macro regime for {day}" + (f" (unknown: {read})" if read else "")
                    if read:
                        log.warning("no macro regime for %s (unknown: %s)", day, read)
                    else:
                        log.warning("no macro regime for %s", day)
                    self.store.add_run("no_regime", bar_close, None, detail)
        return self._regime_days[day]

    def _record_consensus(
        self,
        name: str,
        bar_close: datetime,
        at_close: Mapping[str, _AtClose],
        members: Sequence[str],
        finished: Mapping[str, Mapping[str, Forecast | None]],
    ) -> dict[str, Forecast]:
        """The consensus of the members' forecasts finished so far; empty where there are none. Its
        forecasts by symbol."""
        pooled: dict[str, Forecast] = {}
        empty: dict[str, Forecast | None] = {}
        pooled_from: dict[str, dict[str, Forecast]] = {}
        why = "none_finished" if members else "no_members"
        for symbol in at_close:
            present = {m: f for m in members if (f := finished.get(m, {}).get(symbol)) is not None}
            if present:
                pooled[symbol] = pool(present, self.weights.of(symbol, bar_close, list(present)))
                pooled_from[symbol] = present
            else:
                empty[symbol] = None
        version = self.weights.version
        made: list[tuple[str, str, Mapping[str, Forecast | None], str]] = [
            (name, version, forecasts, status)
            for forecasts, status in ((pooled, "ok"), (empty, why))
            if forecasts
        ]
        # A final consensus not to be acted on says why, stored with it for every client (#88).
        blocked = self._blocked(bar_close, at_close, pooled) if name == FINAL else {}
        self._record(
            bar_close,
            at_close,
            made,
            blocked,
            {s: _votes(f) for s, f in pooled_from.items()},
            {s: tuple(f) for s, f in pooled_from.items()},
        )
        if name == FINAL and self.signals is not None:
            try:
                self._signal(self.signals, bar_close, pooled, pooled_from, blocked)
            except Exception:  # an alert never stops predicting
                log.exception("signal alert failed")
        return pooled

    def _blocked(
        self, bar_close: datetime, at_close: Mapping[str, _AtClose], pooled: Mapping[str, Forecast]
    ) -> dict[str, Blocked]:
        """Why each symbol's final consensus is not to be acted on, by what is known now: a VI,
        circuit breaker or market halt (#625) touching its four minutes, no reference price at its
        bar close, or its target time past."""
        now = self.clock.now()
        target_at = bar_close + HORIZON
        known = [w for w in self._special.known(before=now) if w.kind in _BLOCKING]
        blocked: dict[str, Blocked] = {}
        for symbol in pooled:
            if touching(known, symbol, bar_close, target_at):
                blocked[symbol] = Blocked.SPECIAL
            elif at_close[symbol].mid is None:
                blocked[symbol] = Blocked.NO_REFERENCE
            elif now >= target_at:
                blocked[symbol] = Blocked.EXPIRED
        return blocked

    def _signal(
        self,
        signals: Signals,
        bar_close: datetime,
        pooled: Mapping[str, Forecast],
        pooled_from: Mapping[str, Mapping[str, Forecast]],
        blocked: Mapping[str, Blocked],
    ) -> None:
        """Judge a final consensus for signals, block what should not be acted on, and keep
        each signal judged (``runs``)."""
        now = self.clock.now()
        if not self._signals_resumed:
            self._signals_resumed = True  # once, even if reading fails
            midnight = datetime.combine(now.astimezone(KST).date(), time(), KST)
            signals.resume(r.detail for r in self.store.runs(SENT, midnight))
        target_at = bar_close + HORIZON

        def keep(judged: Judged) -> None:
            self.store.add_run(judged.kind, now, None, judged.detail(signals.rule))
            if judged.blocked is not None:
                log.info("signal blocked (%s): %s", judged.blocked.value, judged.signal.text())

        signals.final(bar_close, target_at, pooled, pooled_from, blocked, keep)

    def _record_not_predicted(
        self, bar_close: datetime, symbols: Sequence[str], status: str, running: Sequence[Algorithm]
    ) -> None:
        """Every predictor's empty prediction for ``symbols``, recorded with why."""
        at_close = {s: _AtClose(self._market.mid(s, bar_close), {}) for s in symbols}
        nothing = dict.fromkeys(symbols)
        made = [(b.name, self._version(b), nothing, status) for b in self.baselines]
        made += [(a.name, self.models[a.name].version, nothing, status) for a in running]
        made += [(name, self.weights.version, nothing, status) for name in NAMES]
        self._record(bar_close, at_close, made)

    def _version(self, baseline: Baseline) -> str:
        """The version a baseline's predictions carry: the climate table's for the climate."""
        return self.climate.version if baseline.name == CLIMATE else baseline.version

    def _within_cpu_budget(
        self, bar_close: datetime, running: Sequence[Algorithm | KindAlgorithm]
    ) -> list[Algorithm | KindAlgorithm]:
        """The algorithms to run: the last registered drop out while the expected work is over budget."""
        chosen = list(running)
        while chosen and sum(self._cost.get(a.name, 0.0) for a in chosen) > self._budget.cpu:
            dropped = chosen.pop()
            expected = self._cost.get(dropped.name, 0.0)
            detail = f"expected {expected:.3f}s; budget {self._budget.cpu}s per bar close"
            log.warning("%s skipped at %s for the CPU budget (%s)", dropped.name, bar_close, detail)
            self.store.add_run("cpu_budget", bar_close, dropped.name, detail)
            # Never run, it would never be timed again: expect less, so it is tried soon.
            self._cost[dropped.name] = expected / 2
        return chosen

    async def _run(
        self, algorithm: Algorithm, bar_close: datetime, at_close: Mapping[str, _AtClose]
    ) -> tuple[str, Mapping[str, Forecast | None]]:
        """Run and record one algorithm; its name and forecasts (none unless it finished)."""
        name, model = algorithm.name, self.models[algorithm.name]
        vectors = {s: FeatureVector(c.features, algorithm.features, c.touch) for s, c in at_close.items()}
        made, declined, status = await self._timed(
            name, bar_close, at_close, partial(_predict_symbols, algorithm, model, vectors, bar_close)
        )
        with self._step(RECORDS):
            self._record(bar_close, at_close, _entries(name, model.version, made, declined, status))
        return name, made

    async def _run_kind(
        self, algorithm: KindAlgorithm, bar_close: datetime, at_close: Mapping[str, _AtClose]
    ) -> tuple[str, str, Mapping[str, KindForecast | None]]:
        """Run and record one algorithm of a kind besides the direction (#179), as ``_run``; its
        name, kind and forecasts."""
        kind = self.kind_named(algorithm.kind)
        assert kind is not None  # registered with its kind
        name, model = algorithm.name, self.models[algorithm.name]
        vectors = {s: FeatureVector(c.features, algorithm.features) for s, c in at_close.items()}
        made, declined, status = await self._timed(
            name, bar_close, at_close, partial(_predict_kind_symbols, kind, algorithm, model, vectors)
        )
        with self._step(RECORDS):
            self._record_kind(
                kind, bar_close, at_close, _entries(name, model.version, made, declined, status)
            )
        return name, kind.name, made

    async def _timed(
        self,
        name: str,
        bar_close: datetime,
        symbols: Iterable[str],
        predict: Callable[[], tuple[Mapping[str, _F], Mapping[str, Abstention]]],
    ) -> tuple[Mapping[str, _F | None], Mapping[str, Abstention], str]:
        """Run one algorithm's ``predict`` in a worker thread within its time budget, timing it:
        its forecasts by symbol (none unless it finished), the symbols it declined and why, and
        its status (``ok``, ``timed_out``, ``error``)."""
        made: Mapping[str, _F | None] = dict.fromkeys(symbols)  # nothing, unless it finishes
        status = "timed_out"
        declined: Mapping[str, Abstention] = {}
        if name not in self._busy:
            started = timer.perf_counter()
            work = asyncio.ensure_future(asyncio.to_thread(predict))
            done, _ = await asyncio.wait({work}, timeout=self._budget.per_algorithm)
            if not done:
                log.warning("%s took over %ss at %s; skipped", name, self._budget.per_algorithm, bar_close)
                self._busy.add(name)
                work.add_done_callback(partial(self._finished_late, name))
                self._cost[name] = max(self._cost.get(name, 0.0), self._budget.per_algorithm)
            elif (error := work.exception()) is not None:
                log.error("%s failed at %s: %r", name, bar_close, error, exc_info=error)
                status = "error"
            else:
                (made, declined), status = work.result(), "ok"
                elapsed = timer.perf_counter() - started
                previous = self._cost.get(name)
                self._cost[name] = (
                    elapsed if previous is None else (1 - _COST_WEIGHT) * previous + _COST_WEIGHT * elapsed
                )
        return made, declined, status

    def _finished_late(self, name: str, work: asyncio.Future[Any]) -> None:
        self._busy.discard(name)
        if not work.cancelled() and work.exception() is not None:
            log.error("%s failed after its budget: %r", name, work.exception())

    def _record(
        self,
        bar_close: datetime,
        at_close: Mapping[str, _AtClose],
        made: Sequence[tuple[str, str, Mapping[str, Forecast | None], str]],
        blocked: Mapping[str, Blocked] | None = None,
        votes: Mapping[str, tuple[int, int, int]] | None = None,
        pooled: Mapping[str, tuple[str, ...]] | None = None,
    ) -> None:
        """Store (name, model version, forecasts by symbol, status) entries as displayed now;
        a consensus with how its members leaned (``votes``) and who they were (``pooled``),
        by symbol."""
        displayed_at = self.clock.now()
        mid_display = {symbol: self._market.mid(symbol, displayed_at) for symbol in at_close}
        special = self._special.known(before=displayed_at)
        predictions = []
        for name, version, forecasts, status in made:
            for symbol, forecast in forecasts.items():
                mid = at_close[symbol].mid
                # k comes from the bar close's mid-point, or the display's if that is all there is.
                reference = mid if mid is not None else mid_display[symbol]
                predictions.append(
                    NewPrediction(
                        symbol=symbol,
                        algorithm=name,
                        model_version=version,
                        bar_close=bar_close,
                        displayed_at=displayed_at,
                        forecast=forecast,
                        target_at=bar_close + HORIZON,
                        mid_close=mid,
                        mid_display=mid_display[symbol],
                        flat_ticks=flat_ticks(reference, self.fee_rate) if reference is not None else None,
                        status=status,
                        special=touching(special, symbol, bar_close, bar_close + HORIZON),
                        blocked=why.value
                        if forecast is not None and blocked and (why := blocked.get(symbol)) is not None
                        else None,
                        votes=(votes or {}).get(symbol) if forecast is not None else None,
                        pooled=(pooled or {}).get(symbol) if forecast is not None else None,
                        lifecycle=self._lifecycle.get(name),
                    )
                )
        added = self.store.add_all(predictions)
        for pending in added.pending:
            heapq.heappush(self._pending, (pending.target_at, pending.prediction_id, pending))
        self._tell(PredictionsStored(bar_close, added.ids))

    async def _score_until(self, due: datetime) -> None:
        with self._step(SCORING):
            scores: list[tuple[int, Score]] = []
            # The touch label of every window over (#167), predicted or not (a VI), and of each
            # bar close scored (one from before a restart says so): recorded beside the outcomes,
            # not yet what they are scored on.
            windows = dict.fromkeys(self._market.ended_windows(due))
            while self._pending and self._pending[0][0] <= due:
                _, _, pending = heapq.heappop(self._pending)
                mid_target = self._market.mid(pending.symbol, pending.target_at)
                scores.append((pending.prediction_id, _score(pending, mid_target)))
                windows[(pending.symbol, pending.target_at - HORIZON)] = None
            now = self.clock.now()
            if windows:
                self.store.add_touches([self._market.touch(symbol, close) for symbol, close in windows], now)
            if scores:
                self.store.score_all(scores, now)
                self._tell(OutcomesScored([prediction_id for prediction_id, _ in scores]))
        self._answered = {}
        if self.kinds:
            await self._answer_kinds(due, list(windows), now)

    async def _answer_kinds(
        self, due: datetime, windows: Sequence[tuple[str, datetime]], now: datetime
    ) -> None:
        """Each kind's answer of every window over (#179), as the touch labels are, and of the
        kind's predictions made before a restart (``restarted``): stored once, and told. A
        window is special as a prediction made at its bar close would be: a VI, circuit
        breaker or sidecar known then, or learned since in any version, touches it (as
        training leaves such a bar close out); one learned later marks it then
        (``PredictionStore.mark_special``). A kind that fails is logged.

        The windows are copied on the event loop and answered in a worker thread (#538): a longer
        kind's window holds up to its horizon's hour of quotes, and the loop also reads KIS and
        streams the quotes. Whether one is special is read once they are answered, as they are
        stored: a situation learned meanwhile marks only the answers stored before it. The answers
        are kept for the kinds' baselines (``_answered``)."""
        with self._step(ANSWERS):
            # The windows over: the touch windows' for a four-minute kind, a longer kind's own (#263).
            # Each window length handed out once: two kinds of one horizon share its windows.
            longer = {
                h: self._market.ended_windows(due, h) for h in {k.horizon for k in self.kinds} - {WINDOW}
            }
            by_kind: dict[str, dict[tuple[str, datetime], None]] = {
                k.name: dict.fromkeys(windows if k.horizon == WINDOW else longer[k.horizon])
                for k in self.kinds
            }
            while self._kind_pending and self._kind_pending[0][0] <= due:
                _, kind_name, symbol, close = heapq.heappop(self._kind_pending)
                if kind_name in by_kind:
                    by_kind[kind_name][(symbol, close)] = None
            todo: list[tuple[PredictionKind, list[_ToAnswer]]] = []
            for kind in self.kinds:
                keys = list(by_kind[kind.name])
                if not keys:
                    continue
                try:
                    windows_of = [_ToAnswer(s, c, self._market.kind_window(kind, s, c)) for s, c in keys]
                except Exception:
                    log.exception("%s: its answers failed at %s", kind.name, due)
                    continue
                todo.append((kind, windows_of))
        if not todo:
            return
        made = await asyncio.to_thread(_answer_windows, todo)
        with self._step(ANSWERS):
            special: dict[tuple[str, datetime, timedelta], bool] = {}

            def touched(symbol: str, close: datetime, horizon: timedelta) -> bool:
                if (symbol, close, horizon) not in special:
                    end = close + horizon
                    special[(symbol, close, horizon)] = touching(
                        self._special.known(before=close), symbol, close, end
                    ) or touching(self._special.learned_since(close), symbol, close, end)
                return special[(symbol, close, horizon)]

            for kind, answered in made:
                if isinstance(answered, BaseException):
                    log.error("%s: its answers failed at %s", kind.name, due, exc_info=answered)
                    continue
                try:
                    answers = [(s, c, a, touched(s, c, kind.horizon)) for s, c, a in answered]
                    written = self.store.add_kind_answers(kind.name, kind.answer_version, answers, now)
                except Exception:
                    log.exception("%s: its answers failed at %s", kind.name, due)
                    continue
                for symbol, close, answer in answered:
                    self._answered[(kind.name, symbol, close)] = answer
                if written:
                    self._tell(KindAnswered(kind.name, written))

    def _tell(self, change: Change) -> None:
        for listener in self.listeners:
            try:
                listener(change)
            except Exception:  # a listener never stops predicting
                log.exception("engine listener failed")


def _direction_output(f: Forecast) -> dict[str, float]:
    """A direction forecast as a verdict shows it beside its horizons (#676)."""
    return {"p_up": f.p_up, "p_flat": f.p_flat, "p_down": f.p_down, "expected_ticks": f.expected_ticks}


def _votes(forecasts: Mapping[str, Forecast]) -> tuple[int, int, int]:
    """The members by the class each forecast most likely, as scored (ties: flat, then up):
    up, flat, down (#106)."""
    leaned = [most_likely(f.p_up, f.p_flat, f.p_down) for f in forecasts.values()]
    return leaned.count(Direction.UP), leaned.count(Direction.FLAT), leaned.count(Direction.DOWN)


def _finished(
    runs: Sequence[asyncio.Future[tuple[str, Mapping[str, Forecast | None]]]],
) -> dict[str, Mapping[str, Forecast | None]]:
    """The forecasts of the runs done (a run that failed counts as none)."""
    return dict(r.result() for r in runs if r.done() and not r.cancelled() and r.exception() is None)


def _with_status(standings: Mapping[str, Standing], status: Status) -> set[str]:
    return {name for name, s in standings.items() if s.status is status}


# An algorithm's forecasts by symbol, and why for each symbol it did not predict.
_Predicted = tuple[dict[str, Forecast], dict[str, Abstention]]


def _waiting_text(
    waiting: Sequence[Algorithm | KindAlgorithm | DailyAlgorithm], daily: Sequence[DailyAlgorithm]
) -> str:
    """The alert of the algorithms ``waiting`` for their pre-validation (#508): who pre-validates them
    and when, as the night's scheduled jobs do (``scheduled/prevalidations.py``: the bar-close ones one
    a night, two on a quiet night (#546), once ``MIN_DAYS`` trading days are recorded, a daily one on
    the first night its data is in), and where the result goes. Nothing is asked of anyone."""
    from krx_quant.prevalidation import MIN_DAYS  # the pre-validation replays through this module
    from krx_quant.scheduled.prevalidations import PREVALIDATION_ISSUE

    when = []
    if any(a not in daily for a in waiting):
        when.append(
            f"봉 마감 알고리즘은 녹화가 {MIN_DAYS}거래일 이상이면 밤 학습 뒤 예약 작업이 한 밤에 하나씩"
            "(시간이 남는 밤에는 둘) 차례로 검증합니다"
        )
    if any(a in daily for a in waiting):
        when.append("하루 한 번 예측 알고리즘은 그 입력과 정답이 다 들어온 첫 밤에 예약 작업이 검증합니다")
    return (
        f"사전 검증 대기: {', '.join(a.name for a in waiting)}. "
        "검증을 통과해야 수습을 시작합니다(그때까지는 그림자로 예측만 남깁니다). "
        + "".join(f"{w}. " for w in when)
        + f"결과는 #{PREVALIDATION_ISSUE}에 댓글로 올라옵니다. 손으로 돌릴 것은 없습니다."
    )


def _code(algorithm: Algorithm | KindAlgorithm | DailyAlgorithm) -> str:
    """The digest of the code its pre-validation judged (``prevalidation.code_digest``), for a log
    line: never in the way of the admission it tells."""
    from krx_quant.prevalidation import code_digest  # the pre-validation replays through this module

    try:
        return code_digest(algorithm)
    except (OSError, ValueError):
        return "unknown"


def _predict_symbols(
    algorithm: Algorithm, model: Model, vectors: Mapping[str, FeatureVector], bar_close: datetime
) -> _Predicted:
    """Runs in a worker thread: one algorithm on every symbol at ``bar_close``, each forecast
    checked. A context-aware algorithm (#429) is given each symbol and the bar close."""
    forecasts: dict[str, Forecast] = {}
    declined: dict[str, Abstention] = {}
    for symbol, features in vectors.items():
        try:
            with forecasting(features):  # of the target the features carry (#169)
                f = forecast_of(algorithm, features, model, PredictionContext(symbol, bar_close))
        except NotPredicted as e:
            declined[symbol] = e.why
            continue
        probabilities = (f.p_up, f.p_flat, f.p_down)
        distribution = all(0 <= p <= 1 for p in probabilities) and math.isclose(
            sum(probabilities), 1, abs_tol=1e-6
        )
        if not distribution:
            raise ValueError(f"{algorithm.name} gave probabilities {probabilities} for {symbol}")
        if not math.isfinite(f.expected_ticks):
            raise ValueError(f"{algorithm.name} expected {f.expected_ticks} ticks for {symbol}")
        forecasts[symbol] = f
    return forecasts, declined


def _predict_kind_symbols(
    kind: PredictionKind, algorithm: KindAlgorithm, model: Model, vectors: Mapping[str, FeatureVector]
) -> tuple[dict[str, KindForecast], dict[str, Abstention]]:
    """Runs in a worker thread: one algorithm of a kind on every symbol, each forecast checked
    against the kind's contract (``Kind.invalid``)."""
    forecasts: dict[str, KindForecast] = {}
    declined: dict[str, Abstention] = {}
    for symbol, features in vectors.items():
        try:
            f = algorithm.predict(features, model)
        except NotPredicted as e:
            declined[symbol] = e.why
            continue
        why = kind.invalid(f)
        if why is not None:
            raise ValueError(f"{algorithm.name} for {symbol}: {why}")
        forecasts[symbol] = f
    return forecasts, declined


def _entries(
    name: str,
    version: str,
    made: Mapping[str, _F | None],
    declined: Mapping[str, Abstention],
    status: str,
) -> list[tuple[str, str, Mapping[str, _F | None], str]]:
    """An algorithm's run as (name, model version, forecasts by symbol, status) entries to
    record: what it made (or not, with its status), then the symbols it declined, by reason."""
    entries = [(name, version, made, status)] if made else []
    for why in dict.fromkeys(declined.values()):  # each reason once, in the order met
        nothing: dict[str, _F | None] = {s: None for s, w in declined.items() if w is why}
        entries.append((name, version, nothing, why.value))
    return entries


@dataclass(frozen=True)
class _ToAnswer:
    """A kind's window to answer (#538), copied on the event loop: None for one not followed (the
    engine restarted in it)."""

    symbol: str
    close: datetime
    window: KindWindow | None


def _answer_windows(
    todo: Sequence[tuple[PredictionKind, Sequence[_ToAnswer]]],
) -> list[tuple[PredictionKind, list[tuple[str, datetime, Answer]] | Exception]]:
    """Each kind's (symbol, bar close, answer) of its windows (``KindWindow.answer``; ``restarted``
    for one not followed); a kind whose answer function fails gets its exception instead. Pure:
    run in a worker thread (#538)."""
    made: list[tuple[PredictionKind, list[tuple[str, datetime, Answer]] | Exception]] = []
    for kind, windows in todo:
        try:
            made.append(
                (
                    kind,
                    [
                        (
                            w.symbol,
                            w.close,
                            Answer(None, RESTARTED) if w.window is None else w.window.answer(),
                        )
                        for w in windows
                    ],
                )
            )
        except Exception as e:
            made.append((kind, e))
    return made


def _score(p: Pending, mid_target: float | None) -> Score:
    """Classes on both bases; a missing mid-point leaves its basis unscored, with the reason."""
    missing = [
        name
        for name, value in (("close", p.mid_close), ("display", p.mid_display), ("target", mid_target))
        if value is None
    ]
    reference = p.mid_close if p.mid_close is not None else p.mid_display

    def direction(start: float | None) -> Direction | None:
        if start is None or mid_target is None or reference is None or p.flat_ticks is None:
            return None
        return classify(mid_target - start, p.flat_ticks, tick_size(reference))

    return Score(
        mid_target=mid_target,
        class_a=direction(p.mid_close),
        class_b=direction(p.mid_display),
        unscorable=f"missing mid: {', '.join(missing)}" if missing else None,
    )


async def run_live(engine: PredictionEngine, clock: SessionClock) -> None:
    """Predict and score on the system clock until cancelled; a failure is logged, not fatal."""
    while True:
        try:
            due = engine.next_due()
            if due is None:
                await asyncio.sleep(3600)
                continue
            await clock.sleep_until(due)
            await engine.run_due()
        except Exception:
            log.exception("prediction run failed")
            await asyncio.sleep(1)  # do not spin on a failure that repeats
