"""``krx-quant demo-engine``: a made-up morning through the real engine and Engine API, for
building screens without the server.

Input: nothing but a database path. Output: the Engine API responses a screen reads.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from krx_quant.algorithms import ALGORITHMS
from krx_quant.cli import main
from krx_quant.consensus import NAMES as CONSENSUS
from krx_quant.demo import DEMO_SYMBOLS, demo_app
from krx_quant.macro import INDICATORS
from tests.api_schema import check
from tests.replay_events import get_json
from tests.test_api_stream import receive_until


async def test_the_demo_engine_answers_what_a_screen_reads(tmp_path: Path) -> None:
    app = await demo_app(tmp_path / "demo.sqlite3")

    async with TestClient(TestServer(app)) as client:
        status = await get_json(client, "/v1/status")
        symbols = await get_json(client, "/v1/symbols")
        latest = {s: await get_json(client, f"/v1/symbols/{s}/latest") for s in DEMO_SYMBOLS}
        macro = await get_json(client, "/v1/macro")
        newton = await get_json(client, "/v1/algorithms/newton")

    assert status["mode"] == "demo"
    assert status["phase"] == "continuous"
    assert status["last_bar_close"] == "2026-10-12T10:30:00+09:00"
    assert [w["kind"] for w in status["special"]] == ["circuit_breaker", "vi"], "so a screen shows them"
    assert status["training"]["day"] == "2026-10-09"
    assert [s["symbol"] for s in symbols["symbols"]] == list(DEMO_SYMBOLS)
    assert (macro["day"], macro["regime"]) == ("2026-10-12", "risk_off"), "a macro row to show"
    assert len(macro["readings"]) == len(INDICATORS)
    usd = status["usd_futures"]
    assert usd["previous_close"] == 1392.2 and usd["change_pct"] is not None, "the USD futures move to show"
    assert status["kis"]["connected"] is True
    assert [f["key"] for f in status["kis"]["feeds"] if f["gap"]] == ["000660"], (
        "a gap, so a screen shows one"
    )
    assert [h["why"] for h in newton["model_history"]] == ["fit", "rollback from newton-20261008-5e1f0a"]
    assert all(s["mid"] is not None for s in symbols["symbols"])
    for body in latest.values():
        assert body["bar_close"] == "2026-10-12T10:30:00+09:00"
        made = {p["algorithm"]: p for p in body["predictions"]}
        assert set(made) >= {a.name for a in ALGORITHMS} | set(CONSENSUS)
        assert all(made[name]["status"] == "ok" for name in CONSENSUS), "the algorithms are active"


async def test_the_demo_morning_halts_for_a_circuit_breaker_and_has_a_vi(tmp_path: Path) -> None:
    """#236. Output: the special windows a chart shows (the circuit breaker's first stage from
    09:06 to 09:36 for every symbol, 000660's VI from 09:40:10 to 09:42:20), and no trade
    in the halt: the minute bars skip from 09:05 to 09:36."""
    app = await demo_app(tmp_path / "demo.sqlite3")

    async with TestClient(TestServer(app)) as client:
        special = {s: await get_json(client, f"/v1/symbols/{s}/special") for s in DEMO_SYMBOLS}
        bars = {s: await get_json(client, f"/v1/symbols/{s}/bars") for s in DEMO_SYMBOLS}

    breaker = {
        "kind": "circuit_breaker",
        "symbol": None,
        "start": "2026-10-12T09:06:00+09:00",
        "end": "2026-10-12T09:36:00+09:00",
        "detail": "1단계",
    }
    vi = {
        "kind": "vi",
        "symbol": "000660",
        "start": "2026-10-12T09:40:10+09:00",
        "end": "2026-10-12T09:42:20+09:00",
        "detail": "dynamic",
    }
    assert special["000660"] == {
        "symbol": "000660",
        "from": "2026-10-05",
        "to": "2026-10-12",
        "special": [breaker, vi],
    }
    assert special["005930"]["special"] == special["009150"]["special"] == [breaker]
    for symbol, body in bars.items():
        starts = [b["start"][11:16] for b in body["bars"]]
        assert [s for s in starts if "09:06" <= s < "09:36"] == [], symbol
        assert starts[5:7] == ["09:05", "09:36"], symbol
        assert all(b["volume"] > 0 for b in body["bars"])


async def test_the_demo_scrolls_back_into_two_earlier_afternoons(tmp_path: Path) -> None:
    app = await demo_app(tmp_path / "demo.sqlite3")
    before = "before=2026-10-12T09:00:00%2B09:00"

    async with TestClient(TestServer(app)) as client:
        bars = await get_json(client, f"/v1/symbols/005930/bars?{before}&limit=2000")
        made = await get_json(
            client, f"/v1/symbols/005930/predictions?algorithm=consensus-final&{before}&limit=1000"
        )

    starts = [b["start"] for b in bars["bars"]]
    # 14:00–15:19 of the 7th and the 8th (Friday the 9th is Hangul Day), oldest first.
    assert (len(starts), starts[0], starts[-1]) == (
        160,
        "2026-10-07T14:00:00+09:00",
        "2026-10-08T15:19:00+09:00",
    )
    assert starts[79:81] == ["2026-10-07T15:19:00+09:00", "2026-10-08T14:00:00+09:00"]
    closes = [p["bar_close"] for p in made["predictions"]]
    assert (len(closes), closes[0], closes[-1]) == (
        154,
        "2026-10-08T15:16:00+09:00",
        "2026-10-07T14:00:00+09:00",
    )
    assert all(p["status"] == "ok" and p["outcome"] and p["pooled"] for p in made["predictions"]), (
        "made by active members and scored, as on the morning"
    )


async def test_the_demo_has_two_years_of_day_candles_before_the_morning(tmp_path: Path) -> None:
    app = await demo_app(tmp_path / "demo.sqlite3")

    async with TestClient(TestServer(app)) as client:
        latest = await get_json(client, "/v1/symbols/005930/candles?period=day")
        older = await get_json(
            client, "/v1/symbols/005930/candles?period=day&before=2026-10-07T00:00:00%2B09:00&limit=2000"
        )

    dates = [c["date"] for c in latest["candles"]]
    assert (latest["venue"], len(dates), dates[-2:]) == ("UN", 400, ["2026-10-07", "2026-10-08"])
    assert len(older["candles"]) == 498 and older["candles"][-1]["date"] == "2026-10-06"
    last = latest["candles"][-1]
    assert last["low"] <= min(last["open"], last["close"]) <= max(last["open"], last["close"]) <= last["high"]
    assert abs(last["close"] - 81_250) <= 81_250 * 0.001, "the last close is near the morning's open"


async def test_the_demo_has_finished_weeks_months_and_years_made_of_its_days(tmp_path: Path) -> None:
    app = await demo_app(tmp_path / "demo.sqlite3")

    async with TestClient(TestServer(app)) as client:
        days = await get_json(client, "/v1/symbols/005930/candles?period=day&limit=2000")
        weeks = await get_json(client, "/v1/symbols/005930/candles?period=week&limit=2000")
        months = await get_json(client, "/v1/symbols/005930/candles?period=month")
        years = await get_json(client, "/v1/symbols/005930/candles?period=year")

    # The morning (Monday the 12th) is in a new week: the 8th's is the last one over.
    assert weeks["candles"][-1]["date"] == "2026-10-05"
    assert months["candles"][-1]["date"] == "2026-09-01"
    assert [c["date"] for c in years["candles"]] == ["2024-01-01", "2025-01-01"]
    last_week = [c for c in days["candles"] if "2026-10-05" <= c["date"] <= "2026-10-08"]
    assert weeks["candles"][-1]["close"] == last_week[-1]["close"]
    assert weeks["candles"][-1]["volume"] == sum(c["volume"] for c in last_week)


async def test_the_demo_has_daily_supply_on_its_candles_days_but_the_morning_s(tmp_path: Path) -> None:
    """#226. Output: every symbol's eight daily values on the 500 candle days, none on the
    morning (its day is not over), so a chart shows them in every view."""
    app = await demo_app(tmp_path / "demo.sqlite3")

    async with TestClient(TestServer(app)) as client:
        supply = {
            s: await get_json(client, f"/v1/symbols/{s}/supply?from=2016-01-01&to=2026-10-12")
            for s in DEMO_SYMBOLS
        }
        candles = await get_json(client, "/v1/symbols/005930/candles?period=day&limit=2000")

    for symbol, body in supply.items():
        check("supply", body)
        days = body["days"]
        assert [d["date"] for d in days] == [c["date"] for c in candles["candles"]], symbol
        assert all(v is not None for d in days for v in d.values()), "every value made up"
        assert any(d["foreign_net"] < 0 for d in days) and any(d["foreign_net"] > 0 for d in days)
        assert all(0 < d["foreign_ratio"] < 100 for d in days)


async def test_the_demo_has_the_daily_kinds_days_and_its_morning(tmp_path: Path) -> None:
    """#264. Output: open_to_close's last 20 days up to the 8th, each with the three baselines' and
    Cox's 08:50 predictions and the day's answer, and the morning's predictions waiting for theirs."""
    app = await demo_app(tmp_path / "demo.sqlite3")

    async with TestClient(TestServer(app)) as client:
        kinds = await get_json(client, "/v1/kinds")
        latest = await get_json(client, "/v1/symbols/005930/kinds/open_to_close/latest")
        made = await get_json(client, "/v1/symbols/005930/kinds/open_to_close/predictions?limit=21")
        board = await get_json(client, "/v1/leaderboard?kind=open_to_close&period=20d")
        cox = await get_json(client, "/v1/algorithms/cox")

    daily = next(k for k in kinds["kinds"] if k["name"] == "open_to_close")
    assert daily["daily"]["predict_at"] == "08:50"
    assert latest["bar_close"] == "2026-10-12T08:50:00+09:00"
    assert {p["algorithm"] for p in latest["predictions"]} == {
        "cox",
        "otc-flat",
        "otc-climate",
        "otc-persist",
    }
    assert all(p["answer"] is None for p in latest["predictions"]), "the morning's day is not over"
    days = sorted({p["bar_close"] for p in made["predictions"]})
    assert len(days) == 21 and days[-2] == "2026-10-08T08:50:00+09:00"
    answered = [p for p in made["predictions"] if p["bar_close"] < "2026-10-12"]
    assert answered and all(p["answer"] is not None for p in answered)
    for p in answered:
        check("open_to_close_path", p["answer"]["path"])
    assert {e["algorithm"] for e in board["baselines"]} == {"otc-flat", "otc-climate", "otc-persist"}
    assert any(e["algorithm"] == "cox" and e["n"] > 0 for e in board["unranked"] + board["ranked"])
    assert cox["kind"] == "open_to_close" and cox["model"]["version"].startswith("cox-2026")


