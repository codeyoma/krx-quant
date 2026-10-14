"""Markov: the state of the market a symbol is in, and where it goes next.

Personality card:
- **Scientist**: Andrey Markov.
- **Logic**: a hidden Markov model of four states (quiet, rising, falling, wild), each
  emitting one-minute close changes in ticks from its own normal distribution. The last
  hour of the symbol's moves is filtered forward from the chain's long-run mix of states to
  how likely each state is now. The next four minutes follow the chain's transitions, and
  their summed move is a normal distribution with the mean and variance the chain gives
  it, against the flat band. The transitions are those of the day's macro regime (shock,
  risk_off, risk_on, neutral, decided before the open); on a day without one, the default
  transitions, learned from every day. The options' at-the-money implied volatility at
  the bar close puts the market in an IV regime (low, normal or high against two learned
  bounds): a minute's move there is the states' times the regime's scale. Without the IV
  (a backfilled day) or the bounds (too few recorded days), the scale is 1.
- **Own features**: the day's macro regime, the last hour of one-minute moves and the
  options' at-the-money implied volatility.
- **Learns**: Baum–Welch re-estimation over the last 20 trading days, each symbol's day a
  sequence of moves: the four states' emissions from every day together, each regime's
  transitions from its own days (pulled toward the default where it has few), and the
  default transitions from all days. The IV bounds split the training window's recorded
  bar closes in three equal parts, once five recorded days have the IV; each IV regime's
  scale is matched on its minutes' mean square move, pulled toward 1.
- **Fails**: when the state turns within minutes (the filtered hour lags it), and on a day
  whose macro regime is not the market's.

Training measures moves on closes, which bounce between the bid and the ask, so the wild
state learned on backfilled days is a little wilder than live, on mid-points.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from typing import NamedTuple

from krx_quant.algorithms.contract import Model, Sample
from krx_quant.algorithms.forecasts import (
    NORMAL_CLASSES,
    UNKNOWN,
    lowest_log_loss,
    normal_forecast,
    param,
    with_outcomes,
)
from krx_quant.baselines import Forecast
from krx_quant.cards import Card, Meaning, learned
from krx_quant.features import REGIME_CODES, FeatureVector
from krx_quant.reference import BAR

STATES = ("quiet", "up", "down", "wild")  # in this order once fitted
K = len(STATES)
REGIMES = tuple(REGIME_CODES)
DEFAULT = "default"  # the transitions of a day without a macro regime
TRANSITIONS = (*REGIMES, DEFAULT)
STEPS = 4  # minutes ahead
# Before any training: quiet and wild minutes around no move, trends of a tick a minute,
# each state lasting about ten minutes.
_MEANS = (0.0, 1.0, -1.0, 0.0)
_SDS = (0.5, 1.0, 1.0, 2.5)
_STAY = 0.9
# Baum–Welch: at most this many rounds, stopping once the log-likelihood barely moves.
ROUNDS = 30
_TOLERANCE = 1e-6
# A regime's transitions count this many of the default's, per state, besides its own.
PRIOR = 20.0
# Ticks: never surer of a minute than this. Moves come in whole ticks, so a narrower quiet
# state would fit only exact zeros and leave it at the first one-tick move.
MIN_SD = 0.5
# Each state's own transitions count this many of the untrained ones besides the data's,
# so a state the data never visits keeps them.
_UNTRAINED = 1.0
# The options' at-the-money implied volatility, low, normal or high against two bounds
# learned in training (``iv_cut.0`` and ``iv_cut.1``); a minute's move in each is the states'
# times its scale (``iv_scale.<n>``). Normal is the states' own size, as an unknown IV is:
# its scale stays 1, so the states and the scales cannot trade size between them.
IV_REGIMES = ("low", "normal", "high")
LOW_IV, NORMAL_IV, HIGH_IV = range(len(IV_REGIMES))
# The bounds are learned only from this many recorded days with the IV, or more; with fewer
# the IV is unknown to the model.
MIN_IV_DAYS = 5
# A regime's scale counts this many minutes at 1 besides its own, so one seen little stays
# near 1.
SCALE_PRIOR = 60.0
TRAINING_DAYS = 20


def _param_meanings() -> dict[str, Meaning]:
    """Every parameter a model can hold: the states' emissions, the IV regimes' scales and
    bounds (bounds only once learned), and each regime's transitions."""
    korean = {"quiet": "조용함", "up": "상승추세", "down": "하락추세", "wild": "급변동"}
    states = [korean[s] for s in STATES]
    regimes = {
        "shock": "매크로 국면이 shock(충격)인 날",
        "risk_off": "매크로 국면이 risk_off(위험 회피)인 날",
        "risk_on": "매크로 국면이 risk_on(위험 선호)인 날",
        "neutral": "매크로 국면이 neutral(중립)인 날",
        DEFAULT: "매크로 국면이 없거나 모르는 날(기본 전이행렬, 모든 날로 배운다)",
    }
    iv_regimes = [{"low": "낮음", "normal": "보통", "high": "높음"}[r] for r in IV_REGIMES]
    when = learned(TRAINING_DAYS)
    out: dict[str, Meaning] = {}
    for k, state in enumerate(states):
        out[param("mean", k)] = Meaning(
            f"{state} 국면의 1분 움직임 평균(IV 국면의 배율을 곱하기 전).", "틱", when
        )
        out[param("sd", k)] = Meaning(
            f"{state} 국면의 1분 움직임 표준편차(IV 국면의 배율을 곱하기 전. 곱한 뒤 0.5틱 아래로 내려가지 "
            "않는다).",
            "틱",
            when,
        )
    for r, iv in enumerate(iv_regimes):
        text = f"IV {iv} 국면에서 4국면의 평균과 표준편차에 곱하는 배율. "
        if r == NORMAL_IV:
            text += "늘 1이다(IV를 모를 때와 같다)."
        else:
            text += "그 국면의 분이 적으면 1 쪽으로 당긴다."
        out[param("iv_scale", r)] = Meaning(text, "배", when)
    out[param("iv_cut", 0)] = Meaning(
        "IV 국면의 아래 경계: 봉 마감 ATM IV가 이보다 낮으면 낮음 국면이다. 학습 표본의 IV를 셋으로 똑같이 "
        "나누는 첫 값.",
        "%",
        f"{when} 학습 기간에 IV가 있는 날이 5거래일보다 적으면 없다(그동안 IV를 모르는 것처럼 동작한다).",
    )
    out[param("iv_cut", 1)] = Meaning(
        "IV 국면의 위 경계: 봉 마감 ATM IV가 이 값 이상이면 높음 국면이다. 학습 표본의 IV를 셋으로 똑같이 "
        "나누는 둘째 값.",
        "%",
        f"{when} 학습 기간에 IV가 있는 날이 5거래일보다 적으면 없다(그동안 IV를 모르는 것처럼 동작한다).",
    )
    for t in TRANSITIONS:
        pulled = "" if t == DEFAULT else " 그 국면의 날이 적으면 기본 전이행렬 쪽으로 당긴다."
        for i, before in enumerate(states):
            for j, after in enumerate(states):
                out[param(t, i, j)] = Meaning(
                    f"{regimes[t]}, {before} 국면에서 1분 뒤 {after} 국면에 있을 확률(전이행렬).{pulled}",
                    "확률 (0~1)",
                    when,
                )
    return out


