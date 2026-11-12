"""Friedman-5d: ``friedman`` on the five sessions of ``open_to_close_5d`` (#698), as ``cox-5d`` is
``cox`` on that horizon.

The same trees, inputs and settings (``algorithms.friedman``). What it learns differs: the kind's
answers (the direction of a window of five sessions), on samples whose windows have ended, with
the four days before the held-out part of its 80/20 split (where the rounds stop) left out of
both (``daily_window``); through the kind, the inputs over σ̂ and the class shares are the
five-session window's. Its code is Friedman's beside its own (``code_from``): a change to
Friedman's has it pre-validated again.
"""

from __future__ import annotations

from krx_quant.algorithms.daily_window import Window
from krx_quant.algorithms.friedman import MAX_ROUNDS, OWN, PATIENCE, TRAINING_DAYS, Friedman
from krx_quant.cards import Card
from krx_quant.open_to_close_5d import OPEN_TO_CLOSE_5D, SESSIONS


class Friedman5d(Friedman):
    name = "friedman-5d"
    kind = OPEN_TO_CLOSE_5D.name
    answer_version = OPEN_TO_CLOSE_5D.answer_version
    code_from: tuple[type, ...] = (Friedman, Window)  # it fits and predicts with Friedman's code
    card = Card(
        scientist="Jerome H. Friedman",
        character="모든 하루 입력과 그중 무엇이 중요한지 찾는 트리 (5거래일)",
        logic=(
            "그날 KRX 시가 단일가 → 다섯째 거래일 종가 단일가의 방향(상승·보합·하락)을 `friedman`과 같은 "
            "LightGBM 트리와 입력(하루 한 번 피처 모두와 최근 5거래일 외국인·기관 순매수)으로 분류한다. σ̂로 "
            "나누는 입력과 클래스 비율은 이 종류의 것(5세션 창, 끝난 창)이다."
        ),
        unique_features=OWN,
        training=(
            f"`friedman`과 같다: 최근 {TRAINING_DAYS}거래일 세 종목을 모아, 앞 80%로 키우며 뒤 20%의 log "
            f"loss가 {PATIENCE}번 동안 더 줄지 않은 곳(최대 {MAX_ROUNDS}번)을 횟수로 정한다. 표본은 그 "
            f"시점에 창이 끝난 날만이고, 분할 사이에 {SESSIONS - 1}거래일을 비운다(앞 부분은 뒤 부분의 첫날 "
            "전에 끝난 창만)."
        ),
        forecasting="트리의 세 확률이다(합이 1이 되게 나눈다). 학습 전에는 1/3씩이다.",
        failure="`friedman`과 같고, 5거래일 동안의 뉴스(실적, 일정)를 08:50의 입력이 모르는 만큼 더 틀린다.",
    )
