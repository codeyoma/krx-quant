"""Macro Collector: daily indicators from official sources, and the day's market regime.

Each trading day at ``KRX_MACRO_AT`` (07:30 KST, after the US close) the collector reads
the indicators below, each source on its own, and decides the day's regime from them
(``regime.py``). A source that fails is read again 10 minutes later, 3 times that
morning; its third failure in a row raises a Telegram alert, cleared once it answers. A
source without its API key is skipped. ``krx-quant macro`` reads once.

A reading's move is from what was known on the morning of the KRX trading day before to
what is known this morning: a US holiday adds no move, and a KRX holiday adds every
session since. A value more than 7 days old is missing. Readings and the regime are
recorded in the ``macro`` layer and archived with the rest.

Naver's market page is not read: every item on it has an official source, or is not
needed (docs/design.md §4.5).
"""

from __future__ import annotations

import asyncio
import enum
import json
import logging
import math
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, NamedTuple
from zoneinfo import ZoneInfo

import aiohttp
import pyarrow as pa
import pyarrow.parquet as pq

from krx_quant.archive import partition_files
from krx_quant.backfill import FUTURES_BARS_TR, bars_key
from krx_quant.config import Settings
from krx_quant.contracts import USD
from krx_quant.feeds.base import KST, kst_day
from krx_quant.market_calendar import MarketCalendar, calendar_store, next_on_trading_day
from krx_quant.notify import Alerts
from krx_quant.recorder import ParquetRecorder
from krx_quant.regime import Decision, Regime, RegimeInputs, RegimeRules, decide

log = logging.getLogger(__name__)

TRIES_A_MORNING = 3  # reads of a failing source each morning
ALERT_AFTER = 3  # failures in a row of a source before the alert
RETRY_AFTER = timedelta(minutes=10)
MAX_AGE = timedelta(days=7)  # an older value is missing
_LOOK_BACK = timedelta(days=21)  # enough for a long KRX holiday
_TIMEOUT = aiohttp.ClientTimeout(total=20)
# Yahoo answers clients that look like a browser.
_YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0 (krx-quant macro collector)"}


class SourceError(RuntimeError):
    """A source answered with an error."""


class Source(enum.Enum):
    ECOS = "ecos"  # the Bank of Korea; needs KRX_ECOS_API_KEY
    FRED = "fred"  # needs KRX_FRED_API_KEY
    YAHOO = "yahoo"
    KIS = "kis"  # the USD futures' backfilled bars, already on disk

    @property
    def alert(self) -> str:
        """The key of this source's alert."""
        return f"macro:{self.value}"


class Observation(NamedTuple):
    day: date  # the day the value is for, in the source's own calendar
    value: float


@dataclass(frozen=True)
class Indicator:
    name: str
    source: Source
    series: str  # the source's code: ECOS "stat/item", a FRED series, a Yahoo symbol
    label: str


# What is read, every trading day.
INDICATORS = (
    Indicator("usdkrw", Source.ECOS, "731Y001/0000001", "원/달러 매매기준율"),
    Indicator("ktb3y", Source.ECOS, "817Y002/010200000", "국고채 3년 금리"),
    Indicator("us10y", Source.FRED, "DGS10", "미국 국채 10년 금리"),
    Indicator("wti", Source.FRED, "DCOILWTICO", "WTI 유가"),
    Indicator("sox", Source.YAHOO, "^SOX", "필라델피아 반도체지수"),
    Indicator("vix", Source.YAHOO, "^VIX", "VIX"),
    Indicator("nq", Source.YAHOO, "NQ=F", "나스닥100 선물"),
    Indicator("nvda", Source.YAHOO, "NVDA", "엔비디아"),
    Indicator("mu", Source.YAHOO, "MU", "마이크론"),
    Indicator("usdf", Source.KIS, USD.series, "달러선물 최근월물"),
)


