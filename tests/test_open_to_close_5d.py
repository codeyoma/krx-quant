"""``open_to_close_5d`` (#310): the first session's opening auction to the fifth session's closing
auction, predicted at 08:50 every trading day, answered on the fifth session's night, and the
training that never learns a window not over by the day it is judged on."""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest

from krx_quant.algorithms import Model
from krx_quant.auctions import MINUTE_BAR
from krx_quant.candles import CANDLES_TR
from krx_quant.cards import Card, Meaning
from krx_quant.clock import FakeClock
from krx_quant.daily import (
    DAILY_RULES,
    EX_DIVIDEND_CODES,
    NO_BAND,
    NO_CANDLE,
    PENDING,
    DailyInputs,
    DailyRegistrationError,
    DailySample,
    DayPrices,
    ex_dividend_of,
    first_review_day,
    overlap_review,
    register_daily_kinds,
    sessions_of,
    window_end,
)
from krx_quant.engine import PredictionEngine
from krx_quant.fake_kis import FakeKis
from krx_quant.features import FeatureVector
from krx_quant.feeds.base import KST
from krx_quant.kind_registry import DAILY_KINDS, daily_kind_named
from krx_quant.kinds import DIRECTION_KIND, KindForecast, KindScored
from krx_quant.market_calendar import MarketCalendar
from krx_quant.open_to_close import OPEN_TO_CLOSE, classify
from krx_quant.open_to_close_5d import (
    CLIMATE,
    FLAT_NAME,
    OPEN_TO_CLOSE_5D,
    PERSIST,
    SESSIONS,
    Otc5Climate,
    Otc5Persist,
    answers,
    band,
    call_profit,
    inverse_profit,
    inverse_return,
    open_to_close_5d_stats,
)
from krx_quant.predictions import PredictionStore
from krx_quant.pricing import SELL_TAX
from krx_quant.replay import replay_into
from krx_quant.special import Kind as SpecialKind
from krx_quant.special import Window
from krx_quant.stats import DayOverlap, interval_json, session_index

FEE = 0.015 / 100
SYMBOLS = ("005930", "000660")
# 2026-10-09 (Friday) is Hangul Day: the window begun Monday the 5th ends Monday the 12th.
HANGUL_DAY = date(2026, 10, 9)
CALENDAR = MarketCalendar({HANGUL_DAY: False})
MONDAY = date(2026, 10, 12)
THURSDAY = date(2026, 10, 8)


def at(day: date, hhmm: str) -> datetime:
    return datetime.combine(day, time.fromisoformat(hhmm), KST)


def trading(last: date, n: int, calendar: MarketCalendar = CALENDAR) -> list[date]:
    days: list[date] = []
    day = last
    while len(days) < n:
        if calendar.is_trading_day(day):
            days.append(day)
        day -= timedelta(days=1)
    return sorted(days)


def history(last: date, n: int, seed: int, calendar: MarketCalendar = CALENDAR) -> list[DayPrices]:
    """``n`` made-up KRX days ending on ``last``, on the calendar's trading days."""
    rng = random.Random(seed)
    out, price = [], 50_000.0
    for d in trading(last, n, calendar):
        opened = price * (1 + rng.gauss(0, 0.01))
        close = opened * (1 + rng.gauss(0, 0.015))
        out.append(DayPrices(d, opened, max(opened, close), min(opened, close), close, 1_000))
        price = close
    return out


def test_the_window_runs_from_the_day_s_open_to_its_fifth_session_s_close() -> None:
    days = history(MONDAY, 200, seed=1)
    got = OPEN_TO_CLOSE_5D.answers_of(days, FEE, CALENDAR)
    by_day = {d.day: (i, a) for i, (d, a) in enumerate(zip(days, got, strict=True))}
    i, monday_5th = by_day[date(2026, 10, 5)]
    assert monday_5th.path["end_day"] == "2026-10-12", "Hangul Day is no session: Monday the 12th ends it"
    assert monday_5th.path["sessions"] == SESSIONS
    assert monday_5th.path["change"] == pytest.approx(days[i + 4].close / days[i].open - 1)
    assert monday_5th.path["closes"] == [d.close for d in days[i : i + 5]]
    assert monday_5th.path["previous_close"] == days[i - 1].close
    assert monday_5th.path["ex_dividend"] is None, "days without KIS's 락 구분: not known"
    assert monday_5th.label == classify(monday_5th.path["change"], monday_5th.path["band"])
    # The last four days' windows are not over: no answer yet, but the band they were predicted with.
    assert [a.unscorable for a in got[-4:]] == [PENDING] * 4
    assert all(a.label is None and a.path["band"] is not None for a in got[-4:])
    assert [window_end(d.day, a) for d, a in zip(days[-5:], got[-5:], strict=True)] == [MONDAY, *[None] * 4]
    assert got[0].unscorable == NO_BAND, "no window ended before the first day"


