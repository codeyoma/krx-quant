"""Lorenz-5d: ``lorenz`` on the five sessions of ``open_to_close_5d`` (#698), as ``cox-5d`` is
``cox`` on that horizon.

The same analog forecast and settings (``algorithms.lorenz``): the class shares of the ``k`` training
days nearest the day's nine inputs. What it learns differs: the kind's answers (the direction of a
window of five sessions), on samples whose windows have ended, with the four days before the
held-out part of its 80/20 split left out of both (``daily_window``); through the kind, the inputs
over σ̂ (``oc_1``, ``cc_1``) read the five-session band. Its code is Lorenz's beside its own
(``code_from``): a change to Lorenz's has it pre-validated again.
"""

from __future__ import annotations

from krx_quant.algorithms.daily_window import Window
from krx_quant.algorithms.lorenz import OWN, TRAINING_DAYS, Lorenz
from krx_quant.cards import Card
from krx_quant.open_to_close_5d import OPEN_TO_CLOSE_5D, SESSIONS


class Lorenz5d(Lorenz):
    name = "lorenz-5d"
    kind = OPEN_TO_CLOSE_5D.name
    answer_version = OPEN_TO_CLOSE_5D.answer_version
    code_from: tuple[type, ...] = (Lorenz, Window)  # it fits and predicts with Lorenz's code
    card = Card(
        scientist="Edward Lorenz",
        character="오늘과 가장 닮은 지난날들 (5거래일)",
        logic=(
            "그날 KRX 시가 단일가 → 다섯째 거래일 종가 단일가의 방향(상승·보합·하락)을 `lorenz`와 같은 "
            "유사일 예보로 낸다: 08:50까지 아는 아홉 값(밤사이 SOX와 VIX, 전 거래일 움직임, 변동성 국면, "
            "5·20거래일 움직임, 고점 대비 하락, 범위)이 가장 가까운 지난날 k개의 5거래일 방향 비율이다. "
            "σ̂로 나누는 입력은 이 종류의 것(5세션 창)이다."
        ),
        unique_features=OWN,
        training=(
            f"`lorenz`와 같다: 최근 {TRAINING_DAYS}거래일 세 종목을 모은다. 지난날은 그 시점에 창이 끝난 "
            f"날만이고, k를 고르는 시간순 80/20 분할 사이에 {SESSIONS - 1}거래일을 비운다(앞 부분은 뒤 "
            "부분의 첫날 전에 끝난 창만)."
        ),
        forecasting=(
            "가장 가까운 학습일 k개의 클래스 수에 1씩 더해 k + 3으로 나눈 것이 세 확률이다. 학습 전에는 "
            "1/3씩이다."
        ),
        failure="`lorenz`와 같고, 5거래일 동안의 뉴스(실적, 일정)를 08:50의 입력이 모르는 만큼 더 틀린다.",
    )
