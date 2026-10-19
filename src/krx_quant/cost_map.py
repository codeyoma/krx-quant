"""Cost map (#307): what a call has to hit to pay for its round trip, at each horizon the engine
predicts, beside what the predictors hit (``GET /v1/cost-map``). The research's point
(``research/reports/``): with symmetric targets ±W ticks and a round trip of C ticks, a call
breaks even when P(rise | call) − P(fall | call) ≥ C/W, a hit rate of p* = (W + C) / 2W; with
W < C even every call right loses.

**A symbol's cost card** (``cost_card``), at its reference price (the mid-point now, else the
latest bar close's):

- the tick and its size in basis points;
- the sell tax (``pricing.SELL_TAX``), both commissions (the engine's fee, ``KRX_FEE_PERCENT``)
  and a spread of one tick (``SPREAD_TICKS``), in basis points;
- the round trip of a limit order (the sell tax and both commissions: what k covers,
  ``pricing.cost_ticks``) and of a market order (the spread more: bought at the ask, sold at the
  bid), in basis points and ticks, and k (``pricing.flat_ticks``);
- a short through the symbol's single-stock inverse product (#267, ``pricing.INVERSE_PRODUCTS``):
  both commissions on its notional only, in the stock's terms (÷ its leverage), as the research
  P&L counts it. The product's spread, its premium or discount to its value and the futures
  basis are left out (``omits``): it is a floor. None for a symbol without one;
- the nearest tick-band edges below and above the price (``pricing.tick_band``), with the tick
  and k at the first price past each: a price that crosses one changes what a tick means.

**A row a kind with a direction** (``Kind.sides``), in the registry's order, for the
leaderboard's query (symbol, period, special situations):

- the width W and the cost C, of a limit and of a market order, at one of the period's bar
  closes (each once, whoever predicted it): the one whose limit C/W is the median, the lower of
  two. W is the direction's k or a barrier's width w there, C the round trip at its mid-point
  (both in ticks). With C nearly the same at a symbol's bar closes, that is the median width;
  over every symbol at once (``symbol=all``) it is one symbol's bar close, not a mix of figures
  whose ticks differ. A daily kind's W is the median flat band b and its C the round trip, in
  basis points, the same for both orders (its window runs auction to auction, which fill every
  order at one price: no spread); its reference price is the median opening price;
- the breakeven, for a symmetric ±W answer (the direction's touch, the barriers): C/W and p*,
  for each cost. A call right with p and wrong the other way with q grosses W(p − q) on
  average; with no third outcome q = 1 − p, and p ≥ p* breaks even, as it does with one (then
  q < 1 − p). None for a daily kind: its band is not where a trade exits (``NOT_SYMMETRIC``);
- three predictors: the final consensus (the direction's only: the kinds have none), the
  kind's first-ranked algorithm besides the consensus, and its baseline whose hit rate has the
  highest lower bound. Of each: the hit rate of its rise and fall calls with its Wilson
  interval (the leaderboard's ``precision``), the edge (the share of its calls right less the
  share wrong the other way: P(rise | long) − P(fall | long) and the short's alike), its
  calls, and its research P&L per trade (the kind's own: ticks, a daily kind's basis points;
  a short of the stock, and through the inverse product). The leaderboard's own statistics,
  so the same query gives the same numbers there and in the stats window;
- a verdict a predictor and cost: ``pass`` when the hit rate's lower bound is above p*,
  ``fail`` when not, ``few`` with fewer calls than the leaderboard's minimum sample; None
  without p*.

One bar close's W and C stand for the period's: an approximation, as each bar close has its
own width and price.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, TypeVar

from krx_quant.consensus import FINAL
from krx_quant.consensus import NAMES as CONSENSUS
from krx_quant.daily import DailyKind
from krx_quant.kind_stats import KindBoard, KindEntry
from krx_quant.kinds import DIRECTION, Kind, KindScored
from krx_quant.pricing import (
    INVERSE_PRODUCTS,
    SELL_TAX,
    Direction,
    cost_ticks,
    flat_ticks,
    tick_band,
    tick_size,
)
from krx_quant.stats import Entry, Interval, Leaderboard, Scored, interval_json

BP = 10_000
SPREAD_TICKS = 1  # a market order's spread, as the research's cost table assumes
# What a short through an inverse product costs besides its commissions, left out.
OMITS = ("spread", "premium", "basis")
TICKS, BASIS_POINTS = "ticks", "bp"
NOT_SYMMETRIC = "대칭 장벽이 아님: 보합 폭은 거래를 닫는 자리가 아니다"
NO_WIDTH = "폭을 읽을 정답이 없음"


def cost_card(symbol: str, price: float, fee_rate: float) -> dict[str, Any]:
    """``symbol``'s costs at ``price`` (module doc)."""
    tick = tick_size(price)
    tick_bp = tick / price * BP
    limit = cost_ticks(price, fee_rate)
    product = INVERSE_PRODUCTS.get(symbol)
    inverse = None
    if product is not None:
        commissions = 2 * fee_rate / product.leverage  # of the stock's notional
        inverse = {
            "leverage": product.leverage,
            "listed": product.listed.isoformat(),
            "bp": commissions * BP,
            "ticks": commissions * price / tick,
            "omits": list(OMITS),
        }
    low, high = tick_band(price)
    below = None
    if low > 0:
        past = low - tick_size(low - 1)
        below = {"edge": low, "price": past, "tick": tick_size(past), "k": flat_ticks(past, fee_rate)}
    above = None
    if high is not None:
        above = {"edge": high, "price": high, "tick": tick_size(high), "k": flat_ticks(high, fee_rate)}
    return {
        "tick": tick,
        "tick_bp": tick_bp,
        "k": flat_ticks(price, fee_rate),
        "sell_tax_bp": SELL_TAX * BP,
        "fees_bp": 2 * fee_rate * BP,
        "spread_bp": SPREAD_TICKS * tick_bp,
        "limit": {"bp": limit * tick_bp, "ticks": limit},
        "market": {"bp": (limit + SPREAD_TICKS) * tick_bp, "ticks": limit + SPREAD_TICKS},
        "inverse": inverse,
        "below": below,
        "above": above,
    }


