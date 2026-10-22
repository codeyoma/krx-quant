from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest

from krx_quant import loki
from krx_quant.fake_kis import FakeKis
from tests.algorithm_doubles import FixedCosts
from tests.loki_fake import FakeLoki, serving


@pytest.fixture(autouse=True)
def not_a_child(monkeypatch: pytest.MonkeyPatch) -> None:
    """The tests run as a process of their own, not one the engine started (``process_log``)."""
    monkeypatch.delenv("KRX_LOG_BY_PARENT", raising=False)


@pytest.fixture(autouse=True)
def no_loki(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A command a test runs sends nothing to Loki (#358) unless the test points it at a fake one
    (empty values override a checkout's ``.env``), and no test leaves this process sending."""
    for name in (loki.URL, loki.USER, loki.TOKEN):
        monkeypatch.setenv(name, "")
    yield
    loki.stop(0)


@pytest.fixture
async def fake_loki() -> AsyncIterator[FakeLoki]:
    """A fake Grafana Cloud Loki (``tests/loki_fake.py``)."""
    async with serving() as found:
        yield found


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
