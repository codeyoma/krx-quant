"""The Toss Securities Open API client (``toss.py``): its shared token, the address allowlist and the
pace. The company names (#250, ``names.py``) are its one caller since #694 took the daily supply's
Toss part away.

Input: a fake Toss (``tests/fake_toss.py``) keeping Toss's rules (one valid token per client, the IP
allowlist, 429 with ``Retry-After``) and naming one symbol.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from krx_quant.config import Settings
from krx_quant.forbidden import RULES
from krx_quant.redaction import secrets_of
from krx_quant.toss import IP_MESSAGE, STOCKS, TOKEN_FILE, TossClient, TossError, TossTokens
from tests.fake_toss import CLIENT_ID, CLIENT_SECRET, FakeToss, fake_toss

NAMES = {"symbols": "005930"}  # the stock information call ``names.py`` makes


def cache_token(state_dir: Path, token: str, expires: datetime) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / TOKEN_FILE).write_text(
        json.dumps(
            {
                "key_id": hashlib.sha256(CLIENT_ID.encode()).hexdigest()[:16],
                "token": token,
                "expires": expires.isoformat(),
                "issued_at": 0.0,
            }
        )
    )


def with_toss(toss: FakeToss) -> dict[str, Any]:
    """The settings of a Toss client at ``toss``."""
    return {"toss_client_id": CLIENT_ID, "toss_client_secret": CLIENT_SECRET, "toss_url": toss.url}


def now() -> datetime:
    return datetime.now().astimezone()


def client(session: aiohttp.ClientSession, toss: FakeToss, state: Path, pace: float = 0) -> TossClient:
    return TossClient(
        session, toss.url, TossTokens(session, toss.url, CLIENT_ID, CLIENT_SECRET, state, now), pace
    )


async def test_a_cached_token_is_used_and_an_expired_one_replaced(tmp_path: Path) -> None:
    state = tmp_path / "state"
    async with fake_toss() as toss, aiohttp.ClientSession() as session:
        toss.names = {"005930": "삼성전자"}
        toss.accept("toss-token-cached")
        cache_token(state, "toss-token-cached", now() + timedelta(hours=3))
        tokens = TossTokens(session, toss.url, CLIENT_ID, CLIENT_SECRET, state, now)
        answer = await TossClient(session, toss.url, tokens, 0).get(STOCKS, NAMES)
        assert [s["name"] for s in answer] == ["삼성전자"]
        assert toss.issued == [] and tokens.issued == 0
        assert toss.requests[-1][3] == "toss-token-cached"

        # Expired (or within a minute of it): a new one, cached for every process, mode 600.
        cache_token(state, "toss-token-cached", now() + timedelta(seconds=30))
        await client(session, toss, state).get(STOCKS, NAMES)
        assert toss.issued == ["toss-token-1"] and toss.requests[-1][3] == "toss-token-1"
        saved = json.loads((state / TOKEN_FILE).read_text())
        assert saved["token"] == "toss-token-1"
        assert datetime.fromisoformat(saved["expires"]) > now() + timedelta(hours=23)
        assert stat.S_IMODE(os.stat(state / TOKEN_FILE).st_mode) == 0o600


async def test_a_revoked_token_is_replaced_once_at_most(tmp_path: Path) -> None:
    state = tmp_path / "state"
    async with fake_toss() as toss, aiohttp.ClientSession() as session:
        toss.accept("toss-token-cached")
        cache_token(state, "toss-token-cached", now() + timedelta(hours=3))
        names = client(session, toss, state)
        await names.get(STOCKS, NAMES)
        toss.revoke()  # another process issued one without sharing the cache
        await names.get(STOCKS, NAMES)  # 401 token-revoked: one new token, asked again
        assert toss.issued == ["toss-token-1"]
        toss.revoke()
        with pytest.raises(TossError) as refused:
            await names.get(STOCKS, NAMES)
        assert refused.value.code == "token-revoked" and refused.value.status == 401
        assert toss.issued == ["toss-token-1"]  # not a second time


async def test_a_token_another_process_cached_is_taken_without_issuing(tmp_path: Path) -> None:
    state = tmp_path / "state"
    async with fake_toss() as toss, aiohttp.ClientSession() as session:
        toss.accept("toss-token-cached")
        cache_token(state, "toss-token-cached", now() + timedelta(hours=3))
        names = client(session, toss, state)
        await names.get(STOCKS, NAMES)
        newer = toss.revoke()
        cache_token(state, newer, now() + timedelta(hours=24))  # another process issued and cached it
        await names.get(STOCKS, NAMES)
        assert toss.issued == [] and toss.requests[-1][3] == newer


async def test_an_address_toss_does_not_allow_is_said_plainly(tmp_path: Path) -> None:
    assert (
        IP_MESSAGE
        == "the server's public IP is not in Toss's allowed IPs (WTS 설정 > Open API > 허용 IP 관리)"
    )
    async with fake_toss() as toss, aiohttp.ClientSession() as session:
        toss.ip_allowed = False  # the token endpoint refuses it (access_denied)
        with pytest.raises(TossError) as refused:
            await client(session, toss, tmp_path / "state").get(STOCKS, NAMES)
        assert refused.value.ip_not_allowed and IP_MESSAGE in str(refused.value)

    async with fake_toss() as toss, aiohttp.ClientSession() as session:
        toss.accept("toss-token-cached")
        cache_token(tmp_path / "state", "toss-token-cached", now() + timedelta(hours=3))
        toss.api_ip_allowed = False  # the token is fine, the endpoints refuse the address
        with pytest.raises(TossError) as refused:
            await client(session, toss, tmp_path / "state").get(STOCKS, NAMES)
        assert refused.value.ip_not_allowed and str(refused.value) == f"HTTP 403 {IP_MESSAGE}"
        assert len(toss.requests) == 1  # nothing more is asked


async def test_a_busy_answer_is_waited_out_and_calls_are_paced(tmp_path: Path) -> None:
    async with fake_toss() as toss, aiohttp.ClientSession() as session:
        toss.busy = 1
        names = client(session, toss, tmp_path / "state", 0.05)
        for _ in range(3):
            await names.get(STOCKS, NAMES)
        times = [t for t, *_ in toss.requests]
    assert len(times) == 4  # the 429, then three answers
    assert all(b - a >= 0.045 for a, b in zip(times, times[1:], strict=False))


def test_the_toss_client_is_read_from_the_environment_and_kept_secret(tmp_path: Path) -> None:
    env = {
        "KIS_APP_KEY": "k",
        "KIS_APP_SECRET": "s",
        "TOSS_CLIENT_ID": f" {CLIENT_ID} ",
        "TOSS_CLIENT_SECRET": CLIENT_SECRET,
    }
    settings = Settings.from_env(env, dotenv_path=None)
    assert (settings.toss_client_id, settings.toss_client_secret) == (CLIENT_ID, CLIENT_SECRET)
    assert settings.toss_url == "https://openapi.tossinvest.com"
    assert CLIENT_ID in secrets_of(settings) and CLIENT_SECRET in secrets_of(settings)
    assert CLIENT_ID not in repr(settings) and CLIENT_SECRET not in repr(settings)
    unset = Settings.from_env(
        {"KIS_APP_KEY": "k", "KIS_APP_SECRET": "s", "TOSS_CLIENT_ID": ""}, dotenv_path=None
    )
    assert unset.toss_client_id is None and unset.toss_client_secret is None


def test_toss_order_endpoints_and_client_values_are_forbidden() -> None:
    def hits(text: str) -> list[str]:
        return [name for name, pattern in RULES if pattern.search(text)]

    order, conditional = "/api/v1/" + "orders", "/api/v1/" + "conditional-orders/1/modify"
    assert hits(order) == ["Toss order endpoint"] and hits(conditional) == ["Toss order endpoint"]
    assert hits("/api/v1/stocks?symbols=005930") == []
    assert hits("TOSS_CLIENT_SECRET=" + "a" * 20) == ["Toss credential value"]
    assert hits("TOSS_CLIENT_SECRET=") == []
