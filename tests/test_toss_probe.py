"""The Toss part of ``krx-quant probe-supply`` (#235), and the Toss client it uses.

Input: a fake Toss (``tests/fake_toss.py``) keeping Toss's rules (one valid token per client,
the IP allowlist, pages by ``nextUntil``) and serving five trading-trend series whose parts
start at different days, beside the fake KIS of ``test_supply_probe``. Every value is a
sentinel number. Output: the printed report, which must carry none of them, nor the token.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import replace
from datetime import date, datetime, timedelta
from functools import cache
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from krx_quant import toss_probe
from krx_quant.config import Settings
from krx_quant.fake_kis import FakeKis
from krx_quant.forbidden import RULES
from krx_quant.redaction import secrets_of
from krx_quant.toss import IP_MESSAGE, TOKEN_FILE, TossClient, TossError, TossTokens
from krx_quant.toss_probe import probe_toss
from tests.fake_toss import CLIENT_ID, CLIENT_SECRET, FakeToss, fake_toss
from tests.test_supply_probe import ETF, SENTINELS, TODAY, credit_balance, probed, weekdays

YESTERDAY = TODAY - timedelta(days=1)
INVESTOR_FROM = date(2016, 1, 4)
INDIVIDUAL_FROM = date(2018, 1, 2)  # individuals are null before this
HOLDING_FROM = date(2020, 1, 2)  # foreign holdings are null before this
SHORT_FROM, SHORT_RATE_FROM = date(2021, 1, 4), date(2022, 1, 3)
CREDIT_FROM, STOCK_LOAN_FROM = date(2023, 1, 2), date(2024, 1, 2)
LENDING_FROM = date(2021, 6, 1)
TOSS_SENTINELS = ("31415926", "27182818", "46692015", "0.4728", "0.01618", "1234567", "2345678")


def volume(net: str) -> dict[str, str]:
    return {"buyVolume": "31415926", "sellVolume": "27182818", "netBuyVolume": net}


def investor_record(day: date) -> dict[str, Any]:
    today = day == TODAY  # provisional: Toss fills these in the evening
    holding = {
        "holdingQuantity": "46692016" if day == YESTERDAY else "46692015",
        "limitQuantity": "98765432",
        "holdingRate": "0.4728",
    }
    return {
        "date": day.isoformat(),
        "updatedAt": f"{day.isoformat()}T18:30:00+09:00",
        "individual": None if today or day < INDIVIDUAL_FROM else volume("-2718281"),
        "foreigner": volume("-7314158"),  # the registered foreigners, as KIS's frgn_reg_ntby_qty
        "institution": {**volume("14142135"), "breakdown": None if today else {"bank": volume("1234567")}},
        "otherCorporation": None if today else volume("2345678"),
        "foreignerHolding": None if today or day < HOLDING_FROM else holding,
        "cfd": None,
    }


def short_record(day: date) -> dict[str, Any]:
    return {
        "date": day.isoformat(),
        "updatedAt": f"{day.isoformat()}T18:30:00+09:00",
        "shortSellingVolume": "7314159",
        "shortSellingAmount": "98765432",
        "shortSellingVolumeRate": "0.01618" if day >= SHORT_RATE_FROM else None,
        "shortSellingAmountRate": "0.01618" if day >= SHORT_RATE_FROM else None,
    }


def credit_record(day: date) -> dict[str, Any]:
    detail = {
        "newQuantity": "1234567",
        "returnQuantity": "2345678",
        # As KIS's whol_loan_rmnd_stcn by its settlement day (the server's probe, #226): the
        # balance of the deal two days before.
        "balanceQuantity": credit_balance(day - timedelta(days=2)),
        "balanceRate": "0.01618",
        "tradingRate": "0.4728",
    }
    return {
        "date": day.isoformat(),
        "updatedAt": f"{TODAY.isoformat()}T05:00:00+09:00",
        "marginLoan": detail,
        "stockLoan": detail if day >= STOCK_LOAN_FROM else None,
    }


def lending_record(day: date) -> dict[str, Any]:
    return {
        "date": day.isoformat(),
        "updatedAt": f"{day.isoformat()}T18:30:00+09:00",
        "executionQuantity": "1234567",
        "repaymentQuantity": "2345678",
        "balanceQuantity": "14142135",
        "balanceAmount": "98765432",
    }


def program_record(day: date) -> dict[str, Any]:
    return {"date": day.isoformat(), "arbitrage": volume("1234567"), "nonArbitrage": volume("-2345678")}


@cache
def series(name: str) -> list[dict[str, Any]]:
    """An endpoint's records, newest first."""
    make, first, last = {
        "investor-trading": (investor_record, INVESTOR_FROM, TODAY),
        "short-selling": (short_record, SHORT_FROM, TODAY),
        "credit-trades": (credit_record, CREDIT_FROM, YESTERDAY),  # T+1
        "securities-lending": (lending_record, LENDING_FROM, TODAY),
        "program-trades": (program_record, INVESTOR_FROM, TODAY),
    }[name]
    return [make(day) for day in weekdays(first, last, 10**6)]


