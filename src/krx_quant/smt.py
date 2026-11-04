"""SMT divergence (#519, #520): the rolling-extreme SMT of three pairs, at each bar close.

SMT (ICT's Smart Money Technique) reads two instruments that move together: when one goes past
its recent high (low) and the other does not, the move is taken as a sign of a turn. This module
judges it by one definition, its name and version kept with every event (``DEFINITION``,
``VERSION``): the rolling-extreme SMT, unlike the classical one that compares swing highs and
lows (confirmed some minutes late). Version 1's rules:

- **Pairs and L.** 005930–000660, 005930–KOSPI200 futures, 000660–KOSPI200 futures (``PAIRS``,
  the futures as the Market View's continuous series ``KOSPI200F``); L is 15 and 30 minutes
  (``LENGTHS``). Either instrument of a pair can be the one that sweeps. They are constants.
- **Bars.** One-minute bars by exchange time, closed and as they stood at the bar close: live,
  the engine's Market View's bars of the bar close (``market_view.BarsAtClose``); on a recorded
  day the same view over the day's recorded trades (``replayed``); on another day the backfilled
  bars (``day_events``). At bar close t the current bar is the minute before t; its look-back is
  the L bars before that one.
- **Bearish SMT.** One instrument's current high is above the highest high of its look-back
  (it swept) while the other's current high is at or below its own (it did not). **Bullish
  SMT:** the same with lows, below the lowest low.
- **Ties.** A price equal to the look-back's extreme is not past it: the one that sweeps must go
  strictly beyond, and the other at its extreme has not swept.
- **Both directions in one bar.** A bar close with a bearish and a bullish SMT of the same pair
  and L (an outside bar of one alone, or one sweeping its high while the other sweeps its low)
  gives no verdict for either direction: no event, and it neither starts nor ends an episode.
- **Unknown.** A pair has no verdict at a bar close (unknown, never "not swept") when any of the
  L + 1 bars of either instrument is missing there: the minute had no trade, or its trades were
  received after the bar close (late), so the bar was not in the bars of that close.
- **The first L minutes.** No verdict at a bar close whose look-back would begin before the
  session's open: the first is at open + L + 1 minutes (09:16 for L = 15, 09:31 for L = 30 on a
  normal day). The session comes from the market calendar (a late-opening day starts later).
  The bar closes judged are the engine's: the prediction window (``prediction_window``, 09:01 to
  15:16 on a normal day).
- **Episodes.** Consecutive verdicts with an SMT of the same pair, direction and L are one
  episode, and one event: its first bar close, with that bar's swept side (a change of the side
  that sweeps does not start another). **Rearming:** a known verdict without that SMT ends the
  episode, and the next SMT starts a new one. An unknown or conflicting bar close leaves an
  episode as it is.
- **The extremes (#521).** An event keeps, for each instrument, the start of the look-back bar
  holding the extreme it was judged against at the first bar close: the highest high (bearish)
  or the lowest low (bullish), the latest of equal ones. They read only the look-back's bars.
- **Sessions.** Each session starts afresh: no episode carries over from the day before.

Live, the engine judges each bar close it predicts (``Detector``), and keeps the events in its
database (``smt_events``, source ``LIVE``), never rewritten. A failure is logged and never holds up
the prediction. A restarted engine starts afresh too: its view has no bars from before the restart,
so the windows reaching back before it are unknown, and an episode going on then starts again as a
new event. A day's events made again from its bars (``reconstruct``) are ``RECONSTRUCTED``.
"""

from __future__ import annotations

import bisect
from collections import Counter, deque
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from krx_quant.contracts import KOSPI200
from krx_quant.features import Leaders
from krx_quant.feeds import KOSPI200_FUTURES, TRADE
from krx_quant.feeds.base import KST
from krx_quant.market_calendar import MarketCalendar, Session
from krx_quant.market_view import MarketView, prediction_window
from krx_quant.reference import BAR, Bar

