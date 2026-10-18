"""Training data: each trading day turned into samples with the live feature code.

Each bar close of a day's prediction window becomes a sample per symbol: the features
there (built by the Feature Store, as live) and what happened four minutes later,
classified with the same flat band as live scoring. A day is read one of two ways:

- Recorded: every symbol's integrated trades were recorded. The day's events go through
  the engine's own Market View. The bars, leaders and reference price (the mid-point) are
  what the engine saw. The outcome is the mid-point four minutes later, as live scoring
  decides it (basis A). Every lead group is there, indices and US daytime trades too. The
  bar closes the engine does not predict (a VI) and those live statistics leave out (a
  special situation touches them) are left out.
- Backfilled only: the backfill has bars, not quotes. The reference price is the last
  close, and the outcome the close four minutes later. A close jumps between the bid and
  the ask and back (bid-ask bounce); live scoring on mid-points never sees that. The lead
  groups are the other symbols, the futures' continuous series and the ETFs. Indices and
  US daytime trades have no backfill, so their moves are unknown.

A recorded day is read the first way even when it has backfilled bars too.

With the touch target (``KRX_TARGET_VERSION=3``, 2 before #187; #169) the outcome is its label instead:
on a recorded day the engine's own touch window (``touch.py``) over the replayed quotes,
exactly as the engine labels it; on a backfilled day the one-minute bar rule
(``touch.bar_touch``). The move (``change_ticks``) stays the four-minute one. The features
carry the touch target's terms (``FeatureVector.touch``): half the spread at the bar close
on a recorded day, a one-tick spread's on a backfilled one.

A day read for the kinds besides the direction (#179, ``kinds.py``) also holds each kind's
answers of its bar closes (``TrainingDay.answers``), what the kind's algorithms learn
(``TrainingDay.of_kind``): on a recorded day the kind's answer function on the windows the
Market View follows, as the engine answers them; on a backfilled day the kind's bar rule
(``Kind.bar_answer``; none without one). The bar closes left out are the direction's (a VI,
a special situation); a day counts them by why (``TrainingDay.left_out``, #184), for the
night's report.

The previous trading day's one-minute ranges (``range_prev_day``, #181) and bars (#183, the
barrier's width), and the 20 trading days before's slot returns (#265), come from those days'
backfilled bars (``backfill.PreviousDay``), on a recorded day as on a backfilled one, as the
engine reads them. A kind with barriers (#183) has their
width at each bar close from the bars up to it: on a recorded day the Market View's, as the
engine fixes it; on a backfilled day the same rule on the backfilled bars, given to the bar
rule (``Kind.width``, ``kinds.BarAnswer``).
"""

from __future__ import annotations

import bisect
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time
from pathlib import Path
from typing import Literal

from krx_quant.algorithms import ALGORITHMS, KIND_ALGORITHMS, Algorithm, KindAlgorithm, Model, model_path
from krx_quant.algorithms.contract import KindSample, Sample
from krx_quant.archive import NIGHTLY_ARCHIVE, partition_files
from krx_quant.backfill import FUTURES_BARS_TR, MINUTE_BARS_TR, PreviousDay, bars_key, read_bars
from krx_quant.config import Settings
from krx_quant.contracts import KOSPI200, USD, days_to_expiry
from krx_quant.engine import Event
from krx_quant.features import (
    BARRIER_FEATURES,
    LEAD_FEATURES,
    PREVIOUS_DAY_RANGE,
    SLOT_RETURN_DAYS,
    SLOT_RETURN_FEATURES,
    FeatureStore,
    FeatureVector,
    Leaders,
    PreviousDays,
    Snapshot,
    TouchTerms,
    Value,
)
from krx_quant.feeds import TRADE
from krx_quant.feeds.base import KST
from krx_quant.kind_registry import KINDS, kind_named
from krx_quant.kinds import Answer, Kind
from krx_quant.macro import day_regime
from krx_quant.market_calendar import MarketCalendar, calendar_store
from krx_quant.market_view import HORIZON, MarketView, prediction_window
from krx_quant.model_shelf import Shelf
from krx_quant.pricing import Direction, classify
from krx_quant.reference import BAR, Bar
from krx_quant.replay import recorded_events
from krx_quant.special import SpecialSituations, in_vi, touching
from krx_quant.touch import bar_touch