def test_an_unscheduled_closure_moves_the_end_to_the_fifth_day_open() -> None:
    days = history(MONDAY, 120, seed=2)
    closed = date(2026, 10, 6)  # KIS reports it closed after the fact; KRX held no session
    calendar = MarketCalendar({**{d.day: True for d in days}, HANGUL_DAY: False, closed: False})
    held = [d for d in days if d.day != closed]
    got = {d.day: a for d, a in zip(held, OPEN_TO_CLOSE_5D.answers_of(held, FEE, calendar), strict=True)}
    assert got[date(2026, 10, 1)].path["end_day"] == "2026-10-08", "five days KRX held: 1, 2, 5, 7, 8"
    # A day KRX held that the symbol has none of (its closing auction never kept) is still one.
    unkept = date(2026, 10, 7)
    lacking = [d for d in held if d.day != unkept]
    got = {
        d.day: a for d, a in zip(lacking, OPEN_TO_CLOSE_5D.answers_of(lacking, FEE, calendar), strict=True)
    }
    assert got[date(2026, 10, 1)].path["end_day"] == "2026-10-08"
    assert got[date(2026, 10, 1)].path["closes"][3] is None
    assert got[date(2026, 10, 1)].label is not None
    assert got[date(2026, 9, 30)].unscorable == PENDING, "its end day is the one never kept: it waits"
    assert unkept in sessions_of(lacking, calendar) and closed not in sessions_of(lacking, calendar)


def test_the_band_is_set_from_the_windows_that_ended_before_the_day_only() -> None:
    days = history(MONDAY, 300, seed=3)
    sessions = sessions_of(days, CALENDAR)
    before = answers(days, FEE, sessions)[-1].path["band"]
    assert band(days[:-1], sessions_of(days[:-1], CALENDAR)) == pytest.approx(
        OPEN_TO_CLOSE_5D.band_after(days[:-1], CALENDAR)
    )
    # The day's band from the windows ended by the day before: the day's own close ends a window
    # begun four days before, not over at its 08:50, and changes nothing.
    moved = [*days[:-1], replace(days[-1], close=days[-1].close * 1.5)]
    assert answers(moved, FEE, sessions)[-1].path["band"] == pytest.approx(before)
    # The day before's close ended a window begun five sessions before it: it does.
    moved = [*days[:-2], replace(days[-2], close=days[-2].close * 1.5), days[-1]]
    assert answers(moved, FEE, sessions)[-1].path["band"] != pytest.approx(before)
    shares: dict[str | None, int] = {}
    for a in answers(history(MONDAY, 1_500, seed=4), FEE):
        shares[a.label] = shares.get(a.label, 0) + 1
    assert all(400 <= shares[c] <= 600 for c in ("up", "flat", "down")), "about a third each"


def test_a_window_waits_for_its_end_day_s_candle() -> None:
    days = history(MONDAY, 120, seed=5)
    kept_alone = replace(days[-1], candle=False, source=MINUTE_BAR)  # 15:40: the auctions as traded
    got = OPEN_TO_CLOSE_5D.answers_of([*days[:-1], kept_alone], FEE, CALENDAR)
    assert got[-5].unscorable == PENDING, "its ratio crosses days: one adjusted scale only from the candle"
    assert OPEN_TO_CLOSE.answers_of([*days[:-1], kept_alone], FEE)[-1].label is not None, (
        "a day's own move needs no candle (#264)"
    )


