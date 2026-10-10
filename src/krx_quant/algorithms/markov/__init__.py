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
  transitions, learned from every day.
- **Own features**: the day's macro regime and the last hour of one-minute moves.
- **Learns**: Baum–Welch re-estimation over the last 20 trading days, each symbol's day a
  sequence of moves: the four states' emissions from every day together, each regime's
  transitions from its own days (pulled toward the default where it has few), and the
  default transitions from all days.

The option IV regime of the design is not used yet (the engine does not read the options
feed). Training measures moves on closes, which bounce between the bid and the ask, so the
wild state learned on backfilled days is a little wilder than live, on mid-points.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

from krx_quant.algorithms.contract import Card, Model, Sample
from krx_quant.algorithms.forecasts import UNKNOWN, lowest_log_loss, normal_forecast, with_outcomes
from krx_quant.baselines import Forecast
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


def param(kind: str, *index: int | str) -> str:
    return ".".join([kind, *map(str, index)])


def default_params() -> dict[str, float]:
    params = {param("mean", k): _MEANS[k] for k in range(K)} | {param("sd", k): _SDS[k] for k in range(K)}
    for t in TRANSITIONS:
        for i in range(K):
            for j in range(K):
                params[param(t, i, j)] = _STAY if i == j else (1 - _STAY) / (K - 1)
    return params


Matrix = list[list[float]]


class _Chain:
    """A model's emissions and transitions, read from its parameters."""

    def __init__(self, params: Mapping[str, float]) -> None:
        self.means = [params[param("mean", k)] for k in range(K)]
        self.sds = [max(params[param("sd", k)], MIN_SD) for k in range(K)]
        self.transitions = {
            t: [[params[param(t, i, j)] for j in range(K)] for i in range(K)] for t in TRANSITIONS
        }

    def emissions(self, move: float) -> tuple[list[float], float]:
        """Each state's density of ``move``, scaled so the largest is 1, and the log of the scale."""
        logs = [
            -0.5 * ((move - m) / s) ** 2 - math.log(s) - 0.5 * math.log(2 * math.pi)
            for m, s in zip(self.means, self.sds, strict=True)
        ]
        top = max(logs)
        return [math.exp(v - top) for v in logs], top

    def forward(
        self, moves: Sequence[float], a: Matrix
    ) -> tuple[list[list[float]], list[list[float]], float]:
        """Filter ``moves`` forward from the chain's long-run mix of states: how likely each
        state is after each move, each move's (scaled) emissions, and the log-likelihood."""
        alphas: list[list[float]] = []
        emitted: list[list[float]] = []
        likelihood = 0.0
        p = _stationary(a)
        for move in moves:
            e, top = self.emissions(move)
            raw = [q * e[j] for j, q in enumerate(p if not alphas else _step(alphas[-1], a))]
            c = sum(raw) or 1e-300
            alphas.append([v / c for v in raw])
            emitted.append(e)
            likelihood += math.log(c) + top
        return alphas, emitted, likelihood

    def now(self, moves: Sequence[float], a: Matrix) -> list[float]:
        """How likely each state is after ``moves``."""
        alphas, _, _ = self.forward(moves, a)
        return alphas[-1] if alphas else _stationary(a)

    def ahead(self, p: list[float], a: Matrix) -> tuple[float, float]:
        """The mean and variance of the next ``STEPS`` minutes' summed move from states ``p``."""
        mu, second = self.means, [s * s + m * m for m, s in zip(self.means, self.sds, strict=True)]
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
    features: tuple[str, ...] = ("macro_regime", "minute_ticks", "flat_ticks")
    training_days = 20
    card = Card(
        scientist="Andrey Markov",
        character="국면 탐정",
        logic=(
            "1분 종가 변화(틱)를 조용함·상승추세·하락추세·급변동 4국면의 은닉 마르코프 모델로 본다. "
            "최근 1시간의 움직임으로 지금 국면의 확률을 걸러 내고, 그날 매크로 국면의 전이행렬로 "
            "4분 뒤까지 국면이 옮겨 가는 것을 따라가 4분 움직임의 분포를 낸다. 매크로 국면이 없는 날은 "
            "기본 전이행렬을 쓴다."
        ),
        unique_features=("macro_regime", "minute_ticks"),
        training=(
            "최근 20거래일의 종목·날짜별 1분 움직임으로 Baum–Welch 재추정을 한다. 국면별 분포는 모든 날로, "
            "전이행렬은 매크로 국면마다 그 국면의 날로(날이 적으면 기본 행렬 쪽으로 당긴다), 기본 행렬은 "
            "모든 날로 배운다."
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
        mean, variance = chain.ahead(chain.now(features.series("minute_ticks"), a), a)
        return normal_forecast(mean, math.sqrt(variance), flat)

    def fit(self, samples: Sequence[Sample]) -> Model:
        scored = with_outcomes(samples)
        days = _sequences(samples)
        params = default_params()
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


def _transitions_of(code: float | None) -> str:
    """The transitions of a macro regime's code; the default without one."""
    if code is None:
        return DEFAULT
    return next((r for r, c in REGIME_CODES.items() if c == code), DEFAULT)


def _sequences(samples: Sequence[Sample]) -> list[tuple[str, list[float]]]:
    """Each symbol's day as its transitions' name and its one-minute moves, in time order:
    the first sample's hour, then each later one's newest minutes."""
    days: dict[tuple[str, object], list[Sample]] = {}
    for s in samples:
        days.setdefault((s.symbol, s.bar_close.date()), []).append(s)
    out = []
    for day in days.values():
        day.sort(key=lambda s: s.bar_close)
        moves: list[float] = []
        previous = None
        for s in day:
            hour = s.features.series("minute_ticks")
            new = len(hour) if previous is None else min(len(hour), int((s.bar_close - previous) / BAR))
            moves += hour[len(hour) - new :]
            previous = s.bar_close
        if len(moves) >= 2:
            out.append((_transitions_of(day[0].features["macro_regime"]), moves))
    return out


def _reestimate(
    params: Mapping[str, float], days: Sequence[tuple[str, list[float]]]
) -> tuple[dict[str, float], float]:
    """One Baum–Welch round: the re-estimated parameters, and the log-likelihood before it."""
    chain = _Chain(params)
    weight = [0.0] * K
    total = [0.0] * K
    squares = [0.0] * K
    counts = {t: [[0.0] * K for _ in range(K)] for t in TRANSITIONS}
    likelihood = 0.0
    for name, moves in days:
        a = chain.transitions[name]
        alphas, emitted, day_likelihood = chain.forward(moves, a)
        likelihood += day_likelihood
        beta = [1.0] * K
        for n in range(len(moves) - 1, -1, -1):
            gamma = _normalized([alphas[n][k] * beta[k] for k in range(K)])
            for k in range(K):
                weight[k] += gamma[k]
                total[k] += gamma[k] * moves[n]
                squares[k] += gamma[k] * moves[n] ** 2
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
    out = {}
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
