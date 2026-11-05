"""Reconnects, gap records and alerts, observed through the fake KIS seam, and the REST polls
kept to the hours they answer in (#488)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from krx_quant import collector
from krx_quant.collector import rest_plan, run_collector
from krx_quant.config import Settings
from krx_quant.fake_kis import FakeKis
from krx_quant.feeds import FOREIGN_ESTIMATE, KST, VI_STATUS
from krx_quant.market_calendar import MarketCalendar
from tests.collector_doubles import FIXED_NOW, TRADE_KEY, Clock, read, settings_for
from tests.kis_frames import book_frame, trade_frame, us_trade_frame
from tests.waiting import until

DOWN = "KIS WebSocket down"  # each failed connection's WARNING
SHORT = "not alerted"  # a drop back before its alert's WARNING (#488)


def fast(settings: Settings) -> Settings:
    return replace(settings, reconnect_initial_seconds=0.05, reconnect_max_seconds=0.2)


def alert_after(monkeypatch: pytest.MonkeyPatch, seconds: float) -> None:
    """A drop is alerted once it is ``seconds`` old with the connection not back (#488; 30 s on
    the server, ``collector.WS_ALERT_AFTER_SECONDS``)."""
    monkeypatch.setattr(collector, "WS_ALERT_AFTER_SECONDS", seconds)


def logged(caplog: pytest.LogCaptureFixture, text: str) -> list[str]:
    return [m for r in caplog.records if text in (m := r.getMessage())]


def fill(price: str, symbol: str = "005930") -> str:
    return trade_frame(
        {"MKSC_SHRN_ISCD": symbol, "STCK_CNTG_HOUR": "093015", "STCK_PRPR": price, "BSOP_DATE": "20261008"}
    )


async def test_reconnects_and_restores_every_subscription_after_kis_drops_the_connection(
    kis: FakeKis, tmp_path: Path
) -> None:
    kis.script[TRADE_KEY] = [fill("81200")]
    stop = asyncio.Event()
    task = asyncio.create_task(
        run_collector(fast(settings_for(kis, tmp_path)), stop=stop, clock=lambda: FIXED_NOW)
    )
    await kis.wait_for_subscription_count(TRADE_KEY, 1)
    await kis.round_trip()  # every first-round registration has arrived
    first_round = [(r["body"]["input"]["tr_id"], r["body"]["input"]["tr_key"]) for r in kis.subscriptions]

    kis.script[TRADE_KEY] = [fill("81300")]
    await kis.close_connections()
    await kis.wait_for_subscription_count(TRADE_KEY, 2)
    await kis.round_trip()
    stop.set()
    await asyncio.wait_for(task, 5)

    assert kis.ws_connections == 2
    second_round = [
        (r["body"]["input"]["tr_id"], r["body"]["input"]["tr_key"])
        for r in kis.subscriptions[len(first_round) :]
    ]
    assert second_round == first_round
    assert [t["price"] for t in read(tmp_path, "events", "H0UNCNT0")] == [81200, 81300]


async def test_a_dropped_connection_is_recorded_as_a_gap_for_every_subscription(
    kis: FakeKis, tmp_path: Path
) -> None:
    kis.script[TRADE_KEY] = []
    clock = Clock(FIXED_NOW)
    stop = asyncio.Event()
    task = asyncio.create_task(run_collector(fast(settings_for(kis, tmp_path)), stop=stop, clock=clock))
    await kis.wait_for_subscription_count(TRADE_KEY, 1)

    clock.at = FIXED_NOW + timedelta(seconds=10)
    kis.accept_ws = False
    await kis.close_connections()
    await asyncio.sleep(0.3)  # a few refused reconnect attempts
    clock.at = FIXED_NOW + timedelta(seconds=40)
    kis.accept_ws = True
    await kis.wait_for_subscription_count(TRADE_KEY, 2)
    await kis.round_trip()
    stop.set()
    await asyncio.wait_for(task, 5)

    gaps = read(tmp_path, "gaps", "H0UNCNT0")
    assert [(g["start"], g["end"]) for g in gaps] == [(clock.at - timedelta(seconds=30), clock.at)]
    assert "disconnected" in gaps[0]["reason"]
    assert {g["feed"] for g in read(tmp_path, "gaps", "H0UNASP0")} == {"H0UNASP0"}


def with_telegram(kis: FakeKis, settings: Settings) -> Settings:
    return replace(
        settings,
        telegram_bot_token="123:fake",
        telegram_chat_id="42",
        telegram_api_url=kis.rest_url,
        recovery_hold_seconds=0.05,
    )


async def drop_and_return(
    kis: FakeKis, clock: Clock, caplog: pytest.LogCaptureFixture, down: datetime, up: datetime, n: int
) -> None:
    """KIS closes the connection at ``down`` and refuses new ones until ``up``, then takes the
    ``n``th and answers its registrations."""
    clock.at = down
    kis.accept_ws = False
    failed = len(logged(caplog, DOWN))
    await kis.close_connections()
    await until(lambda: len(logged(caplog, DOWN)) > failed)  # the drop is marked lost
    clock.at = up
    kis.accept_ws = True
    await kis.wait_for_subscription_count(TRADE_KEY, n)
    await kis.round_trip()


async def test_a_drop_back_within_the_alert_wait_sends_nothing_but_its_gap_and_count_are_kept(
    kis: FakeKis, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Input: KIS closes the connection and takes the next one a moment later, well inside the
    30 s the alert waits (code 1006 at 06:34 every day, data back in 1.7-3.0 s, #488), twice.
    Output: no Telegram message; each drop's gap runs from the drop to the new registration, as
    without the wait; a WARNING counts the day's short drops."""
    kis.script[TRADE_KEY] = []
    clock = Clock(FIXED_NOW)
    stop = asyncio.Event()
    settings = with_telegram(kis, fast(settings_for(kis, tmp_path)))
    task = asyncio.create_task(run_collector(settings, stop=stop, clock=clock))
    await kis.wait_for_subscription_count(TRADE_KEY, 1)

    first = (FIXED_NOW + timedelta(seconds=10), FIXED_NOW + timedelta(seconds=12))
    second = (FIXED_NOW + timedelta(seconds=100), FIXED_NOW + timedelta(seconds=103))
    await drop_and_return(kis, clock, caplog, *first, n=2)
    await until(lambda: len(logged(caplog, SHORT)) == 1)
    await drop_and_return(kis, clock, caplog, *second, n=3)
    await until(lambda: len(logged(caplog, SHORT)) == 2)
    stop.set()
    await asyncio.wait_for(task, 5)

    assert kis.telegram_messages == []
    gaps = read(tmp_path, "gaps", "H0UNCNT0")
    assert [(g["start"], g["end"]) for g in gaps] == [first, second]
    assert all(g["reason"].startswith("disconnected: websocket closed by KIS") for g in gaps), gaps
    short = logged(caplog, SHORT)
    assert all(line.startswith("KIS WebSocket back ") for line in short), short
    assert [line.rsplit("(", 1)[1] for line in short] == [
        "short drop 1 today, #488)",
        "short drop 2 today, #488)",
    ]