def parse_ecos(body: Mapping[str, Any]) -> list[Observation]:
    """An ECOS StatisticSearch answer (daily), oldest first; "no data" is empty."""
    if "RESULT" in body:
        result = body["RESULT"]
        if result.get("CODE") == "INFO-200":  # no data in the span asked for
            return []
        raise SourceError(f"ECOS {result.get('CODE')}: {result.get('MESSAGE')}")
    rows = body["StatisticSearch"]["row"]
    out = [
        Observation(datetime.strptime(row["TIME"], "%Y%m%d").date(), float(row["DATA_VALUE"]))
        for row in rows
        if row.get("DATA_VALUE")
    ]
    return sorted(out)


def parse_fred(body: Mapping[str, Any]) -> list[Observation]:
    """A FRED series/observations answer, oldest first, without the days that have no value."""
    if "error_code" in body:
        raise SourceError(f"FRED {body['error_code']}: {body.get('error_message')}")
    out = [
        Observation(date.fromisoformat(o["date"]), float(o["value"]))
        for o in body["observations"]
        if o["value"] not in (".", "")
    ]
    return sorted(out)


def parse_yahoo(body: Mapping[str, Any], now: datetime) -> list[Observation]:
    """A Yahoo chart answer (daily) as closes by exchange day, oldest first; a session still
    trading at ``now`` has no close yet.
    """
    chart = body["chart"]
    if chart.get("error"):
        error = chart["error"]
        raise SourceError(f"Yahoo {error.get('code')}: {error.get('description')}")
    result = chart["result"][0]
    zone = ZoneInfo(result["meta"]["exchangeTimezoneName"])
    closes = result["indicators"]["quote"][0]["close"]
    session = result["meta"].get("currentTradingPeriod", {}).get("regular", {})
    trading = None  # the day of a session under way
    if session and session["start"] <= now.timestamp() < session["end"]:
        trading = datetime.fromtimestamp(session["start"], zone).date()
    out = []
    for stamp, close in zip(result.get("timestamp") or [], closes, strict=True):
        day = datetime.fromtimestamp(stamp, zone).date()
        if close is not None and day != trading:
            out.append(Observation(day, float(close)))
    return sorted(out)


def latest_before(observations: Sequence[Observation], day: date) -> Observation | None:
    """The last value dated before ``day``, unless it is more than ``MAX_AGE`` old."""
    earlier = [o for o in observations if o.day < day]
    if not earlier or day - earlier[-1].day > MAX_AGE:
        return None
    return earlier[-1]


@dataclass(frozen=True)
class Reading:
    indicator: Indicator
    current: Observation | None  # the last value known on the morning of the day
    base: Observation | None  # the last value known on the morning of the KRX day before

    @classmethod
    def of(
        cls, indicator: Indicator, observations: Sequence[Observation], day: date, previous: date
    ) -> Reading:
        return cls(indicator, latest_before(observations, day), latest_before(observations, previous))

    @property
    def change_pct(self) -> float | None:
        """The move since the KRX day before, %; None without both values."""
        if self.current is None or self.base is None or self.base.value == 0:
            return None
        return (self.current.value / self.base.value - 1) * 100


@dataclass
class MacroDay:
    """A day's readings so far, and the regime they give."""

    day: date
    previous: date  # the KRX trading day before
    readings: dict[str, Reading] = field(default_factory=dict)
    failed: dict[Source, str] = field(default_factory=dict)  # why, in the last read
    skipped: list[Source] = field(default_factory=list)  # without their API key
    decision: Decision | None = None

    def decide(self, rules: RegimeRules) -> Decision:
        """The regime of the readings so far."""
        sox, vix = self.readings.get("sox"), self.readings.get("vix")
        # The won: the USD futures' close (yesterday's), else the Bank of Korea's rate (a day older).
        won = [r.change_pct for name in ("usdf", "usdkrw") if (r := self.readings.get(name)) is not None]
        inputs = RegimeInputs(
            sox_pct=sox.change_pct if sox else None,
            vix=vix.current.value if vix and vix.current else None,
            usdkrw_pct=next((c for c in won if c is not None), None),
        )
        self.decision = decide(inputs, rules)
        return self.decision


