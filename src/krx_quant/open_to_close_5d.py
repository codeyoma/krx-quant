"""``open_to_close_5d`` (#310): the direction of a symbol's five sessions, from the KRX opening
auction (09:00) of the day predicted to the KRX closing auction (15:30) of its fifth session
counting that day, predicted at 08:50 every trading day. The second daily kind (``daily.py``), and
the first whose window runs several sessions (``DailyKind.sessions``, H = 5): as long as Monday's
open to Friday's close, opened every day, so a symbol always has four windows waiting for their end.

**Answer** (answer version 1, ``answers``). The window's change is c = (its fifth session's close)
÷ (its first session's open) − 1, the two read as ``open_to_close`` reads a day (``auctions.days_of``,
#317: the user's decision of 2026-10-20 replaces the spec's "both from the KRX day candle"): the
open is the KRX day candle's (the KRX market alone: the integrated candle opens at NXT's 08:00
pre-market), the close the kept 15:30 closing auction's (KRX's one-minute bar of it, else the
recorded print), scaled onto the candle's adjusted prices, or the candle's close on a day before
the after-market without a kept price. Both are adjusted prices of one fetch of the candles (a
later adjustment changes them alike, so c holds; a split inside the window is in them). A day kept
from the auctions alone, before its 20:30 candle, is as traded: a split on it would put the two
ends on two scales, so the window waits for its end day's candle (``DayPrices.candle``) and is
answered from 20:35 that night (``ANSWER_FROM``). Up when c > b, down when c < −b, flat otherwise,
b the window's flat band, ``open_to_close``'s rule over windows:

- σ̂ is the root mean square of c over the latest ``SIGMA_DAYS`` (60) windows that ended before
  the day predicted (begun by D − 5), at least ``SIGMA_MIN_DAYS`` (20) of them with a c; else no
  band and no class (``daily.NO_BAND``);
- s is the 1/3 quantile of |c| ÷ σ̂ (each window's own σ̂, from the windows ended before it began)
  over the latest ``SCALE_DAYS`` (500) windows ended before the day, at least ``SCALE_MIN_DAYS``
  (120) with both; with fewer, Φ⁻¹(2/3) ≈ 0.4307;
- b = s × σ̂, so the three classes come out about a third each.

**The end.** The window's fifth session is set by the trading calendar when it is predicted
(``DailyKind.scheduled_end``) and by the sessions KRX held when it is answered (``daily.sessions_of``:
an unscheduled closure moves it to the fifth day actually open); its path says which
(``end_day``). Its answer waits for that day's candle; ``daily.ANSWER_GIVE_UP`` (7 days) after
its first look, it is given up (``no candle``). A circuit breaker or a sidecar on any of its five
sessions makes it special; a VI does not, and the path counts the five sessions' VIs (``vi``).

**Dividends.** Whether KIS's adjusted prices take cash dividends out is not known yet: the probe
``probe-dividends`` (``dividend_probe``, the scheduled job ``probe-dividends-310``) measures it. If
they do not, a window with an ex-dividend date after its first session leans down by the dividend.
The answer stays as it is, and the path says whether such a day falls in the window
(``ex_dividend``: null while no dividend calendar is kept, ``DayPrices.ex_dividend``).

The path: ``open``, ``close``, ``change`` (c), ``band``, ``sigma``, ``scale``, ``fee_rate``,
``source`` (where the close came from), ``end_day``, ``sessions`` (5), ``closes`` (the five
sessions' closes, null for one the symbol has none of), ``previous_close`` (the session's before the
first: the inverse product's daily reset), ``ex_dividend`` and, written by the engine, ``vi``.

**Baselines**, each from the windows that ended by 08:50 only (the others' answers are the
future): ``otc5-flat``, always flat; ``otc5-climate``, the class shares of the symbol's latest
``CLIMATE_DAYS`` (250) ended windows, each class counted once more; ``otc5-persist``, the class of
the latest window to end, the one begun on D − 5 and ended on D − 1 (never the one begun on D − 1,
not over), with certainty; not predicted (``no_previous``) without it.

**Statistics** (``open_to_close_5d_stats``, research only, after the hit rate): an up call buys at
the first session's open and sells at the fifth's close, a down call sells the stock short and buys
it back there, each less the sell tax and both commissions (``pnl``, ``long``, ``short``); a down
call through the symbol's single-stock −2x inverse product (#267) holds a product reset daily, so
its five sessions' return is the product of each day's −2x, not −2c: with L = 2, P₀ the previous
session's close and O, C each session's open and close, the first day's (bought at the open)
(1 − L(C_D ÷ P₀ − 1)) ÷ (1 − L(O_D ÷ P₀ − 1)), the next days' 1 − L(C_i ÷ C_{i−1} − 1), R their
product − 1, and the P&L on the stock's notional R ÷ L − 2 × fee ÷ L (``pnl_inverse``,
``short_inverse``; none for a symbol without one, 009150, or a window begun before its listing). A
flat call is no trade. Every day trades a new window, so a symbol holds up to five at once: the P&L
is per trade, with no allocation. The intervals are Driscoll–Kraay ones (``stats.overlap_interval``),
as every statistic of this kind's (``Kind.overlap``).
"""

