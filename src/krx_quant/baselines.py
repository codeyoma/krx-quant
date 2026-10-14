"""Baseline predictions: the simple rules every algorithm has to beat (lift).

- always flat;
- the last four minutes' direction goes on;
- a random class, drawn in the proportions of the day's classes so far.

Each predicts one class with certainty. The random one draws from a generator seeded by
the symbol and bar close, so the same input always gives the same prediction. Each has a
card like an algorithm's (``cards.Card``), named after no scientist.

With the touch target (``KRX_TARGET_VERSION=2``, #169, the default since #170,
``baselines_for``) the last four minutes' direction is the touch label of the window that
just ended (none when it could not be scored: the baseline does not predict, status
``no_previous``), the random class is drawn in the proportions of the day's touch labels,
and a fourth baseline calls each symbol and 30-minute slot's most frequent label over the
training days (the climate, ``climate.py``). Their versions go up from ``v1``; always flat
keeps its rule and version, with its card of the touch target (#170).
"""

from __future__ import annotations

import random
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from krx_quant.cards import Card
from krx_quant.pricing import Direction, classify

# The status of a baseline's prediction it did not make (#169): the last four minutes' touch
# label could not be scored (no quotes followed through them, a restart, the first minutes).
NO_PREVIOUS = "no_previous"


@dataclass(frozen=True)
class Forecast:
    p_up: float
    p_flat: float
    p_down: float
    expected_ticks: float

    @classmethod
    def certain(cls, direction: Direction, expected_ticks: float = 0.0) -> Forecast:
        return cls(
            p_up=1.0 if direction is Direction.UP else 0.0,
            p_flat=1.0 if direction is Direction.FLAT else 0.0,
            p_down=1.0 if direction is Direction.DOWN else 0.0,
            expected_ticks=expected_ticks,
        )

    def probability(self, direction: Direction) -> float:
        if direction is Direction.UP:
            return self.p_up
        return self.p_flat if direction is Direction.FLAT else self.p_down


@dataclass(frozen=True)
class Inputs:
    """What a baseline knows at a bar close."""

    symbol: str
    bar_close: datetime
    mid: float | None  # the reference price at the bar close
    mid_before: float | None  # four minutes earlier
    flat_ticks: int | None
    tick: int | None
    # Classes (by bar close) of the symbol's predictions already scored today; with the touch
    # target, of the windows already over today.
    day_classes: Mapping[Direction, int]
    # With the touch target (#169): the label of the window that ended at this bar close
    # (none: it could not be scored), and the climate table's direction here (none: unknown).
    previous: Direction | None = None
    climate: Direction | None = None


class Baseline(Protocol):
    name: str
    version: str
    card: Card

    def predict(self, inputs: Inputs) -> Forecast | None:
        """The forecast; None where it does not predict (status ``NO_PREVIOUS``)."""
        ...


_NOT_LEARNED = "배우지 않는다. 모든 알고리즘이 넘어야 하는 기준이라 규칙이 고정되어 있다."


class AlwaysFlat:
    name = "baseline-flat"
    version = "v1"
    card = Card(
        scientist=None,
        character="기준 예측: 늘 보합",
        logic=(
            "어느 봉 마감에서나 4분 뒤 중간가 변화가 보합 범위(k틱) 안에 머문다고 본다. 아무 정보도 쓰지 "
            "않는다."
        ),
        unique_features=(),
        training=_NOT_LEARNED,
        forecasting="보합에 확률 1, 상승과 하락에 0을 준다. 기대수익은 0틱이다.",
        failure=(
            "4분 변화가 보합 범위를 넘을 때마다 틀린다: 변동이 큰 장, 개장 직후, 호가단위에 비해 많이 "
            "움직이는 종목. 조용한 장에서는 맞힌 비율이 높아 넘기 어려운 기준이 된다."
        ),
    )

    def predict(self, inputs: Inputs) -> Forecast:
        return Forecast.certain(Direction.FLAT)


