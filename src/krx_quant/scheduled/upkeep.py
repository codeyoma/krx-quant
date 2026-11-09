"""The server's upkeep jobs (#576): the monthly restore drill of the nightly state backup.
``JOBS`` (``krx_quant.scheduled``) lists them in the order they run.
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

from krx_quant.process_log import Ran
from krx_quant.scheduled.core import FAILED, OK, Job, Night, Reads, Target
from krx_quant.state_backup import last_backup

# The restore drill (#576, ``state_backup.drill``): the collector backs the server's state up each
# night at 05:35 (``state_backup.BACKUP_AT``: the engine and trader databases, the pre-validations,
# the scheduled jobs' state, the models and the experiments' reports their writers name (#604), one
# encrypted object in the store), and once a month this job downloads the latest backup into a
# scratch folder of the state folder and checks it: the object's SHA-256 against the store's, each
# file's against the backup's list, ``PRAGMA integrity_check`` on each database, its tables' row
# counts against those counted at the backup (beside the live database's now), each experiment report
# read as JSON and none refused at the backup (#604), and that the backup is no older than two days.
# Counts, sizes, times and ok or fail only. On the first night of a month (trading or not) once this
# server has made a backup (``read_last_backup``: a new server's first night has none yet), and
# seven nights after a drill that failed, until one passes. Short: before the pre-validations, which
# the night is for. A drill that fails calls an agent (``label_if``); one that passes is a record and
# changes no label, and no label is taken off: a question an agent left for the user stays. A few
# minutes for a database of a gigabyte or two; half an hour is a bound.
RESTORE_DRILL_ISSUE = 577
AGAIN_AFTER_FAILURE = timedelta(days=7)


async def read_last_backup(reads: Reads) -> date | None:
    """The day of the last backup of the server's state this server made (``state_backup.MARK``)."""
    return await asyncio.to_thread(last_backup, reads.sources.settings.state_dir)


def _a_month_since_the_last_drill(night: Night[date | None]) -> bool:
    """Once the server has made a backup: on the first night of each month, and seven nights after
    a drill that failed; a run a restart cut runs the next night."""
    if night.facts is None:
        return False
    record = night.record
    if record is None or record.outcome not in (OK, FAILED):
        return True
    if (record.day.year, record.day.month) != (night.day.year, night.day.month):
        return record.day < night.day
    return record.outcome == FAILED and night.day >= record.day + AGAIN_AFTER_FAILURE


def restore_drill_calls(ran: Ran) -> bool:
    """Whether a drill calls an agent: it failed (a check, or the run)."""
    return ran.exit_code != 0 or ran.timed_out


RESTORE_DRILL = Job(
    name="restore-drill-577",
    due=_a_month_since_the_last_drill,
    facts=read_last_backup,
    command=("restore-drill",),
    timeout=timedelta(minutes=30),
    label_if=restore_drill_calls,
    unlabel=(),
    targets=(
        Target(
            RESTORE_DRILL_ISSUE,
            note=(
                "서버 상태 백업의 월간 복원 시험(#576): 가장 최근 밤 백업(`state-backup/state.tar.gz`)을 "
                "상태 폴더의 임시 폴더에 받아 풀고, 받은 객체의 SHA-256을 저장소의 것과, 파일마다 SHA-256을 "
                "백업의 목록과 견주고, DB마다 `PRAGMA integrity_check`를 돌리고, 표마다 행 수를 백업 때 센 "
                "수와 견준다(지금 도는 DB의 수는 옆에 보이기만 한다). 허용 목록의 실험 파일(#604)은 수와 "
                "JSON 파싱 ok·fail을 보고, 백업이 거부한 실험 파일이 있으면 실패다. 백업이 2일보다 "
                "오래되면 실패다. "
                "`ok`면 기록이고 라벨을 바꾸지 않는다. `fail`이면 `ready-for-agent`를 붙인다: 에이전트는 "
                '`문제:` 줄을 보고 `docs/operations.md` 6장 "서버 상태 백업과 복원 (#576)"대로 원인을 '
                "찾는다. 실패한 뒤 7일째 밤에 다시 돈다. 다시 돌리기(읽기만, 상태 폴더를 바꾸지 않는다): "
                "`docker compose run --rm engine restore-drill`. 출력에는 개수, 크기, 시각과 ok·fail만 "
                "있다(시세 값, 계좌번호, 주문번호, 키는 없다)."
            ),
        ),
    ),
)
