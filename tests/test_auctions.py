"""The KRX auctions' prices (#264): kept from KRX's one-minute bars (else the recording), and the
KRX days the daily kind reads live and in training, their close the closing auction's."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest

from krx_quant.auctions import (
    AFTER_MARKET_OPENED,
    DAY_CANDLE,
    KEEP_DAYS,
    MINUTE_BAR,
    NONE,
    RECORDED,
    AuctionKeeper,
    closes_pending,
    days_of,
    read_auctions,
    write_auctions,
)
from krx_quant.candles import read_krx_days, write_krx_days
from krx_quant.daily import StoredDays
from krx_quant.daily_features import DailyFiles, samples
from krx_quant.fake_kis import FakeKis
from krx_quant.feeds import KRX_TRADE
from krx_quant.feeds.base import KST
from krx_quant.market_calendar import MarketCalendar
from krx_quant.open_to_close import OPEN_TO_CLOSE
from krx_quant.ratelimit import RateLimiter
from krx_quant.recorder import ParquetRecorder
from tests.daily_doubles import SYMBOLS, made_history
from tests.kis_rest import MINUTE_BARS, minute_bars_api, requests_for, rest_client
from tests.replay_events import at, krx_trade


def weekdays(first: date, last: date) -> MarketCalendar:
    """KIS's holidays as the server knows them from ``first`` (the keeper's days are those KIS
    told of): every weekday trades."""
    days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    return MarketCalendar({d: d.weekday() < 5 for d in days})


CALENDAR = weekdays(date(2025, 9, 1), date(2026, 12, 31))
# The keeper's tests: a few days before the after-market, so they ask little of the fake KIS.
RECENT = weekdays(date(2026, 9, 9), date(2026, 12, 31))
MONDAY = date(2026, 10, 19)
FEE = 0.015 / 100


def candle(day: date, opened: int, close: int, symbol: str = "005930") -> dict[str, Any]:
    return {
        "symbol": symbol,
        "date": day,
        "venue": "KRX",
        "open": opened,
        "high": max(opened, close) + 500,
        "low": min(opened, close) - 500,
        "close": close,
        "volume": 1_000,
        "value": 1_000 * close,
    }


def kept(
    day: date, opened: int | None, close: int | None, source: str = MINUTE_BAR, volume: int | None = None
) -> dict[str, Any]:
    return {
        "symbol": "005930",
        "date": day,
        "open": opened,
        "close": close,
        "source": source,
        "close_volume": volume,
    }


def bar(day: date, hhmm: str, opened: int, close: int) -> dict[str, str]:
    return {
        "stck_bsop_date": day.strftime("%Y%m%d"),
        "stck_cntg_hour": f"{hhmm}00",
        "stck_prpr": str(close),
        "stck_oprc": str(opened),
        "stck_hgpr": str(max(opened, close) + 100),
        "stck_lwpr": str(min(opened, close) - 100),
        "cntg_vol": "1000",
        "acml_tr_pbmn": "1000",
    }


def krx_bars(day: date, opening: int, closing: int, evening: int | None = None) -> list[dict[str, str]]:
    """A KRX (``J``) day of bars, oldest first: the opening auction's print in the 09:00 bar,
    continuous trading to 15:19, the closing auction's print in the 15:30 bar, and, given
    ``evening``, the after-market from 16:00 to 19:59 ending there."""
    start = datetime.combine(day, time(9, 0))
    rows = [bar(day, "0900", opening, opening + 100)]
    for i in range(1, 380):  # 09:01–15:19
        rows.append(bar(day, (start + timedelta(minutes=i)).strftime("%H%M"), opening, opening + 100))
    rows.append(bar(day, "1530", closing, closing))
    if evening is not None:
        evening_start = datetime.combine(day, time(16, 0))
        for i in range(240):  # 16:00–19:59
            rows.append(bar(day, (evening_start + timedelta(minutes=i)).strftime("%H%M"), closing, evening))
    return rows


def test_a_days_close_is_the_closing_auction_price_kept_and_the_candles_before_the_after_market() -> None:
    """The candle's close stands before the after-market only; from it a day without a kept price
    is left out. A kept close is scaled by the two opens (the candle is adjusted, the bars are
    not); the day past the newest candle is the auctions alone; a day settled none has no close."""
    before = AFTER_MARKET_OPENED - timedelta(days=3)  # a Friday
    candles = [
        candle(before, 70_000, 71_000),  # no kept price: the candle's close
        candle(AFTER_MARKET_OPENED, 70_000, 72_500),  # the after-market's last trade
        candle(date(2026, 9, 15), 35_000, 36_000),  # adjusted: a 2:1 split since
        candle(date(2026, 9, 16), 70_000, 71_500),  # nothing kept: left out
        candle(date(2026, 9, 17), 70_000, 71_500),  # settled none: left out
    ]
    prices = {
        AFTER_MARKET_OPENED: kept(AFTER_MARKET_OPENED, 70_000, 71_200),
        date(2026, 9, 15): kept(date(2026, 9, 15), 70_000, 71_000),
        date(2026, 9, 17): kept(date(2026, 9, 17), None, None, NONE),
        date(2026, 9, 18): kept(date(2026, 9, 18), 71_000, 70_500, RECORDED),  # its candle not in yet
    }
    days = {d.day: d for d in days_of(candles, prices)}
    assert sorted(days) == [before, AFTER_MARKET_OPENED, date(2026, 9, 15), date(2026, 9, 18)]
    assert (days[before].close, days[before].source) == (71_000, DAY_CANDLE)
    assert (days[AFTER_MARKET_OPENED].close, days[AFTER_MARKET_OPENED].source) == (71_200, MINUTE_BAR)
    assert days[AFTER_MARKET_OPENED].high == 73_000, "the candle's high and low stand"
    split = days[date(2026, 9, 15)]
    assert (split.open, split.close) == (35_000, 35_500), "the day's move between the auctions"
    alone = days[date(2026, 9, 18)]
    assert (alone.open, alone.high, alone.low, alone.close, alone.volume, alone.source) == (
        71_000,
        71_000,
        70_500,
        70_500,
        0,
        RECORDED,
    )


def test_a_split_never_sets_a_raw_close_against_an_adjusted_open() -> None:
    """#317 review: the candles are adjusted, the kept auction prices are not. After a 2:1 split
    every candle is halved; a day's move stays its two auctions' as traded, its close is on the
    candles' scale (so close to close across days holds, as the features read it), and a day kept
    without its opening price is left out from the after-market and keeps its candle's close
    before it: a raw close is never set against the adjusted open."""
    days = [date(2026, 9, 10), date(2026, 9, 11), AFTER_MARKET_OPENED, date(2026, 9, 15), date(2026, 9, 16)]
    raw = {d: (70_000 + 1_000 * i, 70_600 + 1_200 * i) for i, d in enumerate(days)}  # as traded
    candles = [candle(d, o // 2, c // 2 + 150) for d, (o, c) in raw.items()]  # halved, the evening's close
    prices = {d: kept(d, o, c) for d, (o, c) in raw.items()}
    for d in (days[1], days[4]):  # no opening price kept: no opening bar, no recording
        prices[d] = kept(d, None, raw[d][1])

    got = {d.day: d for d in days_of(candles, prices)}

    assert sorted(got) == days[:4], "from the after-market, a close without its open is no price"
    for d in (days[0], days[2], days[3]):
        opened, closed = raw[d]
        assert got[d].open == opened / 2 and got[d].close == pytest.approx(closed / 2)
        assert got[d].change == pytest.approx(closed / opened - 1), "the auctions' move as traded"
    assert (got[days[1]].close, got[days[1]].source) == (candles[1]["close"], DAY_CANDLE)
    assert got[days[3]].close / got[days[2]].close == pytest.approx(raw[days[3]][1] / raw[days[2]][1])
    assert got[days[3]].open / got[days[2]].close == pytest.approx(raw[days[3]][0] / raw[days[2]][1])
    answers = OPEN_TO_CLOSE.answers_of(list(got.values()), FEE)
    assert [a.path["change"] for a in answers] == pytest.approx([d.change for d in got.values()])


async def test_without_the_opening_bar_the_recorded_opening_print_and_else_no_open(
    kis: FakeKis, tmp_path: Path
) -> None:
    """#317 review: KIS has the closing auction's bar but not the opening one. The recorded opening
    print stands in (as traded too); without a recording the day is kept with no open, which no
    answer sets against the adjusted candle."""
    data = tmp_path / "data"
    recorded = date(2026, 10, 16)
    recorder = ParquetRecorder(data, flush_rows=100, flush_seconds=3600)
    feed, row = krx_trade(at("09:00:00.3", recorded), 80_100, bid=80_000, ask=80_200)
    recorder.record("events", recorded, feed, row["symbol"], row, KRX_TRADE.schema)
    recorder.close()

    def bars(market: str, symbol: str, day: date) -> list[dict[str, str]]:
        return krx_bars(day, 80_000, 80_500)[1:]  # no 09:00 bar, any day

    kis.rest_responses[MINUTE_BARS] = minute_bars_api(bars)
    now = datetime.combine(MONDAY, time(15, 40), KST)
    async with rest_client(kis, data, now) as client:
        keeper = AuctionKeeper(client, RECENT, data, ("005930",), RateLimiter(1, 0.0, margin=0))
        await keeper.run(now)
    prices = read_auctions(data, "005930")
    assert prices[recorded] == kept(recorded, 80_100, 80_500, volume=1000)
    assert prices[MONDAY] == kept(MONDAY, None, 80_500, volume=1000)
    monday = [candle(MONDAY, 40_000, 40_400)]  # adjusted since: a raw close would halve the move
    assert days_of(monday, prices) == [], "no open of its own: no day"


async def test_the_keeper_keeps_the_closing_auction_bar_not_the_after_markets_last(
    kis: FakeKis, tmp_path: Path
) -> None:
    """From KRX's bars (``J``): the close of the closing auction's bar (15:30), not the day's last
    bar (19:59, the after-market), and the open of the opening's (09:00); a page each, to ten
    minutes after the auction. Newest first, the days of the last year up to the last closed."""
    days = {MONDAY: (81_000, 82_000, 82_700), date(2026, 10, 16): (80_000, 80_500, None)}

    def bars(market: str, symbol: str, day: date) -> list[dict[str, str]]:
        assert market == "J", "the KRX market alone"
        if day not in days:
            return []
        opening, closing, evening = days[day]
        return krx_bars(day, opening, closing, evening)

    kis.rest_responses[MINUTE_BARS] = minute_bars_api(bars)
    data = tmp_path / "data"
    now = datetime.combine(MONDAY, time(15, 40), KST)
    async with rest_client(kis, data, now) as client:
        keeper = AuctionKeeper(client, RECENT, data, ("005930",), RateLimiter(1, 0.0, margin=0))
        assert await keeper.run(now) >= 2

    prices = read_auctions(data, "005930")
    assert prices[MONDAY] == kept(MONDAY, 81_000, 82_000, volume=1000), "with the closing bar's volume"
    assert prices[date(2026, 10, 16)] == kept(date(2026, 10, 16), 80_000, 80_500, volume=1000)
    asked = [(q["FID_INPUT_DATE_1"], q["FID_INPUT_HOUR_1"]) for q in requests_for(kis, MINUTE_BARS)]
    assert asked[:4] == [
        ("20261019", "153959"),
        ("20261019", "090959"),
        ("20261016", "153959"),
        ("20261016", "090959"),
    ]
    assert all(q["FID_COND_MRKT_DIV_CODE"] == "J" for q in requests_for(kis, MINUTE_BARS))
    # Days KIS has no bars of: settled none before the after-market, as there is the candle's.
    assert {r["source"] for d, r in prices.items() if d < AFTER_MARKET_OPENED} == {NONE}


async def test_a_day_kept_before_the_volumes_gets_its_closing_bar_s_volume_once(
    kis: FakeKis, tmp_path: Path
) -> None:
    """#305: the closing flow's denominator reads the KRX closing bar's volume × close. Days kept
    from KRX's bars before the volume was kept are asked their closing bar again, once: its volume
    where its close is the one kept, else 0. The days from the recording or settled none are not."""
    data = tmp_path / "data"
    friday = date(2026, 10, 16)
    days = RECENT.trading_days(MONDAY - KEEP_DAYS, MONDAY)
    rows = {d: kept(d, None, None, NONE) for d in days}
    rows[MONDAY] = kept(MONDAY, 81_000, 82_000)  # as the bars: its volume is kept
    rows[friday] = kept(friday, 80_000, 70_000)  # another close than the bar's: 0
    rows[date(2026, 10, 15)] = kept(date(2026, 10, 15), 80_000, 80_500, RECORDED)
    write_auctions(
        data, "005930", {d: {k: v for k, v in r.items() if k != "close_volume"} for d, r in rows.items()}
    )

    def bars(market: str, symbol: str, day: date) -> list[dict[str, str]]:
        return krx_bars(day, 81_000, 82_000) if day == MONDAY else krx_bars(day, 80_000, 80_500)

    kis.rest_responses[MINUTE_BARS] = minute_bars_api(bars)
    now = datetime.combine(MONDAY, time(15, 40), KST)
    async with rest_client(kis, data, now) as client:
        keeper = AuctionKeeper(client, RECENT, data, ("005930",), RateLimiter(1, 0.0, margin=0))
        assert await keeper.run(now) == 2
        prices = read_auctions(data, "005930")
        assert prices[MONDAY] == kept(MONDAY, 81_000, 82_000, volume=1000)
        assert prices[friday] == kept(friday, 80_000, 70_000, volume=0)
        assert prices[date(2026, 10, 15)]["close_volume"] is None, "from the recording: no bar to read"
        asked = [(q["FID_INPUT_DATE_1"], q["FID_INPUT_HOUR_1"]) for q in requests_for(kis, MINUTE_BARS)]
        assert asked == [("20261019", "153959"), ("20261016", "153959")], "the closing page alone"
        assert await keeper.run(now) == 0
        assert len(requests_for(kis, MINUTE_BARS)) == 2, "not again"


async def test_without_the_bar_the_recorded_prints_and_a_day_with_neither_waits_then_is_settled(
    kis: FakeKis, tmp_path: Path
) -> None:
    """No bar of the closing auction: the recorded prints. Neither on the day itself: asked again
    (KIS's bar may come later); once over with other bars of the day, settled without a close."""
    data = tmp_path / "data"
    recorded = date(2026, 10, 16)
    recorder = ParquetRecorder(data, flush_rows=100, flush_seconds=3600)
    for feed, row in [
        krx_trade(at("09:00:00.3", recorded), 80_100, bid=80_000, ask=80_200),
        krx_trade(at("15:30:00.9", recorded), 80_600, bid=80_500, ask=80_700),
        krx_trade(at("16:10:00", recorded), 80_900, bid=80_800, ask=81_000),
    ]:
        recorder.record("events", recorded, feed, row["symbol"], row, KRX_TRADE.schema)
    recorder.close()

    def bars(market: str, symbol: str, day: date) -> list[dict[str, str]]:
        return krx_bars(day, 80_000, 80_500)[:-1]  # no closing auction bar, any day

    kis.rest_responses[MINUTE_BARS] = minute_bars_api(bars)
    now = datetime.combine(MONDAY, time(15, 40), KST)
    async with rest_client(kis, data, now) as client:
        keeper = AuctionKeeper(client, RECENT, data, ("005930",), RateLimiter(1, 0.0, margin=0))
        await keeper.run(now)
        prices = read_auctions(data, "005930")
        assert MONDAY not in prices, "the day itself waits"
        assert prices[recorded] == kept(recorded, 80_100, 80_600, RECORDED)
        assert prices[date(2026, 10, 15)] == kept(date(2026, 10, 15), None, None, NONE)
        asked = len(requests_for(kis, MINUTE_BARS))
        await keeper.run(now)
        assert len(requests_for(kis, MINUTE_BARS)) == asked + 1, "only the day itself is asked again"


def test_the_closes_are_pending_until_every_after_market_day_is_kept(tmp_path: Path) -> None:
    data = tmp_path / "data"
    night = datetime.combine(MONDAY, time(23), KST)
    days = CALENDAR.trading_days(AFTER_MARKET_OPENED, MONDAY)
    why = closes_pending(data, CALENDAR, ("005930",), night)
    assert why == f"closing auction prices not kept yet: 005930 {len(days)} days from {AFTER_MARKET_OPENED}"
    write_auctions(data, "005930", {d: kept(d, 1, 1) for d in days[:-1]})
    assert closes_pending(data, CALENDAR, ("005930",), night) == (
        f"closing auction prices not kept yet: 005930 1 days from {MONDAY}"
    )
    morning = datetime.combine(MONDAY, time(15, 39), KST)
    assert closes_pending(data, CALENDAR, ("005930",), morning) is None, (
        "the day's print is not in before 15:40"
    )
    write_auctions(
        data, "005930", {d: kept(d, 1, 1) for d in days[:-2]} | {MONDAY: kept(MONDAY, None, None, NONE)}
    )
    assert closes_pending(data, CALENDAR, ("005930",), night) is not None
    write_auctions(
        data, "005930", {d: kept(d, 1, 1) for d in days[:-1]} | {MONDAY: kept(MONDAY, None, None, NONE)}
    )
    assert closes_pending(data, CALENDAR, ("005930",), night) is None, "a day settled without one counts"


def _shifted(rows: Mapping[date, dict[str, Any]], by: int) -> dict[date, dict[str, Any]]:
    return {d: {**r, "close": r["close"] + by} for d, r in rows.items()}


def test_live_and_training_read_the_same_days_and_label_them_by_the_closing_auction(tmp_path: Path) -> None:
    """The engine's days (``StoredDays``) and training's (``DailyFiles``, the samples' labels) are
    the same, and their answers end at the kept closing auction price, not the candle's close."""
    data, state = tmp_path / "data", tmp_path / "state"
    last = date(2026, 10, 16)
    made_history(data, state, last, 400)
    for symbol in SYMBOLS:
        prices = read_auctions(data, symbol)
        after = {d: r for d, r in prices.items() if d >= AFTER_MARKET_OPENED}
        write_auctions(data, symbol, {**prices, **_shifted(after, -700)})  # the after-market moved the candle

    live = StoredDays(data)
    files = DailyFiles(data, state)
    by_day = {(s.symbol, s.day): s for s in samples(OPEN_TO_CLOSE, files, SYMBOLS, ("oc_1",), FEE)}
    for symbol in SYMBOLS:
        days = live.days(symbol)
        assert days == files.days(symbol)
        answers = OPEN_TO_CLOSE.answers_of(days, FEE)
        for day, answer in zip(days, answers, strict=True):
            if (symbol, day.day) in by_day:
                trained = by_day[(symbol, day.day)].answer
                assert trained is not None and trained.label == answer.label
                assert trained.path["close"] == answer.path["close"]
        late = [(d, a) for d, a in zip(days, answers, strict=True) if d.day >= AFTER_MARKET_OPENED]
        assert late and all(a.path["source"] == MINUTE_BAR for _, a in late)
        candles = {r["date"]: r for r in read_krx_days(data, symbol)}
        assert all(d.close == candles[d.day]["close"] - 700 for d, _ in late), (
            "the auction's, not the candle's"
        )


def test_a_symbols_days_are_read_again_when_either_file_changes(tmp_path: Path) -> None:
    data = tmp_path / "data"
    friday = AFTER_MARKET_OPENED - timedelta(days=3)
    write_krx_days(data, "005930", [candle(friday, 70_000, 71_000)])
    live = StoredDays(data)
    assert [d.day for d in live.days("005930")] == [friday]
    write_auctions(data, "005930", {MONDAY: kept(MONDAY, 71_000, 71_500)})
    assert [(d.day, d.close) for d in live.days("005930")][-1] == (MONDAY, 71_500), "the day at 15:40"
    assert StoredDays(data).days("000660") == []


@pytest.mark.parametrize("hour", ["15:39", "15:40"])
def test_the_last_closed_day_turns_at_1540(hour: str) -> None:
    from krx_quant.auctions import last_closed

    now = datetime.combine(MONDAY, time.fromisoformat(hour), KST)
    expected = MONDAY if hour == "15:40" else date(2026, 10, 16)
    assert last_closed(CALENDAR, now) == expected
    assert last_closed(CALENDAR, now.replace(hour=7)) == date(2026, 10, 16)
