"""The KOSPI200 night futures (#264): what is registered, how a night is recorded across
midnight, the contract a night takes, its silence, and the night report (fake KIS seam)."""

from __future__ import annotations

import asyncio
import io
import zipfile
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from krx_quant.archive import RAW_KEEP_DAYS, RawMissing, archive
from krx_quant.collector import run_collector
from krx_quant.config import Settings
from krx_quant.contracts import KOSPI200, night_front_month
from krx_quant.fake_kis import FakeKis
from krx_quant.feeds.base import KST, kst_nearest
from krx_quant.market_calendar import MarketCalendar
from krx_quant.night_probe import master_lines, report
from krx_quant.object_store import LocalObjectStore
from krx_quant.raw_days import RawDays
from krx_quant.recording_quality import subscribed_nights
from krx_quant.scheduled import run_command
from tests.collector_doubles import TRADE_KEY, Clock, collect, serve_masters, settings_for
from tests.kis_frames import FUTURES_COLUMNS, frame, master_zips, night_futures_frame
from tests.kis_rest import HOLIDAYS, holiday_api
from tests.test_calendar_health import health_checks
from tests.test_resilience import with_telegram
from tests.waiting import until

THURSDAY, FRIDAY, SATURDAY, MONDAY = (
    date(2026, 10, 15),
    date(2026, 10, 16),
    date(2026, 10, 17),
    date(2026, 10, 19),
)
CALENDAR = MarketCalendar(
    {THURSDAY: True, FRIDAY: True, SATURDAY: False, date(2026, 10, 18): False, MONDAY: True}
)
TRADES = ("H0MFCNT0", "A01612")


def at(day: date, hhmmss: str) -> datetime:
    return datetime.combine(day, datetime.strptime(hhmmss, "%H:%M:%S").time(), KST)


def night_trade(hhmmss: str, price: str = "1062.35") -> str:
    return night_futures_frame(
        {
            "futs_shrn_iscd": "A01612",
            "bsop_hour": hhmmss,
            "futs_prpr": price,
            "futs_prdy_ctrt": "0.42",
            "last_cnqn": "3",
            "acml_vol": "15234",
            "hts_otst_stpl_qty": "301122",
            "futs_askp1": "1062.40",
            "futs_bidp1": "1062.35",
        }
    )


def events(data_dir: Path, day: date, feed: str) -> list[dict[str, object]]:
    path = data_dir / "events" / f"date={day.isoformat()}" / f"feed={feed}" / "symbol=A01612"
    rows: list[dict[str, object]] = pq.read_table(path).to_pylist() if path.exists() else []
    return rows


# ---- the feeds


def test_a_time_goes_on_the_day_nearest_the_time_received() -> None:
    def received(day: date, hhmmss: str, micro: int = 0) -> datetime:
        return at(day, hhmmss).replace(microsecond=micro).astimezone(UTC)

    # The session crosses midnight and its records carry no date.
    assert kst_nearest("221000", received(THURSDAY, "22:10:00", 600_000)) == at(THURSDAY, "22:10:00")
    assert kst_nearest("235959", received(FRIDAY, "00:00:00", 400_000)) == at(THURSDAY, "23:59:59")
    assert kst_nearest("000001", received(THURSDAY, "23:59:59", 900_000)) == at(FRIDAY, "00:00:01")
    assert kst_nearest("055959", received(FRIDAY, "06:00:00", 300_000)) == at(FRIDAY, "05:59:59")
    assert kst_nearest("999999", received(FRIDAY, "06:00:05", 300_000)) == at(FRIDAY, "06:00:05")
    with pytest.raises(ValueError):
        kst_nearest("253000", received(FRIDAY, "01:30:00"))  # not a time: the record stays raw


async def test_night_trades_are_recorded_like_the_day_futures(kis: FakeKis, tmp_path: Path) -> None:
    kis.script[TRADES] = [night_trade("221000")]

    await collect(kis, settings_for(kis, tmp_path), now=at(THURSDAY, "22:10:00"))

    [trade] = events(tmp_path, THURSDAY, "H0MFCNT0")
    assert trade["event_time"] == at(THURSDAY, "22:10:00")
    assert (trade["price"], trade["change_pct"], trade["qty"], trade["cum_volume"]) == (
        1062.35,
        0.42,
        3,
        15234,
    )
    assert (trade["open_interest"], trade["ask1"], trade["bid1"]) == (301122, 1062.4, 1062.35)


