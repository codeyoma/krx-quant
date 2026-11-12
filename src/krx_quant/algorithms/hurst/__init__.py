"""Hurst: momentum or reversal, a rule on one lookback. A candidate of the daily kind
``open_to_close`` (#698).

Personality card:
- **Scientist**: Harold Edwin Hurst, whose 1951 study of the Nile's floods ("Long-term storage
  capacity of reservoirs") measured how far a series persists or turns back.
- **Logic**: whether a symbol's last days' move goes on or turns back. Of the lookbacks of 5, 20
  and 60 trading days (``cc_5``, ``cc_20``, ``cc_60``: the move over the realised volatility),
  the one whose terciles told the classes apart best; the day's forecast is the class shares of
  the training days in its tercile. Momentum and reversal are both in the table: a lookback whose
  top tercile went up more often reads as momentum, one whose bottom tercile did, as reversal.
  The evidence it leans on: individual stocks' returns turn back over a week or a month
  (Jegadeesh 1990, Lehmann 1990) and go on over three to twelve months (Jegadeesh & Titman 1993).
- **Own features**: ``cc_5``, ``cc_20``, ``cc_60``.
- **Learns**: on the last ``TRAINING_DAYS`` (2,450) trading days of every symbol pooled
  (``daily_window``). For each lookback, the terciles' cuts and class counts (plus one each) on
  the first 80% of the days, scored by log loss on the last 20%; the best lookback's cuts and
  counts are then taken from all of them. A day without the lookback's value takes the shares of
  every training day.
- **Forecasts**: the tercile's shares. Before training, a third each.
- **Fails**: when the market's persistence changes sign (a trend after years of reversal); one
  lookback and three bins cannot tell a crash from a dip.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence

import numpy as np

from krx_quant.algorithms.contract import Model, NothingToLearn, TrainedOn
from krx_quant.algorithms.daily_window import Window, log_loss, window
from krx_quant.cards import Card, Meaning
from krx_quant.daily import DailySample
from krx_quant.features import FeatureVector
from krx_quant.kinds import KindForecast
from krx_quant.open_to_close import CLASSES, OPEN_TO_CLOSE

NAME = "hurst"
TRAINING_DAYS = 2_450
LOOKBACKS: tuple[str, ...] = ("cc_5", "cc_20", "cc_60")
BINS = ("low", "mid", "high")
MIN_PER_BIN = 50  # fewer values of a lookback in the fitted part, a bin each: not tried
_DAYS = {"cc_5": 5, "cc_20": 20, "cc_60": 60}
_LEARNED = (
    f"밤 학습이 최근 {TRAINING_DAYS}거래일(약 10년)의 KRX 일봉으로 매일 다시 정한다. 새 모델은 다음 거래일 "
    "08:00 교체 때부터 쓴다. 학습 전에는 기본값이다."
)
_BIN_TITLES = {
    "low": "아래 3분위(가장 많이 내린 쪽)",
    "mid": "가운데 3분위",
    "high": "위 3분위(가장 많이 오른 쪽)",
}


def param_meanings() -> dict[str, Meaning]:
    """The meaning of every parameter a model can hold."""
    out: dict[str, Meaning] = {
        "lookback": Meaning("고른 되돌아보는 기간(5, 20, 60거래일 중 하나).", "거래일", _LEARNED),
        "cut.low": Meaning(
            "아래 3분위와 가운데 3분위의 경계(그 기간 모멘텀 피처의 값).", "표준편차", _LEARNED
        ),
        "cut.high": Meaning("가운데 3분위와 위 3분위의 경계.", "표준편차", _LEARNED),
        "samples": Meaning("학습한 표본 수(종목 × 날).", "개", _LEARNED),
        "validation_loss": Meaning(
            "고른 기간의 검증 log loss: 학습 기간 앞 80%로 정한 표를 뒤 20%로 쟀다(1/3씩이면 1.0986).",
            "nats",
            _LEARNED,
        ),
    }
    for b in (*BINS, "missing"):
        where = "그 기간의 값이 없는 날(모든 학습일의 비율)" if b == "missing" else _BIN_TITLES[b]
        for c in CLASSES:
            out[f"p.{b}.{c}"] = Meaning(
                f"{where}에 그날이 {c}였던 비율(클래스마다 1을 더해).", "확률", _LEARNED
            )
    return out


def _values(samples: Sequence[DailySample], name: str) -> np.ndarray:
    out = []
    for s in samples:
        v = s.features[name]
        out.append(math.nan if v is None or not math.isfinite(float(v)) else float(v))
    return np.array(out, dtype=float)


def _shares(y: np.ndarray) -> np.ndarray:
    counts = np.bincount(y, minlength=len(CLASSES)).astype(float) + 1.0
    out: np.ndarray = counts / counts.sum()
    return out


def _table(x: np.ndarray, y: np.ndarray) -> tuple[float, float, dict[str, np.ndarray]]:
    """The tercile cuts of the known values of ``x`` and each bin's class shares (plus one each);
    ``missing``, the shares of every sample."""
    known = np.isfinite(x)
    low, high = (float(q) for q in np.quantile(x[known], [1 / 3, 2 / 3]))
    bins = _bin(x, low, high)
    table = {b: _shares(y[bins == i]) for i, b in enumerate(BINS)}
    table["missing"] = _shares(y)
    return low, high, table


def _bin(x: np.ndarray, low: float, high: float) -> np.ndarray:
    """Each value's bin (0 low, 1 mid, 2 high), −1 where missing."""
    bins = np.where(x <= low, 0, np.where(x <= high, 1, 2))
    out: np.ndarray = np.where(np.isfinite(x), bins, -1)
    return out