BarDays = Mapping[tuple[str, date], Sequence[Bar]]  # (symbol, day) -> bars, oldest first
# The reference price a day's samples were made with: "mid" from recorded events, "close"
# from backfilled bars.
Reference = Literal["mid", "close"]
# The kinds' answers of a day's bar closes (#179), by (kind, symbol, bar close).
Answers = dict[tuple[str, str, datetime], Answer | None]
# Why a recorded day's bar close has no sample (#184): the symbol in a VI there (the engine does
# not predict it), or its four minutes touched by a special situation (live statistics leave it
# out).
IN_VI = "vi"
SPECIAL = "special"


@dataclass(frozen=True)
class TrainingDay:
    """A day's samples, with every feature asked for, and the reference price they used; with
    the kinds asked for (#179), their answers of its bar closes; and how many of its bar closes
    (and symbols) have no sample, by why (#184: ``IN_VI``, ``SPECIAL``; none on a backfilled
    day, which knows of no VI)."""

    reference: Reference
    samples: list[Sample]
    answers: Mapping[tuple[str, str, datetime], Answer | None] = field(default_factory=dict)
    left_out: Mapping[str, int] = field(default_factory=dict)

    def of(self, features: Sequence[str]) -> list[Sample]:
        """The samples with only ``features``, as one algorithm declared them, each naming the
        day's reference price (a fitted model counts its days by it, #165)."""
        return [
            replace(s, features=FeatureVector(s.features, features), reference=self.reference)
            for s in self.samples
        ]

    def of_kind(self, kind: Kind, features: Sequence[str]) -> list[KindSample]:
        """A kind's samples (#179): the same bar closes with only ``features``, each labelled
        with the kind's answer (none where the day has none)."""
        return [
            KindSample(
                s.symbol,
                s.bar_close,
                FeatureVector(s.features, features),
                self.answers.get((kind.name, s.symbol, s.bar_close)),
                kind.answer_version,
                self.reference,
            )
            for s in self.samples
        ]


def training_day(
    data_dir: Path,
    day: date,
    calendar: MarketCalendar,
    symbols: Sequence[str],
    names: Sequence[str],
    fee_rate: float,
    leaders: Leaders,
    *,
    target_version: int = 1,
    kinds: Sequence[Kind] = (),
) -> TrainingDay | None:
    """``day``'s samples: from its recorded events if every symbol's trades were recorded,
    else from its backfilled bars if every symbol has them; None otherwise. A day never
    trains on some symbols only. Their outcomes are of ``target_version`` (#169); with the
    ``kinds`` besides the direction (#179), each one's answers of its bar closes too.
    """
    session = calendar.session(day)
    regime = day_regime(data_dir, day, session.open) if session is not None else None
    # The previous trading days' ranges, bars and slot returns (#181, #183, #265), read only when
    # something uses them.
    previous = PreviousDay(data_dir, calendar) if needs_previous_day(names, kinds) else None
    if is_recorded(data_dir, day, symbols):
        events = recorded_events(data_dir, day, symbols, leaders)
        made, answers, left_out = recorded_day(
            events,
            calendar,
            day,
            symbols,
            names,
            fee_rate,
            leaders,
            regime=regime,
            target_version=target_version,
            kinds=kinds,
            previous=previous,
        )
        return TrainingDay("mid", made, answers, left_out)
    bar_days = bar_days_on(data_dir, symbols, [day])
    if len(bar_days) < len(symbols):
        return None
    leader_days = load_leader_days(data_dir, leaders, [day]) if LEAD_FEATURES & set(names) else {}
    made = samples(
        bar_days,
        calendar,
        names,
        fee_rate,
        leader_days=leader_days,
        leaders=leaders,
        regimes={day: regime},
        target_version=target_version,
        previous=previous,
    )
    return TrainingDay("close", made, bar_answers(bar_days, calendar, fee_rate, kinds, previous=previous))