async def test_the_demo_has_the_five_session_kind_s_windows(tmp_path: Path) -> None:
    """#310. Output: open_to_close_5d's windows begun on the last 20 days up to the 8th, each with
    its three baselines' 08:50 predictions; those ended by the 8th answered at their end day's
    20:35, the four begun from the 5th and the morning's waiting for their end; and the windows over
    005930's made-up interim ex-dividend day (2026-09-29, 락 구분 05) marked ``ex_dividend``."""
    app = await demo_app(tmp_path / "demo.sqlite3")

    async with TestClient(TestServer(app)) as client:
        kinds = await get_json(client, "/v1/kinds")
        latest = await get_json(client, "/v1/symbols/005930/kinds/open_to_close_5d/latest")
        made = await get_json(client, "/v1/symbols/005930/kinds/open_to_close_5d/predictions?limit=21")
        board = await get_json(client, "/v1/leaderboard?kind=open_to_close_5d&period=20d")

    daily = next(k for k in kinds["kinds"] if k["name"] == "open_to_close_5d")["daily"]
    assert daily["awaiting"] is None and daily["review_hold"] is not None
    baselines = {"otc5-flat", "otc5-climate", "otc5-persist"}
    assert latest["bar_close"] == "2026-10-12T08:50:00+09:00"
    assert {p["algorithm"] for p in latest["predictions"]} == baselines, "cox-5d is held"
    assert all(p["target_at"] == "2026-10-16T15:30:00+09:00" for p in latest["predictions"])
    assert len({p["bar_close"] for p in made["predictions"]}) == 21
    waiting = sorted({p["bar_close"][:10] for p in made["predictions"] if p["answer"] is None})
    assert waiting == ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-12"]
    answered = [p for p in made["predictions"] if p["answer"] is not None]
    for p in answered:
        check("open_to_close_5d_path", p["answer"]["path"])
        assert p["answer"]["scored_at"] == f"{p['answer']['path']['end_day']}T20:35:00+09:00"
    marked = sorted({p["bar_close"][:10] for p in answered if p["answer"]["path"]["ex_dividend"]})
    assert marked == ["2026-09-23", "2026-09-24", "2026-09-25", "2026-09-28"], "sessions 2–5 hold the 29th"
    assert all(
        p["answer"]["path"]["ex_dividend"] is False for p in answered if p["bar_close"][:10] not in marked
    )
    assert {e["algorithm"] for e in board["baselines"]} == baselines


