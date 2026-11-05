"""The probe jobs (#500): each measures on the server what an outside source's data means (KIS,
Toss, the KRX Open API, the recordings) before the engine uses it (CODING_STANDARDS.md "External
data is probed before it is used"), or what the server's own files are, when a fix waits for that.
``JOBS`` (``krx_quant.scheduled``) lists them in the order they run.

The sixteen probes and checks up to ``probe-member-frames-381`` ran once, posted their results and
left ``JOBS`` (#505), as a one-off job does once it has done its work, so that a lost state file
cannot run them again. Most of their commands stay for a run by hand (``krx-quant probe-supply`` and
the rest); those whose questions were settled went with their modules (#563). A new probe goes here,
with its condition (``core._first_night`` and ``core._first_trading_night`` are the usual ones).
"""

from __future__ import annotations

from datetime import timedelta

from krx_quant.scheduled.core import FAILED, OK, Job, Night, Target

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
