"""Macro Collector: daily indicators from official sources, and the day's market regime.

Each trading day at ``KRX_MACRO_AT`` (07:30 KST, after the US close) the collector reads
the indicators below, each source on its own, and decides the day's regime from them
(``regime.py``). A source that fails is tried again 10 minutes later, up to 3 times a
day; its third failure in a row raises a Telegram alert, cleared once it answers again.
A source without its API key is skipped. ``krx-quant macro`` does the same once.

Readings and the regime are recorded in the ``macro`` layer, one row each, and archived
with the rest. An observation more than 7 days older than the day counts as missing.

Naver's market page is not read: every item on it has an official source, or is not
needed (docs/design.md §4.5).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, NamedTuple
from zoneinfo import ZoneInfo

import aiohttp
import pyarrow as pa
import pyarrow.parquet as pq

from krx_quant.backfill import FUTURES_BARS_TR, bars_key
from krx_quant.config import Settings
from krx_quant.contracts import USD
from krx_quant.feeds.base import KST, kst_day
from krx_quant.market_calendar import MarketCalendar, next_on_trading_day
from krx_quant.notify import Alerts
from krx_quant.recorder import ParquetRecorder
from krx_quant.regime import Decision, RegimeInputs, decide

log = logging.getLogger(__name__)

TRIES = 3  # a day's tries of a failing source; the third failure in a row alerts
RETRY_AFTER = timedelta(minutes=10)
MAX_AGE = timedelta(days=7)  # an older observation is missing
_LOOK_BACK = timedelta(days=14)
_TIMEOUT = aiohttp.ClientTimeout(total=20)
# Yahoo answers clients that look like a browser.
_YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0 (krx-quant macro collector)"}


class SourceError(RuntimeError):
    """A source answered with an error."""


class Observation(NamedTuple):
    day: date  # the day the value is for, in the source's own calendar
    value: float


@dataclass(frozen=True)
class Indicator:
    name: str
    source: str  # ecos, fred, yahoo or kis
    series: str  # the source's code: ECOS "stat/item", a FRED series, a Yahoo symbol
    label: str


# What is read, by source. Every one is read once a trading day.
INDICATORS = (
    Indicator("usdkrw", "ecos", "731Y001/0000001", "원/달러 매매기준율"),
    Indicator("ktb3y", "ecos", "817Y002/010200000", "국고채 3년 금리"),
    Indicator("us10y", "fred", "DGS10", "미국 국채 10년 금리"),
    Indicator("wti", "fred", "DCOILWTICO", "WTI 유가"),
    Indicator("vix", "fred", "VIXCLS", "VIX"),
    Indicator("sox", "yahoo", "^SOX", "필라델피아 반도체지수"),
    Indicator("nq", "yahoo", "NQ=F", "나스닥100 선물"),
    Indicator("nvda", "yahoo", "NVDA", "엔비디아"),
    Indicator("mu", "yahoo", "MU", "마이크론"),
    Indicator("usdf", "kis", USD.series, "달러선물 최근월물 (백필 1분봉의 종가)"),
)
SOURCES = ("ecos", "fred", "yahoo", "kis")


def parse_ecos(body: Mapping[str, Any]) -> list[Observation]:
    """An ECOS StatisticSearch answer (daily), oldest first; "no data" is empty."""
    if "RESULT" in body:
        result = body["RESULT"]
        if result.get("CODE") == "INFO-200":  # 해당하는 데이터가 없습니다
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


def change_pct(observations: Sequence[Observation]) -> float | None:
    """The last value's move from the one before, %; None without two."""
    if len(observations) < 2 or observations[-2].value == 0:
        return None
    return (observations[-1].value / observations[-2].value - 1) * 100


@dataclass(frozen=True)
class Reading:
    indicator: Indicator
    observations: tuple[Observation, ...]  # recent, oldest first; none if missing or too old

    @property
    def last(self) -> Observation | None:
        return self.observations[-1] if self.observations else None

    @property
    def change_pct(self) -> float | None:
        return change_pct(self.observations)


@dataclass
class MacroDay:
    """A day's readings so far, and its regime."""

    day: date
    readings: dict[str, Reading] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)  # source -> why, in the last try
    skipped: list[str] = field(default_factory=list)  # sources without their API key

    def decision(self, settings: Settings) -> Decision:
        def reading(name: str) -> Reading | None:
            return self.readings.get(name)

        sox, vix = reading("sox"), reading("vix")
        # The won: the Bank of Korea's rate, else the USD futures' close.
        usdkrw = next(
            (c for name in ("usdkrw", "usdf") if (r := reading(name)) and (c := r.change_pct) is not None),
            None,
        )
        inputs = RegimeInputs(
            sox_pct=sox.change_pct if sox else None,
            vix=vix.last.value if vix and vix.last else None,
            usdkrw_pct=usdkrw,
        )
        return decide(inputs, settings.regime_rules)


Write = Callable[[MacroDay, datetime], None]


