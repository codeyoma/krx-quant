"""The app key's REST rate limiter, run on a clock that moves only when the limiter sleeps."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest

from krx_quant import auth, ratelimit
from krx_quant.config import Settings
from krx_quant.ratelimit import RateLimiter
from tests.test_collector import FIXED_NOW


class Monotonic:
    """``time.monotonic`` for the limiter: it stands still until the limiter sleeps."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


async def rest_limiter(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> RateLimiter:
    """The rate limiter ``tokens_and_client`` gives the app key's REST client."""
    built: list[RateLimiter] = []

    def build(*args: Any, **kwargs: Any) -> RateLimiter:
        built.append(RateLimiter(*args, **kwargs))
        return built[-1]

    monkeypatch.setattr(auth, "RateLimiter", build)
    async with aiohttp.ClientSession() as session:
        auth.tokens_and_client(session, settings, lambda: FIXED_NOW)
    [limiter] = built
    return limiter


async def test_the_app_keys_rest_limiter_lets_twenty_calls_through_per_1_1_second_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(kis_app_key="key", kis_app_secret="secret", state_dir=tmp_path)
    limiter = await rest_limiter(settings, monkeypatch)
    clock = Monotonic()
    monkeypatch.setattr(ratelimit, "time", SimpleNamespace(monotonic=clock))
    monkeypatch.setattr(ratelimit, "asyncio", SimpleNamespace(sleep=clock.sleep))

    let_through = [await limiter.acquire() for _ in range(21)]

    # KIS allows 20 calls a second. The extra 0.1 s absorbs delays on the way to KIS, up to its
    # size (docs/kis-feeds.md). Counting the limiter's grants in the busiest second cannot tell
    # this window from a 1.0 s one, so the window is checked here.
    assert let_through == pytest.approx([0.0] * 20 + [1.1])
