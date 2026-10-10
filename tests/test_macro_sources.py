"""Macro sources' answers read into daily observations, and a day's readings of them.

Input: answers shaped as ECOS, FRED and Yahoo send them (taken from live answers on
2026-10-10), including no data, missing values, a session still trading and errors.
Output: the observations, oldest first, or the source's error; and the move each
reading shows on a KRX day.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from krx_quant.macro import INDICATORS, Observation, Reading, SourceError, parse_ecos, parse_fred, parse_yahoo


def ecos_row(day: str, value: str) -> dict[str, Any]:
    return {
        "STAT_CODE": "731Y001",
        "ITEM_CODE1": "0000001",
        "UNIT_NAME": "원",
        "TIME": day,
        "DATA_VALUE": value,
    }


def test_ecos_rows_are_read_by_day() -> None:
    body = {
        "StatisticSearch": {
            "list_total_count": 3,
            "row": [ecos_row("20261002", "1355.7"), ecos_row("20261001", "1352.0"), ecos_row("20261006", "")],
        }
    }

    assert parse_ecos(body) == [
        Observation(date(2026, 10, 1), 1352.0),
        Observation(date(2026, 10, 2), 1355.7),
    ]


def test_ecos_without_data_is_empty_and_its_errors_are_errors() -> None:
    assert parse_ecos({"RESULT": {"CODE": "INFO-200", "MESSAGE": "해당하는 데이터가 없습니다."}}) == []
    with pytest.raises(SourceError, match="INFO-100"):
        parse_ecos({"RESULT": {"CODE": "INFO-100", "MESSAGE": "인증키가 유효하지 않습니다."}})


def test_fred_skips_the_days_without_a_value() -> None:
    body = {
        "observations": [
            {"date": "2026-10-06", "value": "4.11"},
            {"date": "2026-10-07", "value": "."},  # a holiday
            {"date": "2026-10-08", "value": "4.15"},
        ]
    }

    assert parse_fred(body) == [Observation(date(2026, 10, 6), 4.11), Observation(date(2026, 10, 8), 4.15)]
    with pytest.raises(SourceError, match="api_key"):
        parse_fred({"error_code": 400, "error_message": "Bad Request.  Variable api_key is not set."})


def yahoo(closes: list[float | None], session_end: int) -> dict[str, Any]:
    return {
        "chart": {
            "result": [
                {
                    "meta": {
                        "symbol": "^SOX",
                        "exchangeTimezoneName": "America/New_York",
                        "currentTradingPeriod": {
                            "regular": {"start": session_end - 23_400, "end": session_end}
                        },
                    },
                    # 09:30 New York on 2026-10-05 .. 2026-10-09
                    "timestamp": [1791207000, 1791293400, 1791379800, 1791466200, 1791552600],
                    "indicators": {"quote": [{"close": closes}]},
                }
            ],
            "error": None,
        }
    }


FRIDAY_CLOSE = 1791576000  # 2026-10-09 16:00 New York


def test_yahoo_closes_are_read_by_new_york_day() -> None:
    body = yahoo([13172.74, 13217.82, None, 12623.71, 12572.42], FRIDAY_CLOSE)
    after_the_close = datetime(2026, 10, 9, 22, 30, tzinfo=UTC)

    observations = parse_yahoo(body, after_the_close)

    assert [o.day for o in observations] == [date(2026, 10, d) for d in (5, 6, 8, 9)]
    assert observations[-1].value == pytest.approx(12572.42)


def test_a_session_still_trading_is_not_a_close() -> None:
    body = yahoo([13172.74, 13217.82, 13066.15, 12623.71, 12600.0], FRIDAY_CLOSE)
    during_friday = datetime(2026, 10, 9, 17, 0, tzinfo=UTC)  # 13:00 New York

    assert parse_yahoo(body, during_friday)[-1].day == date(2026, 10, 8)


def test_yahoo_errors_are_errors() -> None:
    body = {"chart": {"result": None, "error": {"code": "Not Found", "description": "No data found"}}}

    with pytest.raises(SourceError, match="Not Found"):
        parse_yahoo(body, datetime(2026, 10, 9, tzinfo=UTC))


SOX = next(i for i in INDICATORS if i.name == "sox")


def closes(*pairs: tuple[int, float]) -> list[Observation]:
    """Closes on October days of 2026."""
    return [Observation(date(2026, 10, d), v) for d, v in pairs]


def test_the_move_is_from_what_was_known_on_the_morning_of_the_krx_day_before() -> None:
    # Monday the 12th: Friday's US session is new since Friday morning.
    reading = Reading.of(SOX, closes((8, 12623.71), (9, 12572.42)), date(2026, 10, 12), date(2026, 10, 9))

    assert reading.current == Observation(date(2026, 10, 9), 12572.42)
    assert reading.change_pct == pytest.approx(-0.4063, abs=1e-4)


def test_a_us_holiday_adds_no_move_and_a_krx_holiday_adds_all_the_sessions_since() -> None:
    # No US session on Monday the 12th: Tuesday the 13th learns nothing new overnight.
    us_holiday = Reading.of(SOX, closes((8, 100.0), (9, 98.0)), date(2026, 10, 13), date(2026, 10, 12))
    assert us_holiday.change_pct == 0.0
    # KRX closed on the 13th and 14th: Thursday the 15th sees three US sessions at once.
    sessions = closes((9, 100.0), (12, 101.0), (13, 102.0), (14, 104.0))
    krx_holiday = Reading.of(SOX, sessions, date(2026, 10, 15), date(2026, 10, 12))
    assert krx_holiday.change_pct == pytest.approx(4.0)


def test_a_value_older_than_a_week_is_missing() -> None:
    stale = Reading.of(SOX, closes((1, 100.0), (2, 101.0)), date(2026, 10, 12), date(2026, 10, 9))

    assert stale.current is None
    assert stale.change_pct is None
