"""Shared types and helpers for feed definitions."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import pyarrow as pa

KST = ZoneInfo("Asia/Seoul")

Row = dict[str, Any]

LEVELS = range(1, 11)


@dataclass(frozen=True)
class Feed:
    """A KIS real-time WebSocket feed: column layout and normalized event schema."""

    tr_id: str
    columns: tuple[str, ...]
    schema: pa.Schema
    normalize: Callable[[dict[str, str], datetime], Row]


@dataclass(frozen=True)
class RestFeed:
    """A KIS quotation endpoint polled over REST, keyed by e.g. a symbol."""

    tr_id: str
    path: str
    schema: pa.Schema
    params: Callable[[str, date], dict[str, str]]  # (key, the day polled) -> query
    # (response body, key, received_at) -> event rows
    normalize: Callable[[dict[str, Any], str, datetime], list[Row]]
    # The WebSocket feed this endpoint stands in for when its registration is cut, if its
    # events are that feed's: they are recorded, and read by the engine, under its name
    # (the answers themselves stay under this TR).
    stands_in_for: str | None = None

    @property
    def events_feed(self) -> str:
        """The feed this endpoint's events are recorded under."""
        return self.stands_in_for or self.tr_id


RAW_SCHEMA = pa.schema([("received_at", pa.timestamp("us", tz="UTC")), ("message", pa.string())])

EVENT_TIME = pa.timestamp("s", tz="Asia/Seoul")
RECEIVED_AT = pa.timestamp("us", tz="UTC")


def kst(day: date, hhmmss: str) -> datetime:
    return datetime.combine(day, time(int(hhmmss[0:2]), int(hhmmss[2:4]), int(hhmmss[4:6])), KST)


# After the evening close KIS sends index (and derivative) records with the day's final
# values and this in place of a time (seen live on 2026-10-08).
CLOSING_TIME = "999999"


def received_second(received_at: datetime) -> datetime:
    """The time of a record that carries none: the second it was received, in KST."""
    return received_at.astimezone(KST).replace(microsecond=0)


def kst_or_received(hhmmss: str, received_at: datetime) -> datetime:
    """``hhmmss`` on the day received, or the second received for a closing record.

    Any other value that is not a time raises ValueError, so the record stays raw only.
    """
    if hhmmss == CLOSING_TIME:
        return received_second(received_at)
    return kst(kst_day(received_at), hhmmss)


def kst_day(received_at: datetime) -> date:
    return received_at.astimezone(KST).date()


def to_int(value: Any) -> int:
    """KIS sends blank numeric fields when a value does not apply; record those as 0."""
    text = str(value).strip()
    return int(float(text)) if text else 0


def to_float(value: Any) -> float:
    text = str(value).strip()
    return float(text) if text else 0.0
