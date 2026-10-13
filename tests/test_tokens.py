"""Access-token reuse and refresh, observed through the fake KIS seam."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

from krx_quant.collector import run_collector
from krx_quant.config import Settings
from krx_quant.fake_kis import FakeKis
from tests.test_collector import FIXED_NOW, settings_for
from tests.test_resilience import Clock
from tests.test_subscription_limit import until

QUOTE = "FHKST01010100"


def polling(kis: FakeKis, tmp_path: Path) -> Settings:
    """Settings whose only REST traffic is one fallback quote poll every 0.05s."""
    kis.rest_responses[QUOTE] = lambda q: {
        "rt_cd": "0",
        "output": {"stck_prpr": "1", "acml_vol": "1", "prdy_ctrt": "0"},
    }
    return replace(
        settings_for(kis, tmp_path),
        state_dir=tmp_path / "state",
        ws_max_subscriptions=0,
        fallback_poll_seconds=0.05,
        token_min_interval_seconds=0.3,
    )


async def run_briefly(settings: Settings, clock: Any, seconds: float = 0.3) -> None:
    stop = asyncio.Event()
    task = asyncio.create_task(run_collector(settings, stop=stop, clock=clock))
    await asyncio.sleep(seconds)
    stop.set()
    await asyncio.wait_for(task, 5)


async def test_a_restart_reuses_the_cached_token(kis: FakeKis, tmp_path: Path) -> None:
    settings = polling(kis, tmp_path)

    await run_briefly(settings, lambda: FIXED_NOW)
    await run_briefly(settings, lambda: FIXED_NOW)

    assert kis.tokens_issued == ["fake-access-token-1"]


async def test_the_token_is_refreshed_before_it_expires(kis: FakeKis, tmp_path: Path) -> None:
    kis.token_expires = (FIXED_NOW + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    clock = Clock(FIXED_NOW)
    stop = asyncio.Event()
    task = asyncio.create_task(run_collector(polling(kis, tmp_path), stop=stop, clock=clock))
    await until(lambda: len(kis.tokens_issued) == 1 and len(kis.rest_requests) > 0)

    clock.at = FIXED_NOW + timedelta(hours=1, minutes=30)  # inside the one-hour refresh margin
    kis.token_expires = (clock.at + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")  # a new token's day
    await until(lambda: len(kis.tokens_issued) == 2)
    # Polls outrun the 20-per-second limiter here, so the next request can wait up to a second.
    await until(lambda: any(t == QUOTE and h["_auth"].endswith("-2") for _, t, _, h in kis.rest_requests))
    stop.set()
    await asyncio.wait_for(task, 5)

    assert kis.tokens_issued == ["fake-access-token-1", "fake-access-token-2"], "refreshed exactly once"
    last = [h for _, t, _, h in kis.rest_requests if t == QUOTE][-1]
    assert last["_auth"] == "Bearer fake-access-token-2"


async def test_a_refused_issuance_is_retried_after_the_once_a_minute_limit(
    kis: FakeKis, tmp_path: Path
) -> None:
    kis.token_min_interval = 0.3
    kis.tokens_issued.append("issued-by-another-process")
    kis._last_token_at = asyncio.get_running_loop().time()  # as if just issued elsewhere

    await run_briefly(polling(kis, tmp_path), lambda: FIXED_NOW, seconds=1.0)

    assert kis.tokens_issued[1:] == ["fake-access-token-2"]
    assert any(t == QUOTE for _, t, _, _ in kis.rest_requests), "REST polling ran with the retried token"


async def test_a_refused_refresh_keeps_using_the_still_valid_token(kis: FakeKis, tmp_path: Path) -> None:
    kis.token_expires = (FIXED_NOW + timedelta(minutes=50)).strftime("%Y-%m-%d %H:%M:%S")  # inside the margin
    kis.token_min_interval = 30.0  # KIS refuses a second issuance for the rest of the test
    settings = replace(polling(kis, tmp_path), token_min_interval_seconds=0.05)

    def used() -> list[str]:
        return [h["_auth"] for _, t, _, h in kis.rest_requests if t == QUOTE]

    stop = asyncio.Event()
    task = asyncio.create_task(run_collector(settings, stop=stop, clock=lambda: FIXED_NOW))
    # Wait for the polls rather than a fixed window: a slow runner fits fewer polls in 0.6s.
    try:
        await until(lambda: len(used()) >= 3)
    except TimeoutError:
        raise AssertionError("polling was blocked by the refused refresh") from None
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)

    assert set(used()) == {"Bearer fake-access-token-1"}
    assert kis.tokens_issued == ["fake-access-token-1"], "KIS refused the refresh"
