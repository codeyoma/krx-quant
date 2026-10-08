"""KIS REST access-token lifecycle.

KIS issues at most one access token per minute per app key (EGW00133 otherwise) and
a token lives about a day. The manager therefore:

- caches the token on disk (mode 600) so restarts reuse it instead of re-issuing;
- refreshes it lazily, on the first request inside the refresh margin before expiry;
- spaces issuances at least ``min_interval`` apart, and when KIS still refuses one
  (another process issued a token), waits that interval and retries.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import aiohttp

from krx_quant import kis

log = logging.getLogger(__name__)

_RETRIES = 3


class TokenManager:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        rest_url: str,
        app_key: str,
        app_secret: str,
        cache_path: Path,
        now: Callable[[], datetime],
        refresh_margin: timedelta = timedelta(hours=1),
        min_interval: float = 61.0,
    ) -> None:
        self._session = session
        self._rest_url = rest_url
        self._app_key = app_key
        self._app_secret = app_secret
        self._cache_path = cache_path
        self._now = now
        self._refresh_margin = refresh_margin
        self._min_interval = min_interval
        # The cache is only valid for the app key it was issued to.
        self._key_id = hashlib.sha256(app_key.encode()).hexdigest()[:16]
        self._token: str | None = None
        self._expires: datetime | None = None
        self._last_issue: float | None = None  # time.monotonic() of the last issuance
        self._issued_at: float | None = None  # the same moment as wall time, persisted
        self._lock = asyncio.Lock()
        self._load()

    async def get(self) -> str:
        """A usable token. Inside the refresh margin a refresh is tried, but a refused or
        failed refresh never blocks callers while the current token is still valid.
        """
        async with self._lock:
            now = self._now()
            if self._token is not None and self._expires is not None and self._expires > now:
                if self._expires - now > self._refresh_margin or not self._may_issue():
                    return self._token
                try:
                    return await self._issue_once()
                except (kis.KisError, aiohttp.ClientError, OSError, ValueError, KeyError) as e:
                    log.warning("token refresh failed (%s); the current token is still valid", e)
                    return self._token
            return await self._issue()

    def invalidate(self) -> None:
        """KIS rejected the token: the next ``get`` issues a new one."""
        self._token = self._expires = None

    def _may_issue(self) -> bool:
        return self._last_issue is None or time.monotonic() - self._last_issue >= self._min_interval

    async def _issue(self) -> str:
        """No usable token: issue one, waiting out the once-a-minute limit as needed."""
        for attempt in range(1, _RETRIES + 1):
            if self._last_issue is not None:
                wait = self._min_interval - (time.monotonic() - self._last_issue)
                if wait > 0:
                    await asyncio.sleep(wait)
            try:
                return await self._issue_once()
            except kis.KisError as e:
                if "EGW00133" not in str(e) or attempt == _RETRIES:
                    raise
                log.warning("token issuance refused for the once-a-minute limit; retrying")
        raise kis.KisError("access token not issued")  # unreachable: the loop returns or raises

    async def _issue_once(self) -> str:
        self._last_issue = time.monotonic()
        self._issued_at = time.time()
        token, expires = await kis.issue_access_token(
            self._session, self._rest_url, self._app_key, self._app_secret
        )
        self._token, self._expires = token, expires
        self._save()
        log.info("issued a KIS access token valid until %s", expires)
        return token

    def _load(self) -> None:
        try:
            data = json.loads(self._cache_path.read_text())
        except (OSError, ValueError):
            return
        if data.get("key_id") != self._key_id:
            return
        try:
            self._token = str(data["token"])
            self._expires = datetime.fromisoformat(data["expires"])
            # Keep the once-a-minute spacing across restarts.
            self._issued_at = float(data["issued_at"])
            self._last_issue = time.monotonic() - max(0.0, time.time() - self._issued_at)
        except (KeyError, ValueError, TypeError):
            self._token = self._expires = None

    def _save(self) -> None:
        assert self._token is not None and self._expires is not None
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._cache_path.with_suffix(".tmp")
        tmp.unlink(missing_ok=True)  # O_CREAT's mode only applies to a new file
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(
                {
                    "key_id": self._key_id,
                    "token": self._token,
                    "expires": self._expires.isoformat(),
                    "issued_at": self._issued_at,
                },
                f,
            )
        tmp.replace(self._cache_path)
