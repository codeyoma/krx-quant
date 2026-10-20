"""Cox-5d: ``cox`` on the five sessions of ``open_to_close_5d`` (#310), as ``breiman-15m`` is
``breiman`` on its horizon.

Personality card: Cox's (``algorithms.cox``): the same multinomial logistic regression, ridge
penalty, flat the reference class, the same features (``daily_features``: the night's US
semiconductors and VIX, the previous day's flows, the previous days' moves over the volatility, the
volatility regime, the class shares, the NXT pre-market gap, the calendar, the night futures) and
settings (``TRAINING_DAYS``, ``LAMBDAS``, the 80/20 split). What it learns differs: the kind's
answers, the direction of a window of five sessions. Through the kind its inputs read the kind's
own band (σ̂ of five-session windows) and class shares (of the windows that ended): the same
feature code, the window's values.

**Learns** as Cox does, on samples whose windows have ended (the caller's purge, ``daily_training``),
and in its own chronological 80/20 split of the training days by the same rule (#310 "학습에서
겹침"): the fitted part holds only the windows that ended before the held-out part's first day,
so the H − 1 (4) days before it are left out of both. Every window is fitted again on all of them
with the λ chosen.

The first version keeps Cox's features as they are; the weekly horizon's candidates (5- and 20-day
returns, the five days' net buys, the window's option expiry, month end, FOMC) are chosen after
the pre-validation's result (#310). Its code is Cox's beside its own (``code_from``): a change to
Cox's has it pre-validated again.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence

import numpy as np

from krx_quant.algorithms.contract import Model, NothingToLearn, TrainedOn
from krx_quant.algorithms.cox import (
    FEATURES,
    LAMBDAS,
    MIN_SAMPLES,
    OWN,
    TRAINING_DAYS,
    VALIDATION_SHARE,
    Cox,
    _fit,
    _matrix,
    _probabilities,
    _row,
    _standardizer,
    param_meanings,
)
from krx_quant.cards import Card, Meaning
from krx_quant.daily import DailySample
from krx_quant.open_to_close import CLASSES
from krx_quant.open_to_close_5d import OPEN_TO_CLOSE_5D, SESSIONS

NAME = "cox-5d"
UP, FLAT, DOWN = CLASSES


class Cox5d(Cox):
    name = NAME
    kind = OPEN_TO_CLOSE_5D.name
    features: tuple[str, ...] = FEATURES
    training_days = TRAINING_DAYS
    param_meanings: Mapping[str, Meaning] = param_meanings()
    code_from: tuple[type, ...] = (Cox,)  # it fits and predicts with Cox's code
    card = Card(
        scientist="David Cox",
        character="밤사이 정보와 전날 수급으로 5거래일 방향",
        logic=(
            "그날 KRX 시가 단일가 → 다섯째 거래일 종가 단일가의 방향(상승·보합·하락)을 08:50까지 알던 "
            "것으로 다항 로지스틱 회귀한다. `cox`와 같은 모델과 입력이다(밤사이 미국 반도체와 VIX, 전 "
            "거래일 외국인·기관 순매수, 전 거래일의 움직임, 변동성 국면, 최근 클래스 비율, NXT 프리마켓 "
            "갭, 요일, 월말·월초, 야간선물). 입력의 변동성(σ̂)과 클래스 비율은 이 종류의 것(5세션 창, "
            "끝난 창)이다."
        ),
        unique_features=OWN,
        training=(
            f"`cox`와 같다: 최근 {TRAINING_DAYS}거래일 세 종목을 모아 학습한다. 표본은 그 시점에 창이 "
            f"끝난 날만이다(창이 {SESSIONS}세션이라 그 전 {SESSIONS - 1}일에 시작한 창의 정답은 뒤의 날을 "
            f"담는다). 벌점 λ를 고르는 시간순 80/20 분할 사이에 {SESSIONS - 1}거래일을 비운다: 앞 부분은 "
            "뒤 부분의 첫날 전에 끝난 창만 맞춘다."
        ),
        forecasting="상승·하락 로짓과 보합의 0을 소프트맥스해 세 확률을 낸다. 학습 전에는 1/3씩이다.",
        failure=(
            "`cox`와 같고, 5거래일 동안의 뉴스(실적, 일정)를 08:50의 입력이 모르는 만큼 더 틀린다. 첫 "
            "버전은 주간 지평의 입력(5·20일 수익률, 5일 누적 순매수, 창 안의 일정)이 없다."
        ),
    )

    def default_model(self) -> Model:
        return Model(f"{NAME}-default", {})

    def fit(self, samples: Sequence[DailySample]) -> Model:
        labelled = [s for s in samples if s.label is not None]
        days = sorted({s.day for s in labelled})[-self.training_days :]
        kept = {*days}
        labelled = sorted((s for s in labelled if s.day in kept), key=lambda s: (s.day, s.symbol))
        if len(labelled) < MIN_SAMPLES:
            raise NothingToLearn(f"{len(labelled)} labelled days, fewer than {MIN_SAMPLES}")
        raw = [_row(s.features, self.features) for s in labelled]
        y = np.array([CLASSES.index(s.label) for s in labelled if s.label is not None])
        cut = days[int(len(days) * (1 - VALIDATION_SHARE))]
        # The held-out part from ``cut``; the fitted part, the windows ended before it (#310): the
        # ones begun in the four days before end within the held-out days and are left out.
        early = np.array([s.ended_before(cut) for s in labelled])
        late = np.array([s.day >= cut for s in labelled])
        best: tuple[float, float] | None = None  # (loss, lambda)
        if early.sum() >= MIN_SAMPLES // 2 and late.sum() > 0:
            cols, mean, scale = _standardizer([r for r, e in zip(raw, early, strict=True) if e])
            fitted, held = _matrix(raw, cols, mean, scale), y[late]
            for penalty in LAMBDAS:
                theta = _fit(fitted[early], y[early], penalty)
                p = _probabilities(fitted[late], theta)
                loss = float(-np.mean(np.log(np.maximum(p[np.arange(len(p)), held], 1e-12))))
                if best is None or loss < best[0] - 1e-12:
                    best = (loss, penalty)
        penalty = LAMBDAS[-1] if best is None else best[1]
        cols, mean, scale = _standardizer(raw)
        x = _matrix(raw, cols, mean, scale)
        theta = _fit(x, y, penalty)
        p = _probabilities(x, theta)
        loss = float(-np.mean(np.log(np.maximum(p[np.arange(len(p)), y], 1e-12))))
        k = len(cols) + 1
        params: dict[str, float] = {"lambda": penalty, "samples": float(len(labelled))}
        params[f"b.{UP}"], params[f"b.{DOWN}"] = float(theta[0]), float(theta[k])
        for i, col in enumerate(cols):
            params[f"mean.{col}"] = mean[col]
            params[f"scale.{col}"] = scale[col]
            params[f"w.{UP}.{col}"] = float(theta[1 + i])
            params[f"w.{DOWN}.{col}"] = float(theta[k + 1 + i])
        first, last = labelled[0].day, labelled[-1].day
        canonical = json.dumps(params, sort_keys=True)
        digest = hashlib.sha256(f"{canonical}|{first}|{last}|{len(labelled)}".encode()).hexdigest()[:6]
        return Model(
            version=f"{NAME}-{last:%Y%m%d}-{digest}",
            params=params,
            trained=f"{first}..{last}, {len(labelled)} samples, log loss {loss:.4f}",
            trained_until=last,
            training=TrainedOn(first, last, len(labelled), loss),
            target_version=OPEN_TO_CLOSE_5D.answer_version,
        )
