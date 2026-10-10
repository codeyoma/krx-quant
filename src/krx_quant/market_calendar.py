"""Market Calendar: KRX trading days and session phases.

Trading days come from the KIS holiday API (``CalendarStore``). Two kinds of day
move the session (both checked on 2026-10-08 against past KRX notices):

- the first trading day of the year opens at 10:00, after the opening ceremony, and
  closes as usual at 15:30;
- the college entrance exam day (수능) opens at 10:00 and closes at 16:30.

The opening auction takes orders for the 30 minutes before the open, the closing
auction runs the last 10 minutes.

Out of scope: the KRX after-market (16:00–20:00, since 2026-09-14) and NXT-only hours.
"""

from __future__ import annotations

import enum
import json
import logging
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

import aiohttp

from krx_quant.config import Settings
from krx_quant.feeds.base import KST, kst_day
from krx_quant.kis import KisError, RestClient

log = logging.getLogger(__name__)

REGULAR_OPEN, REGULAR_CLOSE = time(9, 0), time(15, 30)
LATE_OPEN = time(10, 0)
EXAM_DAY_CLOSE = time(16, 30)
OPENING_AUCTION = timedelta(minutes=30)  # orders are taken from 08:30 for the 09:00 open
CLOSING_AUCTION = timedelta(minutes=10)
# KRX never trades on New Year's Day or on the year-end closing day.
ALWAYS_CLOSED = frozenset({(1, 1), (12, 31)})

# Exam days, when KRX opens and closes an hour late. The ministry fixes each year's date
# more than a year ahead; add it here.
EXAM_DAYS = frozenset({date(2025, 11, 13), date(2026, 11, 19)})


class Phase(enum.Enum):
    CLOSED = "closed"  # not a trading day
    PRE_OPEN = "pre_open"
    OPENING_AUCTION = "opening_auction"  # the opening call auction
    CONTINUOUS = "continuous"  # continuous trading: the prediction window
    CLOSING_AUCTION = "closing_auction"  # the closing call auction
    AFTER_CLOSE = "after_close"


@dataclass(frozen=True)
class Session:
    day: date
    open: datetime  # continuous trading starts
    close: datetime  # the closing auction ends

    @property
    def closing_auction(self) -> datetime:
        return self.close - CLOSING_AUCTION

    def phase(self, at: datetime) -> Phase:
        if at < self.open - OPENING_AUCTION:
            return Phase.PRE_OPEN
        if at < self.open:
            return Phase.OPENING_AUCTION
        if at < self.closing_auction:
            return Phase.CONTINUOUS
        if at < self.close:
            return Phase.CLOSING_AUCTION
        return Phase.AFTER_CLOSE


class MarketCalendar:
    """Which days KRX trades, and the session on each.

    Days KIS has not reported are guessed: weekdays trade, except New Year's Day and the
    year-end closing day. ``knows`` tells reported days from guessed ones.
    """

    def __init__(self, open_days: Mapping[date, bool] | None = None) -> None:
        self._open: dict[date, bool] = dict(open_days or {})

    def update(self, open_days: Mapping[date, bool]) -> None:
        self._open.update(open_days)

    def knows(self, day: date) -> bool:
        return day in self._open

    def is_trading_day(self, day: date) -> bool:
        known = self._open.get(day)
        if known is not None:
            return known
        return day.weekday() < 5 and (day.month, day.day) not in ALWAYS_CLOSED

    def trading_days(self, start: date, end: date) -> list[date]:
        """Days in ``[start, end]`` that KIS reported as trading days (guesses excluded)."""
        return sorted(d for d, is_open in self._open.items() if is_open and start <= d <= end)

    def known_days(self) -> dict[date, bool]:
        return dict(self._open)

    def session(self, day: date) -> Session | None:
        if not self.is_trading_day(day):
            return None
        open_at, close_at = REGULAR_OPEN, REGULAR_CLOSE
        if day in EXAM_DAYS:
            open_at, close_at = LATE_OPEN, EXAM_DAY_CLOSE
        elif self._first_of_year(day):
            open_at = LATE_OPEN
        return Session(day, datetime.combine(day, open_at, KST), datetime.combine(day, close_at, KST))

    def phase(self, at: datetime) -> Phase:
        session = self.session(kst_day(at))
        return Phase.CLOSED if session is None else session.phase(at.astimezone(KST))

    def _first_of_year(self, day: date) -> bool:
        d = date(day.year, 1, 1)
        while d < day:
            if self.is_trading_day(d):
                return False
            d += timedelta(days=1)
        return True