def test_kis_s_ex_date_codes_say_whether_a_day_is_a_cash_dividend_s_ex_date() -> None:
    """#310, ``probe-dividends-310`` (2026-10-21): 02 (배당락) and 05 (중간배당락) mark the ex-dividend
    days, 00 every other day; a code the probe never saw (KIS's documentation lists 01 권리락, 03,
    04, …) or none at all is not known."""
    assert EX_DIVIDEND_CODES == {"02", "05"}
    assert {c: ex_dividend_of(c) for c in ("02", "05", "00", "01", "03", "04", "", None)} == {
        "02": True,
        "05": True,
        "00": False,
        "01": None,
        "03": None,
        "04": None,
        "": None,
        None: None,
    }
    row: dict[str, Any] = {"date": MONDAY, "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1}
    read = [DayPrices.of({**row, "ex_code": c}) for c in ("05", "00", "01", None)]
    assert [None if d is None else d.ex_dividend for d in read] == [True, False, None, None]
    old = DayPrices.of(row)  # a candle stored before #310 that KIS has not answered again
    assert old is not None and old.ex_dividend is None


def test_a_window_with_an_ex_dividend_day_after_its_first_session_is_marked() -> None:
    """#310: the answer stays as it is (KIS's adjusted prices take no dividend out), and the path's
    ``ex_dividend`` says whether one of sessions two to five is an ex-dividend day: the first's open
    is ex already. False once all four are marked 00; null for one not known."""
    days = [replace(d, ex_dividend=False) for d in history(MONDAY, 200, seed=6)]
    ex = days.index(next(d for d in days if d.day == date(2026, 9, 29)))  # a Tuesday
    marked = [*days[:ex], replace(days[ex], ex_dividend=True), *days[ex + 1 :]]
    got = OPEN_TO_CLOSE_5D.answers_of(marked, FEE, CALENDAR)
    plain = OPEN_TO_CLOSE_5D.answers_of(days, FEE, CALENDAR)
    flags = {days[i].day: got[i].path["ex_dividend"] for i in range(ex - 6, ex + 2)}
    assert [flags[days[i].day] for i in range(ex - 4, ex)] == [True] * 4, "the day is session 2–5"
    assert flags[days[ex].day] is False, "its own window opens ex: no lean"
    assert flags[days[ex - 5].day] is False and flags[days[ex + 1].day] is False, "it is outside"
    assert [a.label for a in got] == [a.label for a in plain], "the answers stay as they are"
    unknown = [*marked[: ex - 2], replace(marked[ex - 2], ex_dividend=None), *marked[ex - 1 :]]
    got = OPEN_TO_CLOSE_5D.answers_of(unknown, FEE, CALENDAR)
    assert got[ex - 4].path["ex_dividend"] is True, "a known ex-date in the window: marked"
    assert got[ex - 6].path["ex_dividend"] is None, "no ex-date known, one session not known"


async def test_the_kept_krx_day_candles_mark_the_windows_over_kis_s_ex_dividend_days(
    kis: FakeKis, tmp_path: Path
) -> None:
    """#310, through the fake KIS: the collector keeps KIS's 락 구분 in the KRX day candles, and the
    answers read from them (``StoredDays``, as the engine and training do) mark the windows whose
    sessions two to five hold a 02 (year-end) or 05 (interim) day."""
    from krx_quant.daily import StoredDays
    from tests.test_candles import candles_api, keep, kis_candles, trading_days, with_ex_codes

    days = trading_days(date(2025, 10, 1), date(2026, 4, 30))  # before the after-market: candles' closes
    marks = {date(2025, 12, 29): "02", date(2026, 3, 30): "05"}
    kis.rest_responses[CANDLES_TR] = candles_api(
        lambda market, symbol: with_ex_codes(kis_candles(days), marks)
    )
    await keep(kis, tmp_path, datetime(2026, 4, 30, 21, 0, tzinfo=KST))

    stored = StoredDays(tmp_path).days("005930")
    assert [d.day for d in stored] == days
    assert {d.day for d in stored if d.ex_dividend} == set(marks)
    got = {d.day: a for d, a in zip(stored, OPEN_TO_CLOSE_5D.answers_of(stored, FEE), strict=True)}
    marked = {day for day, a in got.items() if a.path.get("ex_dividend") is True}
    expected = {days[days.index(m) - k] for m in marks for k in range(1, 5)}
    assert marked == expected, "the four windows begun one to four sessions before each"
    assert all(got[d].path["ex_dividend"] is False for d in days[:-4] if d not in expected)


def inputs(past: Sequence[tuple[date, str | None]], ends: Sequence[date], previous: date) -> DailyInputs:
    return DailyInputs("005930", MONDAY, at(MONDAY, "08:50"), tuple(past), None, previous, tuple(ends))


def test_the_baselines_know_only_the_windows_that_ended() -> None:
    previous = THURSDAY
    ended: list[tuple[date, str | None]] = [(date(2026, 10, 1), "up")]
    persist = Otc5Persist()
    carried = persist.predict(inputs(ended, [THURSDAY], previous))
    assert carried is not None and carried.probabilities == (1.0, 0.0, 0.0)
    assert persist.predict(inputs(ended, [date(2026, 10, 7)], previous)) is None, "none ended the day before"
    assert persist.predict(inputs([(date(2026, 10, 1), None)], [THURSDAY], previous)) is None
    climate = Otc5Climate().predict(
        inputs([(date(2026, 9, d), "down") for d in range(1, 6)], [THURSDAY] * 5, previous)
    )
    assert climate is not None and climate.probabilities == pytest.approx((1 / 8, 1 / 8, 6 / 8))


def test_the_research_p_and_l_holds_the_inverse_product_reset_daily() -> None:
    path: dict[str, Any] = {
        "open": 100.0,
        "previous_close": 101.0,
        "closes": [99.0, 98.0, 100.0, 97.0, 95.0],
        "change": 95.0 / 100.0 - 1,
        "fee_rate": FEE,
    }
    # First day bought at the open: (1 − 2(99/101 − 1)) ÷ (1 − 2(100/101 − 1)); then each day's −2x.
    by_hand = (1 - 2 * (99 / 101 - 1)) / (1 - 2 * (100 / 101 - 1))
    for previous, close in ((99, 98), (98, 100), (100, 97), (97, 95)):
        by_hand *= 1 - 2 * (close / previous - 1)
    assert inverse_return(path, 2) == pytest.approx(by_hand - 1)
    assert inverse_profit("down", path, "005930", date(2026, 10, 12)) == pytest.approx(
        ((by_hand - 1) / 2 - 2 * FEE / 2) * 10_000
    )
    assert inverse_profit("down", path, "005930", date(2026, 5, 26)) is None, "before its listing"
    assert inverse_profit("down", path, "009150", date(2026, 10, 12)) is None, "no inverse product"
    assert (
        inverse_profit("down", {**path, "closes": [99.0, None, 100.0, 97.0, 95.0]}, "005930", MONDAY) is None
    )
    assert call_profit("up", path) == pytest.approx((-0.05 - SELL_TAX - 2 * FEE) * 10_000)
    assert call_profit("down", path) == pytest.approx((0.05 - SELL_TAX - 2 * FEE) * 10_000)
    assert call_profit("flat", path) is None
    records = [
        KindScored(
            "alg", "v1", "005930", at(MONDAY + timedelta(days=d), "08:50"), (0.0, 0.0, 1.0), 2, {}, path
        )
        for d in range(3)
    ]
    places = session_index(r.bar_close.date() for r in records)
    stats = open_to_close_5d_stats("alg", records, {}, DayOverlap(SESSIONS, places))
    assert stats["short"]["trades"] == 3 and stats["short_inverse"]["trades"] == 3
    profit = call_profit("down", path)
    assert profit is not None
    assert stats["short"]["per_trade"] == interval_json(
        DayOverlap(SESSIONS, places).mean_interval([(r.bar_close.date(), profit) for r in records])
    )


def test_the_kind_is_registered_with_its_overlap_and_its_review_days_by_the_rule() -> None:
    assert daily_kind_named("open_to_close_5d") is OPEN_TO_CLOSE_5D
    assert DAILY_KINDS == (OPEN_TO_CLOSE, OPEN_TO_CLOSE_5D)
    assert (OPEN_TO_CLOSE_5D.sessions, OPEN_TO_CLOSE_5D.overlap) == (5, 5)
    assert OPEN_TO_CLOSE_5D.baseline_names() == {FLAT_NAME, CLIMATE, PERSIST}
    shown = OPEN_TO_CLOSE_5D.as_dict(OPEN_TO_CLOSE_5D.baseline_cards())["daily"]
    assert (shown["sessions"], shown["answer_from"], shown["to_title"]) == (
        5,
        "20:35",
        "다섯째 거래일 종가 단일가",
    )
    assert OPEN_TO_CLOSE.as_dict([])["daily"]["sessions"] == 1
    with pytest.raises(DailyRegistrationError, match="overlap"):
        register_daily_kinds([replace(OPEN_TO_CLOSE_5D, overlap=1)])
    # #310 "규칙이 주는 값": D₁ by the measured variance ratio.
    table = {1.0: 40, 1.5: 60, 2.0: 60, 2.5: 60, 3.0: 80, 3.5: 80, 4.0: 100, 5.0: 100, 6.0: 120}
    assert {vr: first_review_day(vr, 5) for vr in table} == table
    assert first_review_day(1.0, 1) == DAILY_RULES.probation_days[0], "one session: open_to_close's own"
    review = overlap_review(2.59, 5)
    assert review.rules.probation_days == (60, 120, 180) == review.rules.bench_days
    assert (review.recent_days, review.min_sample, review.rules.confidence) == (60, 180, 0.983)


def test_scheduled_end_and_first_unanswered_count_trading_days() -> None:
    assert OPEN_TO_CLOSE_5D.scheduled_end(date(2026, 10, 5), CALENDAR) == MONDAY
    assert OPEN_TO_CLOSE_5D.scheduled_end(MONDAY, CALENDAR) == date(2026, 10, 16)
    assert OPEN_TO_CLOSE.scheduled_end(MONDAY, CALENDAR) == MONDAY
    assert OPEN_TO_CLOSE.first_unanswered(MONDAY, CALENDAR) == MONDAY - timedelta(days=30)
    assert OPEN_TO_CLOSE_5D.first_unanswered(MONDAY, CALENDAR) == date(2026, 9, 8), (
        "four sessions before 9-12"
    )


class Source:
    def __init__(self, days: dict[str, list[DayPrices]]) -> None:
        self.stored = {s: list(d) for s, d in days.items()}

    def days(self, symbol: str) -> list[DayPrices]:
        return list(self.stored.get(symbol, []))


def engine(db: Path, clock: FakeClock, source: Source) -> PredictionEngine:
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
        daily_kinds=(OPEN_TO_CLOSE_5D,),
    )


