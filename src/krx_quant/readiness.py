"""The readiness line: a trading morning's routine notices as one Telegram line (#614).

Before it, a trading day's Telegram had about six routine notices: the 06:00 deploy (its commit, its
changes, the disk), the 08:00 model swap (every model it installed, with its version), a line for
each scheduled job's run, and the night's training summary, whose promotions the 08:00 swap told
again. The deploy, the swap and the jobs now send nothing of their own when all is well. At
``READY_AT`` (08:40) on each trading day the collector sends one line instead (``Readiness``):

    준비 11-06: 배포 1a2b3c4 → 5d6e7f8 · 모델 11-05 밤 적용, 18개 바뀜 · 매매(mock): 연습만(barrier
    turing) · 한도 여유 하루 50,000원, 누적 96,500원 · 예약 작업 9개, 실패 1(prevalidate-ulam)

- **배포**: the commit the engine runs (``KRX_COMMIT``), after the one the last line told when they
  differ: a deploy since, those of a weekend told on the Monday.
- **모델**: the morning swap (``swap.MorningSwap``): the night whose list it applied and how many
  models it changed (the night's summary named them the night before), or why the list does not run,
  or that there is none. A night the last line told already is 그대로: no night trained since.
- **매매**: the trader's mode and the champion's pick of the day (08:30, ``trader.PICK_AT``): the cells
  it trades, or 연습만 and the cell practised (#427), or none and why; the trader down when its state
  is older than ``trader.store.ALIVE_SECONDS``.
- **한도 여유**: what the daily and the cumulative loss limits leave (``GET /v1/trader``'s ``limits``),
  and the trader's status when it does not buy.
- **예약 작업**: the scheduled jobs' runs no line that went has told (the scheduler tells each run's end,
  ``Readiness.ran``, kept in ``STATE_FILE`` until a line that told it has gone: the scheduler's own
  state keeps a job's last run alone), the failed ones named (a pre-validation's refusal is a verdict,
  not a failure), the ones a restart cut (끊김: they run again the next night), those running and the
  results not on their issues yet (``state/scheduled.json``). Each result is on its issue as before.

It is a report: its ``alert:`` line is INFO (``Alerts.deliver``, as the night's summary's, #580), and
the ``telegram sent:`` line is the same as every message's (#358). It goes once a trading day: the
day is kept in the state folder (``STATE_FILE``), so a restart after it does not send it again. It
waits for the day's swap until the open (an engine started at 08:39 swaps half a minute after it
starts). An engine started after the open sends none that day; the next line tells the runs this one
would have.

It holds nothing back: every problem is still sent at once (docs/operations.md 6장 "텔레그램 알림이 가는
길"): the trader's notices (loss limits, failed orders, the broker's last check withholding one,
pauses, the live mode), the trader's silence, the night's training failure, the deploy's failures,
the KIS WebSocket's drops in the session.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from krx_quant.clock import SessionClock
from krx_quant.feeds.base import KST, kst_day
from krx_quant.market_calendar import MarketCalendar
from krx_quant.scheduled.core import FAILED, INTERRUPTED, REFUSED, RUNNING, JobRecord
from krx_quant.scheduled.core import STATE_FILE as JOBS_FILE
from krx_quant.swap import MorningSwap
from krx_quant.trader.db import reading
from krx_quant.trader.store import ALIVE_SECONDS

log = logging.getLogger(__name__)

READY_AT = time(8, 40)  # after the 08:00 swap and the trader's 08:30 pick, before the open
STATE_FILE = "readiness.json"
NAMED = 3  # the failed jobs named at most; the rest counted
KEPT = 200  # the runs not told yet kept at most: a line that never goes lets them grow no further
_SHORT = 7  # a commit's characters told

# The trader's status (``GET /v1/trader``'s ``status``) when it does not buy as it would.
_STATUS = {
    "entries_paused": "새 진입 정지",
    "paused": "정지",
    "order_failed": "주문 실패로 오늘 사지 않음",
    "daily_limit": "하루 한도로 멈춤",
    "cumulative_limit": "누적 한도로 멈춤",
}
# Why the champion's pick trades no cell (``trader.selection``, ``trader.policy``).
_NO_CELL = {
    "no_pass": "통과한 칸 없음",
    "no_cost_map": "비용 지도를 읽지 못함",
    "not_in_policy": "챔피언 정책의 칸 없음",
    "no_champion": "챔피언 없음",
    "no_policy": "챔피언 정책을 읽지 못함",
}


@dataclass(frozen=True)
class Told:
    """The last line, as kept in ``STATE_FILE``: the day it was for (sent or not), and of the last one
    that went, when, the commit and the night it told; and the scheduled jobs' runs no line that went
    has told yet (``Readiness.ran``), oldest first, ``KEPT`` at most."""

    day: date | None = None
    sent_at: datetime | None = None
    commit: str | None = None
    night: date | None = None
    runs: tuple[dict[str, Any], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "day": None if self.day is None else self.day.isoformat(),
            "sent_at": None if self.sent_at is None else self.sent_at.isoformat(),
            "commit": self.commit,
            "night": None if self.night is None else self.night.isoformat(),
            "runs": list(self.runs),
        }

    @classmethod
    def load(cls, path: Path) -> Told:
        """As kept; nothing told when there is no file, or one that cannot be read (warned): then the
        day's line may go again, which is fine."""
        try:
            data = json.loads(path.read_text())
            return cls(
                _day(data.get("day")),
                None if data.get("sent_at") is None else datetime.fromisoformat(data["sent_at"]),
                None if data.get("commit") is None else str(data["commit"]),
                _day(data.get("night")),
                tuple(dict(r) for r in data.get("runs") or () if isinstance(r, Mapping)),
            )
        except FileNotFoundError:
            return cls()
        except (OSError, ValueError, TypeError, AttributeError) as e:
            log.warning("could not read %s (%s): the readiness line starts afresh", path, e)
            return cls()

    def save(self, path: Path) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.as_dict()))
            tmp.replace(path)
        except OSError as e:
            log.warning("could not keep the readiness line in %s: %s", path, e)


