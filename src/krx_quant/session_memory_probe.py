"""What the engine's memory did after a day's close (#623): a read-only report for its issue.

The scheduled job ``session-memory-623`` runs it on the first trading night after the deploy that
brings the engine's rest (``garbage.Garbage.rest``), with ``--tonight``: of that night's day. The
decision it is for: whether letting go of the session's memory is enough on the server, or the
engine is to restart after the close (docs/operations.md "장 마감 뒤 메모리 (#623)").

It reads what the engine stored, since the server cannot read Loki:

- the engine's rests of the day (``garbage.REST_RUN`` in the store's ``runs``): what each let go
  of and kept, the engine's resident memory before and after, what its allocators held;
- the memory by hour from 15:00 on (``memory_hours.py``, from the heartbeat's readings): the lowest
  available, the most swap, the engine's largest resident memory and most in swap;
- the server's memory now (``/proc/meminfo``).

A day without a rest (the engine was down at the close, a deploy after it) exits 1, and the job
runs again the next trading night. The output holds times, counts, MB and seconds, no price.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from typing import Any

from krx_quant import footprint, memory_hours
from krx_quant.config import Settings
from krx_quant.feeds.base import KST
from krx_quant.garbage import REST_RUN
from krx_quant.predictions import DB_NAME, read_only

FROM_HOUR = 15  # the hours shown: from the close's on


def _mb(value: Any) -> str:
    return "?" if value is None else f"{float(value):.0f}"


def _counts(counts: Any) -> str:
    if not isinstance(counts, dict) or not counts:
        return "없음"
    return ", ".join(f"{name} {n}" for name, n in counts.items())


def _rest_line(at: str, detail: str) -> str:
    try:
        rest = json.loads(detail)
    except ValueError:
        return f"- {at}: 읽을 수 없는 기록"
    when = datetime.fromisoformat(at).astimezone(KST)
    got = rest.get
    return (
        f"- {when:%H:%M:%S} {got('after')}: 놓음 {_counts(got('let_go'))}; 남김 {_counts(got('kept'))};"
        f" 엔진 RSS {_mb(got('rss_before_mb'))} → {_mb(got('rss_mb'))} MB,"
        f" 스왑에 {_mb(got('swapped_mb'))} MB;"
        f" python {got('python_blocks')} 블록, malloc 사용 {_mb(got('malloc_mb'))} MB·빈 것"
        f" {_mb(got('malloc_free_mb'))} MB, arrow {_mb(got('arrow_mb'))} MB; {got('seconds')}초"
    )


def session_memory(settings: Settings, now: datetime, day: date | None = None) -> tuple[str, int]:
    """The report of ``day`` (default: today), and the exit code: 1 when the engine stored no rest
    that day."""
    day = day or now.astimezone(KST).date()
    start = datetime.combine(day, time(), KST)
    end = start + timedelta(days=1)
    title = f"장 마감 뒤 메모리 (#623) · {day}"
    with read_only(settings.state_dir / DB_NAME) as db:
        if db is None:
            return f"{title}: 엔진 DB가 없다", 1
        rests = db.execute(
            "SELECT at, detail FROM runs WHERE kind = ? AND at >= ? AND at < ? ORDER BY at, id",
            (REST_RUN, start.isoformat(), end.isoformat()),
        ).fetchall()
    if not rests:
        return f"{title}: 그날 엔진이 쉰 기록이 없다", 1
    lines = [title, "", f"1. 엔진이 쉰 기록 ({len(rests)}번)"]
    lines += [_rest_line(at, detail or "") for at, detail in rests]
    lines += [
        "",
        f"2. 시간대별 메모리 ({FROM_HOUR}시부터, heartbeat의 1분마다 값)",
        "| 시 | 읽은 수 | 여유 최소 MB | swap 최대 MB | 엔진 RSS 최대 MB | 엔진의 스왑 최대 MB |",
        "|---|---|---|---|---|---|",
    ]
    first = f"{day.isoformat()}T{FROM_HOUR:02d}"
    hours = {key: hour for key, hour in memory_hours.read(settings.state_dir).items() if key >= first}
    for key, hour in hours.items():
        lines.append(
            f"| {key[5:10]} {key[11:]}시 | {hour.readings} | {_mb(hour.available_min)} | {_mb(hour.swap_max)}"
            f" | {_mb(hour.engine_max)} | {_mb(hour.engine_swapped_max)} |"
        )
    if not hours:
        lines.append("| 기록 없음 | | | | | |")
    available, swap = footprint.server_mb()
    lines += [
        "",
        f"3. 지금 ({now.astimezone(KST):%m-%d %H:%M}): 여유 {_mb(available)} MB, swap {_mb(swap)} MB",
    ]
    return "\n".join(lines), 0
