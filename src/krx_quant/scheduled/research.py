"""The research and review jobs (#500): the signal threshold's review on a fixed window, the official
algorithm's fixed holdout and its regime gate, the nightly swap's hit rates, the measure of where making
a recorded day's samples spends its time, and the experiments on an algorithm's inputs, which run last.
``JOBS`` (``krx_quant.scheduled``) lists them in the order they run.

The diagnosis and the entry delay, the first two signal reviews, the structure check, the error
breakdown and the climate, link and turing prior experiments ran, posted their results and left
``JOBS`` (#505), as a one-off job does once it has done its work. Their commands stay for a run by
hand.
"""

from __future__ import annotations

from datetime import timedelta

from krx_quant.holdout import DAYS as HOLDOUT_DAYS
from krx_quant.neyman_variants import REPORT as NEYMAN_VARIANTS_REPORT
from krx_quant.nightly_hit_rates import HIT_RATE_NIGHTS
from krx_quant.sample_profile import LIMIT as SAMPLE_PROFILE_LIMIT
from krx_quant.scheduled.core import (
    FAILED,
    SIGNAL_REVIEW_FIXED_DAYS,
    SIGNAL_REVIEW_FROM,
    Attachment,
    Job,
    Night,
    Target,
)
from krx_quant.scheduled.prevalidations import _a_prevalidation

# ②″ The signal-threshold review on a fixed window (#206): on 2026-11-03 the user moved the default
# of ``KRX_SIGNAL_MIN_PROBABILITY`` from 0.6 to 0.70 (#483), provisionally: on section 5 of the
# second run (seven days), 0.70's up signals hit 67.0% [57.7–75.1%], its lower bound below 0.60's
# 58.1%. The default is held for ``SIGNAL_REVIEW_FIXED_DAYS`` (20) trading days and measured again on
# them alone: the first days with predictions from ``SIGNAL_REVIEW_FROM`` (2026-11-04, the first day
# the 06:00 deploy runs the new default), a fixed window (``signal-review --from``) that a later run
# reads the same. Once, on the first trading night they are all in (``Facts.signal_review_days``,
# counted in the engine DB read-only when the night's conditions are read: up to 20 small queries)
# with an official algorithm: the night of 2026-12-01 if every trading day from 11-04 has
# predictions (no market holiday in between). Its sections 4 and 5 compare the bands by trading day
# and section 6 prints the branch of the rule fixed here, before those days (``signal_review.verdict``,
# the note, docs/operations.md). Before the pre-validations, as the first run. The window's two
# constants are in ``core``, where ``read_facts`` counts its days.
SIGNAL_REVIEW_ISSUE = 206


def _signal_review_window_in(night: Night) -> bool:
    """Once, on the first trading night the fixed window's days are all in, with an official algorithm."""
    return (
        night.trading_day
        and night.facts.signal_review_days >= SIGNAL_REVIEW_FIXED_DAYS
        and night.facts.official > 0
        and not night.ran
    )


SIGNAL_REVIEW_FIXED = Job(
    name="signal-review-206-1201",
    due=_signal_review_window_in,
    command=(
        "signal-review",
        "--from",
        SIGNAL_REVIEW_FROM.isoformat(),
        "--days",
        str(SIGNAL_REVIEW_FIXED_DAYS),
    ),
    timeout=timedelta(hours=1),
    targets=(
        Target(
            SIGNAL_REVIEW_ISSUE,
            note=(
                "신호 기준 검토의 세 번째 실행이다. 2026-11-03 사용자가 "
                "`KRX_SIGNAL_MIN_PROBABILITY` 기본값을 0.6에서 0.70으로 바꿨다(#483, 잠정값). "
                "그 뒤 고정 창 20거래일(2026-11-04부터 예측이 있는 첫 20일)만으로 다시 잰 결과다. "
                "판정 규칙은 그날들을 보기 전에 정했고 결과를 보고 바꾸지 않는다: 5절의 구간 비교"
                "(같은 방향 ≥ 2, 상승 신호, 거래일 단위 95% 구간)로 위에서부터 처음 맞는 갈래를 "
                "고른다. (1) ≥ 0.75가 [0.70, 0.75)보다 높고 그 차이의 하한이 0보다 크며 ≥ 0.75의 "
                "상승 신호가 하루 5번 이상이면 0.75로 올린다. (2) ≥ 0.70 − [0.60, 0.70)의 점추정이 "
                "0 이하이면([0.60, 0.70)가 ≥ 0.70보다 나쁘지 않으면) 0.60으로 되돌린다. (3) 그 밖에는 "
                "0.70을 둔다. 채점 30번 미만이나 5일 미만인 구간의 비교는 판정 불가이고 그 갈래를 "
                "채우지 않는다. 6절이 고른 갈래를 적는다. 알림(`KRX_SIGNAL_ALERT`)은 판정과 상관없이 "
                "끈 채로 둔다. 켜는 것은 사용자가 따로 정한다. 에이전트가 6절의 갈래로 추천하고 "
                "사용자가 정한다. 1절의 지금 설정이 0.7이 아니면 서버 `.env`가 "
                "`KRX_SIGNAL_MIN_PROBABILITY`를 따로 적고 있다."
            ),
        ),
    ),
)