def _day(value: Any) -> date | None:
    return None if value is None else date.fromisoformat(str(value))


class Readiness:
    """The collector's readiness line: told the morning swap's outcome (``swapped``), it sends the
    line of a trading day (``send``) through ``send_line`` (``Alerts.deliver`` at INFO: whether it
    went). ``swaps``: whether a morning swap is to be waited for (none without an object store)."""

    def __init__(
        self,
        state_dir: Path,
        trader_db: Path,
        commit: str,
        send_line: Callable[[str], Awaitable[bool]],
        *,
        swaps: bool = True,
    ) -> None:
        self._path = state_dir / STATE_FILE
        self._jobs = state_dir / JOBS_FILE
        self._trader = trader_db
        self._commit = commit
        self._send = send_line
        self._swaps = swaps
        self._swap: MorningSwap | None = None
        self._changed = asyncio.Event()
        self.told = Told.load(self._path)

    def swapped(self, outcome: MorningSwap) -> None:
        """The morning swap's outcome (``swap.keep_models``)."""
        self._swap = outcome
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()

    def ran(self, name: str, record: JobRecord) -> None:
        """A scheduled job's run ended, or was found cut (``Scheduler``'s ``ran``): kept in the state
        folder until a line that tells it has gone (#614). The scheduler's state keeps a job's last run
        alone, so a failure followed by a success before the next line went would be lost there."""
        if record.outcome == RUNNING:
            return
        run = {
            "job": name,
            "day": record.day.isoformat(),
            "started_at": record.started_at.isoformat(),
            "outcome": record.outcome,
            "verdict": record.verdict,
        }
        self.told = replace(self.told, runs=(*self.told.runs, run)[-KEPT:])
        self.told.save(self._path)

    async def wait_for_swap(self, day: date, until: datetime, clock: SessionClock) -> None:
        """Until the swap of ``day`` is told, or ``until`` (the open) comes."""
        if self._swaps and (self._swap is None or self._swap.day != day):
            log.info("the readiness line of %s waits for the morning swap until %s", day, f"{until:%H:%M}")
        while self._swaps and (self._swap is None or self._swap.day != day):
            if clock.now() >= until:
                log.warning("no morning swap of %s by the open: the readiness line goes without it", day)
                return
            changed = asyncio.ensure_future(self._changed.wait())
            timer = asyncio.ensure_future(clock.sleep_until(until))
            try:
                await asyncio.wait({changed, timer}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                changed.cancel()
                timer.cancel()

    def line(self, day: date, now: datetime, runs: Sequence[Mapping[str, Any]] | None = None) -> str:
        """The line of ``day`` at ``now``, telling the jobs' ``runs`` (by default those not told yet);
        it reads the trader's store and the jobs' state (a worker thread's work)."""
        swap = self._swap if self._swap is not None and self._swap.day == day else None
        parts = [
            commit_said(self._commit, self.told.commit),
            model_said(swap, self.told.night, swaps=self._swaps),
            *trader_said(self._trader, now),
            jobs_said(self.told.runs if runs is None else runs, self._jobs),
        ]
        return f"준비 {day:%m-%d}: " + " · ".join(parts)

    async def send(self, day: date, now: datetime) -> bool:
        """Send the line of ``day``, once: whether it went. The day is kept either way; the commit,
        the night, the time, and that the runs it told are told, only when it went: the next line tells
        what this one could not (a run that ended meanwhile waits for it too)."""
        told_runs = self.told.runs
        text = await asyncio.to_thread(self.line, day, now, told_runs)
        went = await self._send(text)
        swap = self._swap if self._swap is not None and self._swap.day == day else None
        told = self.told  # with the runs that ended while it was sent
        if went:
            night = swap.night if swap is not None and swap.night is not None and swap.applied else told.night
            left = tuple(r for r in told.runs if r not in told_runs)
            self.told = Told(day, now, self._commit, night, left)
        else:
            self.told = replace(told, day=day)
        self.told.save(self._path)
        return went


def commit_said(commit: str, told: str | None) -> str:
    """The commit the engine runs, after the one the last line told when it differs."""
    now = commit[:_SHORT]
    if told is None:
        return f"커밋 {now}"
    if told == commit:
        return f"커밋 {now} 그대로"
    return f"배포 {told[:_SHORT]} → {now}"


def model_said(swap: MorningSwap | None, told: date | None, *, swaps: bool = True) -> str:
    """The morning swap's outcome; ``told``: the night the last line told applied."""
    if not swaps:
        return "모델 교체 없음(저장소 없음)"
    if swap is None:
        return "모델 아침 교체를 모름"
    if swap.night is None:
        return "모델 밤 학습 목록 없음"
    night = f"{swap.night:%m-%d} 밤"
    if not swap.applied:
        return f"모델 {night} 쓰지 않음({swap.reason})"
    if swap.night == told:
        return f"모델 그대로({night})"
    if not swap.changed:
        return f"모델 {night} 적용, 바뀐 것 없음"
    return f"모델 {night} 적용, {len(swap.changed)}개 바뀜"


def trader_said(path: Path, now: datetime) -> list[str]:
    """The trader's part: its mode and the champion's pick of the day, then its limits' room; one part
    without a store, a state, or one that cannot be read."""
    try:
        with reading(path) as db:
            if db is None:
                return ["trader 없음"]
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            row = None
            if "snapshot" in tables:
                row = db.execute("SELECT at, body FROM snapshot WHERE id = 1").fetchone()
            if row is None:
                return ["trader 상태 없음"]
            updated = datetime.fromisoformat(row["at"])
            body = json.loads(row["body"])
            mode = str(body.get("mode") or "?")
            champion = _champion(db, tables)
            pick = _pick(db, mode, kst_day(now), champion) if "selections" in tables else None
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError) as e:  # the line goes without it
        log.warning("could not read the trader's store for the readiness line: %s", e)
        return ["trader를 읽지 못함"]
    trading = f"매매({mode}): {cells_said(pick)}"
    if abs((now - updated).total_seconds()) > ALIVE_SECONDS:
        trading = f"trader 멈춤(마지막 상태 {updated.astimezone(KST):%m-%d %H:%M}) · {trading}"
    return [trading, limits_said(body)]


