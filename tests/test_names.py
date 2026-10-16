"""The symbols' company names and logos (#250).

Input: a fake Toss (``tests/fake_toss.py``) naming some symbols, and the fake KIS serving the
stock masters in KIS's fixed-width layout (short code, standard code, the cp949 name, a 228-
or 222-byte tail). Output: the names ``read_names`` gives, and ``GET /v1/symbols``.
"""

from __future__ import annotations

import io
import logging
import zipfile
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

from krx_quant.api import create_app
from krx_quant.config import Settings
from krx_quant.fake_kis import FakeKis
from krx_quant.names import STOCK_MASTERS, logo_url, parse_stock_master, read_names
from tests.api_schema import check
from tests.fake_toss import fake_toss
from tests.replay_events import at, run
from tests.test_toss_probe import with_toss

KOSPI_TAIL, KOSDAQ_TAIL = (tail for _, tail in STOCK_MASTERS)


def row(code: str, name: str, tail: int) -> bytes:
    """A stock master row: the 9-byte code, the 12-byte standard code, the name, the tail."""
    return f"{code:<9}KR7{code}003".encode() + name.encode("cp949") + b"ST1000000000N".ljust(tail, b"0")


def zipped(name: str, rows: list[bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, b"\r\n".join(rows))
    return buf.getvalue()


def masters(kospi: dict[str, str], kosdaq: dict[str, str]) -> dict[str, bytes]:
    return {
        "kospi_code.mst.zip": zipped("kospi_code.mst", [row(c, n, KOSPI_TAIL) for c, n in kospi.items()]),
        "kosdaq_code.mst.zip": zipped("kosdaq_code.mst", [row(c, n, KOSDAQ_TAIL) for c, n in kosdaq.items()]),
    }


def settings_for(kis: FakeKis, tmp_path: Path, symbols: tuple[str, ...]) -> Settings:
    return Settings("k", "s", symbols=symbols, master_url=kis.master_url, state_dir=tmp_path / "state")


def now() -> datetime:
    return datetime.now().astimezone()


def no_records(symbol: str, name: str) -> list[dict[str, str]]:
    return []


def test_a_stock_master_names_the_wanted_codes_only() -> None:
    data = b"\r\n".join(
        [
            row("005930", "삼성전자", KOSPI_TAIL),
            row("000660", "SK하이닉스", KOSPI_TAIL),
            row("133690", "TIGER 미국나스닥100", KOSPI_TAIL),
        ]
    )

    assert parse_stock_master(data, KOSPI_TAIL, {"005930", "133690", "999999"}) == {
        "005930": "삼성전자",
        "133690": "TIGER 미국나스닥100",
    }


def test_a_name_cut_inside_a_character_loses_that_half() -> None:
    # KIS cuts a name too long for its field at a byte, maybe inside a two-byte character.
    cut = f"{'0008S0':<9}KR70008S0004".encode() + "삼성전자".encode("cp949")[:-1] + b"0" * KOSPI_TAIL

    assert parse_stock_master(cut, KOSPI_TAIL, {"0008S0"}) == {"0008S0": "삼성전"}


async def test_without_toss_the_kis_masters_name_the_symbols(kis: FakeKis, tmp_path: Path) -> None:
    kis.masters = masters({"005930": "삼성전자", "000660": "SK하이닉스"}, {"247540": "에코프로비엠"})
    settings = settings_for(kis, tmp_path, ("005930", "247540"))

    async with aiohttp.ClientSession() as session:
        names = await read_names(session, settings, settings.symbols, now)

    assert names == {"005930": "삼성전자", "247540": "에코프로비엠"}


async def test_toss_names_first_and_the_masters_name_the_rest(kis: FakeKis, tmp_path: Path) -> None:
    kis.masters = masters({"005930": "삼성전자(마스터)", "009150": "삼성전기"}, {})
    async with fake_toss(no_records) as toss:
        toss.names = {"005930": "삼성전자"}
        settings = replace(settings_for(kis, tmp_path, ("005930", "009150")), **with_toss(toss))

        async with aiohttp.ClientSession() as session:
            names = await read_names(session, settings, settings.symbols, now)

    assert names == {"005930": "삼성전자", "009150": "삼성전기"}, "Toss's name wins"
    asked = [(path, query) for _, path, query, _ in toss.requests]
    assert asked == [("/api/v1/stocks", {"symbols": "005930,009150"})], "one call for all"


async def test_when_toss_names_every_symbol_the_masters_are_not_read(
    kis: FakeKis, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async with fake_toss(no_records) as toss:
        toss.names = {"005930": "삼성전자"}
        settings = replace(settings_for(kis, tmp_path, ("005930",)), **with_toss(toss))

        async with aiohttp.ClientSession() as session:
            with caplog.at_level(logging.WARNING):
                names = await read_names(session, settings, settings.symbols, now)

    assert names == {"005930": "삼성전자"}
    assert caplog.text == "", "no master is served: asking would have warned"


async def test_a_refusing_toss_falls_back_to_the_masters(
    kis: FakeKis, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    kis.masters = masters({"005930": "삼성전자"}, {})
    async with fake_toss(no_records) as toss:
        toss.names = {"005930": "토스의 이름"}
        toss.api_ip_allowed = False
        settings = replace(settings_for(kis, tmp_path, ("005930",)), **with_toss(toss))

        async with aiohttp.ClientSession() as session:
            with caplog.at_level(logging.WARNING):
                names = await read_names(session, settings, settings.symbols, now)

    assert names == {"005930": "삼성전자"}
    assert "Toss did not name the symbols" in caplog.text
    assert "toss-token" not in caplog.text, "the token is never logged"


async def test_a_symbol_no_source_names_is_left_out(
    kis: FakeKis, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = settings_for(kis, tmp_path, ("005930",))  # no masters served: 404

    async with aiohttp.ClientSession() as session:
        with caplog.at_level(logging.WARNING):
            names = await read_names(session, settings, settings.symbols, now)

    assert names == {}
    assert "the KIS stock masters could not be read" in caplog.text
    assert "no company name for 005930" in caplog.text


async def test_the_symbols_carry_their_name_and_toss_logo(tmp_path: Path) -> None:
    engine = await run(tmp_path / "engine.db", [], until=at("09:00:00"))
    names: dict[str, str] = {}

    async with TestClient(TestServer(create_app(engine, names=names))) as client:
        before = await (await client.get("/v1/symbols")).json()
        names["005930"] = "삼성전자"  # the collector fills them in after the API starts
        after = await (await client.get("/v1/symbols")).json()

    for body in (before, after):
        check("symbols", body)
    assert [s["name"] for s in before["symbols"]] == [None]
    assert [s["name"] for s in after["symbols"]] == ["삼성전자"]
    assert after["symbols"][0]["logo_url"] == (
        "https://static.toss.im/png-icons/securities/icn-sec-fill-005930.png"
    )
    assert logo_url("000660").endswith("/icn-sec-fill-000660.png")