PARAM_MEANINGS: Mapping[str, Meaning] = _param_meanings()


def default_params() -> dict[str, float]:
    """No IV bounds before training (an unknown IV), every scale 1."""
    params = {param("mean", k): _MEANS[k] for k in range(K)} | {param("sd", k): _SDS[k] for k in range(K)}
    params |= {param("iv_scale", r): 1.0 for r in range(len(IV_REGIMES))}
    for t in TRANSITIONS:
        for i in range(K):
            for j in range(K):
                params[param(t, i, j)] = _STAY if i == j else (1 - _STAY) / (K - 1)
    return params


Matrix = list[list[float]]


class _Chain:
    """A model's emissions, transitions and IV regimes, read from its parameters (a model
    trained before the IV has neither bounds nor scales: its IV is unknown)."""

    def __init__(self, params: Mapping[str, float]) -> None:
        self.means = [params[param("mean", k)] for k in range(K)]
        self.sds = [params[param("sd", k)] for k in range(K)]
        self.transitions = {
            t: [[params[param(t, i, j)] for j in range(K)] for i in range(K)] for t in TRANSITIONS
        }
        low, high = params.get(param("iv_cut", 0)), params.get(param("iv_cut", 1))
        self.cuts = None if low is None or high is None else (low, high)
        self.scales = [params.get(param("iv_scale", r), 1.0) for r in range(len(IV_REGIMES))]

    def scale(self, regime: int | None) -> float:
        """The move scale of an IV regime; 1 without one."""
        return 1.0 if regime is None else self.scales[regime]

    def scale_at(self, iv: float | None) -> float:
        """The move scale at the options' implied volatility ``iv``."""
        return self.scale(_iv_regime(iv, self.cuts))

    def emitting(self, scale: float) -> tuple[list[float], list[float]]:
        """The states' means and standard deviations of a minute's move at ``scale``."""
        return [m * scale for m in self.means], [max(s * scale, MIN_SD) for s in self.sds]

    def emissions(self, move: float, scale: float) -> tuple[list[float], float]:
        """Each state's density of ``move``, scaled so the largest is 1, and the log of the scale."""
        means, sds = self.emitting(scale)
        logs = [
            -0.5 * ((move - m) / s) ** 2 - math.log(s) - 0.5 * math.log(2 * math.pi)
            for m, s in zip(means, sds, strict=True)
        ]
        top = max(logs)
        return [math.exp(v - top) for v in logs], top

    def forward(
        self, moves: Sequence[float], a: Matrix, scales: Sequence[float]
    ) -> tuple[list[list[float]], list[list[float]], float]:
        """Filter ``moves`` (each at its IV scale) forward from the chain's long-run mix of
        states: how likely each state is after each move, each move's (scaled) emissions, and
        the log-likelihood."""
        alphas: list[list[float]] = []
        emitted: list[list[float]] = []
        likelihood = 0.0
        p = _stationary(a)
        for n, move in enumerate(moves):
            e, top = self.emissions(move, scales[n])
            raw = [q * e[j] for j, q in enumerate(p if not alphas else _step(alphas[-1], a))]
            c = sum(raw) or 1e-300
            alphas.append([v / c for v in raw])
            emitted.append(e)
            likelihood += math.log(c) + top
        return alphas, emitted, likelihood

    def now(self, moves: Sequence[float], a: Matrix, scale: float) -> list[float]:
        """How likely each state is after ``moves``, all at ``scale``."""
        alphas, _, _ = self.forward(moves, a, [scale] * len(moves))
        return alphas[-1] if alphas else _stationary(a)

    def ahead(self, p: list[float], a: Matrix, scale: float) -> tuple[float, float]:
        """The mean and variance of the next ``STEPS`` minutes' summed move from states ``p``,
        at ``scale``."""
        mu, sds = self.emitting(scale)
        second = [s * s + m * m for m, s in zip(mu, sds, strict=True)]
        dists = [p]
        for _ in range(STEPS):
            dists.append(_step(dists[-1], a))
        means = [_dot(d, mu) for d in dists[1:]]
        variance = sum(_dot(d, second) - m * m for d, m in zip(dists[1:], means, strict=True))
        for i in range(1, STEPS + 1):
            # E[move_i · move_j]: the state at i weighs the means reached from it j − i later.
            reach = [mu[:]]  # mu after 0 steps
            for _ in range(STEPS - i):
                reach.append([sum(a[s][t] * reach[-1][t] for t in range(K)) for s in range(K)])
            for gap in range(1, STEPS - i + 1):
                joint = sum(dists[i][s] * mu[s] * reach[gap][s] for s in range(K))
                variance += 2 * (joint - means[i - 1] * means[i - 1 + gap])
        return sum(means), max(variance, 0.0)


