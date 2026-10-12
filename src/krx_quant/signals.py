"""Telegram signal alerts (optional, off by default): a final consensus sure enough of a move
that enough of its members agree on.

At each bar close's final consensus, for each symbol, the move is up if the consensus's
``p_up`` is at least ``min_probability``, else down if its ``p_down`` is. At least
``min_agree`` of the active algorithms it pooled must then predict that move: the most
likely of their three classes, a tie going to flat, then up, as in the stats. A signal for
the same symbol and move is not sent again before the last one's target time (4 minutes
later): a move that keeps holding is called every 4 minutes, not every minute.
``KRX_SIGNAL_ALERT`` turns it on.

A signal is then blocked, not sent, when the engine knows by then a reason not to act on
it (``Blocked``): a VI or circuit breaker touching its four minutes, no reference price at
its bar close, or its target time already past. Each signal judged is kept as it is sent
or blocked (``SENT``, ``BLOCKED``); the ones sent today are read back when the engine
restarts, so a running move is not called again.
"""

from __future__ import annotations

import enum
import json
import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from krx_quant.pricing import Direction, most_likely

if TYPE_CHECKING:  # the settings read SignalRule: this module stays light
    from krx_quant.baselines import Forecast

log = logging.getLogger(__name__)

# The kinds a judged signal is kept as (the store's ``runs``).
SENT = "signal_sent"
BLOCKED = "signal_blocked"


@dataclass(frozen=True)
class SignalRule:
    min_probability: float = 0.6  # of the move, in the final consensus; at least 0.5
    min_agree: int = 2  # active algorithms predicting the move


class Blocked(enum.Enum):
    """Why a signal is not sent."""

    # A VI or circuit breaker touches its four minutes. Narrower than a special prediction's
    # windows: a sidecar halts program trading only and does not block.
    SPECIAL = "special"
    NO_REFERENCE = "no_reference"  # no mid-point at its bar close: the book was silent a minute
    EXPIRED = "expired"  # its target time is past


@dataclass(frozen=True)
class Signal:
    symbol: str
    bar_close: datetime  # in KST, as the engine's
    target_at: datetime
    direction: Direction  # up or down
    probability: float
    agree: int
    members: int  # the active algorithms pooled

    def text(self) -> str:
        move = "상승" if self.direction is Direction.UP else "하락"
        return (
            f"{self.symbol} {move} {self.probability:.0%} · 정식 {self.members}개 중 {self.agree}개 같은 방향"
            f" · {self.bar_close:%H:%M} 봉 마감, {self.target_at:%H:%M}까지"
        )


@dataclass(frozen=True)
class Judged:
    """A signal that held, and why it was blocked (None: sent)."""

    signal: Signal
    blocked: Blocked | None

    @property
    def kind(self) -> str:
        """The kind it is kept as: ``SENT`` or ``BLOCKED``."""
        return SENT if self.blocked is None else BLOCKED

    def detail(self, rule: SignalRule) -> str:
        """What is kept of it, as JSON: the signal, the rule it held by, and why it was blocked."""
        s = self.signal
        record: dict[str, Any] = {
            "symbol": s.symbol,
            "direction": s.direction.value,
            "probability": s.probability,
            "agree": s.agree,
            "members": s.members,
            "bar_close": s.bar_close.isoformat(),
            "target_at": s.target_at.isoformat(),
            "rule": {"min_probability": rule.min_probability, "min_agree": rule.min_agree},
            "blocked": None if self.blocked is None else self.blocked.value,
        }
        return json.dumps(record)


def judge(
    symbol: str,
    bar_close: datetime,
    target_at: datetime,
    consensus: Forecast,
    members: Mapping[str, Forecast],
    rule: SignalRule,
) -> Signal | None:
    """The signal of a symbol's final consensus, or None."""
    if consensus.p_up >= rule.min_probability:
        direction, probability = Direction.UP, consensus.p_up
    elif consensus.p_down >= rule.min_probability:
        direction, probability = Direction.DOWN, consensus.p_down
    else:
        return None
    agree = sum(most_likely(f.p_up, f.p_flat, f.p_down) is direction for f in members.values())
    if agree < rule.min_agree:
        return None
    return Signal(symbol, bar_close, target_at, direction, probability, agree, len(members))


class Signals:
    """Judges each final consensus and sends what holds and is not blocked, once per move."""

    def __init__(self, rule: SignalRule, send: Callable[[str], None]) -> None:
        self.rule = rule
        self._send = send
        self._running: dict[tuple[str, Direction], datetime] = {}  # until its target time

    def resume(self, sent: Iterable[str]) -> None:
        """Carry on from the signals sent before a restart (their ``Judged.detail``); one that
        cannot be read is skipped."""
        for detail in sent:
            try:
                record = json.loads(detail)
                key = (record["symbol"], Direction(record["direction"]))
                until = datetime.fromisoformat(record["target_at"])
            except (ValueError, KeyError, TypeError) as e:
                log.warning("a sent signal not resumed (%s): %s", e, detail)
                continue
            if key not in self._running or self._running[key] < until:
                self._running[key] = until

    def final(
        self,
        bar_close: datetime,
        target_at: datetime,
        pooled: Mapping[str, Forecast],
        members: Mapping[str, Mapping[str, Forecast]],
        blocked: Mapping[str, Blocked],
        keep: Callable[[Judged], None],
    ) -> None:
        """A bar close's final consensus by symbol, the members' forecasts it pooled and why a
        symbol's signal is blocked: each signal that holds is sent unless blocked, and given
        to ``keep`` as soon as it is. A move still running is neither sent nor blocked again."""
        for symbol, consensus in pooled.items():
            signal = judge(symbol, bar_close, target_at, consensus, members.get(symbol, {}), self.rule)
            if signal is None:
                continue
            key = (symbol, signal.direction)
            if key in self._running and bar_close < self._running[key]:
                continue
            why = blocked.get(symbol)
            if why is None:
                self._running[key] = target_at
                self._send(signal.text())
            keep(Judged(signal, why))
