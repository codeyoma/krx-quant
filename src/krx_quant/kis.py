"""KIS Open API protocol: authentication and the real-time WebSocket wire format.

Market data only. This module must never contain order endpoints or order
TR IDs; CI rejects them.
"""

from __future__ import annotations

import asyncio
import errno
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import aiohttp

from krx_quant.feeds.base import KST
from krx_quant.ratelimit import RateLimiter

log = logging.getLogger(__name__)

WS_PATH = "/tryitout"

# KIS's REST answers carry ``Keep-Alive: timeout=5, max=100``, and KIS closes a connection left
# idle for 5 s (measured 2026-10-23, #363). aiohttp ignores that header and keeps an idle connection
# for 15 s by default, so it could send a call on a connection KIS had closed or was closing: the
# call failed with "Server disconnected". A connection idle for longer than this is not used again.
# The margin under 5 s covers the round trip and the event loop's stalls (the engine's heartbeat
# was up to 1.2 s late that day).
KEEPALIVE_SECONDS = 3.0


def rest_session(**kwargs: Any) -> aiohttp.ClientSession:
    """A client session for KIS REST calls: it reuses an idle connection only while KIS keeps it
    open (``KEEPALIVE_SECONDS``). ``kwargs`` go to ``aiohttp.ClientSession`` (e.g. ``timeout``)."""
    return aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(keepalive_timeout=KEEPALIVE_SECONDS), **kwargs
    )


class KisError(RuntimeError):
    pass


class ShortFrame(KisError):
    """A data frame with fewer fields than its records need (``RealtimeFrame.records``)."""

    def __init__(self, tr_id: str, count: int, width: int, got: int) -> None:
        super().__init__(f"{tr_id}: expected {count}x{width} fields, got {got}")
        self.expected = count * width
        self.got = got


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
        """Split the payload into ``count`` records of ``width`` fields each.

        Fewer fields than that raise ``ShortFrame``, more raise ``KisError``.
        """
        fields = self.payload.split("^")
        if len(fields) < self.count * width:
            raise ShortFrame(self.tr_id, self.count, width, len(fields))
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

    Market data only: callers pass read TR IDs and quotation paths. Every call is a GET of a
    quotation, which changes nothing at KIS, so sending one again is safe.
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

        A call KIS refuses for its per-second limit is sent again after a short pause. A call
        whose connection closed before KIS answered is sent again at once on a new connection
        (``_ask``).
        """
        body, _ = await self.get_page(tr_id, path, params)
        return body

    async def get_page(
        self, tr_id: str, path: str, params: dict[str, str], tr_cont: str = ""
    ) -> tuple[dict[str, Any], str]:
        """``get``, with KIS's continuation header: the answer's ``tr_cont`` (``M`` or ``F``:
        KIS has another page; ``D`` or ``E``: the last). The next page is asked with the same
        ``params`` and ``tr_cont="N"``."""
        for attempt in range(_BUSY_RETRIES + 1):
            token = await self._access_token()
            headers = {**self._headers, "authorization": f"Bearer {token}", "tr_id": tr_id}
            if tr_cont:
                headers["tr_cont"] = tr_cont
            status, body, more = await self._ask(tr_id, f"{self._rest_url}{path}", params, headers)
            if isinstance(body, dict) and body.get("rt_cd") == "0":
                return body, more
            code = body.get("msg_cd") if isinstance(body, dict) else None
            if code == _BUSY and attempt < _BUSY_RETRIES:
                await asyncio.sleep(_BUSY_PAUSE)
                continue
            if code in _TOKEN_REJECTED and self._on_token_rejected:
                self._on_token_rejected()
            raise KisError(f"{tr_id} {params}: HTTP {status} {body}")
        raise KisError("unreachable: the last attempt returns or raises")

    async def _ask(
        self, tr_id: str, url: str, params: dict[str, str], headers: dict[str, str]
    ) -> tuple[int, Any, str]:
        """One call: KIS's HTTP status, JSON body and ``tr_cont`` header.

        The call goes on the session's pooled connections. When its connection closes before
        the answer, aiohttp (3.14) sends a GET again by itself, once, on the pool's next
        connection, without the limiter; on 2026-10-23 that one was closed too (#363). When the
        call still fails that way (``dropped``), it is sent once more, at once, through the
        limiter, on a new connection of its own, and an INFO line says so. A failure of that send
        is raised, as is any error once KIS has answered: nothing is sent again after an answer,
        whatever its status.
        """
        await self._limiter.acquire()
        try:
            response = await self._session.get(url, params=params, headers=headers)
        except aiohttp.ClientError as e:
            if not dropped(e):
                raise
            first: aiohttp.ClientError = e
        else:
            return await _read(response)
        await self._limiter.acquire()
        # A connector of its own, so the call cannot go on another pooled connection KIS closed.
        connector = aiohttp.TCPConnector(force_close=True)
        async with aiohttp.ClientSession(connector=connector, timeout=self._session.timeout) as fresh:
            # aiohttp's own second send is off on this session, as aiohttp's test client
            # (``aiohttp.test_utils``) turns it off: this is one send, and it went through the limiter.
            fresh._retry_connection = False
            answer = await _read(await fresh.get(url, params=params, headers=headers))
        log.info(
            "KIS REST %s answered on a new connection after its pooled one closed before an answer (%s: %s)",
            tr_id,
            type(first).__name__,
            first,
        )
        return answer


def dropped(error: BaseException) -> bool:
    """Whether ``error`` is a connection closed or reset before KIS answered: KIS sent no answer,
    so the call can be sent again. Not a connection that could not be made (KIS unreachable) nor
    a timeout (KIS may still be working on the call): those wait for the caller's next try."""
    if isinstance(error, aiohttp.ClientConnectorError):
        return False
    if isinstance(error, aiohttp.ServerDisconnectedError | aiohttp.ClientConnectionResetError):
        return True
    return isinstance(error, aiohttp.ClientOSError) and error.errno in (errno.ECONNRESET, errno.EPIPE)


async def _read(response: aiohttp.ClientResponse) -> tuple[int, Any, str]:
    async with response:
        body = await response.json(content_type=None)
        return response.status, body, response.headers.get("tr_cont", "")


# KIS answers these when the access token is expired or not valid.
_TOKEN_REJECTED = {"EGW00121", "EGW00123"}
# KIS refuses some calls with EGW00201 (per-second limit) even under 20 a second: measured
# 2026-10-08, a few refusals at 10–20 calls a second, none at 8. Such a call is retried.
_BUSY = "EGW00201"
_BUSY_RETRIES = 5
_BUSY_PAUSE = 0.5
