"""The market-wide halt (#625): a circuit breaker confirmed from the integrated trades and book,
its reopening auction, and its end at the real reopening.

Input: hand-made events like 2026-11-06's (#625's probe): the KOSPI down past 8% from 09:02:30,
the last trades flagged halted at 09:03:32, nothing for 20 minutes, the book back in the
reopening auction (``HOUR_CLS_CODE`` 'C') at 09:23:33, the trades with it at 09:33:38, and
continuous trading ('0') from 09:34:10. Output: the windows the engine keeps (and a recording's
fill decides), the predictions and answers it marks special, and ``special.SpecialSituations``'
windows for the cases around it.
"""

from __future__ import annotations

import asyncio
import random
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from krx_quant.engine import Event
from krx_quant.feeds import BOOK, TRADE
from krx_quant.rescore import fill_special_windows
from krx_quant.special import Kind, SpecialSituations, Window
from tests.api_schema import check
from tests.replay_doubles import NEXT_MORNING, record
from tests.replay_events import CALENDAR, SYMBOL, api, at, book, every, get_json, kospi, run, trade
from tests.test_rescore import settings
from tests.test_special_windows import before_236

OTHER = "000660"
END_OF_DAY = at("23:59:59.999999")


def coded(event: Event, code: str, *, halted: bool = False) -> Event:
    """``event`` (an integrated trade or book) with the time code ``code`` (``HOUR_CLS_CODE``) and,
    for a trade, the halt flag (``TRHT_YN``)."""
    feed, row = event
    row = {**row, "hour_cls": code}
    if feed == TRADE.tr_id:
        row["halted"] = halted
    return (feed, row)


def quotes(start: str, end: str, code: str, symbol: str = SYMBOL, *, trades: bool = True) -> list[Event]:
    """A book every 5 seconds from ``start`` to ``end`` and, if ``trades``, a trade a second after each,
    all with the time code ``code``."""

    made: list[Event] = []
    when = at(start)
    while when <= at(end):
        made.append(coded(book(when, 81_200, 81_300, symbol), code))
        if trades:
            made.append(coded(trade(when + timedelta(seconds=1), 81_300, symbol=symbol), code))
        when += timedelta(seconds=5)
    return made


def index(start: str, end: str, change: float) -> list[Event]:
    return every(at(start), at(end), 5, lambda when: kospi(when, change))


def halt_morning(symbol: str = SYMBOL, *, flagged: bool = True) -> list[Event]:
    """One symbol's morning like 2026-11-06's (module doc), and the KOSPI's to 10:00."""
    return [
        *quotes("09:00:00", "09:03:25", "0", symbol),
        # The last trade, at 09:03:32: halted (``flagged``), and the book after it.
        coded(trade(at("09:03:32"), 81_300, symbol=symbol), "0", halted=flagged),
        coded(book(at("09:03:33"), 81_200, 81_300, symbol), "0"),
        # Nothing for 20 minutes; then the reopening auction's book, and its trades from 09:33:38.
        *quotes("09:23:33", "09:33:33", "C", symbol, trades=False),
        *quotes("09:33:37", "09:34:07", "C", symbol),
        *quotes("09:34:10", "10:00:00", "0", symbol),
    ]


def market() -> list[Event]:
    return [
        *index("09:00:00", "09:02:25", -5.0),
        *index("09:02:30", "09:34:00", -8.4),
        *index("09:34:05", "10:00:00", -6.0),
    ]


def kept(db: Path) -> list[tuple[Any, ...]]:
    with sqlite3.connect(db) as conn:
        return conn.execute(
            "SELECT kind, start, end, detail, learned_at FROM special_windows ORDER BY start, kind"
        ).fetchall()


# The windows of the halt morning: the breaker the rules call at 09:03:30, ended at the real reopening
# (continuous trading at 09:34:10), and the halt confirmed by the halted trade of 09:03:32, ended once
# a minute of fresh quotes and trades has passed (09:35:10).
KEPT = [
    (
        "circuit_breaker",
        "2026-10-12T09:03:30+09:00",
        "2026-10-12T09:34:10+09:00",
        "1단계",
        "2026-10-12T09:34:10+09:00",
    ),
    (
        "market_halt",
        "2026-10-12T09:03:32+09:00",
        "2026-10-12T09:35:10+09:00",
        "재개",
        "2026-10-12T09:35:10+09:00",
    ),
]