async def test_the_demo_has_a_briefing_of_its_morning(tmp_path: Path) -> None:
    """#304. Output: the night begun on the 8th, each symbol's pre-market and the auction's last
    expected price, the morning's economic calendar, US closes and Cox's 08:50 inputs."""
    app = await demo_app(tmp_path / "demo.sqlite3")

    async with TestClient(TestServer(app)) as client:
        body = await get_json(client, "/v1/briefing")

    check("briefing", body)
    assert (body["day"], body["previous_day"]) == ("2026-10-12", "2026-10-08")
    assert body["night_futures"]["recorded"] and body["night_futures"]["evening"] == "2026-10-08"
    for symbol in body["symbols"]:
        assert symbol["premarket"]["window"] == "08:40-08:50"
        assert symbol["expected_open"]["final"] is True, "the clock is past the open"
        assert symbol["after_hours"] is None, "the 8th's bars end at 15:20"
    assert [e["country"] for e in body["schedule"]["events"]] == ["KR", "US"]
    assert all(len(u["closes"]) == 5 for u in body["overnight"]["us"])
    assert body["overnight"]["macro"]["regime"] == "risk_off"
    otc = body["open_to_close"]
    assert otc["day"] == "2026-10-12" and not otc["previous"]
    for symbol in otc["symbols"]:
        [inputs] = symbol["inputs"]
        assert inputs["algorithm"] == "cox" and inputs["bias"] is not None