DEFINITION = "rolling_extreme"
VERSION = 1
FUTURES = KOSPI200.series
PAIRS: tuple[tuple[str, str], ...] = (("005930", "000660"), ("005930", FUTURES), ("000660", FUTURES))
STOCKS = tuple(dict.fromkeys(s for pair in PAIRS for s in pair if s != FUTURES))
LENGTHS = (15, 30)  # L, minutes of look-back
BEARISH, BULLISH = "bearish", "bullish"
DIRECTIONS = (BEARISH, BULLISH)
# Where an event comes from: the engine at the bar close, or a day's bars made again.
LIVE, RECONSTRUCTED = "live", "reconstructed"
# What a reconstruction was made from: the day's recording, or its backfilled bars.
RECORDED, BACKFILLED = "recorded", "backfilled"

Event = tuple[str, dict[str, Any]]  # (feed TR ID, normalized row), as the engine takes them


@dataclass(frozen=True)
class SmtEvent:
    """An SMT episode, by its first bar close."""

    bar_close: datetime  # t: the bar is the minute before it
    a: str  # the pair, as ``PAIRS`` lists it
    b: str
    direction: str  # BEARISH (a high swept by one alone) or BULLISH (a low)
    minutes: int  # L
    swept: str  # a or b: the one past its look-back's extreme, at the first bar close
    source: str  # LIVE or RECONSTRUCTED
    definition: str = DEFINITION
    version: int = VERSION
    # The start of the look-back bar holding a's (b's) extreme at the first bar close (#521): its
    # highest high (BEARISH) or lowest low (BULLISH), the latest of equal ones. None: an event
    # kept before #521.
    a_extreme: datetime | None = None
    b_extreme: datetime | None = None


class Detector:
    """Judges the bar closes of the sessions given to it, in time order, from the bars as they
    stood at each (by instrument: ``PAIRS``' names); the events of episodes that begin there.
    Causal: it reads only the bars of the minutes before the bar close."""

    def __init__(self, calendar: MarketCalendar, source: str) -> None:
        self._calendar = calendar
        self._source = source
        self._day: date | None = None
        self._session: Session | None = None
        self._going_on: set[tuple[str, str, str, int]] = set()  # (a, b, direction, L)

    def at_close(self, close: datetime, bars: Mapping[str, Sequence[Bar]]) -> list[SmtEvent]:
        day = close.astimezone(KST).date()
        if day != self._day:  # a new session starts afresh
            self._day, self._session = day, self._calendar.session(day)
            self._going_on.clear()
        if self._session is None or close > self._session.closing_auction:
            return []
        found: list[SmtEvent] = []
        for a, b in PAIRS:
            for minutes in LENGTHS:
                if close - (minutes + 1) * BAR < self._session.open:
                    continue  # its look-back would begin before the open
                verdict = _verdict(a, b, bars, close, minutes)
                if verdict is None:
                    continue  # unknown or conflicting: episodes stay as they are
                for direction in DIRECTIONS:
                    key, swept = (a, b, direction, minutes), verdict[direction]
                    if swept is None:
                        self._going_on.discard(key)  # rearmed
                    elif key not in self._going_on:
                        self._going_on.add(key)
                        found.append(
                            SmtEvent(
                                close,
                                a,
                                b,
                                direction,
                                minutes,
                                swept,
                                self._source,
                                a_extreme=_extreme(bars.get(a), close, minutes, direction),
                                b_extreme=_extreme(bars.get(b), close, minutes, direction),
                            )
                        )
        return found


def _window(bars: Sequence[Bar] | None, close: datetime, minutes: int) -> Sequence[Bar] | None:
    """The look-back's ``minutes`` bars and the current one before ``close``, oldest first; None
    if one of them is missing. ``bars`` are oldest first."""
    if not bars:
        return None
    first = close - (minutes + 1) * BAR
    i = bisect.bisect_left(bars, first, key=lambda bar: bar.start)
    window = bars[i : i + minutes + 1]
    if len(window) < minutes + 1 or any(bar.start != first + k * BAR for k, bar in enumerate(window)):
        return None
    return window


