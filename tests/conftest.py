from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from krx_quant.fake_kis import FakeKis
from tests.algorithm_doubles import FixedCosts


@pytest.fixture(autouse=True)
def no_log_files(monkeypatch: pytest.MonkeyPatch) -> None:
    """A command a test runs (``cli.main``) writes no log file under ``state/logs`` (#356) unless
    the test turns them on: its state folder may be the working directory's."""
    monkeypatch.setenv("KRX_LOG_DAYS", "off")
    monkeypatch.delenv("KRX_LOG_BY_PARENT", raising=False)


@pytest.fixture
async def kis() -> AsyncIterator[FakeKis]:
    fake = FakeKis()
    await fake.start()
    yield fake
    await fake.stop()


@pytest.fixture
def fixed_costs(monkeypatch: pytest.MonkeyPatch) -> None:
    """The engine times each run by the seconds its stand-ins spend (``FixedCosts``): a CPU
    budget test waits for what the budget does with known costs, not for a fast machine (#327)."""
    monkeypatch.setattr("krx_quant.engine.timer", FixedCosts())
