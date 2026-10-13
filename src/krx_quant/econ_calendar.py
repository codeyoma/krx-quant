"""Economic Calendar (#150): the scheduled macro releases of the coming week.

Every ``KRX_CALENDAR_EVERY`` minutes the collector reads TradingView's public economic
calendar for ``KRX_CALENDAR_COUNTRIES``, from the start of yesterday (KST) to seven days
ahead. The feed needs no key and carries what the screen's calendar shows (release time,
period, actual, forecast, previous, importance). It is not an official API, so it can change
or refuse without notice: a failed read keeps the last good answer, and ``ALERT_AFTER``
failures in a row raise a Telegram alert, cleared once it answers again.

The latest answer is written whole to ``<data>/calendar/latest.json`` (a temporary file
renamed over it), which the Engine API serves as ``/v1/calendar``; a restarted collector
serves the last one at once. Every new or changed event (a forecast moving, the actual
arriving) is recorded in the ``calendar`` layer, ``date=<day read>/feed=tradingview/
symbol=<country>``, so later features can see what was known when. The archive uploads that
layer like the others and leaves ``latest.json`` alone.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any

import aiohttp
import pyarrow as pa

from krx_quant.feeds.base import KST, kst_day
from krx_quant.notify import Alerts
from krx_quant.recorder import ParquetRecorder

log = logging.getLogger(__name__)

DEFAULT_URL = "https://economic-calendar.tradingview.com/events"
DEFAULT_COUNTRIES = ("US", "KR", "CN", "JP", "EU", "DE", "GB")
SOURCE = "tradingview"
LAYER = "calendar"
LATEST = Path(LAYER) / "latest.json"
LOOK_BACK = timedelta(days=1)  # from the start of yesterday (KST): today's released actuals
LOOK_AHEAD = timedelta(days=7)
ALERT_AFTER = 4  # failures in a row before the alert: an hour at the default 15 minutes
ALERT_KEY = "calendar"
_TIMEOUT = aiohttp.ClientTimeout(total=30)
# The feed answers requests from TradingView's own pages.
_HEADERS = {
    "Origin": "https://www.tradingview.com",
    "User-Agent": "Mozilla/5.0 (krx-quant calendar collector)",
}
IMPORTANCE = {-1: "low", 0: "medium", 1: "high"}

EVENT_SCHEMA = pa.schema(
    [
        ("recorded_at", pa.timestamp("us", tz="UTC")),
        ("id", pa.string()),
        ("at", pa.timestamp("us", tz="UTC")),
        ("country", pa.string()),
        ("title", pa.string()),
        ("indicator", pa.string()),
        ("category", pa.string()),
        ("period", pa.string()),
        ("actual", pa.float64()),
        ("forecast", pa.float64()),
        ("previous", pa.float64()),
        ("unit", pa.string()),
        ("currency", pa.string()),
        ("importance", pa.string()),
        ("source", pa.string()),
        ("source_url", pa.string()),
    ]
)


class CalendarError(RuntimeError):
    """The feed answered with something that is not a calendar."""


@dataclass(frozen=True)
class CalendarEvent:
    """One scheduled release. ``at`` is when it comes out; values are the feed's numbers, in
    ``unit`` (``%``, ``B`` and the like), or None while not known."""

    id: str
    at: datetime
    country: str
    title: str
    indicator: str | None
    category: str | None
    period: str | None
    actual: float | None
    forecast: float | None
    previous: float | None
    unit: str | None
    currency: str | None
    importance: str  # low, medium, high
    source: str | None
    source_url: str | None

    def to_json(self) -> dict[str, Any]:
        """The Engine API's form: ``at`` in KST with its offset."""
        body = asdict(self)
        body["at"] = self.at.astimezone(KST).isoformat()
        return body

    @classmethod
    def from_json(cls, body: Mapping[str, Any]) -> CalendarEvent:
        fields = dict(body)
        fields["at"] = datetime.fromisoformat(body["at"])
        return cls(**fields)

    def known(self) -> tuple[Any, ...]:
        """What a change of is worth recording."""
        return (self.at, self.title, self.period, self.actual, self.forecast, self.previous, self.importance)


def _text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def parse_tradingview(body: Any) -> list[CalendarEvent]:
    """The feed's answer, ``{"status": "ok", "result": [...]}``, as events in time order. An
    item without an id, a time or a title is left out."""
    if not isinstance(body, Mapping) or not isinstance(body.get("result"), list):
        raise CalendarError(f"not a calendar answer: {str(body)[:200]}")
    events = []
    for item in body["result"]:
        if not isinstance(item, Mapping):
            continue
        ident, title, when = _text(item.get("id")), _text(item.get("title")), _text(item.get("date"))
        if ident is None or title is None or when is None:
            continue
        try:
            at = datetime.fromisoformat(when)
        except ValueError:
            continue
        if at.tzinfo is None:
            continue
        importance = item.get("importance")
        events.append(
            CalendarEvent(
                id=ident,
                at=at.astimezone(UTC),
                country=(_text(item.get("country")) or "").upper(),
                title=title,
                indicator=_text(item.get("indicator")),
                category=_text(item.get("category")),
                period=_text(item.get("period")),
                actual=_number(item.get("actual")),
                forecast=_number(item.get("forecast")),
                previous=_number(item.get("previous")),
                unit=_text(item.get("unit")),
                currency=_text(item.get("currency")),
                importance=IMPORTANCE.get(importance, "low") if isinstance(importance, int) else "low",
                source=_text(item.get("source")),
                source_url=_text(item.get("source_url")),
            )
        )
    events.sort(key=lambda e: (e.at, e.country, e.title))
    return events


