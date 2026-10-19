"""``GET /v1/symbols/{symbol}/flow`` (#306): a symbol's intraday flows of a day, read from the
recorded feeds, for the flow screen to draw over the day's 1-minute prices.

Three feeds the engine records (``docs/events.md``) and no answer showed until now:

- **Program trading** (``H0UNPGM0``; its REST stand-in writes the same feed): the day's
  cumulative program buys, sells and net, by the record's exchange time. A row the REST
  stand-in wrote has no resting orders (``whole_net_qty`` empty): its minute is ``rest``.
- **Member firms** (``H0UNMBC0``, and its REST snapshot ``FHKST01010600`` under its own name):
  the top five selling and buying members and the foreign members' cumulative net, by the time
  received (the frames carry none).
- **The investor estimates** (``HHPTJ04160200``, polled every 5 minutes): brokers' provisional
  foreign and institutional net buys, entered in a few slots a day. Each poll gives every slot so
  far; a slot is shown from when it first had a value, with its latest value.

Each minute of the regular session (the open to the close, the closing print's minute too)
holds the last value recorded in that minute; a minute with none is empty: a silent feed is not
shown as flat. The day's 1-minute prices come from the caller (the engine's bars today, else
the backfill). Only the server's local recording is read, never the Object Store: a day with
none is ``recorded: false``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from krx_quant.feeds import FOREIGN_ESTIMATE, MEMBER, MEMBER_REST, PROGRAM
from krx_quant.feeds.base import KST
from krx_quant.macro import read_partition
from krx_quant.predictions import iso
from krx_quant.supply import KIS, read_supply

MEMBERS = 5  # the top members shown a side
_PROGRAM_COLUMNS = (
    "event_time",
    "received_at",
    "buy_value",
    "sell_value",
    "net_value",
    "net_qty",
    "whole_net_qty",
)
_MEMBER_COLUMNS = (
    "received_at",
    "foreign_net_qty",
    *[
        f"{side}_member_{i}_{f}"
        for side in ("sell", "buy")
        for i in range(1, MEMBERS + 1)
        for f in ("no", "name", "qty", "foreign")
    ],
)
_ESTIMATE_COLUMNS = ("received_at", "slot", "foreign_net_qty", "institution_net_qty")


@dataclass(frozen=True)
class Window:
    """The regular session of the day asked: its minutes run from ``open`` to ``close``."""

    day: date
    open: datetime
    close: datetime

    def minutes(self) -> list[datetime]:
        out, at = [], self.open
        while at <= self.close:
            out.append(at)
            at += timedelta(minutes=1)
        return out


def _minute(at: datetime) -> datetime:
    return at.astimezone(KST).replace(second=0, microsecond=0)


def _partition(data_dir: Path, day: date, feed: str, symbol: str) -> Path:
    return data_dir / "events" / f"date={day.isoformat()}" / f"feed={feed}" / f"symbol={symbol}"


def _rows(
    data_dir: Path | None, day: date, feed: str, symbol: str, columns: Sequence[str]
) -> list[dict[str, Any]]:
    if data_dir is None:
        return []
    table = read_partition(_partition(data_dir, day, feed, symbol), columns)
    return [] if table is None else table.to_pylist()


def recorded(data_dir: Path | None, day: date, symbol: str) -> bool:
    """Whether any of the three feeds was recorded for ``symbol`` on ``day``, on this server."""
    if data_dir is None:
        return False
    feeds = (PROGRAM.tr_id, MEMBER.tr_id, MEMBER_REST.tr_id, FOREIGN_ESTIMATE.tr_id)
    return any(_partition(data_dir, day, feed, symbol).is_dir() for feed in feeds)


def program_rows(data_dir: Path | None, day: date, symbol: str) -> list[dict[str, Any]]:
    """The day's program trading, in exchange-time order (then as received)."""
    rows = _rows(data_dir, day, PROGRAM.tr_id, symbol, _PROGRAM_COLUMNS)
    return sorted(rows, key=lambda r: (r["event_time"], r["received_at"]))


def member_rows(data_dir: Path | None, day: date, symbol: str) -> list[dict[str, Any]]:
    """The day's member-firm rows of the WebSocket feed and its REST snapshot, as received;
    each with its ``source``."""
    rows = []
    for feed, source in ((MEMBER.tr_id, "ws"), (MEMBER_REST.tr_id, "rest")):
        rows += [r | {"source": source} for r in _rows(data_dir, day, feed, symbol, _MEMBER_COLUMNS)]
    return sorted(rows, key=lambda r: r["received_at"])


def estimates(data_dir: Path | None, day: date, symbol: str) -> list[dict[str, Any]]:
    """The day's estimate slots, in the order they first had a value: when that was, and each
    investor's latest value in it (None until entered)."""
    slots: dict[str, dict[str, Any]] = {}
    rows = sorted(
        _rows(data_dir, day, FOREIGN_ESTIMATE.tr_id, symbol, _ESTIMATE_COLUMNS),
        key=lambda r: r["received_at"],
    )
    for r in rows:
        foreign, institution = r["foreign_net_qty"], r["institution_net_qty"]
        if foreign is None and institution is None:
            continue  # nothing entered in that slot yet
        kept = slots.setdefault(
            r["slot"],
            {
                "slot": r["slot"],
                "received_at": r["received_at"],
                "foreign_net_qty": None,
                "institution_net_qty": None,
            },
        )
        if foreign is not None:
            kept["foreign_net_qty"] = foreign
        if institution is not None:
            kept["institution_net_qty"] = institution
    return sorted(slots.values(), key=lambda s: (s["received_at"], s["slot"]))


