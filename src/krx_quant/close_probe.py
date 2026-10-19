"""``krx-quant probe-close``: what the KRX day candle's close is, against KRX's one-minute bars
and the recorded closing auction print (#264).

The daily check of 2026-10-19 found the KRX (``J``) day candle's open equal to the recorded
opening auction print and its close unlike the closing auction's, on all three symbols. KRX's
after-market opened on 2026-09-14 (16:00–20:00); if the candle's close is the after-market's last
price, every ``open_to_close`` answer and training label from that day on was not the closing
auction's. This tells it from the other explanations, per symbol, before and from that day,
over the trading days of the last year the stored candles have (KIS keeps about a year of minute
bars; a day it has none of is counted as such). For each day it asks KIS for the day's KRX
(``J``) one-minute bars (``FHKST03010230``): the latest page (the bars to 23:59:59), the page to
the closing auction's bar (``auctions.PRINT_WITHIN`` after the close) where the latest does not
reach it, and the page to the opening's. It counts:

- the bars: days with and without any, and the time of the day's last bar;
- the closing auction's bar (``auctions.print_bar``: the first from the session's close within 10
  minutes) and when it starts;
- the candle's close against that bar's close, and against the day's last bar's close, and which
  of the two it equals (only the auction's, only the last bar's, both: the same price, neither);
- the candle's open against the opening auction's bar's open (the bar of the open's minute,
  ``auctions.opening_bar``; ``auctions.days_of`` scales a kept
  close by the two opens: they should be the same on a day not adjusted since);
- on the recorded days (``auctions.recorded_prints``), the recorded closing print against the
  auction's bar and against the candle.

If the candle's close equals the last bar's and not the auction's on the days from the
after-market and the auction's on the days before, it is the after-market's last price. It prints
counts and bar times only, no price, so its report can go on the public issue (the scheduled job
``probe-close-264``). Calls are spaced ``PACE`` apart, under the REST limit the collector shares,
with the cached access token while it is valid (no early refresh).
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path

import aiohttp

from krx_quant.auctions import (
    AFTER_MARKET_OPENED,
    PRINT_WITHIN,
    bars_page,
    opening_bar,
    print_bar,
    recorded_prints,
)
from krx_quant.auth import tokens_and_client
from krx_quant.candles import read_krx_days
from krx_quant.config import Settings
from krx_quant.feeds.base import KST, Row
from krx_quant.kis import KisError, RestClient
from krx_quant.market_calendar import MarketCalendar, calendar_store
from krx_quant.supply_probe import REQUEST_TIMEOUT, kis_error

Out = Callable[[str], None]

PACE = 0.25  # seconds between calls: three symbols' year is about 1,600 calls, seven minutes
YEAR = timedelta(days=365)
MAX_SYMBOLS = 5
MAX_FAILED_IN_A_ROW = 10  # then KIS is down or refusing: the rest is not asked
_DAY_END = time(23, 59, 59)


@dataclass
class Day:
    """What one day's bars said, against its candle (and its recording)."""

    bars: bool = False  # KIS had bars that day
    last_at: str | None = None  # the last bar's time, HH:MM
    auction_at: str | None = None  # the closing auction's bar's time, HH:MM
    close_auction: bool | None = None  # the candle's close = the auction's bar's close
    close_last: bool | None = None  # the candle's close = the last bar's close
    open_same: bool | None = None  # the candle's open = the opening auction's bar's open
    recorded_auction: bool | None = None  # the recorded closing print = the auction's bar's close
    recorded_candle: bool | None = None  # the recorded closing print = the candle's close
    error: str | None = None


@dataclass
class Tally:
    """A symbol's days of one period."""

    days: list[Day] = field(default_factory=list)

    def lines(self, title: str) -> list[str]:
        days = self.days
        if not days:
            return [f"  {title}: 거래일 없음"]
        failed = [d for d in days if d.error is not None]
        asked = [d for d in days if d.error is None]
        with_bars = [d for d in asked if d.bars]
        out = [
            f"  {title} {len(days)}거래일: 1분봉 있음 {len(with_bars)} · 없음 {len(asked) - len(with_bars)}"
            + (f" · 조회 실패 {len(failed)}" if failed else "")
        ]
        if not with_bars:
            return out
        out.append("    마지막 봉 시각: " + _counts(Counter(d.last_at or "없음" for d in with_bars)))
        out.append(
            "    종가 단일가 봉(마감부터 10분 안의 첫 봉) 시각: "
            + _counts(Counter(d.auction_at or "없음" for d in with_bars))
        )
        out.append("    일봉 종가 = 종가 단일가 봉 종가: " + _same([d.close_auction for d in with_bars]))
        out.append("    일봉 종가 = 그날 마지막 봉 종가: " + _same([d.close_last for d in with_bars]))
        which: Counter[str] = Counter()
        for d in with_bars:
            if d.close_auction is None or d.close_last is None:
                which["견줄 봉 없음"] += 1
            elif d.close_auction and d.close_last:
                which["둘 다(같은 값)"] += 1
            elif d.close_auction:
                which["단일가 봉과만"] += 1
            elif d.close_last:
                which["마지막 봉과만"] += 1
            else:
                which["둘 다 아님"] += 1
        out.append("    일봉 종가가 같은 것: " + _counts(which))
        out.append(
            "    일봉 시가 = 시가 단일가 봉(개장 분의 봉) 시가: " + _same([d.open_same for d in with_bars])
        )
        recorded = [d for d in days if d.recorded_candle is not None]
        if recorded:
            out.append(
                f"    녹화한 종가 단일가(녹화 있는 날 {len(recorded)}): = 단일가 봉 종가 "
                + _same([d.recorded_auction for d in recorded])
                + " · = 일봉 종가 "
                + _same([d.recorded_candle for d in recorded])
            )
        return out


