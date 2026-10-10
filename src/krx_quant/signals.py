"""Telegram signal alerts (optional, off by default): a final consensus sure enough of a move
that enough of its members agree on.

At each bar close's final consensus, for each symbol, the move is up if the consensus's
``p_up`` is at least ``min_probability``, else down if its ``p_down`` is. At least
``min_agree`` of the active algorithms it pooled must then predict that move: the most
likely of their three classes, a tie going to flat, then up, as in the stats. A signal for
the same symbol and move is not sent again before the last one's target time (4 minutes
later): a move that keeps holding is called every 4 minutes, not every minute.
``KRX_SIGNAL_ALERT`` turns it on.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from krx_quant.pricing import Direction, most_likely

if TYPE_CHECKING:  # the settings read SignalRule: this module stays light
    from krx_quant.baselines import Forecast


@dataclass(frozen=True)
class SignalRule:
    min_probability: float = 0.6  # of the move, in the final consensus; at least 0.5
    min_agree: int = 2  # active algorithms predicting the move


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
    """Judges each final consensus and sends what holds, once per move."""

    def __init__(self, rule: SignalRule, send: Callable[[str], None]) -> None:
        self.rule = rule
        self._send = send
        self._running: dict[tuple[str, Direction], datetime] = {}  # until its target time

    def final(
        self,
        bar_close: datetime,
        target_at: datetime,
        pooled: Mapping[str, Forecast],
        members: Mapping[str, Mapping[str, Forecast]],
    ) -> None:
        """A bar close's final consensus by symbol, and the members' forecasts it pooled."""
        for symbol, consensus in pooled.items():
            signal = judge(symbol, bar_close, target_at, consensus, members.get(symbol, {}), self.rule)
            if signal is None:
                continue
            key = (symbol, signal.direction)
            if key in self._running and bar_close < self._running[key]:
                continue
            self._running[key] = target_at
            self._send(signal.text())
