"""Does the SMT raise the four-minute prediction's hit rate? (``krx-quant smt-experiment``, #519, #523)

Spec #519 puts the rolling-extreme SMT (``smt.py``, #520) into the prediction only if it passes an
experiment. A frozen rule that calls the SMT's direction is compared, on the same bar closes, with
what the engine published there. Everything below was fixed in #523 before any of the holdout's days
(none of it is tuned, and nothing is changed after seeing a result):

- **The frozen rule** (``RULE``, ``CALLS``): at an SMT event (``smt.SmtEvent``: an episode's first
  bar close) a bearish SMT calls down and a bullish one up, with certainty, for each stock of the
  pair. The futures are not predicted; a stock gets the call when the futures swept too, as its
  chart shows the event (#519). Nothing is fitted.
- **The engine's prediction** (``ENGINE``): what the engine published for that stock and bar close,
  its final consensus (``consensus-final``), made (status ``ok``). A bar close without one (an empty
  consensus, ``no_members`` or ``none_finished``) is left out of the comparison and counted
  (``Claim.unpublished``): no other predictor stands in for it, since the engine published none.
- **The answer** (``read_answers``): the bar close's touch label of answer version ``TARGET`` (3)
  in the engine DB's ``touches``, as the engine scored it, not the sample store's. A bar close that
  could not be scored (no class) or whose four minutes a special situation touched (a prediction
  there marked ``outcomes.special``) has none.
- **The claims** (``CLAIMS``): four, direction × L (bearish 15, bearish 30, bullish 15, bullish 30).
  Each pools the pairs and the stocks (``units``): every (stock, bar close) at which an event of
  that direction and L begins in a pair holding the stock, once however many pairs begin one there.
  The swept side does not matter.
- **The metric**: the rule's hit rate less the engine's on the same (stock, bar close)s, in
  percentage points (``holdout.judge``, paired by symbol and bar close).
- **The verdict** (``holdout.verdict``): on the metric taken a trading day at a time (each day's
  mean gain one value, a Student t interval over the days, ``stats.day_lift``): the events of a day
  overlap, and its pairs and stocks move together. At ``LEVEL``, 98.75% (Bonferroni over the four
  claims: 1 − 0.05 ÷ 4). A claim stands only when the interval's lower end is above 0. With fewer
  than ``MIN_SCORED`` (200) pairs, or pairs on fewer than ``MIN_DAYS`` (8) of the window's days, it
  has too few (``표본 부족``) and is not judged.
- **The holdout** (``holdout_days``): the first ``DAYS`` (10) eligible days after ``AFTER``
  (2026-11-04, the day #523 was merged), a fixed window that a later run reads the same. A day is
  eligible (``eligible``) when it is a trading day, finished (its recording archived, 21:00), the
  stocks' integrated trades were recorded (``training.is_recorded``: the events come from the
  recording, ``smt.reconstruct``), and the engine DB holds a scored final consensus of a stock that
  day (a day the consensus was empty all day compares nothing). It never looks at the SMT events: a
  day with few is in like any other.
- **The events**: every day's are made again from its recording through the engine's Market View
  (``smt.reconstruct``, source ``reconstructed``), the same rule on the discovery's days and the
  holdout's. The live table (``smt_events``) is not read: an engine restarted in the day misses the
  windows reaching back before it.

Two stages, each a scheduled job (``scheduled.research``) posting on #519:

- **Discovery** (``smt-experiment``): the finished recorded days up to ``AFTER``, every one of them
  (their answers and the engine's predictions where the DB has them); and apart, the days with
  backfilled one-minute bars only, whose answers are approximate (``touch.bar_touch``, the training's
  bar rule) and which have no engine prediction: they never enter a comparison with the engine nor
  the verdict.
- **Holdout** (``smt-experiment --holdout``): the four claims judged on the window, once it is full;
  nothing before.

Both print the secondary outputs: each pair's one-minute return correlation and the share of the
session's minutes both have a bar (from the backfilled bars, ``CoMove``), the events by pair,
direction, L and swept side with the days they came on, the up/flat/down shares after each claim's
events beside those of every answered bar close, and the rule against ``baseline-climate`` on the
same bar closes (described, not judged). Counts, rates, percentage points and dates: no price.

It reads the data directory and the engine DB (``predictions.read_only``) and writes nothing.
"""

