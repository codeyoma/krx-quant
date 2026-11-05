"""The probe jobs (#500): each measures on the server what an outside source's data means (KIS,
Toss, the KRX Open API, the recordings) before the engine uses it (CODING_STANDARDS.md "External
data is probed before it is used"), or, every night, what the recording kept of it. ``JOBS``
(``krx_quant.scheduled``) lists them in the order they run.

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

# The recording's quality, every trading night (#570, ``recording_quality.py``): the day's
# disconnects in the session by feed, the recorded one-minute bars against KIS's (minutes missing,
# volumes and closes that differ, rows recorded twice), the frames short of fields or not parsed,
# the REST polls' failures and the night futures' recording against the nights before. The command
# reads files and the engine's ``/v1/status``, and imports the probes it reuses (``gaps_probe``,
# ``night_probe``): nothing here imports it, so the collector loads no probe (#552, #558).
#
# It runs right after the trader's report, at about 22:31: the polls' counts are the engine's of
# today, in memory, and are gone at midnight. A server-sized day read in about 6 s on a Mac, at a
# peak of 365 MB resident (``recording_quality.MEMORY_MB`` bounds it); fifteen minutes is a bound,
# short so that the simulations and pre-validations after it keep their starts.
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
