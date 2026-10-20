"""``open_to_close`` (#264): the direction of a symbol's day from the KRX opening auction (09:00)
to the KRX closing auction (15:30), predicted at 08:50. The first daily kind (``daily.py``).

Both ends are single-price auctions: an order at the open or the close is filled at that price
whole, so the move between them is one a market-on-open buy and a market-on-close sell made
(the P&L below has no spread).

**Answer** (answer version 2, ``answers``). The day's change is c = close / open − 1: the open is
the KRX opening auction's, its KRX day candle's (the KRX market alone: the integrated candle
opens at NXT's 08:00 pre-market, docs/kis-feeds.md), and the close the 15:30 KRX closing
auction's, from KRX's one-minute bar of it, else the recorded print (``auctions``). Up when
c > b, down when c < −b, flat otherwise (|c| ≤ b), with b the day's flat band:

- σ̂ is the root mean square of c over the ``SIGMA_DAYS`` (60) trading days before the day
  (at least ``SIGMA_MIN_DAYS``, 20; else no band, and no class: ``daily.NO_BAND``);
- s is the 1/3 quantile of |c| / σ̂ over the ``SCALE_DAYS`` (500) days before it that have a
  σ̂ (each day's own, from the days before that day), at least ``SCALE_MIN_DAYS`` (120) of
  them; with fewer, Φ⁻¹(2/3) ≈ 0.4307, the normal law's;
- b = s × σ̂.

Under a normal law of c / σ̂ the band Φ⁻¹(2/3) σ̂ leaves a third of the days flat and splits
the rest; stock days have fatter tails (more small days), so s is fitted on the symbol's own
standardized days instead: a third of its recent days were flat by their own bands, whatever
its tails, and σ̂ follows its volatility (a volatile spell widens the band, so up and down do
not crowd out flat). Every term is of the days before: the band is known at 08:50, and a
training label is the answer a day would have been given. A price adjustment (a split)
divides a day's open and close alike, so c, σ̂ and b do not move.

The path: ``open``, ``close`` (adjusted as the candle is, ``auctions.days_of``), ``change`` (c),
``band`` (b), ``sigma``, ``scale``, ``fee_rate`` (the engine's, for the P&L's costs), ``source``
(where the close came from: ``krx_minute_bar``, ``recorded_print``, or ``krx_day_candle`` on a day
before the after-market without a kept price) and, written by the engine, ``vi`` (the day's VIs
of the symbol).

**Version 1** (to #264's fix) took the close from the KRX day candle too. On 2026-10-19 the
daily check found that candle's close unlike the recorded closing auction print on all three
symbols (its open alike): since KRX's after-market opened (2026-09-14, 16:00–20:00) the candle's
close is most likely the after-market's last price. Version 2 ends the day at the closing
auction, which can be traded; the answers of version 1 are kept, and the days predicted are
answered again in version 2 (``daily.PENDING_DAYS``). Its closes of the days from the
after-market's opening must all be kept before its algorithms' probation is simulated or a
scheduled walk-forward runs (``pending``: ``auctions.closes_pending``).

**Baselines.** ``otc-flat``: always flat. ``otc-climate``: the class shares of the symbol's
latest ``CLIMATE_DAYS`` (250) answered days, each class counted once more. ``otc-persist``: the
previous trading day's class, with certainty; not predicted (``no_previous``) without it (no
band then, or its candle not stored by 08:50).

**Statistics** (``open_to_close_stats``), beside the three-class ones (action classes: up and
down): the research P&L of following each call from the open to the close, in basis points of
the open, after costs (the flat band leaves them out, so the classes stay a third each, #264):
an up call buys at the open and sells at the close, less the sell tax and both commissions; a
down call sells the stock short at the open and buys it back at the close, at the same cost
(``pnl``, ``long``, ``short``), or through the symbol's single-stock −2x inverse product, less
both commissions on its notional, half the stock's (``pnl_inverse``, ``short_inverse``; none for
a symbol without one or a day before its listing, as the direction's #267). A flat call is no
trade.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date
from statistics import NormalDist
from typing import Any

import numpy as np

from krx_quant.auctions import closes_pending
from krx_quant.cards import Card
from krx_quant.daily import NO_BAND, Band, DailyInputs, DailyKind, DayPrices
from krx_quant.feeds.base import kst_day
from krx_quant.kinds import Answer, KindForecast, KindScored
from krx_quant.pricing import SELL_TAX
from krx_quant.products import INVERSE_PRODUCTS
from krx_quant.stats import INDEPENDENT, DayOverlap, interval_json

NAME = "open_to_close"
CLASSES = ("up", "flat", "down")
UP, FLAT, DOWN = CLASSES
ANSWER_VERSION = 2  # 1 to #264's fix: the close was the KRX day candle's (module doc)
SIGMA_DAYS = 60
SIGMA_MIN_DAYS = 20
SCALE_DAYS = 500
SCALE_MIN_DAYS = 120
# The share of days the band leaves flat, and the normal law's scale for it: Φ⁻¹(2/3).
FLAT_SHARE = 1 / 3
NORMAL_SCALE = NormalDist().inv_cdf(0.5 + FLAT_SHARE / 2)
CLIMATE_DAYS = 250
BP = 10_000  # basis points in one

FLAT_NAME = "otc-flat"
CLIMATE = "otc-climate"
PERSIST = "otc-persist"


def _bands(days: Sequence[DayPrices]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Each day's change, and the σ̂ and the scale of its band (``nan`` σ̂: no band), from the
    days before it only; one more entry of σ̂ and the scale than days: the band of the day after
    the last."""
    changes = np.array([d.change for d in days], dtype=float)
    n = len(changes)
    squares = np.concatenate([[0.0], np.cumsum(changes**2)])
    sigma = np.full(n + 1, np.nan)
    for i in range(n + 1):
        lo = max(0, i - SIGMA_DAYS)
        if i - lo >= SIGMA_MIN_DAYS:
            sigma[i] = math.sqrt((squares[i] - squares[lo]) / (i - lo))
    with np.errstate(divide="ignore", invalid="ignore"):
        standard = np.abs(changes) / sigma[:n]
    standard[~np.isfinite(standard)] = np.nan
    scale = np.full(n + 1, NORMAL_SCALE)
    for i in range(n + 1):
        window = standard[max(0, i - SCALE_DAYS) : i]
        known = window[~np.isnan(window)]
        if len(known) >= SCALE_MIN_DAYS:
            scale[i] = float(np.quantile(known, FLAT_SHARE))
    return changes, sigma, scale


