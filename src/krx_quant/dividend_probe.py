"""``krx-quant probe-dividends`` (#310): whether KIS's adjusted day candles take cash dividends out,
and what KIS's rows say of the days, measured, with no price.

``open_to_close_5d`` reads its window's two ends from KIS's adjusted KRX day candles
(``FID_ORG_ADJ_PRC=0``, ``candles``). If KIS's adjustment covers splits and rights issues only, a
window with an ex-dividend date after its first session leans down by the dividend; the spec's
fallback keeps the answer and marks such a window (``ex_dividend``), which needs the ex-dividend
dates. Neither is known, so this asks, for each symbol, its KRX (``J``) day candles of the last
``YEARS`` years twice, adjusted (``0``) and as traded (``1``), and counts:

- the days both answers have, and the days only one has;
- where the adjusted close over the traded close changes from one day to the next (a step: an
  adjustment divides every price before its event by one factor, so the ratio steps on the event's
  day), by the step's size (under 0.05% is the rounding of the won), 0.05–0.5%, 0.5–2%, 2–5%
  (a dividend's size), 5% or more (a split, a bonus or a rights issue), and the steps' dates;
- the rows' flags as KIS gives them, each value's days: ``flng_cls_code`` (KIS's documentation
  calls it 락 구분: 00 none, 01 rights, 02 dividend, 03 distribution, 04 rights and dividend, 05
  interim dividend, ...; what each value means here is what this measures), ``mod_yn``,
  ``revl_issu_reas`` and a ``prtt_rate`` other than zero; and how many days of each
  ``flng_cls_code`` value are steps.

Small steps at each year's (or quarter's) ex-dividend dates mean the adjusted prices take dividends
out: the answers have no dividend lean, and ``ex_dividend`` only marks. No small steps but days
flagged as dividend ex-dates mean they do not, and KIS's flag is the calendar the mark can read
(the collector would keep it). Neither means KIS gives no dividend calendar. It prints counts,
dates and flag values, no price, so its report can go on the public issue (the scheduled job
``probe-dividends-310``). Calls are spaced ``PACE`` apart, under the REST limit the collector
shares, with the cached access token while it is valid (no early refresh).
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any

import aiohttp

from krx_quant.auth import tokens_and_client
from krx_quant.candles import CANDLES_PATH, CANDLES_PER_QUERY, CANDLES_TR, KRX_MARKET
from krx_quant.config import Settings
from krx_quant.kis import KisError, RestClient
from krx_quant.supply_probe import REQUEST_TIMEOUT, kis_error

Out = Callable[[str], None]

PACE = 0.5  # seconds between calls: three symbols' ten years twice are about 150 calls
YEARS = 10
MAX_SYMBOLS = 5
MAX_PAGES = 60  # per symbol and switch: 6,000 days
MAX_FAILED_IN_A_ROW = 5
FLAGS = ("flng_cls_code", "mod_yn", "revl_issu_reas")
# A step's size buckets: (bound, name), the last unbounded.
ROUNDING = 0.0005
BUCKETS = ((0.005, "0.05–0.5%"), (0.02, "0.5–2%"), (0.05, "2–5%"))
LARGE = "5% 이상"
MAX_DATES = 60  # the steps' dates listed per symbol, at most


def query(symbol: str, start: date, end: date, adjusted: bool) -> dict[str, str]:
    return {
        "FID_COND_MRKT_DIV_CODE": KRX_MARKET,
        "FID_INPUT_ISCD": symbol,
        "FID_INPUT_DATE_1": start.strftime("%Y%m%d"),
        "FID_INPUT_DATE_2": end.strftime("%Y%m%d"),
        "FID_PERIOD_DIV_CODE": "D",
        "FID_ORG_ADJ_PRC": "0" if adjusted else "1",
    }


def _day(row: Mapping[str, Any]) -> date | None:
    try:
        return datetime.strptime(str(row.get("stck_bsop_date") or ""), "%Y%m%d").date()
    except ValueError:
        return None


def _close(row: Mapping[str, Any]) -> float | None:
    try:
        value = float(str(row.get("stck_clpr") or "0"))
    except ValueError:
        return None
    return value if value > 0 else None


class _Failed(Exception):
    pass


class _Asker:
    def __init__(self, client: RestClient) -> None:
        self._client = client
        self.calls = 0
        self.failed = 0
        self._failed_in_a_row = 0

    @property
    def stopped(self) -> bool:
        return self._failed_in_a_row >= MAX_FAILED_IN_A_ROW

    async def rows(self, params: dict[str, str]) -> list[dict[str, Any]]:
        """The answer's dated rows; raises ``_Failed`` with KIS's refusal."""
        if self.calls:
            await asyncio.sleep(PACE)
        self.calls += 1
        try:
            body = await self._client.get(CANDLES_TR, CANDLES_PATH, params)
        except KisError as e:
            reason = kis_error(e)
        except (aiohttp.ClientError, OSError, ValueError, KeyError) as e:  # OSError: timeouts too
            reason = f"error {type(e).__name__}"
        else:
            self._failed_in_a_row = 0
            return [r for r in body.get("output2") or [] if _day(r) is not None]
        self.failed += 1
        self._failed_in_a_row += 1
        raise _Failed(reason)

    async def history(
        self, symbol: str, first: date, last: date, adjusted: bool
    ) -> dict[date, dict[str, Any]]:
        """Every day candle KIS labels from ``first`` to ``last``, paging back from ``last``."""
        out: dict[date, dict[str, Any]] = {}
        end = last
        for _ in range(MAX_PAGES):
            rows = await self.rows(query(symbol, first, end, adjusted))
            new = {d: r for r in rows if (d := _day(r)) is not None and first <= d <= end and d not in out}
            if not new:
                break
            out.update(new)
            oldest = min(new)
            if oldest <= first or len(rows) < CANDLES_PER_QUERY:
                break
            end = oldest - timedelta(days=1)
        return out


@dataclass
class Steps:
    """Where the adjusted close over the traded one steps, by size, with the steps' dates."""

    days: int = 0  # the days both answers have
    only_adjusted: int = 0
    only_traded: int = 0
    rounding: int = 0  # changes under ``ROUNDING``: the won's rounding
    sizes: Counter[str] = field(default_factory=Counter)
    dates: dict[str, list[date]] = field(default_factory=dict)  # by size bucket

    @property
    def on(self) -> set[date]:
        return {d for days in self.dates.values() for d in days}