from __future__ import annotations

import bisect
import logging
import math
import sqlite3
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

from krx_quant.backfill import FUTURES_BARS_TR, MINUTE_BARS_TR, read_bars
from krx_quant.climate import CLIMATE
from krx_quant.config import Settings
from krx_quant.consensus import FINAL
from krx_quant.feeds.base import KST, kst_day
from krx_quant.holdout import FAILS, STANDS, TOO_FEW, Judged, _ratio, _span, judge
from krx_quant.market_calendar import MarketCalendar, Session
from krx_quant.market_view import HORIZON
from krx_quant.predictions import DB_NAME, iso, read_only
from krx_quant.pricing import Direction
from krx_quant.reference import BAR, Bar
from krx_quant.smt import (
    BACKFILLED,
    BEARISH,
    BULLISH,
    DEFINITION,
    DIRECTIONS,
    FUTURES,
    LENGTHS,
    PAIRS,
    RECORDED,
    STOCKS,
    VERSION,
    SmtEvent,
    closes,
    day_events,
    reconstruct,
)
from krx_quant.stats import Scored, points_text
from krx_quant.touch import bar_touch
from krx_quant.training import finished_days, is_recorded

log = logging.getLogger(__name__)

# Fixed in #523 before the holdout's days: none of these is tuned.
AFTER = date(2026, 11, 4)  # the cutoff: the day #523 was merged; the holdout's days are after it
DAYS = 10  # the holdout's eligible trading days
CLAIMS: tuple[tuple[str, int], ...] = tuple((d, m) for d in DIRECTIONS for m in LENGTHS)  # direction × L
LEVEL = 1 - 0.05 / len(CLAIMS)  # each interval's: Bonferroni over the four claims, 98.75%
MIN_SCORED = 200  # a claim's pairs (the rule and the engine on the same bar close), as #447's cells
MIN_DAYS = 8  # the window's days a claim's pairs must cover
TARGET = 3  # the answer version: the touch label crossed quotes count in (#187)
ENGINE = FINAL  # the engine's published prediction: its final consensus
RULE = "smt-rule"  # the frozen rule's records, as the lifts pair them
CALLS = {BEARISH: Direction.DOWN, BULLISH: Direction.UP}
SURE = {Direction.UP: (1.0, 0.0, 0.0), Direction.DOWN: (0.0, 0.0, 1.0)}  # (p_up, p_flat, p_down)

Key = tuple[str, datetime]  # (stock, bar close)
Claim = tuple[str, int]  # (direction, L)
Pair = tuple[str, str]

NAMES = {BEARISH: "하락", BULLISH: "상승"}
CLASSES = (Direction.UP, Direction.FLAT, Direction.DOWN)
CLASS_NAMES = {Direction.UP: "상승", Direction.FLAT: "보합", Direction.DOWN: "하락"}


# What a day gives


@dataclass(frozen=True)
class CoMove:
    """Two instruments' one-minute bars over sessions (``co_move``): the session's minutes (from the
    open to the closing auction), those with a bar of each and of both, and the sums of their paired
    one-minute log returns (both had bars that minute and the one before)."""

    minutes: int = 0
    a: int = 0
    b: int = 0
    both: int = 0
    n: int = 0
    sx: float = 0.0
    sy: float = 0.0
    sxx: float = 0.0
    syy: float = 0.0
    sxy: float = 0.0

    def __add__(self, other: CoMove) -> CoMove:
        return CoMove(
            self.minutes + other.minutes,
            self.a + other.a,
            self.b + other.b,
            self.both + other.both,
            self.n + other.n,
            self.sx + other.sx,
            self.sy + other.sy,
            self.sxx + other.sxx,
            self.syy + other.syy,
            self.sxy + other.sxy,
        )

    @property
    def correlation(self) -> float | None:
        """The returns' Pearson correlation; None from fewer than two or without spread."""
        if self.n < 2:
            return None
        vx = self.sxx - self.sx**2 / self.n
        vy = self.syy - self.sy**2 / self.n
        if vx <= 0 or vy <= 0:
            return None
        return (self.sxy - self.sx * self.sy / self.n) / math.sqrt(vx * vy)


