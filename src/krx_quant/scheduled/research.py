"""The research and review jobs (#500): the signal threshold's review on a fixed window, the official
algorithm's fixed holdout and its regime gate, the nightly swap's hit rates, and the experiments on an
algorithm's inputs and on the SMT, which run last, the backfilled days' verdict on neyman's variants
(#532) the very last. ``JOBS`` (``krx_quant.scheduled``) lists them in the order they run.

The diagnosis and the entry delay, the first two signal reviews, the structure check, the error
breakdown and the climate, link and turing prior experiments ran, posted their results and left
``JOBS`` (#505), as a one-off job does once it has done its work. Their commands stay for a run by
hand, but the link and turing prior experiments', which left with their code once concluded (#559).
The measure of where making a recorded day's samples spends its time (``sample-profile-512``) ran on
2026-11-07, its fix (#630) is in, and it left with its command and module (#644).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, timedelta

from krx_quant.holdout import DAYS as HOLDOUT_DAYS
from krx_quant.holdout import days_in_window
from krx_quant.inverse_replay import AGENT_LINE
from krx_quant.neyman_backfill import GOES_ON_LINE as NEYMAN_BACKFILL_GOES_ON
from krx_quant.neyman_backfill import REPORT as NEYMAN_BACKFILL_REPORT
from krx_quant.neyman_backfill import Progress as NeymanBackfill
from krx_quant.neyman_backfill import progress as neyman_backfill_progress
from krx_quant.neyman_variants import REPORT as NEYMAN_VARIANTS_REPORT
from krx_quant.neyman_variants import Progress as NeymanVariants
from krx_quant.neyman_variants import progress as neyman_variants_progress
from krx_quant.nightly_hit_rates import HIT_RATE_NIGHTS, nights_with_hit_rates
from krx_quant.predictions import DB_NAME
from krx_quant.process_log import Ran
from krx_quant.scheduled.core import (
    FAILED,
    OK,
    Attachment,
    Job,
    Night,
    Reads,
    Target,
    again_on_new_code,
)
from krx_quant.signal_review import fixed_days
from krx_quant.smt_experiment import AFTER as SMT_AFTER
from krx_quant.smt_experiment import DAYS as SMT_DAYS
from krx_quant.smt_experiment import days_in_holdout as smt_days_in_holdout

# ②″ The signal-threshold review on a fixed window (#206): on 2026-11-03 the user moved the default
# of ``KRX_SIGNAL_MIN_PROBABILITY`` from 0.6 to 0.70 (#483), provisionally: on section 5 of the
# second run (seven days), 0.70's up signals hit 67.0% [57.7–75.1%], its lower bound below 0.60's
# 58.1%. The default is held for ``SIGNAL_REVIEW_FIXED_DAYS`` (20) trading days and measured again on
# them alone: the first days with predictions from ``SIGNAL_REVIEW_FROM`` (2026-11-04, the first day
# the 06:00 deploy runs the new default), a fixed window (``signal-review --from``) that a later run
# reads the same. Once, on the first trading night they are all in (``SignalReviewFacts.days``, counted in
# the engine DB read-only when the job is considered: up to 20 small queries) with an official
# algorithm: the night of 2026-12-01 if every trading day from 11-04 has predictions (no market holiday
# in between). Its sections 4 and 5 compare the bands by trading day
# and section 6 prints the branch of the rule fixed here, before those days (``signal_review.verdict``,
# the note, docs/operations.md). Before the pre-validations, as the first run.
SIGNAL_REVIEW_ISSUE = 206
SIGNAL_REVIEW_FROM = date(2026, 11, 4)
SIGNAL_REVIEW_FIXED_DAYS = 20


@dataclass(frozen=True)
class SignalReviewFacts:
    """What the signal review's condition asks."""

    # How many of the first ``SIGNAL_REVIEW_FIXED_DAYS`` days with predictions from ``SIGNAL_REVIEW_FROM``
    # are in the engine DB (``signal_review.fixed_days``).
    days: int = 0
    official: int = 0  # the consensus members: active algorithms on the engine's target


