"""The climate prior (#429, ``algorithms/priors.py``): each symbol and slot's class frequencies, a
training sample's from the days before its own, and their parameters' meanings by name.

Input: made-up samples whose labels follow the time of day. Output: the prior each training sample
is given, the table, the meanings, and which modules a fresh interpreter loads.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

from krx_quant.algorithms import priors as prior_module
from krx_quant.algorithms.priors import (
    PRIOR_STRENGTH,
    PriorMeanings,
    prior_inputs,
    training_priors,
)
from krx_quant.algorithms.shannon import PARAM_MEANINGS, Shannon
from krx_quant.cards import Meaning
from krx_quant.prevalidation import code_digest
from krx_quant.pricing import Direction
from tests.test_shannon import D1, D2, D3, a_day


def test_a_training_sample_s_prior_counts_only_the_window_s_days_before_its_own() -> None:
    window = [*a_day(D1), *a_day(D2), *a_day(D3)]

    priors = training_priors(window, window)

    assert priors[:20] == [(0.0, 0.0)] * 20, "the window's first day: nothing before it, the even split"
    # After the first day: the symbol's day 10 up, 10 flat, 0 down, with 30 pseudo-counts split evenly;
    # its 09:00 slot 10 up, with 30 in the day's frequencies.
    day = ((10 + 10) / 50, (10 + 10) / 50, 10 / 50)
    opening = (
        (10 + PRIOR_STRENGTH * day[0]) / (10 + PRIOR_STRENGTH),
        PRIOR_STRENGTH * day[1] / (10 + PRIOR_STRENGTH),
        PRIOR_STRENGTH * day[2] / (10 + PRIOR_STRENGTH),
    )
    assert opening == pytest.approx((0.55, 0.3, 0.15))
    assert priors[20] == pytest.approx(prior_inputs(opening)), "09:01 on the second day: the first day's"
    assert priors[20][0] > 0 > priors[30][0], "up likelier than flat at the open, not at 10:00"
    # A sample's own day's labels and the later days' never reach it.
    turned = [*a_day(D1), *a_day(D2, Direction.DOWN), *a_day(D3, Direction.DOWN)]
    assert training_priors(turned, turned)[:40] == priors[:40]
    assert training_priors(turned, turned)[40:] != priors[40:], "the third day counts the second's"
    # Nor do days outside the window: only what it is given is counted.
    assert training_priors(window[20:], window[20:])[:20] == [(0.0, 0.0)] * 20


def test_the_table_s_parameters_have_a_meaning_by_name_and_are_not_listed() -> None:
    fixed = {"penalty": Meaning("test", "none", "always")}
    meanings = PriorMeanings(fixed, "밤 학습")

    assert list(meanings) == ["penalty"] and len(meanings) == 1, "the symbols are the settings'"
    opening = meanings["prior.005930.09:00.up"]
    assert "005930" in opening.text and "09:01–09:30" in opening.text and "상승" in opening.text
    assert (opening.unit, opening.known) == ("비율", "밤 학습")
    assert "하루 전체" in meanings["prior.000660.all.down"].text
    for name in ("prior.005930.09:00.sideways", "prior.005930.9:00.up", "prior.005930.up", "w.up.x"):
        assert name not in meanings
    assert "prior.005930.15:00.flat" in PARAM_MEANINGS, "shannon's"


def test_production_code_never_loads_the_experiment_and_the_climate_imports_first() -> None:
    # ``priors`` may not import ``climate``: ``climate`` imports the algorithms' contract, whose
    # package imports shannon, which imports ``priors``. A fresh interpreter, ``climate`` first.
    script = (
        "import sys, krx_quant.climate, krx_quant.engine, krx_quant.nightly, krx_quant.prevalidation; "
        "print('krx_quant.climate_prior' in sys.modules)"
    )

    ran = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)

    assert ran.stdout.strip() == "False"


def test_shannon_s_code_digest_holds_the_prior_s_code() -> None:
    # Its package, then this module (``code_from``): a change to the prior asks for a new verdict.
    root = Path(__file__).parents[1] / "src" / "krx_quant" / "algorithms" / "shannon"
    digest = hashlib.sha256()
    for f in sorted(root.rglob("*.py")):
        digest.update(f.relative_to(root).as_posix().encode())
        digest.update(f.read_bytes())
    digest.update(b"\0code_from\0")
    module = Path(prior_module.__file__)
    digest.update(module.name.encode())
    digest.update(module.read_bytes())

    assert code_digest(Shannon()) == digest.hexdigest()[:16]