def query(countries: Sequence[str], now: datetime) -> dict[str, str]:
    """The feed's query: from the start of yesterday (KST) for ``LOOK_BACK + LOOK_AHEAD``."""
    start = datetime.combine(kst_day(now), time(0), KST) - LOOK_BACK
    end = start + LOOK_BACK + LOOK_AHEAD
    stamp = "%Y-%m-%dT%H:%M:%S.000Z"
    return {
        "from": start.astimezone(UTC).strftime(stamp),
        "to": end.astimezone(UTC).strftime(stamp),
        "countries": ",".join(countries),
    }


def latest_path(data_dir: Path) -> Path:
    return data_dir / LATEST


def read_latest(data_dir: Path) -> dict[str, Any] | None:
    """The last answer written, or None before any (or if it cannot be read)."""
    path = latest_path(data_dir)
    try:
        body = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        log.warning("cannot read %s: %s", path, e)
        return None
    return body if isinstance(body, dict) else None


def _write_latest(data_dir: Path, body: Mapping[str, Any]) -> None:
    path = latest_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(body, ensure_ascii=False))
    tmp.replace(path)


class CalendarCollector:
    """Reads the feed, keeps the latest answer and records what changed."""

    def __init__(
        self,
        *,
        session: aiohttp.ClientSession,
        data_dir: Path,
        countries: Sequence[str],
        alerts: Alerts,
        recorder: ParquetRecorder | None,
        now: Callable[[], datetime],
        url: str = DEFAULT_URL,
    ) -> None:
        self._session = session
        self._data_dir = data_dir
        self._countries = tuple(countries)
        self._alerts = alerts
        self._recorder = recorder
        self._now = now
        self._url = url
        self.failures = 0
        # Seeded from the answer kept on disk, so a restart does not record every event again.
        self._known: dict[str, tuple[Any, ...]] = {}
        kept = read_latest(data_dir)
        for item in (kept or {}).get("events", []):
            try:
                event = CalendarEvent.from_json(item)
            except (KeyError, TypeError, ValueError):
                continue
            self._known[event.id] = event.known()

    async def run(self) -> bool:
        """One read. True when it answered; a failure keeps the last answer."""
        at = self._now()
        try:
            async with self._session.get(
                self._url, params=query(self._countries, at), headers=_HEADERS, timeout=_TIMEOUT
            ) as response:
                if response.status != 200:
                    raise CalendarError(f"HTTP {response.status}")
                events = parse_tradingview(await response.json(content_type=None))
        except (aiohttp.ClientError, TimeoutError, CalendarError, ValueError) as e:
            self.failures += 1
            log.warning("economic calendar read failed (%d in a row): %s", self.failures, e)
            if self.failures >= ALERT_AFTER:
                self._alerts.raise_(
                    ALERT_KEY, f"경제 캘린더(TradingView)를 {self.failures}번 연속 읽지 못했습니다: {e}"
                )
            return False

        changed = [e for e in events if self._known.get(e.id) != e.known()]
        body = {
            "fetched_at": at.astimezone(KST).isoformat(),
            "source": SOURCE,
            "countries": list(self._countries),
            "events": [e.to_json() for e in events],
        }
        await asyncio.to_thread(_write_latest, self._data_dir, body)
        if self._recorder is not None and changed:
            self._record(changed, at)
        for event in events:
            self._known[event.id] = event.known()
        if self.failures >= ALERT_AFTER:
            self._alerts.resolve(ALERT_KEY, "경제 캘린더를 다시 읽습니다.")
        self.failures = 0
        log.info("economic calendar: %d events, %d new or changed", len(events), len(changed))
        return True

    def _record(self, events: Sequence[CalendarEvent], at: datetime) -> None:
        assert self._recorder is not None
        day = kst_day(at)
        stamp = at.astimezone(UTC)
        for e in events:
            row = {"recorded_at": stamp, **asdict(e)}
            self._recorder.record(LAYER, day, SOURCE, e.country or "XX", row, EVENT_SCHEMA)
        self._recorder.flush()


async def keep_calendar(
    collector: CalendarCollector,
    every: timedelta,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """The collector's calendar task, until cancelled: read now, then every ``every``. The
    sleep is a parameter for tests."""
    while True:
        try:
            await collector.run()
        except Exception:
            log.exception("the economic calendar read failed")
        await sleep(every.total_seconds())


def calendar_answer(kept: Mapping[str, Any] | None, now: datetime, limit: int) -> dict[str, Any]:
    """``/v1/calendar``'s answer from the kept one: the events from the start of today (KST)
    on, in time order, at most ``limit``."""
    if kept is None:
        return {"fetched_at": None, "source": SOURCE, "countries": [], "events": []}
    start = datetime.combine(kst_day(now), time(0), KST)
    events = []
    for item in kept.get("events", []):
        try:
            if datetime.fromisoformat(item["at"]) >= start:
                events.append(item)
        except (KeyError, TypeError, ValueError):
            continue
    return {
        "fetched_at": kept.get("fetched_at"),
        "source": kept.get("source", SOURCE),
        "countries": list(kept.get("countries", [])),
        "events": events[:limit],
    }
