"""The probe jobs (#500): each measures on the server what an outside source's data means (KIS,
Toss, the KRX Open API, the recordings) before the engine uses it (CODING_STANDARDS.md "External
data is probed before it is used"), what the server's own files are, when a fix waits for that,
or, every night, what the recording kept of it. ``JOBS`` (``krx_quant.scheduled``) lists them in
the order they run.

The sixteen probes and checks up to ``probe-member-frames-381`` ran once, posted their results and
left ``JOBS`` (#505), as a one-off job does once it has done its work, so that a lost state file
cannot run them again. Most of their commands stay for a run by hand (``krx-quant probe-supply`` and
the rest); those whose questions were settled went with their modules (#563). A new probe goes here,
with its condition (``core._first_night`` and ``core._first_trading_night`` are the usual ones).
"""

from __future__ import annotations

from datetime import timedelta

from krx_quant.process_log import Ran
from krx_quant.scheduled.core import FAILED, OK, Job, Night, Target
from krx_quant.trader.report import calls_agent

# What changes the key of a day the night stored (#565): a normal night makes the trading day before
# its new one again (532–1,132 s, about 40% of the night), so that day's key changed between the night
# that stored it and the next. ``probe-sample-keys`` (``sample_keys_probe.py``) prints, after a night's
# training, the inputs of the two latest stored days' keys that the Object Store stored after their own
# night (with the copies' SHA-256, sizes and times), the latest day's part files not merged yet, and
# the keys that changed since its last run, whose keys it keeps in ``state/probe-sample-keys.json``.
# Twice: on the first trading night after the deploy that brings it, and on the next trading night,
# whose run names every part of the key that changed between the two (a file or not). Either again
# after a failure, on other code. It reads the state, the data directory and the Object Store and
# writes only its state file; its output holds paths, sizes, times, counts and SHA-256 prefixes, no
# price. The fix waits for its results.
SAMPLE_KEYS_ISSUE = 565


def _again_on_other_code(night: Night) -> bool:
    """Not run yet; or failed, and the code is other now."""
    record = night.record
    return not night.ran or (
        record is not None and record.outcome == FAILED and record.commit != night.commit
    )


def _first_sample_keys(night: Night) -> bool:
    """On the first trading night (after a failure, again on other code)."""
    return night.trading_day and _again_on_other_code(night)


def _second_sample_keys(night: Night) -> bool:
    """On a trading night after the first run's, once it ran to its end (after a failure, again on
    other code)."""
    first = night.records.get(SAMPLE_KEYS.name)
    return (
        night.trading_day
        and first is not None
        and first.outcome == OK
        and first.day < night.day
        and _again_on_other_code(night)
    )


_NOTE = (
    "밤 학습 뒤 표본 저장소의 키를 본 결과다. 평소 밤에 새 날 말고 그 앞 거래일을 다시 만드는 까닭(#565)을 "
    "찾는다. 1절: 표본 저장소의 날과 파일, 그 밤이 만든 날. 2절: 마지막 두 날의 키 입력 가운데 그 날의 밤 "
    "학습이 시작한 뒤 Object Store에 올라간 것과 지금 SHA-256이 키와 다른 것(Object Store, 밤의 캐시, 데이터 "
    "폴더의 크기·시각). 3절: 마지막 날의 아직 합치지 않은 part 파일(표본이 읽는 파티션과 아닌 것). 4절: 지난 "
    "실행 뒤 키가 바뀐 날과 바뀐 부분. 두 번째 실행(다음 거래일 밤)의 4절이 두 밤 사이에 바뀐 키의 부분을 "
    "모두 보인다. 에이전트가 원인을 이 이슈에 적고 고치는 PR을 연다. 다시 돌리기(쓰는 것은 "
    "`state/probe-sample-keys.json`뿐, 엔진 옆에서 돌아도 된다): `docker compose exec engine krx-quant "
    "probe-sample-keys`. 출력에는 시세 값이 없다(경로, 크기, 시각, 개수, SHA-256 앞 12자만)."
)

SAMPLE_KEYS = Job(
    name="probe-sample-keys-565",
    due=_first_sample_keys,
    command=("probe-sample-keys",),
    timeout=timedelta(minutes=30),
    targets=(Target(SAMPLE_KEYS_ISSUE, note=_NOTE),),
)

SAMPLE_KEYS_AGAIN = Job(
    name="probe-sample-keys-565-again",
    due=_second_sample_keys,
    command=("probe-sample-keys",),
    timeout=timedelta(minutes=30),
    targets=(Target(SAMPLE_KEYS_ISSUE, note=_NOTE),),
)