def _same(flags: Sequence[bool | None]) -> str:
    same = sum(1 for f in flags if f is True)
    different = sum(1 for f in flags if f is False)
    missing = sum(1 for f in flags if f is None)
    return f"같음 {same} · 다름 {different}" + (f" · 견줄 것 없음 {missing}" if missing else "")


def _counts(counter: Counter[str]) -> str:
    if not counter:
        return "없음"
    return " · ".join(f"{k} {n}" for k, n in sorted(counter.items()))


def _hhmm(bar: Row | None) -> str | None:
    return None if bar is None else bar["bar_time"].astimezone(KST).strftime("%H:%M")


class _Asker:
    def __init__(self, client: RestClient) -> None:
        self._client = client
        self.calls = 0
        self.failed = 0
        self._failed_in_a_row = 0

    @property
    def stopped(self) -> bool:
        return self._failed_in_a_row >= MAX_FAILED_IN_A_ROW

    async def page(self, symbol: str, day: date, before: datetime) -> list[Row]:
        """A page of the day's J bars; raises ``_Failed`` with KIS's refusal."""
        if self.calls:
            await asyncio.sleep(PACE)
        self.calls += 1
        try:
            rows = await bars_page(self._client, symbol, day, before)
        except KisError as e:
            reason = kis_error(e)
        except (aiohttp.ClientError, OSError, ValueError, KeyError) as e:  # OSError: timeouts too
            reason = f"error {type(e).__name__}"
        else:
            self._failed_in_a_row = 0
            return rows
        self.failed += 1
        self._failed_in_a_row += 1
        raise _Failed(reason)


class _Failed(Exception):
    pass


async def _day(asker: _Asker, data_dir: Path, calendar: MarketCalendar, symbol: str, candle: Row) -> Day:
    day: date = candle["date"]
    session = calendar.session(day)
    if session is None:
        return Day(error="세션 없음")
    try:
        latest = await asker.page(symbol, day, datetime.combine(day, _DAY_END, KST))
        if not latest:
            return Day()
        closing = latest
        if latest[0]["bar_time"] > session.close:  # the latest page does not reach the auction
            closing = await asker.page(symbol, day, session.close + PRINT_WITHIN - timedelta(seconds=1))
        opening = await asker.page(symbol, day, session.open + PRINT_WITHIN - timedelta(seconds=1))
    except _Failed as e:
        return Day(error=str(e))
    auction = print_bar(closing, session.close)
    first = opening_bar(opening, session)
    last = latest[-1]
    found = Day(
        bars=True,
        last_at=_hhmm(last),
        auction_at=_hhmm(auction),
        close_auction=None if auction is None else auction["close"] == candle["close"],
        close_last=last["close"] == candle["close"],
        open_same=None if first is None else first["open"] == candle["open"],
    )
    prints = recorded_prints(data_dir, day, symbol, session)
    if prints is not None and prints[1] is not None:
        found.recorded_candle = prints[1] == candle["close"]
        found.recorded_auction = None if auction is None else prints[1] == auction["close"]
    return found


async def probe_close(settings: Settings, today: date, out: Out = print) -> tuple[int, int]:
    """Ask each symbol's days of the last year, print the counts; the calls made, and how many
    failed."""
    symbols = settings.symbols[:MAX_SYMBOLS]
    # The cached token while it is valid: no early refresh beside the running collector.
    settings = replace(settings, token_refresh_margin_seconds=0.0)
    calendar = calendar_store(settings).calendar
    first = today - YEAR
    out(f"KRX 일봉 종가 확인 (#264) {today}: 종목 {', '.join(symbols)}")
    out(
        "KRX(J) 일봉(data/candles/period=day/symbol=<종목>/krx.parquet)의 시가·종가를 KIS KRX(J) 1분봉"
        " (FHKST03010230)과 녹화한 KRX 체결(H0STCNT0)의 단일가에 견준다. 가격은 싣지 않는다(같음·다름과"
        " 개수, 봉 시각만)."
    )
    out(f"기간: {first} – {today}의 저장된 일봉, 애프터마켓 {AFTER_MARKET_OPENED}부터")
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        tokens, client = tokens_and_client(session, settings, lambda: datetime.now().astimezone())
        asker = _Asker(client)
        try:
            await tokens.get()
        except (KisError, aiohttp.ClientError, OSError, ValueError) as e:
            reason = kis_error(e) if isinstance(e, KisError) else f"error {type(e).__name__}"
            out(f"멈춤: KIS 접근 토큰이 없다: {reason}")
            return 1, 1
        for symbol in symbols:
            candles = [r for r in read_krx_days(settings.data_dir, symbol) if first <= r["date"] <= today]
            before, after = Tally(), Tally()
            for candle in candles:
                if asker.stopped:
                    found = Day(error=f"묻지 않음: {MAX_FAILED_IN_A_ROW}번 잇달아 실패")
                else:
                    found = await _day(asker, settings.data_dir, calendar, symbol, candle)
                (after if candle["date"] >= AFTER_MARKET_OPENED else before).days.append(found)
            out("")
            out(f"{symbol}: 저장된 KRX 일봉 {len(candles)}일")
            for line in before.lines(f"애프터마켓 전(~{AFTER_MARKET_OPENED - timedelta(days=1)})"):
                out(line)
            for line in after.lines(f"애프터마켓 뒤({AFTER_MARKET_OPENED}~)"):
                out(line)
    out("")
    out(f"KIS: {asker.calls}번 조회, {asker.failed}번 실패")
    return asker.calls, asker.failed