def needs_previous_day(names: Iterable[str], kinds: Sequence[Kind] = ()) -> bool:
    """Whether a day's samples of the features ``names`` and the ``kinds``' answers read the
    previous trading days' backfilled bars: the previous day's ranges (``range_prev_day``,
    #181), the barrier's width (its features, and a kind with barriers, #183), the slot
    returns (#265, the 20 days before)."""
    named = set(names)
    return (
        PREVIOUS_DAY_RANGE in named
        or bool((BARRIER_FEATURES | SLOT_RETURN_FEATURES) & named)
        or any(k.width for k in kinds)
    )


def previous_days_needed(names: Iterable[str], kinds: Sequence[Kind] = ()) -> int:
    """How many trading days before a day's samples read the symbols' backfilled bars of: the
    longest of the slot returns (``SLOT_RETURN_DAYS``, #265), else the previous day's (#181,
    #183), else none."""
    if SLOT_RETURN_FEATURES & set(names):
        return max(SLOT_RETURN_DAYS)
    return 1 if needs_previous_day(names, kinds) else 0


def is_recorded(data_dir: Path, day: date, symbols: Sequence[str]) -> bool:
    """Whether every symbol's integrated trades were recorded on ``day``."""
    root = data_dir / "events" / f"date={day.isoformat()}" / f"feed={TRADE.tr_id}"
    return all(partition_files(root / f"symbol={s}") for s in symbols)


def days_with_data(data_dir: Path, symbols: Sequence[str]) -> list[date]:
    """The days with backfilled bars or recorded trades of every one of ``symbols``, oldest
    first.
    """
    found: set[date] = set()
    for folder in [*(data_dir / "bars").glob("date=*"), *(data_dir / "events").glob("date=*")]:
        try:
            day = date.fromisoformat(folder.name.removeprefix("date="))
        except ValueError:
            continue  # not a day's folder
        backfilled = all((data_dir / bars_key(day, MINUTE_BARS_TR, s)).exists() for s in symbols)
        if backfilled or is_recorded(data_dir, day, symbols):
            found.add(day)
    return sorted(found)


def finished_days(data_dir: Path, symbols: Sequence[str], now: datetime) -> list[date]:
    """The days with data, oldest first. Today counts only once its recording is archived
    (21:00): before that it is still being recorded.
    """
    local = now.astimezone(KST)
    archived = local.time() >= NIGHTLY_ARCHIVE
    return [d for d in days_with_data(data_dir, symbols) if d < local.date() or archived]