def co_move(session: Session, a: Sequence[Bar] | None, b: Sequence[Bar] | None) -> CoMove:
    """``a``'s and ``b``'s bars (oldest first; None: none) over ``session``'s continuous minutes."""
    closes_a = {bar.start: bar.close for bar in a or () if bar.close > 0}
    closes_b = {bar.start: bar.close for bar in b or () if bar.close > 0}
    minutes = in_a = in_b = both = 0
    xs: list[float] = []
    ys: list[float] = []
    minute = session.open
    while minute < session.closing_auction:
        minutes += 1
        in_a += minute in closes_a
        in_b += minute in closes_b
        if minute in closes_a and minute in closes_b:
            both += 1
            before = minute - BAR
            if before >= session.open and before in closes_a and before in closes_b:
                xs.append(math.log(closes_a[minute] / closes_a[before]))
                ys.append(math.log(closes_b[minute] / closes_b[before]))
        minute += BAR
    return CoMove(
        minutes,
        in_a,
        in_b,
        both,
        len(xs),
        sum(xs),
        sum(ys),
        sum(x * x for x in xs),
        sum(y * y for y in ys),
        sum(x * y for x, y in zip(xs, ys, strict=True)),
    )


@dataclass(frozen=True)
class Day:
    """A day as the experiment reads it (``measure``): its events, the answers, the engine's and the
    climate's predictions at the (stock, bar close)s the events call (``units``), the classes of
    every answered bar close of its stocks, and each pair's one-minute bars."""

    day: date
    bars: str  # what its events were made from: smt.RECORDED or smt.BACKFILLED
    events: list[SmtEvent]
    answers: dict[Key, Direction]  # at the units' (stock, bar close)s
    engine: dict[Key, Scored]  # likewise, the final consensus made there
    climate: dict[Key, Scored]  # likewise, baseline-climate's
    base: Counter[Direction]  # every answered bar close of its stocks
    co_moves: dict[Pair, CoMove] = field(default_factory=dict)


def units(events: Iterable[SmtEvent]) -> dict[Claim, set[Key]]:
    """Each claim's (stock, bar close)s: every stock of a pair with an event of that direction and
    L there, once however many pairs have one."""
    out: dict[Claim, set[Key]] = {claim: set() for claim in CLAIMS}
    for e in events:
        for stock in (e.a, e.b):
            if stock != FUTURES:
                out[(e.direction, e.minutes)].add((stock, _instant(e.bar_close)))
    return out


def pair_units(events: Iterable[SmtEvent]) -> dict[tuple[Pair, Claim], set[Key]]:
    """``units`` of each pair alone."""
    out: dict[tuple[Pair, Claim], set[Key]] = {}
    for e in events:
        for stock in (e.a, e.b):
            if stock != FUTURES:
                out.setdefault(((e.a, e.b), (e.direction, e.minutes)), set()).add(
                    (stock, _instant(e.bar_close))
                )
    return out


def _instant(at: datetime) -> datetime:
    """``at`` in KST, as the DB's times are read back."""
    return at.astimezone(KST)


def measure(
    day: date,
    bars: str,
    events: Sequence[SmtEvent],
    answers: Mapping[Key, Direction],
    engine: Mapping[Key, Scored],
    climate: Mapping[Key, Scored],
    co_moves: Mapping[Pair, CoMove] | None = None,
) -> Day:
    """``day`` from its events and every answered (stock, bar close) of it, with the engine's and
    the climate's predictions (by (stock, bar close)); only what the events call is kept."""
    called: set[Key] = set()
    for keys in units(events).values():
        called |= keys
    return Day(
        day,
        bars,
        list(events),
        {k: v for k, v in answers.items() if k in called},
        {k: v for k, v in engine.items() if k in called},
        {k: v for k, v in climate.items() if k in called},
        Counter(answers.values()),
        dict(co_moves or {}),
    )


# The claims


@dataclass(frozen=True)
class Measured:
    """A claim over days (``measured``): the rule's records with an answer, the engine's and the
    climate's at the same bar closes, the rule's records without an engine prediction, and the
    answers' classes."""

    claim: Claim
    rule: list[Scored]
    engine: list[Scored]
    climate: list[Scored]
    unpublished: int
    shares: Counter[Direction]

    def against_engine(self, level: float = LEVEL) -> Judged:
        """The rule against the engine on the bar closes both have: the verdict's comparison."""
        theirs = {(r.symbol, r.bar_close) for r in self.engine}
        paired = [r for r in self.rule if (r.symbol, r.bar_close) in theirs]
        return judge(paired, self.engine, 1, level, MIN_SCORED, MIN_DAYS)

    def against_climate(self, level: float = LEVEL) -> Judged:
        theirs = {(r.symbol, r.bar_close) for r in self.climate}
        paired = [r for r in self.rule if (r.symbol, r.bar_close) in theirs]
        return judge(paired, self.climate, 1, level, MIN_SCORED, MIN_DAYS)