def _bucket(change: float) -> str:
    for bound, name in BUCKETS:
        if change < bound:
            return name
    return LARGE


def steps(adjusted: Mapping[date, Mapping[str, Any]], traded: Mapping[date, Mapping[str, Any]]) -> Steps:
    """The steps of the adjusted close over the traded close, day by day (module doc)."""
    found = Steps(
        only_adjusted=len(set(adjusted) - set(traded)), only_traded=len(set(traded) - set(adjusted))
    )
    ratios: list[tuple[date, float]] = []
    for day in sorted(set(adjusted) & set(traded)):
        a, t = _close(adjusted[day]), _close(traded[day])
        if a is not None and t is not None:
            ratios.append((day, a / t))
    found.days = len(ratios)
    for (_, before), (day, after) in zip(ratios, ratios[1:], strict=False):
        change = abs(after / before - 1)
        if change == 0:
            continue
        if change < ROUNDING:
            found.rounding += 1
            continue
        name = _bucket(change)
        found.sizes[name] += 1
        found.dates.setdefault(name, []).append(day)
    return found


def flags(rows: Mapping[date, Mapping[str, Any]]) -> dict[str, Counter[str]]:
    """Each flag's values over the rows, by days; ``prtt_rate`` as zero or not."""
    out: dict[str, Counter[str]] = {name: Counter() for name in (*FLAGS, "prtt_rate")}
    for row in rows.values():
        for name in FLAGS:
            out[name][str(row.get(name, "(없음)")) or "(빈 값)"] += 1
        rate = str(row.get("prtt_rate", "")).strip()
        try:
            zero = float(rate or "0") == 0
        except ValueError:
            zero = False
        out["prtt_rate"]["0" if zero else "0 아님"] += 1
    return out


