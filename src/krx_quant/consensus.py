"""Condorcet, the consensus: the active algorithms' probabilities, pooled by weight.

At each bar close two consensus predictions are made, each stored and scored like an
algorithm's:

- **fast** (``consensus-fast``): from the active algorithms finished 2 seconds after they
  started, just after the bar close;
- **final** (``consensus-final``): once every active algorithm has finished, failed or
  run out of its time budget (``KRX_ALGORITHM_SECONDS``, 10 seconds by default).

The pool is linear: each probability (and the expected move) is the weighted mean of the
members', the weights of those present scaled to add up to 1. Algorithms on probation,
on the bench, retired or clones are never members. With no member, the consensus is
recorded empty (``no_members``); with members but no forecast from any by then (still
working, timed out or failed), ``none_finished``.

**Weights**, per symbol and 30-minute slot, are refitted after each day's close:

1. For the last 5 and the last 20 days with predictions, the weights whose pool would
   have had the lowest log loss on the bar closes every member predicted, found by EM.
   Scored on the bar close's touch label (#170), every member's alike; special predictions
   are left out, and so are a member's predictions by a model not trained on the touch
   target (``stats.on_target``): its old target's or its defaults'.
2. Only a weight steady across both spans moves: above equal in both, it takes the
   smaller; below in both, the larger; otherwise equal. What the steady losers give up
   goes to the steady winners (or, with none, to the rest alike), so they add up to 1.
   An algorithm good only lately gains nothing.
3. Shrunk toward equal weights by the evidence: with n bar closes in the 20 days, the
   fitted weights count n / (n + 200).

Before the first fit the members count equally; a member the fit did not know gets an
equal share, the others the rest as fitted.

The fast consensus waits 2 seconds from when the algorithms start working, just after the
bar close; an engine running late still gives them that long.

Each of the two has a card like an algorithm's (``cards``, with the engine's own fast
cutoff); the members and the weights now are ``/v1/consensus``'s.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from krx_quant.baselines import Forecast
from krx_quant.cards import Card
from krx_quant.feeds.base import KST
from krx_quant.stats import on_target, scored_by_algorithm
from krx_quant.touch import TARGET_VERSION

FAST = "consensus-fast"
FINAL = "consensus-final"
NAMES = (FAST, FINAL)
EQUAL_VERSION = "condorcet-equal"  # before any fit
SPANS = (5, 20)  # days with predictions
SHRINK = 200  # bar closes' worth of belief in equal weights
_EM_ROUNDS = 200
_EM_TOLERANCE = 1e-9
_FLOOR = 1e-6  # the smallest probability counted, as in training

Cell = tuple[str, str]  # (symbol, slot)


@dataclass(frozen=True)
class Timing:
    fast_seconds: float = 2.0  # after the algorithms start, just after the bar close


def cards(timing: Timing | None = None) -> dict[str, Card]:
    """The two consensus predictions' cards: who is in, how the weights are set (per symbol
    and 30-minute slot), and how they make the three probabilities; the fast one's cutoff as
    ``timing`` sets it (the engine's)."""
    fast = f"{(timing or Timing()).fast_seconds:g}"
    members = (
        "구성원은 정식(active) 알고리즘뿐이다. 수습·벤치·은퇴·복제는 빠진다. 그 시점에 결과가 있는 구성원의 "
        "가중치만 합이 1이 되게 다시 나눈다."
    )
    training = (
        "가중치는 종목 × 30분 시간대(봉 마감 09:01–09:30이 09:00 칸) 칸마다 매일 장 마감 뒤 상태 점검 직후 "
        f"다시 정한다. 최근 {SPANS[0]}일과 {SPANS[1]}일(예측이 있는 날) 각각에서 모든 구성원이 예측한 봉 "
        "마감에 대해 합의의 로그손실이 가장 작은 가중치를 EM으로 구한다. 정답은 그 봉 마감의 도달 정답(4분 "
        "안에 매수 1호가가 중간가 + k틱에 닿으면 상승, 아니고 매도 1호가가 − k틱에 닿으면 하락, 아니면 "
        "보합)이고, 도달 정답으로 학습한 모델의 예측만 쓴다(특수 예측 제외). 두 "
        "기간 모두 균등(1/구성원 수)보다 높아야 올리고(둘 중 작은 값), 두 기간 모두 낮아야 내린다(둘 중 큰 "
        f"값). 엇갈리면 균등이다. 그다음 {SPANS[1]}일의 봉 마감 수 n에 n ÷ (n + {SHRINK})만 믿고 나머지는 "
        "균등 쪽으로 당긴다. 계산 전이나 가중치가 없는 칸은 균등이고, 계산 뒤에 정식이 된 알고리즘은 균등한 "
        "몫을 받는다."
    )
    forecasting = (
        "구성원 세 확률의 가중 평균(선형 결합)이 그대로 세 확률이다. 기대수익도 가중 평균이다. 가중치는 그 "
        "종목과 봉 마감이 든 30분 시간대 칸의 것이다."
    )
    shared_failure = (
        "구성원이 같은 쪽으로 함께 틀리는 장에서는 합쳐도 틀린다. 가중치는 전날까지의 성적이라 장세가 바뀐 "
        "날에는 늦게 따라간다. 정식 알고리즘이 하나도 없으면(모두 수습일 때, 정답이 바뀐 뒤 첫 판정 전까지) "
        "예측이 비어 있다(no_members)."
    )
    return {
        FAST: Card(
            scientist="Nicolas de Condorcet",
            character="합의(빠른)",
            logic=(
                "봉 마감마다 정식 알고리즘의 확률을 가중 평균한다. 빠른 합의는 알고리즘 계산을 시작하고(봉 "
                f"마감 직후) {fast}초 안에 끝난 구성원만 쓴다. {members}"
            ),
            unique_features=(),
            training=training,
            forecasting=f"{forecasting} {fast}초 안에 끝난 구성원이 없으면 비어 있다(none_finished).",
            failure=(
                f"{shared_failure} {fast}초 안에 끝나지 않은 구성원은 빠져서, 최종 합의보다 적은 구성원으로 "
                "낸다."
            ),
        ),
        FINAL: Card(
            scientist="Nicolas de Condorcet",
            character="합의(최종)",
            logic=(
                "봉 마감마다 정식 알고리즘의 확률을 가중 평균한다. 최종 합의는 구성원이 모두 끝나거나, "
                f"실패하거나, 각자의 시간 예산(기본 10초)에 걸리면 낸다. {members}"
            ),
            unique_features=(),
            training=training,
            forecasting=f"{forecasting} 결과가 있는 구성원이 하나도 없으면 비어 있다(none_finished).",
            failure=(
                f"{shared_failure} 시간 예산에 걸리거나 실패한 구성원은 빠지고, 늦게 나오는 만큼 그 사이 "
                "목표에 이미 닿아(표시 전 닿음) 따라 하기 어려울 수 있다."
            ),
        ),
    }


CARDS = cards()  # with the default timing


@dataclass(frozen=True)
class Weights:
    version: str
    computed_at: datetime | None
    cells: Mapping[Cell, Mapping[str, float]] = field(default_factory=dict)

    def of(self, symbol: str, bar_close: datetime, present: Sequence[str]) -> dict[str, float]:
        """The weights of the ``present`` members at ``symbol``'s bar close, adding up to 1.

        A member the fit did not know gets an equal share; the known ones share the rest as
        fitted.
        """
        fitted = self.cells.get((symbol, slot(bar_close)), {})
        equal = 1 / len(present)
        known = {name: fitted[name] for name in present if name in fitted}
        total = sum(known.values())
        if total <= 0:
            return dict.fromkeys(present, equal)
        rest = 1 - equal * (len(present) - len(known))
        return {name: rest * known[name] / total if name in known else equal for name in present}


NO_WEIGHTS = Weights(EQUAL_VERSION, None)


def slot(bar_close: datetime) -> str:
    """The 30-minute slot of the bar a prediction was made at the close of: ``"09:00"`` holds
    the bar closes 09:01–09:30.
    """
    start = (bar_close - timedelta(minutes=1)).astimezone(KST)
    return f"{start.hour:02d}:{start.minute - start.minute % 30:02d}"


def pool(forecasts: Mapping[str, Forecast], weights: Mapping[str, float]) -> Forecast:
    """The weighted mean of ``forecasts`` (whose ``weights`` add up to 1)."""
    return Forecast(
        p_up=sum(weights[n] * f.p_up for n, f in forecasts.items()),
        p_flat=sum(weights[n] * f.p_flat for n, f in forecasts.items()),
        p_down=sum(weights[n] * f.p_down for n, f in forecasts.items()),
        expected_ticks=sum(weights[n] * f.expected_ticks for n, f in forecasts.items()),
    )


def fit(
    rows: Sequence[Mapping[str, Any]], members: Sequence[str], days: Sequence[date], at: datetime
) -> Weights:
    """Weights for ``members`` from stored scored predictions (``rows``) of ``days`` (latest
    first, the longest span): only those of models trained on the touch target (#170).
    """
    version = f"condorcet-{at.astimezone(KST):%Y%m%d}"
    scored = scored_by_algorithm(on_target(rows, (), TARGET_VERSION))
    # Each bar close's probability of what happened, by member, in each cell.
    seen: dict[Cell, dict[datetime, dict[str, float]]] = {}
    for name in members:
        for r in scored.get(name, []):
            by_close = seen.setdefault((r.symbol, slot(r.bar_close)), {})
            by_close.setdefault(r.bar_close, {})[name] = max(r.probability(r.outcome), _FLOOR)
    spans = [set(days[:n]) for n in SPANS]
    equal = 1 / len(members) if members else 0.0
    cells: dict[Cell, dict[str, float]] = {}
    for cell, closes in seen.items():
        complete = [(close, probs) for close, probs in closes.items() if len(probs) == len(members)]
        if not complete:
            continue
        fitted = [
            _em(
                [[probs[m] for m in members] for close, probs in complete if _day(close) in span],
                len(members),
            )
            for span in spans
        ]
        kept = _steady([[w[i] for w in fitted] for i in range(len(members))], equal)
        trust = len(complete) / (len(complete) + SHRINK)
        cells[cell] = {m: trust * k + (1 - trust) * equal for m, k in zip(members, kept, strict=True)}
    return Weights(version, at, cells)


def _day(at: datetime) -> date:
    return at.astimezone(KST).date()


def _steady(by_member: Sequence[Sequence[float]], equal: float) -> list[float]:
    """The members' weights from their weights over the spans, adding up to 1.

    A weight is above equal only if above it in every span (then the least), below only if
    below in every span (the most), else equal. What the steady losers give up goes to the
    steady winners only, in proportion; with none, to everyone not losing, alike.
    """
    kept = []
    for weights in by_member:
        if all(w > equal for w in weights):
            kept.append(min(weights))
        elif all(w < equal for w in weights):
            kept.append(max(weights))
        else:
            kept.append(equal)
    gap = 1 - sum(kept)
    winners = [i for i, k in enumerate(kept) if k > equal]
    if winners:
        above = sum(kept[i] - equal for i in winners)
        for i in winners:
            kept[i] += gap * (kept[i] - equal) / above
    else:
        level = [i for i, k in enumerate(kept) if k >= equal]
        for i in level:
            kept[i] += gap / len(level)
    return kept


def _em(observations: Sequence[Sequence[float]], k: int) -> list[float]:
    """Mixture weights maximizing the pooled likelihood of ``observations`` (each the members'
    probabilities of what happened); equal without any.
    """
    weights = [1 / k] * k
    if not observations:
        return weights
    for _ in range(_EM_ROUNDS):
        shares = [0.0] * k
        for probs in observations:
            mixed = sum(w * p for w, p in zip(weights, probs, strict=True))
            for i in range(k):
                shares[i] += weights[i] * probs[i] / mixed
        updated = [s / len(observations) for s in shares]
        moved = max(abs(a - b) for a, b in zip(updated, weights, strict=True))
        weights = updated
        if moved < _EM_TOLERANCE:
            break
    return weights