def rule_record(key: Key, direction: str, answer: Direction) -> Scored:
    """The frozen rule's call at ``key``: ``direction``'s, with certainty."""
    p_up, p_flat, p_down = SURE[CALLS[direction]]
    symbol, bar_close = key
    return Scored(RULE, f"{DEFINITION}-{VERSION}", symbol, bar_close, p_up, p_flat, p_down, answer)


def measured(days: Sequence[Day], claim: Claim, selects: Pair | None = None) -> Measured:
    """``claim`` over ``days`` (of the pair ``selects`` alone, if given)."""
    rule: list[Scored] = []
    engine: list[Scored] = []
    climate: list[Scored] = []
    unpublished = 0
    shares: Counter[Direction] = Counter()
    for d in days:
        if selects is None:
            keys = units(d.events)[claim]
        else:
            keys = pair_units(d.events).get((selects, claim), set())
        for key in sorted(keys):
            answer = d.answers.get(key)
            if answer is None:
                continue
            shares[answer] += 1
            rule.append(rule_record(key, claim[0], answer))
            if key in d.engine:
                engine.append(d.engine[key])
            else:
                unpublished += 1
            if key in d.climate:
                climate.append(d.climate[key])
    return Measured(claim, rule, engine, climate, unpublished, shares)


# Reading


def read_answers(conn: sqlite3.Connection, day: date) -> dict[Key, Direction]:
    """The stocks' touch labels of answer version ``TARGET`` on ``day``, by (stock, bar close):
    those scored, with no prediction there marked special."""
    marks = ", ".join("?" for _ in STOCKS)
    rows = conn.execute(
        "SELECT t.symbol, t.bar_close, t.class FROM touches t"
        f" WHERE t.target_version = ? AND t.class IS NOT NULL AND t.symbol IN ({marks})"
        " AND t.bar_close >= ? AND t.bar_close < ? AND NOT EXISTS (SELECT 1 FROM predictions p"
        " JOIN outcomes o ON o.prediction_id = p.id WHERE p.symbol = t.symbol"
        " AND p.bar_close = t.bar_close AND o.special = 1)",
        (TARGET, *STOCKS, *_span_of(day)),
    )
    return {(symbol, datetime.fromisoformat(at)): Direction(label) for symbol, at, label in rows}


def read_calls(conn: sqlite3.Connection, day: date, algorithm: str) -> dict[Key, Scored]:
    """``algorithm``'s predictions made (status ``ok``) for the stocks on ``day``, by (stock, bar
    close), without their answer (``Direction.FLAT`` stands in; ``with_answers`` sets it)."""
    marks = ", ".join("?" for _ in STOCKS)
    rows = conn.execute(
        "SELECT symbol, bar_close, model_version, p_up, p_flat, p_down FROM predictions"
        f" WHERE algorithm = ? AND symbol IN ({marks}) AND bar_close >= ? AND bar_close < ?"
        " AND status = 'ok' AND p_up IS NOT NULL",
        (algorithm, *STOCKS, *_span_of(day)),
    )
    out: dict[Key, Scored] = {}
    for symbol, at, version, up, flat, down in rows:
        when = datetime.fromisoformat(at)
        out[(symbol, when)] = Scored(algorithm, version, symbol, when, up, flat, down, Direction.FLAT)
    return out


def with_answers(calls: Mapping[Key, Scored], answers: Mapping[Key, Direction]) -> dict[Key, Scored]:
    """``calls`` that have an answer, scored on it."""
    return {k: _scored(c, answers[k]) for k, c in calls.items() if k in answers}


def _scored(c: Scored, answer: Direction) -> Scored:
    return Scored(c.algorithm, c.model_version, c.symbol, c.bar_close, c.p_up, c.p_flat, c.p_down, answer)


def _span_of(day: date) -> tuple[str, str]:
    start = datetime.combine(day, time(), KST)
    return iso(start), iso(start + timedelta(days=1))


