"""Cox: the day's direction from what the night and the days before tell. The first model of
the daily kind ``open_to_close`` (#264).

Personality card:
- **Scientist**: David Cox, whose 1958 paper "The regression analysis of binary sequences"
  set out logistic regression.
- **Logic**: a multinomial logistic regression of the day's class (up, flat, down: the KRX
  opening auction to the closing auction, ``open_to_close``) on what is known at 08:50
  (``daily_features``): the overnight US semiconductors (SOX, NVIDIA, Micron) and the VIX, the
  previous day's foreign and institution net buys, the previous day's moves over the day's
  volatility, the volatility regime, the symbol's recent class shares, the NXT pre-market gap,
  the weekday and the turn of the month, and the night futures once they have a history.
  The evidence it leans on: a US shock is priced at the open and partly given back during the
  day (Park & Yi 2011), Korea's overnight and intraday returns move against each other (Ham et
  al. 2023), and the foreigners' and institutions' flows run with the next day's gap and
  fade after the open (김수경·변영태 2011).
- **Own features**: ``us_sox``, ``us_nvda``, ``us_mu``, ``foreign_net``, ``institution_net``,
  ``premarket_gap``.
- **Learns**: on the last ``TRAINING_DAYS`` (2,450, about ten years) trading days of every
  symbol pooled (three symbols: about 7,350 samples), each input standardized by its training
  mean and spread and clipped at ±5, a missing one 0 with an indicator of its own (an input
  that is always missing or never moves is dropped); flat is the reference class, so up and
  down each have an intercept and a weight per input. The weights are ridge-penalized
  ((λ/2)·|w|², the intercepts not), the mean log loss minimized by Newton's method; λ is the
  one of ``LAMBDAS`` whose model, fitted on the first 80% of the training days, has the lowest
  log loss on the last 20% (time-ordered), and the model is fitted again on all of them with it.
- **Forecasts**: the softmax of the two logits and flat's 0: the three probabilities. Before
  training, a third each.
- **Fails**: when the regime of the night changes (a new driver the last ten years did not
  show), and on days whose news comes after 08:50.

Why this model: a day gives one sample per symbol, so ten years are about 2,450 a symbol and
7,350 pooled; the inputs are few (about 20) and mostly weak. A penalized linear model has few
parameters (two per input), cannot fit the noise a tree ensemble can at that size, gives
calibrated probabilities (the log loss it minimizes is the Brier-like score it is judged on),
deals with missing inputs by an indicator, and is deterministic. The symbols are pooled so the
weights see three times the days; their own base rates enter through the class shares
(``climate_up``, ``climate_down``) rather than a weight per symbol.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence

import numpy as np

from krx_quant.algorithms.contract import Model, NothingToLearn, TrainedOn
from krx_quant.cards import Card, Meaning
from krx_quant.daily import DailySample
from krx_quant.daily_features import DAILY_FEATURES
from krx_quant.features import FeatureVector
from krx_quant.kinds import KindForecast
from krx_quant.open_to_close import CLASSES, OPEN_TO_CLOSE

NAME = "cox"
TRAINING_DAYS = 2_450
LAMBDAS = (0.001, 0.01, 0.03, 0.1, 0.3, 1.0)
VALIDATION_SHARE = 0.2
CLIP = 5.0
MIN_SAMPLES = 250  # fewer labelled samples: nothing to learn
_ITERATIONS = 50
_TOLERANCE = 1e-9

FEATURES: tuple[str, ...] = (
    "us_sox",
    "us_nvda",
    "us_mu",
    "vix",
    "foreign_net",
    "institution_net",
    "oc_1",
    "cc_1",
    "gap_1",
    "range_1",
    "oc_5",
    "vol_ratio",
    "sigma",
    "climate_up",
    "climate_down",
    "premarket_gap",
    "weekday",
    "month_end",
    "month_start",
    "night_futures",
)
OWN: tuple[str, ...] = ("us_sox", "us_nvda", "us_mu", "foreign_net", "institution_net", "premarket_gap")
UP, FLAT, DOWN = CLASSES
_LEARNED = (
    f"밤 학습이 최근 {TRAINING_DAYS}거래일(약 10년)의 KRX 일봉으로 매일 다시 정한다. 새 모델은 다음 거래일 "
    "08:00 교체 때부터 쓴다. 학습 전에는 기본값이다."
)


def columns(features: Sequence[str] = FEATURES) -> list[str]:
    """Every input column the model can have: each feature but the weekday, its missing
    indicator, and the weekday as four indicators (Tuesday to Friday; Monday is the base)."""
    out: list[str] = []
    for f in features:
        if f == "weekday":
            out += [f"weekday.{d}" for d in range(1, 5)]
        else:
            out += [f, f"{f}.missing"]
    return out


def _row(features: FeatureVector, names: Sequence[str]) -> dict[str, float]:
    """A day's raw input columns (missing values 0, their indicator 1)."""
    out: dict[str, float] = {}
    for f in names:
        value = features[f]
        if f == "weekday":
            weekday = int(value) if value is not None else 0
            for d in range(1, 5):
                out[f"weekday.{d}"] = float(weekday == d)
            continue
        missing = value is None or not math.isfinite(float(value))
        out[f] = 0.0 if missing else float(value)  # type: ignore[arg-type]
        out[f"{f}.missing"] = float(missing)
    return out