class Persistence:
    """The move of the last four minutes, classified like an outcome, continues."""

    name = "baseline-persist"
    version = "v1"
    card = Card(
        scientist=None,
        character="기준 예측: 직전 4분 방향",
        logic=(
            "직전 4분(t − 4분 → t)의 중간가 변화를 채점과 같은 규칙(보합 범위 k틱)으로 상승·보합·하락으로 "
            "나누고, 그 방향이 다음 4분에도 이어진다고 본다. 중간가를 모르면 보합이다."
        ),
        unique_features=(),
        training=_NOT_LEARNED,
        forecasting="그 방향에 확률 1, 나머지 둘에 0을 준다. 기대수익은 직전 4분 변화의 틱 수다.",
        failure=(
            "방향이 자주 바뀌는 장과 되돌림(직전 4분의 움직임이 되밀릴 때)에서 틀린다. 추세가 이어지거나 "
            "조용한 장에서는 맞기 쉽다."
        ),
    )

    def predict(self, inputs: Inputs) -> Forecast:
        if (
            inputs.mid is None
            or inputs.mid_before is None
            or inputs.flat_ticks is None
            or inputs.tick is None
        ):
            return Forecast.certain(Direction.FLAT)
        change = inputs.mid - inputs.mid_before
        return Forecast.certain(classify(change, inputs.flat_ticks, inputs.tick), change / inputs.tick)


class ClassRatioRandom:
    """A class drawn in the day's proportions so far, each class counted once more."""

    name = "baseline-random"
    version = "v1"
    card = Card(
        scientist=None,
        character="기준 예측: 비율대로 무작위",
        logic=(
            "그날 그 종목에서 이미 채점된 봉 마감의 클래스 비율(클래스마다 1을 더한다)대로 상승·보합·하락 중 "
            "하나를 뽑는다. 종목과 봉 마감으로 시드를 정해서 같은 입력이면 늘 같은 값이 나온다."
        ),
        unique_features=(),
        training="배우지 않는다. 그날 채점된 클래스 수만 센다(그날 첫 채점 전에는 셋이 똑같다).",
        forecasting="뽑은 클래스에 확률 1, 나머지 둘에 0을 준다. 기대수익은 0틱이다.",
        failure=(
            "정보 없이 뽑으므로 늘 운이다: 그날 비율이 이어지면 맞힌 비율이 대략 클래스 비율의 제곱합이다. "
            "장 초반에는 셀 봉이 적어 거의 고르게 뽑고, 장중에 클래스 비율이 바뀌면 지난 비율로 뽑는다."
        ),
    )

    def predict(self, inputs: Inputs) -> Forecast:
        rng = random.Random(f"{self.name}:{inputs.symbol}:{inputs.bar_close.isoformat()}")
        classes = list(Direction)
        weights = [inputs.day_classes.get(d, 0) + 1 for d in classes]
        return Forecast.certain(rng.choices(classes, weights)[0])


class TouchFlat(AlwaysFlat):
    """Always flat, as the touch target names it (#170): the same rule, its card of that target."""

    card = Card(
        scientist=None,
        character="기준 예측: 늘 보합",
        logic=(
            "어느 봉 마감에서나 4분 안에 매수 1호가가 위 목표(봉 마감 중간가 + k틱)에도, 매도 1호가가 아래 "
            "목표(− k틱)에도 닿지 않는다고 본다. 아무 정보도 쓰지 않는다."
        ),
        unique_features=(),
        training=_NOT_LEARNED,
        forecasting="보합에 확률 1, 상승과 하락에 0을 준다. 기대수익은 0틱이다.",
        failure=(
            "4분 안에 호가가 어느 목표에든 닿을 때마다 틀린다: 변동이 큰 장, 개장 직후, 호가단위에 비해 "
            "많이 움직이는 종목, 스프레드가 넓은 때. 조용한 종목과 시간대에서는 맞힌 비율이 높아 넘기 어려운 "
            "기준이 된다."
        ),
    )


class TouchPersistence:
    """The touch label of the four minutes that just ended continues (#169)."""

    name = "baseline-persist"
    version = "v2"
    card = Card(
        scientist=None,
        character="기준 예측: 직전 4분 방향",
        logic=(
            "직전 4분(t − 4분 → t)의 도달 정답을 그대로 다음 4분의 방향으로 본다. 그 4분 동안 매수 1호가가 "
            "t − 4분 중간가 + k틱에 닿은 적이 있으면 상승, 아니고 매도 1호가가 − k틱에 닿았으면 하락, 둘 다 "
            "아니면 보합이다. 저장된 예측이 아니라 시장 보기가 따라간 호가로 정한다(그 봉 마감에 예측이 "
            "없었어도 된다). 그 4분을 채점할 수 없으면(호가 끊김, REST 대체 호가, 재시작, 그날 처음 4분) "
            "예측하지 않는다."
        ),
        unique_features=(),
        training=_NOT_LEARNED,
        forecasting=(
            "그 방향에 확률 1, 나머지 둘에 0을 준다. 기대수익은 직전 4분 중간가 변화의 틱 수다(모르면 0). "
            "예측하지 않은 봉 마감은 상태가 no_previous다."
        ),
        failure=(
            "방향이 자주 바뀌는 장과 되돌림(목표에 닿은 뒤 되밀릴 때)에서 틀린다. 추세가 이어지거나 조용한 "
            "장에서는 맞기 쉽다. 위·아래 둘 다 닿은 4분은 상승으로 이어 간다."
        ),
    )

    def predict(self, inputs: Inputs) -> Forecast | None:
        if inputs.previous is None:
            return None
        expected = 0.0
        if inputs.mid is not None and inputs.mid_before is not None and inputs.tick:
            expected = (inputs.mid - inputs.mid_before) / inputs.tick
        return Forecast.certain(inputs.previous, expected)