def next_on_trading_day(calendar: MarketCalendar, at: time, after: datetime) -> datetime:
    """The first ``at`` (KST) on a trading day later than ``after``."""
    day = after.astimezone(KST).date()
    while True:
        moment = datetime.combine(day, at, KST)
        if moment > after and calendar.is_trading_day(day):
            return moment
        day += timedelta(days=1)


def calendar_store(settings: Settings) -> CalendarStore:
    """The store kept in the state directory, covering the one-minute bar backfill window."""
    return CalendarStore(settings.state_dir / "market_calendar.json", settings.backfill_days)


HOLIDAYS_TR = "CTCA0903R"
HOLIDAYS_PATH = "/uapi/domestic-stock/v1/quotations/chk-holiday"


class CalendarStore:
    """Keeps a ``MarketCalendar`` filled from the KIS holiday API, cached on disk.

    KIS asks for about one holiday query a day, and one query answers about 24 days
    from a given date. So the store reads the past ``history_days`` once, page by page,
    then one page from the current day on per day; past days never change. A failed
    query is logged and the calendar keeps what it has.
    """

    def __init__(self, path: Path, history_days: int) -> None:
        self.calendar = MarketCalendar()
        self._path = path
        self._history = timedelta(days=history_days)
        self._refreshed_on: date | None = None
        self._load()

    async def refresh(self, client: RestClient, today: date) -> None:
        start = today - self._history
        missing = next((d for d in _days(start, today) if not self.calendar.knows(d)), None)
        if missing is None and self._refreshed_on == today:
            return
        day = missing or today
        try:
            while day <= today:
                page = await _holiday_page(client, day)
                if not page or max(page) < day:
                    raise KisError(f"holiday query from {day} answered no days")
                self.calendar.update(page)
                day = max(page) + timedelta(days=1)
        except (KisError, aiohttp.ClientError, OSError, ValueError, KeyError, TimeoutError) as e:
            log.error("market calendar not refreshed (%s); using what is cached", e)
        else:
            self._refreshed_on = today
        self._save()

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text())
            self.calendar.update({date.fromisoformat(d): bool(v) for d, v in data["open_days"].items()})
            self._refreshed_on = date.fromisoformat(data["refreshed_on"]) if data["refreshed_on"] else None
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return

    def _save(self) -> None:
        data = {
            "refreshed_on": self._refreshed_on.isoformat() if self._refreshed_on else None,
            "open_days": {d.isoformat(): v for d, v in sorted(self.calendar.known_days().items())},
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=0))
        tmp.replace(self._path)


async def _holiday_page(client: RestClient, start: date) -> dict[date, bool]:
    """Days from ``start`` on, open or not (``opnd_yn``: the stock market opens that day).

    KIS lists every calendar day; one it leaves out of the page's span counts as closed.
    """
    params = {"BASS_DT": start.strftime("%Y%m%d"), "CTX_AREA_NK": "", "CTX_AREA_FK": ""}
    body = await client.get(HOLIDAYS_TR, HOLIDAYS_PATH, params)
    listed = {
        datetime.strptime(row["bass_dt"], "%Y%m%d").date(): row["opnd_yn"] == "Y"
        for row in body.get("output") or []
    }
    if not listed:
        return {}
    return {day: listed.get(day, False) for day in _days(min(start, *listed), max(listed))}


def _days(start: date, end: date) -> Iterator[date]:
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)