async def read_signal_review(reads: Reads) -> SignalReviewFacts:
    sources = reads.sources
    days = await asyncio.to_thread(
        fixed_days, sources.settings.state_dir / DB_NAME, SIGNAL_REVIEW_FROM, SIGNAL_REVIEW_FIXED_DAYS
    )
    return SignalReviewFacts(days, len(sources.running.members()))


def _signal_review_window_in(night: Night[SignalReviewFacts]) -> bool:
    """Once, on the first trading night the fixed window's days are all in, with an official algorithm."""
    return (
        night.trading_day
        and night.facts.days >= SIGNAL_REVIEW_FIXED_DAYS
        and night.facts.official > 0
        and not night.ran
    )


SIGNAL_REVIEW_FIXED = Job(
    name="signal-review-206-1201",
    due=_signal_review_window_in,
    facts=read_signal_review,
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
# ``read_hit_rate_nights``: before the night's training ends, it may count that night the next), on
# that night, trading or not, once: ``nightly-hit-rates`` sums them up by algorithm, and an agent
# proposes on #437 whether to change the swap rule; the user decides. After a failure, again once a
# deploy brings other code. It reads ten promotion lists from the store: seconds; the quarter hour is
# a bound. Before the simulations and pre-validations, which may take the rest of the night. Its
# output holds counts, rates and dates, no price.
NIGHTLY_HIT_RATES_ISSUE = 437


async def read_hit_rate_nights(reads: Reads) -> int:
    """How many of the latest nights' promotion lists carry the hit rates (``nights_with_hit_rates``, up
    to ``HIT_RATE_NIGHTS``)."""
    return await asyncio.to_thread(nights_with_hit_rates, reads.sources.settings)


def _with_hit_rate_nights(night: Night[int]) -> bool:
    """Once, on the first night ``HIT_RATE_NIGHTS`` promotion lists carry the hit rates, trading or
    not; after a failed run, again on other code."""
    return night.facts >= HIT_RATE_NIGHTS and again_on_new_code(night)


NIGHTLY_HIT_RATES = Job(
    name="nightly-hit-rates-437",
    due=_with_hit_rate_nights,
    facts=read_hit_rate_nights,
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


# The down calls replayed through the inverse ETF (#606, ``inverse_replay.py``): the explicit
# ``down`` and ``down_first`` calls of the traded kinds, by every predictor, each bought as one share of
# 0193L0 at the first ETF ask after its display and sold at the ETF's bid when 005930 reaches a barrier
# or at the time barrier, on the ETF's recorded quotes (its events, or its raw frames before they were
# made), with the gaps and the quotes older than 5 s left out; net won after the commissions (an ETF's
# sale pays no tax), not the cost map's floor. A measurement: no order, and nothing of the scoring, the
# live eligibility or the promotion reads it. Every trading night, once (a run a restart cut, the next),
# on the latest 20 recorded trading days, a day's recording at a time: 005930's quotes of a day are read
# in about a second, the ETF's 40,000 records and the day's predictions in less; the quarter hour is a
# bound. Each night's comment on #606 is a record and changes no label; ``ready-for-agent`` comes on a
# failed run and on the first report with 20 recorded days or more, once (``inverse_replay.AGENT_LINE``,
# kept in ``state/inverse-replay.json`` so that a night missed does not skip it), when an
# agent proposes on #606 whether down calls should be bought through the ETF and the user decides. Its
# output holds counts, rates, ticks and won, no price.
INVERSE_REPLAY_ISSUE = 606


def _every_trading_night(night: Night[None]) -> bool:
    """Every trading day's night, once; a run a restart cut runs the next trading night."""
    if not night.trading_day:
        return False
    record = night.record
    return record is None or record.outcome not in (OK, FAILED) or record.day != night.day


def inverse_replay_calls(ran: Ran) -> bool:
    """Whether the replay's night calls an agent: it failed, or its output says so (``AGENT_LINE``)."""
    return ran.exit_code != 0 or ran.timed_out or AGENT_LINE in ran.stdout


INVERSE_REPLAY = Job(
    name="inverse-replay-606",
    due=_every_trading_night,
    command=("inverse-replay",),
    timeout=timedelta(minutes=15),
    label_if=inverse_replay_calls,
    unlabel=(),  # a question an agent put to the user on the issue (``ready-for-human``) stays
    targets=(
        Target(
            INVERSE_REPLAY_ISSUE,
            note=(
                "아래 호출을 인버스 ETF(0193L0)로 재생했다(#606, 매매하지 않는다): 매매하는 종류의 명시적 "
                "`down`·`down_first` 호출마다(`not_up`은 횡보가 섞여 쓰지 않는다) 표시된 뒤 첫 ETF 매도 "
                "호가에서 1주를 사고, 005930이 장벽에 닿거나 만기가 오면 ETF 매수 호가에서 판다. 결측 구간과 "
                "5초보다 오래된 호가는 뺀다. 순손익은 기록된 호가와 양쪽 수수료로 잰다(ETF는 거래세가 없고, "
                "비용표의 하한은 쓰지 않는다). 최근 20거래일. 1절 날마다, 2절 모든 호출, 3절 종류별, 4절 "
                "칸별. 밤마다의 댓글은 기록이고 라벨을 바꾸지 않는다. 실패한 밤과 기록된 날이 20일 이상인 첫 "
                "보고(`에이전트 차례:` 줄, 한 번)에만 `ready-for-agent`를 붙인다. "
                "그때 에이전트가 이 이슈에 안을 남기고 사용자가 정한다. 다시 돌리기(읽기만): "
                "`docker compose run --rm engine inverse-replay --day <날>`. 출력에는 가격이 없다(개수, "
                "비율, 틱, 원만)."
            ),
        ),
    ),
)


# turing's hypotheses judged on a fixed holdout (#447): four hypotheses picked from #431's report,
# fixed in #447 before the days after its period, are judged on the first ``holdout.DAYS`` (10) days
# after 2026-10-30 with turing's predictions on answer version 3 (``holdout.py``). Once, on the first
# night the window is full (``read_holdout_days``, counted in the engine DB read-only when the job is
# considered: a few small queries), trading or not; after a failure, again once a
# deploy brings other code. It reads the window's scored predictions read-only beside the running
# engine, a day at a time, as ``error-breakdown`` does (which read 15 days in 24 seconds on the
# server); the hour is a bound. Before the simulations and pre-validations, which may take the rest
# of the night. Its output holds counts, ratios and lifts: no price, market value or σ̂₄ cut.
HOLDOUT_ISSUE = 447


async def read_holdout_days(reads: Reads) -> int:
    """How many days of turing's holdout window are in the engine DB (``holdout.days_in_window``, up to
    ``holdout.DAYS``); the climate gate's condition asks it too, read once a night for both."""
    return await asyncio.to_thread(days_in_window, reads.sources.settings.state_dir / DB_NAME)


def _holdout_full(night: Night[int]) -> bool:
    """Once, on the first night the holdout window is full, trading or not; after a failed run,
    again on other code."""
    return night.facts >= HOLDOUT_DAYS and again_on_new_code(night)


ERROR_HOLDOUT = Job(
    name="error-holdout-447",
    due=_holdout_full,
    facts=read_holdout_days,
    command=("error-holdout",),
    timeout=timedelta(hours=1),
    targets=(
        Target(
            HOLDOUT_ISSUE,
            note=(
                "#431의 리포트에서 고른 가설 넷(이 이슈 본문에 고정)을 그 뒤의 고정 창 10거래일로 판정한 "
                "결과다. 에이전트가 이 출력을 #431의 후속(B #448, C #449)과 엮어 이 이슈에 댓글로 "
                "정리하고 `ready-for-human`으로 돌린다. `섰다`인 가설만 C의 규칙이나 turing 개선의 "
                "근거가 된다. `서지 않음`과 `표본 부족`은 근거가 아니다. 가설, 창, σ̂₄ 경계, 구간, 최소 "
                "표본은 결과를 보고 바꾸지 않는다. 사용자가 정한다. 다시 돌리기(읽기만): `docker compose "
                "run --rm engine error-holdout`. 출력에는 가격과 시세 값이 없다(개수, 비율, lift만)."
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
    facts=read_holdout_days,
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


# Neyman's variants (#455, after #283's diagnosis of neyman's pre-validation): ``neyman-variants``
# walks neyman's pre-validation's folds over the training samples, replaying nothing, and compares
# neyman as it is (A) with its flips swapped back (B), without slot_return_5d and slot_return_20d (C),
# and C with the cost's distance in volatilities as a second-model input (D), on the same bar closes
# (``neyman_variants.py``). Two stages, each run once: on the first night after the deploy that brings
# it, trading or not, the discovery on neyman's pre-validation days fixes N, the window's days, from
# the days' paired gains' spread; then on the first night N recorded days after 2026-11-02 are in that
# have not been counted short (``read_neyman_variants``: read from its report and the day folders),
# the evaluation counts the window's days and, once it is full, judges it once (a window a short day
# left unfilled waits for the next recorded day). After a failure, again on other code. A day fits ten
# tree models on neyman's 20 days (about 1.7 s each on a Mac with 22,000 made-up samples, the server
# about three times slower) and reads a day: the discovery's 15 days take a quarter of an hour or so,
# a window of 60 about an hour; the four hours are a bound, with room for the burst CPU running out
# and for the window's first days the sample store has let go by then (made again, minutes a recorded
# day). Last: it holds back no simulation nor pre-validation. Its output holds counts, rates, lifts,
# scores and dates, no price nor σ̂₄. Adopting a candidate is #454's, and the user decides. Since #532
# the window's plan judges D − A alone (0.5 points at 95%, N 56 from the discovery's s_d, moved into the
# report with its date and reason; B − A and C − A described): the three are judged as registered on
# backfilled days instead (``neyman-backfill-532``, below). The window is as it was: 56 recorded days
# after 2026-11-02 are in late January 2027 at the earliest.
NEYMAN_VARIANTS_ISSUE = 455


async def read_neyman_variants(reads: Reads) -> NeymanVariants:
    """Where neyman's variants experiment stands (``neyman_variants.progress``): its window's N once
    discovered, whether it was judged, and the recorded days after 2026-11-02 that can be in the
    window. The backfill's condition asks it too."""
    sources = reads.sources
    return await asyncio.to_thread(neyman_variants_progress, sources.settings, sources.clock.now())


def _neyman_variants_due(night: Night[NeymanVariants]) -> bool:
    """The discovery on the first night; then the evaluation on the first night its window can be full,
    until it is judged; after a failed run, again on other code. Not on a night its progress was not
    read."""
    record = night.record
    if record is not None and record.outcome == FAILED and record.commit == night.commit:
        return False
    found = night.facts
    if not found.read:
        return False
    if found.n is None:
        return True
    return not found.judged and found.candidates >= found.n


NEYMAN_VARIANTS = Job(
    name="neyman-variants-283",
    due=_neyman_variants_due,
    facts=read_neyman_variants,
    command=("neyman-variants",),
    timeout=timedelta(hours=4),
    targets=(
        Target(
            NEYMAN_VARIANTS_ISSUE,
            note=(
                "에이전트가 이 출력으로 이 이슈에 안을 남긴다. 발견 단계(첫 실행)면 N과 비교마다의 s_d를 "
                "정리한다. N이 60을 넘으면 평가 전에 비교를 줄일지 그대로 기다릴지 사용자에게 묻는다. "
                "평가면 돌리기 전에 정한 판정을 그대로 읽는다: 2026-11-04에 바꾼 계획(#532)대로 D − A "
                '하나만 판정한다. 짝 lift가 0보다 크고 거래일 단위 95% 하한도 0보다 크면 D가 "후보"다. '
                "B − A, C − A는 설명일 뿐이다(세 비교는 백필 날로 판정했다, `neyman-backfill-532`). 창, N, "
                "판정 규칙은 결과를 보고 바꾸지 않는다. 후보도 바로 neyman에 넣지 않는다(#454가 끝난 뒤 "
                "사용자가 정한다). "
                "출력에는 시세 값이 없다(날짜, 개수, 비율, lift, 점수만. σ̂₄ 값과 경계도 없다)."
            ),
        ),
    ),
    attach=(Attachment(f"리포트 state/{NEYMAN_VARIANTS_REPORT}", NEYMAN_VARIANTS_REPORT),),
)


# The SMT experiment (#523, spec #519): does the rolling-extreme SMT (#520) raise the four-minute
# prediction's hit rate? A frozen rule (a bearish SMT calls down, a bullish one up, for each stock of
# the pair) against the engine's final consensus on the same bar closes, four claims (direction × L),
# all fixed in ``smt_experiment.py`` before the holdout's days (``smt-experiment``). Two stages, each
# run once, after a failure again on other code. The discovery, on the first night after the deploy
# that brings it, trading or not: the finished recorded days up to ``SMT_AFTER`` (2026-11-04), each
# day's trades replayed through the Market View as ``krx-quant smt --day`` does, and the days with
# backfilled bars only (about a year, bars only: a few minutes) apart. The verdict, on the first night
# the holdout's ``SMT_DAYS`` (10) eligible days after ``SMT_AFTER`` are in (``read_smt_holdout_days``,
# counted from the day folders and the engine DB read-only when the job is considered: a
# small query a day after the cutoff): the night of 2026-11-18 if every trading day from 11-05 is
# eligible. A day's replay reads two stocks' and the futures' trades, about as long as ``smt --day``:
# the discovery's two hours and the verdict's hour are bounds. Last, after the experiments on an
# algorithm's inputs: they hold back no simulation nor pre-validation. Their output holds counts,
# rates, percentage points and dates, no price. Opening the feature ticket is the user's, on a claim
# that stood.
SMT_ISSUE = 519


async def read_smt_holdout_days(reads: Reads) -> int:
    """How many days of the SMT experiment's holdout are in (``smt_experiment.days_in_holdout``, up to
    ``smt_experiment.DAYS``)."""
    sources = reads.sources
    now = sources.clock.now()
    return await asyncio.to_thread(smt_days_in_holdout, sources.settings, sources.calendar, now)


def _smt_holdout_full(night: Night[int]) -> bool:
    """Once, on the first night the SMT holdout's days are all in, trading or not; after a failed run,
    again on other code."""
    return night.facts >= SMT_DAYS and again_on_new_code(night)


SMT_DISCOVERY = Job(
    name="smt-discovery-519",
    due=again_on_new_code,
    command=("smt-experiment",),
    timeout=timedelta(hours=2),
    targets=(
        Target(
            SMT_ISSUE,
            note=(
                f"SMT 실험(#523)의 탐색이다. {SMT_AFTER}까지의 녹화일(사건은 녹화를 시장 보기로 재생해 "
                "다시 계산)과, 따로 채운 1분봉만 있는 날(정답이 근사, 엔진 예측 없음)로 쟀다. 세 쌍의 "
                "1분 수익률 상관과 함께 있는 시간, 사건 수, 사건 뒤 상승·보합·하락, 고정 규칙(하락 SMT면 "
                "하락, 상승 SMT면 상승)과 최종 합의의 같은 봉 마감 맞힘률 차이(%p), 기후 비교다. "
                "탐색이라 판정하지 않는다. 규칙, 판정 넷(방향 × L), 묶기, 최소 표본, 신뢰 수준, "
                "홀드아웃(기준일 뒤 자격이 있는 첫 10거래일)은 머지 때 코드와 문서에 고정했고 이 결과를 "
                "보고 바꾸지 않는다. 에이전트가 이 출력을 이 이슈에 정리한다. 판정은 홀드아웃이 다 찬 "
                "밤의 `smt-holdout-519`가 낸다. 다시 돌리기(읽기만): `docker compose run --rm engine "
                "smt-experiment`. 출력에는 가격이 없다(건수, 비율, %p, 날짜만)."
            ),
        ),
    ),
)

SMT_HOLDOUT = Job(
    name="smt-holdout-519",
    due=_smt_holdout_full,
    facts=read_smt_holdout_days,
    command=("smt-experiment", "--holdout"),
    timeout=timedelta(hours=1),
    targets=(
        Target(
            SMT_ISSUE,
            note=(
                f"SMT 실험(#523)의 사전 등록 판정이다. {SMT_AFTER} 뒤 자격이 있는 첫 10거래일(고정 "
                "창)에서 판정 넷(하락·상승 × L 15·30)마다 고정 규칙과 최종 합의의 같은 봉 마감 맞힘률 "
                "차이를 거래일 98.75% t 구간(본페로니)으로 봤다. 하한 > 0인 판정만 `섰다`다. `서지 "
                "않음`과 `표본 부족`은 근거가 아니다. 에이전트가 이 출력을 탐색 결과와 엮어 이 이슈에 "
                "정리하고 `ready-for-human`으로 돌린다. `섰다`가 있으면 피처와 알고리즘 변경을 다음 "
                "티켓의 사전 검증으로 넣을지 사용자가 정한다. 규칙, 창, 묶기, 최소 표본, 구간은 결과를 "
                "보고 바꾸지 않는다. 다시 돌리기(읽기만): `docker compose run --rm engine smt-experiment "
                "--holdout`. 출력에는 가격이 없다(건수, 비율, %p, 날짜만)."
            ),
        ),
    ),
)


# Neyman's variants on backfilled days (#532, after #455's discovery set N = 206 recorded days: a verdict
# in August–September 2027): ``neyman-variants --backfill`` judges #455's three comparisons as
# registered on the backfilled days before the discovery's training, each day predicted by the arms
# fitted on the 20 trading days before it (``neyman_backfill.py``), and moves the live window's saved
# plan to D − A alone (0.5 points at 95%, N 56 from the saved s_d; the discovery is not run again).
# Once, on the first night after the deploy that brings it with the discovery in its report
# (``read_neyman_variants``), trading or not; after a failure (a run cut by its limit among them), again
# on other code, reading the days the first run listed. A day fits ten tree models on its 20 days
# (about 51 s on the server: the discovery's 15 days took 11 minutes) and makes the one backfilled day
# that enters its window (4–6 s; the sample store keeps only the nights' days); the list's count makes
# each day once more, with no feature. The backfill reaches 365 calendar days back, so about 200 days
# (the oldest is the server's: the output says it), about three hours and a half. Last in the list:
# after the pre-validations, which it never holds back, and after the experiment due the night of
# 2026-11-05 (``smt-discovery-519``), which it does not push to another night. It only reads the
# sample store, so the nightly training's days stay (#512). Its output holds dates, counts, rates,
# lifts and powers, no price nor σ̂₄.
# By day across nights (#643): with a five-hour limit it had to start by 00:30, and after trainings
# ending 01:28–02:06 it waited four nights running (2026-11-05 to 11-08). It now starts when a day
# still fits before 05:30 (``resumes``: 30 minutes, the first night's listing, about 200 days counted
# at 4–6 s, among them), runs to 05:30 at most, keeps each day it walked and goes on from the first
# day left the next night (``neyman_backfill``'s "By day across nights"): due again while its report
# has no verdict. Its issue is called only on the run that judges (``label_if``); each night's
# comment says how many days are walked and left.
NEYMAN_BACKFILL_DAY = timedelta(minutes=30)


@dataclass(frozen=True)
class BackfillFacts:
    """What the backfill's condition asks: where the variants experiment stands, and the backfill."""

    variants: NeymanVariants = field(default_factory=NeymanVariants)
    backfill: NeymanBackfill = field(default_factory=NeymanBackfill)  # its verdict in, from its report


async def read_backfill(reads: Reads) -> BackfillFacts:
    return BackfillFacts(
        await reads(read_neyman_variants),
        await asyncio.to_thread(neyman_backfill_progress, reads.sources.settings),
    )


def _neyman_backfill_due(night: Night[BackfillFacts]) -> bool:
    """On each night with the variants' discovery in its report and no verdict of its own yet,
    trading or not, while it goes on by day (#643): after a run that stopped before the last day, or
    one its time limit stopped (the days it walked are kept); after another failure, again on other
    code. Not on a night its progress was not read."""
    found, backfill = night.facts.variants, night.facts.backfill
    if not found.read or found.n is None or not backfill.read or backfill.judged:
        return False
    record = night.record
    if record is None or record.outcome not in (OK, FAILED):
        return True  # never ran, or a restart cut it
    if record.day == night.day:
        return False  # once a night
    return record.outcome == OK or record.timed_out or record.commit != night.commit


def _neyman_backfill_judged(ran: Ran) -> bool:
    """Whether the run calls an agent: not when it stopped before the last day (#643)."""
    return NEYMAN_BACKFILL_GOES_ON not in ran.stdout


NEYMAN_BACKFILL = Job(
    name="neyman-backfill-532",
    due=_neyman_backfill_due,
    facts=read_backfill,
    command=("neyman-variants", "--backfill"),
    timeout=timedelta(hours=5),
    resumes=NEYMAN_BACKFILL_DAY,
    label_if=_neyman_backfill_judged,
    targets=(
        Target(
            NEYMAN_VARIANTS_ISSUE,
            note=(
                "#455의 세 비교(B, C, D 각각 대 A)를 등록 그대로 백필 날로 판정한 결과다(#532). 평가 날은 "
                "발견 단계가 학습한 첫날보다 앞선 백필 거래일 중, 그 앞에 데이터가 있는 날이 40일(학습 "
                "20거래일과 피처 준비 20거래일) 이상이고 네 팔 모두에 맞춘 채점 봉 마감이 200개 이상인 "
                "날이다. 날짜 목록은 어느 팔도 학습하기 전에 정해 리포트에 남겼다. 판정 규칙은 등록 "
                '그대로다: 짝 lift > 0이고 거래일 단위 98.333% 하한 > 0이면 "후보(백필)"다. N_avail이 '
                "206보다 적어도 판정한다. 통과하지 못했고 0.3%p를 찾을 검정력(발견의 s_d로)이 80%보다 "
                '낮으면 "결론 없음"이다. 월별·시간대별·σ̂₄ 삼분위별 결과와 주의점(정답이 1분봉 도달 '
                "정답, 호가·체결 흐름·테이프·IV 입력 없음, 국면이 다름)은 설명으로만 낸다. 후보(백필)는 "
                "지난 데이터에서의 후보이고, 실시간 맞힘률이 오른다는 뜻이 아니다. 같은 실행이 저장된 "
                "리포트의 실시간 계획을 D − A 하나(0.5%p, 양측 95%, N 56)로 옮긴다(사용자 결정 2026-11-04, "
                "발견 단계는 다시 돌지 않는다). 에이전트가 판정을 그대로 읽어 안을 이 이슈에 남긴다. "
                "후보를 neyman에 넣는 것은 #454가 끝난 뒤 사용자가 정한다. 날짜 목록, 규칙, 실시간 계획은 "
                "결과를 보고 바꾸지 않는다. 다시 돌리기(판정 뒤에는 저장한 줄만 다시 낸다): "
                "`docker compose run --rm engine neyman-variants --backfill`. 출력에는 시세 값이 없다"
                "(날짜, 개수, 비율, lift, 검정력만. σ̂₄ 값과 경계도 없다). 날 단위로 밤을 넘어 이어 한다"
                "(#643): 05:30 전에 하루치(30분)가 들어가면 시작해 05:30 전에 멈추고, 끝낸 날을 남겨 다음 "
                "밤에 남은 날부터 한다. 판정 전의 밤에는 몇 날째까지 했는지와 남은 날만 올리고 라벨은 "
                "바꾸지 않는다. 판정은 마지막 날 뒤에 한 번, 같은 규칙으로 낸다."
            ),
        ),
    ),
    attach=(Attachment(f"리포트 state/{NEYMAN_BACKFILL_REPORT}", NEYMAN_BACKFILL_REPORT),),
)