def _verdict(
    a: str, b: str, bars: Mapping[str, Sequence[Bar]], close: datetime, minutes: int
) -> dict[str, str | None] | None:
    """The pair's verdict at ``close`` for L = ``minutes``: the swept side of each direction's SMT
    (None: no SMT); None for no verdict (a bar missing, or both directions at once)."""
    window_a, window_b = _window(bars.get(a), close, minutes), _window(bars.get(b), close, minutes)
    if window_a is None or window_b is None:
        return None
    high_a, low_a = _past(window_a)
    high_b, low_b = _past(window_b)
    verdict = {BEARISH: _alone(a, high_a, b, high_b), BULLISH: _alone(a, low_a, b, low_b)}
    if verdict[BEARISH] is not None and verdict[BULLISH] is not None:
        return None
    return verdict


def _past(window: Sequence[Bar]) -> tuple[bool, bool]:
    """Whether the current bar's high is above the look-back's highest, and its low below its
    lowest (equal is not past)."""
    *before, now = window
    return now.high > max(bar.high for bar in before), now.low < min(bar.low for bar in before)


def _extreme(bars: Sequence[Bar] | None, close: datetime, minutes: int, direction: str) -> datetime | None:
    """The start of the look-back bar before ``close`` holding its highest high (``BEARISH``) or
    lowest low (``BULLISH``), the latest of equal ones; None if a bar of the window is missing."""
    window = _window(bars, close, minutes)
    if window is None:
        return None
    *before, _ = window
    if direction == BEARISH:
        highest = max(bar.high for bar in before)
        return next(bar.start for bar in reversed(before) if bar.high == highest)
    lowest = min(bar.low for bar in before)
    return next(bar.start for bar in reversed(before) if bar.low == lowest)


def _alone(a: str, a_past: bool, b: str, b_past: bool) -> str | None:
    """The one that went past alone, if one did."""
    if a_past != b_past:
        return a if a_past else b
    return None


def closes(session: Session) -> Iterator[datetime]:
    """The bar closes judged in ``session``: the engine's, its prediction window."""
    first, last = prediction_window(session)
    while first <= last:
        yield first
        first += BAR


def day_events(
    calendar: MarketCalendar, day: date, bars: Mapping[str, Sequence[Bar]], source: str = RECONSTRUCTED
) -> list[SmtEvent]:
    """``day``'s SMT events from its finished one-minute bars by instrument, oldest first (the
    backfilled bars): at each bar close, those of the minutes before it."""
    session = calendar.session(day)
    if session is None:
        return []
    detector = Detector(calendar, source)
    return [event for close in closes(session) for event in detector.at_close(close, bars)]


def replayed(
    calendar: MarketCalendar, day: date, events: Iterable[Event], source: str = RECONSTRUCTED
) -> list[SmtEvent]:
    """``day``'s SMT events from its events, in receipt order, through the engine's Market View:
    at each bar close, the bars as they stood there, before any event received at or after it, as
    the engine judges them live."""
    session = calendar.session(day)
    if session is None:
        return []
    view = MarketView(calendar, STOCKS, Leaders(), datetime.combine(day, time(), KST))
    detector = Detector(calendar, source)
    due = deque(closes(session))
    found: list[SmtEvent] = []

    def reach(moment: datetime) -> None:
        while due and due[0] <= moment:
            frozen = view.at_close(due.popleft())
            found.extend(detector.at_close(frozen.close, {**frozen.leaders, **frozen.symbols}))

    for feed, row in events:
        reach(row["received_at"])
        view.on_event(feed, row)
    reach(session.closing_auction)
    return found


@dataclass(frozen=True)
class Reconstruction:
    """A day's SMT events made again, and what from: ``RECORDED`` or ``BACKFILLED``."""

    day: date
    bars: str
    events: list[SmtEvent]


def reconstruct(
    data_dir: Path, day: date, calendar: MarketCalendar, first: str = RECORDED
) -> Reconstruction | None:
    """``day``'s SMT events made again (``RECONSTRUCTED``): from its recording if every stock's
    trades were recorded (as the engine saw them), else from its backfilled bars if every stock
    has them; None otherwise. ``first=BACKFILLED`` tries the backfilled bars first (the Engine
    API, #521: they are the bars a chart shows of a past day, and are read in milliseconds where
    a day's recording takes seconds to replay). A pair whose instrument has neither is unknown
    all day."""
    ways = {RECORDED: _recorded, BACKFILLED: _backfilled}
    for bars in (first, *(b for b in ways if b != first)):
        made = ways[bars](data_dir, day, calendar)
        if made is not None:
            return made
    return None