from __future__ import annotations

import math
from bisect import bisect_left
from collections.abc import Mapping, Sequence
from datetime import date, time
from typing import Any

import numpy as np

from krx_quant.auctions import closes_pending
from krx_quant.cards import Card
from krx_quant.daily import (
    NO_BAND,
    NO_CANDLE,
    PENDING,
    Band,
    DailyInputs,
    DailyKind,
    DayPrices,
    overlap_review,
)
from krx_quant.feeds.base import kst_day
from krx_quant.kinds import Answer, KindForecast, KindScored
from krx_quant.open_to_close import (
    BP,
    CLASSES,
    CLIMATE_DAYS,
    DOWN,
    FLAT,
    FLAT_SHARE,
    NORMAL_SCALE,
    SCALE_DAYS,
    SCALE_MIN_DAYS,
    SIGMA_DAYS,
    SIGMA_MIN_DAYS,
    UP,
    _pnl_json,
    classify,
)
from krx_quant.pricing import SELL_TAX
from krx_quant.products import INVERSE_PRODUCTS
from krx_quant.stats import INDEPENDENT, DayOverlap

NAME = "open_to_close_5d"
SESSIONS = 5
ANSWER_VERSION = 1
# The window's end day's KRX day candle is kept after the 20:30 backfill (``candles``).
ANSWER_FROM = time(20, 35)
# The variance ratio its review days are set by until one is measured (#310): five sessions'
# overlap alone, 1 + 2 × (0.8 + 0.6 + 0.4 + 0.2).
UNMEASURED_RATIO = 5.0

FLAT_NAME = "otc5-flat"
CLIMATE = "otc5-climate"
PERSIST = "otc5-persist"


def _windows(
    days: Sequence[DayPrices], sessions: Sequence[date]
) -> tuple[list[date | None], list[int | None]]:
    """Each day's window: its end, the ``SESSIONS``-th session counting the day (None: past the
    sessions known), and the place of that day among ``days`` (None while the symbol has none
    from a candle: not over)."""
    known = sorted({*sessions, *(d.day for d in days)})
    place = {d: k for k, d in enumerate(known)}
    index = {d.day: i for i, d in enumerate(days)}
    ends: list[date | None] = []
    lasts: list[int | None] = []
    for d in days:
        k = place[d.day] + SESSIONS - 1
        end = known[k] if k < len(known) else None
        last = None if end is None else index.get(end)
        ends.append(end)
        lasts.append(last if last is not None and days[last].candle else None)
    return ends, lasts


