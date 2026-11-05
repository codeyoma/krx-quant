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

from krx_quant.night_book import NIGHTS, VERDICT_DAY
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


# The night futures' book (#605, ``night_book.py``): the bytes a night of ``H0MFASP0`` takes, raw and
# parsed as the merged files hold them, over seven complete nights from 2026-11-06's evening; then, on
# 2026-11-20, the rule set in advance: a median over 100 MiB a night and no live model reading the book
# stop its subscription (in a PR of its own), anything else keeps it. The night trades and what was
# recorded stay either way. The command reads the two days' partitions of each night (their received
# times and the files' Parquet metadata, not the frames) and writes only its state file, whose count of
# complete nights the measure's condition reads (``Facts.night_book_nights``).
#
# The measure runs each trading night right after the recording's quality, so the night that ended the
# morning before was merged at 21:00 (``archive.NIGHTLY_ARCHIVE``), until it has its seven complete
# nights or the verdict ran. Each run posts the whole table again on #605 as a record: the issue gets
# ``ready-for-agent`` only when the run failed, and no label is taken off. The verdict runs once, on the
# night of 2026-11-20 (the first after it if that one is missed; after a failure, again on other code),
# and labels the issue for the agent that acts on it. Both re-measure from the files, about ten nights'
# timestamps, in seconds; fifteen minutes is a bound, short so that the jobs after them keep their starts.
NIGHT_BOOK_ISSUE = 605
_NIGHT_BOOK_VERDICT = "night-book-verdict-605"


def _until_seven_complete_nights(night: Night) -> bool:
    """Each trading night, once, until the measure has its complete nights or the verdict ran; a run a
    restart cut runs the next trading night."""
    if night.facts.night_book_nights >= NIGHTS:
        return False
    verdict = night.records.get(_NIGHT_BOOK_VERDICT)
    if verdict is not None and verdict.outcome == OK:
        return False
    return _each_trading_night(night)


def _failed(ran: Ran) -> bool:
    return ran.exit_code != 0 or ran.timed_out


def _on_the_verdict_day(night: Night) -> bool:
    """Once, on the verdict's night or the first after it (after a failure, again on other code)."""
    return night.day >= VERDICT_DAY and _again_on_other_code(night)


NIGHT_BOOK_BYTES = Job(
    name="night-book-bytes-605",
    due=_until_seven_complete_nights,
    command=("night-book-bytes",),
    timeout=timedelta(minutes=15),
    label_if=_failed,
    unlabel=(),  # a record: a question an agent put to the user on the issue (``ready-for-human``) stays
    targets=(
        Target(
            NIGHT_BOOK_ISSUE,
            note=(
                "야간 호가(H0MFASP0) 기록량(#605): 2026-11-06 저녁부터 거래일 밤마다 raw와 parsed(events)의"
                " 합친 Parquet 바이트, 행 수, 18:00–06:00의 가장 긴 침묵, 완전한 밤인지. 완전한 밤 7개가 차면"
                " 더 돌지 않는다. 기록이라 실패한 밤에만 `ready-for-agent`를 붙인다. 판정은 2026-11-20 밤"
                " `night-book-verdict-605`가 올린다. 다시 보기(상태 파일 `state/night-book-605.json`만 쓴다):"
                " `docker compose run --rm engine night-book-bytes`. 출력에는 시세 값이 없다(바이트, 행 수,"
                " 시각만)."
            ),
        ),
    ),
)

NIGHT_BOOK_VERDICT = Job(
    name=_NIGHT_BOOK_VERDICT,
    due=_on_the_verdict_day,
    command=("night-book-bytes", "--verdict"),
    timeout=timedelta(minutes=15),
    targets=(
        Target(
            NIGHT_BOOK_ISSUE,
            note=(
                "야간 호가(H0MFASP0) 판정(#605): 완전한 밤 7개의 raw+parsed 합 중앙값이 100 MiB를 넘고"
                " 이 호가를 읽는 실전 모델이 없으면 구독을 멈춘다. 그렇지 않으면 계속 기록한다."
                " 야간 체결(H0MFCNT0)과 지난 기록은 어느 경우든 그대로 둔다. `결론:` 줄이 규칙의 결론이고"
                " 그다음 줄이 할 일이다. 멈추는 변경은 에이전트가 따로 PR로 한다(docs/operations.md"
                ' "야간 호가 기록량과 판정 (#605)"). 다시 돌리기:'
                " `docker compose run --rm engine night-book-bytes --verdict`. 출력에는 시세 값이 없다"
                "(바이트, 행 수, 시각, 모듈 이름만)."
            ),
        ),
    ),
)


# The inverse ETF's KRX book (#606, ``inverse_book_probe.py``): the collector registers 0193L0's
# ``H0STASP0`` in the session's last slot and records it raw only. Before the replay of down calls
# through the ETF is built on it, the frames' layout, times and best quotes, the gaps and what
# recording them costs (#606's bounds: 32 MiB, 5% of a core) are read on the first trading night
# after the deploy that brings it. The command reads files and writes only to a temporary directory;
# a server-sized day of the integrated book (about 100,000 records) is read in seconds, three times.
# Again after a failure on other code: exit 3 says no frame came, which a PR changing the
# registration answers.
INVERSE_BOOK_ISSUE = 606


def _first_inverse_book(night: Night) -> bool:
    """On the first trading night (after a failure, again on other code)."""
    return night.trading_day and _again_on_other_code(night)


INVERSE_BOOK = Job(
    name="probe-inverse-book-606",
    due=_first_inverse_book,
    command=("probe-inverse-book",),
    timeout=timedelta(minutes=20),
    targets=(
        Target(
            INVERSE_BOOK_ISSUE,
            note=(
                "인버스 ETF(0193L0)의 KRX 호가(`H0STASP0`) 확인(#606): 1절 구독 응답과 세션 한도로 "
                "거절된 구독, 2절 프레임(레코드당 필드 수, KIS 예제는 63개, 2026-09-28 전 59개), 3절 "
                "필드별 빈 값·0과 코드 값, 4절 시각, 5절 연속매매의 1호가(스프레드 틱, 5초보다 오래된 "
                "호가), 6절 결측, 7절 기록 부담(디스크, CPU, 버퍼 메모리를 #606의 기준과). 에이전트가 "
                '이 결과로 이벤트 기록과 재생 보고를 만든다(`docs/operations.md` "인버스 ETF 호가 '
                '확인 (#606)"). 종료 코드 3은 그날 프레임이 없었다는 뜻이다. 다시 돌리기(읽기만): '
                "`docker compose run --rm engine probe-inverse-book --day <날>`. 출력에는 시세 값이 "
                "없다(개수, 비율, 초, 틱, 필드 이름, 코드만)."
            ),
        ),
    ),
)