def samples(
    bar_days: BarDays,
    calendar: MarketCalendar,
    names: Sequence[str],
    fee_rate: float,
    store: FeatureStore | None = None,
    *,
    leader_days: BarDays | None = None,
    leaders: Leaders | None = None,
    regimes: Mapping[date, str | None] | None = None,
    target_version: int = 1,
    previous: PreviousDays | None = None,
) -> list[Sample]:
    """A sample per bar close in each day's prediction window, in time order, from backfilled
    bars: the reference price is the last close, the outcome the close four minutes later;
    with the touch target (``target_version`` 3, #169, #187), the one-minute bar rule's label.

    ``leader_days`` holds the leading series' bars by (series, day); the other symbols of
    ``bar_days`` lead too. ``regimes`` holds each day's macro regime (none: unknown).
    ``previous`` gives each symbol's previous trading day's ranges and bars (#181, #183; none:
    unknown).
    """
    store = store or FeatureStore()
    leaders = leaders or Leaders()
    stocks = _by_day(bar_days)
    series = _by_day(leader_days or {})
    leading = bool(LEAD_FEATURES & set(names))  # leaders are sliced only when asked for
    touch = TouchTerms() if target_version >= 2 else None  # no quotes: a one-tick spread
    out: list[Sample] = []
    for (symbol, day), bars in sorted(bar_days.items(), key=lambda item: (item[0][1], item[0][0])):
        session = calendar.session(day)
        if session is None:
            continue
        ends = [b.start + BAR for b in bars]
        starts = [b.start for b in bars]
        closes = {b.start: b.close for b in bars}
        expiry = days_to_expiry(day, calendar)
        ranges = previous(symbol, day) if previous is not None else None
        before = previous.bars(symbol, day) if previous is not None else ()
        slot_returns = previous.slot_returns(symbol, day) if previous is not None else ()
        t, last = prediction_window(session)
        while t <= last:
            done = bars[: bisect.bisect_right(ends, t)]
            price = done[-1].close if done else None
            groups = None
            if leading:
                groups = leaders.groups(symbol, stocks[day].until(t), series.get(day, _DayBars({})).until(t))
            snapshot = Snapshot.at(
                symbol,
                t,
                done,
                price,
                fee_rate,
                session_open=session.open,
                leaders=groups,
                regime=(regimes or {}).get(day),
                expiry_days=expiry,
                previous_ranges=ranges,
                previous_bars=before,
                previous_slot_returns=slot_returns,
            )
            values = store.compute(snapshot, names)
            later = closes.get(t + HORIZON - BAR)  # the close of the bar ending at t + 4 minutes
            sample = _sample(snapshot, values, names, later, touch)
            if touch is not None:
                # The label needs the bar that ended at t itself: an older close is not t's.
                closing = done[-1] if done and done[-1].start + BAR == t else None
                after = bars[bisect.bisect_left(starts, t) : bisect.bisect_left(starts, t + HORIZON)]
                outcome = bar_touch(closing, after, fee_rate) if closing is not None else None
                sample = replace(sample, outcome=outcome)
            out.append(sample)
            t += BAR
    return out


def recorded_samples(
    events: Iterable[Event],
    calendar: MarketCalendar,
    day: date,
    symbols: Sequence[str],
    names: Sequence[str],
    fee_rate: float,
    leaders: Leaders,
    store: FeatureStore | None = None,
    *,
    regime: str | None = None,
    target_version: int = 1,
) -> list[Sample]:
    """A sample per bar close of ``day``'s prediction window and symbol, in time order, from
    the day's events (in receipt order) through the engine's Market View (``recorded_day``).
    """
    made, _, _ = recorded_day(
        events,
        calendar,
        day,
        symbols,
        names,
        fee_rate,
        leaders,
        store,
        regime=regime,
        target_version=target_version,
    )
    return made


