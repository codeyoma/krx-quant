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
    params: Callable[[str], dict[str, str]]
    # (response body, key, received_at) -> event rows
    normalize: Callable[[dict[str, Any], str, datetime], list[Row]]


RAW_SCHEMA = pa.schema([("received_at", pa.timestamp("us", tz="UTC")), ("message", pa.string())])

EVENT_TIME = pa.timestamp("s", tz="Asia/Seoul")
RECEIVED_AT = pa.timestamp("us", tz="UTC")


def kst(day: date, hhmmss: str) -> datetime:
    return datetime.combine(day, time(int(hhmmss[0:2]), int(hhmmss[2:4]), int(hhmmss[4:6])), KST)


def kst_or_received(day: date, hhmmss: str, received_at: datetime) -> datetime:
    """``hhmmss`` on ``day``, or the second it was received when KIS sends no real time.

    After the evening close KIS sends index (and derivative) records with the final
    values and the time ``999999`` (seen live on 2026-10-08).
    """
    try:
        return kst(day, hhmmss)
    except ValueError:
        return received_at.astimezone(KST).replace(microsecond=0)


def kst_day(received_at: datetime) -> date:
    return received_at.astimezone(KST).date()


def to_int(value: Any) -> int:
    """KIS sends blank numeric fields when a value does not apply; record those as 0."""
    text = str(value).strip()
    return int(float(text)) if text else 0


def to_float(value: Any) -> float:
    text = str(value).strip()
    return float(text) if text else 0.0