class TouchRatioRandom(ClassRatioRandom):
    """A class drawn in the proportions of the day's touch labels so far (#169)."""

    version = "v2"
    card = Card(
        scientist=None,
        character="기준 예측: 비율대로 무작위",
        logic=(
            "그날 그 종목에서 4분이 다 지난 창(봉 마감 + 4분 ≤ t)의 도달 정답 비율(클래스마다 1을 "
            "더한다)대로 상승·보합·하락 중 하나를 뽑는다. 예측하지 않은 봉 마감(VI)의 창도 센다. 종목과 "
            "봉 마감으로 시드를 정해서 같은 입력이면 늘 같은 값이 나온다."
        ),
        unique_features=(),
        training="배우지 않는다. 그날 끝난 창의 도달 정답 수만 센다(그날 첫 창이 끝나기 전에는 셋이 똑같다).",
        forecasting="뽑은 클래스에 확률 1, 나머지 둘에 0을 준다. 기대수익은 0틱이다.",
        failure=(
            "정보 없이 뽑으므로 늘 운이다: 그날 비율이 이어지면 맞힌 비율이 대략 클래스 비율의 제곱합이다. "
            "장 초반에는 셀 창이 적어 거의 고르게 뽑고, 장중에 클래스 비율이 바뀌면 지난 비율로 뽑는다."
        ),
    )


class Climate:
    """Each symbol and 30-minute slot's most frequent touch label in training (#169)."""

    name = "baseline-climate"
    version = "v1"  # its predictions carry the table's version instead (``climate.py``)
    card = Card(
        scientist=None,
        character="기준 예측: 시간대 기후",
        logic=(
            "종목 × 30분 시간대(봉 마감 09:01–09:30은 09:00 칸)마다, 최근 20거래일 학습 창의 도달 "
            "정답(녹화일은 호가, 그 전 날은 1분봉 규칙)에서 가장 많이 나온 방향을 그 칸의 모든 봉 마감에 "
            "낸다. 시간대 말고는 아무 정보도 쓰지 않는다. 시간대와 변동성만 맞혀서 이기는 모델을 거르는 "
            "기준이다."
        ),
        unique_features=(),
        training=(
            "밤 학습이 매일 최근 20거래일(그날까지)의 도달 정답으로 칸마다 방향을 센다. 동률이면 보합, "
            "그다음 상승을 고른다. 성적 비교 없이 늘 새 표로 바꾸고, 다음 거래일 08:00 교체 때부터 쓴다. "
            "학습 전에는 모든 칸이 보합이다."
        ),
        forecasting=(
            "그 칸의 방향에 확률 1, 나머지 둘에 0을 준다. 기대수익은 0틱이다. 예측의 모델 버전은 그 표의 "
            "버전이다(`baseline-climate-<날짜>-<해시>`, 학습 전에는 `baseline-climate-default`)."
        ),
        failure=(
            "그날 장세가 학습 창의 시간대 버릇과 다를 때 틀린다: 장 초반 변동이 평소보다 작거나 큰 날, "
            "뉴스로 한쪽으로 쏠린 날, 학습 창이 한 방향으로 크게 움직인 기간이었을 때."
        ),
    )

    def predict(self, inputs: Inputs) -> Forecast:
        return Forecast.certain(inputs.climate or Direction.FLAT)


BASELINES: tuple[Baseline, ...] = (AlwaysFlat(), Persistence(), ClassRatioRandom())
BASELINE_NAMES = frozenset(b.name for b in BASELINES)
# With the touch target (target version 2, #169).
TOUCH_BASELINES: tuple[Baseline, ...] = (TouchFlat(), TouchPersistence(), TouchRatioRandom(), Climate())


def baselines_for(target_version: int) -> tuple[Baseline, ...]:
    """The baselines of ``target_version`` (``KRX_TARGET_VERSION``)."""
    return TOUCH_BASELINES if target_version >= 2 else BASELINES