def published(conn: sqlite3.Connection, day: date) -> bool:
    """Whether the engine DB holds a scored final consensus of a stock on ``day``: made, its answer
    of version ``TARGET`` known, not special."""
    marks = ", ".join("?" for _ in STOCKS)
    row = conn.execute(
        "SELECT 1 FROM predictions p JOIN outcomes o ON o.prediction_id = p.id"
        " JOIN touches t ON t.symbol = p.symbol AND t.bar_close = p.bar_close AND t.target_version = ?"
        f" WHERE p.algorithm = ? AND p.symbol IN ({marks}) AND p.bar_close >= ? AND p.bar_close < ?"
        " AND p.status = 'ok' AND o.scored_at IS NOT NULL AND t.class IS NOT NULL AND o.special = 0"
        " LIMIT 1",
        (TARGET, ENGINE, *STOCKS, *_span_of(day)),
    ).fetchone()
    return row is not None


def eligible(data_dir: Path, conn: sqlite3.Connection, calendar: MarketCalendar, day: date) -> bool:
    """Whether ``day`` (a finished day) can be a holdout day: it trades, its stocks' trades were
    recorded and the engine published a scored final consensus on it. Its events are not read."""
    return calendar.session(day) is not None and is_recorded(data_dir, day, STOCKS) and published(conn, day)


def holdout_days(
    data_dir: Path, conn: sqlite3.Connection, calendar: MarketCalendar, now: datetime
) -> list[date]:
    """The first ``DAYS`` eligible days after ``AFTER``, oldest first; fewer while the window fills."""
    found: list[date] = []
    for day in finished_days(data_dir, STOCKS, now):
        if day > AFTER and eligible(data_dir, conn, calendar, day):
            found.append(day)
            if len(found) == DAYS:
                break
    return found


def days_in_holdout(settings: Settings, calendar: MarketCalendar, now: datetime) -> int:
    """How many of the holdout's days are in (``Facts.smt_holdout_days``): 0 without the DB, or
    when it cannot be read (logged; the jobs' other conditions are read all the same)."""
    try:
        with read_only(settings.state_dir / DB_NAME) as conn:
            return 0 if conn is None else len(holdout_days(settings.data_dir, conn, calendar, now))
    except sqlite3.Error:
        log.exception("could not count the SMT experiment's holdout days (#523) in the engine DB")
        return 0


def discovery_days(data_dir: Path, now: datetime) -> tuple[list[date], list[date]]:
    """The finished days up to ``AFTER``: those whose stocks' trades were recorded, and those with
    backfilled bars only."""
    recorded: list[date] = []
    backfilled: list[date] = []
    for day in finished_days(data_dir, STOCKS, now):
        if day <= AFTER:
            (recorded if is_recorded(data_dir, day, STOCKS) else backfilled).append(day)
    return recorded, backfilled


def _backfilled_bars(data_dir: Path, day: date) -> dict[str, list[Bar]]:
    out = {}
    for name in (*STOCKS, FUTURES):
        found = read_bars(data_dir, day, FUTURES_BARS_TR if name == FUTURES else MINUTE_BARS_TR, name)
        if found is not None:
            out[name] = found
    return out


def _co_moves(session: Session, bars: Mapping[str, Sequence[Bar]]) -> dict[Pair, CoMove]:
    return {(a, b): co_move(session, bars.get(a), bars.get(b)) for a, b in PAIRS}


def recorded_day(
    data_dir: Path, conn: sqlite3.Connection | None, calendar: MarketCalendar, day: date
) -> Day | None:
    """A recorded day: its events made again from the recording, its answers and the predictions
    from the engine DB (none without it), the pairs' bars from the backfill. None if it does not
    trade or was not recorded."""
    session = calendar.session(day)
    made = reconstruct(data_dir, day, calendar)
    if session is None or made is None or made.bars != RECORDED:
        return None
    answers = {} if conn is None else read_answers(conn, day)
    engine = {} if conn is None else with_answers(read_calls(conn, day, ENGINE), answers)
    climate = {} if conn is None else with_answers(read_calls(conn, day, CLIMATE), answers)
    co_moves = _co_moves(session, _backfilled_bars(data_dir, day))
    return measure(day, RECORDED, made.events, answers, engine, climate, co_moves)


