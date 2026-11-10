"""``krx-quant verdict-replay``: the verdict (#676) made again at each bar close of the recorded days,
and what its BUYs would have earned (#679). A diagnosis, not a tuning: the margin m, the lean's grades
and the family map are the verdict's version (``verdict.Rules``, fixed in #676 before this ran), and
nothing here picks another from these days (a choice made on them would be judged on them). A change is
a new verdict version, judged forward in the mock account (#677). It places no order and changes
nothing: no scoring, no status, no model reads it.

**Which days**: the latest ``DAYS`` (20) recorded trading days up to the latest whose session is over
(``--day``: up to that one), as a pre-validation picks them.

**The members, two ways**, each a verdict of its own at every bar close of the traded symbol
(``trader.SYMBOL``):

- *as live statuses were* (``LIVE``): the barrier algorithms the live engine ran that day, with the
  forecasts it stored (``kind_predictions``, status ok): what the engine knew then. An algorithm waiting
  for its pre-validation, or refused, had none; none of the barrier algorithms ran live before
  2026-11-10, so this verdict waits (``no_members``) on every day until one does.
- *every barrier algorithm* (``ALL``): each barrier kind's algorithm of the registry, whatever its
  status (active, waiting, refused), retrained before each day on its own window of the days before it
  (``prevalidation._WalkForward``, the pre-validation's walk-forward, #681), never on the day itself,
  and its forecast of each bar close of the day made from that day's training samples (their features,
  as the night makes them; as the queue screen, #599, predicts them, nothing is replayed through the
  engine). A day its model could not be trained for (nothing to learn before it: its defaults) it takes
  no part. The baselines are never members.

**The verdict's inputs at a bar close**, as the engine gives them (``engine._judge``): each horizon's
width and the bar close's reference mid-point and tick are those its answer fixed when its window opened
(the samples' answer path: ``w``, ``anchor``, ``tick``, nothing it saw after); the quote is the latest
best quote received by the decision (crossed ones aside), on the trader's stream of the symbol
(``trader.replay.read_recording``). The decision is made when the live engine had displayed every
prediction of that bar close (its latest ``displayed_at``, which the verdict waits for), else at the bar
close (no engine database, or no prediction stored). Nothing is held when the verdicts are counted.

**A BUY's trade**, by the paper trader's rules (``trader.book``, through the re-fit's replay of the
recording, ``trader.replay.Fills``): not before 09:05 nor with its deadline after 15:15 (the trader's
entry window: ``before_entry``, ``past_last_exit``); bought at the ask of the first quote received after
the decision within 5 s (else ``no_quote``; ``target_reached`` when that ask is at the target already,
``expired`` at the deadline, ``special`` when a special window known by then touches it); sold at the
target once a best bid reaches it (``take_profit``), at the first best bid at or below the stop
(``stop_loss``), or at the deadline (``time``). A recorded gap of the quotes over it, no recording or no
quote to sell on at the deadline leave it out (``gap``, ``no_recording``, ``no_exit_quote``). The net is
the trader's round trip (``money.net_of``: both commissions and the sell tax), one share, in won and in
ticks of its reference. Two ways:

- *each alone* (``alone``): every BUY its own share, overlapping or not;
- *one at a time* (``held``): a BUY while a share is held is not taken; while held, the verdict is made
  with the position (``verdict.Held``) and a SELL (exit) sells at the bid of the first quote received
  after its decision within 5 s (``verdict_exit``), before its target, stop or deadline.

**The report**, by verdict version (each run's are the version this code makes; the live engine's stored
ones, ``predictions.verdicts``, are counted by version beside them): the days replayed, each algorithm's
walk-forward; then for each way of the members, by day (the verdicts, BUYs by horizon, those filled, the
net each alone and one at a time), the verdicts by action and reason, the BUYs by horizon, and for each
way of trading the BUYs left out by why, those filled, how they ended (target, stop, deadline, the
verdict's exit), the net (the sum, a trade's mean, a day's mean with its day-level 95% interval,
``stats.day_mean``, every day replayed one value, a day without a trade 0), those that netted above 0,
the net in ticks and the largest drawdown of the cumulated net, trade by trade in the order sold.
Counts, rates, ticks and won: no price.

**Memory** (the server has 2 GB, and a pre-validation's replay was killed at 993 MB, #383): one
algorithm is walked forward at a time, its window of training days let go before the next
(``footprint.give_back``), and each of its days logs its memory (``footprint.DayUsage``) as the queue
screen's do (about 300 MB for a barrier algorithm on 2026-11-10); what is kept across days is a few
numbers a bar close. Then one day's recording is read at a time and let go. The run's peak resident
memory and its time are printed.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import statistics
import time as clock
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from functools import partial
from typing import Any

import numpy as np

from krx_quant.algorithms import ALGORITHMS, KIND_ALGORITHMS, Algorithm, KindAlgorithm, NotPredicted
from krx_quant.barriers import CLASSES as BARRIER_CLASSES
from krx_quant.config import Settings
from krx_quant.features import Leaders
from krx_quant.feeds.base import KST
from krx_quant.footprint import DayUsage, Meter, give_back
from krx_quant.inverse_replay import probed_day
from krx_quant.kind_registry import KINDS
from krx_quant.kinds import Kind
from krx_quant.market_calendar import MarketCalendar, Session, calendar_store
from krx_quant.predictions import DB_NAME, read_only
from krx_quant.prevalidation import PREVALIDATION_DAYS, _WalkForward
from krx_quant.sample_store import night_reads
from krx_quant.stats import day_mean
from krx_quant.trader import SYMBOL
from krx_quant.trader.book import (
    EXPIRED,
    NO_QUOTE,
    SPECIAL,
    STOP_LOSS,
    TAKE_PROFIT,
    TARGET_REACHED,
    TIME,
    Plan,
)
from krx_quant.trader.book import Rules as BookRules
from krx_quant.trader.cells import Signal, special_windows
from krx_quant.trader.money import net_of
from krx_quant.trader.replay import (
    GAP,
    NO_EXIT_QUOTE,
    NO_RECORDING,
    Fills,
    Recording,
    micros,
    moment,
    read_recording,
)
from krx_quant.training import finished_days, is_recorded
from krx_quant.verdict import (
    BUY,
    EXIT,
    RULES,
    SELL,
    Held,
    Horizon,
    Market,
    Member,
    Rules,
    Verdict,
    verdict,
)

log = logging.getLogger(__name__)

TITLE = "판정 재생 (#679)"
DAYS = PREVALIDATION_DAYS  # the latest recorded trading days replayed: the pre-validation's 20
NO_DAYS = 3  # exit code: no recorded day

# The members, two ways (module doc), in the report's order, with their words there.
LIVE, ALL = "live", "all"
MEMBERS = {LIVE: "라이브 그대로(엔진이 그날 돌린 장벽 알고리즘)", ALL: "장벽 알고리즘 모두(전진 학습)"}
# The trades, two ways (module doc).
ALONE, HELD = "alone", "held"
# How a BUY was taken: filled, else why not (module doc), in the report's order with their words.
FILLED = "filled"
BEFORE_ENTRY, PAST_LAST_EXIT = "before_entry", "past_last_exit"
HOLDING = "holding"  # one at a time: a share held already
NOT_TAKEN = {
    BEFORE_ENTRY: "09:05 전",
    PAST_LAST_EXIT: "기한이 15:15 뒤",
    HOLDING: "들고 있음",
    NO_RECORDING: "녹화 없음",
    GAP: "결측 구간",
    NO_QUOTE: "5초 안에 살 호가 없음",
    NO_EXIT_QUOTE: "팔 호가 없음",
    TARGET_REACHED: "살 때 이미 목표",
    EXPIRED: "기한 지남",
    SPECIAL: "특수 상황",
}
VERDICT_EXIT = "verdict_exit"
EXITS = {TAKE_PROFIT: "목표", STOP_LOSS: "손절", TIME: "기한", VERDICT_EXIT: "판정의 매도"}
# A verdict's action and its meaning or reason, as the report counts them.
ACTIONS = {
    "buy": "매수",
    "sell_exit": "매도(청산)",
    "sell_avoid": "매도(사지 않음)",
    "wait_no_edge": "기다림(근거 없음)",
    "wait_holding": "기다림(들고 있음)",
    "wait_no_members": "기다림(멤버 없음)",
}


# The bar closes, walked forward.


@dataclass
class Close:
    """A bar close of the traded symbol on a replayed day: what its windows fixed when they opened (the
    reference mid-point, its tick, each barrier kind's width) and each algorithm's forecast of each kind,
    walked forward (``ALL``)."""

    anchor: float | None = None
    tick: float | None = None
    widths: dict[str, int | None] = field(default_factory=dict)
    forecasts: dict[str, dict[str, tuple[float, float, float]]] = field(default_factory=dict)  # kind, name

    def fix(self, kind: Kind, path: Mapping[str, Any]) -> None:
        """What ``kind``'s answer fixed at the bar close (module doc): never what it saw after."""
        if self.anchor is None:
            self.anchor = _number(path.get("anchor"))
        if self.tick is None:
            self.tick = _number(path.get("tick"))
        w = path.get("w")
        self.widths[kind.name] = w if isinstance(w, int) and not isinstance(w, bool) else None


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


@dataclass
class Walked:
    """An algorithm's walk-forward: the days replayed, those its model was trained for, its forecasts
    and the errors of its forecasts (not counted, as live)."""

    days: int = 0
    trained: int = 0
    forecasts: int = 0
    errors: int = 0


Closes = dict[date, dict[datetime, Close]]


def barrier_kinds(kinds: Sequence[Kind]) -> list[Kind]:
    """The kinds the verdict's horizons are (#676): the barrier and its horizon ladder."""
    return sorted((k for k in kinds if k.classes == BARRIER_CLASSES), key=lambda k: k.horizon)


def walk(
    settings: Settings,
    kinds: Sequence[Kind],
    algorithms: Sequence[KindAlgorithm],
    available: Sequence[date],
    days: Sequence[date],
    calendar: MarketCalendar,
    night: Any,
) -> tuple[Closes, dict[str, Walked]]:
    """Each bar close of ``days`` (module doc): its windows' fixed figures, and each algorithm's forecast
    by its model trained before the day on its window of ``available`` days before it, one algorithm at
    a time (its days let go before the next)."""
    closes: Closes = {d: {} for d in days}
    walked: dict[str, Walked] = {}
    leaders = Leaders.of(settings)
    meter = Meter()
    named = {k.name: k for k in kinds}
    for algorithm in algorithms:
        kind = named[algorithm.kind]
        tally = walked[algorithm.name] = Walked()
        trainer = _WalkForward(settings, algorithm, available, calendar, leaders, kind, night=night)
        for n, day in enumerate(days, 1):
            usage = DayUsage(meter)  # its memory, logged (#383)
            with usage.phase("training"):
                model = trainer.before(day)
            trained = model.target_version == kind.answer_version  # not its defaults (#681's DayTrained)
            tally.days += 1
            tally.trained += trained
            with usage.phase("predicting"):
                for s in trainer.kind_samples_on(day):
                    if s.symbol != SYMBOL:
                        continue
                    close = closes[day].setdefault(s.bar_close, Close())
                    close.fix(kind, {} if s.answer is None else s.answer.path)
                    if not trained:
                        continue
                    try:
                        f = algorithm.predict(s.features, model)
                    except NotPredicted:
                        continue
                    except Exception:
                        if not tally.errors:
                            log.exception(
                                "%s: a forecast failed on %s; not counted, as live", algorithm.name, day
                            )
                        tally.errors += 1
                        continue
                    p = f.probabilities
                    close.forecasts.setdefault(kind.name, {})[algorithm.name] = (p[0], p[1], p[2])
                    tally.forecasts += 1
            give_back()
            log.info("%s: walked %s with %s", algorithm.name, day, model.version)
            log.info("%s: memory on %s (day %d of %d): %s", algorithm.name, day, n, len(days), usage.line())
        del trainer
        give_back()
    return closes, walked


# What the live engine stored.


@dataclass
class Stored:
    """A day of the live engine's database: the barrier algorithms' forecasts it stored by bar close
    (kind, name), when it had displayed every prediction of each bar close, and its verdicts by version
    and action."""

    forecasts: dict[datetime, dict[str, dict[str, tuple[float, float, float]]]] = field(default_factory=dict)
    displayed: dict[datetime, datetime] = field(default_factory=dict)
    verdicts: Counter[tuple[int, str]] = field(default_factory=Counter)


def stored_day(db: sqlite3.Connection, day: date, kinds: Sequence[Kind], names: Sequence[str]) -> Stored:
    """``day``'s ``Stored`` of the traded symbol."""
    start = datetime.combine(day, time(), KST)
    span = (SYMBOL, start.isoformat(), (start + timedelta(days=1)).isoformat())
    found = Stored()
    named = {k.name: k for k in kinds}
    marks = ",".join("?" * len(named))
    if named:
        rows = db.execute(
            "SELECT kind, algorithm, bar_close, output FROM kind_predictions WHERE symbol = ?"
            " AND bar_close >= ? AND bar_close < ? AND status = 'ok' AND output IS NOT NULL"
            f" AND kind IN ({marks})",
            (*span, *named),
        ).fetchall()
        wanted = set(names)
        for kind_name, name, bar_close, output in rows:
            if name not in wanted:
                continue
            kind = named[kind_name]
            try:
                values = json.loads(output)
                p = tuple(float(values[f"p_{c}"]) for c in kind.classes)
            except (ValueError, KeyError, TypeError):
                continue
            at = datetime.fromisoformat(bar_close)
            found.forecasts.setdefault(at, {}).setdefault(kind_name, {})[name] = (p[0], p[1], p[2])
    for bar_close, shown in db.execute(
        "SELECT bar_close, MAX(displayed_at) FROM (SELECT bar_close, displayed_at FROM predictions"
        " WHERE symbol = ? AND bar_close >= ? AND bar_close < ? UNION ALL SELECT bar_close, displayed_at"
        " FROM kind_predictions WHERE symbol = ? AND bar_close >= ? AND bar_close < ?) GROUP BY bar_close",
        (*span, *span),
    ).fetchall():
        found.displayed[datetime.fromisoformat(bar_close)] = datetime.fromisoformat(shown)
    try:
        for version, action, n in db.execute(
            "SELECT version, verdict, COUNT(*) FROM verdicts WHERE symbol = ? AND bar_close >= ?"
            " AND bar_close < ? GROUP BY version, verdict",
            span,
        ).fetchall():
            found.verdicts[(int(version), str(action))] += int(n)
    except sqlite3.OperationalError:  # a database from before the verdicts (#676)
        pass
    return found


# The trades.


@dataclass(frozen=True)
class Bought:
    """A BUY (or, one at a time, a BUY not taken for the share held): its day, horizon and bar close,
    how it was taken (``FILLED`` or why not), and once filled how it ended, its net in won and in ticks
    and when it was sold."""

    day: date
    kind: str
    bar_close: datetime
    taken: str
    exit: str | None = None
    net: float = 0.0
    ticks: float = 0.0
    sold_at: datetime | None = None


@dataclass
class Replayed:
    """A way of the members over the days: its verdicts by day and action, and its BUYs each alone and
    one at a time."""

    verdicts: dict[date, Counter[str]] = field(default_factory=dict)
    buys: dict[date, Counter[str]] = field(default_factory=dict)  # by horizon
    alone: list[Bought] = field(default_factory=list)
    held: list[Bought] = field(default_factory=list)


def _label(v: Verdict) -> str:
    if v.action == BUY:
        return "buy"
    return f"{v.action}_{v.meaning if v.action == SELL else v.reason}"


def _quote(day: Recording | None, at: datetime) -> tuple[float | None, float | None]:
    """The latest best quote received by ``at``, crossed ones aside (``engine._latest_quote``)."""
    if day is None:
        return None, None
    quotes = day.quotes
    i = int(np.searchsorted(quotes.times, micros(at), side="right")) - 1
    while i >= 0 and quotes.asks[i] <= quotes.bids[i]:
        i -= 1
    return (None, None) if i < 0 else (float(quotes.bids[i]), float(quotes.asks[i]))


@dataclass
class _Position:
    """The share held one at a time: its record's place, its barriers and when its own exit sells."""

    index: int
    held: Held
    entry: float
    tick: float
    sold_at: datetime


class Day:
    """A replayed day's verdicts and trades (module doc), for each way of the members."""

    def __init__(
        self,
        day: date,
        session: Session,
        closes: Mapping[datetime, Close],
        kinds: Sequence[Kind],
        fills: Fills,
        decided: Mapping[datetime, datetime],
        fee_rate: float,
        book: BookRules,
        rules: Rules = RULES,
    ) -> None:
        self.day = day
        self._session = session
        self._closes = closes
        self._kinds = kinds
        self._fills = fills
        self._decided = decided
        self._fee = fee_rate
        self._book = book
        self._rules = rules

    def replay(
        self,
        members: Callable[[datetime, Close], Mapping[str, Mapping[str, tuple[float, float, float]]]],
        into: Replayed,
    ) -> None:
        """The day's verdicts with ``members`` (by bar close: kind, name, probabilities) into ``into``."""
        counted = into.verdicts.setdefault(self.day, Counter())
        buys = into.buys.setdefault(self.day, Counter())
        position: _Position | None = None
        for bar_close in sorted(self._closes):
            close = self._closes[bar_close]
            decided = max(self._decided.get(bar_close, bar_close), bar_close)
            horizons = self._horizons(close, members(bar_close, close))
            bid, ask = _quote(self._fills.day, decided)
            market = Market(close.anchor, bid, ask, self._fee)
            v = verdict(SYMBOL, bar_close, horizons, market, rules=self._rules)
            counted[_label(v)] += 1
            if position is not None and position.sold_at <= decided:
                position = None
            if position is not None:  # one at a time: the verdict with the share held
                held = verdict(SYMBOL, bar_close, horizons, market, position.held, rules=self._rules)
                if held.action == SELL and held.meaning == EXIT and self._exit(into, position, decided):
                    position = None
            if v.action != BUY or v.horizon is None:
                continue
            buys[v.horizon.kind] += 1
            alone, filled = self._buy(v, decided)
            into.alone.append(alone)
            if position is not None:
                into.held.append(Bought(self.day, alone.kind, bar_close, HOLDING))
                continue
            into.held.append(alone)
            if filled is not None:
                position = filled
                position.index = len(into.held) - 1

    def _horizons(
        self, close: Close, members: Mapping[str, Mapping[str, tuple[float, float, float]]]
    ) -> list[Horizon]:
        return [
            Horizon(
                k.name,
                round(k.horizon.total_seconds() / 60),
                close.widths.get(k.name),
                [Member(name, k.name, None, 0, p) for name, p in sorted(members.get(k.name, {}).items())],
            )
            for k in self._kinds
        ]

    def _buy(self, v: Verdict, decided: datetime) -> tuple[Bought, _Position | None]:
        """A BUY's share bought and sold by the paper trader's rules (module doc)."""
        h = v.horizon
        assert h is not None and h.target is not None and h.stop is not None and h.tick
        bar_close = v.bar_close
        deadline = bar_close + timedelta(minutes=h.minutes)
        session, book = self._session, self._book
        if bar_close < session.open + book.entry_after_open:
            return Bought(self.day, h.kind, bar_close, BEFORE_ENTRY), None
        if deadline > session.closing_auction - book.exit_before_auction:
            return Bought(self.day, h.kind, bar_close, PAST_LAST_EXIT), None
        signal = Signal(
            prediction_id=micros(bar_close),
            kind=h.kind,
            predictor="verdict",
            symbol=SYMBOL,
            bar_close=bar_close,
            displayed_at=decided,
            status="ok",
            probabilities=h.probabilities,
            rise=0,
            anchor=h.target - (h.width_ticks or 0) * h.tick,
            tick=h.tick,
            width=h.width_ticks,
            horizon=timedelta(minutes=h.minutes),
        )
        fill = self._fills.at(signal, Plan(h.target, h.stop, deadline), decided)
        why = fill.missing or fill.refused
        if why is not None:
            return Bought(self.day, h.kind, bar_close, why), None
        assert fill.entry is not None and fill.sale is not None and fill.entry_at is not None
        net = net_of(fill.entry, fill.sale.price, 1, self._fee)
        bought = Bought(
            self.day, h.kind, bar_close, FILLED, fill.sale.reason, net, net / h.tick, fill.free_at
        )
        held = Held(fill.entry, h.target, h.stop, deadline)
        return bought, _Position(-1, held, fill.entry, h.tick, fill.free_at)

    def _exit(self, into: Replayed, position: _Position, decided: datetime) -> bool:
        """The held share sold on a SELL (exit) decided at ``decided``: at the bid of the first quote
        received after it within ``quote_wait``, if before its own exit; False without one."""
        day = self._fills.day
        if day is None:
            return False
        times = day.quotes.times
        k = int(np.searchsorted(times, micros(decided), side="right"))
        if k >= len(times) or times[k] > micros(decided + self._book.quote_wait):
            return False
        sold_at = moment(int(times[k]))
        if sold_at >= position.sold_at:
            return False
        net = net_of(position.entry, float(day.quotes.bids[k]), 1, self._fee)
        was = into.held[position.index]
        into.held[position.index] = Bought(
            was.day, was.kind, was.bar_close, FILLED, VERDICT_EXIT, net, net / position.tick, sold_at
        )
        return True


# The run.


@dataclass
class Run:
    """The days replayed, each algorithm's walk-forward, each way of the members' replay, the live
    engine's stored verdicts by version and action, and whether there was an engine database."""

    days: list[date]
    walked: dict[str, Walked]
    replayed: dict[str, Replayed]
    stored: Counter[tuple[int, str]]
    engine_db: bool


def replay(
    settings: Settings,
    days: Sequence[date],
    available: Sequence[date],
    calendar: MarketCalendar,
    *,
    algorithms: Sequence[Algorithm] = ALGORITHMS,
    kind_algorithms: Sequence[KindAlgorithm] = KIND_ALGORITHMS,
    kinds: Sequence[Kind] = KINDS,
    rules: Rules = RULES,
) -> Run:
    """The verdicts of ``days`` both ways and their trades (module doc)."""
    horizons = barrier_kinds(kinds)
    names = {k.name for k in horizons}
    members = [a for a in kind_algorithms if a.kind in names]
    night = night_reads(algorithms, kind_algorithms, kinds)
    closes, walked = walk(settings, horizons, members, available, days, calendar, night)
    run = Run(list(days), walked, {LIVE: Replayed(), ALL: Replayed()}, Counter(), False)
    book = BookRules(fee_rate=settings.fee_rate)
    with read_only(settings.state_dir / DB_NAME) as db:
        run.engine_db = db is not None
        if db is not None:
            db.row_factory = sqlite3.Row
        for day in days:
            session = calendar.session(day)
            if session is None:
                continue
            start = datetime.combine(day, time(), KST)
            stored = Stored() if db is None else stored_day(db, day, horizons, [a.name for a in members])
            run.stored.update(stored.verdicts)
            windows = [] if db is None else special_windows(db, SYMBOL, start)
            fills = Fills(read_recording(settings.data_dir, day), windows, book)
            replayed = Day(
                day,
                session,
                closes.get(day, {}),
                horizons,
                fills,
                stored.displayed,
                settings.fee_rate,
                book,
                rules,
            )
            replayed.replay(partial(_stored, stored.forecasts), run.replayed[LIVE])
            replayed.replay(_walked, run.replayed[ALL])
            del fills, replayed, stored
            give_back()
            log.info("verdict replay: %s replayed", day)
    return run


Forecasts = Mapping[str, Mapping[str, tuple[float, float, float]]]  # by kind, then algorithm


def _stored(forecasts: Mapping[datetime, Forecasts], bar_close: datetime, close: Close) -> Forecasts:
    """The members as live statuses were (``LIVE``): the forecasts the live engine stored."""
    return forecasts.get(bar_close, {})


def _walked(bar_close: datetime, close: Close) -> Forecasts:
    """Every barrier algorithm, walked forward (``ALL``)."""
    return close.forecasts


# The report.


def _pct(part: float, whole: float) -> str:
    return f"{100 * part / whole:.1f}%" if whole else "-"


def _won(value: float) -> str:
    return f"{value:+,.1f}원"


def drawdown(trades: Sequence[Bought]) -> float:
    """The largest fall of the cumulated net from its highest before, trade by trade in the order sold
    (0 when it never fell)."""
    top = total = worst = 0.0
    for t in sorted(trades, key=lambda t: t.sold_at or datetime.min.replace(tzinfo=KST)):
        total += t.net
        top = max(top, total)
        worst = max(worst, top - total)
    return worst


def trade_lines(buys: Sequence[Bought], days: Sequence[date]) -> list[str]:
    """A way of trading's lines (module doc "The report")."""
    taken = Counter(b.taken for b in buys)
    filled = [b for b in buys if b.taken == FILLED]
    left = " · ".join(f"{NOT_TAKEN[r]} {taken[r]}" for r in NOT_TAKEN if taken[r])
    lines = [
        f"매수 {len(buys)} · 체결 {len(filled)}({_pct(len(filled), len(buys))}) · 안 산 것: {left or '없음'}"
    ]
    if not filled:
        return lines
    exits = Counter(b.exit for b in filled)
    lines.append("청산: " + " · ".join(f"{EXITS[e]} {exits[e]}" for e in EXITS))
    nets = [b.net for b in filled]
    by_day = {d: [sum(b.net for b in filled if b.day == d)] for d in days}
    interval = day_mean(by_day)
    daily = "하루 평균 -"
    if interval is not None and interval.low is not None and interval.high is not None:
        daily = (
            f"하루 평균 {_won(interval.value)}(거래일 단위 95% [{_won(interval.low)}, {_won(interval.high)}],"
            f" {interval.n}일)"
        )
    gained = sum(n > 0 for n in nets)
    lines.append(
        f"1주 순손익: 합 {_won(sum(nets))} · 매매당 평균 {_won(statistics.fmean(nets))} · {daily}"
        f" · 이익 {gained}({_pct(gained, len(nets))})"
    )
    ticks = statistics.fmean(b.ticks for b in filled)
    lines.append(f"순 틱 평균 {ticks:+.2f} · 최대 낙폭 {_won(-drawdown(filled))}")
    return lines


def _minutes(kind: Kind) -> int:
    return round(kind.horizon.total_seconds() / 60)


def members_lines(replayed: Replayed, days: Sequence[date], kinds: Sequence[Kind]) -> list[str]:
    """A way of the members' sections (module doc "The report")."""
    out = ["  1. 날마다"]
    for day in days:
        verdicts = replayed.verdicts.get(day, Counter())
        buys = replayed.buys.get(day, Counter())
        by_horizon = " · ".join(f"{_minutes(k)}분 {buys[k.name]}" for k in kinds if buys[k.name])
        alone = [b for b in replayed.alone if b.day == day and b.taken == FILLED]
        held = [b for b in replayed.held if b.day == day and b.taken == FILLED]
        weekday = "월화수목금토일"[day.weekday()]
        out.append(
            f"    {day}({weekday}): 판정 {sum(verdicts.values())} · 매수 {sum(buys.values())}"
            + (f"({by_horizon})" if by_horizon else "")
            + f" · 체결 {len(alone)} · 각각 {_won(sum(b.net for b in alone))}"
            f" · 하나씩 {len(held)}건 {_won(sum(b.net for b in held))}"
        )
    total: Counter[str] = Counter()
    for counted in replayed.verdicts.values():
        total.update(counted)
    n = sum(total.values())
    shown = " · ".join(f"{ACTIONS.get(a, a)} {total[a]}({_pct(total[a], n)})" for a in ACTIONS if total[a])
    out += ["  2. 판정", f"    판정 {n}: {shown or '없음'}"]
    out.append("  3. 매수 각각 1주(겹쳐도 각각)")
    out += [f"    {line}" for line in trade_lines(replayed.alone, days)]
    for k in kinds:
        these = [b for b in replayed.alone if b.kind == k.name]
        if these:
            out += [f"    {_minutes(k)}분({k.name})", *(f"      {line}" for line in trade_lines(these, days))]
    out.append("  4. 한 번에 하나(들고 있는 동안의 판정이 매도면 판다)")
    out += [f"    {line}" for line in trade_lines(replayed.held, days)]
    return out


def report_lines(
    run: Run, kinds: Sequence[Kind], rules: Rules, seconds: float, peak: float | None
) -> list[str]:
    """The report (module doc)."""
    days = run.days
    horizons = barrier_kinds(kinds)
    families = ", ".join(f"{name}→{family}" for name, family in sorted(rules.families.items())) or "없음"
    out = [
        f"{TITLE}: {SYMBOL}의 판정(#676)을 기록된 날의 봉 마감마다 다시 내리고, 매수를 1주씩 재생한다",
        "진단이지 조정이 아니다. 개수, 비율, 틱, 원만 보인다(가격은 없다)."
        ' 규칙은 `docs/operations.md` "판정 재생 (#679)".',
        "",
        f"판정 버전 {rules.version}: m {rules.margin} · 기울기 하한 {rules.lean_floor}"
        f" · 약 {rules.moderate} 미만 · 강 {rules.strong} 이상 · 가족 {families}."
        " m과 가족은 #676에서 정했고, 이 날들로 고르지 않는다"
        "(바꾸면 새 판정 버전이고 모의 계좌에서 앞으로 판단한다, #677).",
        f"재생한 날: {days[0]} – {days[-1]}, {len(days)}거래일",
        "전진 학습(장벽 알고리즘 모두): "
        + (
            " · ".join(
                f"{name} 학습 {w.trained}/{w.days}일, 예측 {w.forecasts}"
                + (f", 오류 {w.errors}" if w.errors else "")
                for name, w in run.walked.items()
            )
            or "장벽 알고리즘 없음"
        ),
    ]
    if not run.engine_db:
        out.append("엔진 DB가 없다: 라이브 멤버는 없고, 결정 시각은 봉 마감이다.")
    stored = " · ".join(f"버전 {v} {ACTIONS.get(a, a)} {n}" for (v, a), n in sorted(run.stored.items()))
    out.append(f"엔진이 저장한 판정(이 날들): {stored or '없음'}")
    for i, (way, words) in enumerate(MEMBERS.items(), 1):
        out += [
            "",
            f"{i}. {words} · 판정 버전 {rules.version}",
            *members_lines(run.replayed[way], days, horizons),
        ]
    out += [
        "",
        f"{len(MEMBERS) + 1}. 실행",
        f"  {seconds:.1f}초 · 최대 RSS {'-' if peak is None else f'{peak:.0f} MB'}",
    ]
    return out


def verdict_replay(
    settings: Settings,
    now: datetime,
    day: date | None = None,
    days: int = DAYS,
    *,
    algorithms: Sequence[Algorithm] = ALGORITHMS,
    kind_algorithms: Sequence[KindAlgorithm] = KIND_ALGORITHMS,
    kinds: Sequence[Kind] = KINDS,
    rules: Rules = RULES,
) -> tuple[str, int]:
    """The command: the report of the latest ``days`` recorded days up to ``day`` (default: the latest
    trading day whose session is over), and its exit code."""
    started, meter = clock.monotonic(), Meter()
    calendar = calendar_store(settings).calendar
    through = probed_day(calendar, now) if day is None else day
    available = finished_days(settings.data_dir, settings.symbols, now)
    replayed = [
        d
        for d in available
        if d <= through
        and calendar.session(d) is not None
        and is_recorded(settings.data_dir, d, settings.symbols)
    ][-days:]
    if not replayed:
        return f"{TITLE}: {through}까지 기록된 거래일이 없다.", NO_DAYS
    run = replay(
        settings,
        replayed,
        available,
        calendar,
        algorithms=algorithms,
        kind_algorithms=kind_algorithms,
        kinds=kinds,
        rules=rules,
    )
    lines = report_lines(run, kinds, rules, clock.monotonic() - started, meter.peak_mb())
    return "\n".join(lines), 0
