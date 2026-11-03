"""The research and review jobs (#500): the engine's diagnosis and the entry delay, the signal
threshold's reviews, the structure check, the official algorithm's error breakdown and its fixed
holdout, the nightly swap's hit rates, and the experiments on an algorithm's inputs, which run last.
``JOBS`` (``krx_quant.scheduled``) lists them in the order they run.
"""

from __future__ import annotations

from datetime import date, timedelta

from krx_quant.climate_prior import REPORT as CLIMATE_PRIOR_REPORT
from krx_quant.holdout import DAYS as HOLDOUT_DAYS
from krx_quant.neyman_variants import REPORT as NEYMAN_VARIANTS_REPORT
from krx_quant.nightly_hit_rates import HIT_RATE_NIGHTS
from krx_quant.poisson_link import REPORT as POISSON_LINK_REPORT
from krx_quant.prevalidation import MIN_DAYS
from krx_quant.scheduled.core import (
    FAILED,
    SIGNAL_REVIEW_FIXED_DAYS,
    SIGNAL_REVIEW_FROM,
    Attachment,
    Job,
    Night,
    Target,
    _first_night,
    _first_trading_night,
    sections,
)
from krx_quant.turing_prior import REPORT as TURING_PRIOR_REPORT

# ① The diagnosis (#203, #187): after the engine with its section 9 (#213) is deployed, on
# the first trading day's night. Sections 7 and 8 decide BAR_C and record the barrier bar
# rule's agreement (#203); section 9 is the crossed quotes (#187). It reads the engine DB
# read-only, safe beside the running engine, one feed at a time; the hour is a bound, not
# an estimate. It ran on the night of 2026-10-16: BAR_C = 1 was confirmed and the barrier
# bar rule's agreement measured (#203), and #187 chose from section 9 to count crossed quotes
# in the touches (target version 3, barrier answer version 2).
DIAGNOSE = Job(
    name="diagnose-203-187",
    due=_first_trading_night,
    command=("diagnose", "--days", "20"),
    timeout=timedelta(hours=1),
    targets=(
        Target(203),
        Target(
            187,
            sections("진단", "9. 뒤집힌 호가(#187)"),
            "출력 중 머리말과 9절(뒤집힌 호가)만 싣는다. 전체 출력은 #203에 있다.",
        ),
    ),
)

# ② The signal-threshold review (#206): on the first night with an official algorithm (the
# first probation verdicts on the touch target, about ten trading days after it restarted
# probation, #170), once. Before, the consensus has no member and no signal goes out. The
# command reads the engine DB read-only, beside the running engine; the hour is a bound, not
# an estimate. Before the pre-validations: it is short, and they may take the rest of the night.
SIGNAL_REVIEW_ISSUE = 206


def _first_official_night(night: Night) -> bool:
    """Once, on the first night the consensus has a member (an official algorithm)."""
    return night.facts.official > 0 and not night.ran


SIGNAL_REVIEW = Job(
    name="signal-review-206",
    due=_first_official_night,
    command=("signal-review", "--days", "20"),
    timeout=timedelta(hours=1),
    targets=(
        Target(
            SIGNAL_REVIEW_ISSUE,
            note=(
                "에이전트가 이 출력으로 신호 기준의 추천값(`KRX_SIGNAL_MIN_PROBABILITY`, "
                "`KRX_SIGNAL_MIN_AGREE`)을 낸다. 사용자가 정하면 서버 `.env`의 두 값을 바꾸거나 기본값을 "
                "PR로 바꾼다."
            ),
        ),
    ),
)

# ②′ The signal-threshold review again (#206): its first run (the night of 2026-10-24) had one day
# on which newton and turing were both official members, so the thresholds were kept and the review
# put off until more trading days were recorded (the #206 comment of 2026-10-25). Once, on the night
# of the first trading day on or after ``SIGNAL_REVIEW_AGAIN_FROM`` with an official algorithm, the
# same command reads the days since; its section 5 (the consensus had the official algorithms been
# members all along) then covers them. Before the pre-validations, as the first run.
SIGNAL_REVIEW_AGAIN_FROM = date(2026, 11, 2)