def _champion(db: sqlite3.Connection, tables: set[str]) -> int | None:
    if "champions" not in tables:
        return None
    row = db.execute("SELECT policy_id FROM champions ORDER BY id DESC LIMIT 1").fetchone()
    return None if row is None else int(row[0])


def _pick(db: sqlite3.Connection, mode: str, day: date, champion: int | None) -> dict[str, Any] | None:
    """The champion's last pick of ``day`` in ``mode`` (the trader's), as ``report._pick`` reads it."""
    row = db.execute(
        "SELECT * FROM selections WHERE mode = ? AND substr(at, 1, 10) = ?"
        " AND (policy_id IS NULL OR policy_id = ?) ORDER BY id DESC LIMIT 1",
        (mode, day.isoformat(), -1 if champion is None else champion),
    ).fetchone()
    if row is None:
        return None
    pick = dict(row)
    pick["picks"] = json.loads(pick["picks"])
    return pick


def cells_said(pick: Mapping[str, Any] | None) -> str:
    """The cells the champion's pick trades: 연습만 and the cell when it practises (#427), none and why."""
    if pick is None:
        return "오늘 고르기 없음"
    picks = [p for p in pick.get("picks") or [] if isinstance(p, Mapping)]
    cells = [f"{p.get('kind')} {p.get('predictor')}" for p in picks]
    if pick.get("practice"):
        return f"연습만({', '.join(cells)})" if cells else "연습만"
    if cells:
        return ", ".join(cells)
    reason = pick.get("reason")
    return f"칸 없음({_NO_CELL.get(str(reason), reason)})" if reason else "칸 없음"