def next_day(days: list[DayPrices], day: date, *, candle: bool = True) -> DayPrices:
    opened = days[-1].close
    return DayPrices(day, opened, opened * 1.01, opened * 0.99, opened * 1.004, 1_000, MINUTE_BAR, candle)


async def test_a_window_is_predicted_at_0850_and_answered_on_its_fifth_session_s_night(
    tmp_path: Path,
) -> None:
    pasts = {s: history(THURSDAY, 400, seed=i) for i, s in enumerate(SYMBOLS)}
    source = Source(pasts)
    clock = FakeClock(at(MONDAY, "07:20"))
    e = engine(tmp_path / "e.sqlite3", clock, source)
    await replay_into(e, clock, [], until=at(MONDAY, "08:51"))
    made = e.store.kind_recent(OPEN_TO_CLOSE_5D.name, 1, "005930", 100)
    assert {(r["algorithm"], r["bar_close"]) for r in made} == {
        (b, "2026-10-12T08:50:00+09:00") for b in (FLAT_NAME, CLIMATE, PERSIST)
    }
    expected = OPEN_TO_CLOSE_5D.band_after(pasts["005930"], CALENDAR)
    assert expected is not None and all(r["band"] == pytest.approx(expected.value) for r in made)
    # Persist: the window that ended on Thursday the 8th (the trading day before Monday: Hangul Day
    # between), begun Friday the 2nd.
    ended = {
        d.day: a
        for d, a in zip(
            pasts["005930"], OPEN_TO_CLOSE_5D.answers_of(pasts["005930"], FEE, CALENDAR), strict=True
        )
    }
    persisted = next(r for r in made if r["algorithm"] == PERSIST)
    label = ended[date(2026, 10, 2)].label
    assert ended[date(2026, 10, 2)].path["end_day"] == "2026-10-08"
    assert persisted["output"] == {f"p_{c}": float(c == label) for c in OPEN_TO_CLOSE_5D.classes}
    friday = date(2026, 10, 16)
    for day in (MONDAY, date(2026, 10, 13), date(2026, 10, 14), date(2026, 10, 15)):
        for s in SYMBOLS:
            source.stored[s].append(next_day(source.stored[s], day))
    await replay_into(e, clock, [], until=at(date(2026, 10, 15), "23:00"))
    monday = [
        a for a in e.store.kind_answers(OPEN_TO_CLOSE_5D.name) if a["bar_close"].startswith("2026-10-12")
    ]
    assert monday == [], "its fifth session is Friday"
    for s in SYMBOLS:  # Friday at 15:40: the auctions alone, before the 20:30 candle
        source.stored[s].append(next_day(source.stored[s], friday, candle=False))
    await replay_into(e, clock, [], until=at(friday, "20:40"))
    monday = [
        a for a in e.store.kind_answers(OPEN_TO_CLOSE_5D.name) if a["bar_close"].startswith("2026-10-12")
    ]
    assert monday == [], "it waits for Friday's candle"
    e.store.keep_special(
        Window(SpecialKind.VI, "005930", at(date(2026, 10, 14), "10:00"), at(date(2026, 10, 14), "10:02")),
        at(date(2026, 10, 14), "10:00"),
    )
    for s in SYMBOLS:
        source.stored[s][-1] = replace(source.stored[s][-1], candle=True)
    await replay_into(e, clock, [], until=at(friday, "20:51"))
    answered = {
        a["symbol"]: a
        for a in e.store.kind_answers(OPEN_TO_CLOSE_5D.name)
        if a["bar_close"].startswith("2026-10-12")
    }
    assert set(answered) == set(SYMBOLS)
    first = answered["005930"]
    assert first["scored_at"] == "2026-10-16T20:45:00+09:00", "the first look after its candle"
    assert first["path"]["end_day"] == "2026-10-16" and first["path"]["vi"] == 1, "the five sessions' VIs"
    assert first["special"] is False
    the_week = source.stored["005930"][-5:]
    assert first["path"]["change"] == pytest.approx(the_week[-1].close / the_week[0].open - 1)


