"""The structure check: the research's market-structure claims measured on stored data.

Input: hand-made one-minute bars, day candles and KIS daily flows, with every expected
value worked out by hand below. Output: the day's prices, the bar rule's labels, the
sections' lines.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from krx_quant.base_rates import PERIODS
from krx_quant.candles import write_candles
from krx_quant.cli import main
from krx_quant.market_calendar import MarketCalendar
from krx_quant.structure_check import _turn_of_month, correlation, ols, structure_check
from krx_quant.supply import KIS, write_supply
from tests.test_base_rates import FEE, SYMBOL, flat_bar, write_bars


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
    # After NXT opened, a 0 is a real figure (#270): counted, but in neither split (no sign).
    assert f"{SYMBOL} 일봉 2026-05-26 – 2026-06-05, 외국인 순매수 7일(2026-05-26 – 2026-06-04)" in section
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