def _signal_review_again(night: Night) -> bool:
    """Once, on the first trading night from ``SIGNAL_REVIEW_AGAIN_FROM`` with an official algorithm."""
    return (
        night.trading_day
        and night.day >= SIGNAL_REVIEW_AGAIN_FROM
        and night.facts.official > 0
        and not night.ran
    )


SIGNAL_REVIEW_AGAIN = Job(
    name="signal-review-206-1102",
    due=_signal_review_again,
    command=("signal-review", "--days", "20"),
    timeout=timedelta(hours=1),
    targets=(
        Target(
            SIGNAL_REVIEW_ISSUE,
            note=(
                "신호 기준 검토의 두 번째 실행이다. 첫 실행(2026-10-24 밤)은 정식 합의가 있는 날이 "
                "하루뿐이라 기준을 그대로 두고 2026-11-02쯤 다시 보기로 했다. 에이전트가 이 출력으로 "
                "`KRX_SIGNAL_MIN_PROBABILITY`, `KRX_SIGNAL_MIN_AGREE`의 추천값을 낸다. 사용자가 정하면 "
                "서버 `.env`의 두 값을 바꾸거나 기본값을 PR로 바꾼다."
            ),
        ),
    ),
)


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


# ④ The structure check (#270): the research's market-structure claims (``structure_check``)
# measured on the stored one-minute bars, day candles and KIS daily flows, once, on the first
# night after the deploy that brings the job (trading or not: it reads finished days only).
# It reads files and makes no call; its output holds no price, volume or flow value, so it can
# go on the public issue. A year of bars for three symbols is a few minutes; the half hour is a
# bound. Before the pre-validations, which may take the night.
STRUCTURE_CHECK_ISSUE = 270

STRUCTURE_CHECK = Job(
    name="structure-check-270",
    due=_first_night,
    command=("structure-check",),
    timeout=timedelta(minutes=30),
    targets=(
        Target(
            STRUCTURE_CHECK_ISSUE,
            note=(
                "에이전트가 이 출력으로 리서치 보고서(`research/reports/`)와 #262(채점), "
                "#264(하루 한 번 예측)에 반영할 것을 정리한다. 출력에는 가격·거래량·수급 값이 없다"
                "(비율, 기울기, 개수, 중앙값만)."
            ),
        ),
    ),
)

# The entry delay (#262): after the engine with the diagnosis's section 10 is deployed, on the
# first trading day's night, once: each predictor's skill against the class rates, and its up
# and down calls acted on 1, 5 and 30 seconds after the bar close, over the 20 days. The whole
# diagnosis runs (it is one command); only its header and section 10 go to #262. The hour is a
# bound, as the diagnosis's above. Before the pre-validations.
ENTRY_DELAY_ISSUE = 262