def backfilled_day(data_dir: Path, calendar: MarketCalendar, day: date, fee_rate: float) -> Day | None:
    """A day with backfilled bars only: its events from them (``smt.day_events``), each stock's
    answers by the one-minute bar rule (``touch.bar_touch``, approximate), no engine prediction.
    None if it does not trade or a stock has no bars."""
    session = calendar.session(day)
    bars = _backfilled_bars(data_dir, day)
    if session is None or any(s not in bars for s in STOCKS):
        return None
    answers = {
        key: label for s in STOCKS for key, label in bar_answers(session, s, bars[s], fee_rate).items()
    }
    events = day_events(calendar, day, bars)
    return measure(day, BACKFILLED, events, answers, {}, {}, _co_moves(session, bars))


def bar_answers(session: Session, symbol: str, bars: Sequence[Bar], fee_rate: float) -> dict[Key, Direction]:
    """``symbol``'s answer at each bar close of ``session``'s prediction window by the one-minute bar
    rule, as training labels a backfilled day: the bar that ended there and those of the four
    minutes after it."""
    starts = [b.start for b in bars]
    by_start = {b.start: b for b in bars}
    out: dict[Key, Direction] = {}
    for t in closes(session):
        closing = by_start.get(t - BAR)
        if closing is None:
            continue
        after = bars[bisect.bisect_left(starts, t) : bisect.bisect_left(starts, t + HORIZON)]
        label = bar_touch(closing, after, fee_rate)
        if label is not None:
            out[(symbol, _instant(t))] = label
    return out


# Writing


TITLE = "SMT 실험 (#519, #523)"


def smt_experiment(settings: Settings, calendar: MarketCalendar, now: datetime, holdout: bool) -> str:
    """The discovery (``holdout`` False) or the holdout's verdict, as text to paste."""
    data = settings.data_dir
    with read_only(settings.state_dir / DB_NAME) as conn:
        has_db = conn is not None
        if holdout:
            days = [] if conn is None else holdout_days(data, conn, calendar, now)
            read = days if len(days) >= DAYS else []  # nothing is read before the window is full
            made = _kept(recorded_day(data, conn, calendar, day) for day in read)
            return "\n".join(holdout_lines(days, made, has_db))
        recorded, backfilled = discovery_days(data, now)
        made = _kept(recorded_day(data, conn, calendar, day) for day in recorded)
    approximate = _kept(backfilled_day(data, calendar, day, settings.fee_rate) for day in backfilled)
    return "\n".join(discovery_lines(made, approximate, has_db))


def _kept(days: Iterable[Day | None]) -> list[Day]:
    return [d for d in days if d is not None]


def registered() -> list[str]:
    """What #523 fixed, as the output's head."""
    pairs = ", ".join(f"{a}-{b}" for a, b in PAIRS)
    return [
        f"정의: {DEFINITION} 판 {VERSION}(smt.py). 쌍 {pairs}. L {'·'.join(map(str, LENGTHS))}분.",
        "사전 등록(#523에 고정, 결과를 보고 바꾸지 않는다):",
        "- 규칙: 사건(묶음의 첫 봉 마감)에서 하락 SMT면 하락, 상승 SMT면 상승을 그 쌍의 주식마다 "
        "확신으로 낸다. 선물은 예측하지 않고, 선물이 넘었어도 주식에 낸다. 다시 맞추지 않는다.",
        f"- 엔진의 예측: 그 종목·봉 마감의 최종 합의({ENGINE}, 상태 ok). 없으면(no_members, "
        "none_finished) 그 봉 마감은 엔진과의 비교에서 빼고 센다(다른 예측으로 대신하지 않는다).",
        f"- 정답: 엔진 DB의 도달 정답(정답 버전 {TARGET}). 채점할 수 없는 봉 마감과 특수 상황이 닿은 봉 "
        "마감은 뺀다. 사건은 녹화를 엔진의 시장 보기로 재생해 다시 계산한다(smt.reconstruct).",
        "- 판정 넷(방향 × L). 판정마다 세 쌍의 (주식, 봉 마감)을 한 번씩 센다(여러 쌍이 같은 봉 마감에 같은 "
        "방향·L의 사건을 내도 하나). 넘은 쪽은 보지 않는다.",
        "- 지표: 같은 (주식, 봉 마감)에서 규칙의 맞힘률 − 엔진의 맞힘률(%p). 거래일마다 평균을 내 "
        f"{LEVEL:.2%} t 구간(판정 넷의 본페로니)으로 본다. 하한 > 0이면 '{STANDS}', 아니면 '{FAILS}'. "
        f"짝이 {MIN_SCORED}개 미만이거나 짝이 있는 거래일이 {MIN_DAYS}일 미만이면 '{TOO_FEW}'이고 "
        "판정하지 않는다.",
        f"- 홀드아웃: {AFTER}(머지한 날) 뒤 자격이 있는 첫 {DAYS}거래일, 고정 창. 자격: 장이 선 날, 두 "
        "주식의 통합 체결을 녹화했고 엔진 DB에 채점된 최종 합의가 있는 날. 사건 수는 보지 않는다.",
        "- 채운 1분봉만 있는 날은 정답이 근사(touch.bar_touch)이고 엔진 예측이 없다. 따로 적고 비교와 판정에 "
        "넣지 않는다.",
        "가격과 시세 값은 싣지 않는다(건수, 비율, %p, 날짜만).",
    ]


