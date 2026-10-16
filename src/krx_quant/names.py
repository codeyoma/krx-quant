"""The symbols' company names and logos, for the screen's symbols panel (#250).

The screen shows a predicted symbol as Toss Securities does (tossinvest.com): a round logo,
the company's name, and the code under it. ``GET /v1/symbols`` gives both.

- Name: the Toss Open API's stock information (``GET /api/v1/stocks?symbols=…``, ``name``, up
  to 200 symbols a call, ``STOCK`` group) when its client is set (``TOSS_CLIENT_ID``,
  ``TOSS_CLIENT_SECRET``). A symbol Toss does not name, or every symbol when Toss is not set
  or does not answer, takes the Korean short name of the KIS stock masters
  (``kospi_code.mst``, ``kosdaq_code.mst``, no key needed). On 2026-10-16 the two named
  005930, 000660, 009150, 133690 and 381180 alike.
- Logo: the square PNG tossinvest.com shows (``LOGO_URL``, 480×480; Toss crops it round). A
  code Toss has no logo for answers 403, and the screen draws its own.

The collector reads the names once when it starts; it restarts every trading day. Nothing is
stored: Toss's data is for the user's own trading only, so neither the names nor the logos go
into the repository or onto the disk.

KIS stock master rows (checked 2026-10-16) are fixed-width cp949 bytes: ``[0:9]`` the short
code, ``[9:21]`` the standard code, then the Korean name up to the fixed-width tail, 228 bytes
in ``kospi_code.mst`` and 222 in ``kosdaq_code.mst``. A name too long for its field is cut.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Mapping, Sequence
from datetime import datetime

import aiohttp

from krx_quant.config import Settings
from krx_quant.masters import MasterError, download
from krx_quant.toss import STOCKS, TossClient, TossError, TossTokens

log = logging.getLogger(__name__)

LOGO_URL = "https://static.toss.im/png-icons/securities/icn-sec-fill-{symbol}.png"
# The KIS stock masters and the width of their rows' tail after the name.
STOCK_MASTERS = (("kospi_code.mst.zip", 228), ("kosdaq_code.mst.zip", 222))
_TOSS_MAX_SYMBOLS = 200  # symbols one stock information call takes


def logo_url(symbol: str) -> str:
    return LOGO_URL.format(symbol=symbol)


def parse_stock_master(data: bytes, tail: int, wanted: Collection[str]) -> dict[str, str]:
    """The names of the ``wanted`` codes in a KIS stock master."""
    names: dict[str, str] = {}
    for line in data.splitlines():
        code = line[:9].decode("ascii", "replace").strip()
        if code in wanted:
            # A name cut inside a two-byte character loses that half.
            name = line[21 : len(line) - tail].decode("cp949", "ignore").strip()
            if name:
                names[code] = name
    return names


async def toss_names(
    session: aiohttp.ClientSession, settings: Settings, symbols: Sequence[str], now: Callable[[], datetime]
) -> dict[str, str]:
    """Toss's names of ``symbols``; raises TossError. Needs the Toss client."""
    assert settings.toss_client_id and settings.toss_client_secret
    tokens = TossTokens(
        session,
        settings.toss_url,
        settings.toss_client_id,
        settings.toss_client_secret,
        settings.state_dir,
        now,
    )
    client = TossClient(session, settings.toss_url, tokens)
    names: dict[str, str] = {}
    for i in range(0, len(symbols), _TOSS_MAX_SYMBOLS):
        asked = symbols[i : i + _TOSS_MAX_SYMBOLS]
        result = await client.get(STOCKS, {"symbols": ",".join(asked)})
        for item in result if isinstance(result, list) else ():
            if not isinstance(item, Mapping):
                continue
            symbol, name = item.get("symbol"), item.get("name")
            if isinstance(symbol, str) and symbol in asked and isinstance(name, str) and name.strip():
                names[symbol] = name.strip()
    return names


async def master_names(
    session: aiohttp.ClientSession, master_url: str, symbols: Sequence[str]
) -> dict[str, str]:
    """The KIS stock masters' names of ``symbols``; raises MasterError."""
    names: dict[str, str] = {}
    for file, tail in STOCK_MASTERS:
        missing = set(symbols) - names.keys()
        if not missing:
            break
        names.update(parse_stock_master(await download(session, f"{master_url}/{file}"), tail, missing))
    return names


async def read_names(
    session: aiohttp.ClientSession, settings: Settings, symbols: Sequence[str], now: Callable[[], datetime]
) -> dict[str, str]:
    """The company names of ``symbols``: Toss's, then the KIS masters' for the rest. A source
    that fails is logged and passed over; a symbol neither names is left out."""
    names: dict[str, str] = {}
    if settings.toss_client_id and settings.toss_client_secret:
        try:
            names.update(await toss_names(session, settings, symbols, now))
        except (TossError, aiohttp.ClientError, OSError, ValueError, TimeoutError) as e:
            log.warning("Toss did not name the symbols (%s): the KIS masters name them", e)
    missing = [s for s in symbols if s not in names]
    if missing:
        try:
            names.update(await master_names(session, settings.master_url, missing))
        except (MasterError, aiohttp.ClientError, OSError, ValueError, TimeoutError) as e:
            log.warning("the KIS stock masters could not be read (%s)", e)
    unnamed = [s for s in symbols if s not in names]
    if unnamed:
        log.warning("no company name for %s: the screen shows the code", ", ".join(unnamed))
    return names


async def fill_names(
    names: dict[str, str],
    session: aiohttp.ClientSession,
    settings: Settings,
    now: Callable[[], datetime],
) -> None:
    """Fill ``names`` (the Engine API's) with the symbols' names, in the background of a start."""
    names.update(await read_names(session, settings, settings.symbols, now))
    log.info("company names: %s", ", ".join(f"{s} {n}" for s, n in names.items()))
