"""The algorithms forecast and learn the same with NumPy's BLAS on one thread or on two (#589).

The image keeps BLAS and OpenMP on the thread that calls them (``Dockerfile``: ``OPENBLAS_NUM_THREADS``
and ``OMP_NUM_THREADS`` 1). OpenBLAS reads its variable once, when NumPy is first imported, so each
side runs in a process of its own: the test process has NumPy on one thread already
(``tests/__init__.py``). On Linux NumPy's BLAS is OpenBLAS, and each process says how many threads
it ran on; macOS's Accelerate reads neither variable, and the sides are the same there.

"The same" is to 1e-12, not to the bit: on some x86-64 CPUs OpenBLAS sums a matrix product in
another order on two threads. bishop's network then learns weights a last bit apart (3e-16 at most,
its forecasts 3e-15, under emulation), and its model's version, a digest of those bits, differs (seen
on a CI runner on 2026-11-06). ito's grid choice does not.
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
from pathlib import Path

from krx_quant.algorithms import (
    ALGORITHMS, KIND_ALGORITHMS, PredictionContext, kind_predicted, load_models, predicted, save_model
)
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
# Fitted here and kept in the folder argv[2] ("fit"), or read from it ("load"): the same models.
learned = Path(sys.argv[2])
if sys.argv[3] == "fit":
    fitted = {a.name: a.fit(made) for a in ALGORITHMS if a.name in ("ito", "bishop")}
    for name, model in fitted.items():
        save_model(learned, name, model)
else:
    kept = load_models(learned, ALGORITHMS)
    fitted = {name: kept[name] for name in ("ito", "bishop")}
forecasts, params = {}, {name: model.params for name, model in fitted.items()}
versions = {name: model.version for name, model in fitted.items()}
for s in made[:: max(1, len(made) // 20)]:
    at, context = s.bar_close.isoformat(), PredictionContext(s.symbol, s.bar_close)
    for algorithm in ALGORITHMS:
        vector = FeatureVector(s.features, algorithm.features)
        for role, model in (("default", algorithm.default_model()), ("fitted", fitted.get(algorithm.name))):
            if model is not None:
                with forecasting(vector):
                    f = predicted(algorithm, vector, model, context)
                forecasts[f"{algorithm.name} {role} {at}"] = (
                    None if f is None else [f.p_up, f.p_flat, f.p_down, f.expected_ticks]
                )
    for algorithm in KIND_ALGORITHMS:
        vector = FeatureVector(s.features, algorithm.features)
        k = kind_predicted(algorithm, vector, algorithm.default_model())
        forecasts[f"{algorithm.name} {at}"] = None if k is None else [*k.probabilities, *k.fields.values()]
out = {"blas_threads": blas_threads(), "forecasts": forecasts, "params": params, "versions": versions}
print(json.dumps(out))
"""


def side(threads: int, models: Path, how: str) -> dict[str, Any]:
    """One process's side on ``threads`` BLAS and OpenMP threads: fitting the models into ``models``
    (``fit``) or forecasting with those fitted there (``load``)."""
    env = os.environ | {"OPENBLAS_NUM_THREADS": str(threads), "OMP_NUM_THREADS": str(threads)}
    done = subprocess.run(
        [sys.executable, "-c", SIDE, str(TESTS / "synthetic_bars.py"), str(models), how],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    out: dict[str, Any] = json.loads(done.stdout)
    return out


def same(one: dict[str, Any], other: dict[str, Any]) -> None:
    """Every forecast of ``one`` in ``other`` too, the same to 1e-12."""
    assert other["forecasts"].keys() == one["forecasts"].keys()
    for name, probabilities in one["forecasts"].items():
        if probabilities is None:
            assert other["forecasts"][name] is None, name
        else:
            assert other["forecasts"][name] == pytest.approx(probabilities, abs=1e-12), name


# Three processes, each importing the engine (two fit ito on a day): about 2 s each on an idle 4-CPU
# Linux container. With BLAS on four threads beside other busy processes, one took 38 s.
@pytest.mark.timeout(120)
def test_the_algorithms_forecast_and_learn_the_same_with_blas_on_one_thread_or_two(tmp_path: Path) -> None:
    """Input: the models fitted on one thread; forecast on two threads with those very models, and
    with models fitted on two threads. Output: the same forecasts to 1e-12 each time, and the same
    fitted parameters and ito model (bishop's version may differ: module doc)."""
    cpus = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count() or 1
    several = min(2, cpus)  # the server's two vCPUs, OpenBLAS's own count there
    one = side(1, tmp_path / "one", "fit")
    those = side(several, tmp_path / "one", "load")
    own = side(several, tmp_path / "own", "fit")

    if one["blas_threads"] is not None:  # OpenBLAS: each side ran on the threads it was given
        assert one["blas_threads"] == 1
        assert those["blas_threads"] == own["blas_threads"] == several
    assert those["versions"] == one["versions"], "the very models, read back"
    same(one, those)
    assert own["params"] == one["params"], "the fitted models' parameters"
    assert own["versions"]["ito"] == one["versions"]["ito"]
    assert any(name.startswith("ito fitted") for name in one["forecasts"]), "ito fitted and forecast"
    same(one, own)