# The hit rates beside the nightly swap (#437): from the deploy that brings it (#446), each nightly report
# that compares the two models also has their hit rates on the same bar closes, what a hit-rate-first
# rule would have chosen and whether the log loss promoted a model whose hit rate fell
# (``nightly_hit_rates``); the swap rule stays the mean log loss. Once ``HIT_RATE_NIGHTS`` (10)
# nights' promotion lists carry them (counted in the store when the night's conditions are read,
# ``Facts.hit_rate_nights``: before the night's training ends, it may count that night the next), on
# that night, trading or not, once: ``nightly-hit-rates`` sums them up by algorithm, and an agent
# proposes on #437 whether to change the swap rule; the user decides. After a failure, again once a
# deploy brings other code. It reads ten promotion lists from the store: seconds; the quarter hour is
# a bound. Before the simulations and pre-validations, which may take the rest of the night. Its
# output holds counts, rates and dates, no price.
NIGHTLY_HIT_RATES_ISSUE = 437


def _with_hit_rate_nights(night: Night) -> bool:
    """Once, on the first night ``HIT_RATE_NIGHTS`` promotion lists carry the hit rates, trading or
    not; after a failed run, again on other code."""
    if night.facts.hit_rate_nights < HIT_RATE_NIGHTS:
        return False
    record = night.record
    return not night.ran or (
        record is not None and record.outcome == FAILED and record.commit != night.commit
    )


NIGHTLY_HIT_RATES = Job(
    name="nightly-hit-rates-437",
    due=_with_hit_rate_nights,
    command=("nightly-hit-rates", "--nights", str(HIT_RATE_NIGHTS)),
    timeout=timedelta(minutes=15),
    targets=(
        Target(
            NIGHTLY_HIT_RATES_ISSUE,
            note=(
                "밤 학습이 챔피언 교체(검증일 평균 로그 손실) 옆에 남긴 맞힘률의 요약이다(최근 10밤). "
                "교체 규칙은 바꾸지 않았다. 밤마다 두 모델이 모두 예측한 봉 마감에서 잰 맞힘률이고, "
                "이어진 두 밤은 검증일 3일 중 2일을 함께 쓰므로 밤들은 독립이 아니다. 에이전트가 이 "
                "출력(불일치: 로그 손실로는 승격했는데 맞힘률이 떨어진 밤, 두 규칙이 갈린 밤, 승격한 밤의 "
                "맞힘률 변화)으로 교체 규칙을 바꿀지에 대한 안을 이 이슈에 남기고 `ready-for-human`으로 "
                "돌린다. 사용자가 정한다. 다시 돌리기(읽기만): "
                "`docker compose run --rm engine nightly-hit-rates --nights 10`. "
                "출력에는 가격과 시세 값이 없다(개수, 비율, 날짜만)."
            ),
        ),
    ),
)


# turing's hypotheses judged on a fixed holdout (#447): four hypotheses picked from #431's report,
# fixed in #447 before the days after its period, are judged on the first ``holdout.DAYS`` (10) days
# after 2026-10-30 with turing's predictions on answer version 3 (``holdout.py``). Once, on the first
# night the window is full (``Facts.holdout_days``, counted in the engine DB read-only when the
# night's conditions are read: a few small queries), trading or not; after a failure, again once a
# deploy brings other code. It reads the window's scored predictions read-only beside the running
# engine, a day at a time, as ``error-breakdown`` does (which read 15 days in 24 seconds on the
# server); the hour is a bound. Before the simulations and pre-validations, which may take the rest
# of the night. Its output holds counts, ratios and lifts: no price, market value or σ̂₄ cut.
HOLDOUT_ISSUE = 447


def _holdout_full(night: Night) -> bool:
    """Once, on the first night the holdout window is full, trading or not; after a failed run,
    again on other code."""
    if night.facts.holdout_days < HOLDOUT_DAYS:
        return False
    record = night.record
    return not night.ran or (
        record is not None and record.outcome == FAILED and record.commit != night.commit
    )


