"""The day's market regime, decided once before the open from the overnight indicators.

Rules, in order; the first that holds decides:

1. shock: the US semiconductor index (SOX) moved at least ±3%, the VIX is at least 30,
   or the won moved at least ±1% against the dollar;
2. risk_off: SOX fell at least 1%, or the won weakened at least 0.5%;
3. risk_on: SOX rose at least 1% and the won strengthened;
4. neutral: none of these, or no indicator at all.

A missing indicator holds no rule. The thresholds are settings (``KRX_REGIME_*``). The
same inputs always give the same regime and reasons. Markov (#14) learns a transition
matrix per regime.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class Regime(enum.Enum):
    SHOCK = "shock"
    RISK_OFF = "risk_off"
    RISK_ON = "risk_on"
    NEUTRAL = "neutral"


@dataclass(frozen=True)
class RegimeRules:
    shock_sox_pct: float = 3.0
    shock_vix: float = 30.0
    shock_usdkrw_pct: float = 1.0
    sox_move_pct: float = 1.0  # a fall this large is risk_off, a rise risk_on
    weak_won_pct: float = 0.5  # the won weakening this much is risk_off


@dataclass(frozen=True)
class RegimeInputs:
    sox_pct: float | None  # the move since the last KRX session's morning, %
    vix: float | None  # the last close
    usdkrw_pct: float | None  # won per dollar, the same span: up is a weaker won


@dataclass(frozen=True)
class Decision:
    regime: Regime
    reasons: tuple[str, ...]  # the conditions that held, in Korean


def decide(inputs: RegimeInputs, rules: RegimeRules) -> Decision:
    """The regime of ``inputs`` under ``rules``."""
    sox, vix, won = inputs.sox_pct, inputs.vix, inputs.usdkrw_pct
    if sox is None and vix is None and won is None:
        return Decision(Regime.NEUTRAL, ("지표 없음",))
    shock = []
    if sox is not None and abs(sox) >= rules.shock_sox_pct:
        shock.append(_sox(sox, f"±{rules.shock_sox_pct:g}% 이상"))
    if vix is not None and vix >= rules.shock_vix:
        shock.append(f"VIX {vix:.1f} ({rules.shock_vix:g} 이상)")
    if won is not None and abs(won) >= rules.shock_usdkrw_pct:
        shock.append(_won(won, f"±{rules.shock_usdkrw_pct:g}% 이상"))
    if shock:
        return Decision(Regime.SHOCK, tuple(shock))
    off = []
    if sox is not None and sox <= -rules.sox_move_pct:
        off.append(_sox(sox, f"-{rules.sox_move_pct:g}% 이하"))
    if won is not None and won >= rules.weak_won_pct:
        off.append(_won(won, f"+{rules.weak_won_pct:g}% 이상, 원화 약세"))
    if off:
        return Decision(Regime.RISK_OFF, tuple(off))
    if sox is not None and sox >= rules.sox_move_pct and won is not None and won < 0:
        return Decision(Regime.RISK_ON, (_sox(sox, f"+{rules.sox_move_pct:g}% 이상"), _won(won, "원화 강세")))
    return Decision(Regime.NEUTRAL, ("어느 규칙에도 해당하지 않음",))


def _sox(pct: float, condition: str) -> str:
    return f"SOX {pct:+.1f}% ({condition})"


def _won(pct: float, condition: str) -> str:
    return f"원/달러 {pct:+.1f}% ({condition})"