class Markov:
    name = "markov"
    features: tuple[str, ...] = ("macro_regime", "minute_ticks", "flat_ticks", "atm_iv")
    training_days = TRAINING_DAYS
    param_meanings = PARAM_MEANINGS
    card = Card(
        scientist="Andrey Markov",
        character="국면 탐정",
        logic=(
            "1분 종가 변화(틱)를 조용함·상승추세·하락추세·급변동 4국면의 은닉 마르코프 모델로 본다. "
            "최근 1시간의 움직임으로 지금 국면의 확률을 걸러 내고, 그날 매크로 국면의 전이행렬로 "
            "4분 뒤까지 국면이 옮겨 가는 것을 따라가 4분 움직임의 분포를 낸다. 매크로 국면이 없는 날은 "
            "기본 전이행렬을 쓴다. 옵션 등가격 내재변동성(ATM IV)이 낮음·보통·높음 중 어느 국면인지에 따라 "
            "1분 움직임의 크기를 줄이거나 늘린다."
        ),
        unique_features=("macro_regime", "minute_ticks", "atm_iv"),
        training=(
            "최근 20거래일의 종목·날짜별 1분 움직임으로 Baum–Welch 재추정을 한다. 국면별 분포는 모든 날로, "
            "전이행렬은 매크로 국면마다 그 국면의 날로(날이 적으면 기본 행렬 쪽으로 당긴다), 기본 행렬은 "
            "모든 날로 배운다. IV 국면의 경계는 녹화된 봉 마감 IV의 3분위로(녹화 5거래일부터), 국면별 "
            "움직임 배율은 그 국면의 분들로 배운다."
        ),
        forecasting=(
            "최근 1시간의 1분 움직임으로 거른 지금 국면의 확률에서, 그날 전이행렬로 4분을 따라가 4분 변화 "
            "합의 평균과 분산을 정확히 계산한다(분마다의 분산과 국면이 이어지며 생기는 공분산 포함). IV "
            "국면의 배율은 4국면의 평균과 표준편차에 곱한다. 4분 뒤 중간가 변화를 그 평균과 분산의 "
            f"정규분포로 본다. {NORMAL_CLASSES} 기대수익은 그 평균이다. 보합 범위를 모르면 세 확률을 1/3씩 "
            "둔다."
        ),
        failure=(
            "국면이 몇 분 안에 바뀌는 장(갑작스러운 뉴스, 급반전)에서는 최근 1시간으로 거른 국면이 늦게 "
            "따라가 틀리기 쉽다. 그날 매크로 국면이 장중 흐름과 다르거나 국면이 없는 날에는 전이행렬이 맞지 "
            "않는다. 변동성이 장중에 갑자기 바뀌어도 IV 국면은 봉 마감의 값 하나로 1시간 전체를 본다."
        ),
    )

    def default_model(self) -> Model:
        return Model(f"{self.name}-default", default_params())

    def predict(self, features: FeatureVector, model: Model) -> Forecast:
        flat = features["flat_ticks"]
        if flat is None:
            return UNKNOWN
        chain = _Chain(model.params)
        a = chain.transitions[_transitions_of(features["macro_regime"])]
        # The IV regime now stands for the whole hour: it moves slowly.
        scale = chain.scale_at(features["atm_iv"])
        mean, variance = chain.ahead(chain.now(features.series("minute_ticks"), a, scale), a, scale)
        return normal_forecast(mean, math.sqrt(variance), flat)

    def fit(self, samples: Sequence[Sample]) -> Model:
        scored = with_outcomes(samples)
        cuts = _iv_bounds(samples)
        days = _sequences(samples, cuts)
        params = default_params()
        if cuts is not None:
            params |= {param("iv_cut", 0): cuts[0], param("iv_cut", 1): cuts[1]}
        previous = None
        # Not strictly EM (the regimes' transitions are pulled toward the default), so the
        # likelihood may wobble: stop once it barely moves either way.
        for _ in range(ROUNDS):
            params, likelihood = _reestimate(params, days)
            if previous is not None and abs(likelihood - previous) < _TOLERANCE * max(1, len(days)):
                break
            previous = likelihood
        fitted = _in_order(params)
        return lowest_log_loss(self.name, scored, [fitted], lambda f, p: self.predict(f, Model(self.name, p)))


