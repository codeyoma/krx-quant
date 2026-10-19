"""``GET /v1/briefing`` (#304): what the pre-open briefing screen shows of a trading day.

Everything a day's open is weighed with, in one answer, each part read from the files the
engine keeps and computed by the same rules as the inputs of the 08:50 ``open_to_close``
prediction (``daily_features``), so the screen and the model never show two numbers:

- **overnight**: the day's macro read (``/v1/macro``'s answer, ``macro.latest_macro``) and the
  last US closes known that morning (SOX, NVDA, MU, VIX, ``us_daily``);
- **night futures**: the KOSPI200 night session begun on the previous trading day's evening
  (``H0MFCNT0``, 18:00–06:00), from the recording;
- **each symbol**: the previous trading day's KRX close (its KRX day candle), the integrated
  price at 20:00 that evening (the kept one-minute bars), the NXT pre-market's last trade
  (``daily_features.premarket_window``: 08:40–08:50, else 08:00–08:30, received before 08:50),
  the opening auction's last expected price (the book's ``expected_px``, #304) and the previous
  trading day's net buys (KIS's daily supply);
- **open to close**: the day's 08:50 predictions as ``/v1/symbols/{symbol}/kinds/open_to_close``
  gives them, with each daily algorithm's inputs and their parts in its forecast (#304); before
  the day's are made, the latest day's before it;
- **schedule**: the day's economic calendar events and its flags (the KOSPI200 options' and
  futures' expiry, the month's end and start, a session that opens late).

A part without its data is there with nulls (or ``recorded: false``), never left out.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from krx_quant.auctions import days_of, read_auctions
from krx_quant.backfill import MINUTE_BARS_TR, read_bars
from krx_quant.candles import read_krx_days
from krx_quant.contracts import KOSPI200, last_trading_day
from krx_quant.daily import DailyKind
from krx_quant.daily_features import PREMARKET, month_flags, night_window, premarket_window
from krx_quant.econ_calendar import calendar_answer, read_latest
from krx_quant.feeds import BOOK, NIGHT_FUTURES, TRADE
from krx_quant.feeds.base import KST, kst_day
from krx_quant.macro import latest_macro, macro_json, read_partition
from krx_quant.market_calendar import EXAM_DAYS, OPENING_AUCTION, MarketCalendar
from krx_quant.predictions import PredictionStore, iso
from krx_quant.stream import kind_prediction_json, kind_target
from krx_quant.supply import KIS, read_supply
from krx_quant.us_daily import US_DAILY, read_us_daily

US_DAYS = 5  # the US closes shown before the day
SUPPLY_DAYS = 5  # the trading days the net buys are summed over
AFTER_HOURS_END = time(20, 0)  # the integrated market's last minute: KRX's and NXT's after-market
EXPECTED_FROM = OPENING_AUCTION  # before the open: KRX takes the auction's orders from 08:30
_MAX_EVENTS = 1000


@dataclass(frozen=True)
class Sources:
    """What a briefing is read from."""

    data_dir: Path | None
    state_dir: Path | None
    store: PredictionStore
    calendar: MarketCalendar
    symbols: tuple[str, ...]
    open_to_close: DailyKind | None  # the daily kind, when the engine predicts it
    now: datetime


def default_day(calendar: MarketCalendar, now: datetime) -> date:
    """Today if it is a trading day, else the next trading day."""
    day = kst_day(now)
    while not calendar.is_trading_day(day):
        day += timedelta(days=1)
    return day


@dataclass(frozen=True)
class DayPredictions:
    """The ``open_to_close`` predictions a briefing shows, read from the store (on the thread
    that owns it): the day's, else the latest day's before it (``previous``)."""

    rows: Mapping[str, list[dict[str, Any]]]  # symbol -> its predictions of that day
    previous: bool


def day_predictions(src: Sources, day: date) -> DayPredictions | None:
    """The predictions ``briefing`` shows of ``day``; None without the daily kind."""
    kind = src.open_to_close
    if kind is None:
        return None
    at, version = kind.predicted_at(day), kind.answer_version
    rows = {s: src.store.kind_at_close(kind.name, version, s, at) for s in src.symbols}
    if not any(rows.values()):
        earlier = {s: src.store.kind_recent(kind.name, version, s, 1, before=at) for s in src.symbols}
        if any(earlier.values()):
            return DayPredictions(earlier, previous=True)
    return DayPredictions(rows, previous=False)


def briefing(src: Sources, day: date, predictions: DayPredictions | None) -> dict[str, Any]:
    """The briefing of ``day``, with its ``predictions`` (``day_predictions``). It reads files
    only, so it may run in a worker thread."""
    calendar = src.calendar
    session = calendar.session(day)
    previous = calendar.previous_trading_day(day)
    special = None
    if session is not None and session.open.time() != time(9, 0):
        special = "exam_day" if day in EXAM_DAYS else "first_day"
    return {
        "day": day.isoformat(),
        "as_of": iso(src.now),
        "phase": calendar.phase(src.now).value,
        "trading_day": session is not None,
        "session": None
        if session is None
        else {"open": iso(session.open), "close": iso(session.close), "special": special},
        "previous_day": previous.isoformat(),
        "overnight": _overnight(src.data_dir, day),
        "night_futures": _night_futures(src.data_dir, previous),
        "symbols": [_symbol(src, symbol, day, previous) for symbol in src.symbols],
        "open_to_close": _open_to_close(src, predictions),
        "schedule": _schedule(src.data_dir, calendar, day),
    }


# Overnight


def _overnight(data_dir: Path | None, day: date) -> dict[str, Any]:
    latest = None if data_dir is None else latest_macro(data_dir, day)
    us = []
    for name in US_DAILY:
        closes = [] if data_dir is None else [o for o in read_us_daily(data_dir, name) if o.day < day]
        shown = closes[-(US_DAYS + 1) :]
        history = [
            {"day": o.day.isoformat(), "close": o.value, "change_pct": _change_pct(o.value, before)}
            for o, before in zip(shown, [None, *(b.value for b in shown)], strict=False)
        ][-US_DAYS:]
        us.append({"indicator": name, "closes": history})
    return {"macro": macro_json(latest), "us": us}


# Night futures


def _night_futures(data_dir: Path | None, evening: date) -> dict[str, Any]:
    start, end = night_window(evening)
    trades: list[tuple[datetime, datetime, float, float | None, int | None, str]] = []
    for day in (evening, evening + timedelta(days=1)):
        if data_dir is None:
            break
        root = data_dir / "events" / f"date={day.isoformat()}" / f"feed={NIGHT_FUTURES.tr_id}"
        for partition in sorted(root.glob("symbol=*")):
            columns = ("received_at", "event_time", "price", "change_pct", "cum_volume")
            table = read_partition(partition, columns)
            if table is None:
                continue
            code = partition.name.removeprefix("symbol=")
            for row in table.to_pylist():
                if row["price"] and start <= row["received_at"] < end:
                    trades.append(
                        (
                            row["received_at"],
                            row["event_time"],
                            float(row["price"]),
                            row["change_pct"],
                            row["cum_volume"],
                            code,
                        )
                    )
    out: dict[str, Any] = {"evening": evening.isoformat(), "from": iso(start), "to": iso(end)}
    if not trades:
        empty = dict.fromkeys(("contract", "last", "change_pct", "high", "low", "volume", "last_trade_at"))
        return {**out, "recorded": False, **empty, "minutes": []}
    trades.sort(key=lambda t: t[0])
    last = trades[-1]
    minutes: dict[datetime, float] = {}
    for _, at, price, *_rest in trades:
        minutes[at.astimezone(KST).replace(second=0, microsecond=0)] = price
    return {
        **out,
        "recorded": True,
        "contract": last[5],
        "last": last[2],
        "change_pct": last[3],
        "high": max(t[2] for t in trades),
        "low": min(t[2] for t in trades),
        "volume": last[4],
        "last_trade_at": iso(last[1]),
        "minutes": [{"at": iso(at), "price": price} for at, price in sorted(minutes.items())],
    }


# Each symbol


def _symbol(src: Sources, symbol: str, day: date, previous: date) -> dict[str, Any]:
    data_dir = src.data_dir
    close = None
    if data_dir is not None:
        # The day's KRX close as open_to_close reads it (#264): the closing auction's price.
        days = days_of(read_krx_days(data_dir, symbol), read_auctions(data_dir, symbol))
        close = next((d.close for d in days if d.day == previous), None)
    return {
        "symbol": symbol,
        "previous_close": close,
        "after_hours": _after_hours(src.calendar, data_dir, symbol, previous, close),
        "premarket": _premarket(data_dir, symbol, day, close),
        "expected_open": _expected_open(src, symbol, day, close),
        "supply": _supply(src.state_dir, symbol, previous),
    }


def _change_pct(price: float, close: float | None) -> float | None:
    """``price`` against ``close``, in percent; None without ``close``."""
    return None if not close else (price / close - 1) * 100


def _events(data_dir: Path, day: date, feed: str, symbol: str) -> Path:
    return data_dir / "events" / f"date={day.isoformat()}" / f"feed={feed}" / f"symbol={symbol}"


def _after_hours(
    calendar: MarketCalendar, data_dir: Path | None, symbol: str, previous: date, close: float | None
) -> dict[str, Any] | None:
    """The integrated market's last one-minute bar of the previous trading day's evening: from its
    close to 20:00 (NXT's and KRX's after-market). None without one."""
    session = calendar.session(previous)
    if data_dir is None or session is None:
        return None
    bars = read_bars(data_dir, previous, MINUTE_BARS_TR, symbol) or []
    shown = [b for b in bars if b.start >= session.close and b.start.astimezone(KST).time() < AFTER_HOURS_END]
    if not shown:
        return None
    bar = max(shown, key=lambda b: b.start)
    return {"price": bar.close, "at": iso(bar.start), "change_pct": _change_pct(bar.close, close)}


def _premarket(data_dir: Path | None, symbol: str, day: date, close: float | None) -> dict[str, Any] | None:
    """The NXT pre-market's last trade as ``open_to_close``'s ``premarket_gap`` takes it: the
    recorded integrated trades of its later window, else its earlier one, received before 08:50."""
    if data_dir is None:
        return None
    columns = ("event_time", "received_at", "price")
    table = read_partition(_events(data_dir, day, TRADE.tr_id, symbol), columns)
    if table is None:
        return None
    cut = datetime.combine(day, time(8, 50), KST)
    kept: dict[int, tuple[datetime, float]] = {}
    for row in table.to_pylist():
        at, price = row["event_time"], row["price"]
        if not price or row["received_at"] >= cut:
            continue
        window = premarket_window(at.astimezone(KST).time())
        if window is not None and (window not in kept or at >= kept[window][0]):
            kept[window] = (at, float(price))
    for window in range(len(PREMARKET)):
        if window in kept:
            at, price = kept[window]
            start, end = PREMARKET[window]
            return {
                "price": price,
                "at": iso(at),
                "window": f"{start:%H:%M}-{end:%H:%M}",
                "change_pct": _change_pct(price, close),
            }
    return None


def _expected_open(src: Sources, symbol: str, day: date, close: float | None) -> dict[str, Any] | None:
    """The opening auction's last expected fill recorded before the open (the integrated book's
    ``expected_px``, #304); ``final`` once the auction is over."""
    session = src.calendar.session(day)
    if src.data_dir is None or session is None:
        return None
    partition = _events(src.data_dir, day, BOOK.tr_id, symbol)
    columns = ("received_at", "expected_px", "expected_qty", "expected_exch_cls")
    table = read_partition(partition, columns)
    if table is None:
        return None  # nothing recorded, or only before #304 recorded the columns
    start = session.open - EXPECTED_FROM
    last = None
    for row in table.to_pylist():
        if row["expected_px"] and start <= row["received_at"] < session.open:
            if last is None or row["received_at"] >= last["received_at"]:
                last = row
    if last is None:
        return None
    return {
        "price": last["expected_px"],
        "qty": last["expected_qty"],
        "venue_code": last["expected_exch_cls"],
        "at": iso(last["received_at"]),
        "change_pct": _change_pct(last["expected_px"], close),
        "final": src.now >= session.open,
    }