def _bands(
    days: Sequence[DayPrices], sessions: Sequence[date]
) -> tuple[list[date | None], list[int | None], np.ndarray, np.ndarray, np.ndarray, list[int]]:
    """The windows (``_windows``), their changes (``nan`` without one), σ̂ and the scale by how many
    windows had ended (``m``: the first m, ended before a day), and each day's m."""
    ends, lasts = _windows(days, sessions)
    n = len(days)
    changes = np.array(
        [np.nan if last is None else days[last].close / days[i].open - 1 for i, last in enumerate(lasts)],
        dtype=float,
    )
    over = [e for e in ends if e is not None]  # increasing: a later start ends later
    m_of = [bisect_left(over, d.day) for d in days]
    finite = np.isfinite(changes)
    squares = np.concatenate([[0.0], np.cumsum(np.where(finite, changes**2, 0.0))])
    counted = np.concatenate([[0], np.cumsum(finite)])
    sigma = np.full(n + 1, np.nan)
    for m in range(n + 1):
        lo = max(0, m - SIGMA_DAYS)
        k = counted[m] - counted[lo]
        if k >= SIGMA_MIN_DAYS:
            sigma[m] = math.sqrt((squares[m] - squares[lo]) / k)
    with np.errstate(divide="ignore", invalid="ignore"):
        standard = np.abs(changes) / sigma[np.array(m_of, dtype=int)] if n else np.array([])
    standard[~np.isfinite(standard)] = np.nan
    scale = np.full(n + 1, NORMAL_SCALE)
    for m in range(n + 1):
        window = standard[max(0, m - SCALE_DAYS) : m]
        known = window[~np.isnan(window)]
        if len(known) >= SCALE_MIN_DAYS:
            scale[m] = float(np.quantile(known, FLAT_SHARE))
    return ends, lasts, changes, sigma, scale, m_of


def band(days: Sequence[DayPrices], sessions: Sequence[date] = ()) -> Band | None:
    """The flat band of the day after ``days`` (oldest first): from the windows that ended by the
    last of them."""
    if not days:
        return None
    ends, _, _, sigma, scale, _ = _bands(days, sessions)
    m = sum(1 for e in ends if e is not None and e <= days[-1].day)
    if math.isnan(sigma[m]):
        return None
    return Band(float(scale[m] * sigma[m]), float(sigma[m]), float(scale[m]))


def answers(days: Sequence[DayPrices], fee_rate: float, sessions: Sequence[date] = ()) -> list[Answer]:
    """Each day's window's answer (oldest first): its band from the windows ended before the day,
    ``PENDING`` while its end day's candle is not among ``days``."""
    ends, lasts, changes, sigma, scale, m_of = _bands(days, sessions)
    known = sorted({*sessions, *(d.day for d in days)})
    place = {d: k for k, d in enumerate(known)}
    index = {d.day: i for i, d in enumerate(days)}
    out = []
    for i, (day, end, last) in enumerate(zip(days, ends, lasts, strict=True)):
        m = m_of[i]
        value = None if math.isnan(sigma[m]) else float(scale[m] * sigma[m])
        banded = {
            "band": value,
            "sigma": None if value is None else float(sigma[m]),
            "scale": None if value is None else float(scale[m]),
        }
        if end is None or last is None:
            out.append(Answer(None, PENDING, banded))
            continue
        k = place[day.day]
        window = known[k : k + SESSIONS]
        held = [days[j] if (j := index.get(s)) is not None else None for s in window]
        before = index.get(known[k - 1]) if k > 0 else None
        # An ex-date after the first session (whose open is ex already); unknown for a session the
        # symbol has none of, or without a dividend calendar.
        flags = [None if d is None else d.ex_dividend for d in held[1:]]
        ex_dividend = True if True in flags else False if all(f is False for f in flags) else None
        path: dict[str, Any] = {
            "open": day.open,
            "close": days[last].close,
            "change": float(changes[i]),
            **banded,
            "fee_rate": fee_rate,
            "source": days[last].source,
            "end_day": end.isoformat(),
            "sessions": SESSIONS,
            "closes": [None if d is None else d.close for d in held],
            "previous_close": None if before is None else days[before].close,
            "ex_dividend": ex_dividend,
        }
        if value is None:
            out.append(Answer(None, NO_BAND, path))
            continue
        out.append(Answer(classify(float(changes[i]), value), None, path))
    return out