def band(days: Sequence[DayPrices]) -> Band | None:
    """The flat band of the day after ``days`` (oldest first), from them only."""
    _, sigma, scale = _bands(days)
    if math.isnan(sigma[-1]):
        return None
    return Band(float(scale[-1] * sigma[-1]), float(sigma[-1]), float(scale[-1]))


def classify(change: float, value: float) -> str:
    """A day's class by its band: up above it, down below its negative, flat between."""
    return UP if change > value else DOWN if change < -value else FLAT


def answers(days: Sequence[DayPrices], fee_rate: float) -> list[Answer]:
    """Each day's answer (oldest first), its band from the days before it."""
    changes, sigma, scale = _bands(days)
    out = []
    for i, day in enumerate(days):
        path: dict[str, Any] = {
            "open": day.open,
            "close": day.close,
            "change": float(changes[i]),
            "fee_rate": fee_rate,
            "source": day.source,
        }
        if math.isnan(sigma[i]):
            out.append(Answer(None, NO_BAND, {**path, "band": None, "sigma": None, "scale": None}))
            continue
        value = float(scale[i] * sigma[i])
        path |= {"band": value, "sigma": float(sigma[i]), "scale": float(scale[i])}
        out.append(Answer(classify(float(changes[i]), value), None, path))
    return out


def _certain(label: str) -> KindForecast:
    up, flat, down = (1.0 if c == label else 0.0 for c in CLASSES)
    return KindForecast((up, flat, down))


class OtcFlat:
    """Always flat."""

    name = FLAT_NAME
    version = "v1"
    card = Card(
        scientist=None,
        character="기준 예측: 늘 보합",
        logic="그날 시가 단일가에서 종가 단일가까지 보합 폭 안에서 끝난다고 늘 낸다.",
        unique_features=(),
        training="학습하지 않는다.",
        forecasting="보합 확률 1, 상승·하락 확률 0이다.",
        failure=(
            "보합 폭을 세 클래스가 1/3씩 나오게 정해서, 이 기준의 맞힘률은 1/3 안팎이다. 이것을 넘지 못하는 "
            "모델은 하루 방향에 쓸모가 없다."
        ),
    )

    def predict(self, inputs: DailyInputs) -> KindForecast | None:
        return _certain(FLAT)


class OtcClimate:
    """The class shares of the symbol's latest answered days."""

    name = CLIMATE
    version = "v1"
    card = Card(
        scientist=None,
        character="기준 예측: 시가→종가 기후",
        logic=(
            f"그 종목의 최근 {CLIMATE_DAYS}거래일 시가→종가 정답(그날마다의 보합 폭으로 정한 것)에서 "
            "상승·보합·하락의 비율을 센다."
        ),
        unique_features=(),
        training="학습하지 않는다. 08:50마다 그 전 거래일까지의 KRX 일봉으로 다시 센다.",
        forecasting=(
            "세 클래스의 비율(각 클래스에 1을 더한 수)을 세 확률로 낸다. 정답이 있는 날이 없으면 1/3씩이다."
        ),
        failure=(
            "종목의 하루 버릇(오른 날이 많았는지)만 맞힌다. 이것을 넘지 못하는 모델은 그 종목의 기울기만 "
            "배운 것이다."
        ),
    )

    def predict(self, inputs: DailyInputs) -> KindForecast | None:
        recent = [label for _, label in inputs.past if label is not None][-CLIMATE_DAYS:]
        counts = [recent.count(c) + 1 for c in CLASSES]
        total = sum(counts)
        up, flat, down = (c / total for c in counts)
        return KindForecast((up, flat, down))


