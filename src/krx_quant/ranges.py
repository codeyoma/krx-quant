"""The range (#177, #180): how far the integrated mid-point moves in the four minutes after
a bar close, against the cost of trading it. A kind of prediction (``kinds.py``) beside the
direction: range-based volatility (Parkinson 1980, CARR) measured on mid-points, as the
bid-ask bounce of trade prints would inflate it.

**Answer** (answer version 1, ``range_answer``). The reference is the integrated mid-point at
the bar close t (the reference price). The window's mid-points are the reference and those
of the usable integrated best quotes received in [t, t + 4 minutes) (the touch label's
quotes: ``kinds.Followed``). The range r is their highest less their lowest, in ticks of the
reference's tick size: a multiple of half a tick. k is the reference's flat band
(``pricing.flat_ticks``):

- small: r < k (no round trip within the four minutes could have covered its cost, even
  at mid-points);
- medium: k ≤ r < 2k;
- large: r ≥ 2k.

A window the shared rules cannot score (no mid-point at t, a REST snapshot, sixty seconds
without a quote, a restart) has no class (#160). The path keeps r, k, the tick, the
reference, the highest and lowest mid-points and when each was first received (the bar
close for the reference), how far the mid-point went up (highest − reference) and down
(reference − lowest), the quotes received, whether they went silent, and, for diagnosis,
the highest bid and lowest ask (as the touch label's path has them).

**Bar rule** (``bar_range``), for days with one-minute bars only (training): r ≈ max(0,
(the highest high − the lowest low of t's close and the bars of the four minutes from t)
÷ the tick − c), the reference and k from t's close. A one-minute range is about a tick wider
than the mid-points' (005930: bars' median 3 ticks, recorded mid-points' 1 to 2), so c takes
the bounce off: ``BAR_C`` is 1 for now; ``krx-quant diagnose`` measures, on the recorded
days, how often each c's class is the quote answer's (section 7), to choose it.

**Output** (``quantiles_ordered``): the three probabilities ``p_small``, ``p_medium``,
``p_large`` and the quantiles ``q10``, ``q50``, ``q90`` of r, in ticks, with
0 ≤ q10 ≤ q50 ≤ q90; anything else is an ``error``.

**Baselines.** ``range-climate`` (``RangeClimate``): each symbol and 30-minute slot's class
shares and quantiles of r over the latest 20 training days, a table the nightly training
refits and the 08:00 swap installs. ``range-persist`` (``RangePersist``): the last window's
r goes on, with certainty; not predicted when that window could not be scored.

**Statistics** (``quantile_stats``), beside the three-class ones (action classes: small and
large): each quantile's pinball loss (mean with a normal 95% interval, and by trading day),
the share of r within q10–q90 (ideally 80%) with its Wilson interval, the mean absolute
error of q50, and the pinball loss less ``range-climate``'s on the same bar closes (paired
and by trading day; descriptive, never judged on).
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from krx_quant.cards import Card, Meaning
from krx_quant.consensus import slot
from krx_quant.feeds.base import KST, kst_day
from krx_quant.kinds import Answer, Followed, Kind, KindForecast, KindInputs, KindScored, went_silent
from krx_quant.pricing import flat_ticks, tick_size
from krx_quant.reference import Bar
from krx_quant.stats import Interval, day_mean, interval_json, mean_interval, wilson

if TYPE_CHECKING:  # the algorithm contract builds on the kinds: its types only
    from krx_quant.algorithms.contract import KindSample, Model

NAME = "range"
CLASSES = ("small", "medium", "large")
ANSWER_VERSION = 1
# The output's quantiles of r and their levels.
QUANTILES: tuple[tuple[str, float], ...] = (("q10", 0.1), ("q50", 0.5), ("q90", 0.9))
# The bar rule's bounce correction, in ticks (``bar_range``). Provisional: chosen from
# #177's measurement (one-minute ranges about a tick wider than the mid-points'), until
# ``krx-quant diagnose`` measures c = 0 and 1 against the quote answers on the recorded days.
BAR_C = 1

CLIMATE = "range-climate"
PERSIST = "range-persist"
CLIMATE_DAYS = 20  # the latest training days its table counts, as the direction's climate
CLIMATE_DEFAULT = f"{CLIMATE}-default"
ALL_DAY = "all"  # the slot of a symbol's whole day, the table's back-off


def range_class(r: float, k: int) -> str:
    """The class of a range of ``r`` ticks against the flat band ``k``."""
    return "small" if r < k else "medium" if r < 2 * k else "large"


def _at(t: datetime | None) -> str | None:
    return t.astimezone(KST).isoformat() if t is not None else None


def range_answer(window: Followed) -> Answer:
    """The range answer of a window (answer version 1): its class and path (module doc)."""
    mids = [(q.received_at, (q.bid + q.ask) / 2) for q in window.quotes]
    if window.anchor is not None:
        mids.insert(0, (window.bar_close, window.anchor))
    high = max(mids, key=lambda m: m[1]) if mids else None  # the first received, on a tie
    low = min(mids, key=lambda m: m[1]) if mids else None
    known = window.anchor is not None and window.tick is not None and window.k is not None
    r = up = down = None
    if known and high is not None and low is not None:
        assert window.anchor is not None and window.tick is not None
        r = (high[1] - low[1]) / window.tick
        up = (high[1] - window.anchor) / window.tick
        down = (window.anchor - low[1]) / window.tick
    path: dict[str, Any] = {
        "r": r,
        "k": window.k,
        "tick": window.tick,
        "anchor": window.anchor,
        "max_mid": high[1] if high else None,
        "max_mid_at": _at(high[0]) if high else None,
        "min_mid": low[1] if low else None,
        "min_mid_at": _at(low[0]) if low else None,
        "up": up,
        "down": down,
        "quotes": len(window.quotes),
        "silent": went_silent(window.bar_close, window.end, [q.received_at for q in window.quotes]),
        "max_bid": max((q.bid for q in window.quotes), default=None),
        "min_ask": min((q.ask for q in window.quotes), default=None),
    }
    if r is None or window.k is None:
        return Answer(None, window.unscorable, path)
    return Answer(range_class(r, window.k), None, path)


def bar_range(closing: Bar, after: Sequence[Bar], fee_rate: float, c: int = BAR_C) -> Answer | None:
    """The range answer of the four minutes after a bar close, from one-minute bars (training
    on a day without quotes): the bar that ended at the bar close (``closing``) and the bars of
    the four minutes from it (``after``, those with trades). None without a trade in those
    minutes, as the touch label's bar rule.

    The close is the reference, and k the flat band at it. r is the highest high less the
    lowest low of the close and those bars, in ticks of the close's tick size, less ``c``
    (the bounce: a trade prints on the bid or the ask, a mid-point between them), at least 0.
    """
    if not after:
        return None
    price = closing.close
    tick = tick_size(price)
    k = flat_ticks(price, fee_rate)
    high = max(price, *(b.high for b in after))
    low = min(price, *(b.low for b in after))
    width = (high - low) / tick
    r = max(0.0, width - c)
    path = {"r": r, "k": k, "tick": tick, "anchor": price, "high": high, "low": low, "width": width, "c": c}
    return Answer(range_class(r, k), None, path)


def quantiles_ordered(output: Mapping[str, float]) -> str | None:
    """Why a range output breaks its contract (0 ≤ q10 ≤ q50 ≤ q90), None if it keeps it."""
    q10, q50, q90 = (output[key] for key, _ in QUANTILES)
    if 0 <= q10 <= q50 <= q90:
        return None
    return f"quantiles q10 {q10}, q50 {q50}, q90 {q90} are not 0 ≤ q10 ≤ q50 ≤ q90"


def pinball(q: float, level: float, r: float) -> float:
    """The pinball (quantile) loss of the ``level`` quantile ``q`` when the range was ``r``."""
    return level * (r - q) if r >= q else (1 - level) * (q - r)


def _by_day(values: Sequence[tuple[datetime, float]]) -> dict[date, list[float]]:
    days: dict[date, list[float]] = {}
    for at, value in values:
        days.setdefault(kst_day(at), []).append(value)
    return days


def _mean_json(values: Sequence[tuple[datetime, float]]) -> dict[str, Any]:
    """A mean, with its normal 95% interval, and by trading day (``stats.day_mean``)."""
    by_day: Interval | None = day_mean(_by_day(values))
    return {"mean": interval_json(mean_interval([v for _, v in values])), "by_day": interval_json(by_day)}


def quantile_stats(
    algorithm: str, records: Sequence[KindScored], baselines: Mapping[str, Sequence[KindScored]]
) -> dict[str, Any]:
    """``algorithm``'s quantile statistics (module doc), from its scored range predictions; its
    pinball loss less ``range-climate``'s on the bar closes both predicted, but for the climate
    itself (known by name, scored or not yet)."""
    scored = [
        (rec, float(rec.path["r"]))
        for rec in records
        if rec.path.get("r") is not None and all(key in rec.output for key, _ in QUANTILES)
    ]
    out: dict[str, Any] = {
        "n": len(scored),
        "pinball": {
            key: _mean_json([(rec.bar_close, pinball(rec.output[key], level, r)) for rec, r in scored])
            for key, level in QUANTILES
        },
        "interval_coverage": interval_json(
            wilson(sum(rec.output["q10"] <= r <= rec.output["q90"] for rec, r in scored), len(scored))
        ),
        "q50_abs_error": interval_json(mean_interval([abs(r - rec.output["q50"]) for rec, r in scored])),
        "vs_climate": None,
    }
    climate = baselines.get(CLIMATE)
    if climate is None or algorithm == CLIMATE:
        return out
    theirs = {(c.symbol, c.bar_close): c.output for c in climate}
    paired = [(rec, r, theirs[key]) for rec, r in scored if (key := (rec.symbol, rec.bar_close)) in theirs]
    out["vs_climate"] = {
        key: _mean_json(
            [
                (rec.bar_close, pinball(rec.output[key], level, r) - pinball(other[key], level, r))
                for rec, r, other in paired
            ]
        )
        for key, level in QUANTILES
    }
    return out


def _certain(label: str, r: float) -> KindForecast:
    """``label`` with certainty, every quantile ``r``."""
    small, medium, large = (1.0 if c == label else 0.0 for c in CLASSES)
    return KindForecast((small, medium, large), {key: r for key, _ in QUANTILES})


class RangePersist:
    """The last four minutes' range goes on (#180)."""

    name = PERSIST
    version = "v1"
    card = Card(
        scientist=None,
        character="기준 예측: 직전 4분 변동폭",
        logic=(
            "직전 4분(t − 4분 → t)의 변동폭 정답 r(그 4분 동안 통합 중간가의 최고 − 최저, 틱)이 다음 4분에도 "
            "그대로라고 본다. 저장된 예측이 아니라 시장 보기가 따라간 호가로 정한다(그 봉 마감에 예측이 "
            "없었어도 된다). 그 4분을 채점할 수 없으면(호가 끊김, REST 대체 호가, 재시작, 그날 처음 4분) "
            "예측하지 않는다."
        ),
        unique_features=(),
        training="배우지 않는다.",
        forecasting=(
            "r을 지금 봉 마감의 k로 나눈 구간(작음 r < k, 보통 k ≤ r < 2k, 큼 r ≥ 2k)에 확률 1, 나머지 둘에 "
            "0을 주고, 분위수는 q10 = q50 = q90 = r이다. 예측하지 않은 봉 마감은 상태가 no_previous다."
        ),
        failure=(
            "변동성이 갑자기 바뀔 때 틀린다: 조용하다가 뉴스로 커지거나, 큰 움직임 뒤 잦아들 때. 분위수가 "
            "한 점이라 q10~q90 구간 적중률이 낮다. 변동성 군집(큰 움직임 뒤 큰 움직임)이 강한 장에서는 "
            "맞기 쉬운 강한 기준이다."
        ),
    )

    def predict(self, inputs: KindInputs) -> KindForecast | None:
        previous = inputs.previous
        if previous is None or previous.label is None or previous.path.get("r") is None:
            return None
        r = float(previous.path["r"])
        k = inputs.flat_ticks if inputs.flat_ticks is not None else int(previous.path["k"])
        return _certain(range_class(r, k), r)