def _certain(label: str) -> KindForecast:
    up, flat, down = (1.0 if c == label else 0.0 for c in CLASSES)
    return KindForecast((up, flat, down))


class Otc5Flat:
    """Always flat."""

    name = FLAT_NAME
    version = "v1"
    card = Card(
        scientist=None,
        character="기준 예측: 5거래일 늘 보합",
        logic="그날 시가 단일가에서 다섯째 거래일 종가 단일가까지 보합 폭 안에서 끝난다고 늘 낸다.",
        unique_features=(),
        training="학습하지 않는다.",
        forecasting="보합 확률 1, 상승·하락 확률 0이다.",
        failure=(
            "보합 폭을 세 클래스가 1/3씩 나오게 정해서, 이 기준의 맞힘률은 1/3 안팎이다. 이것을 넘지 못하는 "
            "모델은 5거래일 방향에 쓸모가 없다."
        ),
    )

    def predict(self, inputs: DailyInputs) -> KindForecast | None:
        return _certain(FLAT)


class Otc5Climate:
    """The class shares of the symbol's latest ended windows."""

    name = CLIMATE
    version = "v1"
    card = Card(
        scientist=None,
        character="기준 예측: 5거래일 기후",
        logic=(
            f"그 종목의 08:50까지 끝난 최근 {CLIMATE_DAYS}개 5거래일 창(시작일이 5거래일 전 이전인 것)의 "
            "정답에서 상승·보합·하락의 비율을 센다. 끝나지 않은 창의 정답은 미래라 넣지 않는다."
        ),
        unique_features=(),
        training="학습하지 않는다. 08:50마다 그때까지 끝난 창으로 다시 센다.",
        forecasting=(
            "세 클래스의 비율(각 클래스에 1을 더한 수)을 세 확률로 낸다. 끝난 창이 없으면 1/3씩이다."
        ),
        failure="종목의 5거래일 버릇만 맞힌다. 이것을 넘지 못하는 모델은 그 종목의 기울기만 배운 것이다.",
    )

    def predict(self, inputs: DailyInputs) -> KindForecast | None:
        recent = [label for _, label in inputs.past if label is not None][-CLIMATE_DAYS:]
        counts = [recent.count(c) + 1 for c in CLASSES]
        total = sum(counts)
        up, flat, down = (c / total for c in counts)
        return KindForecast((up, flat, down))


class Otc5Persist:
    """The latest window to end goes on: the one begun five sessions before, ended the day before."""

    name = PERSIST
    version = "v1"
    card = Card(
        scientist=None,
        character="기준 예측: 지난 5거래일 방향 잇기",
        logic=(
            "가장 최근에 끝난 5거래일 창(5거래일 전에 시작해 전 거래일에 끝난 것)의 정답이 이어진다고 본다. "
            "전 거래일에 시작한 창은 아직 끝나지 않아 쓰지 않는다."
        ),
        unique_features=(),
        training="학습하지 않는다.",
        forecasting=(
            "그 창의 정답 클래스를 확률 1로 낸다. 그 정답이 없으면(보합 폭을 정할 창이 모자람, 전 거래일 "
            "일봉이 08:50에 없음) 예측하지 않는다(no_previous)."
        ),
        failure="5거래일 방향은 다음 5거래일로 이어지지 않는 때가 많다. 이것을 넘지 못하는 모델은 지난 주 "
        "방향만 따라간 것이다.",
    )

    def predict(self, inputs: DailyInputs) -> KindForecast | None:
        if not inputs.past or not inputs.ends or inputs.ends[-1] != inputs.previous_day:
            return None
        label = inputs.past[-1][1]
        return None if label is None else _certain(label)