def _last_by_minute(rows: Sequence[dict[str, Any]], at: str) -> dict[datetime, dict[str, Any]]:
    out: dict[datetime, dict[str, Any]] = {}
    for r in rows:  # in order: the last of a minute wins
        out[_minute(r[at])] = r
    return out


def _last_until(rows: Sequence[dict[str, Any]], at: str, until: datetime) -> dict[str, Any] | None:
    found = None
    for r in rows:
        if r[at] <= until:
            found = r
    return found


def _members(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None

    def side(name: str) -> list[dict[str, Any]]:
        out = []
        for i in range(1, MEMBERS + 1):
            member_name = row[f"{name}_member_{i}_name"]
            if not member_name and not row[f"{name}_member_{i}_qty"]:
                continue  # fewer members than five traded
            out.append(
                {
                    "no": row[f"{name}_member_{i}_no"],
                    "name": member_name,
                    "qty": row[f"{name}_member_{i}_qty"],
                    "foreign": bool(row[f"{name}_member_{i}_foreign"]),
                }
            )
        return out

    return {
        "received_at": iso(row["received_at"]),
        "source": row["source"],
        "sell": side("sell"),
        "buy": side("buy"),
    }


def _totals(data_dir: Path | None, day: date, symbol: str, until: datetime) -> dict[str, Any]:
    """Each flow's day total as last recorded by ``until`` (that day's clock)."""
    program = _last_until(program_rows(data_dir, day, symbol), "event_time", until)
    member = _last_until(member_rows(data_dir, day, symbol), "received_at", until)
    known = [s for s in estimates(data_dir, day, symbol) if s["received_at"] <= until]
    foreign = next((s["foreign_net_qty"] for s in reversed(known) if s["foreign_net_qty"] is not None), None)
    institution = next(
        (s["institution_net_qty"] for s in reversed(known) if s["institution_net_qty"] is not None), None
    )
    return {
        "program_net_value": None if program is None else program["net_value"],
        "member_foreign_net_qty": None if member is None else member["foreign_net_qty"],
        "estimate_foreign_net_qty": foreign,
        "estimate_institution_net_qty": institution,
    }


def flow(
    data_dir: Path | None,
    state_dir: Path | None,
    symbol: str,
    window: Window,
    previous: Window | None,
    prices: Sequence[tuple[datetime, float]],
    now: datetime,
    special: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """The flow answer of ``symbol`` on ``window.day``. ``prices`` are the day's 1-minute bars
    (start, close); ``previous`` the trading day before, for the totals at the same clock time
    (now's on today, the close on a past day); ``special`` the day's special windows."""
    day = window.day
    program = program_rows(data_dir, day, symbol)
    members = member_rows(data_dir, day, symbol)
    by_minute_program = _last_by_minute(program, "event_time")
    by_minute_member = _last_by_minute(members, "received_at")
    close_of = {_minute(at): close for at, close in prices}
    minutes = []
    for at in window.minutes():
        p, m = by_minute_program.get(at), by_minute_member.get(at)
        minutes.append(
            {
                "at": iso(at),
                "price": close_of.get(at),
                "program_net_value": None if p is None else p["net_value"],
                "program_buy_value": None if p is None else p["buy_value"],
                "program_sell_value": None if p is None else p["sell_value"],
                "program_net_qty": None if p is None else p["net_qty"],
                "program_source": None if p is None else ("rest" if p["whole_net_qty"] is None else "ws"),
                "member_foreign_net_qty": None if m is None else m["foreign_net_qty"],
                "member_source": None if m is None else m["source"],
            }
        )
    local_now = now.astimezone(KST)
    clock = min(local_now.time(), window.close.time()) if local_now.date() == day else window.close.time()
    same_time = None
    if previous is not None:
        same_time = {
            "day": previous.day.isoformat(),
            "at": iso(datetime.combine(previous.day, clock, KST)),
            **_totals(data_dir, previous.day, symbol, datetime.combine(previous.day, clock, KST)),
        }
    final = None
    if state_dir is not None:
        row = next((r for r in read_supply(state_dir, symbol, KIS) if r["date"] == day), None)
        if row is not None:
            final = {"foreign_net": row.get("foreign_net"), "institution_net": row.get("institution_net")}
    return {
        "symbol": symbol,
        "day": day.isoformat(),
        "as_of": iso(now),
        "recorded": recorded(data_dir, day, symbol),
        "open": iso(window.open),
        "close": iso(window.close),
        "minutes": minutes,
        "estimates": [
            {
                "slot": s["slot"],
                "received_at": iso(s["received_at"]),
                "foreign_net_qty": s["foreign_net_qty"],
                "institution_net_qty": s["institution_net_qty"],
            }
            for s in estimates(data_dir, day, symbol)
        ],
        "members": _members(members[-1] if members else None),
        "totals": _totals(data_dir, day, symbol, datetime.combine(day, clock, KST)),
        "previous_day_same_time": same_time,
        "final": final,
        "special": list(special),
    }
