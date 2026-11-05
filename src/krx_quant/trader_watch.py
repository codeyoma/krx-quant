"""Trader Watch: a Telegram alert when the trader goes silent during the session (#542).

The trader (``krx-quant trade``, compose service ``trader``) runs on its own: the collector neither
starts nor stops it, and it trades on while the collector is down (docs/design.md 14장 "프로세스").
It writes its state into its store (``state/trader.sqlite3``'s ``snapshot``) at least every
``limits.HEARTBEAT`` (5 s), and every second while an open position's bid moves. ``GET /v1/trader``'s
``alive`` reads it, but only when someone asks; while the trader is down nobody watches the stop-loss
and the time barrier (docs/operations.md "시작할 때 계좌 맞추기 (#406)").

The collector reads that state's time every ``CHECK_SECONDS``, read-only (``db.reading``). During a
trading day's session, from its open to its close (the collector's market calendar), a state older
than ``SILENT_SECONDS``, or ``HOLDING_SECONDS`` while it shows an open position, raises one alert.
Out of the session (a holiday, before the open, after the close, the 06:00 deploy) it raises none. A
store without a state (a server with no trader) has nothing to watch.

The alert clears once the trader has written again for ``RECOVER_SECONDS``, each state read at most
``store.ALIVE_SECONDS`` old (``alive``'s rule), in the session or out of it. A trader that writes
once and dies again (Docker starting one that crashes again and again, waiting twice as long each
time) stays alerted, rather than told down and back at each start. The silence alerted is kept in
the state folder (``STATE_FILE``) by its last state's time: a restart of the collector while it
lasts does not alert it again, and still tells its end. The trader reads none of this.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from krx_quant.feeds.base import KST, kst_day
from krx_quant.market_calendar import MarketCalendar
from krx_quant.notify import Alerts
from krx_quant.trader.db import reading
from krx_quant.trader.store import ALIVE_SECONDS

log = logging.getLogger(__name__)

CHECK_SECONDS = 5.0  # the trader's heartbeat (``limits.HEARTBEAT``)
# Past this without a new state in the session, the trader is told silent (#542)...
SILENT_SECONDS = 60.0
# ... past this while its last state holds an open position: four heartbeats, and past ``alive``'s
# 15 s, so the Engine API already says it is down.
HOLDING_SECONDS = 20.0
# How long the trader must write again, each state fresh, for its silence to be told over.
RECOVER_SECONDS = 60.0
STATE_FILE = "trader_alert.json"
KEY = "trader:silent"


@dataclass(frozen=True)
class Beat:
    """The trader's state as it last wrote it: when, in which mode, and whether it held an open
    position, of which trade."""

    at: datetime
    mode: str | None
    holding: bool
    trade: int | None


def last_beat(path: Path) -> Beat | None:
    """The trader's last state in its store at ``path``, read-only; None without a store or a
    state. A store that cannot be read raises ``sqlite3.Error``, a state that cannot be read
    ``ValueError``."""
    with reading(path) as db:
        if db is None:
            return None
        known = "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'snapshot'"
        if db.execute(known).fetchone() is None:
            return None
        row = db.execute("SELECT at, body FROM snapshot WHERE id = 1").fetchone()
    if row is None:
        return None
    body = json.loads(row["body"])
    position = body.get("position")
    trade = position.get("trade_id") if isinstance(position, dict) else None
    return Beat(datetime.fromisoformat(row["at"]), body.get("mode"), position is not None, trade)


def alerted(state_dir: Path) -> datetime | None:
    """The last state's time of the silence alerted, as kept in the state folder; None if none is,
    or the file is missing or unreadable."""
    try:
        return datetime.fromisoformat(json.loads((state_dir / STATE_FILE).read_text())["silent_since"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


class TraderWatch:
    """The trader's silence, judged at each ``check`` on its last state."""

    def __init__(self, path: Path, calendar: MarketCalendar, alerts: Alerts, state_dir: Path) -> None:
        self.path = path  # the trader's store
        self._calendar = calendar
        self._alerts = alerts
        self._state_dir = state_dir
        self._silent_since = alerted(state_dir)  # the last state before the silence alerted
        self._back: datetime | None = None  # the first fresh state read since, while it stays fresh
        if self._silent_since is not None:  # alerted before a restart: its end is still told
            alerts.mark(KEY)

    def check(self, now: datetime, beat: Beat | None) -> None:
        """Judge the trader's last state ``beat`` (None: no state) at ``now``."""
        if beat is None:
            return
        if self._silent_since is not None:
            self._recovering(now, beat, self._silent_since)
            return
        session = self._calendar.session(kst_day(now))
        if session is None or not session.open <= now < session.close:
            return
        silent = (now - beat.at).total_seconds()
        if silent <= (HOLDING_SECONDS if beat.holding else SILENT_SECONDS):
            return
        position = "열린 포지션 없음"
        if beat.holding:
            position = "열린 포지션 있음" + ("" if beat.trade is None else f": 매매 {beat.trade}")
        self._alerts.raise_(
            KEY,
            f"{_who(beat)}: trader가 {_span(silent)} 동안 상태를 쓰지 않았습니다"
            f"(마지막 {_when(beat.at, now)}, {position}). trader가 멈췄으면 손절가와 시간 장벽을 아무도 보지"
            " 않습니다. 서버에서 `docker compose ps trader`와 `docker compose logs --tail 50 trader`를 봅니다"
            '(docs/operations.md 3장 "trader 침묵 (#542)").',
        )
        self._silent_since, self._back = beat.at, None
        self._save()

    def _recovering(self, now: datetime, beat: Beat, since: datetime) -> None:
        """While a silence is alerted: told over once the trader has written for ``RECOVER_SECONDS``,
        each state read fresh."""
        if beat.at <= since or (now - beat.at).total_seconds() > ALIVE_SECONDS:
            self._back = None  # silent still, or again
            return
        back = self._back = self._back or beat.at
        if (beat.at - back).total_seconds() < RECOVER_SECONDS:
            return
        self._alerts.resolve(
            KEY,
            f"{_who(beat)}: trader가 다시 상태를 씁니다({_when(back, now)}부터,"
            f" 멈춘 동안 {_span((back - since).total_seconds())}).",
        )
        self._silent_since = self._back = None
        self._save()

    def _save(self) -> None:
        """Keep the silence alerted, or that none is; a state folder that cannot take the file
        alerts the silence again after a restart, which is fine."""
        path = self._state_dir / STATE_FILE
        since = None if self._silent_since is None else self._silent_since.isoformat()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"silent_since": since}))
            tmp.replace(path)
        except OSError as e:
            log.warning("could not keep the trader's silence in %s: %s", path, e)


