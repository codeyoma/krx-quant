"""Breiman on the scalp (#706): the barrier's trees (``algorithms/breiman/``) trained on the scalp's
answers (``scalp.py``: a long bought at the ask at a bar close reaches +0.2 % net, or falls 0.3 %, first,
within 15 minutes), so the mock scalp policy (#705) reads a forecast of its own trade.

Personality card: Breiman's (Leo Breiman's trees, López de Prado's pairing of them with the triple
barrier). The same features, own features (``barrier_width``, ``barrier_sigma``: how far the scalp's
fixed levels lie in the symbol's own volatility), boosting and settings as ``breiman``; its trees'
labels are the scalp's classes in their order (target first, timeout, stop first), over its last 20
trading days of answers. Days with one-minute bars only have no scalp answer (no ask at the bar close),
so it learns from the recorded days alone. Without a reference price at the bar close it does not
predict (``no_book``), as ``breiman``.

Its code is Breiman's beside its own (``code_from``): a change to Breiman's asks it for a new
pre-validation too. It is a package of its own so that adding it left Breiman's code, and so the
digests the four barrier Breimans were judged on, as they were.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace

import lightgbm as lgb
import numpy as np

from krx_quant.algorithms.breiman import BOOSTING, ROUNDS, TRAINING_DAYS, Breiman
from krx_quant.algorithms.contract import KindSample, Model, NothingToLearn
from krx_quant.algorithms.forecasts import LOSS_FLOOR, number
from krx_quant.cards import Meaning, learned
from krx_quant.scalp import SCALP

NAME = "breiman-scalp"
CLASSES = SCALP.classes  # the trees' labels 0, 1, 2: target_first, timeout, stop_first


def _share(label: str) -> str:
    return f"share.{label}"


# Its trees are text (the model's blob), not parameters: the API does not show them.
PARAM_MEANINGS: Mapping[str, Meaning] = {
    "rounds": Meaning(
        f"부스팅 횟수(학습 설정, 늘 {ROUNDS}). 0이면 학습 전이라 트리가 없다.",
        "번",
        "밤 학습이 모델을 만들 때 적는다.",
    ),
    **{
        _share(label): Meaning(
            f"학습 표본(클래스가 있는 스캘프 정답) 중 정답이 {title}이었던 비율. 학습 구간의 기본 비율을 "
            "보여 주는 값이고, 예측은 트리만으로 낸다. 학습 전에는 0이다.",
            "비율",
            learned(TRAINING_DAYS),
        )
        for label, title in zip(CLASSES, SCALP.class_titles, strict=True)
    },
}


class BreimanScalp(Breiman):
    """Breiman's trees on the scalp's answers (module doc)."""

    code_from: tuple[type, ...] = (Breiman,)  # it predicts with Breiman's code
    param_meanings = PARAM_MEANINGS
    card = replace(
        Breiman.card,
        character=f"트리 앙상블 ({SCALP.title})",
        logic=(
            "LightGBM 그래디언트 부스팅 트리로 봉 마감에 매도 호가로 산 롱이 15분 안에 수수료·거래세 뒤 순 "
            "+0.2% 목표가에 먼저 닿을지, 0.3% 내린 손절가에 먼저 닿을지, 둘 다 아닐지(목표 먼저·시간 만료·"
            "손절 먼저, 스캘프 정답)를 분류한다. 장벽의 breiman과 같은 피처를 읽는다: Turing이 읽는 피처 "
            "저장소의 숫자 피처(프로그램매매·외국계 회원사 흐름, 호가의 주문 흐름, 체결의 방향과 도착, 옵션 "
            "내재변동성, 시간대, 최우선 호가, 앞선 거래일들의 같은 10분 시간대 수익률 포함)에 그 봉 마감의 "
            "4분 장벽 폭 w(`barrier_width`)와 폭 ÷ 4분 변동성(`barrier_sigma`)을 더한다. 목표와 손절은 "
            "가격의 비율로 정해져 있어, 그 종목의 변동성에 비해 얼마나 먼지를 폭이 말해 준다. 모르는 피처는 "
            "비워 두고 트리가 다룬다."
        ),
        training=(
            "최근 20거래일의 스캘프 정답(녹화된 날의 호가로 정한다. 1분봉만 있는 날에는 봉 마감의 매도 "
            "호가를 몰라 정답이 없다)으로 LightGBM을 breiman과 같은 설정으로 200번 부스팅한다(학습률 0.05, "
            "잎 15개, 잎당 최소 표본 50, 피처·표본 80%, L2 1). 클래스가 없는 정답(채점할 수 없는 창, 매도 "
            "호가가 없던 봉 마감, 같은 받은 시각에 양쪽이 닿은 창)과 VI 중 봉 마감, 특수 상황 창은 뺀다. "
            "한 스레드와 고정 시드라 같은 표본이면 같은 트리가 나온다."
        ),
        forecasting=(
            "스캘프 정답으로 직접 학습한 LightGBM 다중 분류 트리가 목표 먼저·시간 만료·손절 먼저의 세 확률을 "
            "낸다(합이 1이 되게 나눈다). 모르는 피처는 비워 두고, 트리가 학습에서 정한 쪽으로 보낸다. 학습 "
            "전(트리 없음)에는 1/3씩이다. 봉 마감에 기준가가 없으면 예측하지 않는다(no_book)."
        ),
        failure=(
            "학습한 20거래일과 다른 장세(국면 전환, 큰 이벤트가 있는 날)에서 틀리기 쉽다. 트리는 학습에서 본 "
            "값의 범위 밖을 외삽하지 못한다. 목표(+0.43% 총)가 손절(−0.3%)보다 멀어 목표 먼저는 드문 "
            "클래스이고, 표본이 적은 종목·시간대에서는 그 확률이 거칠다."
        ),
    )

    def __init__(self) -> None:
        self.kind = SCALP.name
        self.name = NAME

    def default_model(self) -> Model:
        """No trees before training: a third each."""
        return Model(f"{self.name}-default", {"rounds": 0.0} | {_share(c): 0.0 for c in CLASSES})

    def fit(self, samples: Sequence[KindSample]) -> Model:
        """The trees of the samples with a scalp class (NothingToLearn without any)."""
        answered = [s for s in samples if s.label is not None]
        if not answered:
            raise NothingToLearn("no scalp answers to learn from")
        x = np.array([[number(s.features[n]) for n in self.features] for s in answered], dtype=float)
        y = np.array([CLASSES.index(s.label or "") for s in answered])  # all have one
        data = lgb.Dataset(x, y, feature_name=list(self.features), free_raw_data=True)
        booster = lgb.train(BOOSTING, data, num_boost_round=ROUNDS)
        probabilities = np.asarray(booster.predict(x, num_threads=1), dtype=float)
        loss = -float(np.mean(np.log(np.maximum(probabilities[np.arange(len(y)), y], LOSS_FLOOR))))
        shares = {_share(c): float(np.mean(y == i)) for i, c in enumerate(CLASSES)}
        params = {"rounds": float(ROUNDS)} | shares
        return Model.fitted(self.name, params, answered, loss, blob=booster.model_to_string())