def _supply(state_dir: Path | None, symbol: str, previous: date) -> dict[str, Any]:
    """The previous trading day's foreign and institution net buys (shares) and their sums over
    the last ``SUPPLY_DAYS`` days stored up to it; None where KIS gave no figure."""
    stored = [] if state_dir is None else read_supply(state_dir, symbol, KIS)
    rows = [r for r in stored if r["date"] <= previous]
    recent = rows[-SUPPLY_DAYS:]
    day = rows[-1] if rows and rows[-1]["date"] == previous else None

    def total(field: str) -> float | None:
        values = [r[field] for r in recent if r.get(field) is not None]
        return float(sum(values)) if values else None

    return {
        "day": previous.isoformat(),
        "foreign_net": None if day is None else day.get("foreign_net"),
        "institution_net": None if day is None else day.get("institution_net"),
        "days": len(recent),
        "foreign_net_sum": total("foreign_net"),
        "institution_net_sum": total("institution_net"),
    }


# Open to close


def _open_to_close(src: Sources, predictions: DayPredictions | None) -> dict[str, Any] | None:
    kind = src.open_to_close
    if kind is None or predictions is None:
        return None
    rows = predictions.rows
    shown = next((r[0]["bar_close"] for r in rows.values() if r), None)
    target = kind_target(kind, src.calendar)
    return {
        "kind": kind.name,
        "day": None if shown is None else kst_day(datetime.fromisoformat(shown)).isoformat(),
        "predicted_at": shown,
        "previous": predictions.previous,
        "symbols": [
            {
                "symbol": symbol,
                "predictions": [kind_prediction_json(r, target=target) for r in rows.get(symbol, [])],
                "inputs": [_inputs(r) for r in rows.get(symbol, []) if r.get("inputs") is not None],
            }
            for symbol in src.symbols
        ],
    }


