"""Nearest-month derivative codes from the KIS master files.

KIS publishes daily master files of every listed contract. Futures codes roll
(KOSPI200 quarterly, USD futures monthly), so the collector reads the masters
at startup instead of hard-coding codes. Format notes, checked on 2026-10-08:

- ``fo_idx_code_mts.mst``: ``|``-separated rows ``type|short code|standard code|
  name|ATM flag|strike|month code|underlying code|underlying name``. Type ``1`` is an
  index future, ``5`` a call option; month code ``1`` marks the nearest month;
  underlying ``2001`` is KOSPI200. Option names look like ``C 202610   750.0``.
- ``fo_com_code.mst``: fixed-width rows; ``[0]`` category (``2`` currency), ``[1]``
  type (``1`` future), ``[2:11]`` short code, and later a 9-digit block whose last
  digit is the month code, followed by the underlying code (``USD``).
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass

import aiohttp

_COM_MONTH_UNDERLYING = re.compile(r"(\d{9})([A-Z]{3})")


class MasterError(RuntimeError):
    pass


@dataclass(frozen=True)
class FrontMonths:
    kospi200_futures: str
    usd_futures: str
    # Nearest-expiry KOSPI200 options around the money: calls then puts, by strike.
    kospi200_options: tuple[str, ...]


async def fetch_front_months(
    session: aiohttp.ClientSession, master_url: str, strikes_each_side: int = 2
) -> FrontMonths:
    idx = await _download(session, f"{master_url}/fo_idx_code_mts.mst.zip")
    com = await _download(session, f"{master_url}/fo_com_code.mst.zip")
    futures, options = parse_index_master(idx, strikes_each_side)
    return FrontMonths(futures, parse_commodity_master(com, "USD"), options)


def parse_index_master(text: str, strikes_each_side: int = 2) -> tuple[str, tuple[str, ...]]:
    """Return the nearest KOSPI200 futures code and option codes around the money.

    Options are the nearest expiry's ATM strike (the master's ATM flag ``1``, set from the
    previous close) and ``strikes_each_side`` strikes above and below it, calls then puts.
    """
    futures: str | None = None
    # (kind, month) -> {strike: code}; kind 5 = call, 6 = put
    options: dict[tuple[str, str], dict[float, str]] = {}
    atm: dict[str, float] = {}
    for line in text.splitlines():
        cols = line.split("|")
        if len(cols) < 8 or cols[7].strip() != "2001":
            continue
        kind, code, name, month_code = cols[0].strip(), cols[1].strip(), cols[3].split(), cols[6].strip()
        if kind == "1" and month_code == "1":
            futures = code
        elif kind in ("5", "6") and len(name) >= 3 and name[1].isdigit():
            strike = float(name[2].replace(",", ""))
            options.setdefault((kind, name[1]), {})[strike] = code
            if cols[4].strip() == "1":
                atm[name[1]] = strike
    months = sorted({month for _, month in options})
    if futures is None or not months or months[0] not in atm:
        raise MasterError("KOSPI200 nearest futures or ATM options not found in fo_idx_code_mts")
    month, center = months[0], atm[months[0]]
    codes: list[str] = []
    for kind in ("5", "6"):
        strikes = sorted(options.get((kind, month), {}))
        if center not in strikes:
            continue
        i = strikes.index(center)
        for strike in strikes[max(0, i - strikes_each_side) : i + strikes_each_side + 1]:
            codes.append(options[kind, month][strike])
    return futures, tuple(codes)


def parse_commodity_master(text: str, underlying: str) -> str:
    """Return the nearest-month futures code for ``underlying`` (e.g. USD)."""
    for line in text.splitlines():
        if line[:2] != "21":  # currency futures
            continue
        match = _COM_MONTH_UNDERLYING.search(line, 11)
        if match and match.group(2) == underlying and match.group(1)[-1] == "1":
            return line[2:11].strip()
    raise MasterError(f"nearest {underlying} futures not found in fo_com_code")


async def _download(session: aiohttp.ClientSession, url: str) -> str:
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
        if resp.status != 200:
            raise MasterError(f"{url}: HTTP {resp.status}")
        data = await resp.read()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return z.read(z.namelist()[0]).decode("cp949")
