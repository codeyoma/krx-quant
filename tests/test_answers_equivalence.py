"""The answers made faster (#630) are the answers as they were, to the bit.

Input: made-up recorded days of two symbols, the book and the integrated trades at random moments
all session, with crossed, locked and one-sided quotes, quotes received at the same moment, REST
book snapshots, silences of more than a minute, a VI, a circuit breaker and a sidecar, a recording
cut short, a day that opens and closes an hour late (an exam day) and a first trading day of the year
(an hour late open). Output: the same day made by the code before #630 (``tests/answers_oracle.py``)
and by today's: every sample (its features, touch terms and label), every kind's answer, the bar
closes left out; and, followed by a Market View the way the engine follows it (events a little out of
order among them), every touch window's label and path, and every kind's answer of it, at every
minute.
"""

from __future__ import annotations

import random
from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import pytest

from krx_quant.algorithms import ALGORITHMS, KIND_ALGORITHMS
from krx_quant.barriers import barrier_answer
from krx_quant.engine import Event
from krx_quant.features import Leaders, Width
from krx_quant.feeds import BOOK_SNAPSHOT, KRX_TRADE
from krx_quant.feeds.base import KST
from krx_quant.kind_registry import KINDS
from krx_quant.kinds import Followed, Kind, Quote, went_silent
from krx_quant.market_calendar import MarketCalendar
from krx_quant.market_view import MarketView, prediction_window
from krx_quant.sample_store import fingerprint, night_reads
from krx_quant.touch import TouchResult
from krx_quant.training import TrainingDay, recorded_day
from tests import answers_oracle as oracle
from tests.answers_oracle import OracleView, before_630, oracle_kinds
from tests.replay_events import book, kospi, kospi200_futures, trade, vi

SYMBOLS = ("005930", "000660")
LEADERS = Leaders()
FEE = 0.015 / 100
NAMES = ("spread_ticks", "return_1", "range_20")
ANSWERED = night_reads(ALGORITHMS, KIND_ALGORITHMS, KINDS).kinds  # the range and the barriers
REGULAR = date(2026, 10, 13)
EXAM = date(2026, 11, 19)  # opens 10:00, closes 16:30
NEW_YEAR = date(2026, 1, 2)  # the year's first trading day: opens 10:00
CUT = date(2026, 10, 14)  # the recording stops at 11:20
CALENDAR = MarketCalendar(dict.fromkeys((REGULAR, EXAM, NEW_YEAR, CUT), True))


def _at(day: date, hhmmss: str) -> datetime:
    return datetime.combine(day, time.fromisoformat(hhmmss), KST)


def _quotes(
    day: date, symbol: str, mid: int, tick: int, start: datetime, end: datetime, seed: int
) -> Iterator[Event]:
    """A symbol's book and integrated trades from ``start`` to ``end``: a random walk of the mid-point
    that now and then jumps several ticks, a quote every 0–3 s, some at the same moment as the one
    before; crossed, locked and one-sided quotes, KRX-only trades, and silences of 70 s, 95 s and
    exactly 60 s (the shortest that leaves a window unscorable) from the quote before them."""
    rng = random.Random(seed)
    silences = [
        (start + timedelta(minutes=m), timedelta(seconds=s)) for m, s in ((37, 70), (98, 60), (151, 95))
    ]
    when = last = start
    while when < end:
        for quiet, length in silences:
            if quiet <= when < quiet + length:
                when = last + length  # the quote before the silence was the last for ``length``
        last = when
        r = rng.random()
        mid += tick * (rng.choice((-1, 1)) * (4 if r < 0.01 else 1) if r < 0.25 else 0)
        bid, ask = mid - tick, mid + tick
        shape = rng.random()
        if shape < 0.02:
            bid, ask = ask, bid  # crossed: the two venues' best quotes the other way round
        elif shape < 0.03:
            bid = ask  # locked
        elif shape < 0.04:
            bid = 0  # one-sided
        if rng.random() < 0.55:
            yield book(when, bid, ask, symbol, bid_qty=rng.randint(1, 500), ask_qty=rng.randint(1, 500))
        else:
            side = rng.choice(("buy", "sell"))
            price = ask if side == "buy" else bid
            yield trade(when, price or mid, rng.randint(1, 30), bid=bid, ask=ask, symbol=symbol, side=side)
        if rng.random() < 0.05:
            yield trade(when, mid, 1, bid=mid - tick, ask=mid + tick, feed=KRX_TRADE, symbol=symbol)
        if rng.random() < 0.97:  # else a second quote at the same moment
            when += timedelta(seconds=rng.random() * 3)


