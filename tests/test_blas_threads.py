"""The algorithms forecast and learn the same with NumPy's BLAS on one thread or on two (#589).

The image keeps BLAS and OpenMP on the thread that calls them (``Dockerfile``: ``OPENBLAS_NUM_THREADS``
and ``OMP_NUM_THREADS`` 1). OpenBLAS reads its variable once, when NumPy is first imported, so each
side runs in a process of its own: the test process has NumPy on one thread already
(``tests/__init__.py``). On Linux NumPy's BLAS is OpenBLAS, and each process says how many threads
it ran on; macOS's Accelerate reads neither variable, and both sides are the same there.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

TESTS = Path(__file__).resolve().parent

# One process's side: a made-up day of one-minute bars, the training's samples of them, the models
# the BLAS-heavy algorithms fit (ito's eigen-decompositions, bishop's network), and every bar-close
# and kind algorithm's forecast of a spread of those samples by its default and its fitted model.
# The bars come from their file, not the package ``tests``: its __init__ sets OPENBLAS_NUM_THREADS.
SIDE = """
import ctypes, importlib.util, json, sys
from datetime import date

from krx_quant.algorithms import ALGORITHMS, KIND_ALGORITHMS, PredictionContext, kind_predicted, predicted
from krx_quant.algorithms.forecasts import forecasting
from krx_quant.features import FeatureVector
from krx_quant.market_calendar import MarketCalendar
from krx_quant.training import samples


def blas_threads():
    # OpenBLAS's own count (NumPy's wheels on Linux); None for another BLAS.
    try:
        with open("/proc/self/maps") as maps:
            path = next(line.split()[-1] for line in maps if "openblas" in line)
        return int(ctypes.CDLL(path).scipy_openblas_get_num_threads64_())
    except (OSError, StopIteration, AttributeError):
        return None


spec = importlib.util.spec_from_file_location("synthetic_bars", sys.argv[1])
bars = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bars)
days = [date(2026, 9, 14)]
names = sorted({f for a in [*ALGORITHMS, *KIND_ALGORITHMS] for f in a.features})
made = samples(
    {("005930", d): bars.random_walk(d, seed=i) for i, d in enumerate(days)},
    MarketCalendar(dict.fromkeys(days, True)),
    names,
    0.00015,
    target_version=3,
)
fitted = {a.name: a.fit(made) for a in ALGORITHMS if a.name in ("ito", "bishop")}
forecasts, params = {}, {name: model.params for name, model in fitted.items()}
for s in made[:: max(1, len(made) // 20)]:
    at, context = s.bar_close.isoformat(), PredictionContext(s.symbol, s.bar_close)
    for algorithm in ALGORITHMS:
        vector = FeatureVector(s.features, algorithm.features)
        for model in (algorithm.default_model(), fitted.get(algorithm.name)):
            if model is not None:
                with forecasting(vector):
                    f = predicted(algorithm, vector, model, context)
                forecasts[f"{algorithm.name} {model.version} {at}"] = (
                    None if f is None else [f.p_up, f.p_flat, f.p_down, f.expected_ticks]
                )
    for algorithm in KIND_ALGORITHMS:
        vector = FeatureVector(s.features, algorithm.features)
        k = kind_predicted(algorithm, vector, algorithm.default_model())
        forecasts[f"{algorithm.name} {at}"] = None if k is None else [*k.probabilities, *k.fields.values()]
print(json.dumps({"blas_threads": blas_threads(), "forecasts": forecasts, "params": params}))
"""


def side(threads: int) -> dict[str, Any]:
    env = os.environ | {"OPENBLAS_NUM_THREADS": str(threads), "OMP_NUM_THREADS": str(threads)}
    done = subprocess.run(
        [sys.executable, "-c", SIDE, str(TESTS / "synthetic_bars.py")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    out: dict[str, Any] = json.loads(done.stdout)
    return out


# Two processes, each importing the engine and fitting ito on a day: about 2 s each on an idle
# 4-CPU Linux container. With BLAS on four threads beside other busy processes, one took 38 s.
@pytest.mark.timeout(120)
def test_the_algorithms_forecast_and_learn_the_same_with_blas_on_one_thread_or_two() -> None:
    cpus = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count() or 1
    several = min(2, cpus)  # the server's two vCPUs, OpenBLAS's own count there
    one, many = side(1), side(several)

    if one["blas_threads"] is not None:  # OpenBLAS: each side ran on the threads it was given
        assert one["blas_threads"] == 1
        assert many["blas_threads"] == several
    assert many["params"] == one["params"], "the fitted models' parameters"
    assert many["forecasts"].keys() == one["forecasts"].keys()
    assert any(name.startswith("ito ito-2026") for name in one["forecasts"]), "ito fitted and forecast"
    for name, probabilities in one["forecasts"].items():
        if probabilities is None:
            assert many["forecasts"][name] is None, name
        else:
            assert many["forecasts"][name] == pytest.approx(probabilities, abs=1e-12), name