Record = Callable[[MacroDay, datetime], None]  # records a day's readings and regime


class MacroCollector:
    """Reads the sources for a day, counts each source's failures in a row and alerts."""

    def __init__(
        self,
        settings: Settings,
        session: aiohttp.ClientSession,
        alerts: Alerts,
        record: Record,
        calendar: MarketCalendar,
        now: Callable[[], datetime],
    ) -> None:
        self._settings = settings
        self._session = session
        self._alerts = alerts
        self._record = record
        self._calendar = calendar
        self._now = now
        self._failures = dict.fromkeys(Source, 0)
        self._macro: MacroDay | None = None  # the day under way

    async def run(self, day: date, sources: Sequence[Source] = tuple(Source)) -> MacroDay:
        """Read ``sources`` for ``day``, keep what each one gave, decide and record the regime."""
        if self._macro is None or self._macro.day != day:
            self._macro = MacroDay(day, self._calendar.previous_trading_day(day))
        macro = self._macro
        macro.skipped = [s for s in Source if not self._has_key(s)]
        tried = [s for s in sources if self._has_key(s)]
        results = await asyncio.gather(*(self._read(s, macro) for s in tried))
        for source, (readings, error) in zip(tried, results, strict=True):
            macro.readings.update(readings)  # what answered, even when another indicator failed
            if error is None:
                macro.failed.pop(source, None)
                self._answered(source)
            else:
                self._failed(macro, source, error)
        macro.decide(self._settings.regime_rules)
        self._record(macro, self._now())
        return macro

    def _has_key(self, source: Source) -> bool:
        if source is Source.ECOS:
            return self._settings.ecos_api_key is not None
        if source is Source.FRED:
            return self._settings.fred_api_key is not None
        return True

    def _failed(self, macro: MacroDay, source: Source, error: Exception) -> None:
        why = str(error) or type(error).__name__
        log.warning("macro source %s failed: %s", source.value, why)
        macro.failed[source] = why
        self._failures[source] += 1
        if self._failures[source] >= ALERT_AFTER:
            text = f"매크로 출처 {source.value}가 {ALERT_AFTER}번 연속 실패했습니다: {why}"
            self._alerts.raise_(source.alert, text)

    def _answered(self, source: Source) -> None:
        self._failures[source] = 0
        self._alerts.resolve(source.alert, f"매크로 출처 {source.value}가 다시 응답합니다.")

    async def _read(self, source: Source, macro: MacroDay) -> tuple[dict[str, Reading], Exception | None]:
        """The source's indicators, and the first error among those that failed."""
        readings: dict[str, Reading] = {}
        error: Exception | None = None
        for indicator in (i for i in INDICATORS if i.source is source):
            try:
                observations = await self._observations(indicator, macro.day)
            except Exception as e:
                error = error or e
                continue
            readings[indicator.name] = Reading.of(indicator, observations, macro.day, macro.previous)
        return readings, error

    async def _observations(self, indicator: Indicator, day: date) -> list[Observation]:
        start = day - _LOOK_BACK
        settings = self._settings
        if indicator.source is Source.ECOS:
            stat, item = indicator.series.split("/")
            url = (
                f"{settings.ecos_url}/StatisticSearch/{settings.ecos_api_key}/json/kr/1/100/"
                f"{stat}/D/{start:%Y%m%d}/{day:%Y%m%d}/{item}"
            )
            return parse_ecos(await self._get(url))
        if indicator.source is Source.FRED:
            params = {
                "series_id": indicator.series,
                "api_key": settings.fred_api_key or "",
                "file_type": "json",
                "observation_start": start.isoformat(),
            }
            return parse_fred(await self._get(f"{settings.fred_url}/fred/series/observations", params))
        if indicator.source is Source.YAHOO:
            url = f"{settings.yahoo_url}/v8/finance/chart/{indicator.series}"
            body = await self._get(url, {"range": "1mo", "interval": "1d"}, _YAHOO_HEADERS)
            return parse_yahoo(body, self._now())
        return await asyncio.to_thread(_backfilled_closes, settings, indicator.series, start, day)

    async def _get(
        self, url: str, params: Mapping[str, str] | None = None, headers: Mapping[str, str] | None = None
    ) -> Mapping[str, Any]:
        async with self._session.get(url, params=params, headers=headers, timeout=_TIMEOUT) as response:
            text = await response.text()
        try:
            body: Any = json.loads(text)
        except ValueError:
            body = None
        if isinstance(body, dict):
            return body  # an error body too: the parsers read the source's own message
        raise SourceError(
            f"HTTP {response.status}" if response.status >= 400 else "the answer is not a JSON object"
        )