async def test_after_midnight_a_night_goes_on_in_the_next_day_s_partition(
    kis: FakeKis, tmp_path: Path
) -> None:
    # Received just after midnight, a trade of 23:59:59 is the evening's; it is filed by the
    # day it was received, as every feed is.
    kis.script[TRADES] = [night_trade("235959"), night_trade("000001")]

    await collect(kis, settings_for(kis, tmp_path), now=at(FRIDAY, "00:00:01"))

    assert not events(tmp_path, THURSDAY, "H0MFCNT0")
    times = [t["event_time"] for t in events(tmp_path, FRIDAY, "H0MFCNT0")]
    assert times == [at(THURSDAY, "23:59:59"), at(FRIDAY, "00:00:01")]


async def test_a_night_trade_in_the_day_futures_layout_stays_raw(kis: FakeKis, tmp_path: Path) -> None:
    # KIS's example gives 49 fields; the day futures have 50. A frame that does not fit the
    # layout is kept raw, for another look, and not guessed at.
    kis.script[TRADES] = [
        frame("H0MFCNT0", FUTURES_COLUMNS, {"futs_shrn_iscd": "A01612", "bsop_hour": "221000"})
    ]

    await collect(kis, settings_for(kis, tmp_path), now=at(THURSDAY, "22:10:00"))

    raw = tmp_path / "raw" / f"date={THURSDAY}" / "feed=H0MFCNT0" / "symbol=A01612"
    assert pq.read_table(raw).num_rows == 1
    assert not events(tmp_path, THURSDAY, "H0MFCNT0")


# ---- the plan and the contract


async def test_the_night_futures_follow_the_day_futures(kis: FakeKis, tmp_path: Path) -> None:
    serve_masters(kis)
    kis.script[TRADES] = []

    await collect(kis, settings_for(kis, tmp_path), now=at(THURSDAY, "07:20:00"))

    sent = [(s["body"]["input"]["tr_id"], s["body"]["input"]["tr_key"]) for s in kis.subscriptions]
    assert sent[:3] == [("H0IFCNT0", "A01612"), ("H0CFCNT0", "A75610"), TRADES]
    # Their book (H0MFASP0) is not registered since #694: no algorithm read it.
    assert all(tr != "H0MFASP0" for tr, _ in sent)


async def test_without_masters_the_night_futures_come_first(kis: FakeKis, tmp_path: Path) -> None:
    # The night's contract comes from the expiry rules: it needs no master file.
    kis.script[TRADES] = []

    await collect(kis, settings_for(kis, tmp_path), now=at(THURSDAY, "07:20:00"))

    sent = [(s["body"]["input"]["tr_id"], s["body"]["input"]["tr_key"]) for s in kis.subscriptions]
    assert sent[:2] == [TRADES, ("H0UNCNT0", "005930")]


@pytest.mark.parametrize(
    ("started", "code"),
    [
        (datetime(2026, 10, 15, 7, 20, tzinfo=KST), "A01612"),
        # December last trades on Thursday the 10th, through that day's regular session
        # (KRX, OPN01041402): the night before still trades it.
        (datetime(2026, 12, 9, 7, 20, tzinfo=KST), "A01612"),
        (datetime(2026, 12, 10, 5, 0, tzinfo=KST), "A01612"),  # that night, before 06:00
        # March starts in the night that begins on the 10th, and goes on after it.
        (datetime(2026, 12, 10, 7, 20, tzinfo=KST), "A01703"),
        (datetime(2026, 12, 11, 7, 20, tzinfo=KST), "A01703"),
        (datetime(2026, 12, 11, 3, 0, tzinfo=KST), "A01703"),  # the expiry day's night, going on
    ],
)
def test_a_night_takes_the_month_still_trading_in_it(started: datetime, code: str) -> None:
    assert night_front_month(KOSPI200, started, MarketCalendar()) == code


def test_an_expiry_a_holiday_moved_moves_the_night_s_roll() -> None:
    # Were Thursday 2026-12-10 a holiday, December would last trade on Wednesday the 9th:
    # Tuesday's night still takes December, Wednesday's takes March.
    calendar = MarketCalendar({date(2026, 12, 10): False})
    assert night_front_month(KOSPI200, datetime(2026, 12, 8, 7, 20, tzinfo=KST), calendar) == "A01612"
    assert night_front_month(KOSPI200, datetime(2026, 12, 9, 7, 20, tzinfo=KST), calendar) == "A01703"
    # Friday's night, which counts as Monday's trading, takes the month Friday does.
    assert night_front_month(KOSPI200, datetime(2026, 12, 4, 22, 0, tzinfo=KST), calendar) == "A01612"


