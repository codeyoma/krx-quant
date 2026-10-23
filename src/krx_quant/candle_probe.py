"""``krx-quant probe-candles``: what KIS's period chart query gives, measured (#111).

Before the engine collects day, week, month and year candles (#111, #112), the facts of
KIS's 국내주식기간별시세 (``FHKST03010100``) are measured on the server, read-only and
beside the collector:

- which market codes answer for a stock and for an ETF (``UN`` integrated KRX+NXT, ``J``
  KRX, ``NX`` NXT), and the fields of an answer;
- how many candles one call gives, and how far back each period (D, W, M, Y) goes, paging
  back from today;
- what the adjusted-price switch (``FID_ORG_ADJ_PRC``: 0 adjusted, 1 as traded) changes,
  around Samsung Electronics' 50:1 split (2018-05-04).

It prints a report to paste into docs/kis-feeds.md. Calls are spaced ``PACE`` seconds apart,
well under the REST limit the collector shares.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any

from krx_quant.auth import tokens_and_client
from krx_quant.candles import CANDLES_PATH, CANDLES_TR
from krx_quant.config import Settings
from krx_quant.kis import KisError, RestClient, rest_session

MARKETS = ("UN", "J", "NX")
PERIODS = ("D", "W", "M", "Y")
PACE = 0.5  # seconds between calls
MAX_PAGES = 150  # per period: 15,000 candles if a page is 100
FIRST_DAY = date(1900, 1, 1)
# Samsung Electronics split 50:1 on 2018-05-04: adjusted and traded prices differ before it.
SPLIT_SYMBOL, SPLIT_FROM, SPLIT_TO = "005930", date(2018, 4, 23), date(2018, 5, 15)

Out = Callable[[str], None]


def query(
    market: str, symbol: str, start: date, end: date, period: str, adjusted: bool = True
) -> dict[str, str]:
    return {
        "FID_COND_MRKT_DIV_CODE": market,
        "FID_INPUT_ISCD": symbol,
        "FID_INPUT_DATE_1": start.strftime("%Y%m%d"),
        "FID_INPUT_DATE_2": end.strftime("%Y%m%d"),
        "FID_PERIOD_DIV_CODE": period,
        "FID_ORG_ADJ_PRC": "0" if adjusted else "1",
    }


def _day(row: dict[str, Any]) -> date | None:
    text = row.get("stck_bsop_date") or ""
    try:
        return datetime.strptime(text, "%Y%m%d").date()
    except ValueError:
        return None


class _Prober:
    def __init__(self, client: RestClient, out: Out) -> None:
        self._client = client
        self._out = out
        self.calls = 0

    async def candles(self, params: dict[str, str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """The answer's dated rows (``output2``) and its ``output1``; raises KisError."""
        if self.calls:
            await asyncio.sleep(PACE)
        self.calls += 1
        body = await self._client.get(CANDLES_TR, CANDLES_PATH, params)
        rows = [r for r in body.get("output2") or [] if _day(r) is not None]
        return rows, body.get("output1") or {}

    async def markets(self, symbol: str, today: date) -> None:
        """Each market code for the last 30 days: rows, the dates they span, the fields."""
        for market in MARKETS:
            try:
                rows, head = await self.candles(query(market, symbol, today - timedelta(days=30), today, "D"))
            except KisError as e:
                self._out(f"  {market}: error {e}")
                continue
            days = sorted(d for r in rows if (d := _day(r)) is not None)
            span = f"{days[0]} .. {days[-1]}" if days else "-"
            self._out(f"  {market}: {len(rows)} rows, {span}")
            if rows:
                self._out(f"    output2 fields: {', '.join(sorted(rows[0]))}")
                self._out(f"    newest: {rows[0]}")
            if head:
                self._out(f"    output1 fields: {', '.join(sorted(head))}")

    async def history(self, market: str, symbol: str, period: str, today: date) -> None:
        """Page back from today until no older candle comes: per call and in all."""
        end, total, sizes, oldest, calls = today, 0, [], None, 0
        for _ in range(MAX_PAGES):
            calls += 1  # the last, which finds nothing older, too
            try:
                rows, _ = await self.candles(query(market, symbol, FIRST_DAY, end, period))
            except KisError as e:
                self._out(f"  {period}: error after {total} candles: {e}")
                return
            days = [d for r in rows if (d := _day(r)) is not None and (oldest is None or d < oldest)]
            if not days:
                break
            sizes.append(len(rows))
            total += len(days)
            oldest = min(days)
            end = oldest - timedelta(days=1)
        else:
            self._out(f"  {period}: stopped at {MAX_PAGES} calls, still going")
        per_call = f"at most {max(sizes)} a call" if sizes else "none"
        self._out(f"  {period}: {total} candles in {calls} calls ({per_call}), oldest {oldest}")

    async def adjusted(self, market: str) -> None:
        """Closes around the split, adjusted and as traded."""
        for adjusted in (True, False):
            try:
                rows, _ = await self.candles(query(market, SPLIT_SYMBOL, SPLIT_FROM, SPLIT_TO, "D", adjusted))
            except KisError as e:
                self._out(f"  FID_ORG_ADJ_PRC={0 if adjusted else 1}: error {e}")
                continue
            closes = ", ".join(
                f"{r['stck_bsop_date']}:{r.get('stck_clpr')}" for r in sorted(rows, key=_day_key)
            )
            self._out(f"  FID_ORG_ADJ_PRC={0 if adjusted else 1}: {closes or '-'}")


def _day_key(row: dict[str, Any]) -> date:
    return _day(row) or FIRST_DAY


async def probe_candles(settings: Settings, today: date, out: Out = print) -> int:
    """Measure the period chart query and print the report; the number of calls made."""
    stock = settings.symbols[0]
    etfs = [s for s in settings.etf_symbols if s not in settings.symbols]
    async with rest_session() as session:
        _, client = tokens_and_client(session, settings, lambda: datetime.now().astimezone())
        prober = _Prober(client, out)
        out(f"{CANDLES_TR} {CANDLES_PATH}, {today}")
        for symbol in [stock, *etfs[:1]]:
            out(f"markets for {symbol} (D, the last 30 days):")
            await prober.markets(symbol, today)
        out(f"history of {stock} (paging back from {today}, adjusted):")
        for period in PERIODS:
            for market in ("UN", "J"):
                out(f" market {market}:")
                await prober.history(market, stock, period, today)
        out(f"adjusted prices of {SPLIT_SYMBOL} around its 2018-05-04 split (J, D):")
        await prober.adjusted("J")
        out(f"{prober.calls} calls")
        return prober.calls