ENTRY_DELAY = Job(
    name="entry-delay-262",
    due=_first_trading_night,
    command=("diagnose", "--days", "20"),
    timeout=timedelta(hours=1),
    targets=(
        Target(
            ENTRY_DELAY_ISSUE,
            sections("진단", "10. 예측자별 실력과 진입 지연(#262"),
            "출력 중 머리말과 10절(예측자별 실력과 진입 지연)만 싣는다. 에이전트가 이것으로 #262의 진입 지연"
            " 민감도를 정리한다. 목표 도달과 닿음 손익은 봉 마감, +1초, +5초, +30초에 들어갔을 때다.",
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


# Where turing errs (#431): ``error-breakdown`` breaks the official algorithm's live predictions of
# the latest 20 days with predictions down by symbol, 30-minute slot, spread and volatility, each
# cell with its confusion matrix and its lift over the climate baseline, before any algorithm is
# changed or added for accuracy. Once, on the first night after the deploy that brings it (trading
# or not: it reads the stored days only). It reads the engine DB read-only beside the running
# engine, a day at a time, as ``signal-review`` does; the hour is a bound, not an estimate. It
# prints counts and ratios, no price or market value, so it can go on the public issue. Before the
# pre-validations, which may take the rest of the night.
ERROR_BREAKDOWN_ISSUE = 431

ERROR_BREAKDOWN = Job(
    name="error-breakdown-431",
    due=_first_night,
    command=("error-breakdown", "--days", "20", "--algorithm", "turing"),
    timeout=timedelta(hours=1),
    targets=(
        Target(
            ERROR_BREAKDOWN_ISSUE,
            note=(
                "탐색용 리포트다. 에이전트가 이 출력으로 turing의 정확도(상승·보합·하락 맞힘률)를 올릴 "
                "만한 곳(시간대, 스프레드, 변동성, 종목)과 후속 티켓 후보를 이 이슈에 댓글로 남기고 "
                "`ready-for-human`으로 돌린다. 근거는 칸의 기후 대비 lift와 거래일 하한, 혼동 행렬, "
                "정밀도다. `표본 부족` 칸은 고르지 않는다. 여기서 고른 칸은 그대로 판정하지 않고, 후속 "
                "티켓이 이 기간 뒤의 날들(홀드아웃)로 판정한다. 사용자가 정한다. 다시 돌리기(읽기만): "
                "`docker compose run --rm engine error-breakdown --days 20`. 출력에는 가격과 시세 값이 "
                "없다(개수, 비율, lift만)."
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


# The climate prior experiment (#429): shannon's pre-validation was refused 5.5 points below the
# climate baseline (2026-10-30, #204). ``climate-prior`` walks that pre-validation's folds over the
# training samples, replaying nothing, and compares the climate alone, shannon as it is and shannon
# with each symbol and slot's smoothed class frequencies as two inputs more, on the same bar closes
# (``climate_prior.py``). Once, on the first night with ``MIN_DAYS`` recorded days (trading or not:
# it reads finished days); after a failure (its time limit, say), again once a deploy brings other
# code. It reads the days the nights stored and fits shannon's regression twice a day: under a
# minute or two a day, as a pre-validation's training phases without their replays, its memory
# logged a day (#383). The three hours are a bound, with room for the burst CPU running out. Last: it
# holds back no simulation nor pre-validation, and a night one of them leaves too short for it, it
# runs the next. Its output holds counts, rates, lifts, weights and dates, no price. Whether shannon
# takes the prior is decided from it in another PR.
CLIMATE_PRIOR_ISSUE = 429


def _until_a_run_ends_ok(night: Night) -> bool:
    """On the first night with at least ``MIN_DAYS`` recorded days, trading or not, once; after a
    failed run, again on other code."""
    if night.facts.recorded_days < MIN_DAYS:
        return False
    record = night.record
    return not night.ran or (
        record is not None and record.outcome == FAILED and record.commit != night.commit
    )


CLIMATE_PRIOR = Job(
    name="climate-prior-429",
    due=_until_a_run_ends_ok,
    command=("climate-prior",),
    timeout=timedelta(hours=3),
    targets=(
        Target(
            CLIMATE_PRIOR_ISSUE,
            note=(
                "에이전트가 이 출력으로 #429의 채택 규칙을 판정해 이슈에 정리한다: (c)가 (b)보다 낫고"
                "((c) over (b)가 0보다 크고) 기후 대비 거래일 단위 하한이 (b)보다 높으면 `shannon`에 사전 "
                "확률을 넣는 PR을 연다(코드 digest가 바뀌어 사전 검증이 다시 돈다). 아니면 넣지 않고 숫자만 "
                "남긴다. 출력에는 시세 값이 없다(날짜, 개수, 비율, lift, 가중치만)."
            ),
        ),
    ),
    attach=(Attachment(f"리포트 state/{CLIMATE_PRIOR_REPORT}", CLIMATE_PRIOR_REPORT),),
)


# The asymmetric link experiment (#439): poisson's pre-validation was refused, always flat and 5.2
# points below the climate baseline (2026-10-31, #204). ``poisson-link`` walks that pre-validation's
# folds over the training samples, replaying nothing, and compares the climate alone, poisson as it
# is and poisson's lean through a multinomial link of its own, on the same bar closes
# (``poisson_link.py``). Run as ``climate-prior-429`` is (``_until_a_run_ends_ok``): once, on the
# first night with ``MIN_DAYS`` recorded days; after a failure, again on other code. A day fits
# poisson twice (the fold's, and the intensities its link is tuned with) and the link six times:
# a minute or two, its memory logged a day (#383). The three hours are a bound, with room for the
# burst CPU running out. Last with the other experiments: it holds back no simulation nor
# pre-validation. Its output holds counts, rates, lifts, leans, coefficients and dates, no price.
# Whether poisson takes the link is proposed from it on #439, and decided in another PR.
POISSON_LINK_ISSUE = 439
POISSON_LINK = Job(
    name="poisson-link-439",
    due=_until_a_run_ends_ok,
    command=("poisson-link",),
    timeout=timedelta(hours=3),
    targets=(
        Target(
            POISSON_LINK_ISSUE,
            note=(
                "에이전트가 이 출력으로 #439의 비교 규칙을 판정하고, poisson에 넣을지에 대한 안을 "
                "이슈에 남긴다. 규칙은 돌리기 전에 정했다: (c)가 (b)보다 낫고((c) over (b)가 0보다 "
                "크고) 기후 대비 거래일 단위 하한이 (b)보다 높으면 순서형 연결이 poisson을 막았다는 "
                "뜻이다. 그러면 연결을 바꾸는 안을 낸다(코드 digest가 바뀌어 사전 검증이 다시 돈다). "
                "아니면 쏠림 분위별로 나온 클래스와 (c)의 쏠림 가중치로 입력에 정보가 있는지 정리한다. "
                "결과로 poisson을 저절로 바꾸지 않는다. 출력에는 시세 값이 없다(날짜, 개수, 비율, "
                "lift, 쏠림, 계수만)."
            ),
        ),
    ),
    attach=(Attachment(f"리포트 state/{POISSON_LINK_REPORT}", POISSON_LINK_REPORT),),
)


# The symbol and climate prior experiment for turing (#448, after #431's breakdown of turing's
# live predictions): ``turing-prior`` walks turing's pre-validation folds over the training samples,
# replaying nothing, and compares the climate alone, turing as it is and turing's trees with the
# symbol, each symbol and slot's smoothed class frequencies (#429's prior), or both, as inputs more,
# on the same bar closes, overall and by σ̂₄ tercile and 30-minute slot (``turing_prior.py``). Run as
# ``climate-prior-429`` is (``_until_a_run_ends_ok``): once, on the first night with ``MIN_DAYS``
# recorded days; after a failure, again on other code. A day fits turing's trees four times on its
# 60 days (about 3 s each on a Mac with three symbols' made-up 60 days, the server about three
# times slower): a minute or so, its memory logged a day (#383). The three hours are a bound, with
# room for the burst CPU running out. Last with the other experiments: it holds back no simulation
# nor pre-validation. Its output holds counts, rates, lifts, gain shares and dates, no price nor
# σ̂₄. Whether turing takes an input is proposed from it on #448, and decided in another PR.
TURING_PRIOR_ISSUE = 448
TURING_PRIOR = Job(
    name="turing-prior-448",
    due=_until_a_run_ends_ok,
    command=("turing-prior",),
    timeout=timedelta(hours=3),
    targets=(
        Target(
            TURING_PRIOR_ISSUE,
            note=(
                "에이전트가 이 출력으로 #448의 판정 규칙을 보고, turing에 넣을지에 대한 안을 이슈에 "
                "남긴다. 규칙은 돌리기 전에 정했다: (c), (d), (e) 중 (b)보다 낫고((b) 대비 짝 lift가 "
                '0보다 크고) 기후 대비 거래일 하한이 (b)보다 높은 팔이 "후보"다. 후보도 바로 turing에 '
                "넣지 않는다. 넣으면 turing의 코드 digest가 바뀌어 turing이 수습으로 돌아가고, 바뀐 뒤 "
                "녹화일 10일로 `prevalidate-revision-turing`이 다시 판정한다. 정식은 그것과 실시간 수습을 "
                "모두 통과해야 된다(#454). 사용자가 정한다. 출력에는 시세 값이 없다(날짜, 개수, 비율, "
                "lift, 이득 몫만. σ̂₄ 값과 경계도 없다)."
            ),
        ),
    ),
    attach=(Attachment(f"리포트 state/{TURING_PRIOR_REPORT}", TURING_PRIOR_REPORT),),
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
# day). Last with the other experiments: it holds back no simulation nor pre-validation. Its output
# holds counts, rates, lifts, scores and dates, no price nor σ̂₄. Adopting a candidate is #454's, and
# the user decides.
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