def limits_said(body: Mapping[str, Any]) -> str:
    """What the daily and cumulative loss limits leave, and the trader's status when it does not buy."""
    limits = body.get("limits")
    rooms = []
    for key, name in (("daily", "하루"), ("cumulative", "누적")):
        limit = limits.get(key) if isinstance(limits, Mapping) else None
        won = limit.get("won") if isinstance(limit, Mapping) else None
        if not isinstance(won, int | float) or isinstance(won, bool):
            rooms.append(f"{name} 모름")
            continue
        used = limit.get("used") if isinstance(limit, Mapping) else None
        share = float(used) if isinstance(used, int | float) and not isinstance(used, bool) else 0.0
        rooms.append(f"{name} {max(0.0, float(won) * (1.0 - share)):,.0f}원")
    said = "한도 여유 " + ", ".join(rooms)
    status = _STATUS.get(str(body.get("status")))
    return f"{said}({status})" if status else said


def jobs_said(runs: Sequence[Mapping[str, Any]], path: Path) -> str:
    """The scheduled jobs' ``runs`` no line has told yet: how many, the failed ones named, those a
    restart cut; then from their state at ``path`` those still running and the results not posted on
    their issues yet, read as the scheduler last wrote it, never set aside (``JobState.load`` does that
    for the scheduler alone)."""
    failed = [str(r.get("job")) for r in runs if r.get("outcome") == FAILED and r.get("verdict") != REFUSED]
    cut = [str(r.get("job")) for r in runs if r.get("outcome") == INTERRUPTED]
    if not runs:
        said = "예약 작업 없음"
    elif failed or cut:
        said = f"예약 작업 {len(runs)}개"
        if failed:
            said += f", 실패 {len(failed)}({_named(failed)})"
        if cut:
            said += f", 끊김 {len(cut)}({_named(cut)})"
    else:
        said = f"예약 작업 {len(runs)}개, 실패 없음"
    try:
        data = json.loads(path.read_text())
        jobs = data.get("jobs") or {}
        unposted = len(data.get("unposted") or [])
        running = sum(1 for r in jobs.values() if isinstance(r, Mapping) and r.get("outcome") == RUNNING)
    except FileNotFoundError:
        return said
    except (OSError, ValueError, TypeError, AttributeError) as e:
        log.warning("could not read %s for the readiness line: %s", path, e)
        return said + ", 예약 작업 상태를 읽지 못함"
    if running:
        said += f", 도는 중 {running}"
    if unposted:
        said += f", 이슈에 못 올린 결과 {unposted}개"
    return said


def _named(names: list[str]) -> str:
    """The first ``NAMED`` names, a name failed more than once with its count, the rest counted."""
    counts: dict[str, int] = {}
    for name in names:
        counts[name] = counts.get(name, 0) + 1
    said = [f"{n} ×{k}" if k > 1 else n for n, k in counts.items()]
    return ", ".join(said[:NAMED]) + (f" 외 {len(said) - NAMED}개" if len(said) > NAMED else "")


async def keep_ready(ready: Readiness, calendar: MarketCalendar, clock: SessionClock) -> None:
    """Send each trading day's line at ``READY_AT``, until cancelled: once (``Told.day``), before the
    open, after the day's swap (``Readiness.wait_for_swap``)."""
    day = kst_day(clock.now())
    while True:
        session = calendar.session(day)
        if session is not None and ready.told.day == day:
            log.info("the readiness line of %s was tried before this start: not again", day)
        elif session is not None:
            await clock.sleep_until(datetime.combine(day, READY_AT, KST))
            if clock.now() < session.open:
                await ready.wait_for_swap(day, session.open, clock)
                try:
                    await ready.send(day, clock.now())
                except Exception:
                    log.exception("could not send the readiness line of %s", day)
            else:
                log.info("the readiness line of %s is not sent: the engine started after the open", day)
        day += timedelta(days=1)
        await clock.sleep_until(datetime.combine(day, time(0), KST))