class RangeClimate:
    """Each symbol and 30-minute slot's class shares and quantiles of r over the latest
    training days (#180): a table (``kinds.TableBaseline``) whose parameters are
    ``<symbol>.<slot>.<field>`` (``"005930.09:00.p_small"``: the bar closes 09:01–09:30)
    for ``p_small``, ``p_medium``, ``p_large``, ``q10``, ``q50``, ``q90`` and ``n`` (the
    answers counted), and the same of ``<symbol>.all``, the symbol's whole day. A slot the
    table lacks backs off to its symbol's day, then to the defaults: a third each, and
    quantiles 0, k and 2k ticks (a baseline predicts every bar close, so the lifts pair it
    with every forecast). Quantiles are taken at q·(n − 1) of the sorted r, interpolated."""

    name = CLIMATE
    days = CLIMATE_DAYS
    card = Card(
        scientist=None,
        character="기준 예측: 변동폭 시간대 기후",
        logic=(
            "종목 × 30분 시간대(봉 마감 09:01–09:30은 09:00 칸)마다, 최근 20거래일 학습 창의 변동폭 정답"
            "(녹화일은 호가의 중간가, 그 전 날은 1분봉 규칙)에서 작음·보통·큼의 비율과 변동폭 r의 10·50·90% "
            "분위수를 그 칸의 모든 봉 마감에 낸다. 시간대 말고는 아무 정보도 쓰지 않는다. 변동성의 장중 "
            "계절성(개장·마감 때 큼)만 맞혀서 이기는 모델을 거르는 기준이다."
        ),
        unique_features=(),
        training=(
            "밤 학습이 매일 최근 20거래일(그날까지)의 변동폭 정답으로 칸마다 구간 수와 r의 분위수를 센다. "
            "종목마다 하루 전체의 값도 센다. 성적 비교 없이 늘 새 표로 바꾸고, 다음 거래일 08:00 교체 "
            "때부터 쓴다. 학습 전에는 세 구간 1/3씩, 분위수 0·k·2k틱이다."
        ),
        forecasting=(
            "그 칸의 구간 비율을 세 확률로, r의 분위수를 q10·q50·q90(틱)으로 낸다. 분위수는 정렬한 r의 "
            "q·(n − 1) 위치다(사이는 선형 보간). 표에 없는 칸은 그 종목 하루 전체의 값, 그것도 없으면 "
            "기본값이다(모든 봉 마감에 내야 lift가 모든 예측과 짝지어진다). 예측의 모델 버전은 그 표의 "
            "버전이다(`range-climate-<날짜>-<해시>`, 학습 전에는 `range-climate-default`)."
        ),
        failure=(
            "그날 변동성이 학습 창의 시간대 버릇과 다를 때 틀린다: 뉴스나 지표 발표로 갑자기 커진 날, "
            "유난히 조용한 날, 학습 창이 변동성이 크거나 작은 기간이었을 때. 1분봉 날의 r은 1분봉 규칙의 "
            "근사값이라 녹화일의 중간가 r과 조금 다르다."
        ),
    )

    def __init__(self, table: Model | None = None) -> None:
        self.table = table
        self.version = table.version if table is not None else CLIMATE_DEFAULT

    def using(self, table: Model) -> RangeClimate:
        return RangeClimate(table)

    def default_table(self) -> Model:
        from krx_quant.algorithms.contract import Model  # the contract builds on the kinds

        return Model(CLIMATE_DEFAULT, {})

    def fit(self, samples: Sequence[KindSample]) -> Model:
        """The table of ``samples``' range answers (those with a class); the defaults when none
        has one."""
        from krx_quant.algorithms.contract import Model  # the contract builds on the kinds

        answered = [
            (s, s.answer.label, float(s.answer.path["r"]))
            for s in samples
            if s.answer is not None and s.answer.label is not None and s.answer.path.get("r") is not None
        ]
        if not answered:
            return self.default_table()
        cells: dict[str, list[tuple[str, float]]] = {}
        for s, label, r in answered:
            for cell in (f"{s.symbol}.{slot(s.bar_close)}", f"{s.symbol}.{ALL_DAY}"):
                cells.setdefault(cell, []).append((label, r))
        params: dict[str, float] = {}
        for cell, seen in sorted(cells.items()):
            for c in CLASSES:
                params[f"{cell}.p_{c}"] = sum(label == c for label, _ in seen) / len(seen)
            ranges = sorted(r for _, r in seen)
            for key, level in QUANTILES:
                params[f"{cell}.{key}"] = _quantile(ranges, level)
            params[f"{cell}.n"] = float(len(seen))
        first = min(s.bar_close for s, _, _ in answered).astimezone(KST).date()
        last = max(s.bar_close for s, _, _ in answered).astimezone(KST).date()
        canonical = json.dumps(params, sort_keys=True)
        digest = hashlib.sha256(f"{canonical}|{first}|{last}|{len(answered)}".encode()).hexdigest()[:6]
        return Model(
            version=f"{CLIMATE}-{last:%Y%m%d}-{digest}",
            params=params,
            trained=f"{first}..{last}, {len(answered)} samples, {len(cells)} cells",
            trained_until=last,
            target_version=ANSWER_VERSION,  # the range's answers
        )

    def predict(self, inputs: KindInputs) -> KindForecast:
        params = self.table.params if self.table is not None else {}
        for cell in (f"{inputs.symbol}.{slot(inputs.bar_close)}", f"{inputs.symbol}.{ALL_DAY}"):
            if f"{cell}.n" in params:
                small, medium, large = (params[f"{cell}.p_{c}"] for c in CLASSES)
                return KindForecast(
                    (small, medium, large), {key: params[f"{cell}.{key}"] for key, _ in QUANTILES}
                )
        k = float(inputs.flat_ticks or 0)
        return KindForecast((1 / 3, 1 / 3, 1 / 3), {"q10": 0.0, "q50": k, "q90": 2 * k})