class MacroCollector:
    """Reads the sources for a day, counts each source's failures in a row and alerts."""

    def __init__(
        self,
        settings: Settings,
        session: aiohttp.ClientSession,
        alerts: Alerts | None,
        write: Write,
        now: Callable[[], datetime],
    ) -> None:
        self._settings = settings
        self._session = session
        self._alerts = alerts
        self._write = write
        self._now = now
        self._failures: dict[str, int] = dict.fromkeys(SOURCES, 0)
        self._days: dict[date, MacroDay] = {}

    async def run(self, day: date, sources: Sequence[str] = SOURCES) -> MacroDay:
        """Read ``sources`` for ``day``, keep what each one gave, record the day and its regime."""
        macro = self._days.setdefault(day, MacroDay(day))
        self._days = {day: macro}  # only the day under way is kept
        tried = [s for s in sources if self._has_key(s)]
        macro.skipped = [s for s in SOURCES if not self._has_key(s)]
        results = await asyncio.gather(*(self._read(s, day) for s in tried))
        for source, (readings, errors) in zip(tried, results, strict=True):
            macro.readings.update(readings)  # what answered, even when another indicator failed
            if errors:
                self._failed(macro, source, errors[0])
            else:
                macro.failed.pop(source, None)
                self._answered(source)
        self._write(macro, self._now())
        return macro

    def _has_key(self, source: str) -> bool:
        if source == "ecos":
            return self._settings.ecos_api_key is not None
        if source == "fred":
            return self._settings.fred_api_key is not None
        return True

    def _failed(self, macro: MacroDay, source: str, error: BaseException) -> None:
        why = str(error) or type(error).__name__
        log.warning("macro source %s failed: %s", source, why)
        macro.failed[source] = why
        self._failures[source] += 1
        if self._alerts is not None and self._failures[source] >= TRIES:
            self._alerts.raise_(
                f"macro:{source}", f"매크로 출처 {source}가 {TRIES}번 연속 실패했습니다: {why}"
            )

    def _answered(self, source: str) -> None:
        self._failures[source] = 0
        if self._alerts is not None:
            self._alerts.resolve(f"macro:{source}", f"매크로 출처 {source}가 다시 응답합니다.")

    async def _read(self, source: str, day: date) -> tuple[dict[str, Reading], list[Exception]]:
        """Each of the source's indicators, and the errors of those that failed."""
        readings: dict[str, Reading] = {}
        errors: list[Exception] = []
        for indicator in (i for i in INDICATORS if i.source == source):
            try:
                observations = await self._observations(indicator, day)
            except Exception as e:  # noqa: BLE001 - a source's failure is recorded, not raised
                errors.append(e)
                continue
            recent = tuple(o for o in observations if o.day < day and day - o.day <= MAX_AGE)
            readings[indicator.name] = Reading(indicator, recent[-5:])
        return readings, errors

    async def _observations(self, indicator: Indicator, day: date) -> list[Observation]:
        start = day - _LOOK_BACK
        settings = self._settings
        if indicator.source == "ecos":
            stat, item = indicator.series.split("/")
            url = (
                f"{settings.ecos_url}/StatisticSearch/{settings.ecos_api_key}/json/kr/1/100/"
                f"{stat}/D/{start:%Y%m%d}/{day:%Y%m%d}/{item}"
            )
            return parse_ecos(await self._get(url))
        if indicator.source == "fred":
            params = {
                "series_id": indicator.series,
                "api_key": settings.fred_api_key or "",
                "file_type": "json",
                "observation_start": start.isoformat(),
            }
            return parse_fred(await self._get(f"{settings.fred_url}/fred/series/observations", params))
        if indicator.source == "yahoo":
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
        ("change_pct", pa.float64()),
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


def recorder_writer(recorder: ParquetRecorder, settings: Settings) -> Write:
    """Record a day's readings and regime (latest rows win), at once."""

    def write(macro: MacroDay, at: datetime) -> None:
        stamp = at.astimezone(UTC)
        for reading in macro.readings.values():
            last = reading.last
            row = {
                "day": macro.day,
                "recorded_at": stamp,
                "indicator": reading.indicator.name,
                "source": reading.indicator.source,
                "series": reading.indicator.series,
                "as_of": last.day if last else None,
                "value": last.value if last else None,
                "change_pct": reading.change_pct,
            }
            recorder.record(MACRO, macro.day, "readings", reading.indicator.name, row, READING_SCHEMA)
        decision = macro.decision(settings)
        row = {
            "day": macro.day,
            "recorded_at": stamp,
            "regime": decision.regime.value,
            "reasons": json.dumps(decision.reasons, ensure_ascii=False),
            "failed": json.dumps(macro.failed, ensure_ascii=False),
            "skipped": json.dumps(macro.skipped),
        }
        recorder.record(MACRO, macro.day, "regime", "market", row, REGIME_SCHEMA)
        recorder.flush()
        log.info("macro %s: %s (%s)", macro.day, decision.regime.value, "; ".join(decision.reasons))

    return write


async def keep_macro(
    collector: MacroCollector,
    calendar: MarketCalendar,
    at: time,
    now: Callable[[], datetime],
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """The collector's macro task, until cancelled: each trading day at ``at``, and at once
    if started later on a trading day, read every source, then the failed ones again
    every ``RETRY_AFTER``, ``TRIES`` times in all.
    """
    started = now()
    today = kst_day(started)
    if calendar.is_trading_day(today) and started.astimezone(KST).time() >= at:
        start = started
    else:
        start = next_on_trading_day(calendar, at, started)
    while True:
        await sleep(max(0.0, (start - now()).total_seconds()))
        day = kst_day(start)
        sources: Sequence[str] = SOURCES
        for attempt in range(TRIES):
            try:
                macro = await collector.run(day, sources)
            except Exception:
                log.exception("the macro collection failed")
                break
            sources = list(macro.failed)
            if not sources or attempt == TRIES - 1:
                break
            await sleep(RETRY_AFTER.total_seconds())
        start = next_on_trading_day(calendar, at, max(now(), start))


async def run_macro(settings: Settings, now: Callable[[], datetime]) -> MacroDay:
    """``krx-quant macro``: today's indicators and regime, once, recorded into the data dir."""
    recorder = ParquetRecorder(settings.data_dir, flush_rows=1000, flush_seconds=3600)
    try:
        async with aiohttp.ClientSession() as session:
            collector = MacroCollector(settings, session, None, recorder_writer(recorder, settings), now)
            return await collector.run(kst_day(now()))
    finally:
        recorder.close()