async def test_the_demo_has_its_morning_s_flows(tmp_path: Path) -> None:
    """#306. Output: each symbol's program trading and member firms through the morning, nothing
    during the halt, the REST stand-ins from 10:00 to 10:10, two estimate slots, and the 8th's
    totals at 10:30."""
    app = await demo_app(tmp_path / "demo.sqlite3")

    async with TestClient(TestServer(app)) as client:
        body = await get_json(client, "/v1/symbols/000660/flow")

    check("flow", body)
    assert (body["day"], body["recorded"]) == ("2026-10-12", True)
    minutes = {m["at"][11:16]: m for m in body["minutes"]}
    assert minutes["09:00"]["program_source"] == "ws" and minutes["09:00"]["program_net_value"] is not None
    assert minutes["09:20"]["program_net_value"] is None, "the circuit breaker's halt"
    assert (minutes["10:05"]["program_source"], minutes["10:05"]["member_source"]) == ("rest", "rest")
    assert minutes["11:00"]["program_net_value"] is None, "the clock stays at 10:30"
    assert [e["slot"] for e in body["estimates"]] == ["1", "2"]
    assert len(body["members"]["sell"]) == 5 and any(m["foreign"] for m in body["members"]["buy"])
    assert body["previous_day_same_time"]["at"] == "2026-10-08T10:30:00+09:00"
    assert body["previous_day_same_time"]["program_net_value"] is not None
    assert [w["kind"] for w in body["special"]] == ["circuit_breaker", "vi"]


async def test_the_demo_has_a_cost_map(tmp_path: Path) -> None:
    """#307. Output: each symbol's card at its mid-point at 10:30, a row each kind with a
    direction (the range has none), every row's breakeven but the daily kind's, and the made-up
    base rates of the four periods."""
    app = await demo_app(tmp_path / "demo.sqlite3")

    async with TestClient(TestServer(app)) as client:
        body = await get_json(client, "/v1/cost-map?period=20d")

    check("cost_map", body)
    cards = {s["symbol"]: s for s in body["symbols"]}
    assert all(s["live"] and s["costs"] is not None for s in cards.values())
    assert cards["009150"]["costs"]["inverse"] is None
    assert cards["000660"]["costs"]["below"]["edge"] == 200_000
    rows = {r["kind"]: r for r in body["rows"]}
    assert list(rows) == [
        "direction",
        "barrier",
        "barrier_15m",
        "barrier_30m",
        "barrier_60m",
        "open_to_close",
        "open_to_close_5d",
    ]
    assert all(r["breakeven"] is not None for k, r in rows.items() if not k.startswith("open_to_close"))
    assert rows["direction"]["consensus"]["algorithm"] == "consensus-final"
    assert rows["open_to_close"]["unit"] == "bp" and rows["open_to_close"]["bar_closes"] > 0
    regimes = body["regimes"]
    assert len(regimes["rows"]) == 12, "three symbols, four periods"