def situations(events: list[Event], symbols: tuple[str, ...] = (SYMBOL,)) -> list[tuple[datetime, Window]]:
    """Every window ``events`` make, in receipt order, with when it was learned."""
    special = SpecialSituations(CALENDAR)
    out = []
    for feed, row in sorted(events, key=lambda e: e[1]["received_at"]):
        out += [(row["received_at"], w) for w in special.event(feed, row, symbols)]
    return out


def halts(learned: list[tuple[datetime, Window]]) -> list[tuple[datetime, datetime, datetime, str | None]]:
    """The market halt's versions: learned, start, end, phase."""
    return [(r, w.start, w.end, w.detail) for r, w in learned if w.kind is Kind.MARKET_HALT]


# --- The windows.


def test_a_breaker_is_called_in_the_first_five_minutes() -> None:
    # The KOSPI down 8.4% from 09:02:30, held a minute: the rules call the first stage at 09:03:30 (on
    # 2026-11-06 trading halted at 09:03:33); before #625 the minute began at 09:05.
    [(learned, window)] = situations(market())

    assert learned == at("09:03:30")
    assert window == Window(Kind.CIRCUIT_BREAKER, None, at("09:03:30"), at("09:33:30"), "1단계")


def test_the_halt_is_confirmed_by_a_halted_trade_and_its_phases_follow_the_time_codes() -> None:
    learned = situations([*halt_morning(), *market()])

    assert halts(learned) == [
        (at("09:03:32"), at("09:03:32"), END_OF_DAY, "정지"),
        (at("09:23:33"), at("09:03:32"), END_OF_DAY, "단일가"),
        (at("09:35:10"), at("09:03:32"), at("09:35:10"), "재개"),
    ]
    breakers = [(r, w.end) for r, w in learned if w.kind is Kind.CIRCUIT_BREAKER]
    assert breakers == [(at("09:03:30"), at("09:33:30")), (at("09:34:10"), at("09:34:10"))]


def test_without_a_halted_trade_the_breaker_is_all_there_is() -> None:
    learned = situations([*halt_morning(flagged=False), *market()])

    assert [(r, w.kind, w.end) for r, w in learned] == [
        (at("09:03:30"), Kind.CIRCUIT_BREAKER, at("09:33:30"))
    ]


def test_a_halted_trade_with_no_breaker_is_one_stocks_halt() -> None:
    learned = situations([*halt_morning(), *index("09:00:00", "10:00:00", -2.0)])

    assert learned == []


def test_a_halted_trade_in_the_minute_before_the_breaker_confirms_the_halt_with_it() -> None:
    # The trade flagged at 09:03:29, a second before the rules call the breaker: confirmed at 09:03:30.
    early = [e for e in halt_morning() if e[1]["received_at"] != at("09:03:32")]
    early.append(coded(trade(at("09:03:29"), 81_300), "0", halted=True))

    learned = situations([*early, *market()])

    assert [(r, w.kind) for r, w in learned][:2] == [
        (at("09:03:30"), Kind.CIRCUIT_BREAKER),
        (at("09:03:30"), Kind.MARKET_HALT),
    ]
    assert halts(learned)[0] == (at("09:03:30"), at("09:03:29"), END_OF_DAY, "정지")


def test_a_symbol_still_in_the_auction_holds_continuous_trading_back() -> None:
    # The other symbol's book and trades say continuous trading from 09:36:10 only: the breaker
    # ends there, and the halt a minute later.
    other = [e for e in halt_morning(OTHER) if e[1]["received_at"] < at("09:34:10")]
    other += [*quotes("09:34:10", "09:36:05", "C", OTHER), *quotes("09:36:10", "10:00:00", "0", OTHER)]

    learned = situations([*halt_morning(), *other, *market()], (SYMBOL, OTHER))

    assert halts(learned)[-1] == (at("09:37:10"), at("09:03:32"), at("09:37:10"), "재개")
    assert [w.end for _, w in learned if w.kind is Kind.CIRCUIT_BREAKER][-1] == at("09:36:10")


def test_a_silence_after_the_reopening_starts_its_minute_again() -> None:
    # No book from 09:34:30 to 09:34:55: the minute of fresh quotes begins again at 09:34:55.
    gap = [
        e
        for e in halt_morning()
        if not (e[0] == BOOK.tr_id and at("09:34:30") <= e[1]["received_at"] < at("09:34:55"))
    ]

    learned = situations([*gap, *market()])

    assert halts(learned)[-1][2] == at("09:35:55")


