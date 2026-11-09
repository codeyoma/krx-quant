"""Period base rates (#307): the four-minute touch labels by the one-minute bar rule, per
symbol and period, as ``structure-check`` measured them in its section 3 (#270): the periods
(``PERIODS``: before the single-stock ±2x ETFs' listing, after it, after the 2026-07-31 rules,
after the after-market opened), the labels (``day_labels``: ``touch.bar_touch`` at every bar close
the engine predicts) and the figures (``label_shares``):

- the windows, and the shares of up, flat and down labels (always-flat's hit rate is the flat
  share);
- the median k (the lower of two);
- how often the previous window's label (four minutes before) was right, of the windows with
  one.

The cost map (``GET /v1/cost-map``) shows them beside the horizons' hit rates: a hit rate is
an average over markets whose base rates differ. Computed by the collector's history task
(``backfill.keep_history``) at start and once a day after the 20:30 backfill, over every
trading day with stored one-minute bars, the day's included, and kept in the state folder
(``state/base_rates.json``), so the API does not read a year of bars. It holds shares, counts
and k: no price.

The day's prices from its one-minute bars (``day_prices``) and the closing auction's days
(``auction_days``) live here too: the closing flow (``closing_flow.py``, #305) and the demo read
them. They were ``structure_check``'s until #650 moved them to their owner, so that the check is a
module nothing live imports.
"""

from __future__ import annotations

import bisect
import json
import logging
import statistics
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from krx_quant.backfill import MINUTE_BARS_TR, read_bars
from krx_quant.market_calendar import MarketCalendar, Session
from krx_quant.market_view import HORIZON, prediction_window
from krx_quant.predictions import iso
from krx_quant.pricing import Direction, flat_ticks, tick_size
from krx_quant.products import LISTED
from krx_quant.reference import BAR, Bar
from krx_quant.touch import bar_touch

log = logging.getLogger(__name__)

BASE_RATES_FILE = "base_rates.json"
# Why the cost map has no base rates, when it has none.
NOT_COMPUTED = "기간 기준율을 아직 계산하지 않았습니다(수집기가 시작할 때와 20:30 백필 뒤에 계산합니다)"
UNREADABLE = "기간 기준율 파일을 읽지 못했습니다"

# The periods compared: (name, first day, first day of the next).
LISTING = LISTED  # the 16 single-stock ±2x ETFs on 005930 and 000660 listed (products.py)
RULES = date(2026, 7, 31)  # the FSC asked to rebalance earlier (07-28); the deposit rule began
AFTER_MARKET = date(2026, 9, 14)  # the KRX after-market (16:00–20:00) opened
PERIODS: tuple[tuple[str, date | None, date | None], ...] = (
    ("상장 전(~05-26)", None, LISTING),
    ("상장 뒤(05-27~07-30)", LISTING, RULES),
    ("규제 뒤(07-31~09-13)", RULES, AFTER_MARKET),
    ("애프터마켓 뒤(09-14~)", AFTER_MARKET, None),
)
# The minutes before the closing auction whose move the auction's is compared with (``DayPrices.drift_from``).
BEFORE_AUCTION = timedelta(minutes=20)


def period_of(day: date) -> str:
    return next(
        n for n, first, end in PERIODS if (first is None or day >= first) and (end is None or day < end)
    )


@dataclass(frozen=True)
class DayPrices:
    """A day's regular-session prices from its one-minute bars (won)."""

    day: date
    open: float  # the first bar from the session's open: the opening auction's print
    drift_from: float  # the last price 20 minutes before the closing auction
    pre_close: float  # the last price before the closing auction
    close: float  # the closing auction's print
    # The volume of the bar holding it: with ``close``, the closing auction's value when no
    # trade of it was recorded (the closing flow's denominator, #305).
    auction_volume: int = 0


def day_prices(bars: Sequence[Bar], session: Session) -> DayPrices | None:
    """The day's prices; None without a bar in continuous trading or in the closing auction
    (a day the auction printed nothing is left out, not given the last trade)."""
    ordered = sorted(bars, key=lambda b: b.start)
    trading = [b for b in ordered if session.open <= b.start < session.closing_auction]
    auction = [b for b in ordered if session.closing_auction <= b.start <= session.close]
    if not trading or not auction:
        return None
    before_drift = [b for b in trading if b.start < session.closing_auction - BEFORE_AUCTION]
    return DayPrices(
        day=session.day,
        open=trading[0].open,
        drift_from=(before_drift[-1] if before_drift else trading[0]).close,
        pre_close=trading[-1].close,
        close=auction[-1].close,
        auction_volume=auction[-1].volume,
    )