# Two demos built: about 9 s on a laptop, near the 30 s default (``pyproject.toml``) on a loaded
# CI runner (#327).
@pytest.mark.timeout(120)
async def test_the_demo_has_its_closing_flow(tmp_path: Path) -> None:
    """#305. Output: the morning's demand at 10:30 by the engine's price, its products named, one
    without its net assets; the 20 days before, the last 8 with their stored 15:20 demand, the
    others from the bars, a VI day, the first five without a recorded closing auction value, and
    009150 without demand."""
    app = await demo_app(tmp_path / "demo.sqlite3")

    async with TestClient(TestServer(app)) as client:
        today = await get_json(client, "/v1/symbols/000660/closing-flow")
        days = await get_json(client, "/v1/symbols/000660/closing-flow/days")
        control = await get_json(client, "/v1/symbols/009150/closing-flow")

    check("closing_flow", today)
    check("closing_flow_days", days)
    assert (today["day"], today["at"], today["source"]) == ("2026-10-12", "2026-10-12T10:30:00+09:00", "live")
    assert today["demand"] is not None and today["ratio"] is not None
    assert today["denominator_days"] == 15, "the 20 days before, but the five without a recorded print"
    assert today["missing"] == ["0198D0"]
    assert all(p["name"] for p in today["products"]) and len(today["products"]) == 8
    rows = days["days"]
    assert len(rows) == 20 and rows[-1]["date"] == "2026-10-08"
    assert [r["demand_source"] for r in rows].count("live") == 8
    assert {r["demand_source"] for r in rows[:-8]} == {"bars"}
    assert rows[-1]["auction_value_source"] == "recorded" and rows[-1]["next_gap_bp"] is None
    assert [r["auction_value"] for r in rows[:5]] == [None] * 5
    assert rows[0]["denominator_reason"] == "no_recorded_auction" and rows[-1]["denominator_days"] == 14
    assert [r["date"] for r in rows if r["special"]] == ["2026-09-24"]
    assert len(days["summary"]["tertiles"]) == 3
    assert (control["demand"], control["reason"]) == (None, "no_products")


async def test_the_demo_is_the_same_every_time(tmp_path: Path) -> None:
    first, second = await demo_app(tmp_path / "a.sqlite3"), await demo_app(tmp_path / "b.sqlite3")

    async with TestClient(TestServer(first)) as a, TestClient(TestServer(second)) as b:
        for path in (
            "/v1/symbols",
            "/v1/symbols/005930/predictions?limit=20&algorithm=newton",
            "/v1/symbols/005930/bars?limit=500",
            "/v1/symbols/005930/candles?period=day",
            "/v1/symbols/005930/supply",
            "/v1/symbols/005930/kinds/open_to_close/predictions?limit=21",
            "/v1/briefing",
            "/v1/symbols/000660/flow",
            "/v1/symbols/000660/closing-flow",
            "/v1/symbols/005930/closing-flow/days",
            "/v1/cost-map?period=20d",
        ):
            assert await get_json(a, path) == await get_json(b, path)


# Two demos built, one of them paced: about 10 s on a laptop, near the 30 s default
# (``pyproject.toml``) on a loaded CI runner (#327).
@pytest.mark.timeout(120)
async def test_a_paced_demo_streams_its_last_half_hour_as_it_goes(tmp_path: Path) -> None:
    paced = await demo_app(tmp_path / "paced.sqlite3", pace=0.05)

    async with TestClient(TestServer(paced)) as client:
        ws = await client.ws_connect("/v1/stream")
        events = await receive_until(ws, _a_final_consensus_and_an_outcome_seen(), timeout=30)
        await ws.close()
        status = await _until_the_demo_ends(client)
        made = await _until_the_morning_is_stored(client)

    assert any(e["type"] == "prediction" for e in events)
    assert all(e["at"] >= "2026-10-12T10:00:00+09:00" for e in events if e["type"] != "health"), "after 10:00"
    assert status["last_bar_close"] == "2026-10-12T10:30:00+09:00", "it goes on to 10:30, then stays"
    unpaced = await demo_app(tmp_path / "unpaced.sqlite3")
    async with TestClient(TestServer(unpaced)) as client:
        assert await get_json(client, "/v1/symbols/005930/predictions?limit=40&algorithm=newton") == made, (
            "the same morning as without the pace"
        )


