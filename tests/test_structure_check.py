"""The structure check: the research's market-structure claims measured on stored data.

Input: hand-made one-minute bars, day candles and KIS daily flows, with every expected
value worked out by hand below. Output: the day's prices, the bar rule's labels, the
sections' lines.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from krx_quant.backfill import BAR_SCHEMA, MINUTE_BARS_TR
from krx_quant.candles import write_candles
from krx_quant.cli import main
from krx_quant.feeds import KST
from krx_quant.market_calendar import MarketCalendar
from krx_quant.pricing import Direction
from krx_quant.reference import Bar
from krx_quant.structure_check import (
    LISTING,
    PERIODS,
    _turn_of_month,
    correlation,
    day_labels,
    day_prices,
    ols,
    period_of,
    structure_check,
)
from krx_quant.supply import KIS, write_supply

SYMBOL = "005930"
FEE = 0.00015
DAY = date(2026, 6, 8)  # a Monday after the listing
UP, FLAT, DOWN = Direction.UP, Direction.FLAT, Direction.DOWN


def bar(day: date, hh: int, mm: int, o: int, h: int, low: int, c: int) -> Bar:
    at = datetime.combine(day, time(hh, mm), KST)
    return Bar(start=at, open=o, high=h, low=low, close=c, volume=10, trades=0, first_at=at, last_at=at)


def flat_bar(day: date, hh: int, mm: int, price: int) -> Bar:
    return bar(day, hh, mm, price, price, price, price)


def write_bars(data: Path, day: date, bars: list[Bar]) -> None:
    path = data / "bars" / f"date={day}" / f"feed={MINUTE_BARS_TR}" / f"symbol={SYMBOL}" / "bars.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "symbol": SYMBOL,
            "bar_time": b.start,
            "venue": "UN",
            "open": int(b.open),
            "high": int(b.high),
            "low": int(b.low),
            "close": int(b.close),
            "volume": b.volume,
            "cum_value": 0,
        }
        for b in bars
    ]
    pq.write_table(pa.Table.from_pylist(rows, schema=BAR_SCHEMA), path)


def test_periods_split_at_the_listing_the_rules_and_the_after_market() -> None:
    names = [name for name, _, _ in PERIODS]
    assert period_of(date(2026, 5, 26)) == names[0]
    assert period_of(LISTING) == names[1]
    assert period_of(date(2026, 7, 30)) == names[1]
    assert period_of(date(2026, 7, 31)) == names[2]
    assert period_of(date(2026, 9, 13)) == names[2]
    assert period_of(date(2026, 9, 14)) == names[3]


def test_day_prices_take_the_regular_session_and_the_closing_auction() -> None:
    session = MarketCalendar({DAY: True}).session(DAY)
    assert session is not None
    bars = [
        flat_bar(DAY, 8, 30, 80_000),  # NXT pre-market: not the open
        bar(DAY, 9, 0, 81_000, 81_200, 81_000, 81_100),  # the opening auction's print opens it
        flat_bar(DAY, 14, 59, 81_500),  # the last price 20 minutes before the auction
        flat_bar(DAY, 15, 0, 81_600),
        flat_bar(DAY, 15, 19, 82_000),  # the last price before the auction
        flat_bar(DAY, 15, 30, 82_300),  # the auction's print
        flat_bar(DAY, 15, 45, 82_500),  # NXT after-market: not the close
    ]
    found = day_prices(bars, session)
    assert found is not None
    assert (found.open, found.drift_from, found.pre_close, found.close) == (81_000, 81_500, 82_000, 82_300)
    # A day whose auction printed nothing is left out, not closed at its last trade.
    assert day_prices([b for b in bars if b.start.time() != time(15, 30)], session) is None


def test_day_labels_follow_the_bar_rule_and_the_previous_window() -> None:
    session = MarketCalendar({DAY: True}).session(DAY)
    assert session is not None
    price = 81_200  # tick 100, k 2: the bar rule's targets are 81,500 and 80,900 at a flat close
    bars = [flat_bar(DAY, h, m, price) for h in range(9, 16) for m in range(60) if (h, m) < (15, 20)]
    spike = next(i for i, b in enumerate(bars) if b.start.time() == time(10, 0))
    bars[spike] = bar(DAY, 10, 0, price, price + 300, price, price)
    labels = day_labels(bars, session, FEE)
    # Bar closes 09:01 to 15:16, each with its closing bar and trades in the four minutes after.
    assert len(labels) == 376
    assert Counter(x.label for x in labels) == {FLAT: 372, UP: 4}  # the windows 09:57 to 10:00
    assert {(x.tick, x.k) for x in labels} == {(100, 2)}
    paired = [x for x in labels if x.previous is not None]
    assert len(paired) == 372  # 09:01 to 09:04 have no window four minutes before
    # Wrong: the four up windows (flat before) and the four after them (up before).
    assert sum(x.previous == x.label for x in paired) == 364


def test_ols_and_correlation() -> None:
    assert ols([(0.0, 0.0), (1.0, 1.0)]) is None
    beta, t = ols([(0.0, 1.0), (1.0, 0.0), (2.0, -1.0)]) or (0.0, 0.0)
    assert beta == pytest.approx(-1.0)
    assert t == float("-inf")  # an exact take-back keeps its sign
    assert ols([(0.0, 2.0), (1.0, 2.0), (2.0, 2.0)]) is None  # the second never moved: no t-value
    beta, t = ols([(0.0, 0.0), (1.0, 2.0), (2.0, 1.0), (3.0, 3.0)]) or (0.0, 0.0)
    # β = 4/5; residuals −0.3, 0.9, −0.9, 0.3: se = √(1.8 / 2 / 5) = 0.4243
    assert beta == pytest.approx(0.8)
    assert t == pytest.approx(0.8 / 0.4242641, rel=1e-6)
    assert correlation([(1.0, 1.0), (2.0, 2.0), (3.0, 3.0)]) == pytest.approx(1.0)
    assert correlation([(1.0, 1.0), (1.0, 2.0), (1.0, 3.0)]) is None  # no spread


def test_turn_of_month_leaves_out_the_history_s_cut_ends() -> None:
    days = [date(2026, 5, d) for d in (26, 27, 28, 29)] + [date(2026, 6, d) for d in (1, 2, 4, 5, 8)]
    # May is the first month (its start cut off) and June the last (maybe not over).
    assert _turn_of_month(days) == {date(2026, 5, 29), date(2026, 6, 1), date(2026, 6, 2), date(2026, 6, 4)}


def four_days(data: Path) -> tuple[MarketCalendar, list[date]]:
    """Mon–Thu 2026-06-08..11 (after the listing): (open, 14:59, 15:19, auction) each day."""
    days = [date(2026, 6, d) for d in (8, 9, 10, 11)]
    prices = [
        (100_000, 100_000, 100_000, 100_000),
        (101_000, 102_000, 103_000, 103_100),  # gap +, day +, auction +1 tick
        (102_000, 101_000, 100_000, 99_900),  # gap −, day −, auction −1 tick
        (100_500, 100_500, 100_500, 100_500),  # gap +, no move in the day, none at the auction
    ]
    for day, (o, d, p, c) in zip(days, prices, strict=True):
        write_bars(
            data,
            day,
            [
                flat_bar(day, 9, 0, o),
                flat_bar(day, 14, 59, d),
                flat_bar(day, 15, 19, p),
                flat_bar(day, 15, 30, c),
            ],
        )
    return MarketCalendar(dict.fromkeys(days, True)), days


def test_days_and_auction_sections(tmp_path: Path) -> None:
    calendar, days = four_days(tmp_path)
    out = structure_check(tmp_path, tmp_path / "state", calendar, [SYMBOL], fee_rate=FEE)
    assert out.startswith(f"구조 실측 {days[0]} – {days[-1]} (1분봉 4거래일, {SYMBOL})")
    after = PERIODS[1][0]
    one = next(line for line in out.split("\n\n")[1].splitlines() if f"{SYMBOL} {after}: 3일" in line)
    # The second day's gap and day went up, the third's down; the fourth's day did not move.
    assert "갭→장중 100.0% (n=2)" in one
    # Both gaps were taken back the next day.
    assert "갭→다음날 0.0% (n=2)" in one
    assert "ρ1 없음" in one  # two pairs only
    auction = next(line for line in out.split("\n\n")[2].splitlines() if f"{SYMBOL} {after}" in line)
    # +100 on 103,000 and −100 on 100,000, and none: medians 9.7bp and 1 tick.
    assert "3일 · 9.7bp 1.0틱" in auction
    assert "15:00–15:20과 100.0% (n=2)" in auction
    assert "그날 흐름과 100.0% (n=2)" in auction
    assert "다음날 갭 반대 100.0% (n=2)" in auction
    assert "다음날 종가 반대 100.0% (n=2)" in auction
    # Four bars a day leave no bar close with trades after it.
    assert "3. 4분 도달 정답(1분봉 규칙): 기간·틱 크기별\n  없음" in out
    assert "4. 월말·월초 × 전날 외국인 순매수(일봉 전체, KIS 수급)\n  없음" in out


def test_turn_of_month_section(tmp_path: Path) -> None:
    state = tmp_path / "state"
    closes = {
        date(2026, 5, 26): 100_000,
        date(2026, 5, 27): 101_000,  # +, foreign + the day before (other)
        date(2026, 5, 28): 102_000,  # +, foreign − (other)
        date(2026, 5, 29): 103_000,  # +, foreign + (turn)
        date(2026, 6, 1): 104_000,  # +, foreign − (turn)
        date(2026, 6, 2): 103_000,  # −, foreign + (turn)
        date(2026, 6, 4): 102_000,  # −, foreign 0: in no split (turn)
        date(2026, 6, 5): 101_000,  # −, foreign − (other)
    }
    write_candles(
        tmp_path,
        "day",
        SYMBOL,
        [
            {"symbol": SYMBOL, "date": d, "venue": "UN", "open": c, "high": c, "low": c, "close": c}
            | {"volume": 1, "value": c}
            for d, c in closes.items()
        ],
    )
    nets = {
        date(2026, 5, 26): 2,
        date(2026, 5, 27): -1,
        date(2026, 5, 28): 5,
        date(2026, 5, 29): -3,
        date(2026, 6, 1): 1,
        date(2026, 6, 2): 0,
        date(2026, 6, 4): -2,
    }
    write_supply(state, SYMBOL, KIS, [{"date": d, "foreign_net": n} for d, n in nets.items()])
    out = structure_check(tmp_path, state, MarketCalendar(), [SYMBOL], fee_rate=FEE)
    section = out.split("\n\n")[4]
    assert f"{SYMBOL} 일봉 2026-05-26 – 2026-06-05, 수급 7일" in section
    turn = next(line for line in section.splitlines() if "월말·월초:" in line)
    assert "월말·월초: 4일 평균" in turn and "상승 50.0%" in turn.split(" · ")[0]
    assert "외국인 + 2일" in turn and "외국인 − 1일" in turn
    other = next(line for line in section.splitlines() if "그 밖:" in line)
    assert "그 밖: 3일" in other and "상승 66.7%" in other.split(" · ")[0]
    assert "외국인 + 1일" in other and "외국인 − 2일" in other


def test_cli_runs_on_an_empty_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("KRX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KRX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("KRX_SYMBOLS", SYMBOL)
    assert main(["structure-check"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("구조 실측: 1분봉 없음")
    assert out.count("\n  없음") == 4
