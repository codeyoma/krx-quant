"""Lorenz: the days most like this one, and what they did. A candidate of the daily kind
``open_to_close`` (#698).

Personality card:
- **Scientist**: Edward Lorenz, whose 1969 "Atmospheric predictability as revealed by naturally
  occurring analogues" forecast the weather from the past days most like the present.
- **Logic**: an analog forecast (k nearest neighbours). A day is a point of what is known at 08:50:
  the night's SOX move and the VIX, the previous day's open→close and close→close, the volatility
  regime, the last 5 and 20 days' moves (``cc_5``, ``cc_20``), how far below its year's high the
  symbol closed (``drawdown``) and how wide its last days were against its usual (``range_5``).
  The day's forecast is the class shares of the ``k`` training days nearest it.
- **Own features**: ``drawdown``, ``range_5``.
- **Learns**: on the last ``TRAINING_DAYS`` (2,450) trading days of every symbol pooled
  (``daily_window``): each input standardized by its training mean and spread and clipped at ±5,
  a missing one 0 (the mean). ``k`` is the one of ``NEIGHBOURS`` whose forecasts of the last 20%
  of the days from the first 80% had the lowest log loss. The model keeps every training day's
  standardized point and class (the library, in the model's blob).
- **Forecasts**: the class counts among the ``k`` nearest days (Euclidean), plus one each, over
  ``k`` + 3. Before training, a third each.
- **Fails**: on a day unlike any in ten years (its nearest days are far, and their shares are
  the climate's), and where the inputs that matter are not among its nine.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
from collections.abc import Mapping, Sequence

import numpy as np

from krx_quant.algorithms.contract import Model, TrainedOn
from krx_quant.algorithms.daily_window import Window, log_loss, window
from krx_quant.cards import Card, Meaning
from krx_quant.daily import DailySample
from krx_quant.daily_features import DAILY_FEATURES
from krx_quant.features import FeatureVector
from krx_quant.kinds import KindForecast
from krx_quant.open_to_close import CLASSES, OPEN_TO_CLOSE

NAME = "lorenz"
TRAINING_DAYS = 2_450
NEIGHBOURS = (25, 50, 100, 200, 400)
CLIP = 5.0
FEATURES: tuple[str, ...] = (
    "us_sox",
    "vix",
    "oc_1",
    "cc_1",
    "vol_ratio",
    "cc_5",
    "cc_20",
    "drawdown",
    "range_5",
)
OWN: tuple[str, ...] = ("drawdown", "range_5")
_CHUNK = 128  # query rows per distance block: 128 × 7,350 × 4 bytes, under 4 MB
_KEPT = 4
_LIBRARIES: dict[str, tuple[np.ndarray, np.ndarray]] = {}  # read once per model version
_LEARNED = (
    f"밤 학습이 최근 {TRAINING_DAYS}거래일(약 10년)의 KRX 일봉으로 매일 다시 정한다. 새 모델은 다음 거래일 "
    "08:00 교체 때부터 쓴다. 학습 전에는 기본값이다."
)


def param_meanings(features: Sequence[str] = FEATURES) -> dict[str, Meaning]:
    """The meaning of every parameter a model can hold (the library is the blob, not a parameter)."""
    out: dict[str, Meaning] = {
        "k": Meaning(
            "이웃 수: 가장 가까운 학습일 몇 개의 비율을 내는지. 학습 기간 뒤 20%로 고른다.", "개", _LEARNED
        ),
        "samples": Meaning("학습한 표본 수(종목 × 날), 곧 비교하는 지난날의 수.", "개", _LEARNED),
        "validation_loss": Meaning(
            "고른 k의 검증 log loss: 앞 80%를 지난날로 두고 뒤 20%를 예측했다(1/3씩이면 1.0986).",
            "nats",
            _LEARNED,
        ),
    }
    for f in features:
        what = DAILY_FEATURES[f].meaning.text
        out[f"mean.{f}"] = Meaning(f"표준화에 쓰는 학습 평균. {f}: {what}", "입력 단위", _LEARNED)
        out[f"scale.{f}"] = Meaning(f"표준화에 쓰는 학습 표준편차. {f}: {what}", "입력 단위", _LEARNED)
    return out


def _raw(samples: Sequence[DailySample], features: Sequence[str]) -> np.ndarray:
    """The inputs as rows, a missing one NaN."""
    rows = [[_number(s.features[f]) for f in features] for s in samples]
    return np.array(rows, dtype=float).reshape(len(samples), len(features))


def _number(value: object) -> float:
    if not isinstance(value, int | float) or not math.isfinite(float(value)):
        return math.nan
    return float(value)


def _standardizer(x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Each column's mean and spread over its known values (0 and 1 for one that never moves)."""
    mean = np.zeros(x.shape[1])
    scale = np.ones(x.shape[1])
    for j in range(x.shape[1]):
        known = x[np.isfinite(x[:, j]), j]
        if len(known) and known.std() > 1e-12:
            mean[j], scale[j] = known.mean(), known.std()
    return mean, scale