def _counts(counter: Counter[str]) -> str:
    return " · ".join(f"{k} {n}" for k, n in sorted(counter.items())) if counter else "없음"


def report(
    symbol: str, adjusted: Mapping[date, Mapping[str, Any]], traded: Mapping[date, Mapping[str, Any]]
) -> list[str]:
    """A symbol's lines (module doc)."""
    found = steps(adjusted, traded)
    sizes = _counts(found.sizes) if found.sizes else "그 밖에 없음"
    lines = [
        f"{symbol}: 두 답 모두 있는 날 {found.days} · 수정주가에만 {found.only_adjusted}"
        f" · 원주가에만 {found.only_traded}",
        f"  수정주가 ÷ 원주가 종가의 계단(전날과 다른 날): 원 단위 반올림({ROUNDING:.2%} 미만)"
        f" {found.rounding} · {sizes}",
    ]
    by_month = Counter(f"{d.month}월" for d in found.on)
    lines.append(f"  계단의 달: {_counts(by_month)}")
    for name in [*(b for _, b in BUCKETS), LARGE]:
        days = found.dates.get(name, [])
        if days:
            listed = ", ".join(d.isoformat() for d in days[-MAX_DATES:])
            more = f" (앞의 {len(days) - MAX_DATES}개 생략)" if len(days) > MAX_DATES else ""
            lines.append(f"  계단 {name} 날짜{more}: {listed}")
    for name, counter in flags(adjusted).items():
        lines.append(f"  {name}(수정주가 답): {_counts(counter)}")
    on = found.on
    near = {d + timedelta(days=k) for d in on for k in (-1, 0, 1)}
    by_code: dict[str, list[date]] = {}
    for day, row in adjusted.items():
        by_code.setdefault(str(row.get("flng_cls_code", "(없음)")) or "(빈 값)", []).append(day)
    for code, days in sorted(by_code.items()):
        if len(days) == len(adjusted):
            continue  # every day the same value: no event told
        stepped = sum(1 for d in days if d in on)
        nearby = sum(1 for d in days if d in near)
        lines.append(
            f"  flng_cls_code {code}: {len(days)}일 중 그날 계단 {stepped} · ±1일 안에 계단 {nearby}"
            + (
                f" · 날짜 {', '.join(d.isoformat() for d in sorted(days)[-MAX_DATES:])}"
                if code != "00"
                else ""
            )
        )
    return lines


async def probe_dividends(settings: Settings, today: date, out: Out = print) -> tuple[int, int]:
    """Ask each symbol's ten years both ways and print the counts; the calls made, and how many
    failed."""
    symbols: Sequence[str] = settings.symbols[:MAX_SYMBOLS]
    # The cached token while it is valid: no early refresh beside the running collector.
    settings = replace(settings, token_refresh_margin_seconds=0.0)
    first = today - timedelta(days=round(365.25 * YEARS))
    out(f"배당과 수정주가 (#310) {today}: 종목 {', '.join(symbols)}")
    out(
        f"KIS 국내주식기간별시세({CANDLES_TR}) KRX(J) 일봉을 수정주가(FID_ORG_ADJ_PRC=0)와 원주가(1)로 "
        f"{first}부터 받아 견준다. 가격은 싣지 않는다(개수, 날짜, 필드 값, 계단 크기의 구간만)."
    )
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
            out("")
            if asker.stopped:
                out(f"{symbol}: 묻지 않음: {MAX_FAILED_IN_A_ROW}번 잇달아 실패")
                continue
            try:
                adjusted = await asker.history(symbol, first, today, adjusted=True)
                traded = await asker.history(symbol, first, today, adjusted=False)
            except _Failed as e:
                out(f"{symbol}: 조회 실패: {e}")
                continue
            for line in report(symbol, adjusted, traded):
                out(line)
    out("")
    out(f"KIS: {asker.calls}번 조회, {asker.failed}번 실패")
    return asker.calls, asker.failed