async def test_a_breaker_in_any_session_makes_the_window_special_and_a_missing_end_is_given_up(
    tmp_path: Path,
) -> None:
    pasts = {s: history(THURSDAY, 300, seed=i) for i, s in enumerate(SYMBOLS)}
    source = Source(pasts)
    clock = FakeClock(at(MONDAY, "08:00"))
    e = engine(tmp_path / "e.sqlite3", clock, source)
    await replay_into(e, clock, [], until=at(MONDAY, "09:00"))
    tuesday = date(2026, 10, 13)
    e.store.keep_special(
        Window(SpecialKind.SIDECAR, None, at(tuesday, "10:00"), at(tuesday, "10:05")), at(tuesday, "10:00")
    )
    friday = date(2026, 10, 16)
    for day in (MONDAY, tuesday, date(2026, 10, 14), date(2026, 10, 15), friday):
        source.stored["005930"].append(next_day(source.stored["005930"], day))
    e.store.close()
    later = FakeClock(at(friday, "20:30"))
    e2 = engine(tmp_path / "e.sqlite3", later, source)
    await replay_into(e2, later, [], until=at(friday, "20:36"))
    answered = {a["symbol"]: a for a in e2.store.kind_answers(OPEN_TO_CLOSE_5D.name)}
    assert answered["005930"]["special"] is True, "a sidecar on Tuesday, the window's second session"
    assert "000660" not in answered, "its days have not come: it waits"
    e2.store.close()
    week_on = FakeClock(at(date(2026, 10, 23), "20:34"))
    e3 = engine(tmp_path / "e.sqlite3", week_on, source)
    await replay_into(e3, week_on, [], until=at(date(2026, 10, 23), "20:36"))
    given_up = [a for a in e3.store.kind_answers(OPEN_TO_CLOSE_5D.name) if a["symbol"] == "000660"]
    assert [(a["class"], a["unscorable"]) for a in given_up] == [(None, NO_CANDLE)]
    assert given_up[0]["special"] is True, "its sessions held the sidecar too"