def _backfilled_closes(settings: Settings, series: str, start: date, end: date) -> list[Observation]:
    """A futures series' daily closes, from its backfilled one-minute bars."""
    out = []
    day = start
    while day < end:
        path = settings.data_dir / bars_key(day, FUTURES_BARS_TR, series)
        if path.exists():
            closes = pq.read_table(path, columns=["close"]).column("close").to_pylist()
            if closes and closes[-1] is not None and math.isfinite(closes[-1]):
                out.append(Observation(day, float(closes[-1])))
        day += timedelta(days=1)
    return out


MACRO = "macro"
READING_SCHEMA = pa.schema(
    [
        ("day", pa.date32()),  # the KRX day the reading is for
        ("recorded_at", pa.timestamp("us", tz="UTC")),
        ("indicator", pa.string()),
        ("source", pa.string()),
        ("series", pa.string()),
        ("as_of", pa.date32()),  # the source's day of the value
        ("value", pa.float64()),
        ("change_pct", pa.float64()),  # since the KRX day before
    ]
)
REGIME_SCHEMA = pa.schema(
    [
        ("day", pa.date32()),
        ("recorded_at", pa.timestamp("us", tz="UTC")),
        ("regime", pa.string()),
        ("reasons", pa.string()),  # JSON list, in Korean
        ("failed", pa.string()),  # JSON object: source -> why
        ("skipped", pa.string()),  # JSON list of sources without a key
    ]
)


def macro_recorder(parquet: ParquetRecorder) -> Record:
    """Record a day's readings and regime into the ``macro`` layer at once; later rows win."""

    def record(macro: MacroDay, at: datetime) -> None:
        stamp = at.astimezone(UTC)
        for reading in macro.readings.values():
            current = reading.current
            row = {
                "day": macro.day,
                "recorded_at": stamp,
                "indicator": reading.indicator.name,
                "source": reading.indicator.source.value,
                "series": reading.indicator.series,
                "as_of": current.day if current else None,
                "value": current.value if current else None,
                "change_pct": reading.change_pct,
            }
            parquet.record(MACRO, macro.day, "readings", reading.indicator.name, row, READING_SCHEMA)
        assert macro.decision is not None
        row = {
            "day": macro.day,
            "recorded_at": stamp,
            "regime": macro.decision.regime.value,
            "reasons": json.dumps(macro.decision.reasons, ensure_ascii=False),
            "failed": json.dumps({s.value: why for s, why in macro.failed.items()}, ensure_ascii=False),
            "skipped": json.dumps([s.value for s in macro.skipped]),
        }
        parquet.record(MACRO, macro.day, "regime", "market", row, REGIME_SCHEMA)
        parquet.flush()
        reasons = "; ".join(macro.decision.reasons)
        log.info("macro %s: %s (%s)", macro.day, macro.decision.regime.value, reasons)

    return record