def param_meanings(features: Sequence[str] = FEATURES) -> dict[str, Meaning]:
    """The meaning of every parameter a model can hold."""
    out: dict[str, Meaning] = {
        "lambda": Meaning("가중치의 능선(L2) 벌점 λ. 학습 기간 뒤 20%로 검증해 고른다.", "배", _LEARNED),
        "samples": Meaning("학습한 표본 수(종목 × 날).", "개", _LEARNED),
        f"b.{UP}": Meaning("상승의 절편(보합 대비 로짓).", "로짓", _LEARNED),
        f"b.{DOWN}": Meaning("하락의 절편(보합 대비 로짓).", "로짓", _LEARNED),
    }
    for col in columns(features):
        base = col.split(".")[0]
        what = DAILY_FEATURES[base].meaning.text if base in DAILY_FEATURES else col
        if col.endswith(".missing"):
            label = f"{base}이 없는지(1) 있는지(0)"
        elif col.startswith("weekday."):
            label = f"요일이 {'화수목금'[int(col.split('.')[1]) - 1]}요일인지"
        else:
            label = f"{base}: {what}"
        out[f"mean.{col}"] = Meaning(f"표준화에 쓰는 학습 평균. {label}", "입력 단위", _LEARNED)
        out[f"scale.{col}"] = Meaning(f"표준화에 쓰는 학습 표준편차. {label}", "입력 단위", _LEARNED)
        out[f"w.{UP}.{col}"] = Meaning(f"상승 로짓의 가중치(표준화한 입력 하나당). {label}", "로짓", _LEARNED)
        out[f"w.{DOWN}.{col}"] = Meaning(
            f"하락 로짓의 가중치(표준화한 입력 하나당). {label}", "로짓", _LEARNED
        )
    return out


def _softmax(z_up: np.ndarray, z_down: np.ndarray) -> np.ndarray:
    """The probabilities (up, flat, down) of the logits, flat's 0."""
    z = np.stack([z_up, np.zeros_like(z_up), z_down], axis=1)
    z -= z.max(axis=1, keepdims=True)
    e = np.exp(z)
    out: np.ndarray = e / e.sum(axis=1, keepdims=True)
    return out


def _fit(x: np.ndarray, y: np.ndarray, penalty: float) -> np.ndarray:
    """Newton's method on the mean log loss + (λ/2)|w|²: the parameters (up's intercept and
    weights, then down's), flat the reference class."""
    n, d = x.shape
    design = np.hstack([np.ones((n, 1)), x])
    k = d + 1
    theta = np.zeros(2 * k)
    targets = np.stack([(y == 0), (y == 2)], axis=1).astype(float)  # up, down
    ridge = np.full(2 * k, penalty)
    ridge[0] = ridge[k] = 0.0  # the intercepts are not penalized

    def loss(theta: np.ndarray) -> float:
        p = _softmax(design @ theta[:k], design @ theta[k:])
        chosen = p[np.arange(n), y]
        return float(-np.mean(np.log(np.maximum(chosen, 1e-12))) + 0.5 * np.sum(ridge * theta**2))

    current = loss(theta)
    for _ in range(_ITERATIONS):
        p = _softmax(design @ theta[:k], design @ theta[k:])
        pu, pd = p[:, 0], p[:, 2]
        residual = np.stack([pu, pd], axis=1) - targets
        gradient = np.concatenate([design.T @ residual[:, 0], design.T @ residual[:, 1]]) / n + ridge * theta
        h_uu = (design * (pu * (1 - pu))[:, None]).T @ design / n
        h_dd = (design * (pd * (1 - pd))[:, None]).T @ design / n
        h_ud = -(design * (pu * pd)[:, None]).T @ design / n
        hessian = np.block([[h_uu, h_ud], [h_ud, h_dd]]) + np.diag(ridge)
        hessian += np.eye(2 * k) * 1e-9
        step = np.linalg.solve(hessian, gradient)
        scale = 1.0
        for _ in range(30):  # halve the step until the loss falls
            trial = theta - scale * step
            tried = loss(trial)
            if tried <= current + 1e-15:
                break
            scale /= 2
        theta, previous = trial, current
        current = tried
        if np.max(np.abs(scale * step)) < _TOLERANCE or previous - current < _TOLERANCE * 1e-3:
            break
    return theta


