"""REST polls' outages (#441): each poll job's calls and failures, by feed and key.

An outage of a key starts at a failed call after a success (or at the first call) and ends at
the next success. How long it took to recover is the time from its first failure to that
success. A failure is kept as its kind only (``error_kind``): KIS's code (``msg_cd``), else the
HTTP status, else the error's type; never the answer's text.

This only counts. A failed call is still asked again on the next interval, and nothing is sent
again or re-issued because of it (``polling.Poller``, ``kis.RestClient``).

An endpoint (TR ID) is alerted once when one of its keys has failed ``SUSTAINED_FAILURES`` times
in a row over ``SUSTAINED_SECONDS`` or more, and its recovery once every key of it has answered
again. Today's (KST) counts start from zero each day and when the engine starts; an outage going
on at midnight counts in the new day too.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from krx_quant.feeds.base import kst_day
from krx_quant.kis import KisError
from krx_quant.notify import Alerts

log = logging.getLogger(__name__)

Key = tuple[str, str]  # (tr_id, key), as ``health`` keys them

# An outage alerted: this many failures in a row, over at least this long. On 2026-10-31 the
# investor estimates (every 300 s) failed 41 times on three symbols (OPSQ1002, EGW00316), mostly
# once or twice in a row; their longest run was 4 calls over 15 minutes. The floor in seconds keeps
# a poll asked every few seconds (the VI status) from alerting on a minute's trouble.
SUSTAINED_FAILURES = 3
SUSTAINED_SECONDS = 600.0
ALERT_PREFIX = "poll:"  # the alert's key: poll:<tr_id>

_KIS_CODE = re.compile(r"[A-Z0-9]{1,16}")


def error_kind(error: BaseException) -> str:
    """A failed call's kind: KIS's code, else ``HTTP <status>``, else the error's type."""
    if isinstance(error, KisError):
        if error.code is not None and _KIS_CODE.fullmatch(error.code):
            return error.code
        if error.status is not None:
            return f"HTTP {error.status}"
    return type(error).__name__


@dataclass(frozen=True)
class PollState:
    """A polled key's calls today (KST) and its outage, as ``/v1/status``'s ``kis.polls`` shows it."""

    feed: str
    key: str
    attempts: int  # calls answered or failed today
    failures: int
    errors: dict[str, int]  # today's failures by kind (``error_kind``)
    outages: int  # outages going on at some time today
    consecutive_failures: int  # since the last success: 0 once one comes
    max_consecutive_failures: int  # today's longest run
    outage_since: datetime | None  # the first failure of the outage going on
    last_success_at: datetime | None
    last_recovery_seconds: float | None  # the last ended outage, first failure to the success


@dataclass
class _Poll:
    day: date  # the KST day the counts are for
    attempts: int = 0
    failures: int = 0
    errors: Counter[str] = field(default_factory=Counter)
    outages: int = 0
    max_in_a_row: int = 0
    # The outage going on: its failures so far, when the first came and their kinds.
    in_a_row: int = 0
    since: datetime | None = None
    outage_errors: Counter[str] = field(default_factory=Counter)
    last_success_at: datetime | None = None
    last_recovery_seconds: float | None = None

    def roll(self, today: date) -> None:
        """Today's counts from zero on a new day; an outage going on counts in it too."""
        if today == self.day:
            return
        self.day = today
        self.attempts = self.failures = 0
        self.errors = Counter()
        self.outages = 1 if self.in_a_row else 0
        self.max_in_a_row = self.in_a_row


class PollOutages:
    """The poll jobs' calls and outages by feed and key, and their alerts (``alerts``)."""

    def __init__(self, now: Callable[[], datetime], alerts: Alerts | None = None) -> None:
        self._now = now
        self._alerts = alerts
        self._polls: dict[Key, _Poll] = {}
        self._alerted: dict[str, datetime] = {}  # endpoint -> when the outage it was alerted for began

    def succeeded(self, key: Key) -> None:
        at = self._now()
        poll = self._poll(key, at)
        poll.attempts += 1
        if poll.since is not None:
            seconds = (at - poll.since).total_seconds()
            log.info(
                "poll outage of %s %s ended after %.0f s: %d failures in a row (%s)",
                key[0],
                key[1],
                seconds,
                poll.in_a_row,
                _kinds(poll.outage_errors, ", "),
            )
            poll.last_recovery_seconds = seconds
            poll.in_a_row, poll.since, poll.outage_errors = 0, None, Counter()
            self._recovered(key[0], at)
        poll.last_success_at = at

    def failed(self, key: Key, error: BaseException) -> None:
        at = self._now()
        poll = self._poll(key, at)
        kind = error_kind(error)
        poll.attempts += 1
        poll.failures += 1
        poll.errors[kind] += 1
        if poll.since is None:
            poll.since = at
            poll.outages += 1
        poll.in_a_row += 1
        poll.max_in_a_row = max(poll.max_in_a_row, poll.in_a_row)
        poll.outage_errors[kind] += 1
        if poll.in_a_row >= SUSTAINED_FAILURES and (at - poll.since).total_seconds() >= SUSTAINED_SECONDS:
            self._sustained(key, poll, at)

    def snapshot(self) -> list[PollState]:
        """Every key called so far, by feed and key."""
        today = kst_day(self._now())
        states = []
        for (feed, key), poll in sorted(self._polls.items()):
            poll.roll(today)
            states.append(
                PollState(
                    feed,
                    key,
                    poll.attempts,
                    poll.failures,
                    dict(sorted(poll.errors.items())),
                    poll.outages,
                    poll.in_a_row,
                    poll.max_in_a_row,
                    poll.since,
                    poll.last_success_at,
                    poll.last_recovery_seconds,
                )
            )
        return states

    def _poll(self, key: Key, at: datetime) -> _Poll:
        poll = self._polls.setdefault(key, _Poll(kst_day(at)))
        poll.roll(kst_day(at))
        return poll

    def _sustained(self, key: Key, poll: _Poll, at: datetime) -> None:
        feed = key[0]
        if feed in self._alerted or poll.since is None:
            return
        self._alerted[feed] = poll.since
        if self._alerts is not None:
            self._alerts.raise_(
                ALERT_PREFIX + feed,
                f"KIS REST 조회 {feed}의 {key[1]} 키가 {poll.in_a_row}번 잇달아 실패했습니다"
                f" ({_minutes(at - poll.since)}째: {_kinds(poll.outage_errors, ', ', '번')})."
                " 재시도와 간격은 그대로라 다음 간격에 다시 묻습니다.",
            )

    def _recovered(self, feed: str, at: datetime) -> None:
        """The endpoint's recovery, once none of its keys is in an outage."""
        since = self._alerted.get(feed)
        if since is None or any(poll.in_a_row for (f, _), poll in self._polls.items() if f == feed):
            return
        del self._alerted[feed]
        if self._alerts is not None:
            self._alerts.resolve(
                ALERT_PREFIX + feed,
                f"KIS REST 조회 {feed}의 모든 키가 다시 답합니다 (장애 시작부터 {_minutes(at - since)}).",
            )


def _kinds(errors: Counter[str], sep: str, unit: str = "") -> str:
    """``EGW00316 2, OPSQ1002 3``: by kind, in order."""
    return sep.join(f"{kind} {n}{unit}" for kind, n in sorted(errors.items()))


def _minutes(span: timedelta) -> str:
    """``span`` in whole minutes (``15분``), or seconds under half a minute."""
    seconds = span.total_seconds()
    minutes = round(seconds / 60)
    return f"{minutes}분" if minutes else f"{round(seconds)}초"
