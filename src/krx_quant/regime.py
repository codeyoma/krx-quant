"""The day's market regime, decided once before the open from the overnight indicators.

Rules, in order; the first that holds decides:

1. shock: the US semiconductor index (SOX) moved at least ±3%, the VIX is at least 30,
   or the won moved at least ±1% against the dollar;
2. risk_off: SOX fell at least 1%, or the won weakened at least 0.5%;
3. risk_on: SOX rose at least 1% and the won strengthened;
4. neutral: none of these.

A missing indicator holds no rule; with every indicator missing the regime is unknown.
The thresholds are settings (``KRX_REGIME_*``). The same inputs always give the same
regime and reasons. Markov (#14) learns a transition matrix per regime.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class Regime(enum.Enum):
    SHOCK = "shock"
    RISK_OFF = "risk_off"
    RISK_ON = "risk_on"
    NEUTRAL = "neutral"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RegimeRules:
    shock_sox_pct: float = 3.0
    shock_vix: float = 30.0
    shock_usdkrw_pct: float = 1.0
    trend_sox_pct: float = 1.0
    off_usdkrw_pct: float = 0.5


@dataclass(frozen=True)
class RegimeInputs:
    sox_pct: float | None  # the last US session's move, %
    vix: float | None  # the last close
    usdkrw_pct: float | None  # won per dollar, the last day's move, %: up is a weaker won


@dataclass(frozen=True)
class Decision:
    regime: Regime
    reasons: tuple[str, ...]  # the conditions that held, in Korean


def decide(inputs: RegimeInputs, rules: RegimeRules) -> Decision:
    """The regime of ``inputs`` under ``rules``."""
    sox, vix, usdkrw = inputs.sox_pct, inputs.vix, inputs.usdkrw_pct
    if sox is None and vix is None and usdkrw is None:
        return Decision(Regime.UNKNOWN, ("지표 없음",))
    shock = []
    if sox is not None and abs(sox) >= rules.shock_sox_pct:
        shock.append(f"SOX {sox:+.1f}% (±{rules.shock_sox_pct:g}% 이상)")
    if vix is not None and vix >= rules.shock_vix:
        shock.append(f"VIX {vix:.1f} ({rules.shock_vix:g} 이상)")
    if usdkrw is not None and abs(usdkrw) >= rules.shock_usdkrw_pct:
        shock.append(f"원/달러 {usdkrw:+.1f}% (±{rules.shock_usdkrw_pct:g}% 이상)")
    if shock:
        return Decision(Regime.SHOCK, tuple(shock))
    off = []
    if sox is not None and sox <= -rules.trend_sox_pct:
        off.append(f"SOX {sox:+.1f}% (-{rules.trend_sox_pct:g}% 이하)")
    if usdkrw is not None and usdkrw >= rules.off_usdkrw_pct:
        off.append(f"원/달러 {usdkrw:+.1f}% (+{rules.off_usdkrw_pct:g}% 이상, 원화 약세)")
    if off:
        return Decision(Regime.RISK_OFF, tuple(off))
    if sox is not None and sox >= rules.trend_sox_pct and usdkrw is not None and usdkrw < 0:
        return Decision(
            Regime.RISK_ON,
            (f"SOX {sox:+.1f}% (+{rules.trend_sox_pct:g}% 이상)", f"원/달러 {usdkrw:+.1f}% (원화 강세)"),
        )
    return Decision(Regime.NEUTRAL, ("어느 규칙에도 해당하지 않음",))