# ---- silence


async def test_the_night_feeds_quiet_all_day_are_never_stale(
    kis: FakeKis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checks = health_checks(monkeypatch)
    serve_masters(kis)
    kis.rest_responses[HOLIDAYS] = holiday_api(())
    kis.script[TRADE_KEY] = []  # registered, never delivers
    kis.script[TRADES] = []
    start = at(THURSDAY, "09:30:16")
    clock = Clock(start)
    quick = replace(settings_for(kis, tmp_path), health_check_seconds=0.05, backfill_days=1)
    settings = with_telegram(kis, quick)
    stop = asyncio.Event()
    task = asyncio.create_task(run_collector(settings, stop=stop, clock=clock))
    await until(lambda: (settings.state_dir / "market_calendar.json").exists())
    clock.at = start + timedelta(hours=2)  # quiet through continuous trading
    await until(lambda: checks.stale(TRADE_KEY))  # the check that found it saw the night feeds too
    stop.set()
    await asyncio.wait_for(task, 5)  # the alerts are sent by then

    assert list(tmp_path.glob("gaps/*/feed=H0UNCNT0/*")), "the stock trades went stale"
    assert not list(tmp_path.glob("gaps/*/feed=H0MF*")), "the night feeds did not"
    alerts = " ".join(m["text"] for m in kis.telegram_messages)
    assert "stale H0UNCNT0" in alerts
    assert "H0MF" not in alerts


# ---- the night report


async def collect_again(kis: FakeKis, settings: Settings, now: datetime) -> None:
    """Run another collector until its night registrations' frames are handled."""
    count = kis.subscription_count(TRADES) + 1
    stop = asyncio.Event()
    task = asyncio.create_task(run_collector(settings, stop=stop, clock=lambda: now))
    await kis.wait_for_subscription_count(TRADES, count)
    await kis.round_trip()
    stop.set()
    await asyncio.wait_for(task, 5)


async def record_a_night(kis: FakeKis, data_dir: Path) -> None:
    """Thursday: registered at 07:20, trades from 22:10, more after midnight."""
    settings = settings_for(kis, data_dir)
    await collect_again(kis, settings, at(THURSDAY, "07:20:00"))
    kis.script[TRADES] = [night_trade("221000"), night_trade("221005")]
    await collect_again(kis, settings, at(THURSDAY, "22:10:05"))
    kis.script[TRADES] = [
        night_trade("235959"),
        frame("H0MFCNT0", FUTURES_COLUMNS, {"futs_shrn_iscd": "A01612", "bsop_hour": "003000"}),
    ]
    await collect_again(kis, settings, at(FRIDAY, "00:30:00"))


async def test_a_night_subscribed_for_from_its_start_is_found(kis: FakeKis, tmp_path: Path) -> None:
    await record_a_night(kis, tmp_path)

    assert subscribed_nights(RawDays(tmp_path), CALENDAR, FRIDAY) == [THURSDAY]
    assert subscribed_nights(RawDays(tmp_path), CALENDAR, THURSDAY) == [], "only nights before the day asked"


async def test_a_night_registered_only_after_18_00_is_not_found(kis: FakeKis, tmp_path: Path) -> None:
    kis.script[TRADES] = []
    await collect(kis, settings_for(kis, tmp_path), now=at(THURSDAY, "19:00:00"))

    assert subscribed_nights(RawDays(tmp_path), CALENDAR, FRIDAY) == []


async def test_the_report_counts_and_times_a_night_without_its_prices(kis: FakeKis, tmp_path: Path) -> None:
    await record_a_night(kis, tmp_path)

    text = report(tmp_path, CALENDAR, at(FRIDAY, "22:40:00"), ["마스터 줄"])

    lines = [line.strip() for line in text.splitlines()]
    assert lines[0] == "야간선물 녹화 확인 (#264): 2026-10-15 밤 (18:00 – 2026-10-16 06:00)"
    assert any(line.startswith("구독한 밤") and line.endswith("2026-10-15") for line in lines)
    assert "만기 규칙이 이 밤에 고르는 월물: A01612" in lines
    assert "H0MFCNT0 A01612: SUBSCRIBE SUCCESS 3번(첫 07:20:00, 마지막 10-16 00:30:00)" in lines
    trades = text.split("2. H0MFCNT0 체결")[1].split("3. 결측 구간")[0]
    assert "raw: 프레임 4 · 레코드 4 (날짜 파티션별 프레임: 2026-10-15 2, 2026-10-16 2)" in trades
    assert "레코드당 필드 수: 49개 3, 50개 1 (KIS 예제는 49개)" in trades
    assert "수신: 첫 22:10:05 · 마지막 10-16 00:30:00" in trades
    assert "시간대별 레코드: 22시 2 · 00시 2" in trades
    assert "events: 행 3 (raw 레코드 중 정규화하지 못한 것 1)" in trades
    assert "event_time: 첫 22:10:00 · 마지막 23:59:59" in trades
    assert "받은 날과 날짜가 다른 행 1" in trades  # 23:59:59 received after midnight
    assert "늘 비거나 0인 필드: futs_prdy_vrss" in trades
    assert "H0MFASP0" not in text, "the night book is not recorded since #694"
    assert "H0IFCNT0 프레임 0개" in text
    assert "5. KIS 마스터\n  마스터 줄" in text
    for price in ("1062.35", "1062.40", "15234", "301122", "820"):
        assert price not in text, price


async def test_a_night_pruned_on_the_server_is_read_back_from_the_store(
    kis: FakeKis, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """#607: a month on the archive has deleted the night's raw on the server; the report on it
    (``--day``) reads it back from the store."""
    await record_a_night(kis, tmp_path)
    store = LocalObjectStore(tmp_path_factory.mktemp("store"))
    await archive(tmp_path, store)  # merged, as the server's are by then
    now = at(FRIDAY, "22:40:00")
    before = report(tmp_path, CALENDAR, now, ["마스터 줄"])
    pruned = await archive(tmp_path, store, today=FRIDAY + timedelta(days=RAW_KEEP_DAYS + 10))
    assert pruned.pruned and not pruned.raw_left

    with pytest.raises(RawMissing):
        report(tmp_path, CALENDAR, now, ["마스터 줄"])
    with RawDays(tmp_path, store) as raw:
        assert report(tmp_path, CALENDAR, now, ["마스터 줄"], raw=raw) == before
        assert raw.restored


def test_the_report_without_a_subscribed_night_says_so(tmp_path: Path) -> None:
    text = report(tmp_path, CALENDAR, at(FRIDAY, "22:40:00"), [])

    assert "2026-10-15 밤" in text  # the latest trading day's, Thursday's
    assert "없음: 그날 수집기가 야간선물을 구독하지 않았다" in text
    assert "받은 프레임 없음" in text


async def test_the_command_runs_in_its_own_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """As its scheduled job ran it (``probe-night-264``, which ran on 2026-10-20 and left ``JOBS``,
    #505): on a server with nothing recorded, the masters out of reach."""
    monkeypatch.chdir(tmp_path)  # no .env
    monkeypatch.setenv("KRX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KRX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("KIS_MASTER_URL", "http://127.0.0.1:9/master")  # nothing listens: no network
    ran = await run_command(["probe-night"], timedelta(seconds=25))
    assert "야간선물 녹화 확인 (#264)" in ran.stdout and "2. H0MFCNT0 체결" in ran.stdout
    assert "fo_cme_code.mst를 읽지 못했다" in ran.stdout  # said, not fatal
    assert ran.exit_code == 0


def test_the_masters_show_codes_and_names_only() -> None:
    zips = master_zips(kospi200_futures=[("A01612", "1"), ("A01703", "2")], usd_futures=[], options=[])
    with zipfile.ZipFile(io.BytesIO(zips["fo_idx_code_mts.mst.zip"])) as z:
        index = z.read("fo_idx_code_mts.mst").decode("cp949")
    lines = master_lines(
        index, "1A01612   KR4101W90002F 202612\n", ["fo_cme_code.mst를 읽지 못했다: HTTP 404"]
    )

    assert (
        lines[0]
        == "fo_idx_code_mts.mst의 KOSPI200 선물: A01612(월물구분 1, F A01612), A01703(월물구분 2, F A01703)"
    )
    assert lines[1:3] == ["fo_cme_code.mst: 1줄. 앞 1줄:", "  1A01612   KR4101W90002F 202612"]
    assert lines[3] == "fo_cme_code.mst를 읽지 못했다: HTTP 404"