class Cox:
    name = NAME
    kind = OPEN_TO_CLOSE.name
    features: tuple[str, ...] = FEATURES
    training_days = TRAINING_DAYS
    param_meanings: Mapping[str, Meaning] = param_meanings()
    card = Card(
        scientist="David Cox",
        character="밤사이 정보와 전날 수급으로 하루 방향",
        logic=(
            "그날 KRX 시가 단일가 → 종가 단일가의 방향(상승·보합·하락)을 08:50까지 알던 것으로 다항 "
            "로지스틱 회귀한다: 밤사이 미국 반도체(SOX, 엔비디아, 마이크론)와 VIX, 전 거래일 외국인·기관 "
            "순매수, 전 거래일의 움직임(변동성으로 나눈 것), 변동성 국면, 종목의 최근 클래스 비율, NXT "
            "프리마켓 갭, 요일, 월말·월초, 이력이 쌓인 뒤의 야간선물. 미국 충격은 시가에 반영되고 장중에 "
            "일부 되돌아가며(Park & Yi 2011), 한국의 밤사이와 장중 수익률은 반대로 움직이고(Ham et al. "
            "2023), 전일 외국인·기관 순매수는 다음날 갭과 같은 방향이고 시가 뒤 사라진다(김수경·변영태 "
            "2011)."
        ),
        unique_features=OWN,
        training=(
            f"최근 {TRAINING_DAYS}거래일(약 10년) 세 종목의 날을 한데 모아 학습한다(약 7,350개). 입력은 "
            f"학습 평균과 표준편차로 표준화하고 ±5에서 자른다. 모르는 입력은 0으로 두고 없음 표시를 따로 "
            f"넣는다. 보합을 기준으로 상승·하락의 절편과 가중치를 능선 벌점을 준 로그손실 최소화(뉴턴법)로 "
            f"정한다. 벌점 λ는 학습 기간의 앞 80%로 맞추고 뒤 20%로 검증해 가장 좋은 값을 고른 뒤, 그 λ로 "
            f"전 기간을 다시 맞춘다."
        ),
        forecasting="상승·하락 로짓과 보합의 0을 소프트맥스해 세 확률을 낸다. 학습 전에는 1/3씩이다.",
        failure=(
            "밤사이 정보의 성격이 바뀌는 때(지난 10년에 없던 동인), 08:50 뒤에 나오는 뉴스가 그날을 정하는 "
            "날에 틀리기 쉽다. 일 단위 신호가 약해 맞힘률의 기대치는 1/3보다 조금 높은 정도다."
        ),
    )

    def default_model(self) -> Model:
        return Model(f"{NAME}-default", {})

    def fit(self, samples: Sequence[DailySample]) -> Model:
        labelled = [s for s in samples if s.label is not None]
        days = sorted({s.day for s in labelled})[-self.training_days :]
        kept = {*days}
        labelled = sorted((s for s in labelled if s.day in kept), key=lambda s: (s.day, s.symbol))
        if len(labelled) < MIN_SAMPLES:
            raise NothingToLearn(f"{len(labelled)} labelled days, fewer than {MIN_SAMPLES}")
        raw = [_row(s.features, self.features) for s in labelled]
        y = np.array([CLASSES.index(s.label) for s in labelled if s.label is not None])
        cut = days[int(len(days) * (1 - VALIDATION_SHARE))]
        early = np.array([s.day < cut for s in labelled])
        best: tuple[float, float] | None = None  # (loss, lambda)
        if early.sum() >= MIN_SAMPLES // 2 and (~early).sum() > 0:
            cols, mean, scale = _standardizer([r for r, e in zip(raw, early, strict=True) if e])
            fitted, held = _matrix(raw, cols, mean, scale), y[~early]
            for penalty in LAMBDAS:
                theta = _fit(fitted[early], y[early], penalty)
                p = _probabilities(fitted[~early], theta)
                loss = float(-np.mean(np.log(np.maximum(p[np.arange(len(p)), held], 1e-12))))
                if best is None or loss < best[0] - 1e-12:
                    best = (loss, penalty)
        penalty = LAMBDAS[-1] if best is None else best[1]
        cols, mean, scale = _standardizer(raw)
        x = _matrix(raw, cols, mean, scale)
        theta = _fit(x, y, penalty)
        p = _probabilities(x, theta)
        loss = float(-np.mean(np.log(np.maximum(p[np.arange(len(p)), y], 1e-12))))
        k = len(cols) + 1
        params: dict[str, float] = {"lambda": penalty, "samples": float(len(labelled))}
        params[f"b.{UP}"], params[f"b.{DOWN}"] = float(theta[0]), float(theta[k])
        for i, col in enumerate(cols):
            params[f"mean.{col}"] = mean[col]
            params[f"scale.{col}"] = scale[col]
            params[f"w.{UP}.{col}"] = float(theta[1 + i])
            params[f"w.{DOWN}.{col}"] = float(theta[k + 1 + i])
        first, last = labelled[0].day, labelled[-1].day
        canonical = json.dumps(params, sort_keys=True)
        digest = hashlib.sha256(f"{canonical}|{first}|{last}|{len(labelled)}".encode()).hexdigest()[:6]
        return Model(
            version=f"{NAME}-{last:%Y%m%d}-{digest}",
            params=params,
            trained=f"{first}..{last}, {len(labelled)} samples, log loss {loss:.4f}",
            trained_until=last,
            training=TrainedOn(first, last, len(labelled), loss),
            target_version=OPEN_TO_CLOSE.answer_version,
        )

    def predict(self, features: FeatureVector, model: Model) -> KindForecast:
        params = model.params
        if f"b.{UP}" not in params:
            return KindForecast((1 / 3, 1 / 3, 1 / 3))
        row = _row(features, self.features)
        z_up, z_down = params[f"b.{UP}"], params[f"b.{DOWN}"]
        for col, value in row.items():
            if f"w.{UP}.{col}" not in params:
                continue  # dropped in training: always missing, or never moving
            scale = params[f"scale.{col}"]
            x = min(CLIP, max(-CLIP, (value - params[f"mean.{col}"]) / scale))
            z_up += params[f"w.{UP}.{col}"] * x
            z_down += params[f"w.{DOWN}.{col}"] * x
        up, flat, down = (float(v) for v in _softmax(np.array([z_up]), np.array([z_down]))[0])
        return KindForecast((up, flat, down))


def _standardizer(
    rows: Sequence[Mapping[str, float]],
) -> tuple[list[str], dict[str, float], dict[str, float]]:
    """The columns that move in ``rows``, with their means and spreads."""
    if not rows:
        return [], {}, {}
    names = list(rows[0])
    cols, mean, scale = [], {}, {}
    for col in names:
        values = np.array([r[col] for r in rows])
        spread = float(values.std())
        if spread < 1e-12:
            continue
        cols.append(col)
        mean[col], scale[col] = float(values.mean()), spread
    return cols, mean, scale


def _matrix(
    rows: Sequence[Mapping[str, float]],
    cols: Sequence[str],
    mean: Mapping[str, float],
    scale: Mapping[str, float],
) -> np.ndarray:
    x = np.array([[(r[c] - mean[c]) / scale[c] for c in cols] for r in rows], dtype=float).reshape(
        len(rows), len(cols)
    )
    out: np.ndarray = np.clip(x, -CLIP, CLIP)
    return out


def _probabilities(x: np.ndarray, theta: np.ndarray) -> np.ndarray:
    n = len(x)
    design = np.hstack([np.ones((n, 1)), x])
    k = design.shape[1]
    return _softmax(design @ theta[:k], design @ theta[k:])