async def test_a_drop_not_back_within_the_alert_wait_alerts_once_and_its_reconnect_recovers(
    kis: FakeKis, tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Input: KIS refuses every reconnect for longer than the alert waits (0.2 s here). Output:
    one [장애] however many attempts fail, sent once the wait is over, not at the drop; one
    [복구] once the connection is back and stays up. It is not counted as a short drop."""
    alert_after(monkeypatch, 0.2)
    kis.script[TRADE_KEY] = []
    stop = asyncio.Event()
    settings = with_telegram(kis, fast(settings_for(kis, tmp_path)))
    task = asyncio.create_task(run_collector(settings, stop=stop, clock=lambda: FIXED_NOW))
    await kis.wait_for_subscription_count(TRADE_KEY, 1)

    kis.accept_ws = False
    await kis.close_connections()
    await until(lambda: len(kis.telegram_messages) >= 1)
    await until(lambda: len(logged(caplog, DOWN)) >= 4)  # refused attempts after the alert too
    kis.accept_ws = True
    await kis.wait_for_subscription_count(TRADE_KEY, 2)
    await until(lambda: len(kis.telegram_messages) >= 2)
    stop.set()
    await asyncio.wait_for(task, 5)

    texts = [m["text"] for m in kis.telegram_messages]
    assert len(texts) == 2, texts
    assert texts[0].startswith("[장애] KIS WebSocket 연결이 끊겼습니다 (websocket closed by KIS"), texts
    assert texts[1].startswith("[복구]") and "WebSocket" in texts[1]
    assert {(m["bot_token"], m["chat_id"]) for m in kis.telegram_messages} == {("123:fake", "42")}
    dropped = next(r.created for r in caplog.records if DOWN in r.getMessage())
    [alerted] = [
        r.created for r in caplog.records if r.getMessage().startswith("alert: [장애] KIS WebSocket")
    ]
    assert alerted - dropped >= 0.15, "the alert waited for the drop to be 0.2 s old"
    assert logged(caplog, SHORT) == []


async def test_a_reconnect_after_a_long_session_uses_a_fresh_approval_key(
    kis: FakeKis, tmp_path: Path
) -> None:
    kis.script[TRADE_KEY] = []
    clock = Clock(FIXED_NOW)
    stop = asyncio.Event()
    task = asyncio.create_task(run_collector(fast(settings_for(kis, tmp_path)), stop=stop, clock=clock))
    await kis.wait_for_subscription_count(TRADE_KEY, 1)

    clock.at = FIXED_NOW + timedelta(hours=21)  # approval keys last about a day
    await kis.close_connections()
    await kis.wait_for_subscription_count(TRADE_KEY, 2)
    stop.set()
    await asyncio.wait_for(task, 5)

    assert len(kis.approvals_issued) == 2
    last = [s for s in kis.subscriptions if s["body"]["input"]["tr_id"] == TRADE_KEY[0]][-1]
    assert last["header"]["approval_key"] == kis.approvals_issued[-1]


async def test_a_feed_that_goes_quiet_in_market_hours_is_a_stale_gap_with_one_alert(
    kis: FakeKis, tmp_path: Path
) -> None:
    kis.script[TRADE_KEY] = [fill("81200")]
    clock = Clock(FIXED_NOW)  # Thursday 09:30:16 KST
    stop = asyncio.Event()
    settings = replace(with_telegram(kis, settings_for(kis, tmp_path)), health_check_seconds=0.05)
    task = asyncio.create_task(run_collector(settings, stop=stop, clock=clock))
    await kis.wait_for_subscription_count(TRADE_KEY, 1)
    await kis.round_trip()

    clock.at = FIXED_NOW + timedelta(minutes=5)  # no trades for 5 minutes
    await until(lambda: len(kis.telegram_messages) >= 1)
    await asyncio.sleep(0.2)  # more checks while still stale: no second alert
    clock.at = FIXED_NOW + timedelta(minutes=6)
    await kis.push(fill("81300"))
    await until(lambda: len(kis.telegram_messages) >= 2)
    stop.set()
    await asyncio.wait_for(task, 5)

    texts = [m["text"] for m in kis.telegram_messages]
    assert len(texts) == 2 and texts[0].startswith("[장애]") and "stale H0UNCNT0" in texts[0], texts
    assert texts[1] == "[복구] 데이터가 다시 들어오는 피드: stale H0UNCNT0", texts
    [gap] = [g for g in read(tmp_path, "gaps", "H0UNCNT0") if g["reason"] == "stale"]
    assert (gap["start"], gap["end"]) == (FIXED_NOW, FIXED_NOW + timedelta(minutes=6))


async def test_one_quiet_key_is_only_a_gap_and_the_alert_waits_for_the_whole_feed(
    kis: FakeKis, tmp_path: Path
) -> None:
    other = ("H0UNCNT0", "000660")
    kis.script[TRADE_KEY] = [fill("81200")]
    kis.script[other] = [fill("201000", "000660")]
    clock = Clock(FIXED_NOW)  # Thursday 09:30:16 KST
    stop = asyncio.Event()
    settings = replace(
        with_telegram(kis, settings_for(kis, tmp_path, ("005930", "000660"))), health_check_seconds=0.05
    )
    task = asyncio.create_task(run_collector(settings, stop=stop, clock=clock))
    await kis.wait_for_subscriptions({TRADE_KEY, other})
    await kis.round_trip()

    clock.at = FIXED_NOW + timedelta(minutes=2)
    await kis.push(fill("81300"))
    await kis.round_trip()
    clock.at = FIXED_NOW + timedelta(minutes=4)  # 000660 quiet for 4 minutes, 005930 for 2
    await asyncio.sleep(0.3)
    quiet_one = [m["text"] for m in kis.telegram_messages if "H0UNCNT0" in m["text"]]
    clock.at = FIXED_NOW + timedelta(minutes=6)  # now both
    await until(lambda: any("H0UNCNT0" in m["text"] for m in kis.telegram_messages))
    await kis.push(fill("201500", "000660"))
    await until(lambda: sum("H0UNCNT0" in m["text"] for m in kis.telegram_messages) >= 2)
    stop.set()
    await asyncio.wait_for(task, 5)

    assert quiet_one == [], "one quiet stock is the market, not a fault"
    texts = [m["text"] for m in kis.telegram_messages if "H0UNCNT0" in m["text"]]
    assert texts[0].startswith("[장애] 구독한 키가 모두 끊긴 피드: ") and "stale H0UNCNT0" in texts[0], texts
    assert texts[1:] == ["[복구] 데이터가 다시 들어오는 피드: stale H0UNCNT0"], "one key back: the feed works"
    gaps = {
        (g["symbol"], g["start"], g["end"])
        for symbol in ("005930", "000660")
        for g in read(tmp_path, "gaps", "H0UNCNT0", symbol)
    }
    assert gaps == {
        ("000660", FIXED_NOW, FIXED_NOW + timedelta(minutes=6)),  # recorded though not alerted alone
        ("005930", FIXED_NOW + timedelta(minutes=2), None),  # still quiet at shutdown
    }


async def test_a_us_symbol_that_never_traded_today_does_not_hide_the_whole_feed_going_quiet(
    kis: FakeKis, tmp_path: Path
) -> None:
    nvda, aapl = ("HDFSCNT0", "RBAQNVDA"), ("HDFSCNT0", "RBAQAAPL")
    us_fill = {"RSYM": "RBAQNVDA", "SYMB": "NVDA", "XYMD": "20261007", "XHMS": "203016"}
    kis.script[nvda] = [us_trade_frame({**us_fill, "KYMD": "20261008", "KHMS": "093016", "LAST": "192.3400"})]
    kis.script[aapl] = []  # keeps its own hours: no trade today yet, so it cannot go stale
    clock = Clock(FIXED_NOW)
    stop = asyncio.Event()
    us = replace(settings_for(kis, tmp_path), us_symbols=("NVDA", "AAPL"))
    settings = replace(with_telegram(kis, us), health_check_seconds=0.05)
    task = asyncio.create_task(run_collector(settings, stop=stop, clock=clock))
    await kis.wait_for_subscriptions({nvda, aapl})
    await kis.round_trip()

    clock.at = FIXED_NOW + timedelta(minutes=16)  # NVDA quiet past its 15 minutes
    await until(lambda: any("stale HDFSCNT0" in m["text"] for m in kis.telegram_messages))
    stop.set()
    await asyncio.wait_for(task, 5)

    [alert] = [m["text"] for m in kis.telegram_messages if "stale HDFSCNT0" in m["text"]]
    assert alert.startswith("[장애] 구독한 키가 모두 끊긴 피드: "), alert


async def test_after_a_reconnect_refused_subscriptions_stay_on_one_rest_poll(
    kis: FakeKis, tmp_path: Path
) -> None:
    kis.max_subscriptions = 3  # the night futures (#264) and the trade feed fit, the book feed is refused
    kis.rest_responses["FHKST01010200"] = lambda q: {"rt_cd": "1", "msg1": "not needed here"}
    kis.script[TRADE_KEY] = []
    stop = asyncio.Event()
    task = asyncio.create_task(
        run_collector(fast(settings_for(kis, tmp_path)), stop=stop, clock=lambda: FIXED_NOW)
    )
    await kis.wait_for_subscription_count(TRADE_KEY, 1)
    await kis.close_connections()
    await kis.wait_for_subscription_count(TRADE_KEY, 2)
    await kis.round_trip()
    stop.set()
    await asyncio.wait_for(task, 5)

    book = ("H0UNASP0", "005930")
    assert kis.subscription_count(book) == 1, "a refused key is not registered again after a reconnect"
    assert all(g["end"] is not None for g in read(tmp_path, "gaps", "H0UNCNT0"))
    assert not (tmp_path / "gaps" / "date=2026-10-08" / "feed=H0UNASP0").exists(), "REST covers the book"


async def test_backoff_keeps_growing_when_kis_drops_the_connection_before_answering(
    kis: FakeKis, tmp_path: Path
) -> None:
    kis.drop_on_connect = True
    stop = asyncio.Event()
    settings = replace(settings_for(kis, tmp_path), reconnect_initial_seconds=0.05, reconnect_max_seconds=0.4)
    task = asyncio.create_task(run_collector(settings, stop=stop, clock=lambda: FIXED_NOW))
    await asyncio.sleep(1.0)
    stop.set()
    await asyncio.wait_for(task, 5)

    # 0.05 + 0.1 + 0.2 + 0.4 + ... : about five attempts in a second, not twenty.
    assert 2 <= kis.ws_connections <= 7, kis.ws_connections


async def test_a_flapping_connection_does_not_repeat_alerts(
    kis: FakeKis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alert_after(monkeypatch, 0.1)
    kis.script[TRADE_KEY] = []
    stop = asyncio.Event()
    settings = replace(with_telegram(kis, fast(settings_for(kis, tmp_path))), recovery_hold_seconds=0.6)
    task = asyncio.create_task(run_collector(settings, stop=stop, clock=lambda: FIXED_NOW))
    await kis.wait_for_subscription_count(TRADE_KEY, 1)
    kis.accept_ws = False
    await kis.close_connections()
    await until(lambda: len(kis.telegram_messages) >= 1)  # down past the alert's wait
    kis.accept_ws = True
    await kis.wait_for_subscription_count(TRADE_KEY, 2)
    for n in (3, 4):  # drops again shortly after every reconnect
        await kis.close_connections()
        await kis.wait_for_subscription_count(TRADE_KEY, n)
    await until(lambda: len(kis.telegram_messages) >= 2)  # then stays up longer than the hold
    stop.set()
    await asyncio.wait_for(task, 5)

    texts = [m["text"] for m in kis.telegram_messages]
    assert [t[:4] for t in texts] == ["[장애]", "[복구]"], texts


async def test_silence_counts_from_the_open_not_from_the_nxt_pre_market(kis: FakeKis, tmp_path: Path) -> None:
    kis.script[TRADE_KEY] = [fill("81200")]
    pre_market = datetime(2026, 10, 8, 8, 49, 0, tzinfo=FIXED_NOW.tzinfo)
    clock = Clock(pre_market)
    stop = asyncio.Event()
    settings = replace(settings_for(kis, tmp_path), health_check_seconds=0.05)
    task = asyncio.create_task(run_collector(settings, stop=stop, clock=clock))
    await kis.wait_for_subscription_count(TRADE_KEY, 1)
    await kis.round_trip()

    clock.at = pre_market.replace(hour=9, minute=0, second=30)  # 30s into the session
    await asyncio.sleep(0.3)
    clock.at = pre_market.replace(hour=9, minute=10)
    await asyncio.sleep(0.3)
    stop.set()
    await asyncio.wait_for(task, 5)

    [gap] = read(tmp_path, "gaps", "H0UNCNT0")
    assert gap["reason"] == "stale"
    assert gap["start"] == pre_market.replace(hour=9, minute=0, second=0)


async def test_a_connection_that_goes_silent_without_closing_is_reconnected_with_a_gap_and_an_alert(
    kis: FakeKis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Input: KIS stops sending on a connection without closing it (half-open, as on
    2026-10-19 at 16:33, #308), and never answers the close. Output: after the silence limit
    the collector reconnects and registers every subscription again, a disconnect gap runs
    from the last frame to the new registration, and one alert pair goes out: the drop began at
    the last frame, longer ago than the alert waits (30 s against 60 s on the server, #488)."""
    monkeypatch.setattr(collector, "WS_CLOSE_SECONDS", 0.2)  # the dead side never replies
    alert_after(monkeypatch, 0.1)
    kis.script[TRADE_KEY] = [fill("81200")]
    clock = Clock(FIXED_NOW)  # 09:30:16; the watchdog's silence is real time, not this clock
    stop = asyncio.Event()
    settings = replace(with_telegram(kis, fast(settings_for(kis, tmp_path))), ws_silence_seconds=0.3)
    task = asyncio.create_task(run_collector(settings, stop=stop, clock=clock))
    await kis.wait_for_subscription_count(TRADE_KEY, 1)
    await kis.round_trip()  # the last frame: a PINGPONG at FIXED_NOW
    first_round = len(kis.subscriptions)

    kis.script[TRADE_KEY] = [fill("81300")]
    clock.at = FIXED_NOW + timedelta(seconds=75)
    kis.go_silent()
    # 0.3 s of silence, then the close waits 0.2 s for a reply that never comes (aiohttp's own
    # default would hold the reconnect 10 s).
    await kis.wait_for_subscription_count(TRADE_KEY, 2, timeout=3)
    await kis.round_trip()
    await until(lambda: len(kis.telegram_messages) >= 2)
    stop.set()
    await asyncio.wait_for(task, 5)

    assert kis.ws_connections == 2
    again = {
        (r["body"]["input"]["tr_id"], r["body"]["input"]["tr_key"]) for r in kis.subscriptions[first_round:]
    }
    assert again == kis.subscribed_keys(), "every subscription is registered again"
    assert [t["price"] for t in read(tmp_path, "events", "H0UNCNT0")] == [81200, 81300]
    [gap] = read(tmp_path, "gaps", "H0UNCNT0")
    assert gap["reason"] == "disconnected: silent 0.3s"
    assert (gap["start"], gap["end"]) == (FIXED_NOW, FIXED_NOW + timedelta(seconds=75))
    texts = [m["text"] for m in kis.telegram_messages]
    assert len(texts) == 2, texts
    assert texts[0] == "[장애] KIS WebSocket 연결이 끊겼습니다 (silent 0.3s). 재연결을 시도합니다.", texts
    assert texts[1].startswith("[복구]") and "WebSocket" in texts[1]


async def test_a_silent_connection_is_marked_lost_before_its_close_is_waited_for(
    kis: FakeKis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Closing a dead connection waits for a reply that never comes (up to WS_CLOSE_SECONDS).
    Input: KIS goes silent in continuous trading, then, while that close waits, the feeds pass
    their stale thresholds (the book's is 60 s, the watchdog's default too) under the 5-second
    health check. Output: the connection was marked lost when the silence was noticed, so the
    gaps are the silent disconnect, not stale, and only the WebSocket alert goes out."""
    monkeypatch.setattr(collector, "WS_CLOSE_SECONDS", 1.5)
    alert_after(monkeypatch, 0.1)  # under the silence: alerted when it is noticed (#488)
    book = ("H0UNASP0", "005930")
    kis.script[TRADE_KEY] = [fill("81200")]
    kis.script[book] = [
        book_frame({"MKSC_SHRN_ISCD": "005930", "BSOP_HOUR": "093016", "BIDP1": "81200", "ASKP1": "81300"})
    ]
    clock = Clock(FIXED_NOW)  # Thursday 09:30:16, continuous trading
    stop = asyncio.Event()
    settings = replace(
        with_telegram(kis, fast(settings_for(kis, tmp_path))),
        ws_silence_seconds=0.3,
        health_check_seconds=0.05,
    )
    task = asyncio.create_task(run_collector(settings, stop=stop, clock=clock))
    await kis.wait_for_subscriptions({TRADE_KEY, book})
    await kis.round_trip()

    kis.go_silent()
    await until(lambda: len(kis.telegram_messages) >= 1, timeout=1.0)  # before the 1.5 s close ends
    assert kis.ws_connections == 1, "the close still waits for the dead side"
    clock.at = FIXED_NOW + timedelta(seconds=90)  # the book and the trades past their thresholds
    await asyncio.sleep(0.3)  # health checks while the close still waits
    assert kis.ws_connections == 1
    await kis.wait_for_subscription_count(TRADE_KEY, 2)
    await kis.round_trip()
    stop.set()
    await asyncio.wait_for(task, 5)

    texts = [m["text"] for m in kis.telegram_messages]
    assert texts[0] == "[장애] KIS WebSocket 연결이 끊겼습니다 (silent 0.3s). 재연결을 시도합니다.", texts
    assert not any("stale" in t for t in texts), texts
    for tr_id in (TRADE_KEY[0], book[0]):
        [gap] = read(tmp_path, "gaps", tr_id)
        assert (gap["reason"], gap["start"]) == ("disconnected: silent 0.3s", FIXED_NOW), tr_id


async def test_the_silence_alert_goes_out_of_market_hours_too(
    kis: FakeKis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The incident's silence began at 16:33, after the close, when no stale check runs. Out of the
    session the wait counts from when the silence is noticed (#614): the close's 0.2 s wait for the
    dead side alone outlasts it here."""
    monkeypatch.setattr(collector, "WS_CLOSE_SECONDS", 0.2)
    alert_after(monkeypatch, 0.1)
    kis.script[TRADE_KEY] = []
    evening = FIXED_NOW.replace(hour=16, minute=33, second=43)
    stop = asyncio.Event()
    settings = replace(with_telegram(kis, fast(settings_for(kis, tmp_path))), ws_silence_seconds=0.3)
    task = asyncio.create_task(run_collector(settings, stop=stop, clock=lambda: evening))
    await kis.wait_for_subscription_count(TRADE_KEY, 1)
    kis.go_silent()
    await until(lambda: len(kis.telegram_messages) >= 1)
    stop.set()
    await asyncio.wait_for(task, 5)

    assert kis.telegram_messages[0]["text"].startswith("[장애] KIS WebSocket 연결이 끊겼습니다 (silent 0.3s)")


OUT = "out of the session: not alerted"  # a silent drop out of the session back in time (#614)


async def silent_after_the_close(
    kis: FakeKis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[asyncio.Event, asyncio.Task[None]]:
    """A collector at 15:36:13 on a trading day, after the close (the connection went silent so on
    2026-11-05), whose connection goes silent for 1 s. Its alert waits 0.9 s: in the session the silence
    alone is past it (alerted at once), and the dead side's close is 0.05 s."""
    monkeypatch.setattr(collector, "WS_CLOSE_SECONDS", 0.05)
    alert_after(monkeypatch, 0.9)
    kis.script[TRADE_KEY] = [fill("81200")]
    stop = asyncio.Event()
    settings = replace(with_telegram(kis, fast(settings_for(kis, tmp_path))), ws_silence_seconds=1.0)
    after_close = FIXED_NOW.replace(hour=15, minute=36, second=13)
    task = asyncio.create_task(run_collector(settings, stop=stop, clock=lambda: after_close))
    await kis.wait_for_subscription_count(TRADE_KEY, 1)
    await kis.round_trip()
    return stop, task


async def test_a_silent_connection_back_soon_out_of_the_session_is_logged_and_not_alerted(
    kis: FakeKis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """#614: out of the session the trader trades nothing, so a silent connection the collector has back
    within the wait, counted from when the silence is noticed, sends neither the [장애] nor the [복구].
    A WARNING says so; it is not one of #488's short drops (the Grafana rule counts those)."""
    stop, task = await silent_after_the_close(kis, tmp_path, monkeypatch)
    kis.go_silent()
    await kis.wait_for_subscription_count(TRADE_KEY, 2, timeout=5)
    await until(lambda: len(logged(caplog, OUT)) == 1)
    stop.set()
    await asyncio.wait_for(task, 5)

    assert kis.telegram_messages == []
    [line] = logged(caplog, OUT)
    assert line.startswith("KIS WebSocket back ") and line.endswith(
        "after it dropped (silent 1s), out of the session: not alerted (#614)"
    ), line
    assert not any("short drop" in m for m in logged(caplog, SHORT))
    [gap] = read(tmp_path, "gaps", "H0UNCNT0")
    assert gap["reason"] == "disconnected: silent 1s", "the gap runs from the last frame, as in the session"


async def test_a_silent_connection_out_of_the_session_not_back_in_time_is_alerted_after_the_wait(
    kis: FakeKis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    stop, task = await silent_after_the_close(kis, tmp_path, monkeypatch)
    kis.accept_ws = False  # every reconnect refused
    kis.go_silent()
    await until(lambda: len(kis.telegram_messages) >= 1)
    kis.accept_ws = True
    await until(lambda: len(kis.telegram_messages) >= 2)
    stop.set()
    await asyncio.wait_for(task, 5)

    texts = [m["text"] for m in kis.telegram_messages]
    assert texts[0] == "[장애] KIS WebSocket 연결이 끊겼습니다 (silent 1s). 재연결을 시도합니다.", texts
    assert texts[1].startswith("[복구]"), texts
    noticed = next(r.created for r in caplog.records if r.getMessage().startswith("KIS WebSocket silent for"))
    [alerted] = [
        r.created for r in caplog.records if r.getMessage().startswith("alert: [장애] KIS WebSocket")
    ]
    assert alerted - noticed >= 0.8, "the wait counted from when the silence was noticed"
    assert logged(caplog, OUT) == []


async def test_a_quiet_connection_that_still_gets_pingpong_or_data_is_not_reconnected(
    kis: FakeKis, tmp_path: Path
) -> None:
    """Idle, KIS sends a PINGPONG about every 10.8 s; in the sessions, data flows. Here each
    comes a third of the silence limit apart, for four times the limit: no reconnect."""
    kis.script[TRADE_KEY] = []
    stop = asyncio.Event()
    settings = replace(with_telegram(kis, fast(settings_for(kis, tmp_path))), ws_silence_seconds=0.3)
    task = asyncio.create_task(run_collector(settings, stop=stop, clock=lambda: FIXED_NOW))
    await kis.wait_for_subscription_count(TRADE_KEY, 1)
    for _ in range(6):  # 1.2 s of PINGPONG only
        await kis.round_trip()
        await asyncio.sleep(0.1)
    for _ in range(6):  # then 1.2 s of data only
        await kis.push(fill("81200"))
        await asyncio.sleep(0.1)
    await kis.round_trip()
    stop.set()
    await asyncio.wait_for(task, 5)

    assert kis.ws_connections == 1
    assert kis.subscription_count(TRADE_KEY) == 1
    assert kis.telegram_messages == []
    assert not (tmp_path / "gaps").exists()


def test_the_investor_estimates_are_asked_only_in_a_trading_days_session() -> None:
    """Input: the REST plan, asked about a trading day's hours, a weekday holiday and a Saturday
    (2026-10-31, when KIS refused HHPTJ04160200 41 times, #441). Output: the investor estimates
    are asked from the open (09:00) to the close (15:30) of a trading day only, each symbol's
    alike (#488); the VIs keep their hours, from the opening auction."""
    thursday, holiday, saturday = date(2026, 10, 8), date(2026, 10, 9), date(2026, 10, 31)
    calendar = MarketCalendar({thursday: True, holiday: False, saturday: False})
    settings = Settings(kis_app_key="key", kis_app_secret="secret", symbols=("005930", "000660"))
    plan = rest_plan(settings, calendar)
    estimates = [job for job in plan if job.feed is FOREIGN_ESTIMATE]
    [vi, *_] = [job for job in plan if job.feed is VI_STATUS]
    assert [job.key for job in estimates] == ["005930", "000660"]

    def asked(day: date, hour: int, minute: int, second: int = 0) -> set[bool]:
        at = datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=KST)
        return {job.when is not None and job.when(at) for job in estimates}

    assert asked(thursday, 8, 59, 59) == {False}
    assert asked(thursday, 9, 0) == {True}
    assert asked(thursday, 15, 29, 59) == {True}
    assert asked(thursday, 15, 30) == {False}
    assert asked(thursday, 20, 48) == {False}  # 2026-10-19's poll that evening (#308)
    assert asked(holiday, 10, 0) == {False}
    assert asked(saturday, 9, 21) == asked(saturday, 15, 32) == {False}
    opening_auction = datetime(2026, 10, 8, 8, 30, tzinfo=KST)
    assert vi.when is not None and vi.when(opening_auction)
    assert asked(thursday, 8, 30) == {False}