def test_the_book_just_after_the_halted_trade_is_not_the_reopening() -> None:
    # The book of 09:03:33 in a single-price auction (one stock's VI): received a second after the
    # halt, it is the frame before the silence, not the reopening's auction.
    events = [e if e[1]["received_at"] != at("09:03:33") else coded(e, "C") for e in halt_morning()]

    learned = situations([*events, *market()])

    assert [p for _, _, _, p in halts(learned)] == ["정지", "단일가", "재개"]
    assert halts(learned)[1][0] == at("09:23:33")


# --- Through the engine.


async def test_the_engine_keeps_the_halt_and_the_breaker_ended_at_the_reopening(tmp_path: Path) -> None:
    db = tmp_path / "engine.db"

    await run(db, [*halt_morning(), *market()], until=at("09:50:00"))

    assert kept(db) == KEPT


async def test_answers_whose_window_meets_the_halt_are_special(tmp_path: Path) -> None:
    # 09:01 and 09:02's four minutes meet the breaker from 09:03:30 (with the opening's 5 minutes
    # left out, its window began at 09:06 and left them unmarked: #625's first comment). 09:35's
    # meet the halt to 09:35:10; 09:36's do not.
    db = tmp_path / "engine.db"

    await run(db, [*halt_morning(), *market()], until=at("09:50:00"))

    with sqlite3.connect(db) as conn:
        outcomes = dict(
            conn.execute(
                "SELECT substr(p.bar_close, 12, 5), max(o.special) FROM predictions p"
                " JOIN outcomes o ON o.prediction_id = p.id GROUP BY p.bar_close"
            ).fetchall()
        )
        answers = dict(
            conn.execute(
                "SELECT substr(bar_close, 12, 5), max(special) FROM kind_answers GROUP BY bar_close"
            ).fetchall()
        )
    for close in ("09:01", "09:02"):
        assert outcomes[close] == 1, close
    for close in ("09:01", "09:02", "09:20", "09:35"):  # 09:20 and 09:35 not predicted (the halt), answered
        assert answers[close] == 1, close
    assert outcomes["09:36"] == 0
    assert answers["09:36"] == 0


def test_the_fill_decides_a_recorded_halt_as_the_engine_kept_it(tmp_path: Path) -> None:
    s = settings(tmp_path)
    events = sorted([*halt_morning(), *market()], key=lambda e: e[1]["received_at"])
    record(s.data_dir, events)
    replayed = tmp_path / "replayed.sqlite3"

    async def replay() -> None:
        engine = await run(replayed, events, until=at("11:00:00"))
        engine.store.close()

    asyncio.run(replay())
    db = before_236(s.state_dir, replayed)

    [filled] = fill_special_windows(s, NEXT_MORNING)

    assert (filled.labeled, filled.failed) == (2, None)
    assert kept(db) == KEPT


def test_a_halted_trade_after_the_breakers_window_is_one_stocks_halt() -> None:
    # A breaker called at 09:03:30 that no flagged trade confirmed; at 11:00, long after its window
    # (to 09:33:30), one stock's trade is flagged halted: not the market's.
    morning = [*halt_morning(flagged=False), coded(trade(at("11:00:00"), 81_300), "0", halted=True)]

    learned = situations([*morning, *market(), *index("10:00:05", "11:30:00", -6.0)])

    assert halts(learned) == []


def test_quotes_further_apart_than_fresh_never_make_the_fresh_minute() -> None:
    # After the reopening the book comes every 11 seconds and the trades a second after it: each
    # symbol's trades and book are never each within 10 seconds of the one before, so the halt holds.
    sparse = [e for e in halt_morning() if e[1]["received_at"] < at("09:34:10")]
    when = at("09:34:10")
    while when <= at("10:00:00"):
        sparse += [
            coded(book(when, 81_200, 81_300), "0"),
            coded(trade(when + timedelta(seconds=1), 81_300), "0"),
        ]
        when += timedelta(seconds=11)

    learned = situations([*sparse, *market()])

    assert [p for _, _, _, p in halts(learned)] == ["정지", "단일가"]


async def test_the_status_and_the_special_windows_show_a_market_halt_within_the_contract(
    tmp_path: Path,
) -> None:
    engine = await run(tmp_path / "engine.db", [*halt_morning(), *market()], until=at("09:50:00"))

    async with api(engine) as client:
        status = await get_json(client, "/v1/status")
        special = await get_json(client, f"/v1/symbols/{SYMBOL}/special?from=2026-10-12&to=2026-10-12")
    check("status", status)
    check("special", special)
    assert {w["kind"] for w in status["special"]} == {"circuit_breaker", "market_halt"}
    [halt] = [w for w in special["special"] if w["kind"] == "market_halt"]
    assert (halt["start"], halt["end"], halt["detail"]) == (
        "2026-10-12T09:03:32+09:00",
        "2026-10-12T09:35:10+09:00",
        "재개",
    )


