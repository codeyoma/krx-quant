"""Toss Securities Open API: the access token and authenticated GET calls (#226, #235).

Market data only: callers ask the stock information endpoints. This module must never hold
order or account endpoints (``scripts/check_forbidden.py`` rejects the order ones).

Toss keeps one valid access token per client: issuing one revokes the one before it, wherever
it is in use (401 ``token-revoked``). So every process of this client (the supply probe now,
the collector later) shares the token cached in the state directory (``toss_token.json``,
mode 600, like the KIS token in ``auth.py``), and a new one is issued only when the cached
one is missing or expired (its ``expires_in``, less ``EXPIRY_MARGIN``). Issuing holds a file
lock, so two processes do not both issue. A call Toss refuses for its token reads the cache
again (another process may have issued since) and, if it still holds the refused token,
issues one; a client does that once at most.

Toss answers only allowed addresses (WTS 설정 > Open API > 허용 IP 관리): any other gets 403
``ip-not-allowed`` (``access_denied`` from the token endpoint), reported as ``IP_MESSAGE``.
The token is never logged or put in an error.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import json
import logging
import os
import time
from collections.abc import Callable, Iterator, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

TOKEN_FILE = "toss_token.json"
LOCK_FILE = ".toss_token.lock"
EXPIRY_MARGIN = timedelta(minutes=1)  # a token this close to its expiry counts as expired
IP_NOT_ALLOWED = "ip-not-allowed"
IP_MESSAGE = "the server's public IP is not in Toss's allowed IPs (WTS 설정 > Open API > 허용 IP 관리)"
TOKEN_REJECTED = frozenset({"token-revoked", "expired-token", "invalid-token"})
STOCKS = "/api/v1/stocks"  # the stock information endpoints: /api/v1/stocks/{symbol}/...
_BUSY_RETRIES = 3  # a 429 is waited out and asked again this many times
_MAX_WAIT = 5.0  # seconds, at most, a 429's Retry-After is waited


class TossError(RuntimeError):
    """Toss refused a call: its HTTP status, error code and message (never the token)."""

    def __init__(self, status: int, code: str, message: str = "") -> None:
        self.status = status
        self.code = code
        self.message = message
        text = IP_MESSAGE if code == IP_NOT_ALLOWED else " ".join(p for p in (code, message) if p)
        super().__init__(f"HTTP {status} {text}".strip())

    @property
    def ip_not_allowed(self) -> bool:
        return self.code == IP_NOT_ALLOWED


def error_of(status: int, body: Any) -> TossError:
    """Toss's refusal from its body: the API's envelope (``error.code``) or OAuth2's (``error``,
    from the token endpoint, whose 403 ``access_denied`` is the IP allowlist)."""
    if isinstance(body, Mapping):
        error = body.get("error")
        if isinstance(error, Mapping):
            return TossError(status, str(error.get("code") or ""), str(error.get("message") or ""))
        if isinstance(error, str):
            code = IP_NOT_ALLOWED if status == 403 and error == "access_denied" else error
            return TossError(status, code, str(body.get("error_description") or ""))
    return TossError(status, "", "no error code")


async def _json(response: aiohttp.ClientResponse) -> Any:
    try:
        return await response.json(content_type=None)
    except ValueError:
        return None


class TossTokens:
    """The client's one access token, shared through the state directory."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        url: str,
        client_id: str,
        client_secret: str,
        state_dir: Path,
        now: Callable[[], datetime],
    ) -> None:
        self._session = session
        self._url = url
        self._client_id = client_id
        self._client_secret = client_secret
        self._path = state_dir / TOKEN_FILE
        self._lock_path = state_dir / LOCK_FILE
        self._now = now
        # The cache is only valid for the client it was issued to.
        self._key_id = hashlib.sha256(client_id.encode()).hexdigest()[:16]
        self._lock = asyncio.Lock()
        self.issued = 0  # tokens this process issued

    def cached(self) -> str | None:
        """The cached token of this client, if it has not expired."""
        try:
            data = json.loads(self._path.read_text())
            if data.get("key_id") != self._key_id:
                return None
            token, expires = str(data["token"]), datetime.fromisoformat(data["expires"])
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None
        return token if expires - EXPIRY_MARGIN > self._now() else None

    async def get(self, rejected: str | None = None) -> str:
        """A usable token: the cached one, else a new one. ``rejected``: Toss refused that
        token; a different cached one (another process issued it) is used, else a new one.
        Raises TossError."""
        async with self._lock:
            with self._file_lock():
                token = self.cached()
                if token is not None and token != rejected:
                    return token
                return await self._issue()

    @contextlib.contextmanager
    def _file_lock(self) -> Iterator[None]:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._lock_path, "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    async def _issue(self) -> str:
        form = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }
        async with self._session.post(f"{self._url}/oauth2/token", data=form) as response:
            status, body = response.status, await _json(response)
        if status != 200 or not isinstance(body, Mapping) or not body.get("access_token"):
            raise error_of(status, body)
        token = str(body["access_token"])
        # Toss always says (a day); without it, an hour, rather than issuing again at once.
        expires = self._now() + timedelta(seconds=float(body.get("expires_in") or 3600))
        self._save(token, expires)
        self.issued += 1
        log.info("issued a Toss access token valid until %s", expires)
        return token

    def _save(self, token: str, expires: datetime) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.unlink(missing_ok=True)  # O_CREAT's mode only applies to a new file
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(
                {
                    "key_id": self._key_id,
                    "token": token,
                    "expires": expires.isoformat(),
                    "issued_at": time.time(),
                },
                handle,
            )
        tmp.replace(self._path)


class TossClient:
    """Authenticated GET calls to Toss's market data, ``min_interval`` seconds apart at least."""

    def __init__(
        self, session: aiohttp.ClientSession, url: str, tokens: TossTokens, min_interval: float = 0.2
    ) -> None:
        self._session = session
        self._url = url
        self._tokens = tokens
        self._min_interval = min_interval
        self._last: float | None = None
        self._renewed = False
        self.requests = 0

    async def get(self, path: str, params: Mapping[str, str]) -> Any:
        """The answer's ``result``; raises TossError. A token Toss refuses is replaced once in
        this client's life at most; a 429 waits its Retry-After (``_MAX_WAIT`` at most) and is
        asked again, ``_BUSY_RETRIES`` times."""
        token = await self._tokens.get()
        busy = 0
        while True:
            await self._pace()
            self.requests += 1
            headers = {"Authorization": f"Bearer {token}"}
            async with self._session.get(
                f"{self._url}{path}", params=dict(params), headers=headers
            ) as response:
                status, wait, body = (
                    response.status,
                    response.headers.get("Retry-After"),
                    await _json(response),
                )
            if status == 200 and isinstance(body, Mapping) and "result" in body:
                return body["result"]
            error = error_of(status, body)
            if status == 401 and error.code in TOKEN_REJECTED and not self._renewed:
                self._renewed = True
                token = await self._tokens.get(rejected=token)
                continue
            if status == 429 and busy < _BUSY_RETRIES:
                busy += 1
                await asyncio.sleep(_seconds(wait))
                continue
            raise error

    async def _pace(self) -> None:
        if self._last is not None:
            wait = self._min_interval - (time.monotonic() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
        self._last = time.monotonic()


def _seconds(retry_after: str | None) -> float:
    try:
        return min(max(float(retry_after or 1), 0.0), _MAX_WAIT)
    except ValueError:
        return 1.0
