"""The test suite.

The doubles modules' helpers assert as the tests do, so pytest rewrites their asserts as it does a
test module's and a failed one shows its values (#511). Registered here, before any is imported.

NumPy's BLAS (OpenBLAS on Linux) computes on the thread that calls it, set here before NumPy is
first imported (#574). With a thread per CPU, the calling thread spins while it waits for the
others. On a 4-CPU Linux container beside four busy processes, ito's 600 predictions of the
prediction budget's check (``tests/test_algorithm_contract.py``) took 2.7-3.2 s of the clock and
1.4-1.6 s of the calling thread's CPU; with BLAS on one thread, 0.8-0.9 s and 0.4 s. That check
counts the calling thread's CPU. The image runs the engine so too (``Dockerfile``, #589);
``tests/test_blas_threads.py`` checks that the forecasts are the same either way.
"""

import os
from pathlib import Path

import pytest

os.environ["OPENBLAS_NUM_THREADS"] = "1"

pytest.register_assert_rewrite(
    *sorted(f"tests.{path.stem}" for path in Path(__file__).parent.glob("*_doubles.py"))
)