def _probabilities(x: np.ndarray, low: float, high: float, table: Mapping[str, np.ndarray]) -> np.ndarray:
    rows = [*(table[b] for b in BINS), table["missing"]]
    return np.array([rows[i] for i in _bin(x, low, high)])


class Hurst:
    name = NAME
    kind = OPEN_TO_CLOSE.name
    answer_version = OPEN_TO_CLOSE.answer_version
    features: tuple[str, ...] = LOOKBACKS
    training_days = TRAINING_DAYS
    param_meanings: Mapping[str, Meaning] = param_meanings()
    code_from: tuple[type, ...] = (Window,)  # its window and split (``daily_window``)
    card = Card(
        scientist="Harold Edwin Hurst",
        character="모멘텀이냐 되돌림이냐, 한 기간의 규칙",
        logic=(
            "종목의 최근 5·20·60거래일 움직임(실현 변동성으로 나눈 것, `cc_5`, `cc_20`, `cc_60`) 중 3분위로 "
            "나눴을 때 그날 방향(상승·보합·하락)을 가장 잘 가른 기간 하나를 고르고, 그 기간의 같은 3분위에 "
            "든 학습일들의 클래스 비율을 그날 예측으로 낸다. 위 3분위가 더 자주 오르면 모멘텀, 아래 3분위가 "
            "더 자주 오르면 되돌림으로 읽힌다. 개별 종목 수익률은 1주·1달에는 되돌아가고(Jegadeesh 1990, "
            "Lehmann 1990) 3–12달에는 이어진다(Jegadeesh & Titman 1993)."
        ),
        unique_features=LOOKBACKS,
        training=(
            f"최근 {TRAINING_DAYS}거래일(약 10년) 세 종목의 날을 한데 모은다. 기간마다 학습 기간 앞 80%로 "
            "3분위 경계와 칸마다 클래스 수(1씩 더해)를 정하고 뒤 20%의 log loss로 잰다(여러 세션 창이면 그 "
            "사이를 비운다). 가장 낮은 기간을 골라 전 기간으로 경계와 비율을 다시 정한다. "
            "그 기간의 값이 없는 날은 모든 학습일의 비율을 낸다."
        ),
        forecasting="그날 값이 든 3분위의 클래스 비율이 세 확률이다. 학습 전에는 1/3씩이다.",
        failure=(
            "시장의 지속성이 부호를 바꾸는 때(몇 년의 되돌림 뒤 추세), 한 기간과 세 칸으로는 폭락과 작은 "
            "하락을 가르지 못한다. 그날 밤사이 소식은 모른다."
        ),
    )

    def default_model(self) -> Model:
        return Model(f"{self.name}-default", {})

    def fit(self, samples: Sequence[DailySample]) -> Model:
        w = window(samples, self.training_days)
        y = np.array([CLASSES.index(label) for label in w.labels])
        values = {f: _values(w.samples, f) for f in self.features}
        best: tuple[float, str] | None = None  # (loss, lookback)
        if w.splits(1):
            for f in self.features:
                x = values[f]
                if np.isfinite(x[w.early]).sum() < MIN_PER_BIN * len(BINS):
                    continue
                low, high, table = _table(x[w.early], y[w.early])
                loss = log_loss(_probabilities(x[w.late], low, high, table), y[w.late])
                if best is None or loss < best[0] - 1e-12:
                    best = (loss, f)
        if best is None:
            known = [f for f in self.features if np.isfinite(values[f]).sum() >= MIN_PER_BIN * len(BINS)]
            if not known:
                raise NothingToLearn(f"no lookback has {MIN_PER_BIN * len(BINS)} values")
            best = (math.nan, known[-1])
        validation, lookback = best
        low, high, table = _table(values[lookback], y)
        loss = log_loss(_probabilities(values[lookback], low, high, table), y)
        params: dict[str, float] = {
            "lookback": float(_DAYS[lookback]),
            "cut.low": low,
            "cut.high": high,
            "samples": float(len(y)),
        }
        if math.isfinite(validation):
            params["validation_loss"] = validation
        for b, shares in table.items():
            for c, p in zip(CLASSES, shares, strict=True):
                params[f"p.{b}.{c}"] = float(p)
        first, last = w.samples[0].day, w.samples[-1].day
        digest = hashlib.sha256(
            f"{json.dumps(params, sort_keys=True)}|{first}|{last}|{len(y)}".encode()
        ).hexdigest()[:6]
        return Model(
            version=f"{self.name}-{last:%Y%m%d}-{digest}",
            params=params,
            trained=f"{first}..{last}, {len(y)} samples, {lookback}, log loss {loss:.4f}",
            trained_until=last,
            training=TrainedOn(first, last, len(y), loss),
            target_version=self.answer_version,
        )

    def predict(self, features: FeatureVector, model: Model) -> KindForecast:
        params = model.params
        if "lookback" not in params:
            return KindForecast((1 / 3, 1 / 3, 1 / 3))
        lookback = f"cc_{int(params['lookback'])}"
        value = features[lookback]
        if not isinstance(value, int | float) or not math.isfinite(float(value)):
            row = "missing"
        elif value <= params["cut.low"]:
            row = "low"
        elif value <= params["cut.high"]:
            row = "mid"
        else:
            row = "high"
        up, flat, down = (params[f"p.{row}.{c}"] for c in CLASSES)
        return KindForecast((up, flat, down))