async def keep_trader_watched(
    watch: TraderWatch, now: Callable[[], datetime], every: float = CHECK_SECONDS
) -> None:
    """Check the trader's state now and every ``every`` seconds until cancelled, its store read in
    a worker thread. The time is taken before the read: a read that waits makes the state look
    newer, never older. A failure is logged once until a check goes through again."""
    failing = False
    while True:
        at = now()
        try:
            watch.check(at, await asyncio.to_thread(last_beat, watch.path))
        except (sqlite3.Error, ValueError) as e:
            if not failing:
                log.warning(
                    "could not read the trader's state in %s (%s): read again every %gs", watch.path, e, every
                )
            failing = True
        except Exception:
            if not failing:
                log.exception("could not check the trader's state")
            failing = True
        else:
            if failing:
                log.info("the trader's state is read again")
            failing = False
        await asyncio.sleep(every)


def _who(beat: Beat) -> str:
    """The alert's head, as the trader's own notices have it: ``자동 매매(<mode>)``."""
    return "자동 매매" if beat.mode is None else f"자동 매매({beat.mode})"


def _when(at: datetime, now: datetime) -> str:
    """``HH:MM:SS``, with ``MM-DD`` in front when it is not ``now``'s day (KST)."""
    local = at.astimezone(KST)
    return local.strftime("%H:%M:%S" if kst_day(local) == kst_day(now) else "%m-%d %H:%M:%S")


def _span(seconds: float) -> str:
    """``N초``, ``N분 M초`` or ``N시간 M분``."""
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}초"
    if s < 3600:
        return f"{s // 60}분 {s % 60}초"
    return f"{s // 3600}시간 {s % 3600 // 60}분"