@dataclass(frozen=True)
class Label:
    """A bar close's touch label by the bar rule, and the one four minutes before it."""

    period: str
    tick: int
    k: int
    label: Direction
    previous: Direction | None  # None at the day's first windows


def day_labels(bars: Sequence[Bar], session: Session, fee_rate: float) -> list[Label]:
    """The bar rule's label at every bar close the engine predicts that has its closing bar
    and a trade in the four minutes after it (``training`` labels the same way)."""
    ordered = sorted(bars, key=lambda b: b.start)
    starts = [b.start for b in ordered]
    by_start = {b.start: b for b in ordered}
    first, last = prediction_window(session)
    found: dict[datetime, Direction] = {}
    out: list[Label] = []
    t = first
    period = period_of(session.day)
    while t <= last:
        closing = by_start.get(t - BAR)
        if closing is not None:
            after = ordered[bisect.bisect_left(starts, t) : bisect.bisect_left(starts, t + HORIZON)]
            label = bar_touch(closing, after, fee_rate)
            if label is not None:
                found[t] = label
                price = closing.close
                out.append(
                    Label(
                        period, tick_size(price), flat_ticks(price, fee_rate), label, found.get(t - HORIZON)
                    )
                )
        t += BAR
    return out


def bar_days(data_dir: Path, symbols: Sequence[str], calendar: MarketCalendar) -> list[date]:
    """The trading days with a symbol's one-minute bars, oldest first."""
    found: set[date] = set()
    for partition in (data_dir / "bars").glob("date=*"):
        day = date.fromisoformat(partition.name.removeprefix("date="))
        if calendar.is_trading_day(day) and any(
            (partition / f"feed={MINUTE_BARS_TR}" / f"symbol={s}" / "bars.parquet").exists() for s in symbols
        ):
            found.add(day)
    return sorted(found)


def follows(calendar: MarketCalendar, before: DayPrices, after: DayPrices) -> bool:
    """``after`` is the trading day right after ``before``'s (no day missing between)."""
    return calendar.previous_trading_day(after.day) == before.day


def change(new: float, old: float) -> float:
    return new / old - 1


def sign(x: float) -> int:
    return (x > 0) - (x < 0)


@dataclass(frozen=True)
class AuctionDay:
    """A day of the closing auction's move (``structure-check``'s section 2): a day whose trading day
    before has prices too (no day missing between), with the next day's moves where that one follows
    it too. The closing flow's days (#305) are these."""

    day: date
    move: float  # the closing auction's: close / the last price before it − 1
    ticks: float  # the same in ticks of that price
    drift: float  # the 20 minutes before the auction: its last price / the one 20 minutes before − 1
    flow: float  # the day to the auction: its last price before it / the day before's close − 1
    next_gap: float | None  # the next day's open / the close − 1
    next_close: float | None  # the next day's close / the close − 1


def auction_days(days: Sequence[DayPrices], calendar: MarketCalendar) -> list[AuctionDay]:
    """The closing auction's days of one symbol's prices (oldest first)."""
    out = []
    for before, day, after in zip(days, days[1:], [*days[2:], None], strict=False):
        if not follows(calendar, before, day):
            continue
        nxt = after if after is not None and follows(calendar, day, after) else None
        out.append(
            AuctionDay(
                day=day.day,
                move=change(day.close, day.pre_close),
                ticks=(day.close - day.pre_close) / tick_size(day.pre_close),
                drift=change(day.pre_close, day.drift_from),
                flow=change(day.pre_close, before.close),
                next_gap=change(nxt.open, day.close) if nxt is not None else None,
                next_close=change(nxt.close, day.close) if nxt is not None else None,
            )
        )
    return out


@dataclass(frozen=True)
class LabelShares:
    """The figures of some windows' labels: the cost map's period base rates (#307), and
    ``structure-check``'s section 3."""

    windows: int
    up: float  # the share of up labels
    flat: float  # of flat ones: how often always-flat was right
    down: float
    k: int  # the median k (the lower of two)
    previous: float | None  # how often the previous window's label was right; None without one
    paired: int  # the windows with a previous window's label


