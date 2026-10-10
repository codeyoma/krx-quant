"""Macro sources' answers read into daily observations.

Input: answers shaped as ECOS, FRED and Yahoo send them (taken from live answers on
2026-10-10), including no data, missing values, a session still trading and errors.
Output: the observations, oldest first, or the source's error.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from krx_quant.macro import Observation, SourceError, change_pct, parse_ecos, parse_fred, parse_yahoo


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


def test_the_change_is_from_the_close_before() -> None:
    observations = [Observation(date(2026, 10, 8), 12623.71), Observation(date(2026, 10, 9), 12572.42)]

    assert change_pct(observations) == pytest.approx(-0.4063, abs=1e-4)
    assert change_pct(observations[:1]) is None