def _inputs(row: Mapping[str, Any]) -> dict[str, Any]:
    """A daily algorithm's inputs as stored with its prediction (``daily.inputs_json``): each
    feature's value and, when the algorithm explains itself, its part in each class's score."""
    stored = row["inputs"]
    explained = stored.get("explanation") or None
    effects = {} if explained is None else explained.get("inputs", {})
    return {
        "algorithm": row["algorithm"],
        "model_version": row["model_version"],
        "bias": None if explained is None else explained.get("bias"),
        "inputs": [
            {
                "name": name,
                "value": value,
                "z": effects[name]["z"] if name in effects else None,
                "scores": effects[name]["scores"] if name in effects else None,
            }
            for name, value in stored.get("values", {}).items()
        ],
    }


# Schedule


def _schedule(data_dir: Path | None, calendar: MarketCalendar, day: date) -> dict[str, Any]:
    kept = None if data_dir is None else read_latest(data_dir)
    midnight = datetime.combine(day, time(0), KST)
    events = [
        e
        for e in calendar_answer(kept, midnight, _MAX_EVENTS)["events"]
        if datetime.fromisoformat(e["at"]) < midnight + timedelta(days=1)
    ]
    trading = calendar.is_trading_day(day)
    expiry = trading and day == last_trading_day(KOSPI200, day.year, day.month, calendar)
    end, start = month_flags(day, calendar.is_trading_day) if trading else (False, False)
    return {
        "events": events,
        "option_expiry": expiry,
        "futures_expiry": expiry and day.month in KOSPI200.months,
        "month_end": end,
        "month_start": start,
    }