def _iv_regime(iv: float | None, cuts: tuple[float, float] | None) -> int | None:
    """Low, normal or high (0, 1, 2) against ``cuts``; None without the IV or the bounds."""
    if iv is None or cuts is None:
        return None
    return LOW_IV if iv < cuts[0] else HIGH_IV if iv >= cuts[1] else NORMAL_IV


def _iv_bounds(samples: Sequence[Sample]) -> tuple[float, float] | None:
    """The bounds splitting the samples' IVs in three equal parts, once ``MIN_IV_DAYS``
    recorded days have it; else None."""
    known = [(s.bar_close.date(), iv) for s in samples if (iv := s.features["atm_iv"]) is not None]
    if len({day for day, _ in known}) < MIN_IV_DAYS:
        return None
    low, high = statistics.quantiles([iv for _, iv in known], n=len(IV_REGIMES), method="inclusive")
    return low, high


def _transitions_of(code: float | None) -> str:
    """The transitions of a macro regime's code; the default without one."""
    if code is None:
        return DEFAULT
    return next((r for r, c in REGIME_CODES.items() if c == code), DEFAULT)


class Day(NamedTuple):
    """A symbol's day: its transitions' name, its one-minute moves in time order, and each
    move's IV regime."""

    transitions: str
    moves: list[float]
    regimes: list[int | None]


