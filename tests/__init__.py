"""The test suite.

The doubles modules' helpers assert as the tests do, so pytest rewrites their asserts as it does a
test module's and a failed one shows its values (#511). Registered here, before any is imported."""

from pathlib import Path

import pytest

pytest.register_assert_rewrite(
    *sorted(f"tests.{path.stem}" for path in Path(__file__).parent.glob("*_doubles.py"))
)