@dataclass(frozen=True)
class _Window:
    """A bar close's width and reference price, as a row reads them."""

    width: float
    reference: float


Record = TypeVar("Record", Scored, KindScored)


def direction_row(
    kind: Kind,
    board: Leaderboard,
    records: Mapping[str, Sequence[Scored]],
    fee_rate: float,
    min_sample: int,
    days: Sequence[date],
) -> dict[str, Any]:
    """The direction's row, from its leaderboard and every predictor's ``records``."""
    windows: dict[tuple[str, str], _Window] = {}
    for rs in records.values():
        for r in rs:
            if r.prices is not None:
                ref = r.prices.reference
                windows[(r.symbol, r.bar_close.isoformat())] = _Window(flat_ticks(ref, fee_rate), ref)
    entries = [*board.ranked, *board.unranked]

    def sides(r: Scored) -> tuple[int, int]:
        return _direction_side(r.predicted), _direction_side(r.outcome)

    return _row(
        kind,
        TICKS,
        list(windows.values()),
        True,
        fee_rate,
        min_sample,
        days,
        consensus=next((e for e in entries if e.algorithm == FINAL), None),
        top=_top(board.ranked),
        baseline=_strongest(board.baselines),
        records=records,
        sides=sides,
    )


def kind_row(
    kind: Kind,
    board: KindBoard,
    records: Mapping[str, Sequence[KindScored]],
    fee_rate: float,
    min_sample: int,
    days: Sequence[date],
) -> dict[str, Any]:
    """A kind's row (one with ``sides``), from its leaderboard and every predictor's
    ``records``: a barrier's widths and mid-points at the bar close from its answers' paths
    (``w``, ``anchor``), a daily kind's bands and opening prices (``band``, its window's start)."""
    assert kind.sides is not None
    rise, fall = kind.index(kind.sides[0]), kind.index(kind.sides[1])
    windows: dict[tuple[str, str], _Window] = {}
    start = kind.window[0] if isinstance(kind, DailyKind) else None  # the auction it starts at
    daily = start is not None
    unit = BASIS_POINTS if daily else TICKS if kind.width is not None else None
    for rs in records.values():
        for r in rs:
            path = r.path
            if start is not None:
                width, ref = path.get("band"), path.get(start)
                width = None if width is None else float(width) * BP
            else:
                width, ref = path.get("w"), path.get("anchor")
            if unit is not None and width is not None and ref:
                windows[(r.symbol, r.bar_close.isoformat())] = _Window(float(width), float(ref))

    def side(index: int) -> int:
        return 1 if index == rise else -1 if index == fall else 0

    def sides(r: KindScored) -> tuple[int, int]:
        return side(r.predicted), side(r.outcome)

    return _row(
        kind,
        unit,
        list(windows.values()),
        not daily,
        fee_rate,
        min_sample,
        days,
        consensus=None,
        top=_top(board.ranked),
        baseline=_strongest(board.baselines),
        records=records,
        sides=sides,
    )


def _direction_side(d: Direction) -> int:
    return 1 if d is Direction.UP else -1 if d is Direction.DOWN else 0


def _top(ranked: Sequence[Entry] | Sequence[KindEntry]) -> Entry | KindEntry | None:
    """The first-ranked algorithm besides the consensus."""
    return next((e for e in ranked if e.algorithm not in CONSENSUS), None)


def _strongest(baselines: Sequence[Entry] | Sequence[KindEntry]) -> Entry | KindEntry | None:
    """The baseline whose hit rate of rise and fall calls has the highest lower bound; of two,
    the first by name. None when none called a rise or a fall."""
    called = [e for e in baselines if e.summary.precision is not None]
    if not called:
        return None
    return min(called, key=lambda e: (-(_low(e.summary.precision)), e.algorithm))


def _low(i: Interval | None) -> float:
    return 0.0 if i is None or i.low is None else i.low