def call_profit(predicted: str, path: Mapping[str, Any]) -> float | None:
    """A call's P&L over the window (basis points of the open), the stock bought or sold short: the
    change, less the sell tax and both commissions; None for a flat call or without prices."""
    change, fee = path.get("change"), path.get("fee_rate")
    if predicted == FLAT or change is None or fee is None:
        return None
    gross = float(change) if predicted == UP else -float(change)
    return (gross - (SELL_TAX + 2 * float(fee))) * BP


def inverse_return(path: Mapping[str, Any], leverage: int) -> float | None:
    """The five sessions' return of a −``leverage``x product reset every day, bought at the first
    session's open and sold at the last's close (module doc); None without every price."""
    opened, before, closes = path.get("open"), path.get("previous_close"), path.get("closes")
    if opened is None or before is None or not closes or any(c is None for c in closes):
        return None
    grown = (1 - leverage * (closes[0] / before - 1)) / (1 - leverage * (opened / before - 1))
    for previous, close in zip(closes, closes[1:], strict=False):
        grown *= 1 - leverage * (close / previous - 1)
    return float(grown - 1)


def inverse_profit(predicted: str, path: Mapping[str, Any], symbol: str, day: date) -> float | None:
    """A down call through the symbol's single-stock inverse product (#267), on the stock's notional
    (the product's × its exposure, |L|): R ÷ L less both commissions on the product's notional; None
    for another call, a symbol without one, a window begun before its listing, or without prices."""
    product = INVERSE_PRODUCTS.get(symbol)
    fee = path.get("fee_rate")
    if predicted != DOWN or product is None or day < product.listed or fee is None:
        return None
    returned = inverse_return(path, product.exposure)
    if returned is None:
        return None
    return (returned / product.exposure - 2 * float(fee) / product.exposure) * BP


def open_to_close_5d_stats(
    algorithm: str,
    records: Sequence[KindScored],
    baselines: Mapping[str, Sequence[KindScored]],
    overlap: DayOverlap = INDEPENDENT,
) -> dict[str, Any]:
    """``algorithm``'s research P&L over the five sessions (module doc), its intervals by
    ``overlap`` (the kind's: Driscoll–Kraay ones, ``kind_stats.summarize``)."""
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


# Not run until the dividend probe's result is in (#310 review, #338): its answers read KIS's
# adjusted candles across sessions, and whether that adjustment takes cash dividends out is not
# known (CODING_STANDARDS.md "External data is probed before it is used"). A PR made after reading
# the probe's comment on #310 lifts it, alone (adjusted) or with the spec's fallback.
AWAITING = (
    "the dividend probe (#310, probe-dividends-310): "
    "whether KIS's adjusted day candles take cash dividends out"
)

# Its review days until the variance ratio is measured (#310): the rule's at five sessions' overlap.
_REVIEW = overlap_review(UNMEASURED_RATIO, SESSIONS)
# Until then its algorithms are held out of probation (``DailyKind.review_hold``): their first
# pre-validation measures the ratio (``daily_training``), and the days the rule gives it are fixed
# here in a PR before any of them is judged on them.
REVIEW_HOLD = (
    "its review days are fixed from the variance ratio its first pre-validation measures (#310): not yet"
)

OPEN_TO_CLOSE_5D = DailyKind(
    name=NAME,
    title="5거래일 시가→종가",
    classes=CLASSES,
    class_titles=("상승", "보합", "하락"),
    action=(UP, DOWN),
    answer_version=ANSWER_VERSION,
    sides=(UP, DOWN),
    stats=open_to_close_5d_stats,
    overlap=SESSIONS,
    window_titles=("시가 단일가", "다섯째 거래일 종가 단일가"),
    daily_baselines=(Otc5Flat(), Otc5Climate(), Otc5Persist()),
    answers=answers,
    band=band,
    review_rules=_REVIEW.rules,
    recent_days=_REVIEW.recent_days,
    min_sample=_REVIEW.min_sample,
    pending=closes_pending,
    sessions=SESSIONS,
    answer_from=ANSWER_FROM,
    gives_up=NO_CANDLE,
    awaiting=AWAITING,
    review_hold=REVIEW_HOLD,
)
