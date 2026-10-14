"""Cards and meanings: what the Engine API tells the screen about each predictor.

- A predictor's card (``Card``): the scientist it is named after, its character, how it
  works, its own features, what it learns, how it turns that into the three probabilities
  and the market it tends to be wrong in. The algorithms, the baselines and the consensus
  each carry one.
- A name's meaning (``Meaning``): what a feature or a model parameter is, its unit and when
  it is known.

The text is in Korean, as the screen shows it. It stays in this private engine: the public
screen only shows what the API gives it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# The target the cards describe (#160, #170): the touch label, as a card names it.
TOUCH_LABEL = (
    "도달 정답(4분 안에 매수 1호가가 봉 마감 중간가 + k틱에 닿으면 상승, 아니고 매도 1호가가 − k틱에 "
    "닿으면 하락, 둘 다 아니면 보합)"
)


@dataclass(frozen=True)
class Card:
    """A predictor's card. Card text describes the current target, the touch label (#170,
    ``TOUCH_LABEL``): with ``KRX_TARGET_VERSION=1`` the predictors learn and forecast the
    four-minute move instead, which the cards do not tell."""

    scientist: str | None  # None: a baseline, named after no one
    character: str  # e.g. trend following
    logic: str
    unique_features: tuple[str, ...]
    training: str  # what is learned, and how
    forecasting: str  # how the three probabilities are made
    failure: str  # the market it tends to be wrong in

    def as_dict(self) -> dict[str, Any]:
        """As the Engine API shows it."""
        return {
            "scientist": self.scientist,
            "character": self.character,
            "logic": self.logic,
            "unique_features": list(self.unique_features),
            "training": self.training,
            "forecasting": self.forecasting,
            "failure": self.failure,
        }


@dataclass(frozen=True)
class Meaning:
    """What a feature or a model parameter is, in what unit, and when it is known."""

    text: str
    unit: str
    known: str  # e.g. at the bar close, decided before the open, learned overnight

    def as_dict(self) -> dict[str, str]:
        """As the Engine API shows it."""
        return {"meaning": self.text, "unit": self.unit, "known": self.known}


def learned(days: int) -> str:
    """When a parameter learned overnight on the last ``days`` trading days is known."""
    return (
        f"밤 학습이 최근 {days}거래일로 정한다. 새 모델은 다음 거래일 08:00 교체 때부터 쓴다. 학습 전에는 "
        "기본값이다."
    )


# The classes of a forecast, as the cards name them.
CLASS_NAMES = {"up": "상승", "flat": "보합", "down": "하락"}


def class_move_meanings(days: int) -> dict[str, Meaning]:
    """``move.<class>``: each class's mean move in training, which a classifier weighs by its
    probabilities into the expected move."""
    return {
        f"move.{c}": Meaning(
            f"학습 표본에서 정답이 {name}이었던 봉 마감의 평균 4분 변화. 세 확률로 가중해 기대수익을 낸다.",
            "틱",
            learned(days),
        )
        for c, name in CLASS_NAMES.items()
    }
