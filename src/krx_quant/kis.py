"""KIS Open API protocol: authentication and the real-time WebSocket wire format.

Market data only. This module must never contain order endpoints or order
TR IDs; CI rejects them.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import aiohttp

from krx_quant.feeds.base import KST
from krx_quant.ratelimit import RateLimiter

WS_PATH = "/tryitout"


class KisError(RuntimeError):
    pass


async def issue_access_token(
    session: aiohttp.ClientSession, rest_url: str, app_key: str, app_secret: str
) -> tuple[str, datetime]:
    """REST access token and its expiry (valid ~24h; KIS allows one issuance per minute).

    Use auth.TokenManager rather than calling this directly: it caches and spaces issuances.
    """
    body = {"grant_type": "client_credentials", "appkey": app_key, "appsecret": app_secret}
    async with session.post(f"{rest_url}/oauth2/tokenP", json=body) as resp:
        payload = await resp.json(content_type=None)
        if resp.status != 200 or "access_token" not in payload:
            raise KisError(f"access token request failed ({resp.status}): {payload}")
    # KIS reports expiry as "YYYY-MM-DD HH:MM:SS" in Korean time.
    expires = datetime.strptime(payload["access_token_token_expired"], "%Y-%m-%d %H:%M:%S")
    return str(payload["access_token"]), expires.replace(tzinfo=KST)


async def issue_approval_key(
    session: aiohttp.ClientSession, rest_url: str, app_key: str, app_secret: str
) -> str:
    """WebSocket approval key, required in every subscription request."""
    body = {"grant_type": "client_credentials", "appkey": app_key, "secretkey": app_secret}
    async with session.post(f"{rest_url}/oauth2/Approval", json=body) as resp:
        payload = await resp.json(content_type=None)
        if resp.status != 200 or "approval_key" not in payload:
            raise KisError(f"approval key request failed ({resp.status}): {payload}")
        return str(payload["approval_key"])


def subscribe_message(approval_key: str, tr_id: str, tr_key: str, subscribe: bool = True) -> str:
    """Registration request; ``subscribe=False`` builds the matching unregistration."""
    return json.dumps(
        {
            "header": {
                "approval_key": approval_key,
                "custtype": "P",
                "tr_type": "1" if subscribe else "2",
                "content-type": "utf-8",
            },
            "body": {"input": {"tr_id": tr_id, "tr_key": tr_key}},
        }
    )


@dataclass(frozen=True)
class RealtimeFrame:
    """A real-time data frame: ``<encrypted>|<tr_id>|<count>|<fields joined by ^>``."""

    tr_id: str
    encrypted: bool
    count: int
    payload: str

    @property
    def symbol(self) -> str:
        """The first field of a stock frame is the symbol (MKSC_SHRN_ISCD)."""
        return self.payload.split("^", 1)[0]

    def records(self, width: int) -> list[list[str]]:
        """Split the payload into ``count`` records of ``width`` fields each."""
        fields = self.payload.split("^")
        if len(fields) != self.count * width:
            raise KisError(f"{self.tr_id}: expected {self.count}x{width} fields, got {len(fields)}")
        return [fields[i * width : (i + 1) * width] for i in range(self.count)]


@dataclass(frozen=True)
class SystemMessage:
    """A JSON control message: subscription acknowledgement or PINGPONG heartbeat."""

    tr_id: str
    tr_key: str | None
    ok: bool
    code: str
    message: str

    @property
    def is_pingpong(self) -> bool:
        return self.tr_id == "PINGPONG"

    @property
    def is_limit_rejection(self) -> bool:
        """A registration refused because the session already holds the maximum (41 on 2026-10-08).

        KIS answers ``OPSP0008`` / ``MAX SUBSCRIBE OVER``.
        """
        return not self.ok and (self.code == "OPSP0008" or "MAX SUBSCRIBE" in self.message.upper())

    @property
    def is_invalid_approval(self) -> bool:
        """The approval key was not accepted (``OPSP0011`` / ``invalid approval``)."""
        return not self.ok and (self.code == "OPSP0011" or "INVALID APPROVAL" in self.message.upper())


def parse_message(text: str) -> RealtimeFrame | SystemMessage:
    """Parse one WebSocket text message; raise KisError if it is neither form."""
    if text[:1] in ("0", "1"):
        parts = text.split("|", 3)
        if len(parts) != 4 or not parts[2].isdigit():
            raise KisError(f"malformed data frame: {text[:80]!r}")
        return RealtimeFrame(tr_id=parts[1], encrypted=parts[0] == "1", count=int(parts[2]), payload=parts[3])
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        raise KisError(f"unrecognized message: {text[:80]!r}") from e
    if not isinstance(raw, dict):
        raise KisError(f"unrecognized message: {text[:80]!r}")
    header = raw.get("header") or {}
    body = raw.get("body") or {}
    return SystemMessage(
        tr_id=str(header.get("tr_id", "")),
        tr_key=header.get("tr_key"),
        ok=body.get("rt_cd", "0") == "0",
        code=str(body.get("msg_cd", "")),
        message=str(body.get("msg1", "")),
    )


class RestClient:
    """Authenticated GET calls to KIS quotation endpoints, shared-rate-limited.

    Market data only: callers pass read TR IDs and quotation paths.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        rest_url: str,
        app_key: str,
        app_secret: str,
        access_token: Callable[[], Awaitable[str]],
        limiter: RateLimiter,
        on_token_rejected: Callable[[], None] | None = None,
    ) -> None:
        self._session = session
        self._rest_url = rest_url
        self._access_token = access_token
        self._on_token_rejected = on_token_rejected
        self._headers = {
            "appkey": app_key,
            "appsecret": app_secret,
            "custtype": "P",
            "content-type": "application/json; charset=utf-8",
        }
        self._limiter = limiter

    async def get(self, tr_id: str, path: str, params: dict[str, str]) -> dict[str, Any]:
        """Return the JSON body; raise KisError unless KIS reports success (rt_cd "0").

        A call KIS refuses for its per-second limit is sent again after a short pause.
        """
        for attempt in range(_BUSY_RETRIES + 1):
            token = await self._access_token()
            await self._limiter.acquire()
            headers = {**self._headers, "authorization": f"Bearer {token}", "tr_id": tr_id}
            async with self._session.get(f"{self._rest_url}{path}", params=params, headers=headers) as resp:
                body = await resp.json(content_type=None)
            if isinstance(body, dict) and body.get("rt_cd") == "0":
                return body
            code = body.get("msg_cd") if isinstance(body, dict) else None
            if code == _BUSY and attempt < _BUSY_RETRIES:
                await asyncio.sleep(_BUSY_PAUSE)
                continue
            if code in _TOKEN_REJECTED and self._on_token_rejected:
                self._on_token_rejected()
            raise KisError(f"{tr_id} {params}: HTTP {resp.status} {body}")
        raise KisError("unreachable: the last attempt returns or raises")


# KIS answers these when the access token is expired or not valid.
_TOKEN_REJECTED = {"EGW00121", "EGW00123"}
# KIS refuses some calls with EGW00201 (per-second limit) even under 20 a second: measured
# 2026-10-08, a few refusals at 10–20 calls a second, none at 8. Such a call is retried.
_BUSY = "EGW00201"
_BUSY_RETRIES = 5
_BUSY_PAUSE = 0.5