def records(symbol: str, name: str) -> list[dict[str, Any]] | tuple[int, str]:
    if symbol == ETF and name == "credit-trades":
        return 404, "stock-not-found"
    return series(name)


def with_toss(toss: FakeToss) -> dict[str, Any]:
    return {"toss_client_id": CLIENT_ID, "toss_client_secret": CLIENT_SECRET, "toss_url": toss.url}


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


def now() -> datetime:
    return datetime.now().astimezone()


@pytest.fixture(autouse=True)
def no_pace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(toss_probe, "PACE", 0)


async def test_each_endpoint_s_latest_page_history_and_null_parts(
    kis: FakeKis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with fake_toss(records) as toss:
        report, _, failed = await probed(kis, tmp_path, monkeypatch, **with_toss(toss))
    assert (
        "  token: issued a new one (none was cached, or it had expired); shared through state/toss_token.json"
        in report
    )
    assert "[T1] investor-trading 투자자별 매매동향: GET /api/v1/stocks/{symbol}/investor-trading" in report
    assert (
        "    005930: OK, 100 records, 2026-05-29..2026-10-15, newest first, nextUntil 2026-05-28"
        " · individual.netBuyVolume int±, institution.netBuyVolume int, foreigner.netBuyVolume int±,"
        " foreignerHolding.holdingQuantity int, foreignerHolding.limitQuantity int,"
        " foreignerHolding.holdingRate dec" in report
    )
    assert f"    {ETF}: OK, 100 records" in report
    # The first symbol paged back to the first day, by nextUntil.
    assert "  history of 005930 (count 100, paging back by nextUntil, at most 30 calls):" in report
    assert (
        "    2814 records in 29 calls (at most 100 a call), oldest 2016-01-04: nextUntil null, nothing older"
        in report
    )
    assert "    gaps over 10 days: none" in report
    # From when each part is set; today's record is provisional.
    assert (
        "      individual: from 2018-01-02 to 2026-10-14 (2292 of 2814 days; null on 1 later day)" in report
    )
    assert (
        "      foreignerHolding: from 2020-01-02 to 2026-10-14 (1770 of 2814 days; null on 1 later day)"
        in report
    )
    assert (
        "      foreigner: from 2016-01-04 to 2026-10-15 (2814 of 2814 days; null on 0 later days)" in report
    )
    assert "      cfd: null on every day" in report
    assert (
        "    the newest record (2026-10-15) is today's: provisional, Toss completes it in the evening;"
        " null in it: individual, institution.breakdown, otherCorporation, foreignerHolding, cfd" in report
    )
    # Short selling's ratios start later; credit is a day behind; the ETF has no credit data.
    assert "      shortSellingVolumeRate: from 2022-01-03 to 2026-10-15" in report
    assert "    the newest record is 2026-10-14: today's is not out (yet)" in report
    assert f"    {ETF}: error HTTP 404 stock-not-found" in report
    assert "      stockLoan: from 2024-01-02 to 2026-10-14" in report
    assert failed == report.count(": error ")


def check(report: str, toss: str, kis: str) -> str:
    """The cross-check line of a Toss field against a KIS one, after its label."""
    label = f"{toss} vs {kis}"
    lines = report.split("cross-checks with KIS (005930;")[1].splitlines()
    [line] = [x for x in lines if f"{label}:" in x or f"{label} (" in x]
    return line.split(label, 1)[1]


async def test_toss_is_cross_checked_with_kis_by_counts_only(
    kis: FakeKis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with fake_toss(records) as toss:
        report, _, _ = await probed(kis, tmp_path, monkeypatch, **with_toss(toss))
    month = "30 common days (2026-09-04..2026-10-15)"
    # Individuals: KIS's KRX answer but today (Toss's is provisional, null); not the integrated.
    assert (
        check(report, "individual.netBuyVolume", "FHPTJ04160001 J prsn_ntby_qty") == f": {month}; equal on 29"
    )
    assert (
        check(report, "individual.netBuyVolume", "FHPTJ04160001 UN prsn_ntby_qty") == f": {month}; equal on 0"
    )
    # Toss's foreigners are the registered ones.
    assert (
        check(report, "foreigner.netBuyVolume", "FHPTJ04160001 UN frgn_reg_ntby_qty")
        == f": {month}; equal on 30"
    )
    assert (
        check(report, "foreigner.netBuyVolume", "FHPTJ04160001 UN frgn_ntby_qty") == f": {month}; equal on 0"
    )
    ratio = "FHKST01010400 UN hts_frgn_ehrt"
    assert check(report, "holdingRate x100", ratio) == f" (within 0.01): {month}; equal on 29"
    assert check(report, "limitQuantity x100", ratio) == f" (within 0.01): {month}; equal on 29"
    hundred = "100 common days (2026-05-29..2026-10-15)"
    assert (
        check(report, "shortSellingVolume", "FHPST04830000 J ssts_cntg_qty") == f": {hundred}; equal on 100"
    )
    lending = "HHPST074500C0 3 rmnd_stcn by bsop_date"
    assert check(report, "balanceQuantity", lending) == f": {hundred}; equal on 100"
    # Credit balances are KIS's by its settlement day, not by its trading day (the server's probe on
    # #226: equal on 30 of 30 days by stlm_date, on none by deal_date).
    credit = "FHPST04760000 J whol_loan_rmnd_stcn by"
    by_settlement = check(report, "marginLoan.balanceQuantity", f"{credit} stlm_date")
    assert re.fullmatch(r": (\d+) common days \(.+\); equal on \1", by_settlement), by_settlement
    by_deal = check(report, "marginLoan.balanceQuantity", f"{credit} deal_date")
    assert by_deal == ": 30 common days (2026-09-02..2026-10-13); equal on 0"
    # KIS's holdings of the moment are Toss's of the day before.
    assert (
        "  FHKST01010100 UN frgn_hldn_qty = foreignerHolding.holdingQuantity on: 2026-10-14"
        " (Toss's newest day with holdings: 2026-10-14)" in report
    )
    assert (
        "  FHKST01010100 UN frgn_hldn_qty / lstn_stcn x100 = foreignerHolding.holdingRate x100 (within 0.01)"
        " on: 2026-10-14, 2026-10-13, 2026-10-12 and 1767 more" in report
    )


async def test_the_report_carries_no_value_and_no_token(
    kis: FakeKis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with fake_toss(records) as toss:
        report, _, _ = await probed(kis, tmp_path, monkeypatch, **with_toss(toss))
        tokens = {toss.valid, *toss.issued, *toss.revoked}
    assert [s for s in (*SENTINELS, *TOSS_SENTINELS) if s in report] == []
    assert re.search(r"(?<![A-Za-z0-9])\d{7,}", report) is None
    assert [t for t in tokens if t and t in report] == []
    assert CLIENT_ID not in report and CLIENT_SECRET not in report


async def test_a_cached_token_is_used_and_an_expired_one_replaced(tmp_path: Path) -> None:
    state = tmp_path / "state"
    async with fake_toss(records) as toss, aiohttp.ClientSession() as session:
        toss.accept("toss-token-cached")
        cache_token(state, "toss-token-cached", now() + timedelta(hours=3))
        tokens = TossTokens(session, toss.url, CLIENT_ID, CLIENT_SECRET, state, now)
        client = TossClient(session, toss.url, tokens, 0)
        await client.get("/api/v1/stocks/005930/short-selling", {"count": "1"})
        assert toss.issued == [] and tokens.issued == 0
        assert toss.requests[-1][3] == "toss-token-cached"

        # Expired (or within a minute of it): a new one, cached for every process, mode 600.
        cache_token(state, "toss-token-cached", now() + timedelta(seconds=30))
        tokens = TossTokens(session, toss.url, CLIENT_ID, CLIENT_SECRET, state, now)
        client = TossClient(session, toss.url, tokens, 0)
        await client.get("/api/v1/stocks/005930/short-selling", {"count": "1"})
        assert toss.issued == ["toss-token-1"] and toss.requests[-1][3] == "toss-token-1"
        saved = json.loads((state / TOKEN_FILE).read_text())
        assert saved["token"] == "toss-token-1"
        assert datetime.fromisoformat(saved["expires"]) > now() + timedelta(hours=23)
        assert stat.S_IMODE(os.stat(state / TOKEN_FILE).st_mode) == 0o600


async def test_a_revoked_token_is_replaced_once_at_most(tmp_path: Path) -> None:
    state = tmp_path / "state"
    path = "/api/v1/stocks/005930/securities-lending"
    async with fake_toss(records) as toss, aiohttp.ClientSession() as session:
        toss.accept("toss-token-cached")
        cache_token(state, "toss-token-cached", now() + timedelta(hours=3))
        client = TossClient(
            session, toss.url, TossTokens(session, toss.url, CLIENT_ID, CLIENT_SECRET, state, now), 0
        )
        await client.get(path, {"count": "1"})
        toss.revoke()  # another process issued one without sharing the cache
        await client.get(path, {"count": "1"})  # 401 token-revoked: one new token, asked again
        assert toss.issued == ["toss-token-1"]
        toss.revoke()
        with pytest.raises(TossError) as refused:
            await client.get(path, {"count": "1"})
        assert refused.value.code == "token-revoked" and refused.value.status == 401
        assert toss.issued == ["toss-token-1"]  # not a second time


async def test_a_token_another_process_cached_is_taken_without_issuing(tmp_path: Path) -> None:
    state = tmp_path / "state"
    async with fake_toss(records) as toss, aiohttp.ClientSession() as session:
        toss.accept("toss-token-cached")
        cache_token(state, "toss-token-cached", now() + timedelta(hours=3))
        client = TossClient(
            session, toss.url, TossTokens(session, toss.url, CLIENT_ID, CLIENT_SECRET, state, now), 0
        )
        await client.get("/api/v1/stocks/005930/program-trades", {"count": "1"})
        newer = toss.revoke()
        cache_token(state, newer, now() + timedelta(hours=24))  # the collector issued and cached it
        await client.get("/api/v1/stocks/005930/program-trades", {"count": "1"})
        assert toss.issued == [] and toss.requests[-1][3] == newer


async def test_an_address_toss_does_not_allow_is_said_plainly(
    kis: FakeKis, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with fake_toss(records) as toss:
        toss.ip_allowed = False  # the token endpoint refuses it (access_denied)
        report, _, _ = await probed(kis, tmp_path, monkeypatch, **with_toss(toss))
    assert f"  stopped: no Toss access token: {IP_MESSAGE}" in report
    assert (
        IP_MESSAGE
        == "the server's public IP is not in Toss's allowed IPs (WTS 설정 > Open API > 허용 IP 관리)"
    )
    assert "KIS: " in report and "[1] FHPST04830000" in report  # the KIS part ran all the same

    async with fake_toss(records) as toss:
        toss.accept("toss-token-cached")
        cache_token(tmp_path / "state", "toss-token-cached", now() + timedelta(hours=3))
        toss.api_ip_allowed = False  # the token is fine, the endpoints refuse the address
        lines: list[str] = []
        settings = replace(Settings("k", "s", state_dir=tmp_path / "state"), **with_toss(toss))
        async with aiohttp.ClientSession() as session:
            calls, failed = await probe_toss(session, settings, ["005930"], TODAY, {}, lines.append)
        assert len(toss.requests) == 1  # nothing more is asked
    report = "\n".join(lines)
    assert f"    005930: error HTTP 403 {IP_MESSAGE}" in report
    assert f"  stopped: {IP_MESSAGE}" in report
    assert (calls, failed) == (1, 1)


async def test_a_busy_answer_is_waited_out_and_calls_are_paced(tmp_path: Path) -> None:
    state = tmp_path / "state"
    async with fake_toss(records) as toss, aiohttp.ClientSession() as session:
        toss.busy = 1
        client = TossClient(
            session, toss.url, TossTokens(session, toss.url, CLIENT_ID, CLIENT_SECRET, state, now), 0.05
        )
        for _ in range(3):
            await client.get("/api/v1/stocks/005930/short-selling", {"count": "1"})
        times = [t for t, *_ in toss.requests]
    assert len(times) == 4  # the 429, then three answers
    assert all(b - a >= 0.045 for a, b in zip(times, times[1:], strict=False))


async def test_the_toss_part_stops_at_its_budget(tmp_path: Path) -> None:
    async with fake_toss(records) as toss, aiohttp.ClientSession() as session:
        settings = replace(Settings("k", "s", state_dir=tmp_path / "state"), **with_toss(toss))
        lines: list[str] = []
        calls, _ = await probe_toss(session, settings, ["005930"], TODAY, {}, lines.append, timedelta(0))
    assert calls == 0
    assert "    005930: not asked: the Toss part's 0-minute budget is used up" in "\n".join(lines)


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
    assert hits("/api/v1/stocks/005930/investor-trading") == []
    assert hits("TOSS_CLIENT_SECRET=" + "a" * 20) == ["Toss credential value"]
    assert hits("TOSS_CLIENT_SECRET=") == []