def label_shares(rows: Sequence[Label]) -> LabelShares:
    """The figures of ``rows`` (at least one)."""
    counts = Counter(x.label for x in rows)
    n = len(rows)
    paired = [x for x in rows if x.previous is not None]
    return LabelShares(
        windows=n,
        up=counts[Direction.UP] / n,
        flat=counts[Direction.FLAT] / n,
        down=counts[Direction.DOWN] / n,
        k=statistics.median_low(x.k for x in rows),
        previous=sum(x.previous == x.label for x in paired) / len(paired) if paired else None,
        paired=len(paired),
    )


def same_share(pairs: Iterable[tuple[float, float | None]]) -> tuple[float, int] | None:
    """How often the second went the first's way, of the pairs where both moved, and how many
    those were; None without one. The closing flow's summary (#305) reads it too."""
    moved = [(a, b) for a, b in pairs if b is not None and a != 0 and b != 0]
    if not moved:
        return None
    return sum(sign(a) == sign(b) for a, b in moved) / len(moved), len(moved)


def opposite_share(pairs: Iterable[tuple[float, float | None]]) -> tuple[float, int] | None:
    """How often the second went against the first, of the pairs where both moved, and how many
    those were; None without one."""
    moved = [(a, b) for a, b in pairs if b is not None and a != 0 and b != 0]
    if not moved:
        return None
    return sum(sign(a) != sign(b) for a, b in moved) / len(moved), len(moved)


def base_rates(
    data_dir: Path, calendar: MarketCalendar, symbols: Sequence[str], fee_rate: float, now: datetime
) -> dict[str, Any]:
    """The base rates of every trading day with ``symbols``' stored one-minute bars, as kept:
    when computed, the days, and a row a symbol and period with labels."""
    days = bar_days(data_dir, symbols, calendar)
    labels: dict[str, list[Label]] = {symbol: [] for symbol in symbols}
    labelled: dict[tuple[str, str], int] = {}  # the days with labels, by symbol and period
    for symbol in symbols:
        for day in days:
            session = calendar.session(day)
            bars = read_bars(data_dir, day, MINUTE_BARS_TR, symbol)
            if session is None or not bars:
                continue
            found = day_labels(bars, session, fee_rate)
            if found:
                labels[symbol] += found
                key = (symbol, found[0].period)
                labelled[key] = labelled.get(key, 0) + 1
    rows = []
    for symbol in symbols:
        for name, first, end in PERIODS:
            found = [x for x in labels[symbol] if x.period == name]
            if not found:
                continue
            f = label_shares(found)
            rows.append(
                {
                    "symbol": symbol,
                    "period": name,
                    "from": None if first is None else first.isoformat(),
                    "to": None if end is None else (end - timedelta(days=1)).isoformat(),
                    "days": labelled[(symbol, name)],
                    "windows": f.windows,
                    "up": f.up,
                    "flat": f.flat,
                    "down": f.down,
                    "k": f.k,
                    "persist": f.previous,
                    "persist_windows": f.paired,
                }
            )
    return {
        "computed_at": iso(now),
        "first_day": days[0].isoformat() if days else None,
        "last_day": days[-1].isoformat() if days else None,
        "rows": rows,
    }


def keep_base_rates(
    data_dir: Path,
    state_dir: Path,
    calendar: MarketCalendar,
    symbols: Sequence[str],
    fee_rate: float,
    now: datetime,
) -> None:
    """Compute the base rates and replace the kept file with them."""
    write_base_rates(state_dir, base_rates(data_dir, calendar, symbols, fee_rate, now))


def write_base_rates(state_dir: Path, body: dict[str, Any]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / BASE_RATES_FILE
    partial = path.with_name(path.name + ".tmp")
    partial.write_text(json.dumps(body, ensure_ascii=False, indent=1))
    partial.replace(path)  # a reader sees the old file or the new one, never half of one


def read_base_rates(state_dir: Path | None) -> tuple[dict[str, Any] | None, str | None]:
    """The kept base rates, or None and why not."""
    path = None if state_dir is None else state_dir / BASE_RATES_FILE
    if path is None or not path.exists():
        return None, NOT_COMPUTED
    try:
        body = json.loads(path.read_text())
    except (OSError, ValueError):
        log.exception("%s could not be read", path)
        return None, UNREADABLE
    return body, None