def _standardized(x: np.ndarray, mean: np.ndarray, scale: np.ndarray) -> np.ndarray:
    z = np.clip((x - mean) / scale, -CLIP, CLIP)
    out: np.ndarray = np.where(np.isfinite(z), z, 0.0)
    return out


def _nearest(library: np.ndarray, queries: np.ndarray, k: int) -> np.ndarray:
    """The indices of each query's ``k`` nearest library points, nearest first."""
    out = np.empty((len(queries), k), dtype=np.int64)
    library = library.astype(np.float32)
    squares = (library**2).sum(axis=1)
    for start in range(0, len(queries), _CHUNK):
        q = queries[start : start + _CHUNK].astype(np.float32)
        d = squares[None, :] - 2 * q @ library.T
        part = np.argpartition(d, k - 1, axis=1)[:, :k]
        order = np.lexsort((part, np.take_along_axis(d, part, axis=1)), axis=1)
        out[start : start + _CHUNK] = np.take_along_axis(part, order, axis=1)
    return out


def _shares(labels: np.ndarray, k: int) -> np.ndarray:
    """Each row's class shares among its first ``k`` neighbours' labels, plus one each."""
    counts = np.stack([(labels[:, :k] == c).sum(axis=1) for c in range(len(CLASSES))], axis=1)
    out: np.ndarray = (counts + 1.0) / (k + len(CLASSES))
    return out