ERROR_HOLDOUT = Job(
    name="error-holdout-447",
    due=_holdout_full,
    command=("error-holdout",),
    timeout=timedelta(hours=1),
    targets=(
        Target(
            HOLDOUT_ISSUE,
            note=(
                "#431의 리포트에서 고른 가설 넷(이 이슈 본문에 고정)을 그 뒤의 고정 창 10거래일로 판정한 "
                "결과다. 에이전트가 이 출력을 #431의 후속(B #448, C #449)과 엮어 이 이슈에 댓글로 정리하고 "
                "`ready-for-human`으로 돌린다. `섰다`인 가설만 C의 규칙이나 turing 개선의 근거가 된다. "
                "`서지 않음`과 `표본 부족`은 근거가 아니다. 가설, 창, σ̂₄ 경계, 구간, 최소 표본은 결과를 보고 "
                "바꾸지 않는다. 사용자가 정한다. 다시 돌리기(읽기만): "
                "`docker compose run --rm engine error-holdout`. 출력에는 가격과 시세 값이 없다"
                "(개수, 비율, lift만)."
            ),
        ),
    ),
)

# A regime gate on the same holdout (#449): at a bar close in σ̂₄'s lowest tercile or in the 12:00 or
# 12:30 slot it takes the climate baseline's prediction, elsewhere turing's (``climate_gate.py``),
# compared with both on #447's window. On the nights #447's job runs (``_holdout_full``), after it:
# it reads the same days read-only the same way, so the hour is as loose a bound. Its output holds
# counts, ratios and lifts: no price, market value or σ̂₄ cut.
GATE_ISSUE = 449

CLIMATE_GATE = Job(
    name="climate-gate-449",
    due=_holdout_full,
    command=("climate-gate",),
    timeout=timedelta(hours=1),
    targets=(
        Target(
            GATE_ISSUE,
            note=(
                "이 이슈 본문에 고정한 국면 문(봉 마감이 σ̂₄ 하위 삼분위이거나 12:00·12:30 칸이면 기후 "
                "기준, 아니면 turing)을 #447과 같은 고정 창 10거래일에서 turing, 기후와 같은 봉 마감으로 잰 "
                "결과다. 에이전트가 이 출력을 #447(가설 판정), #448(사전 확률 실험)과 엮어 이 이슈에 댓글로 "
                "정리하고 `ready-for-human`으로 돌린다. 문이 turing보다 낫다는 것은 turing 대비 거래일 95% "
                "하한 > 0(`섰다`)일 때만이다. 엔진에는 넣지 않는다. 결과를 보고 사용자가 정한다. 문, 창, "
                "σ̂₄ 경계, 구간은 결과를 보고 바꾸지 않는다. 다시 돌리기(읽기만): "
                "`docker compose run --rm engine climate-gate`. 출력에는 가격과 시세 값이 없다"
                "(개수, 비율, lift만)."
            ),
        ),
    ),
)


# Where making a recorded day's samples spends its time (#512): on the nights the code that makes
# samples changed, the night made every day again, and newton's step took 9,013 s (2026-10-26) and
# 11,600 s (10-29) for 23 days, turing's 240 s for 40; but newton, the first step, makes each day for
# every step, so the steps' times do not say what is slow. ``sample-profile`` makes the latest finished
# recorded day from scratch, as a night does, with the night's reads, with newton's and turing's
# features alone, and with the night's reads under cProfile: seconds and calls by stage and the top
# functions (``sample_profile.py``). Once, on the first night of a day that does not trade (no
# training of a new day before it, and the day's recording finished), with a recorded day to make;
# after a failure (a run cut by its limit among them), again on other code. A recorded day took
# 515–1,258 s to make on 10-29; four runs make it, one under the profiler, several times slower; the
# command stops itself at ``sample_profile.LIMIT`` (170 minutes), ten minutes before the job's limit.
# Between the simulations and the bar-close pre-validations: it gives way to a pre-validation it would
# carry past its last start, as the simulations do (#376), two nights at most. It reads the data
# directory and writes only a temporary directory. Its output holds seconds, counts, sizes and function
# names, no price. What to change (faster, made earlier in the evening, or as it is) is the next PR's.
SAMPLE_PROFILE_ISSUE = 512


def _first_quiet_night(night: Night) -> bool:
    """Once, on the first night of a day that does not trade, with a recorded day to make; after a
    failed run, again on other code."""
    if night.trading_day or night.facts.recorded_days < 1:
        return False
    record = night.record
    return not night.ran or (
        record is not None and record.outcome == FAILED and record.commit != night.commit
    )


