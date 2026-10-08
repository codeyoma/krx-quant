from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from krx_quant.fake_kis import FakeKis


@pytest.fixture
async def kis() -> AsyncIterator[FakeKis]:
    fake = FakeKis()
    await fake.start()
    yield fake
    await fake.stop()