class Lorenz:
    name = NAME
    kind = OPEN_TO_CLOSE.name
    answer_version = OPEN_TO_CLOSE.answer_version
    features: tuple[str, ...] = FEATURES
    training_days = TRAINING_DAYS
    param_meanings: Mapping[str, Meaning] = param_meanings()
    code_from: tuple[type, ...] = (Window,)  # its window and split (``daily_window``)
    card = Card(
        scientist="Edward Lorenz",
        character="오늘과 가장 닮은 지난날들",
        logic=(
            "유사일 예보(k 최근접 이웃). 하루를 08:50까지 아는 아홉 값의 점으로 본다: 밤사이 SOX 변화와 VIX, "
            "전 거래일 시가 → 종가와 종가 → 종가, 변동성 국면, 최근 5·20거래일 움직임(`cc_5`, `cc_20`), "
            "250거래일 고점 대비 하락(`drawdown`), 최근 5일 범위의 평소 대비(`range_5`). 그날 예측은 이 점에 "
            "가장 가까운 지난날 k개의 클래스 비율이다. Lorenz(1969)가 지금과 가장 닮은 과거의 날씨로 예보한 "
            "방법이다."
        ),
        unique_features=OWN,
        training=(
            f"최근 {TRAINING_DAYS}거래일(약 10년) 세 종목의 날을 한데 모은다. 입력은 학습 평균과 표준편차로 "
            "표준화하고 ±5에서 자르며, 모르는 입력은 0(평균)이다. k는 25·50·100·200·400 중 학습 기간 앞 "
            "80%를 지난날로 두고 뒤 20%를 예측한 log loss가 가장 낮은 값이다(여러 세션 창이면 그 사이를 "
            "비운다). 모델은 학습일마다의 표준화한 점과 클래스를 모두 담는다(blob)."
        ),
        forecasting=(
            "가장 가까운(유클리드 거리) 학습일 k개의 클래스 수에 1씩 더해 k + 3으로 나눈 것이 세 확률이다. "
            "학습 전에는 1/3씩이다."
        ),
        failure=(
            "10년에 없던 날(가장 가까운 날도 멀어 그 비율이 기후와 같아진다), 그날을 정하는 입력이 아홉 값에 "
            "없는 날에 틀리기 쉽다."
        ),
    )

    def default_model(self) -> Model:
        return Model(f"{self.name}-default", {})

    def fit(self, samples: Sequence[DailySample]) -> Model:
        w = window(samples, self.training_days)
        y = np.array([CLASSES.index(label) for label in w.labels])
        raw = _raw(w.samples, self.features)
        best: tuple[float, int] | None = None  # (loss, k)
        largest = max(NEIGHBOURS)
        if w.splits(largest):
            mean, scale = _standardizer(raw[w.early])
            library = _standardized(raw[w.early], mean, scale)
            near = _nearest(library, _standardized(raw[w.late], mean, scale), largest)
            labels = y[w.early][near]
            for k in NEIGHBOURS:
                loss = log_loss(_shares(labels, k), y[w.late])
                if best is None or loss < best[0] - 1e-12:
                    best = (loss, k)
        validation, k = best if best is not None else (math.nan, min(NEIGHBOURS))
        k = min(k, len(y))
        mean, scale = _standardizer(raw)
        points = _standardized(raw, mean, scale).astype(np.float32)
        params: dict[str, float] = {"k": float(k), "samples": float(len(y))}
        if math.isfinite(validation):
            params["validation_loss"] = validation
        for j, f in enumerate(self.features):
            params[f"mean.{f}"], params[f"scale.{f}"] = float(mean[j]), float(scale[j])
        blob = json.dumps(
            {
                "features": list(self.features),
                "points": base64.b64encode(points.tobytes()).decode(),
                "classes": base64.b64encode(y.astype(np.int8).tobytes()).decode(),
            }
        )
        # In-sample, each day is among its own neighbours: a look at the fit, not a validation.
        near = _nearest(points, points, k)
        loss = log_loss(_shares(y[near], k), y)
        first, last = w.samples[0].day, w.samples[-1].day
        digest = hashlib.sha256(
            f"{json.dumps(params, sort_keys=True)}|{first}|{last}|{len(y)}|{blob}".encode()
        ).hexdigest()[:6]
        return Model(
            version=f"{self.name}-{last:%Y%m%d}-{digest}",
            params=params,
            trained=f"{first}..{last}, {len(y)} samples, k {k}, log loss {loss:.4f}",
            trained_until=last,
            blob=blob,
            training=TrainedOn(first, last, len(y), loss),
            target_version=self.answer_version,
        )

    def predict(self, features: FeatureVector, model: Model) -> KindForecast:
        params = model.params
        if "k" not in params or model.blob is None:
            return KindForecast((1 / 3, 1 / 3, 1 / 3))
        points, classes = _library(model)
        names = self.features
        mean = np.array([params[f"mean.{f}"] for f in names])
        scale = np.array([params[f"scale.{f}"] for f in names])
        x = np.array([[_number(features[f]) for f in names]])
        k = int(params["k"])
        near = _nearest(points, _standardized(x, mean, scale), k)
        up, flat, down = (float(p) for p in _shares(classes[near], k)[0])
        return KindForecast((up, flat, down))


def _library(model: Model) -> tuple[np.ndarray, np.ndarray]:
    """The model's training points and classes, decoded once per version."""
    kept = _LIBRARIES.get(model.version)
    if kept is None:
        if len(_LIBRARIES) >= _KEPT:
            _LIBRARIES.clear()
        stored = json.loads(model.blob or "{}")
        n = len(stored["features"])
        points = np.frombuffer(base64.b64decode(stored["points"]), dtype=np.float32).reshape(-1, n)
        classes = np.frombuffer(base64.b64decode(stored["classes"]), dtype=np.int8).astype(np.int64)
        kept = _LIBRARIES[model.version] = (points, classes)
    return kept
