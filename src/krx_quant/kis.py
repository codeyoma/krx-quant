"""KIS Open API protocol: authentication and the real-time WebSocket wire format.

Market data only. This module must never contain order endpoints or order
TR IDs; CI rejects them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import aiohttp

WS_PATH = "/tryitout"


class KisError(RuntimeError):
    pass


async def issue_access_token(
    session: aiohttp.ClientSession, rest_url: str, app_key: str, app_secret: str
) -> str:
    """REST access token (valid ~24h; KIS allows one issuance per minute)."""
    body = {"grant_type": "client_credentials", "appkey": app_key, "appsecret": app_secret}
    async with session.post(f"{rest_url}/oauth2/tokenP", json=body) as resp:
        payload = await resp.json(content_type=None)
        if resp.status != 200 or "access_token" not in payload:
            raise KisError(f"access token request failed ({resp.status}): {payload}")
        return str(payload["access_token"])


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


def subscribe_message(approval_key: str, tr_id: str, tr_key: str) -> str:
    return json.dumps(
        {
            "header": {
                "approval_key": approval_key,
                "custtype": "P",
                "tr_type": "1",
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
    message: str

    @property
    def is_pingpong(self) -> bool:
        return self.tr_id == "PINGPONG"


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
        message=str(body.get("msg1", "")),
    )