# Training (#310 "학습에서 겹침"): a window's label holds the prices up to its end.
class Recording:
    """A stand-in daily algorithm of the kind: it keeps every sample it is fitted on."""

    name = "recording"
    kind = OPEN_TO_CLOSE_5D.name
    features: tuple[str, ...] = ("oc_1", "climate_up")
    training_days = 400
    param_meanings: Mapping[str, Meaning] = {}
    card = Card("Test", "test", "test", ("oc_1",), "test", "test", "test")

    def __init__(self) -> None:
        self.fits: list[list[DailySample]] = []

    def default_model(self) -> Model:
        return Model("recording-default", {}, target_version=1)

    def fit(self, samples: Sequence[DailySample]) -> Model:
        self.fits.append(list(samples))
        return Model(f"recording-{len(self.fits)}", {}, target_version=1)

    def predict(self, features: FeatureVector, model: Model) -> KindForecast:
        return KindForecast((1 / 3, 1 / 3, 1 / 3))


def test_training_never_learns_a_window_not_over_by_the_day_it_is_judged_on(tmp_path: Path) -> None:
    from krx_quant.daily_features import DailyFiles, LiveFeatures, samples
    from krx_quant.daily_training import WALK_MIN_TRAIN, WALK_REFIT, DailyTrainer, walk_forward
    from tests.daily_doubles import SYMBOLS as MADE
    from tests.daily_doubles import made_history

    last = date(2026, 10, 16)
    calendar = MarketCalendar()
    data, state = tmp_path / "data", tmp_path / "state"
    made_history(data, state, last, 1_000)
    files = DailyFiles(data, state)
    found = samples(OPEN_TO_CLOSE_5D, files, MADE, Recording.features, FEE, calendar=calendar)
    by_day = sorted({s.day for s in found})
    # A sample's window ends four sessions after its day; the last four are not over.
    for s in found:
        assert s.ends is None or s.ends == by_day[by_day.index(s.day) + 4]
    assert {s.day for s in found if s.ends is None} == set(by_day[-4:])
    # Its features know only the windows that ended before its day: the same as live at 08:50.
    live = LiveFeatures(OPEN_TO_CLOSE_5D, data, state, calendar, MADE, FEE)
    for s in [x for x in found if x.day == last]:
        history = [d for d in files.days(s.symbol) if d.day < last]
        got = live(Recording(), s.symbol, last, history, OPEN_TO_CLOSE_5D.band_after(history, calendar))
        assert got.as_dict() == pytest.approx(s.features.as_dict(), nan_ok=True)
    # The walk-forward's folds: every sample fitted on ended before the fold's first day.
    algorithm = Recording()
    run = walk_forward(algorithm, OPEN_TO_CLOSE_5D, files, MADE, FEE, calendar=calendar, last=last)
    assert run.folds == len(algorithm.fits)
    for fold, fitted in zip(range(WALK_MIN_TRAIN, len(by_day), WALK_REFIT), algorithm.fits, strict=True):
        first = by_day[fold]
        assert fitted and all(s.ends is not None and s.ends < first for s in fitted)
        assert {s.day for s in fitted} >= {by_day[fold - 5]} and by_day[fold - 4] not in {
            s.day for s in fitted
        }, "the four days before the fold are purged"
    # The night's fit: the windows ended by the night; its recent look, those before its 60 days.
    night = by_day[-1]
    nightly = Recording()
    DailyTrainer(files, MADE, FEE, calendar).train(nightly, OPEN_TO_CLOSE_5D, night)
    full, recent = nightly.fits
    assert all(s.ends is not None and s.ends <= night for s in full)
    cut = sorted({s.day for s in full if s.label is not None})[-60]
    assert all(s.ends is not None and s.ends < cut for s in recent)