def _quantile(ordered: Sequence[float], level: float) -> float:
    """The ``level`` quantile of the sorted values: at level·(n − 1), interpolated."""
    at = level * (len(ordered) - 1)
    below = math.floor(at)
    above = min(below + 1, len(ordered) - 1)
    return ordered[below] + (ordered[above] - ordered[below]) * (at - below)


def _quantile_meaning(text: str) -> Meaning:
    return Meaning(
        f"봉 마감부터 4분 동안 통합 중간가 변동폭(최고 − 최저)의 {text}",
        "틱(봉 마감 중간가의 호가단위)",
        "봉 마감에 낸다.",
    )


# The range, as registered (``kind_registry.KINDS``).
RANGE = Kind(
    name=NAME,
    title="변동폭",
    classes=CLASSES,
    class_titles=("작음", "보통", "큼"),
    action=("small", "large"),
    answer_version=ANSWER_VERSION,
    answer=range_answer,
    bar_answer=bar_range,
    baselines=(RangeClimate(), RangePersist()),
    fields={
        "q10": _quantile_meaning("10% 분위수. 열에 하나는 이보다 작다."),
        "q50": _quantile_meaning("중앙값(50% 분위수)."),
        "q90": _quantile_meaning("90% 분위수. 열에 아홉은 이 이하다."),
    },
    check=quantiles_ordered,
    stats=quantile_stats,
)
