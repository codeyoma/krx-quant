"""Kalman: the fair price under the book's noise, and where it drifts with the futures.

Personality card:
- **Scientist**: Rudolf E. Kálmán.
- **Logic**: a state-space model on the minute grid. The hidden state is a fair price and
  its trend, in ticks; the book's microprice at each minute's end is the fair price plus
  noise. Each minute the fair price moves by its trend, by a share of the KOSPI200
  futures' move of the minute before, and by noise; the trend decays toward 0. A Kalman
  filter runs over the hour since the book last went missing; its state at the bar close
  is carried four minutes ahead (later futures moves unknown, only their spread), and
  the move from the bar close's mid-point is a normal distribution against the flat band.
- **Own features**: the microprices at the minutes' ends and the futures' one-minute moves.
- **Learns**: expectation–maximization over the last 20 trading days' recorded books,
  each symbol's day cut into hours that do not overlap: the Kalman smoother's moments give
  the noises' variances, the trend's decay and the futures' share in closed form.

The book is recorded only (backfilled days have none): on a day with backfilled bars only
it does not learn, and at a bar close without the book it does not predict (``no_book``).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from krx_quant.algorithms.contract import Abstention, Card, Model, NothingToLearn, NotPredicted, Sample
from krx_quant.algorithms.forecasts import UNKNOWN, lowest_log_loss, normal_forecast, with_outcomes
from krx_quant.baselines import Forecast
from krx_quant.features import FeatureVector
from krx_quant.reference import BAR

STEPS = 4  # minutes ahead
# Before any training: a fair price wandering about a tick a minute with a slight trend,
# behind a quarter-tick of noise, not following the futures.
DEFAULT_PARAMS: dict[str, float] = {"q_level": 0.5, "q_trend": 0.02, "r": 0.25, "phi": 0.8, "b": 0.0}
# EM: at most this many rounds, stopping once the log-likelihood barely moves.
ROUNDS = 50
_TOLERANCE = 1e-6
# Bounds that keep the model a model: variances above 0, a trend that decays.
_MIN_VARIANCE = 1e-4
_MAX_PHI = 0.99
# The fair price's spread before the first microprice (ticks²): it knows nothing yet.
_PRIOR = 100.0


@dataclass(frozen=True)
class _Params:
    q_level: float  # the fair price's noise a minute (ticks²)
    q_trend: float  # the trend's noise a minute
    r: float  # the microprice's noise around the fair price
    phi: float  # the trend kept a minute later
    b: float  # ticks the fair price moves for a basis point of futures a minute before

    @classmethod
    def of(cls, params: Mapping[str, float]) -> _Params:
        return cls(params["q_level"], params["q_trend"], params["r"], params["phi"], params["b"])

    def as_dict(self) -> dict[str, float]:
        return {"q_level": self.q_level, "q_trend": self.q_trend, "r": self.r, "phi": self.phi, "b": self.b}


# A state's mean (level, trend) and covariance (level², level·trend, trend²).
_Mean = tuple[float, float]
_Cov = tuple[float, float, float]


def _start(p: _Params, y0: float) -> tuple[_Mean, _Cov]:
    """Before the first microprice: about it, the trend at rest."""
    trend = p.q_trend / (1 - p.phi * p.phi)
    return (y0, 0.0), (_PRIOR, 0.0, trend)


def _ahead(p: _Params, m: _Mean, c: _Cov, u: float) -> tuple[_Mean, _Cov]:
    """A minute later, after a futures move ``u`` in the minute before."""
    (level, trend), (ll, lt, tt) = m, c
    return (
        (level + trend + p.b * u, p.phi * trend),
        (ll + 2 * lt + tt + p.q_level, p.phi * (lt + tt), p.phi * p.phi * tt + p.q_trend),
    )


@dataclass
class _Filtered:
    """The forward pass over one sequence: each minute's predicted and filtered state."""

    predicted: list[tuple[_Mean, _Cov]]
    filtered: list[tuple[_Mean, _Cov]]
    likelihood: float


def _filter(p: _Params, ys: Sequence[float], us: Sequence[float]) -> _Filtered:
    """Filter microprices ``ys``; ``us[k]`` is the futures' move in the minute ending at ``ys[k]``."""
    predicted: list[tuple[_Mean, _Cov]] = []
    filtered: list[tuple[_Mean, _Cov]] = []
    likelihood = 0.0
    m, c = _start(p, ys[0])
    for k, y in enumerate(ys):
        if k > 0:
            m, c = _ahead(p, filtered[-1][0], filtered[-1][1], us[k - 1])
        predicted.append((m, c))
        (level, trend), (ll, lt, tt) = m, c
        s = ll + p.r
        gain_l, gain_t, error = ll / s, lt / s, y - level
        m = (level + gain_l * error, trend + gain_t * error)
        c = (ll - gain_l * ll, lt - gain_l * lt, tt - gain_t * lt)
        filtered.append((m, c))
        if k > 0:  # the first microprice only places the fair price
            likelihood -= 0.5 * (math.log(2 * math.pi * s) + error * error / s)
    return _Filtered(predicted, filtered, likelihood)