async def test_a_kind_awaiting_a_probe_is_listed_but_not_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#310 review (#338): a daily kind whose answers read data a probe has not settled
    (``DailyKind.awaiting``, as ``open_to_close_5d`` was until ``probe-dividends-310``'s result) is
    listed with the reason, and nothing of it is predicted, answered, trained or pre-validated."""
    import krx_quant.scheduled as scheduled
    from krx_quant.daily_features import DailyFiles
    from krx_quant.daily_training import DailyTrainer, prevalidate_daily
    from krx_quant.scheduled import Facts, Night, daily_prevalidation_jobs
    from tests.daily_doubles import made_history
    from tests.test_daily_prevalidation import settings as walk_settings

    gated = replace(OPEN_TO_CLOSE_5D, awaiting="a probe (test)")
    assert not gated.runs and gated.as_dict([])["daily"]["awaiting"] == "a probe (test)"
    pasts = {s: history(THURSDAY, 400, seed=i) for i, s in enumerate(SYMBOLS)}
    source = Source(pasts)
    clock = FakeClock(at(MONDAY, "07:20"))
    algorithm = Recording()
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
        daily_kinds=(OPEN_TO_CLOSE, gated),
        daily_algorithms=[algorithm],
        daily_features=lambda a, s, d, p, b: FeatureVector(dict.fromkeys(a.features), a.features),
        daily_feature_names={"oc_1", "climate_up"},
    )
    assert gated in e.daily_kinds, "listed: /v1/kinds shows it, with the reason"
    for day in (MONDAY, date(2026, 10, 13), date(2026, 10, 14), date(2026, 10, 15), date(2026, 10, 16)):
        await replay_into(e, clock, [], until=at(day, "08:51"))
        for s in SYMBOLS:
            source.stored[s].append(next_day(source.stored[s], day))
    await replay_into(e, clock, [], until=at(date(2026, 10, 16), "21:00"))
    assert e.store.kind_recent(OPEN_TO_CLOSE.name, OPEN_TO_CLOSE.answer_version, "005930", 10), (
        "the other runs"
    )
    assert e.store.kind_recent(gated.name, 1, "005930", 10) == [], "no prediction"
    assert e.store.kind_answers(gated.name) == [], "no answer"
    assert algorithm.name not in e.store.statuses(), "not run, not enrolled"
    assert algorithm.name not in e.waiting(), (
        "nor waiting for a pre-validation: none runs before its kind does"
    )
    # No training rows, no pre-validation, no job for it.
    data, state = tmp_path / "data", tmp_path / "state"
    made_history(data, state, date(2026, 10, 16), 300)
    report, model = DailyTrainer(DailyFiles(data, state), SYMBOLS, FEE).train(
        algorithm,
        gated,
        date(2026, 10, 16),
    )
    assert model is None and algorithm.fits == [] and "waits for" in report["reason"]
    with pytest.raises(ValueError, match="waits for"):
        prevalidate_daily(
            walk_settings(tmp_path / "walk", 0.0, days=300),
            algorithm.name,
            algorithms=[algorithm],
            kinds=[OPEN_TO_CLOSE, gated],
        )
    monkeypatch.setattr(scheduled, "daily_kind_named", lambda name: gated)
    [job] = daily_prevalidation_jobs([algorithm.name], {}, {algorithm.name: gated.name})
    night = Night(date(2026, 10, 16), True, "c", None, {}, Facts(waiting=(algorithm.name,)))
    assert not job.due(night)


async def test_the_kind_runs_now_that_the_dividend_probe_s_result_is_in(tmp_path: Path) -> None:
    """#310: ``probe-dividends-310`` (2026-10-21) found KIS's adjusted prices take no cash dividend
    out and its 락 구분 marking the ex-dividend days, so the kind runs with the spec's fallback (the
    answer as it is, the window marked ``ex_dividend``). Its baselines predict at 08:50; its
    algorithm waits for a pre-validation, which the scheduled job runs that night; it is trained at
    night; and while its review days are held, a pass neither enrolls nor simulates it."""
    from krx_quant.algorithms.cox5d import Cox5d
    from krx_quant.daily_features import DailyFiles
    from krx_quant.daily_training import DailyTrainer
    from krx_quant.scheduled import JOBS, OK, Facts, JobRecord, Night, _daily_simulation_due

    assert OPEN_TO_CLOSE_5D.runs and OPEN_TO_CLOSE_5D.awaiting is None
    assert OPEN_TO_CLOSE_5D.as_dict([])["daily"]["awaiting"] is None
    assert all(k.runs for k in DAILY_KINDS)
    pasts = {s: history(THURSDAY, 400, seed=i) for i, s in enumerate(SYMBOLS)}
    clock = FakeClock(at(MONDAY, "07:20"))
    algorithm = Recording()
    e = PredictionEngine(
        PredictionStore(tmp_path / "e.sqlite3"),
        CALENDAR,
        SYMBOLS,
        FEE,
        clock,
        algorithms=(),
        kinds=(DIRECTION_KIND,),
        kind_algorithms=(),
        daily=Source(pasts),
        daily_algorithms=[algorithm],
        daily_features=lambda a, s, d, p, b: FeatureVector(dict.fromkeys(a.features), a.features),
        daily_feature_names={"oc_1", "climate_up"},
        admits=lambda a: False,  # no pre-validation report yet
    )
    await replay_into(e, clock, [], until=at(MONDAY, "08:51"))
    made = e.store.kind_recent(OPEN_TO_CLOSE_5D.name, 1, "005930", 10)
    assert {r["algorithm"] for r in made} == {FLAT_NAME, CLIMATE, PERSIST}
    assert algorithm.name in e.waiting(), "its pre-validation comes first"
    assert algorithm.name not in e.store.statuses(), "not run before it passes"
    # Trained at night.
    from tests.daily_doubles import made_history

    data, state = tmp_path / "data", tmp_path / "state"
    made_history(data, state, date(2026, 10, 16), 300)
    report, model = DailyTrainer(DailyFiles(data, state), SYMBOLS, FEE).train(
        algorithm, OPEN_TO_CLOSE_5D, date(2026, 10, 16)
    )
    assert model is not None and algorithm.fits, report["reason"]
    # The scheduled pre-validation of cox-5d is due the first night it waits.
    [job] = [j for j in JOBS if j.name == "prevalidate-cox-5d"]
    night = Night(date(2026, 10, 23), True, "c", None, {}, Facts(waiting=("cox-5d",)))
    assert job.due(night)
    assert not job.due(
        replace(night, facts=Facts(waiting=("cox-5d",), pending_answers=(OPEN_TO_CLOSE_5D.name,)))
    )
    # A pass tonight: held for its review days, neither enrolled nor simulated.
    passed = {job.name: JobRecord(date(2026, 10, 23), OK, at(date(2026, 10, 23), "22:31"), "c", 0, 60.0)}
    assert not _daily_simulation_due([Cox5d()])(replace(night, records=passed))
