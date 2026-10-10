"""The daily market regime: the overnight indicators in, one of four regimes out.

Input: the US semiconductor index's move, the VIX and the won's move against the dollar,
any of them missing. Output: the regime and why.
"""

from __future__ import annotations

import pytest

from krx_quant.regime import Regime, RegimeInputs, RegimeRules, decide

RULES = RegimeRules()


@pytest.mark.parametrize(
    ("inputs", "regime"),
    [
        (RegimeInputs(sox_pct=-3.4, vix=18.0, usdkrw_pct=0.1), Regime.SHOCK),
        (RegimeInputs(sox_pct=0.2, vix=31.0, usdkrw_pct=0.0), Regime.SHOCK),
        (RegimeInputs(sox_pct=0.2, vix=18.0, usdkrw_pct=-1.2), Regime.SHOCK),
        (RegimeInputs(sox_pct=-1.5, vix=18.0, usdkrw_pct=-0.2), Regime.RISK_OFF),
        (RegimeInputs(sox_pct=0.4, vix=18.0, usdkrw_pct=0.6), Regime.RISK_OFF),
        (RegimeInputs(sox_pct=1.8, vix=18.0, usdkrw_pct=-0.3), Regime.RISK_ON),
        (RegimeInputs(sox_pct=1.8, vix=18.0, usdkrw_pct=0.2), Regime.NEUTRAL),
        (RegimeInputs(sox_pct=0.5, vix=18.0, usdkrw_pct=-0.3), Regime.NEUTRAL),
    ],
)
def test_the_first_rule_that_holds_decides(inputs: RegimeInputs, regime: Regime) -> None:
    assert decide(inputs, RULES).regime is regime


def test_the_same_inputs_give_the_same_regime_and_reasons() -> None:
    inputs = RegimeInputs(sox_pct=-3.4, vix=31.0, usdkrw_pct=1.1)

    first, second = decide(inputs, RULES), decide(inputs, RULES)

    assert first == second
    assert first.reasons == ("SOX -3.4% (±3% 이상)", "VIX 31.0 (30 이상)", "원/달러 +1.1% (±1% 이상)")


def test_a_missing_indicator_never_holds_a_rule() -> None:
    # Risk-on needs the won to strengthen: unknown, it is not risk-on.
    assert decide(RegimeInputs(sox_pct=1.8, vix=None, usdkrw_pct=None), RULES).regime is Regime.NEUTRAL
    # What is known still decides.
    assert decide(RegimeInputs(sox_pct=None, vix=35.0, usdkrw_pct=None), RULES).regime is Regime.SHOCK
    nothing = decide(RegimeInputs(sox_pct=None, vix=None, usdkrw_pct=None), RULES)
    assert nothing.regime is Regime.NEUTRAL, "four regimes only: without indicators, neutral"
    assert nothing.reasons == ("지표 없음",)
    assert len(Regime) == 4


def test_the_thresholds_are_settings() -> None:
    calm = RegimeRules(
        shock_sox_pct=5.0, shock_vix=40.0, shock_usdkrw_pct=2.0, sox_move_pct=4.0, weak_won_pct=1.0
    )

    assert decide(RegimeInputs(sox_pct=-3.4, vix=31.0, usdkrw_pct=0.6), calm).regime is Regime.NEUTRAL