# The recording's quality, every trading night (#570, ``recording_quality.py``): the day's
# disconnects in the session by feed, the recorded one-minute bars against KIS's (minutes missing,
# volumes and closes that differ, rows recorded twice), the frames short of fields or not parsed,
# the REST polls' failures and the night futures' recording against the nights before. The command
# reads files and the engine's ``/v1/status``, and imports the probes it reuses (``gaps_probe``,
# ``night_probe``): nothing here imports it, so the collector loads no probe (#552, #558).
#
# It runs right after the trader's report, at about 22:31 (later on the sample-key probes' two
# nights, #565): the polls' counts are the engine's of today, in memory, and are gone at midnight. A
# server-sized day read in about 6 s on a Mac, at a peak of 365 MB resident
# (``recording_quality.MEMORY_MB`` bounds it); fifteen minutes is a bound, short so that the
# simulations and pre-validations after it keep their starts.
#
# One comment a night on the standing issue #571, which gets ``ready-for-agent`` only on a night past
# a threshold (its ``에이전트 차례:`` line names why, #402's way) or whose run failed; another night's
# comment is a record and changes no label. No label is taken off: a question an agent left for the
# user there (``ready-for-human``) stays. An agent reads it as ``docs/agents/recording-quality.md``
# says.
RECORDING_QUALITY_ISSUE = 571


def _each_trading_night(night: Night) -> bool:
    """Every trading day's night, once; a run a restart cut runs the next trading night."""
    if not night.trading_day:
        return False
    record = night.record
    return record is None or record.outcome not in (OK, FAILED) or record.day != night.day


def recording_quality_calls(ran: Ran) -> bool:
    """Whether a night's report calls an agent: its ``에이전트 차례:`` line names a reason, or the run
    failed."""
    return ran.exit_code != 0 or ran.timed_out or calls_agent(ran.stdout)


RECORDING_QUALITY = Job(
    name="recording-quality-570",
    due=_each_trading_night,
    command=("recording-quality",),
    timeout=timedelta(minutes=15),
    label_if=recording_quality_calls,
    unlabel=(),  # a question an agent put to the user on the issue (``ready-for-human``) stays
    targets=(
        Target(
            RECORDING_QUALITY_ISSUE,
            note=(
                "기록 품질 리포트(#570): 그날 장중 끊김(피드별 횟수와 초), 녹화한 1분봉과 KIS 1분봉의 "
                "견줌(빠진 분, 거래량·종가가 다른 분, 두 번 기록됐을 수 있는 행), 필드가 모자라거나 "
                "해석하지 못한 프레임, REST 조회 실패, 지난밤 야간선물 기록량을 앞선 밤들과 견준 것. "
                "`에이전트 차례:` 줄이 기준을 넘은 까닭을 말하는 밤과 작업이 실패한 밤에만 "
                "`ready-for-agent`를 붙인다. 에이전트가 할 일은 `docs/agents/recording-quality.md`에 "
                "있다. 다시 보기(읽기만): `docker compose run --rm engine recording-quality --day <날>`"
                "(그날 밤이 지나면 REST 실패는 모른다). 출력에는 시세 값이 없다(개수, 초, 비율, 시각, "
                "피드 이름, 키만)."
            ),
        ),
    ),
)


# How long the bar closes took on the first trading day with NumPy's BLAS and OpenMP on the calling
# thread (#589, ``Dockerfile``), against the five trading days before (``bar_close_times.py``): each bar
# close's first record, final consensus and last record, each algorithm's time after the first record,
# the engine's ``loop_held`` runs (from this deploy on; Loki has the days before) and the trader's quote
# stalls. Once, on the first trading night after the deploy that brings it; after a failure, again on
# other code. It reads the engine's and the trader's databases only, in seconds on a server-sized day;
# five minutes is a bound. Its output holds dates, counts and seconds, no price.
BLAS_THREADS_ISSUE = 589


def _first_bar_close_times(night: Night) -> bool:
    """On the first trading night (after a failure, again on other code)."""
    return night.trading_day and _again_on_other_code(night)


BAR_CLOSE_TIMES = Job(
    name="bar-close-times-589",
    due=_first_bar_close_times,
    command=("bar-close-times",),
    timeout=timedelta(minutes=5),
    targets=(
        Target(
            BLAS_THREADS_ISSUE,
            note=(
                "BLAS·OpenMP 스레드를 하나로 둔 배포(#589) 뒤 첫 거래일의 봉 마감 시간을 앞 거래일 5일과 "
                "견준 것이다. 1절: 날마다 봉 마감의 첫 저장(기준 예측), 최종 합의, 마지막 저장의 초와 "
                "trader의 호가 끊김. 2절: 알고리즘마다 첫 저장 뒤 저장되기까지의 초(ito가 앞 날들보다 "
                "짧아야 한다). 3절: 그날 이벤트 루프를 1초 넘게 쥔 봉 마감(엔진이 이 배포부터 남긴다. 앞 "
                '날들은 Loki: `{service="krx-quant", process="engine"} |= "held the event loop"`). '
                "에이전트가 견준 결과를 이 이슈에 적고, 나아졌으면 닫고 아니면 이슈를 연다. 다시 보기"
                "(읽기만, 엔진 옆에서 돌아도 된다): "
                "`docker compose exec engine krx-quant bar-close-times --day <날>`. "
                "출력에는 시세 값이 없다(날짜, 개수, 초만)."
            ),
        ),
    ),
)
