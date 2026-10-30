"""The order probe (#403): what it places on KIS's mock server, what it refuses, and what its
report shows and keeps out.

KIS is the fake one (``fake_kis.FakeKis`` with a ``FakeAccount``), answering with the codes and
messages the probe measured on the mock server (#403) where it has them; these tests check what the
probe does with an answer, not what KIS answers. Every value here is made up: the keys, the
account, the HTS ID, the prices.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

import pytest

from krx_quant import fake_kis, kis_orders, order_probe
from krx_quant.config import DEFAULT_REST_URL, DEFAULT_WS_URL, KIS_MOCK_REST_URL, KIS_MOCK_WS_URL, Settings
from krx_quant.fake_kis import BUY_SIDE, SELL_SIDE, FakeAccount, FakeKis, encrypt
from krx_quant.feeds.base import KST
from krx_quant.market_calendar import MarketCalendar
from krx_quant.order_probe import (
    CLOSED,
    INCOMPLETE,
    NO_KEYS,
    REFUSED,
    SYMBOL,
    Timing,
    probe_kis_orders,
)
from krx_quant.redaction import secrets_of

FRIDAY, SATURDAY = date(2026, 10, 30), date(2026, 10, 31)
CALENDAR = MarketCalendar({FRIDAY: True, SATURDAY: False})
AT_TEN = datetime.combine(FRIDAY, time(10, 0), KST)
TRADER_KEY = "PStrader" + "k" * 28
TRADER_SECRET = "Sx" + "s" * 60
ACCOUNT = "98765432-01"
HTS_ID = "probehts7"
# Short waits: the fake answers at once. A wait a test relies on is a bound, not a pause.
FAST = Timing(pace=0, burst_rest=0, settle=0, fill_wait=1.0, poll=0.01, subscribe_wait=5, notice_wait=5)


@pytest.fixture
async def trader() -> AsyncIterator[tuple[FakeKis, FakeAccount]]:
    fake = FakeKis(app_key=TRADER_KEY, app_secret=TRADER_SECRET)
    await fake.start()
    account = FakeAccount(fake, FRIDAY, hts_id=HTS_ID, account=ACCOUNT.replace("-", "")).install()
    yield fake, account
    await fake.stop()


def trader_settings(fake: FakeKis, tmp_path: Path, **changes: Any) -> Settings:
    settings = Settings(
        kis_app_key="",
        kis_app_secret="",
        state_dir=tmp_path,
        trader_kis_app_key=TRADER_KEY,
        trader_kis_app_secret=TRADER_SECRET,
        trader_kis_account=ACCOUNT,
        trader_kis_hts_id=HTS_ID,
        trader_kis_rest_url=fake.rest_url,
        trader_kis_ws_url=fake.ws_url,
    )
    return replace(settings, **changes)


def servers_of(fake: FakeKis) -> kis_orders.Servers:
    return kis_orders.Servers(fake.rest_url, fake.ws_url)


async def probe(
    settings: Settings,
    fake: FakeKis | None = None,
    *,
    now: datetime = AT_TEN,
    timing: Timing = FAST,
    servers: kis_orders.Servers | None = None,
) -> tuple[int, str]:
    lines: list[str] = []
    chosen = servers or (servers_of(fake) if fake else kis_orders.MOCK)
    code = await probe_kis_orders(
        settings, now, calendar=CALENDAR, servers=chosen, timing=timing, out=lines.append
    )
    return code, "\n".join(lines)


def nothing_sent(fake: FakeKis) -> bool:
    return not (fake.tokens_issued or fake.rest_requests or fake.posts_received or fake.ws_connections)


def posted(fake: FakeKis, tr: kis_orders.Tr) -> list[dict[str, Any]]:
    return [body for _, tr_id, _, body in fake.posts_received if tr_id == tr.mock]


# What it refuses


async def test_it_never_starts_against_the_live_server(
    trader: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, _ = trader
    live = trader_settings(
        fake, tmp_path, trader_kis_rest_url=DEFAULT_REST_URL, trader_kis_ws_url=DEFAULT_WS_URL
    )
    code, text = await probe(live, fake)
    assert code == REFUSED
    assert "KIS 모의투자 서버가 아니어서 시작하지 않았다" in text and "주문은 하나도 보내지 않았다" in text
    assert nothing_sent(fake)


async def test_it_starts_against_no_server_but_the_mock_one(
    trader: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    # The fake stands in for the mock server only where a test says so: by default nothing else is.
    fake, _ = trader
    code, text = await probe(trader_settings(fake, tmp_path), servers=kis_orders.MOCK)
    assert code == REFUSED
    assert nothing_sent(fake)
    # Nor the mock REST server beside another WebSocket.
    half = trader_settings(fake, tmp_path, trader_kis_rest_url=KIS_MOCK_REST_URL)
    assert (await probe(half, servers=kis_orders.MOCK))[0] == REFUSED


async def test_the_mock_servers_pass_and_without_the_keys_nothing_is_sent(tmp_path: Path) -> None:
    settings = Settings(kis_app_key="", kis_app_secret="", state_dir=tmp_path)
    assert (settings.trader_kis_rest_url, settings.trader_kis_ws_url) == (KIS_MOCK_REST_URL, KIS_MOCK_WS_URL)
    code, text = await probe(settings, servers=kis_orders.MOCK)
    assert code == NO_KEYS
    assert "  - 서버: KIS 모의투자(REST, WebSocket)" in text
    assert (
        "모의 키가 없어 주문하지 않았다: KRX_TRADER_KIS_APP_KEY, KRX_TRADER_KIS_APP_SECRET, "
        "KRX_TRADER_KIS_ACCOUNT이(가) 서버 .env에 없다" in text
    )
    assert "다음 거래일 장중에 다시 돈다" in text


async def test_without_one_key_it_names_it_and_sends_nothing(
    trader: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, _ = trader
    code, text = await probe(trader_settings(fake, tmp_path, trader_kis_account=None), fake)
    assert code == NO_KEYS
    assert "KRX_TRADER_KIS_ACCOUNT이(가) 서버 .env에 없다" in text and "APP_KEY," not in text
    assert nothing_sent(fake)


async def test_a_malformed_account_is_said_and_nothing_is_sent(
    trader: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, _ = trader
    code, text = await probe(trader_settings(fake, tmp_path, trader_kis_account="1234-5678"), fake)
    assert code == NO_KEYS
    assert "KRX_TRADER_KIS_ACCOUNT의 형식이 맞지 않아 주문하지 않았다" in text
    assert nothing_sent(fake)


@pytest.mark.parametrize(
    "now",
    [
        datetime.combine(FRIDAY, time(9, 0), KST),  # the opening's first minutes
        datetime.combine(FRIDAY, time(15, 15), KST),  # past the trader's last exit
        datetime.combine(FRIDAY, time(22, 30), KST),  # a night's job time
        datetime.combine(SATURDAY, time(10, 0), KST),  # not a trading day
    ],
)
async def test_outside_the_session_it_places_nothing(
    trader: tuple[FakeKis, FakeAccount], tmp_path: Path, now: datetime
) -> None:
    fake, _ = trader
    code, text = await probe(trader_settings(fake, tmp_path), fake, now=now)
    assert code == CLOSED
    assert "장중(거래일 09:05–15:15)이 아니어서 주문하지 않았다" in text
    assert nothing_sent(fake)


# A whole run


async def test_a_run_buys_and_sells_one_share_and_cancels_a_far_order(
    trader: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = trader
    code, text = await probe(trader_settings(fake, tmp_path), fake)
    assert code == 0, text
    # On the mock server's TR IDs only.
    mock_ids = {kis_orders.BUY.mock, kis_orders.SELL.mock, kis_orders.CANCEL.mock}
    assert {tr_id for _, tr_id, _, _ in fake.posts_received} == mock_ids
    assert all(tr_id.startswith("V") for _, tr_id, _, _ in fake.posts_received)
    # Four buys (off the tick, beyond the cash, the far one, the marketable one), two sells (of
    # shares not held, the marketable one) and two cancels: the buy beyond the cash, which the mock
    # accepts (#403), and the far one.
    buys, sells = posted(fake, kis_orders.BUY), posted(fake, kis_orders.SELL)
    assert len(buys) == 4 and len(sells) == 2 and len(posted(fake, kis_orders.CANCEL)) == 2
    assert [b["ORD_QTY"] for b in buys][2:] == ["1", "1"] and sells[-1]["ORD_QTY"] == "1"
    assert all(b["EXCG_ID_DVSN_CD"] == "KRX" and b["ORD_DVSN"] == "00" for b in [*buys, *sells])
    assert int(buys[-1]["ORD_UNPR"]) == account.ask and int(sells[-1]["ORD_UNPR"]) == account.bid
    assert int(buys[2]["ORD_UNPR"]) == account.lower
    # The account ends as it began: no share held, no order open.
    assert account.holdings.get(SYMBOL, 0) == 0
    assert all(o.left == 0 for o in account.orders)
    filled = [o for o in account.orders if o.filled]
    assert [(o.side, o.filled) for o in filled] == [(BUY_SIDE, 1), (SELL_SIDE, 1)]

    for section in range(1, 15):
        assert f"\n{section}. " in f"\n{text}", section
    assert "  - 곧바로 다시: HTTP 200" in text and "첫 토큰과 다르다" in text
    assert "  - HTTP 200 20310000 5건" in text  # the burst
    assert "a. 호가 단위가 아닌 가격(하한가 + 1원) 매수 1주: HTTP 200 · rt_cd 1 · msg_cd 40030000" in text
    assert (
        "b. 미수 없는 매수 수량(nrcvb_buy_qty) + 1주를 하한가에 매수: HTTP 200 · rt_cd 0 · msg_cd 40600000"
        in (text)
    )
    assert "    받아들였다(output 필드: int: KRX_FWDG_ORD_ORGNO, ODNO, ORD_TMD · empty: SOR_ODNO)" in text
    assert "c. 들고 있지 않은 주식(상한가) 매도 1주: HTTP 200 · rt_cd 1 · msg_cd 40240000" in text
    assert "  - 정정취소가능주문조회: HTTP 500 · rt_cd 1 · msg_cd EGW02006" in text
    assert "주문 응답과 견줌: odno는 ODNO와 같다, ord_gno_brno는 KRX_FWDG_ORD_ORGNO와 같다" in text
    assert "원주문: " in text and "취소 주문(orgn_odno = 원주문): " in text and "cncl_yn Y" in text
    assert "cncl_cfrm_qty 1" in text  # the cancel's row, by its name on the mock server
    # Filled half a tick better than the order's price, as on the mock server.
    assert (
        "  - 체결: 1주, 조회 1번째" in text
        and "avg_prvs 주문가 -0.5틱" in text
        and "avg_prvs 주문가 +0.5틱" in text
    )
    assert "보유.hldg_qty +1" in text and "보유.hldg_qty -1" in text
    # The deposit two days on fell by the fill's amount and its commission; the deposit stayed.
    assert "합계.prvs_rcdl_excc_amt -1.000" in text and "합계.dnca_tot_amt" not in text
    assert "  - 구독 응답: rt_cd 0 · msg_cd OPSP0000" in text and "key 32자, iv 16자" in text
    assert "  - 필드 26개로 나뉜 레코드" in text and "통보 없음" not in text
    assert "  - 9. 즉시 체결 지정가 매수: 1·0·0·1·02" in text and "2·0·0·2·02" in text
    assert "토큰 됨, 하한가 매수와 취소 됨, 매수 체결 됨, 매도 체결 됨, 체결 통보 됨" in text


async def test_the_report_shows_no_key_account_order_number_or_personal_field(
    trader: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = trader
    settings = trader_settings(fake, tmp_path)
    code, text = await probe(settings, fake)
    assert code == 0, text
    numbers = [o.number for o in account.orders]
    assert len(numbers) == 6  # two buys and their cancels, the marketable buy and sell
    hidden = [TRADER_KEY, TRADER_SECRET, ACCOUNT, "9876543201", "98765432", HTS_ID, *numbers]
    hidden += ["01012345678", "10.11.12.13", "홍길동", "01790", *fake.tokens_issued]
    for value in hidden:
        assert value not in text, value
    # Nor a price: each against the order's own, in ticks.
    fills = [round(o.average) for o in account.orders if o.filled]
    for price in (account.ask, account.bid, account.lower, account.upper, account.price, *fills):
        assert str(price) not in text, price
    # By its look instead.
    assert "ctac_tlno" in text and "ODNO" in text and "ACNT_NO" in text
    assert all(value in secrets_of(settings) for value in (ACCOUNT, "9876543201", "98765432", HTS_ID))


async def test_without_the_hts_id_it_orders_without_the_fill_notice(
    trader: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, _ = trader
    code, text = await probe(trader_settings(fake, tmp_path, trader_kis_hts_id=None), fake)
    assert code == 0, text
    assert "HTS ID(KRX_TRADER_KIS_HTS_ID): 없음, 체결 통보(6·13절)는 건너뛴다" in text
    assert "6. 체결 통보 구독\n  - HTS ID가 없어 건너뛴다." in text
    assert fake.ws_connections == 0 and not fake.approvals_issued


async def test_with_an_hts_id_a_fill_notice_not_subscribed_leaves_the_run_not_done(
    trader: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    # The orders went through, but the notice the scheduled job is to measure was not: it runs again.
    fake, account = trader
    fake.accept_ws = False
    code, text = await probe(trader_settings(fake, tmp_path), fake)
    assert code == INCOMPLETE
    assert "  - 구독 응답이 5초 안에 오지 않았다: " in text
    assert "매수 체결 됨, 매도 체결 됨, 체결 통보 안 됨" in text
    assert account.holdings.get(SYMBOL, 0) == 0 and all(o.left == 0 for o in account.orders)


async def test_with_an_hts_id_notices_that_cannot_be_decrypted_leave_the_run_not_done(
    trader: tuple[FakeKis, FakeAccount], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake, _ = trader
    monkeypatch.setattr(fake_kis, "encrypt", lambda key, iv, text: encrypt("x" * 32, iv, text))
    code, text = await probe(trader_settings(fake, tmp_path), fake, timing=replace(FAST, notice_wait=0.5))
    assert code == INCOMPLETE
    assert "  - 풀리지 않은 프레임 " in text and "체결 통보 안 됨" in text


async def test_notices_of_another_width_are_shown_by_position_without_a_value(
    trader: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    # The mock server's records had 23 fields where KIS's example names 26 (#403): the run is not
    # done, and each position is shown by its look for a later change to name it.
    fake, account = trader
    account.notice_fields = 23
    code, text = await probe(trader_settings(fake, tmp_path), fake, timing=replace(FAST, notice_wait=0.5))
    assert code == INCOMPLETE
    assert "체결 통보 안 됨" in text
    assert "  - 레코드 1개에 필드가 23개인 프레임 " in text
    assert (
        "  - 23칸 레코드 " in text
        and "의 칸별 모양(FILL_NOTICE_COLUMNS는 26칸이라 이름을 붙이지 못한다" in text
    )
    lines = {line.split(":", 1)[0].strip(): line for line in text.splitlines() if line.startswith("    칸 ")}
    assert len(lines) == 23
    # The fake's first fields are KIS's example's: the HTS ID, the account, the order numbers.
    assert "= HTS ID " in lines["칸 1"] and "text" in lines["칸 1"]
    assert "= 계좌 " in lines["칸 2"] and "10자" in lines["칸 2"]
    assert "= 주문번호 " in lines["칸 3"] and "= 종목코드 " in lines["칸 9"]
    assert "= KRX_FWDG_ORD_ORGNO " in lines["칸 16"]
    numbers = [o.number for o in account.orders]
    for value in [HTS_ID, "9876543201", "01790", "홍길동", *numbers, str(account.ask), str(account.bid)]:
        assert value not in text, value


async def test_an_order_that_does_not_fill_is_cancelled_tried_once_more_and_nothing_is_sold(
    trader: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = trader
    account.fills = False
    code, text = await probe(trader_settings(fake, tmp_path), fake, timing=replace(FAST, fill_wait=0.2))
    assert code == INCOMPLETE
    assert "  - 0.2초 안에 0/1주 체결" in text and "  - 다시(새 호가로): HTTP 200" in text
    assert "  - 산 것이 없어 팔지 않았다." in text
    assert "매수 체결 안 됨, 매도 체결 안 됨" in text
    # Both tries were cancelled, and only the share not held was offered for sale.
    assert len(posted(fake, kis_orders.BUY)) == 5 and len(posted(fake, kis_orders.SELL)) == 1
    assert len(posted(fake, kis_orders.CANCEL)) == 4  # with the far buy's and the one beyond the cash
    assert all(o.left == 0 for o in account.orders) and account.holdings.get(SYMBOL, 0) == 0


async def test_a_fill_that_races_the_cancel_is_counted_and_no_second_share_is_bought(
    trader: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, account = trader
    account.fills, account.fills_late = False, True
    code, text = await probe(trader_settings(fake, tmp_path), fake, timing=replace(FAST, fill_wait=0.2))
    assert code == 0, text
    assert "  - 취소: HTTP 200 · rt_cd 1 · msg_cd FK0004" in text and "  - 취소 뒤 조회: 1주 체결" in text
    assert "다시(새 호가로)" not in text, "the share filled: no second order"
    assert len(posted(fake, kis_orders.BUY)) == 4 and len(posted(fake, kis_orders.SELL)) == 2
    assert account.holdings.get(SYMBOL, 0) == 0 and all(o.left == 0 for o in account.orders)


async def test_an_error_midway_cancels_the_orders_still_open(
    trader: tuple[FakeKis, FakeAccount], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake, account = trader
    account.fills = False  # the marketable buy rests, open, when the error comes

    async def broken(self: object, placed: object) -> None:
        raise RuntimeError("an answer it could not read")

    monkeypatch.setattr(order_probe._Probe, "wait_fill", broken)
    code, text = await probe(trader_settings(fake, tmp_path), fake)
    assert code == 1
    assert "오류로 멈췄다: RuntimeError: an answer it could not read" in text
    assert "정리: 아직 열린 주문을 취소한다" in text and "14. 요약" in text
    assert all(o.left == 0 for o in account.orders), "nothing left open on the account"


async def test_without_the_quotes_it_places_no_order(
    trader: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, _ = trader
    fake.rest_limits[kis_orders.BOOK] = 0  # every book read refused for the per-second limit
    code, text = await probe(trader_settings(fake, tmp_path), fake)
    assert code == INCOMPLETE
    assert "주문하지 않았다: 시세(4절)에서 매도·매수 1호가를 읽지 못했다." in text
    assert fake.posts_received == []
    book_reads = [r for r in fake.rest_requests if r[1] == kis_orders.BOOK]
    assert len(book_reads) == 2, "a read refused for the limit is sent once more, not again"


async def test_the_burst_shows_the_per_second_limit(
    trader: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, _ = trader
    fake.rest_limits[kis_orders.ORDERABLE.mock] = 4
    # A second after the burst, its four leave the fake's window before the paced reads.
    code, text = await probe(trader_settings(fake, tmp_path), fake, timing=replace(FAST, burst_rest=1.05))
    assert code == 0, text
    assert "  - HTTP 200 20310000 4건, HTTP 500 EGW00201 1건" in text
    assert '  - 한도를 넘은 응답: HTTP 500 · "초당 거래건수를 초과하였습니다."' in text
    assert "  - 초당 한도(EGW00201) 1번" in text


async def test_a_second_token_refused_is_shown_and_the_first_is_used(
    trader: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    fake, _ = trader
    fake.token_min_interval = 60.0
    code, text = await probe(trader_settings(fake, tmp_path), fake)
    assert code == 0, text
    assert "  - 곧바로 다시: HTTP 403 · EGW00133" in text
    assert len(fake.tokens_issued) == 1


# The module the orders go through


def test_the_mock_tr_ids_differ_from_the_live_ones() -> None:
    for tr in (kis_orders.BUY, kis_orders.SELL, kis_orders.CANCEL, kis_orders.FILLS, kis_orders.BALANCE):
        assert tr.mock != tr.live and tr.mock.startswith("V") and tr.of(True) == tr.mock
        assert tr.of(False) == tr.live


def test_a_fill_notice_frame_is_decrypted_with_the_subscription_s_key() -> None:
    text = "^".join(kis_orders.FILL_NOTICE_COLUMNS)
    key, iv = "k" * 32, "i" * 16
    assert kis_orders.decrypt(key, iv, encrypt(key, iv, text)) == text


def test_a_fill_notice_frame_may_hold_several_records() -> None:
    key, iv = "k" * 32, "i" * 16
    one = [f"{name.lower()}1" for name in kis_orders.FILL_NOTICE_COLUMNS]
    two = [f"{name.lower()}2" for name in kis_orders.FILL_NOTICE_COLUMNS]
    frame = f"1|{kis_orders.FILL_NOTICE.mock}|002|{encrypt(key, iv, '^'.join([*one, *two]))}"
    records = order_probe.notice_records(frame, (key, iv))
    assert isinstance(records, list) and [r["ODER_NO"] for r in records] == ["oder_no1", "oder_no2"]
    short = f"1|{kis_orders.FILL_NOTICE.mock}|002|{encrypt(key, iv, '^'.join(one))}"
    assert order_probe.notice_records(short, (key, iv)) == "레코드 2개에 필드가 26개인 프레임"
    assert order_probe.notice_records(frame, None) == "key·iv가 없어 풀지 못한 프레임"
    assert order_probe.notice_records(f"0|{kis_orders.FILL_NOTICE.mock}|001|{'^'.join(one)}", None) == [
        dict(zip(kis_orders.FILL_NOTICE_COLUMNS, one, strict=True))
    ]


def test_an_account_is_eight_digits_and_a_product_code() -> None:
    assert kis_orders.AccountNo.parse("12345678-01") == kis_orders.AccountNo("12345678", "01")
    assert kis_orders.AccountNo.parse(" 1234567801 ").fields == {"CANO": "12345678", "ACNT_PRDT_CD": "01"}
    for bad in ("1234567-01", "12345678-1", "abcdefgh-01", ""):
        with pytest.raises(ValueError):
            kis_orders.AccountNo.parse(bad)


def test_servers_are_the_same_by_scheme_host_and_port() -> None:
    assert kis_orders.MOCK.same_as(kis_orders.Servers(KIS_MOCK_REST_URL.upper() + "/", KIS_MOCK_WS_URL))
    assert not kis_orders.MOCK.same_as(kis_orders.Servers(DEFAULT_REST_URL, KIS_MOCK_WS_URL))
    assert not kis_orders.MOCK.same_as(kis_orders.Servers(KIS_MOCK_REST_URL, DEFAULT_WS_URL))


def test_the_trader_s_keys_are_read_from_the_env_and_default_to_the_mock_servers() -> None:
    env = {
        "KIS_APP_KEY": "x",
        "KIS_APP_SECRET": "y",
        "KRX_TRADER_KIS_APP_KEY": TRADER_KEY,
        "KRX_TRADER_KIS_APP_SECRET": TRADER_SECRET,
        "KRX_TRADER_KIS_ACCOUNT": ACCOUNT,
        "KRX_TRADER_KIS_HTS_ID": HTS_ID,
    }
    settings = Settings.from_env(env, dotenv_path=None)
    assert settings.trader_kis
    assert (settings.trader_kis_app_key, settings.trader_kis_account) == (TRADER_KEY, ACCOUNT)
    assert (settings.trader_kis_rest_url, settings.trader_kis_ws_url) == (KIS_MOCK_REST_URL, KIS_MOCK_WS_URL)
    assert TRADER_SECRET not in repr(settings)
    assert not Settings.from_env({**env, "KRX_TRADER_KIS_APP_SECRET": " "}, dotenv_path=None).trader_kis
    live = Settings.from_env({**env, "KRX_TRADER_KIS_REST_URL": DEFAULT_REST_URL}, dotenv_path=None)
    assert live.trader_kis_rest_url == DEFAULT_REST_URL  # read as written: the probe refuses it
