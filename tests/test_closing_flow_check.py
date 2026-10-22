"""``krx-quant check-closing-flow`` (#305): what the closing flow kept, read from the server's files.

Input: the net assets of two days (one product missing on the second, the first fetched at 07:50),
the 15:20 rows, the days computed, the one-minute bars and the recorded closing prints of two
days (one equal to the bar's value, one not). Every value is a sentinel number. Output: the
printed report, carrying counts, dates and times, none of the sentinels.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time
from pathlib import Path

import pytest

from krx_quant import cli
from krx_quant.closing_flow import keep_days, write_demand
from krx_quant.feeds.base import KST
from krx_quant.net_assets import KIS, write_net_assets
from krx_quant.products import products_of
from tests.test_closing_flow_api import CALENDAR, bars, engine_at, record_print

TUESDAY, WEDNESDAY, THURSDAY = date(2026, 10, 20), date(2026, 10, 21), date(2026, 10, 22)
ASSETS = 12_345_600_000_000  # sentinels: the net assets, the prices and the volumes
PRICE, VOLUME = 87_600, 4_321


def at(day: date, hhmm: str) -> datetime:
    return datetime.combine(day, time.fromisoformat(hhmm), KST)


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)  # no .env
    monkeypatch.setenv("KRX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KRX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("KRX_SYMBOLS", "005930")
    (tmp_path / "state").mkdir()
    days = {d.isoformat(): v for d, v in CALENDAR.known().items()}
    (tmp_path / "state" / "market_calendar.json").write_text(
        json.dumps({"refreshed_on": None, "open_days": days})
    )
    return tmp_path


async def test_the_report_counts_the_days_and_times_without_a_value(
    server: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = server / "state"
    codes = [p.code for p in products_of("005930")]
    write_net_assets(
        state,
        [
            {
                "code": c,
                "date": TUESDAY,
                "source": KIS,
                "net_assets": ASSETS,
                "fetched_at": at(WEDNESDAY, "07:50"),
            }
            for c in codes
        ]
        + [
            {
                "code": c,
                "date": WEDNESDAY,
                "source": KIS,
                "net_assets": ASSETS,
                "fetched_at": at(THURSDAY, "08:10"),
            }
            for c in codes[1:]
        ],
    )
    write_demand(
        state,
        [
            {
                "symbol": "005930",
                "date": WEDNESDAY,
                "at": at(WEDNESDAY, "15:20"),
                "price": float(PRICE),
                "previous_close": float(PRICE),
                "r": 0.0,
                "demand": 0,
                "spot_futures": 0,
                "futures": 0,
                "products": 8,
                "missing": 0,
            }
        ],
    )
    for day in (TUESDAY, WEDNESDAY, THURSDAY):
        bars(server, "005930", day, PRICE, PRICE, PRICE + 100, VOLUME)
    record_print(server, "005930", WEDNESDAY, PRICE + 100, VOLUME)  # the bar's value
    record_print(server, "005930", THURSDAY, PRICE + 100, 2 * VOLUME)  # twice it
    engine, _ = engine_at(server, at(THURSDAY, "21:00"))
    await keep_days(server, state, CALENDAR, ("005930",), engine.store.special_windows, at(THURSDAY, "20:40"))

    assert cli.main(["check-closing-flow"]) == 0

    out = capsys.readouterr().out
    assert out.startswith("KRX 마감 흐름 확인 (#305)")
    assert "  2026-10-20: 8/8종 · 10-21 07:50:00 – 10-21 07:50:00" in out
    assert f"  2026-10-21: 7/8종 · 10-22 08:10:00 – 10-22 08:10:00 · 없음 {codes[0]}" in out
    assert (
        "  005930: 1일 2026-10-21 – 2026-10-21 · 센 상품 8종 1일 · 시각 15:20:00 1일 · 수요 없음 0일" in out
    )
    assert (
        "  005930: 2일 2026-10-21 – 2026-10-22 · 수요 출처 bars 1일 · live 1일"
        " · 체결대금 출처 bar 1일 · recorded 2일" in out
    )
    assert (
        "  005930: 둘 다 2일 2026-10-21 – 2026-10-22 · 같음 1 · 다름 1 · 비율 0.500 – 1.000 · 1분봉만 1일"
        in out
    )
    for sentinel in ("87600", "87,600", "4321", "4,321", "123456"):
        assert sentinel not in out.replace(" ", ""), sentinel


def test_an_empty_server_says_so(server: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["check-closing-flow"]) == 0

    out = capsys.readouterr().out
    assert "1. 순자산 (state/closing_flow/net_assets.parquet): 행 0\n  없음" in out
    assert "3. 지난날 표 (state/closing_flow/days.json): 없음(아직 계산하지 않았다)" in out
    assert "  005930: 둘 다 있는 날 없음 (1분봉만 0일)" in out