def _recorded(data_dir: Path, day: date, calendar: MarketCalendar) -> Reconstruction | None:
    """``day`` made again from its recording, if every stock's trades were recorded."""
    from krx_quant.replay import recorded_events  # they read the recording
    from krx_quant.training import is_recorded

    if not is_recorded(data_dir, day, STOCKS):
        return None
    feeds = {TRADE.tr_id, KOSPI200_FUTURES.tr_id}  # the bars' trades
    events = recorded_events(data_dir, day, STOCKS, Leaders(), feeds=feeds)
    return Reconstruction(day, RECORDED, replayed(calendar, day, events))


def _backfilled(data_dir: Path, day: date, calendar: MarketCalendar) -> Reconstruction | None:
    """``day`` made again from its backfilled bars, if every stock has them."""
    from krx_quant.backfill import FUTURES_BARS_TR, MINUTE_BARS_TR, read_bars

    bars = {s: read_bars(data_dir, day, MINUTE_BARS_TR, s) for s in STOCKS}
    if any(found is None for found in bars.values()):
        return None
    every = {name: found for name, found in bars.items() if found is not None}
    futures = read_bars(data_dir, day, FUTURES_BARS_TR, FUTURES)
    if futures is not None:
        every[FUTURES] = futures
    return Reconstruction(day, BACKFILLED, day_events(calendar, day, every))


def _key(event: SmtEvent) -> tuple[datetime, str, str, str, int, str, str, int]:
    """An event without its source: what a live one and one made again agree on."""
    return (
        event.bar_close,
        event.a,
        event.b,
        event.direction,
        event.minutes,
        event.swept,
        event.definition,
        event.version,
    )


def lines(day: date, made: Reconstruction | None, live: Sequence[SmtEvent]) -> list[str]:
    """``krx-quant smt``'s report of ``day``: the events by pair, direction, L, swept side and
    source, and, with live events, whether the day made again agrees with them. Counts and
    times only, no price."""
    live = [e for e in live if (e.definition, e.version) == (DEFINITION, VERSION)]
    again = made.events if made is not None else []
    out = [f"SMT {day}: 정의 {DEFINITION} 판 {VERSION}, L {'·'.join(map(str, LENGTHS))}분"]
    if made is None:
        out.append("다시 계산: 녹화도 채운 1분봉도 없다")
    else:
        out.append(f"다시 계산: {'녹화(엔진의 시장 보기로 재생)' if made.bars == RECORDED else '채운 1분봉'}")
    counts = Counter((e.a, e.b, e.direction, e.minutes, e.swept, e.source) for e in [*live, *again])
    out.append("쌍 방향 L 넘은쪽: live, reconstructed")
    for a, b in PAIRS:
        for direction in DIRECTIONS:
            for minutes in LENGTHS:
                for swept in (a, b):
                    n_live = counts[(a, b, direction, minutes, swept, LIVE)]
                    n_again = counts[(a, b, direction, minutes, swept, RECONSTRUCTED)]
                    if n_live or n_again:
                        out.append(f"  {a}-{b} {direction} {minutes} {swept}: {n_live}, {n_again}")
    out.append(f"합계: live {len(live)}, reconstructed {len(again)}")
    if not live:
        out.append("live와 견주기: 그날 live 사건이 없다")
        return out
    if made is None:
        out.append("live와 견주기: 다시 계산하지 못했다")
        return out
    live_keys, again_keys = {_key(e) for e in live}, {_key(e) for e in again}
    only_live, only_again = sorted(live_keys - again_keys), sorted(again_keys - live_keys)
    if not only_live and not only_again:
        out.append(f"live와 견주기: 같다 ({len(live_keys)}건)")
        return out
    out.append(
        f"live와 견주기: 다르다. 같음 {len(live_keys & again_keys)}, live에만 {len(only_live)},"
        f" 다시 계산에만 {len(only_again)}"
    )
    for label, keys in (("live에만", only_live), ("다시 계산에만", only_again)):
        for close, a, b, direction, minutes, swept, _, _ in keys:
            out.append(f"  {label}: {close.astimezone(KST):%H:%M} {a}-{b} {direction} {minutes} {swept}")
    return out
