"""Period base rates (#307): the four-minute touch labels by the one-minute bar rule, per
symbol and period, as ``structure-check`` measures them in its section 3 (#270): the same
periods (``structure_check.PERIODS``: before the single-stock ±2x ETFs' listing, after it,
after the 2026-07-31 rules, after the after-market opened), the same labels
(``structure_check.day_labels``: ``touch.bar_touch`` at every bar close the engine predicts)
and the same figures (``structure_check.label_shares``):

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
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from krx_quant.backfill import MINUTE_BARS_TR, read_bars
from krx_quant.market_calendar import MarketCalendar
from krx_quant.predictions import iso
from krx_quant.structure_check import PERIODS, Label, bar_days, day_labels, label_shares

log = logging.getLogger(__name__)

BASE_RATES_FILE = "base_rates.json"
# Why the cost map has no base rates, when it has none.
NOT_COMPUTED = "기간 기준율을 아직 계산하지 않았습니다(수집기가 시작할 때와 20:30 백필 뒤에 계산합니다)"
UNREADABLE = "기간 기준율 파일을 읽지 못했습니다"


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