def recorded_day(
    events: Iterable[Event],
    calendar: MarketCalendar,
    day: date,
    symbols: Sequence[str],
    names: Sequence[str],
    fee_rate: float,
    leaders: Leaders,
    store: FeatureStore | None = None,
    *,
    regime: str | None = None,
    target_version: int = 1,
    kinds: Sequence[Kind] = (),
    previous: PreviousDays | None = None,
) -> tuple[list[Sample], Answers, dict[str, int]]:
    """A sample per bar close of ``day``'s prediction window and symbol, in time order, from
    the day's events (in receipt order) through the engine's Market View; the ``kinds``'
    answers of its windows (#179), as the engine answers them; and how many bar closes (and
    symbols) were left out, by why (#184: ``IN_VI``, ``SPECIAL``).

    At each bar close the features are computed before any event received at or after it,
    as the engine does. The outcome is the mid-point four minutes later against the one at
    the bar close, as live scoring decides it on basis A; with the touch target
    (``target_version`` 3, #169, #187), the label of the bar close's touch window, as the engine
    records it. A symbol in a VI at a bar close has no sample there (the engine does not
    predict it), and neither has a bar close whose four minutes a VI, circuit breaker or
    sidecar touches (live statistics leave it out). ``previous`` gives each symbol's previous
    trading day's ranges and bars (#181, #183), as the engine's does.
    """
    session = calendar.session(day)
    if session is None:
        return [], {}, {}
    store = store or FeatureStore()
    to_touch = target_version >= 2
    # With the touch target or the kinds, the view follows the windows as the engine's does.
    windows_fee = fee_rate if to_touch or kinds else None
    view = MarketView(
        calendar,
        symbols,
        leaders,
        datetime.combine(day, time(), KST),
        windows_fee,
        kinds=kinds,
        previous=previous,
    )
    special = SpecialSituations(calendar)
    first, last = prediction_window(session)
    # Every minute from the first bar close to the last target: a bar close to predict, a
    # target to look the mid-point up at, or both.
    minutes = deque(first + i * BAR for i in range((last + HORIZON - first) // BAR + 1))
    # Each bar close's snapshot, features, whether a window known then touched it, and the
    # touch target's terms there (target versions 2 and up).
    seen: list[tuple[Snapshot, dict[str, Value], bool, TouchTerms | None]] = []
    mids: dict[tuple[str, datetime], float | None] = {}
    labels: dict[tuple[str, datetime], Direction | None] = {}  # by bar close, once its window ended
    answers: Answers = {}  # the kinds' (#179), likewise
    left_out = {IN_VI: 0, SPECIAL: 0}  # the bar closes without a sample, by why (#184)

    def reach(moment: datetime) -> None:
        """Everything due at the minutes up to ``moment``, before what is received then."""
        while minutes and minutes[0] <= moment:
            minute = minutes.popleft()
            if minute <= last:
                frozen = view.at_close(minute)
                known = special.known(before=minute)
                for symbol in symbols:
                    if in_vi(known, symbol, minute):
                        left_out[IN_VI] += 1
                        continue
                    snapshot = view.snapshot(frozen, symbol, fee_rate, regime)
                    marked = touching(known, symbol, minute, minute + HORIZON)
                    terms = TouchTerms.at(view.quote_at(symbol, minute), snapshot.tick) if to_touch else None
                    seen.append((_bare(snapshot), store.compute(snapshot, names), marked, terms))
            if minute >= first + HORIZON:
                for symbol in symbols:
                    mids[(symbol, minute)] = view.mid(symbol, minute)
                    if to_touch:  # the window from four minutes back has just ended
                        labels[(symbol, minute - HORIZON)] = view.touch(symbol, minute - HORIZON).direction
                    for kind in kinds:
                        # The window from the kind's horizon back has just ended (#263): one that
                        # ends by the closing auction, as every minute here does.
                        if minute >= first + kind.horizon:
                            answers[(kind.name, symbol, minute - kind.horizon)] = view.kind_answer(
                                kind, symbol, minute - kind.horizon
                            )

    for feed, row in events:
        reach(row["received_at"])
        view.on_event(feed, row)
        special.event(feed, row, symbols)
    reach(minutes[-1] if minutes else last)
    # A longer kind's window (#263) touched by a special window, known then or learned since,
    # beyond the four minutes the samples were left out for: no answer to train on.
    for kind in kinds:
        if kind.horizon == HORIZON:
            continue
        for key in [key for key in answers if key[0] == kind.name]:
            _, symbol, t = key
            end = t + kind.horizon
            if touching(special.known(before=t), symbol, t, end) or touching(
                special.learned_since(t), symbol, t, end
            ):
                answers[key] = None
    out = []
    for snapshot, values, marked, terms in seen:
        # Special as the engine marks it: by the windows known when it is made, or learned since.
        t = snapshot.bar_close
        if marked or touching(special.learned_since(t), snapshot.symbol, t, t + HORIZON):
            left_out[SPECIAL] += 1
            continue
        later = mids.get((snapshot.symbol, snapshot.bar_close + HORIZON))
        sample = _sample(snapshot, values, names, later, terms)
        if to_touch:
            sample = replace(sample, outcome=labels.get((snapshot.symbol, t)))
        out.append(sample)
    return out, answers, left_out


def bar_answers(
    bar_days: BarDays,
    calendar: MarketCalendar,
    fee_rate: float,
    kinds: Sequence[Kind],
    *,
    previous: PreviousDays | None = None,
) -> Answers:
    """The ``kinds``' answers of each bar close of the days ``bar_days`` holds, by their bar
    rules (#179, ``Kind.bar_answer``): from the bar that ended at the bar close and the bars of
    the four minutes after it (those with trades); for a kind with barriers (#183), with their
    width at the bar close (``Kind.width``) from the day's bars finished by then and the
    previous trading day's from ``previous`` (none: unknown), at the bar close's close. None
    without that bar; a kind without a bar rule has none."""
    ruled = [k for k in kinds if k.bar_answer is not None]
    out: Answers = {}
    if not ruled:
        return out
    for (symbol, day), bars in bar_days.items():
        session = calendar.session(day)
        if session is None:
            continue
        ends = [b.start + BAR for b in bars]
        starts = [b.start for b in bars]
        before = previous.bars(symbol, day) if previous is not None and any(k.width for k in ruled) else ()
        t, last = prediction_window(session)
        while t <= last:
            done = bars[: bisect.bisect_right(ends, t)]
            closing = done[-1] if done and done[-1].start + BAR == t else None
            snapshot = None
            if closing is not None and any(k.width for k in ruled):
                snapshot = Snapshot.at(
                    symbol, t, done, closing.close, fee_rate, session_open=session.open, previous_bars=before
                )
            for kind in ruled:
                assert kind.bar_answer is not None
                if t + kind.horizon > session.closing_auction:
                    continue  # a longer kind's window would run into the closing auction (#263)
                if closing is None:
                    out[(kind.name, symbol, t)] = None
                    continue
                # The bars of the kind's horizon after it (four minutes, or longer, #263).
                after = bars[bisect.bisect_left(starts, t) : bisect.bisect_left(starts, t + kind.horizon)]
                width = kind.width(snapshot) if kind.width is not None and snapshot is not None else None
                out[(kind.name, symbol, t)] = kind.bar_answer(closing, after, fee_rate, width=width)
            t += BAR
    return out


def _bare(snapshot: Snapshot) -> Snapshot:
    """``snapshot`` without its bars and leaders, all a sample needs once its features are computed."""
    return Snapshot(
        snapshot.symbol,
        snapshot.bar_close,
        (),
        snapshot.price,
        snapshot.tick,
        snapshot.flat_ticks,
        snapshot.session_open,
    )


def _sample(
    snapshot: Snapshot,
    values: Mapping[str, Value],
    names: Sequence[str],
    later: float | None,
    touch: TouchTerms | None = None,
) -> Sample:
    """A sample of ``snapshot``, with the price four minutes later classified against its own;
    its features carry the touch target's terms ``touch`` (target versions 2 and up)."""
    change = None
    outcome = None
    if later is not None and snapshot.price and snapshot.tick and snapshot.flat_ticks:
        change = (later - snapshot.price) / snapshot.tick
        outcome = classify(later - snapshot.price, snapshot.flat_ticks, snapshot.tick)
    features = FeatureVector(values, names, touch)
    return Sample(snapshot.symbol, snapshot.bar_close, features, outcome, change)


class _DayBars:
    """A day's bars of several series, cut at any bar close."""

    def __init__(self, bars: Mapping[str, Sequence[Bar]]) -> None:
        self._bars = bars
        self._ends = {name: [b.start + BAR for b in each] for name, each in bars.items()}

    def until(self, t: datetime) -> dict[str, Sequence[Bar]]:
        """Each series' bars finished by ``t``."""
        return {name: each[: bisect.bisect_right(self._ends[name], t)] for name, each in self._bars.items()}


def _by_day(bar_days: BarDays) -> dict[date, _DayBars]:
    days: dict[date, dict[str, Sequence[Bar]]] = {}
    for (name, day), bars in bar_days.items():
        days.setdefault(day, {})[name] = bars
    return {day: _DayBars(bars) for day, bars in days.items()}


def leader_series(leaders: Leaders) -> list[tuple[str, str]]:
    """The leading series with backfilled bars, as (feed, name): the futures and the ETFs."""
    return [
        (FUTURES_BARS_TR, KOSPI200.series),
        (FUTURES_BARS_TR, USD.series),
        *((MINUTE_BARS_TR, etf) for etf in leaders.etfs),
    ]


def load_leader_days(data_dir: Path, leaders: Leaders, days: Iterable[date]) -> BarDays:
    """The leading series' backfilled bars on ``days``."""
    out: dict[tuple[str, date], list[Bar]] = {}
    for day in days:
        for feed, name in leader_series(leaders):
            bars = read_bars(data_dir, day, feed, name)
            if bars is not None:
                out[(name, day)] = bars
    return out


def bar_days_on(
    data_dir: Path, symbols: Sequence[str], days: Iterable[date]
) -> dict[tuple[str, date], list[Bar]]:
    """The backfilled bars of ``symbols`` on ``days``, where there are any."""
    out: dict[tuple[str, date], list[Bar]] = {}
    for day in days:
        for symbol in symbols:
            bars = read_bars(data_dir, day, MINUTE_BARS_TR, symbol)
            if bars is not None:
                out[(symbol, day)] = bars
    return out


@dataclass(frozen=True)
class Fitted:
    model: Model
    path: Path
    days: int
    recorded: list[date]  # of the days, those trained on recorded mid-points
    samples: int


def fit_algorithm(
    settings: Settings,
    name: str,
    days: int | None = None,
    now: datetime | None = None,
    *,
    kind_algorithms: Sequence[KindAlgorithm] = KIND_ALGORITHMS,
    kinds: Sequence[Kind] = KINDS,
) -> Fitted:
    """Train an algorithm on the latest days with data and save the model for the engine,
    which switches to it within a minute. Today counts only once its recording is archived
    (21:00): before that it is still being recorded. An algorithm of a kind besides the
    direction (#179) learns its kind's answers.
    """
    algorithm = next((a for a in ALGORITHMS if a.name == name), None)
    kind_algorithm = next((a for a in kind_algorithms if a.name == name), None)
    kind = None if kind_algorithm is None else kind_named(kind_algorithm.kind, kinds)
    chosen: Algorithm | KindAlgorithm
    if algorithm is not None:
        chosen = algorithm
    elif kind_algorithm is not None and kind is not None:
        chosen = kind_algorithm
    else:
        names = ", ".join([*(a.name for a in ALGORITHMS), *(a.name for a in kind_algorithms)])
        raise ValueError(f"unknown algorithm {name!r}; registered: {names}")
    features, training_days = chosen.features, chosen.training_days
    local = (now or datetime.now(KST)).astimezone(KST)
    finished = finished_days(settings.data_dir, settings.symbols, local)
    calendar = calendar_store(settings).calendar
    leaders = Leaders.of(settings)
    loaded: dict[date, TrainingDay] = {}
    for day in finished[-(days or training_days) :]:
        found = training_day(
            settings.data_dir,
            day,
            calendar,
            settings.symbols,
            features,
            settings.fee_rate,
            leaders,
            target_version=settings.target_version,
            kinds=() if kind is None else (kind,),
        )
        if found is not None:
            loaded[day] = found
    if not loaded:
        raise FileNotFoundError(
            f"no backfilled bars or recorded trades of {', '.join(settings.symbols)} in {settings.data_dir}"
        )
    if algorithm is not None:
        training = [s for found in loaded.values() for s in found.of(features)]
        model = algorithm.fit(training)
        labelled = sum(s.outcome is not None for s in training)
    else:
        assert kind_algorithm is not None and kind is not None
        answered = [s for found in loaded.values() for s in found.of_kind(kind, features)]
        model = kind_algorithm.fit(answered)
        labelled = sum(s.label is not None for s in answered)
    Shelf(settings.state_dir).install(name, model, "fit", local)
    return Fitted(
        model,
        model_path(settings.state_dir, name),
        len(loaded),
        [day for day, found in loaded.items() if found.reference == "mid"],
        labelled,
    )