def _aligned(ys: Sequence[float], moves: Sequence[float]) -> list[float]:
    """The futures' move of each microprice's minute: both series end at the bar close; a
    minute the futures series does not reach is a move of 0."""
    gap = len(ys) - len(moves)
    return [moves[k - gap] if k >= gap else 0.0 for k in range(len(ys))]


def forecast_move(p: _Params, ys: Sequence[float], moves: Sequence[float]) -> tuple[float, float]:
    """The mean and variance of the mid-point's move over the next ``STEPS`` minutes, in
    ticks from the bar close's (the microprices are in ticks from it)."""
    us = _aligned(ys, moves)
    m, c = _filter(p, ys, us).filtered[-1]
    spread = sum(u * u for u in moves) / len(moves) if moves else 0.0  # a minute's futures move, squared
    m, c = _ahead(p, m, c, us[-1])
    for _ in range(STEPS - 1):
        m, c = _ahead(p, m, c, 0.0)
        c = (c[0] + p.b * p.b * spread, c[1], c[2])
    return m[0], c[0] + p.r


class Kalman:
    name = "kalman"
    features: tuple[str, ...] = ("microprice_ticks", "kospi200f_moves", "flat_ticks")
    training_days = 20
    card = Card(
        scientist="Rudolf E. Kálmán",
        character="잡음 제거·선물 추종",
        logic=(
            "호가창의 microprice를 숨은 적정가에 잡음이 낀 관측으로 본다. 적정가는 매분 추세만큼, "
            "그리고 1분 전 KOSPI200 선물 움직임의 일부만큼 움직이고, 추세는 서서히 줄어든다. "
            "칼만 필터로 최근 1시간의 적정가와 추세를 걸러 내 4분 뒤로 옮기고, 중간가 움직임의 "
            "정규분포를 낸다. 호가가 비어 있으면 예측하지 않는다."
        ),
        unique_features=("microprice_ticks", "kospi200f_moves"),
        training=(
            "최근 20거래일의 녹화된 호가로 EM을 한다. 종목·날짜마다 겹치지 않는 1시간 구간을 칼만 "
            "스무더로 돌려, 적정가·추세·관측 잡음의 분산과 추세 감쇠, 선물 계수를 닫힌 해로 다시 추정한다."
        ),
    )

    def default_model(self) -> Model:
        return Model(f"{self.name}-default", dict(DEFAULT_PARAMS))

    def predict(self, features: FeatureVector, model: Model) -> Forecast:
        ys = features.series("microprice_ticks")
        if not ys:
            raise NotPredicted(Abstention.NO_BOOK)
        flat = features["flat_ticks"]
        if flat is None:
            return UNKNOWN
        mean, variance = forecast_move(_Params.of(model.params), ys, features.series("kospi200f_moves"))
        if not (math.isfinite(mean) and math.isfinite(variance)):
            return UNKNOWN
        return normal_forecast(mean, math.sqrt(max(variance, 0.0)), flat)

    def fit(self, samples: Sequence[Sample]) -> Model:
        scored = [s for s in with_outcomes(samples) if s.features.series("microprice_ticks")]
        if not scored:
            raise NothingToLearn("no training samples with the book")
        sequences = _sequences(samples)
        if not sequences:
            raise NothingToLearn("no two minutes of the book in a row to learn from")
        p = _Params.of(DEFAULT_PARAMS)
        previous = None
        for _ in range(ROUNDS):
            p, likelihood = _reestimate(p, sequences)
            if previous is not None and abs(likelihood - previous) < _TOLERANCE * max(1.0, abs(previous)):
                break
            previous = likelihood
        return lowest_log_loss(
            self.name, scored, [p.as_dict()], lambda f, q: self.predict(f, Model(self.name, q))
        )


def _sequences(samples: Sequence[Sample]) -> list[tuple[list[float], list[float]]]:
    """Each symbol's day as hours that do not overlap, latest first: (microprices, futures
    moves of their minutes). Each sample holds the hour up to its bar close."""
    days: dict[tuple[str, object], list[Sample]] = {}
    for s in samples:
        days.setdefault((s.symbol, s.bar_close.date()), []).append(s)
    out = []
    for day in days.values():
        day.sort(key=lambda s: s.bar_close, reverse=True)
        free_before = None  # the earliest minute's end taken so far
        for s in day:
            ys = list(s.features.series("microprice_ticks"))
            if len(ys) < 2:
                continue
            first = s.bar_close - (len(ys) - 1) * BAR
            if free_before is not None and s.bar_close >= free_before:
                continue
            out.append((ys, _aligned(ys, s.features.series("kospi200f_moves"))))
            free_before = first
    return out


