"""The evening's memory after the restart after the close (#687): a read-only report for its issue.

The scheduled jobs ``close-restart-memory-687`` and ``close-restart-memory-687-again`` run it with
``--last`` on the first two nights after a day the engine restarted after the close
(``close_restart.py``): that day's evening, whose 21:00–23:59 is over by then. It says whether the
night now starts with more memory than before the restart (2026-11-06 and 2026-11-09, below).

It reads what the engine stored, since the server cannot read Loki:

- the day's restart (``close_restart.RESTART_RUN``: why, and the engine's memory as it stopped) and
  the start after it (``close_restart.START_RUN``: the new engine's memory once its API answered);
- the memory by hour from 15:00 (``memory_hours.py``, from the heartbeat's readings), and 21:00–23:59's
  lowest memory available and most swap, against the two evenings before the restart.

A day without a restart exits 1. The output holds times, MB and counts, no price.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from typing import Any

from krx_quant import memory_hours
from krx_quant.bar_close_times import tonight
from krx_quant.close_restart import RESTART_RUN, START_RUN
from krx_quant.config import Settings
from krx_quant.feeds.base import KST
from krx_quant.predictions import DB_NAME, read_only

FROM_HOUR = 15  # the hours shown: from the close's on
NIGHT_HOURS = (21, 22, 23)  # the night compared: the training's (21:30) and the jobs' start (22:30)
# The evenings before the restart (#687's body, from the heartbeat's readings in Loki): 21:00–23:59's
# lowest memory available and most swap in use, in MB.
BEFORE = (("2026-11-06", "203", "828"), ("2026-11-09", "174(시마다 174–212)", "856"))


def _mb(value: Any) -> str:
    return "?" if value is None else f"{float(value):.0f}"


def _detail(detail: str) -> dict[str, Any]:
    try:
        found = json.loads(detail)
    except ValueError:
        return {}
    return found if isinstance(found, dict) else {}


def restart_days(settings: Settings) -> list[date]:
    """The days the engine restarted after the close on, oldest first; none without a database."""
    with read_only(settings.state_dir / DB_NAME) as db:
        if db is None:
            return []
        rows = db.execute("SELECT at FROM runs WHERE kind = ? ORDER BY at", (RESTART_RUN,)).fetchall()
    return sorted({datetime.fromisoformat(at).astimezone(KST).date() for (at,) in rows})


def last_restart_day(settings: Settings, now: datetime) -> date | None:
    """The latest day before the night ``now`` is in (``tonight``) the engine restarted after the
    close on: its evening is over."""
    earlier = [day for day in restart_days(settings) if day < tonight(now)]
    return earlier[-1] if earlier else None


def close_restart_memory(settings: Settings, day: date | None) -> tuple[str, int]:
    """The report of ``day``, and the exit code: 1 when the engine did not restart after the close
    that day (or no day is given)."""
    title = f"장 마감 뒤 다시 시작 (#687) · {day or '날 없음'}"
    if day is None:
        return f"{title}: 오늘 밤 전에 엔진이 장 마감 뒤 다시 시작한 날이 없다", 1
    start = datetime.combine(day, time(), KST)
    end = start + timedelta(days=1)
    with read_only(settings.state_dir / DB_NAME) as db:
        if db is None:
            return f"{title}: 엔진 DB가 없다", 1
        rows = db.execute(
            "SELECT kind, at, detail FROM runs WHERE kind IN (?, ?) AND at >= ? AND at < ? ORDER BY at, id",
            (RESTART_RUN, START_RUN, start.isoformat(), end.isoformat()),
        ).fetchall()
    if not any(kind == RESTART_RUN for kind, _, _ in rows):
        return f"{title}: 그날 엔진이 장 마감 뒤 다시 시작하지 않았다", 1
    lines = [title, "", "1. 다시 시작과 그 뒤의 시작"]
    restarted = False
    for kind, at, detail in rows:
        when = datetime.fromisoformat(at).astimezone(KST)
        got = _detail(detail or "")
        if kind == RESTART_RUN:
            restarted = True
            lines.append(
                f"- {when:%H:%M:%S} 멈춤: {got.get('why', '?')}; 엔진 RSS {_mb(got.get('rss_mb'))} MB,"
                f" 스왑에 {_mb(got.get('swapped_mb'))} MB"
            )
        elif restarted:
            lines.append(
                f"- {when:%H:%M:%S} 시작(API가 답한 때): 엔진 RSS {_mb(got.get('rss_mb'))} MB,"
                f" 스왑에 {_mb(got.get('swapped_mb'))} MB"
            )
    lines += [
        "",
        f"2. 시간대별 메모리 ({FROM_HOUR}시부터, heartbeat의 1분마다 값)",
        "| 시 | 읽은 수 | 여유 최소 MB | swap 최대 MB | 엔진 RSS 최대 MB | 엔진의 스왑 최대 MB |",
        "|---|---|---|---|---|---|",
    ]
    hours = {
        int(key[11:]): hour
        for key, hour in memory_hours.read(settings.state_dir).items()
        if key[:10] == day.isoformat() and int(key[11:]) >= FROM_HOUR
    }
    for at, hour in sorted(hours.items()):
        lines.append(
            f"| {at}시 | {hour.readings} | {_mb(hour.available_min)} | {_mb(hour.swap_max)}"
            f" | {_mb(hour.engine_max)} | {_mb(hour.engine_swapped_max)} |"
        )
    if not hours:
        lines.append("| 기록 없음 | | | | | |")
    night = [hours[h] for h in NIGHT_HOURS if h in hours]
    available = [h.available_min for h in night if h.available_min is not None]
    swap = [h.swap_max for h in night if h.swap_max is not None]
    lines += [
        "",
        f"3. 21–23시 ({sum(h.readings for h in night)}번 읽음): 여유 최소"
        f" {_mb(min(available) if available else None)} MB, swap 최대 {_mb(max(swap) if swap else None)} MB",
        "   견줄 값(다시 시작 전, 21–23시): "
        + "; ".join(f"{d} 여유 최소 {a} MB·swap 최대 {s} MB" for d, a, s in BEFORE),
    ]
    return "\n".join(lines), 0