# --- Event orders (Codex's review of #632): when the halted trades come against the breaker's call.
# The breaker is called at 09:03:30 (received then) with its rules' window to 09:33:30; each case lists
# the halted trades' receipts and the halt's start and when it was learned (None: not confirmed).
ORDERS = [
    ("one just after the call", ["09:03:32"], ("09:03:32", "09:03:32")),
    ("one a second before", ["09:03:29"], ("09:03:29", "09:03:30")),
    ("one a minute before, to the second", ["09:02:30"], ("09:02:30", "09:03:30")),
    ("one too early", ["09:02:29"], None),
    ("one too early, one in the minute", ["09:02:29", "09:03:28"], ("09:03:28", "09:03:30")),
    ("one too early, one after", ["09:02:00", "09:03:33"], ("09:03:33", "09:03:33")),
    ("two in the minute", ["09:02:45", "09:03:20"], ("09:02:45", "09:03:30")),
    ("one late in the window", ["09:30:00"], ("09:30:00", "09:30:00")),
    ("one past the window", ["09:33:30"], None),
    ("long before, then just after", ["08:59:00", "09:03:40"], ("09:03:40", "09:03:40")),
]


@pytest.mark.parametrize(("flags", "expected"), [(f, e) for _, f, e in ORDERS], ids=[n for n, _, _ in ORDERS])
def test_the_halted_trades_against_the_breakers_call(
    flags: list[str], expected: tuple[str, str] | None
) -> None:
    morning = [e for e in halt_morning(flagged=False) if e[1]["received_at"] < at("09:03:00")]
    morning += [coded(trade(at(t), 81_300), "0", halted=True) for t in flags]

    learned = situations([*morning, *market()])

    found = [(w.start, r) for r, w in learned if w.kind is Kind.MARKET_HALT]
    assert found == ([] if expected is None else [(at(expected[0]), at(expected[1]))])


def test_a_later_stage_after_the_reopening_is_confirmed_again() -> None:
    # The first stage's halt reopens at 09:35:10; the KOSPI then falls 15.4%: the second stage at
    # 10:21:00, its halted trade at 10:21:02.
    morning = [e for e in halt_morning() if e[1]["received_at"] < at("10:00:00")]
    morning += [*quotes("10:00:00", "10:20:55", "0"), coded(trade(at("10:21:02"), 81_300), "0", halted=True)]
    kospi_ = [*market(), *index("10:00:05", "10:19:55", -6.0), *index("10:20:00", "10:30:00", -15.4)]

    learned = situations([*morning, *kospi_])

    assert [(w.start, w.detail) for _, w in learned if w.kind is Kind.MARKET_HALT][-1] == (
        at("10:21:02"),
        "정지",
    )


def test_continuous_trading_seen_without_the_auctions_frames_still_reopens() -> None:
    # The auction's book and trades lost: the next frames say continuous trading at 09:34:10.
    missed = [e for e in halt_morning() if not at("09:23:00") <= e[1]["received_at"] < at("09:34:10")]

    learned = situations([*missed, *market()])

    assert [p for _, _, _, p in halts(learned)] == ["정지", "단일가", "재개"]
    # The minute counts from the first trade with the book, at 09:34:11: no trade came before it.
    assert halts(learned)[-1][2] == at("09:35:11")


@pytest.mark.parametrize("seed", range(5))
def test_the_order_of_a_seconds_trades_and_books_does_not_move_the_reopening_past_a_frame(seed: int) -> None:
    # Two symbols' frames of one receipt second in any order: the reopening still ends within the
    # 5 seconds between their frames of 09:35:10, and the phases are the same.
    events = sorted([*halt_morning(), *halt_morning(OTHER), *market()], key=lambda e: e[1]["received_at"])
    shuffled = random.Random(seed)
    by_second: dict[datetime, list[Event]] = {}
    for e in events:
        by_second.setdefault(e[1]["received_at"].replace(microsecond=0), []).append(e)
    ordered = []
    for second in sorted(by_second):
        group = by_second[second]
        shuffled.shuffle(group)
        ordered += group
    special = SpecialSituations(CALENDAR)
    learned = [
        (r, w)
        for f, row in ordered
        for r, w in ((row["received_at"], w) for w in special.event(f, row, (SYMBOL, OTHER)))
    ]

    phases = halts(learned)
    assert [p for _, _, _, p in phases] == ["정지", "단일가", "재개"]
    assert at("09:35:10") <= phases[-1][2] <= at("09:35:16")