def _reestimate(p: _Params, sequences: Sequence[tuple[list[float], list[float]]]) -> tuple[_Params, float]:
    """One EM round: the parameters from the smoothed states, and the log-likelihood before."""
    likelihood = 0.0
    obs_n = 0
    obs_sum = 0.0
    trans_n = 0
    # Sums for the trend's row (v_k = φ v_{k-1}) and the fair price's (D_k = b u_{k-1}).
    vv_cross = vv_prev = vv_now = 0.0
    du = uu = dd = 0.0
    for ys, us in sequences:
        run = _filter(p, ys, us)
        likelihood += run.likelihood
        smoothed, lagged = _smooth(p, run)
        for k, y in enumerate(ys):
            (level, _), (ll, _, _) = smoothed[k]
            obs_sum += (y - level) ** 2 + ll
            obs_n += 1
        for k in range(1, len(ys)):
            (l1, t1), (c_ll1, c_lt1, c_tt1) = smoothed[k]
            (l0, t0), (c_ll0, c_lt0, c_tt0) = smoothed[k - 1]
            x_ll, x_lt, _, x_tt = lagged[k]  # Cov(state_k, state_{k-1}): (L,L'), (L,T'), (T,L'), (T,T')
            u = us[k - 1]
            e_tt_cross = x_tt + t1 * t0
            e_t0 = c_tt0 + t0 * t0
            e_t1 = c_tt1 + t1 * t1
            vv_cross += e_tt_cross
            vv_prev += e_t0
            vv_now += e_t1
            # D_k = L_k − L_{k−1} − T_{k−1}
            e_d = l1 - l0 - t0
            e_l1l1 = c_ll1 + l1 * l1
            e_l0l0 = c_ll0 + l0 * l0
            e_l1l0 = x_ll + l1 * l0
            e_l1t0 = x_lt + l1 * t0
            e_l0t0 = c_lt0 + l0 * t0
            e_dd = e_l1l1 + e_l0l0 + e_t0 - 2 * e_l1l0 - 2 * e_l1t0 + 2 * e_l0t0
            dd += e_dd
            du += e_d * u
            uu += u * u
            trans_n += 1
    if trans_n == 0:
        return p, likelihood
    phi = min(max(vv_cross / vv_prev, 0.0), _MAX_PHI) if vv_prev > 0 else p.phi
    q_trend = max((vv_now - 2 * phi * vv_cross + phi * phi * vv_prev) / trans_n, _MIN_VARIANCE)
    b = du / uu if uu > 0 else 0.0
    q_level = max((dd - 2 * b * du + b * b * uu) / trans_n, _MIN_VARIANCE)
    r = max(obs_sum / obs_n, _MIN_VARIANCE)
    return _Params(q_level, q_trend, r, phi, b), likelihood


def _smooth(
    p: _Params, run: _Filtered
) -> tuple[list[tuple[_Mean, _Cov]], list[tuple[float, float, float, float]]]:
    """The Rauch–Tung–Striebel smoother: each minute's state given the whole sequence, and
    the covariance of each with the one before (row: this minute's level, trend)."""
    n = len(run.filtered)
    smoothed: list[tuple[_Mean, _Cov]] = [run.filtered[-1]] * n
    lagged: list[tuple[float, float, float, float]] = [(0.0, 0.0, 0.0, 0.0)] * n
    for k in range(n - 2, -1, -1):
        (fl, ft), (fll, flt, ftt) = run.filtered[k]
        (pl, pt), (pll, plt, ptt) = run.predicted[k + 1]
        (sl, st), (sll, slt, stt) = smoothed[k + 1]
        # J = P_f Fᵀ P_pred⁻¹, with P_f Fᵀ = [[fll + flt, φ flt], [flt + ftt, φ ftt]].
        a11, a12, a21, a22 = fll + flt, p.phi * flt, flt + ftt, p.phi * ftt
        det = pll * ptt - plt * plt
        i11, i12, i22 = ptt / det, -plt / det, pll / det
        j11, j12 = a11 * i11 + a12 * i12, a11 * i12 + a12 * i22
        j21, j22 = a21 * i11 + a22 * i12, a21 * i12 + a22 * i22
        dl, dt = sl - pl, st - pt
        mean = (fl + j11 * dl + j12 * dt, ft + j21 * dl + j22 * dt)
        # P_s = P_f + J (P_s' − P_pred) Jᵀ
        e11, e12, e22 = sll - pll, slt - plt, stt - ptt
        m11, m12 = j11 * e11 + j12 * e12, j11 * e12 + j12 * e22
        m21, m22 = j21 * e11 + j22 * e12, j21 * e12 + j22 * e22
        cov = (
            fll + m11 * j11 + m12 * j12,
            flt + m11 * j21 + m12 * j22,
            ftt + m21 * j21 + m22 * j22,
        )
        smoothed[k] = (mean, cov)
        # Cov(state_{k+1}, state_k) = P_s(k+1) Jᵀ
        lagged[k + 1] = (
            sll * j11 + slt * j12,
            sll * j21 + slt * j22,
            slt * j11 + stt * j12,
            slt * j21 + stt * j22,
        )
    return smoothed, lagged