SAMPLE_PROFILE = Job(
    name="sample-profile-512",
    due=_first_quiet_night,
    command=("sample-profile",),
    timeout=SAMPLE_PROFILE_LIMIT + timedelta(minutes=10),
    targets=(
        Target(
            SAMPLE_PROFILE_ISSUE,
            note=(
                "녹화일 하루의 표본을 밤 학습처럼 처음부터 만들며 잰 결과다. 밤의 읽기(모든 알고리즘의 "
                "피처와 종류의 정답: 밤에 그 날을 처음 읽는 단계, newton이 모든 단계를 위해 만든다), "
                "newton·turing의 피처만, 그리고 밤의 읽기를 cProfile로 한 번 더 만든다. 단계(파일 읽기 "
                "`reading the files`, 이벤트 재생 `replaying the events`, 피처 `the features`, 정답 "
                "`the answers`, 저장 `storing the day`, 그 밖 `the rest`)마다 초와 호출 수, 함수별 상위 "
                "목록이다. 에이전트가 "
                "이 출력으로 고칠 방법(더 빠르게: 가장 큰 단계와 함수부터, 미리 만들기: 15:40–21:30에 "
                "그날 표본을 만들어 두기, 그대로 두기)에 대한 안을 이 이슈에 남기고 `ready-for-human`으로 "
                "돌린다. 고치는 것은 다음 PR이고 사용자가 정한다. 표본을 만드는 코드를 고치면 그 다음 "
                "밤에 모든 날을 다시 만든다는 것도 안에 적는다. 다시 돌리기(읽기만, 엔진 옆에서 돌아도 "
                "된다): `docker compose run --rm engine sample-profile`. 출력에는 시세 값이 없다(초, "
                "횟수, 크기, 날짜, 함수 이름만)."
            ),
        ),
    ),
    gives_way_to=_a_prevalidation,
)


# Neyman's variants (#455, after #283's diagnosis of neyman's pre-validation): ``neyman-variants``
# walks neyman's pre-validation's folds over the training samples, replaying nothing, and compares
# neyman as it is (A) with its flips swapped back (B), without slot_return_5d and slot_return_20d (C),
# and C with the cost's distance in volatilities as a second-model input (D), on the same bar closes
# (``neyman_variants.py``). Two stages, each run once: on the first night after the deploy that brings
# it, trading or not, the discovery on neyman's pre-validation days fixes N, the window's days, from
# the days' paired gains' spread; then on the first night N recorded days after 2026-11-02 are in that
# have not been counted short (``Facts.neyman_variants``: read from its report and the day folders),
# the evaluation counts the window's days and, once it is full, judges it once (a window a short day
# left unfilled waits for the next recorded day). After a failure, again on other code. A day fits ten
# tree models on neyman's 20 days (about 1.7 s each on a Mac with 22,000 made-up samples, the server
# about three times slower) and reads a day: the discovery's 15 days take a quarter of an hour or so,
# a window of 60 about an hour; the four hours are a bound, with room for the burst CPU running out
# and for the window's first days the sample store has let go by then (made again, minutes a recorded
# day). Last: it holds back no simulation nor pre-validation. Its output holds counts, rates, lifts,
# scores and dates, no price nor σ̂₄. Adopting a candidate is #454's, and the user decides.
NEYMAN_VARIANTS_ISSUE = 455


def _neyman_variants_due(night: Night) -> bool:
    """The discovery on the first night; then the evaluation on the first night its window can be full,
    until it is judged; after a failed run, again on other code. Not on a night its progress was not
    read."""
    record = night.record
    if record is not None and record.outcome == FAILED and record.commit == night.commit:
        return False
    found = night.facts.neyman_variants
    if not found.read:
        return False
    if found.n is None:
        return True
    return not found.judged and found.candidates >= found.n


NEYMAN_VARIANTS = Job(
    name="neyman-variants-283",
    due=_neyman_variants_due,
    command=("neyman-variants",),
    timeout=timedelta(hours=4),
    targets=(
        Target(
            NEYMAN_VARIANTS_ISSUE,
            note=(
                "에이전트가 이 출력으로 이 이슈에 안을 남긴다. 발견 단계(첫 실행)면 N과 비교마다의 s_d를 "
                "정리한다. N이 60을 넘으면 평가 전에 비교를 줄일지 그대로 기다릴지 사용자에게 묻는다. "
                "평가면 돌리기 전에 정한 판정을 그대로 읽는다: B, C, D 중 A 대비 짝 lift가 0보다 크고 "
                '거래일 단위 98.333% 하한도 0보다 큰 팔이 "후보"다. 창, N, 판정 규칙은 결과를 보고 '
                "바꾸지 않는다. 후보도 바로 neyman에 넣지 않는다(#454가 끝난 뒤 사용자가 정한다). "
                "출력에는 시세 값이 없다(날짜, 개수, 비율, lift, 점수만. σ̂₄ 값과 경계도 없다)."
            ),
        ),
    ),
    attach=(Attachment(f"리포트 state/{NEYMAN_VARIANTS_REPORT}", NEYMAN_VARIANTS_REPORT),),
)