# The Engine API looks this many days before today for the latest day decided (a long
# KRX holiday).
SHOWN_DAYS = 10


@dataclass(frozen=True)
class LatestMacro:
    """A day's latest regime, and the readings of the same read, as recorded."""

    day: date
    decided_at: datetime
    regime: str
    reasons: list[str]
    readings: list[tuple[Indicator, dict[str, Any]]]  # READING_SCHEMA rows, in the indicators' order
    failed: dict[str, str]  # source -> why
    skipped: list[str]  # sources without their API key


def regime_prefix(day: date) -> str:
    """Where ``day``'s regime is recorded, under the data directory or the store."""
    return f"{MACRO}/date={day}/feed=regime/"


def day_regime(data_dir: Path, day: date, before: datetime) -> str | None:
    """``day``'s macro regime as last decided before ``before`` (the open, as the engine
    and training use it), or None. A ``krx-quant macro`` run after the open changes nothing."""
    regimes = _rows(data_dir / regime_prefix(day) / "symbol=market")
    decided = [r for r in regimes if r["recorded_at"] < before]
    if not decided:
        return None
    regime: str = decided[-1]["regime"]
    if regime not in {r.value for r in Regime}:  # a newer collector's: no regime here
        log.warning("unknown macro regime %r recorded for %s: none", regime, day)
        return None
    return regime


@dataclass(frozen=True)
class Decided:
    """A day's regime as last decided before a moment, and which indicators that read had."""

    reasons: list[str]
    valued: set[str]  # the indicators with a value in the same read


def decided_before(data_dir: Path, day: date, before: datetime) -> Decided | None:
    """``day``'s regime as last decided before ``before`` (the open, as ``day_regime``), and
    the indicators valued in that read; None if none was decided by then."""
    root = data_dir / MACRO / f"date={day}"
    decided = [r for r in _rows(root / "feed=regime" / "symbol=market") if r["recorded_at"] < before]
    if not decided:
        return None
    regime = decided[-1]
    valued = {
        indicator.name
        for indicator in INDICATORS
        for r in _rows(root / "feed=readings" / f"symbol={indicator.name}")
        if r["recorded_at"] == regime["recorded_at"] and r["value"] is not None
    }
    return Decided(json.loads(regime["reasons"]), valued)


def reading_before(data_dir: Path, day: date, indicator: str, before: datetime) -> dict[str, Any] | None:
    """``indicator``'s reading in ``day``'s last read recorded before ``before`` (the open,
    as ``day_regime`` takes the regime), or None. A ``krx-quant macro`` run after the open
    changes nothing."""
    rows = _rows(data_dir / MACRO / f"date={day}" / "feed=readings" / f"symbol={indicator}")
    earlier = [r for r in rows if r["recorded_at"] < before]
    return earlier[-1] if earlier else None


def latest_macro(data_dir: Path, today: date) -> LatestMacro | None:
    """The latest day decided by ``today`` (at most ``SHOWN_DAYS`` before it): its last
    regime and the readings it was decided from. A reading of an earlier read that day (a
    collector restarted since) is not one of them.
    """
    for back in range(SHOWN_DAYS + 1):
        root = data_dir / MACRO / f"date={today - timedelta(days=back)}"
        if regimes := _rows(root / "feed=regime" / "symbol=market"):
            break
    else:
        return None
    regime = regimes[-1]
    decided_at = regime["recorded_at"]  # one read records all its rows at the same time
    readings = []
    for indicator in INDICATORS:
        rows = _rows(root / "feed=readings" / f"symbol={indicator.name}")
        if same_read := [r for r in rows if r["recorded_at"] == decided_at]:
            readings.append((indicator, same_read[-1]))
    return LatestMacro(
        day=regime["day"],
        decided_at=decided_at,
        regime=regime["regime"],
        reasons=json.loads(regime["reasons"]),
        readings=readings,
        failed=json.loads(regime["failed"]),
        skipped=json.loads(regime["skipped"]),
    )