def _snapshot(when: datetime, symbol: str, bid: int, ask: int) -> Event:
    """A REST snapshot of the book (the fallback when the WebSocket is refused)."""
    _, row = book(when, bid, ask, symbol)
    return (BOOK_SNAPSHOT.tr_id, row)


def made_up_day(day: date, *, until: str | None = None, special: bool = False, seed: int = 0) -> list[Event]:
    """``day``'s events in receipt order: both symbols' quotes over the session (to ``until``), a REST
    snapshot each, a VI of the first; with ``special`` a circuit breaker and a sidecar too."""
    session = CALENDAR.session(day)
    assert session is not None
    start = session.open - timedelta(minutes=1)
    # The integrated book goes on after the close (NXT's after-market): no bar close is followed then.
    end = _at(day, until) if until else session.close + timedelta(minutes=15)
    events = [
        *_quotes(day, SYMBOLS[0], 81_200, 100, start, end, seed),
        *_quotes(day, SYMBOLS[1], 190_000, 100, start + timedelta(seconds=0.5), end, seed + 1),
        _snapshot(session.open + timedelta(minutes=95, seconds=12), SYMBOLS[0], 81_100, 81_300),
        _snapshot(session.open + timedelta(minutes=212, seconds=3), SYMBOLS[1], 189_900, 190_100),
    ]
    vi_at = session.open + timedelta(minutes=44)
    hhmmss = vi_at.strftime("%H:%M:%S")
    released = (vi_at + timedelta(minutes=2, seconds=11)).strftime("%H:%M:%S")
    events.append(vi(vi_at + timedelta(seconds=4), hhmmss, symbol=SYMBOLS[0]))
    events.append(vi(vi_at + timedelta(minutes=2, seconds=20), hhmmss, released, symbol=SYMBOLS[0]))
    if special:
        breaker = session.open + timedelta(minutes=70)
        events += [kospi(breaker + timedelta(seconds=s), -8.4) for s in range(0, 90, 5)]
        events += [kospi(breaker + timedelta(minutes=40, seconds=s), -3.0) for s in range(0, 30, 5)]
        sidecar = session.open + timedelta(minutes=180)
        events += [kospi200_futures(sidecar + timedelta(seconds=s), -5.3) for s in range(0, 80, 4)]
    return sorted((e for e in events if e[1]["received_at"] < end), key=lambda e: e[1]["received_at"])


DAYS: dict[str, tuple[date, dict[str, Any]]] = {
    "regular": (REGULAR, {"special": True, "seed": 1}),
    "exam day": (EXAM, {"seed": 2}),
    "first of the year": (NEW_YEAR, {"seed": 3}),
    "cut short": (CUT, {"until": "11:20:00", "seed": 4}),
}


def _made(events: list[Event], day: date, *, answered: Sequence[Kind] = ANSWERED) -> str:
    made, answers, left_out = recorded_day(
        events,
        CALENDAR,
        day,
        SYMBOLS,
        NAMES,
        FEE,
        LEADERS,
        kinds=answered,
    )
    return fingerprint(TrainingDay("mid", made, answers, left_out))


def _same(after: str, before: str) -> None:
    """``after`` is ``before``; else where they part, briefly (a diff of the whole day takes long)."""
    if after != before:
        at = next(i for i, (a, b) in enumerate(zip(after, before, strict=False)) if a != b)
        pytest.fail(
            f"they part at {at}: {before[at - 300 : at + 300]!r} became {after[at - 300 : at + 300]!r}"
        )