def discovery_lines(recorded: Sequence[Day], backfilled: Sequence[Day], has_db: bool) -> list[str]:
    out = [f"{TITLE}: 탐색 ({AFTER}까지)", *registered(), ""]
    out.append(f"녹화일: {_days(recorded)}" + ("" if has_db else " (엔진 DB가 없다: 정답과 예측이 없다)"))
    out.append(f"채운 1분봉만 있는 날: {_days(backfilled)}")
    out += ["", "1. 쌍: 장중 1분 수익률 상관과 함께 있는 시간(채운 1분봉으로, 개장부터 종가 단일가 전까지)"]
    out += _co_move_lines("녹화일", recorded) + _co_move_lines("채운 1분봉만 있는 날", backfilled)
    out += ["", "2. 사건: 쌍 방향 L 넘은 쪽: 사건 수 · 사건이 있은 날"]
    out += _event_lines("녹화일", recorded) + _event_lines("채운 1분봉만 있는 날", backfilled)
    out += ["", "3. 사건 뒤 정답의 상승·보합·하락(판정마다 정답이 있는 (주식, 봉 마감))"]
    out += _share_lines("녹화일", recorded) + _share_lines("채운 1분봉만 있는 날(근사 정답)", backfilled)
    out += ["", f"4. 주 지표: 규칙 − 엔진({ENGINE}), 녹화일. 탐색이라 판정하지 않는다"]
    out += [claim_line(measured(recorded, c), judged=False) for c in CLAIMS]
    out += ["", f"5. 기후 비교: 규칙 − {CLIMATE}, 녹화일. 판정하지 않는다"]
    out += [climate_line(measured(recorded, c)) for c in CLAIMS]
    return out


def holdout_lines(days: Sequence[date], made: Sequence[Day], has_db: bool) -> list[str]:
    span = f"{days[0]} – {days[-1]}, " if days else ""
    out = [f"{TITLE}: 판정 (사전 등록 홀드아웃)", *registered(), ""]
    if not has_db:
        return [*out, "엔진 DB가 없다: 판정하지 않는다."]
    out.append(f"창: {AFTER} 뒤 자격이 있는 첫 {DAYS}거래일. 지금 {span}{len(days)}/{DAYS}일.")
    if len(days) < DAYS:
        return [*out, f"창이 아직 차지 않았다. 판정하지 않는다({DAYS}일이 차면 판정한다)."]
    claims = [measured(made, c) for c in CLAIMS]
    out += ["", f"판정 넷: 규칙 − 엔진({ENGINE})", *[claim_line(m, judged=True) for m in claims]]
    verdicts = [(m.claim, m.against_engine().verdict) for m in claims]
    by_verdict = {v: [_claim(c) for c, found in verdicts if found == v] for v in (STANDS, FAILS, TOO_FEW)}
    out += ["", " · ".join(f"{v}: {', '.join(names) or '없음'}" for v, names in by_verdict.items())]
    out += ["", "곁 지표(판정하지 않는다)", "1. 쌍: 장중 1분 수익률 상관과 함께 있는 시간(채운 1분봉으로)"]
    out += _co_move_lines("창", made)
    out += ["2. 사건: 쌍 방향 L 넘은 쪽: 사건 수 · 사건이 있은 날", *_event_lines("창", made)]
    out += ["3. 사건 뒤 정답의 상승·보합·하락", *_share_lines("창", made)]
    out += [f"4. 기후 비교: 규칙 − {CLIMATE}", *[climate_line(m) for m in claims]]
    return out