def _sequences(samples: Sequence[Sample], cuts: tuple[float, float] | None) -> list[Day]:
    """Each symbol's day as its transitions' name, its one-minute moves in time order (the
    first sample's hour, then each later one's newest minutes) and each move's IV regime
    (that of the sample it came with)."""
    days: dict[tuple[str, object], list[Sample]] = {}
    for s in samples:
        days.setdefault((s.symbol, s.bar_close.date()), []).append(s)
    out = []
    for day in days.values():
        day.sort(key=lambda s: s.bar_close)
        moves: list[float] = []
        regimes: list[int | None] = []
        previous = None
        for s in day:
            hour = s.features.series("minute_ticks")
            new = len(hour) if previous is None else min(len(hour), int((s.bar_close - previous) / BAR))
            moves += hour[len(hour) - new :]
            regimes += [_iv_regime(s.features["atm_iv"], cuts)] * new
            previous = s.bar_close
        if len(moves) >= 2:
            out.append(Day(_transitions_of(day[0].features["macro_regime"]), moves, regimes))
    return out


def _reestimate(params: Mapping[str, float], days: Sequence[Day]) -> tuple[dict[str, float], float]:
    """One Baum–Welch round: the re-estimated parameters, and the log-likelihood before it.

    The states' means and deviations are those of the moves over their IV scale; the low
    and high IV regimes' scales, how much larger their moves are than the states make them
    (not strictly EM: matched on the mean square as the chain emits it, floor included, and
    pulled toward 1). The normal regime's stays 1.
    """
    chain = _Chain(params)
    weight = [0.0] * K
    total = [0.0] * K
    squares = [0.0] * K
    counts = {t: [[0.0] * K for _ in range(K)] for t in TRANSITIONS}
    # By IV regime: the minutes, their squared moves, and their weight on each state.
    minutes = [0.0] * len(IV_REGIMES)
    squared = [0.0] * len(IV_REGIMES)
    occupancy = [[0.0] * K for _ in IV_REGIMES]
    likelihood = 0.0
    for name, moves, regimes in days:
        a = chain.transitions[name]
        scales = [chain.scale(r) for r in regimes]
        alphas, emitted, day_likelihood = chain.forward(moves, a, scales)
        likelihood += day_likelihood
        beta = [1.0] * K
        for n in range(len(moves) - 1, -1, -1):
            gamma = _normalized([alphas[n][k] * beta[k] for k in range(K)])
            unscaled = moves[n] / scales[n]
            for k in range(K):
                weight[k] += gamma[k]
                total[k] += gamma[k] * unscaled
                squares[k] += gamma[k] * unscaled**2
            if (r := regimes[n]) is not None:
                minutes[r] += 1
                squared[r] += moves[n] ** 2
                for k in range(K):
                    occupancy[r][k] += gamma[k]
            if n == 0:
                break
            e = emitted[n]
            xi = [[alphas[n - 1][i] * a[i][j] * e[j] * beta[j] for j in range(K)] for i in range(K)]
            norm = sum(map(sum, xi)) or 1e-300
            for i in range(K):
                for j in range(K):
                    counts[name][i][j] += xi[i][j] / norm
                    if name != DEFAULT:
                        counts[DEFAULT][i][j] += xi[i][j] / norm
            beta = _normalized([sum(a[i][j] * e[j] * beta[j] for j in range(K)) for i in range(K)])
    new = dict(params)
    for k in range(K):
        if weight[k] > 0:
            mean = total[k] / weight[k]
            new[param("mean", k)] = mean
            new[param("sd", k)] = max(math.sqrt(max(squares[k] / weight[k] - mean * mean, 0.0)), MIN_SD)
    for r in (LOW_IV, HIGH_IV):
        # Over the scale, a deviation floored as the chain floors it at the scale so far.
        floor = MIN_SD / chain.scales[r]
        second = [new[param("mean", k)] ** 2 + max(new[param("sd", k)], floor) ** 2 for k in range(K)]
        expected = _dot(occupancy[r], second)
        if minutes[r] > 0 and expected > 0:
            seen = math.sqrt(squared[r] / expected)
            new[param("iv_scale", r)] = (minutes[r] * seen + SCALE_PRIOR) / (minutes[r] + SCALE_PRIOR)
    untrained = _Chain(default_params()).transitions[DEFAULT]
    default = [
        _normalized([counts[DEFAULT][i][j] + _UNTRAINED * untrained[i][j] for j in range(K)])
        for i in range(K)
    ]
    for t in TRANSITIONS:
        rows = (
            default
            if t == DEFAULT
            else [_normalized([counts[t][i][j] + PRIOR * default[i][j] for j in range(K)]) for i in range(K)]
        )
        for i in range(K):
            for j in range(K):
                new[param(t, i, j)] = rows[i][j]
    return new, likelihood