@pytest.mark.parametrize("name", DAYS)
def test_a_recorded_day_is_made_as_before(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    day, options = DAYS[name]
    events = made_up_day(day, **options)

    with before_630(monkeypatch):
        before = _made(events, day, answered=oracle_kinds(ANSWERED))
    after = _made(events, day)

    _same(after, before)
    # The day has what the comparison is meant to cover.
    assert "'up'" in after and "'down'" in after and "'flat'" in after
    for word in ("up_first", "down_first", "timeout", "silent quotes", "rest quotes"):
        assert word in after, word


def _jostled(events: list[Event], seed: int) -> list[Event]:
    """``events`` with a few neighbours swapped: received out of order, as live they can be."""
    rng = random.Random(seed)
    out = list(events)
    for i in range(len(out) - 1):
        if rng.random() < 0.02:
            out[i], out[i + 1] = out[i + 1], out[i]
    return out


def _followed(view: MarketView, events: list[Event], day: date, answered: Sequence[Kind]) -> list[Any]:
    """What ``view`` says, given ``events``, minute by minute, as the engine asks it: each bar close's
    bars, every window's touch label and path as followed so far and the quote it starts from, and
    each kind's answer of the window that has just ended at its horizon."""
    said: list[Any] = []
    minute = _at(day, "08:59:00")

    session = CALENDAR.session(day)
    assert session is not None
    first, last = prediction_window(session)

    def ask(at: datetime) -> None:
        if first <= at <= last:  # a bar close the engine predicts
            view.at_close(at)
        for symbol in SYMBOLS:
            log = view._logs[symbol]  # what the kinds' windows will read
            said.append((sorted(view._windows[symbol]), list(log._quotes), log._rests, log._trades))
            for close in view._windows[symbol]:
                said.append((view.touch(symbol, close), view.quote_at(symbol, close)))
            for kind in answered:
                if at - kind.horizon in view._windows[symbol]:
                    said.append((kind.name, view.kind_answer(kind, symbol, at - kind.horizon)))

    for feed, row in events:
        while minute + timedelta(minutes=1) <= row["received_at"]:
            minute += timedelta(minutes=1)
            ask(minute)
        view.on_event(feed, row)
    ask(minute + timedelta(minutes=1))
    return said


def test_the_engine_s_windows_are_followed_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    day, options = DAYS["regular"]
    # Through the VI, the circuit breaker, the sidecar and a REST snapshot: the whole day would only
    # take longer. The day's end too: quotes after the last bar close.
    events = [
        e
        for e in _jostled(made_up_day(day, **options), seed=5)
        if e[1]["received_at"] < _at(day, "12:40:00") or e[1]["received_at"] >= _at(day, "15:00:00")
    ]
    start = datetime.combine(day, time(), KST)

    with before_630(monkeypatch):
        old = oracle_kinds(ANSWERED)
        before = _followed(OracleView(CALENDAR, SYMBOLS, LEADERS, start, FEE, kinds=old), events, day, old)
    after = _followed(
        MarketView(CALENDAR, SYMBOLS, LEADERS, start, FEE, kinds=ANSWERED), events, day, ANSWERED
    )

    assert len(after) == len(before)
    for said, was in zip(after, before, strict=True):
        assert said == was
    assert sum(isinstance(s, tuple) and isinstance(s[0], str) for s in after) > 1_000  # kinds' answers
    assert sum(isinstance(s, tuple) and isinstance(s[0], TouchResult) for s in after) > 10_000


def _window(rng: random.Random, start: datetime, ordered: bool) -> Followed:
    """A barrier window of made-up quotes: received in order or not, some at the same moment, some
    crossed, a minute's silence now and then."""
    when, mid, quotes = start, 50_000.0, []
    for _ in range(rng.randint(0, 120)):
        when += timedelta(seconds=rng.choice((0, 0.5, 1, 2, 5, 60, 61)) if rng.random() < 0.9 else 0)
        mid += 50 * rng.choice((-1, 0, 0, 1))
        bid, ask = (mid - 50, mid + 50) if rng.random() > 0.05 else (mid + 50, mid - 50)
        quotes.append(Quote(when, bid, ask, None, None))
    if not ordered:
        rng.shuffle(quotes)
    width = Width(rng.randint(1, 6), 1.5) if rng.random() > 0.05 else None
    return Followed(
        "005930", start, 50_000.0, 50.0, 2, 49_950.0, 50_050.0, tuple(quotes), (), None, 1.2, width
    )


def test_a_barrier_answer_and_a_silence_are_as_before() -> None:
    """Thousands of made-up windows, received in order and out of it: the barrier answer (which
    barrier first, ties, the path) and the silence test are as before."""
    rng = random.Random(630)
    start = _at(REGULAR, "10:00:00")
    for i in range(4_000):
        window = _window(rng, start, ordered=i % 3 != 0)
        assert barrier_answer(window) == oracle.barrier_answer(window)
        received = [q.received_at for q in window.quotes]
        assert went_silent(start, window.end, received) == oracle.went_silent(start, window.end, received)


def test_the_made_up_days_hold_what_they_should() -> None:
    """The days are what the module doc says: received times in UTC, ties, a cut recording."""
    events = made_up_day(CUT, until="11:20:00", seed=4)
    times = [row["received_at"] for _, row in events]
    assert all(t.tzinfo is UTC for t in times)
    assert len(times) - len(set(times)) > 10  # quotes received at the same moment
    assert max(times) < _at(CUT, "11:20:00")