def _rows(partition: Path) -> list[dict[str, Any]]:
    """A partition's rows, in the order written."""
    table = read_partition(partition)
    return [] if table is None else table.to_pylist()


def read_partition(partition: Path, columns: Sequence[str] | None = None) -> pa.Table | None:
    """A partition's rows, each once and in the order written (of ``columns`` only, if
    given); None if it holds no file, or not all ``columns``."""
    try:
        return _read_partition(partition, columns)
    except FileNotFoundError:  # the archive merged the parts in between: list them again
        return _read_partition(partition, columns)


def _read_partition(partition: Path, columns: Sequence[str] | None) -> pa.Table | None:
    files = partition_files(partition)
    if not files or (columns is not None and not set(columns) <= set(pq.read_schema(files[0]).names)):
        return None
    chosen = None if columns is None else list(columns)
    return pa.concat_tables(pq.read_table(path, columns=chosen) for path in files)


async def keep_macro(
    collector: MacroCollector,
    calendar: MarketCalendar,
    at: time,
    now: Callable[[], datetime],
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """The collector's macro task, until cancelled: read every source each trading day at
    ``at``, then the failed ones again every ``RETRY_AFTER``, ``TRIES_A_MORNING`` reads in
    all, and only before the open: once the market opens, the day's regime stays. The sleep
    is a parameter for tests.
    """
    start = first_macro_run(calendar, at, now())
    while True:
        await sleep(max(0.0, (start - now()).total_seconds()))
        day = kst_day(start)
        session = calendar.session(day)
        if session is None or now() >= session.open:
            log.warning("macro read skipped on %s: KRX_MACRO_AT %s is not before the open", day, at)
        else:
            await _morning(collector, day, session.open, now, sleep)
        start = next_on_trading_day(calendar, at, max(now(), start))


async def _morning(
    collector: MacroCollector,
    day: date,
    opens: datetime,
    now: Callable[[], datetime],
    sleep: Callable[[float], Awaitable[None]],
) -> None:
    """A trading day's reads: every source, then the failed ones again while a retry still
    lands before ``opens``."""
    sources: Sequence[Source] = tuple(Source)
    for attempt in range(TRIES_A_MORNING):
        try:
            macro = await collector.run(day, sources)
        except Exception:
            log.exception("the macro collection failed")
            return
        sources = list(macro.failed)
        if not sources or attempt == TRIES_A_MORNING - 1:
            return
        if now() + RETRY_AFTER >= opens:
            log.info("macro retries stop: the market opens at %s", opens.time())
            return
        await sleep(RETRY_AFTER.total_seconds())


def first_macro_run(calendar: MarketCalendar, at: time, started: datetime) -> datetime:
    """At once if started after ``at`` but before the open of a trading day (a restart
    that morning), else the next ``at``: once the market opens, the day's regime stays.
    """
    local = started.astimezone(KST)
    session = calendar.session(local.date())
    if session is not None and datetime.combine(local.date(), at, KST) <= local < session.open:
        return started
    return next_on_trading_day(calendar, at, started)


async def run_macro(settings: Settings, now: Callable[[], datetime]) -> MacroDay:
    """``krx-quant macro``: today's indicators and regime, once, recorded into the data dir."""
    parquet = ParquetRecorder(settings.data_dir, flush_rows=1000, flush_seconds=3600)
    try:
        async with aiohttp.ClientSession() as session:
            alerts = Alerts(
                session, settings.telegram_bot_token, settings.telegram_chat_id, settings.telegram_api_url
            )
            try:
                calendar = calendar_store(settings).calendar
                collector = MacroCollector(settings, session, alerts, macro_recorder(parquet), calendar, now)
                return await collector.run(kst_day(now()))
            finally:
                await alerts.close()  # send what is queued before the event loop ends
    finally:
        parquet.close()