def _row(
    kind: Kind,
    unit: str | None,
    windows: Sequence[_Window],
    symmetric: bool,
    fee_rate: float,
    min_sample: int,
    days: Sequence[date],
    *,
    consensus: Entry | KindEntry | None,
    top: Entry | KindEntry | None,
    baseline: Entry | KindEntry | None,
    records: Mapping[str, Sequence[Record]],
    sides: Callable[[Record], tuple[int, int]],
) -> dict[str, Any]:
    width = reference = tick = None
    cost: dict[str, float] | None = None
    ticked = [w for w in windows if w.width > 0]
    if unit == TICKS and ticked:
        # The bar close whose C/W is the median (the lower of two): W and C of one bar close,
        # so C/W is C over W as shown, also over symbols whose ticks differ.
        middle = sorted(ticked, key=lambda w: cost_ticks(w.reference, fee_rate) / w.width)[
            (len(ticked) - 1) // 2
        ]
        width, reference = middle.width, middle.reference
        tick = tick_size(reference)
        limit = cost_ticks(reference, fee_rate)
        cost = {"limit": limit, "market": limit + SPREAD_TICKS}
    elif unit == BASIS_POINTS and windows:
        width = statistics.median(w.width for w in windows)
        reference = statistics.median(w.reference for w in windows)
        limit = (SELL_TAX + 2 * fee_rate) * BP
        cost = {"limit": limit, "market": limit}  # auction to auction: no spread
    breakeven: dict[str, dict[str, float]] | None = None
    missing = None
    if not symmetric:
        missing = NOT_SYMMETRIC
    elif cost is None or not width:
        missing = NO_WIDTH
    else:
        breakeven = {c: {"c_over_w": v / width, "p_star": (width + v) / (2 * width)} for c, v in cost.items()}

    def predictor(e: Entry | KindEntry | None) -> dict[str, Any] | None:
        if e is None:
            return None
        made = [sides(r) for r in records.get(e.algorithm, [])]
        calls = [(call, outcome) for call, outcome in made if call != 0]
        precision = e.summary.precision
        verdict = None
        if breakeven is not None:
            verdict = {
                c: _verdict(precision, len(calls), b["p_star"], min_sample) for c, b in breakeven.items()
            }
        pnl, inverse = _pnl(e)
        return {
            "algorithm": e.algorithm,
            "rank": e.rank,
            "calls": len(calls),
            "precision": interval_json(precision),
            "edge": sum(call * outcome for call, outcome in calls) / len(calls) if calls else None,
            "pnl": pnl,
            "pnl_inverse": inverse,
            "verdict": verdict,
        }

    return {
        "kind": kind.name,
        "title": kind.title,
        "horizon_minutes": None if isinstance(kind, DailyKind) else int(kind.horizon.total_seconds() // 60),
        "rule": _rule(kind),
        "days": [d.isoformat() for d in days],
        "unit": unit,
        "bar_closes": len(windows),
        "width": width,
        "reference_price": reference,
        "tick": tick,
        "cost": cost,
        "breakeven": breakeven,
        "breakeven_missing": missing,
        "consensus": predictor(consensus),
        "top": predictor(top),
        "baseline": predictor(baseline),
    }


def _verdict(precision: Interval | None, calls: int, p_star: float, min_sample: int) -> str:
    if precision is None or calls < min_sample or precision.low is None:
        return "few"
    return "pass" if precision.low > p_star else "fail"


def _pnl(e: Entry | KindEntry) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """The predictor's research P&L per trade, of the stock and with its shorts through the
    inverse product: the direction's touch P&L, a kind's own (``extra``); None without one."""
    if isinstance(e, Entry):
        s = e.summary
        return (
            {"trades": s.pnl.trades, "per_trade": interval_json(s.pnl.per_trade)},
            {"trades": s.pnl_inverse.trades, "per_trade": interval_json(s.pnl_inverse.per_trade)},
        )
    extra = e.summary.extra or {}

    def of(key: str) -> dict[str, Any] | None:
        found = extra.get(key)
        return None if found is None else {"trades": found["trades"], "per_trade": found["per_trade"]}

    return of("pnl"), of("pnl_inverse")


def _rule(kind: Kind) -> str:
    """The kind's answer, shortly, for the row's tooltip."""
    if kind.name == DIRECTION:
        return (
            "4분 안에 매수 호가가 중간가 + k틱에 닿으면 상승, 매도 호가가 − k틱에 닿으면 하락, "
            "둘 다 아니면 보합"
        )
    if isinstance(kind, DailyKind):
        return "시가 단일가 → 종가 단일가 변화가 보합 폭 b를 넘으면 상승, −b 아래면 하락, 그 사이면 보합"
    if kind.width is not None:
        minutes = int(kind.horizon.total_seconds() // 60)
        return f"봉 마감 중간가 ± w틱 장벽 가운데 {minutes}분 안에 먼저 닿은 쪽, 둘 다 닿지 않으면 시간 만료"
    return kind.title
