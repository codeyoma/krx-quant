"""Hurst-5d: ``hurst`` on the five sessions of ``open_to_close_5d`` (#698), as ``cox-5d`` is ``cox``
on that horizon.

The same rule and settings (``algorithms.hurst``): the lookback of 5, 20 and 60 days whose terciles
told the classes apart best, and the class shares of its tercile. What it learns differs: the
kind's answers, the direction of a window of five sessions, on samples whose windows have ended,
and the 80/20 split leaves the four days before the held-out part out of both (``daily_window``).
Its code is Hurst's beside its own (``code_from``): a change to Hurst's has it pre-validated again.
"""

from __future__ import annotations

from krx_quant.algorithms.daily_window import Window
from krx_quant.algorithms.hurst import LOOKBACKS, TRAINING_DAYS, Hurst
from krx_quant.cards import Card
from krx_quant.open_to_close_5d import OPEN_TO_CLOSE_5D, SESSIONS


class Hurst5d(Hurst):
    name = "hurst-5d"
    kind = OPEN_TO_CLOSE_5D.name
    answer_version = OPEN_TO_CLOSE_5D.answer_version
    code_from: tuple[type, ...] = (Hurst, Window)  # it fits and predicts with Hurst's code
    card = Card(
        scientist="Harold Edwin Hurst",
        character="모멘텀이냐 되돌림이냐, 한 기간의 규칙 (5거래일)",
        logic=(
            "그날 KRX 시가 단일가 → 다섯째 거래일 종가 단일가의 방향(상승·보합·하락)을 `hurst`와 같은 "
            "규칙으로 예측한다: 최근 5·20·60거래일 움직임 중 3분위로 방향을 가장 잘 가른 기간 하나를 "
            "고르고, 그 3분위에 든 학습일들의 클래스 비율을 낸다. 주 단위의 되돌림(Jegadeesh 1990, Lehmann "
            "1990)과 몇 달의 모멘텀(Jegadeesh & Titman 1993)이 이 지평에 더 가깝다."
        ),
        unique_features=LOOKBACKS,
        training=(
            f"`hurst`와 같다: 최근 {TRAINING_DAYS}거래일 세 종목을 모아 학습한다. 표본은 그 시점에 창이 "
            f"끝난 날만이고, 기간을 고르는 시간순 80/20 분할 사이에 {SESSIONS - 1}거래일을 비운다(앞 부분은 "
            "뒤 부분의 첫날 전에 끝난 창만)."
        ),
        forecasting="그날 값이 든 3분위의 클래스 비율이 세 확률이다. 학습 전에는 1/3씩이다.",
        failure="`hurst`와 같고, 5거래일 동안의 뉴스(실적, 일정)를 08:50의 입력이 모르는 만큼 더 틀린다.",
    )