async def test_a_paced_demo_moves_the_bar_being_made_within_its_minute(tmp_path: Path) -> None:
    paced = await demo_app(tmp_path / "paced.sqlite3", pace=3)  # 10 s of the morning every half second

    async with TestClient(TestServer(paced)) as client:
        ws = await client.ws_connect("/v1/stream")
        bars: list[dict[str, Any]] = []

        def two_of_one_minute(event: dict[str, Any]) -> bool:
            if event["type"] == "bar" and event["symbol"] == "005930":
                bars.append(event["bar"])
            return len(bars) >= 2 and bars[-1]["start"] == bars[-2]["start"]

        await receive_until(ws, two_of_one_minute, timeout=15)
        await ws.close()

    assert bars[-1]["start"] >= "2026-10-12T10:00:00+09:00"
    assert bars[-1]["volume"] > bars[-2]["volume"], "the same minute's bar, grown"


async def test_a_paced_demo_starts_at_10(tmp_path: Path) -> None:
    paced = await demo_app(tmp_path / "paced.sqlite3", pace=60)

    async with TestClient(TestServer(paced)) as client:
        status = await get_json(client, "/v1/status")

    assert status["now"] == "2026-10-12T10:00:00+09:00"
    assert status["last_bar_close"] == "2026-10-12T10:00:00+09:00"


def _a_final_consensus_and_an_outcome_seen() -> Callable[[dict[str, Any]], bool]:
    seen: set[tuple[str, str | None]] = set()

    def done(event: dict[str, Any]) -> bool:
        seen.add((event["type"], event.get("kind")))
        return {("consensus", "final"), ("outcome", None)} <= seen

    return done


async def _until_the_demo_ends(client: TestClient[Any, Any]) -> dict[str, Any]:
    async with asyncio.timeout(30):
        while True:
            status: dict[str, Any] = await get_json(client, "/v1/status")
            if status["now"] >= "2026-10-12T10:30:00+09:00":
                return status
            await asyncio.sleep(0.05)


async def _until_the_morning_is_stored(client: TestClient[Any, Any]) -> dict[str, Any]:
    """Newton's predictions of 005930 once the paced morning's last bar close is stored: its 10:30
    prediction, and the outcome of every one made whose four minutes ended by 10:30. The clock reaching
    10:30 is not that: the bar close's predictions are made and stored after it (#337 CI)."""
    path = "/v1/symbols/005930/predictions?limit=40&algorithm=newton"
    async with asyncio.timeout(30):
        while True:
            made: dict[str, Any] = await get_json(client, path)
            listed = made["predictions"]
            last = "2026-10-12T10:30:00+09:00"
            if (
                listed
                and listed[0]["bar_close"] == last
                and all(
                    p["outcome"] is not None for p in listed if p["status"] == "ok" and p["target_at"] <= last
                )
            ):
                return made
            await asyncio.sleep(0.05)


@pytest.mark.parametrize("name", ["engine.sqlite3", "engine.sqlite3-wal", "ENGINE.SQLITE3"])
async def test_the_demo_never_replaces_the_live_engines_database(tmp_path: Path, name: str) -> None:
    live = tmp_path / name
    live.write_text("live")

    with pytest.raises(ValueError, match="live engine"):
        await demo_app(live)

    assert live.read_text() == "live"


def test_a_taken_port_is_a_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    taken = socket.socket()
    taken.bind(("127.0.0.1", 0))
    taken.listen()
    port = taken.getsockname()[1]
    monkeypatch.chdir(tmp_path)
    try:
        code = main(["demo-engine", "--port", str(port), "--db", str(tmp_path / "demo.sqlite3")])
    finally:
        taken.close()

    assert code == 1
    assert f"demo Engine API not started on 127.0.0.1:{port}" in capsys.readouterr().err


@pytest.mark.parametrize("pace", ["0", "-1", "nan", "inf", "1e10000"])
def test_a_pace_is_a_finite_number_of_seconds_more_than_0(
    tmp_path: Path, pace: str, capsys: pytest.CaptureFixture[str]
) -> None:
    # An infinite pace would leave the demo at 10:00 for good, streaming nothing.
    with pytest.raises(SystemExit) as exit:
        main(["demo-engine", "--pace", pace, "--db", str(tmp_path / "demo.sqlite3")])

    assert exit.value.code == 2, "a wrong argument, as the README's exit codes say"
    assert "seconds per minute of the demo morning: a finite number more than 0" in capsys.readouterr().err