class OtcPersist:
    """The previous trading day's class goes on."""

    name = PERSIST
    version = "v1"
    card = Card(
        scientist=None,
        character="기준 예측: 어제 방향 잇기",
        logic="전 거래일의 시가→종가 정답이 오늘도 이어진다고 본다.",
        unique_features=(),
        training="학습하지 않는다.",
        forecasting=(
            "전 거래일의 정답 클래스를 확률 1로 낸다. 그 정답이 없으면(보합 폭을 정할 날이 모자람, 08:50에 "
            "그날 일봉이 아직 없음) 예측하지 않는다(no_previous)."
        ),
        failure="하루 방향은 이어지지 않는 날이 많다. 이것을 넘지 못하는 모델은 어제 방향만 따라간 것이다.",
    )

    def predict(self, inputs: DailyInputs) -> KindForecast | None:
        if not inputs.past or inputs.past[-1][0] != inputs.previous_day or inputs.past[-1][1] is None:
            return None
        return _certain(inputs.past[-1][1])


def _pnl_json(trades: Sequence[tuple[date, float]], overlap: DayOverlap = INDEPENDENT) -> dict[str, Any]:
    """Trades' P&L, each of its day: how many, their total and the mean per trade with its 95%
    interval (normal; Driscoll–Kraay for a kind whose windows overlap, #310), in basis points of
    the open."""
    return {
        "trades": len(trades),
        "total_bp": sum(v for _, v in trades),
        "per_trade": interval_json(overlap.mean_interval(trades)),
    }


def call_profit(predicted: str, path: Mapping[str, Any]) -> float | None:
    """A call's P&L from the open to the close (basis points), the stock bought or sold short:
    the change, less the sell tax and both commissions; None for a flat call or without prices."""
    change, fee = path.get("change"), path.get("fee_rate")
    if predicted == FLAT or change is None or fee is None:
        return None
    gross = float(change) if predicted == UP else -float(change)
    return (gross - (SELL_TAX + 2 * float(fee))) * BP


def inverse_profit(predicted: str, path: Mapping[str, Any], symbol: str, day: Any) -> float | None:
    """A down call through the symbol's single-stock inverse product (#267): the stock's move
    the other way, less both commissions on the product's notional (the stock's ÷ its exposure, |L|);
    None for another call, a symbol without one, or a day before its listing."""
    product = INVERSE_PRODUCTS.get(symbol)
    change, fee = path.get("change"), path.get("fee_rate")
    if predicted != DOWN or product is None or day < product.listed or change is None or fee is None:
        return None
    return (-float(change) - 2 * float(fee) / product.exposure) * BP


def open_to_close_stats(
    algorithm: str,
    records: Sequence[KindScored],
    baselines: Mapping[str, Sequence[KindScored]],
    overlap: DayOverlap = INDEPENDENT,
) -> dict[str, Any]:
    """``algorithm``'s research P&L from the open to the close (module doc), its intervals by
    ``overlap`` (#310: Driscoll–Kraay ones for a kind whose windows overlap)."""
    sides: dict[str, list[tuple[date, float]]] = {UP: [], DOWN: []}
    inverse_shorts: list[tuple[date, float]] = []
    for r in records:
        predicted, day = CLASSES[r.predicted], kst_day(r.bar_close)
        if (profit := call_profit(predicted, r.path)) is not None:
            sides[predicted].append((day, profit))
        if (inverse := inverse_profit(predicted, r.path, r.symbol, day)) is not None:
            inverse_shorts.append((day, inverse))
    return {
        "n": len(records),
        "pnl": _pnl_json([*sides[UP], *sides[DOWN]], overlap),
        "long": _pnl_json(sides[UP], overlap),
        "short": _pnl_json(sides[DOWN], overlap),
        "pnl_inverse": _pnl_json([*sides[UP], *inverse_shorts], overlap),
        "short_inverse": _pnl_json(inverse_shorts, overlap),
    }


OPEN_TO_CLOSE = DailyKind(
    name=NAME,
    title="시가→종가",
    classes=CLASSES,
    class_titles=("상승", "보합", "하락"),
    action=(UP, DOWN),
    answer_version=ANSWER_VERSION,
    sides=(UP, DOWN),
    stats=open_to_close_stats,
    daily_baselines=(OtcFlat(), OtcClimate(), OtcPersist()),
    answers=answers,
    band=band,
    pending=closes_pending,
)
