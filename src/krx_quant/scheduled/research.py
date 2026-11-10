"""The research and review jobs (#500): the nightly swap's hit rates, the down calls replayed through
the inverse ETF, the verdict replayed, and the experiment on the SMT, which runs last. ``JOBS``
(``krx_quant.scheduled``) lists them in the order they run.

The diagnosis and the entry delay, the first two signal reviews, the structure check, the error
breakdown and the climate, link and turing prior experiments ran, posted their results and left
``JOBS`` (#505), as a one-off job does once it has done its work; their commands went later (#559,
#658). The measure of where making a recorded day's samples spends its time (``sample-profile-512``)
ran on 2026-11-07, its fix (#630) is in, and it left with its command and module (#644).

> **삭제됨** (2026-11-10, #NNN): the signal threshold's review on a fixed window
> (``signal-review-206-1201``), turing's holdout and the climate gate on it (``error-holdout-447``,
> ``climate-gate-449``), and neyman's variants and their backfill (``neyman-variants-283``,
> ``neyman-backfill-532``), with the commands and modules only they ran (#689). 이유: the user closed
> their issues (#206, #447, #449, #455) on 2026-11-10: the combined verdict (#676, #677) is the one
> buy/sell signal and the alerts stay off, the verdict trades barrier horizons, and neyman is judged by
> the walk-forward promotion (#681); the backfill took about 3.4 hours a night. 대신: the verdict's
> replay (``verdict-replay-679``) and the walk-forward promotion.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from krx_quant.inverse_replay import AGENT_LINE
from krx_quant.nightly_hit_rates import HIT_RATE_NIGHTS, nights_with_hit_rates
from krx_quant.process_log import Ran
from krx_quant.scheduled.core import (
    FAILED,
    OK,
    Job,
    Night,
    Reads,
    Target,
    again_on_new_code,
)
from krx_quant.smt_experiment import AFTER as SMT_AFTER
from krx_quant.smt_experiment import DAYS as SMT_DAYS
from krx_quant.smt_experiment import days_in_holdout as smt_days_in_holdout

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


# The verdict replayed (#679): the verdict (#676) made again at each bar close of the latest 20 recorded
# days, with the barrier algorithms the live engine ran and with every one walked forward (retrained before
# each day on the days before it, as a pre-validation), and its BUYs replayed a share each by the paper
# trader's rules (``verdict_replay.py``). A diagnosis: the margin and the family map are the verdict's
# version, fixed in #676 before it runs. Once, the first night after it is deployed, trading or not; after a
# failure, again once a deploy brings other code. One algorithm is walked forward at a time (about 2.5
# minutes and 300 MB each for the queue screen's on 2026-11-10, four of them), then a day's recording at a
# time: about 15 minutes, the hour a bound. Before the simulations and pre-validations, which may take the
# rest of the night, and after the nightly training (every job is). Its output holds counts, rates, ticks
# and won, no price.
VERDICT_REPLAY_ISSUE = 679

VERDICT_REPLAY = Job(
    name="verdict-replay-679",
    due=again_on_new_code,
    command=("verdict-replay",),
    timeout=timedelta(hours=1),
    targets=(
        Target(
            VERDICT_REPLAY_ISSUE,
            note=(
                "판정(#676)을 기록된 최근 20거래일의 봉 마감마다 다시 내렸다(#679, 매매하지 않는다). "
                "멤버는 두 가지다: 그날 라이브 엔진이 돌린 장벽 알고리즘(엔진이 저장한 예측)과, 장벽 "
                "알고리즘 모두를 날마다 그 전 날들로만 다시 학습한 것(사전 검증의 전진 학습). 매수는 "
                "trader의 규칙으로 1주씩 재생했다: 결정 뒤 첫 매도 호가에 사고, 목표·손절·기한(한 번에 "
                "하나일 때는 판정의 매도도)에 판다. 순손익은 수수료와 거래세를 뺀 원이다. 진단이지 조정이 "
                "아니다: m과 가족은 #676에서 정했고 이 날들로 고르지 않는다. 다시 돌리기(읽기만): "
                "`docker compose run --rm engine verdict-replay [--day <날>] [--days N]`. 출력에는 가격이 "
                "없다(개수, 비율, 틱, 원만)."
            ),
        ),
    ),
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
# the discovery's two hours and the verdict's hour are bounds. Last: they hold back no simulation nor
# pre-validation. Their output holds counts,
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