def _claim(claim: Claim) -> str:
    direction, minutes = claim
    return f"{NAMES[direction]} {minutes}"


def _days(days: Sequence[Day]) -> str:
    if not days:
        return "없다"
    return f"{days[0].day} – {days[-1].day}, {len(days)}일"


def _co_move_lines(label: str, days: Sequence[Day]) -> list[str]:
    out = [f"  {label}({len(days)}일):"]
    for pair in PAIRS:
        total = sum((d.co_moves.get(pair, CoMove()) for d in days), CoMove())
        a, b = pair
        corr = total.correlation
        head = f"    {a}-{b}: 상관 {'—' if corr is None else f'{corr:+.2f}'} (수익률 {total.n:,}개)"
        if not total.minutes:
            out.append(f"{head} · 장 시간 없음")
            continue
        out.append(
            f"{head} · 함께 있음 {_ratio(total.both / total.minutes)}"
            f" ({a} {_ratio(total.a / total.minutes)} · {b} {_ratio(total.b / total.minutes)},"
            f" 장 시간 {total.minutes:,}분)"
        )
    return out


def _event_lines(label: str, days: Sequence[Day]) -> list[str]:
    counts: Counter[tuple[str, str, str, int, str]] = Counter()
    active: dict[tuple[str, str, str, int, str], set[date]] = {}
    for d in days:
        for e in d.events:
            key = (e.a, e.b, e.direction, e.minutes, e.swept)
            counts[key] += 1
            active.setdefault(key, set()).add(kst_day(e.bar_close))
    out = [f"  {label}({len(days)}일): 합계 {sum(counts.values()):,}"]
    for a, b in PAIRS:
        for direction in DIRECTIONS:
            for minutes in LENGTHS:
                for swept in (a, b):
                    key = (a, b, direction, minutes, swept)
                    out.append(
                        f"    {a}-{b} {direction} {minutes} {swept}: {counts[key]:,} · "
                        f"{len(active.get(key, ())):,}일"
                    )
    return out


def _shares(counts: Counter[Direction]) -> str:
    n = sum(counts.values())
    return (
        " · ".join(f"{CLASS_NAMES[c]} {_ratio(counts[c] / n if n else None)}" for c in CLASSES) + f" ({n:,})"
    )


def _share_lines(label: str, days: Sequence[Day]) -> list[str]:
    base: Counter[Direction] = Counter()
    for d in days:
        base += d.base
    out = [f"  {label}: 모든 봉 마감 {_shares(base)}"]
    for claim in CLAIMS:
        out.append(f"    {_claim(claim)}: {_shares(measured(days, claim).shares)}")
        for pair in PAIRS:
            out.append(f"      {pair[0]}-{pair[1]}: {_shares(measured(days, claim, pair).shares)}")
    return out


def claim_line(m: Measured, judged: bool) -> str:
    """A claim's rule against the engine: its pairs, days, both hit rates, the difference and its
    intervals, and with ``judged`` the verdict."""
    j = m.against_engine()
    head = f"  {_claim(m.claim)}: 짝 {j.lift.n:,} (엔진 예측 없음 {m.unpublished:,}) · {j.days}일"
    if judged and j.verdict == TOO_FEW:
        return f"{head} · {TOO_FEW}"
    line = (
        f"{head} · 규칙 {_ratio(j.accuracy)} · 엔진 {_ratio(j.baseline)} · 차이 {points_text(j.lift.value)}"
        f" · 짝 구간 {_span(j.lift)} · 거래일 구간 {_span(j.by_day)}"
    )
    return f"{line} · {j.verdict}" if judged else line


def climate_line(m: Measured) -> str:
    j = m.against_climate()
    return (
        f"  {_claim(m.claim)}: 짝 {j.lift.n:,} · {j.days}일 · 규칙 {_ratio(j.accuracy)} · 기후 "
        f"{_ratio(j.baseline)} · 차이 {points_text(j.lift.value)} · 거래일 구간 {_span(j.by_day)}"
    )