def _in_order(params: Mapping[str, float]) -> dict[str, float]:
    """The states renumbered quiet, up, down, wild: the widest is wild, the narrowest of the
    rest quiet, and of the other two the higher mean is up."""
    sds = {k: params[param("sd", k)] for k in range(K)}
    wild = max(sds, key=lambda k: sds[k])
    quiet = min((k for k in sds if k != wild), key=lambda k: sds[k])
    up, down = sorted((k for k in sds if k not in (wild, quiet)), key=lambda k: -params[param("mean", k)])
    order = (quiet, up, down, wild)
    out = {k: v for k, v in params.items() if k.startswith(("iv_cut.", "iv_scale."))}
    for new, old in enumerate(order):
        out[param("mean", new)] = params[param("mean", old)]
        out[param("sd", new)] = params[param("sd", old)]
    for t in TRANSITIONS:
        for i, old_i in enumerate(order):
            for j, old_j in enumerate(order):
                out[param(t, i, j)] = params[param(t, old_i, old_j)]
    return out


def _stationary(a: Matrix) -> list[float]:
    p = [1.0 / K] * K
    for _ in range(200):
        nxt = _step(p, a)
        if max(abs(x - y) for x, y in zip(nxt, p, strict=True)) < 1e-12:
            return nxt
        p = nxt
    return p


def _step(p: Sequence[float], a: Matrix) -> list[float]:
    return [sum(p[i] * a[i][j] for i in range(K)) for j in range(K)]


def _dot(x: Sequence[float], y: Sequence[float]) -> float:
    return sum(a * b for a, b in zip(x, y, strict=True))


def _normalized(values: Sequence[float]) -> list[float]:
    total = sum(values)
    if total <= 0 or not math.isfinite(total):
        return [1.0 / K] * K
    return [v / total for v in values]
