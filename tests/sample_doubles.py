"""Labelled samples for the priors' tests (#511): a day of bar closes whose inputs say nothing, the
ten from 09:01 labelled one way and the ten from 10:01 flat, with Shannon's inputs.
"""

from __future__ import annotations

from datetime import date, datetime, time

from krx_quant.algorithms import Sample
from krx_quant.algorithms.shannon import INPUTS, Shannon
from krx_quant.features import FeatureVector
from krx_quant.feeds import KST
from krx_quant.pricing import Direction

SHANNON = Shannon()


def inputs(**values: float | None) -> FeatureVector:
    return FeatureVector(dict.fromkeys(INPUTS) | {"depth_imbalance": 0.0} | values, SHANNON.features)


def labelled(day: date, hhmm: str, outcome: Direction | None, symbol: str = "005930") -> Sample:
    """A bar close whose inputs say nothing (every one the same)."""
    return Sample(
        symbol, datetime.combine(day, time.fromisoformat(hhmm), KST), inputs(ofi_1=0.5), outcome, 0.0
    )


def a_day(day: date, opening: Direction = Direction.UP) -> list[Sample]:
    """Ten bar closes from 09:01 labelled ``opening``, ten from 10:01 flat."""
    return [labelled(day, f"09:{m:02d}", opening) for m in range(1, 11)] + [
        labelled(day, f"10:{m:02d}", Direction.FLAT) for m in range(1, 11)
    ]
