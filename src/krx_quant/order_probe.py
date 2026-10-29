"""``krx-quant probe-kis-orders``: what KIS's mock-trading (모의투자) server answers to the trader's
orders (#403), before the trader reads a field of them (#404; CODING_STANDARDS.md, "External data
is probed before it is used").

During the continuous session (``OPEN_FROM`` to ``OPEN_UNTIL`` of a KRX trading day) it places, on
the trader's own mock account (``KRX_TRADER_KIS_*``, never the collector's key), with
``kis_orders`` (this module holds no TR ID):

1. the settings: the servers, the keys, the account's form; it refuses any server that is not the
   mock one (``REFUSED``) and stops without the keys (``NO_KEYS``) or outside the session
   (``CLOSED``), placing nothing;
2. the token: issued twice, one right after the other (what the second gets);
3. the per-second limit: ``BURST`` reads of the orderable cash sent at once;
4. the quotes the orders are priced from (KRX's: the price, the day's limits, the tick, the best
   ask and bid);
5. the account before: the balance, the orderable cash and the day's orders;
6. the fill notice: its subscription by the HTS ID, kept open to the end (none without it);
7. three orders KIS should refuse, at prices that cannot fill: a price off the tick, more than the
   cash buys, a sell of shares not held;
8. a buy at the day's lower limit, which does not fill, the cancellable orders, its inquiry, its
   cancel and the inquiry after;
9. a marketable limit buy of ``QUANTITY`` share at the best ask, its fill by the inquiry, what is
   left of it cancelled after ``Timing.fill_wait`` (and once more at the new ask if none filled);
10. the account after the buy, by what changed (amounts as a multiple of the fill's amount);
11. the same sell at the best bid, of what was bought;
12. the account after the sell;
13. the fill notices: their frames, decrypted, by kind, matched to the orders, with how long after
    each order's answer they came;
14. a summary: the calls, KIS's per-second refusals, and whether every step was done.

An order it placed that is still open at the end is cancelled. A run a restart cuts may leave a
share on the mock account: the next run shows it in section 5 and leaves it alone.

**What it shows.** Each answer's HTTP status, ``rt_cd``, ``msg_cd`` and ``msg1``, how long it took,
and its fields by their look (``probe_fields``: int, dec, text, empty, zero), never their values,
but for those that say what an order is or how many (``SHOWN``: codes, sides, quantities, times).
Prices are shown against the order's own price in ticks (``PRICES``), amounts by how they changed.
So no key, account, order number, branch, name, phone number or address is printed, and no market
value: its output goes on the public issue (the scheduled job ``probe-kis-orders-403``), masked
again there (``redaction.py``, which also masks the trader's keys, account and HTS ID).

Calls are paced (``Timing.pace``, one a second) but for the burst, and nothing is sent twice but a
read KIS refused for its per-second limit (once more, a pace later).
"""

from __future__ import annotations

import asyncio
import time
import traceback
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from datetime import time as clock_time
from functools import partial
from typing import Any

import aiohttp

from krx_quant import kis
from krx_quant.config import Settings
from krx_quant.feeds.base import KST
from krx_quant.kis_orders import (
    FILL_NOTICE_COLUMNS,
    MOCK,
    Account,
    AccountNo,
    Answer,
    Notices,
    OrderRef,
    Servers,
    decrypt,
    issue_token,
    listen_fill_notices,
    order_ref,
)
from krx_quant.market_calendar import MarketCalendar, calendar_store
from krx_quant.pricing import tick_size
from krx_quant.probe_fields import describe, flags, present
from krx_quant.ratelimit import RateLimiter
from krx_quant.redaction import redact, secrets_of

TITLE = "KIS 모의투자 주문 프로브 (#403)"
SYMBOL = "005930"
QUANTITY = 1
# Exit codes: it ran every step (0); it stopped on an error (1); the servers are not the mock ones;
# the keys are not set (the scheduled job then waits for them); outside the session; a step was
# not done (a refused buy, a fill or a cancel that did not come).
REFUSED, NO_KEYS, CLOSED, INCOMPLETE = 2, 3, 4, 5
# The continuous session it orders in: after the opening's first minutes, and ended before the
# trader's own last exit (#393: every position is closed by 15:15).
OPEN_FROM, OPEN_UNTIL = clock_time(9, 5), clock_time(15, 15)
BURST = 5
BUSY = "EGW00201"  # KIS's per-second refusal (``kis._BUSY``)
PAGES = 3  # pages of a balance or an inquiry read at most
KEYS = ("KRX_TRADER_KIS_APP_KEY", "KRX_TRADER_KIS_APP_SECRET", "KRX_TRADER_KIS_ACCOUNT")

# The fields whose values are shown (by their name, in lower case): what an order is, its side and
# its quantities, codes and times. Every other field is shown by its look only.
SHOWN = frozenset(
    {
        *("rt_cd", "msg_cd", "msg1", "pdno", "prdt_name", "sll_buy_dvsn_cd", "sll_buy_dvsn_cd_name"),
        *("ord_dvsn_cd", "ord_dvsn_name", "excg_id_dvsn_cd", "excg_dvsn_cd", "prdt_type_cd"),
        *("ccld_cndt_name", "cncl_yn", "ord_dt", "ord_tmd", "infm_tmd", "aspr_unit", "aspr_acpt_hour"),
        *("ord_qty", "tot_ccld_qty", "rmn_qty", "rjct_qty", "cnc_cfrm_qty", "tot_ord_qty"),
        *("hldg_qty", "ord_psbl_qty", "thdt_buyqty", "thdt_sll_qty", "bfdy_buy_qty", "bfdy_sll_qty"),
        # the fill notice's
        *("seln_byov_cls", "rctf_cls", "oder_kind", "oder_cond", "stck_shrn_iscd", "cntg_qty"),
        *("stck_cntg_hour", "rfus_yn", "cntg_yn", "acpt_yn", "oder_qty", "ord_exg_gb", "popup_yn"),
        *("crdt_cls", "cntg_isnm40"),
    }
)
# The price fields, shown against the order's own price in ticks.
PRICES = frozenset(
    {
        *("ord_unpr", "avg_prvs", "pchs_avg_pric", "prpr", "stck_prpr", "stck_mxpr", "stck_llam"),
        *("askp1", "bidp1", "cntg_unpr", "oder_prc", "ord_cond_prc", "psbl_qty_calc_unpr"),
    }
)
# The fill notice's fields that tell its kind.
KIND = ("CNTG_YN", "RCTF_CLS", "RFUS_YN", "ACPT_YN", "SELN_BYOV_CLS")

Out = Callable[[str], None]


@dataclass(frozen=True)
class Timing:
    """How long the probe waits, in seconds (shorter in tests)."""

    pace: float = 1.0  # between calls: one a second, under the mock server's limit
    burst_rest: float = 2.0  # after the burst, before the next call
    settle: float = 2.0  # after an order before it is asked of the inquiry
    fill_wait: float = 10.0  # a marketable order's time to fill, then what is left is cancelled
    poll: float = 1.0  # between inquiries while waiting for a fill
    subscribe_wait: float = 10.0  # the fill notice's subscription is waited for this long
    notice_wait: float = 10.0  # the orders' fill notices are waited for this long at the end


DEFAULT_TIMING = Timing()


@dataclass(frozen=True)
class Quote:
    price: int
    ask: int
    bid: int
    upper: int | None
    lower: int | None
    tick: int


@dataclass
class Placed:
    """An order the probe placed, or a cancel, and what became of it."""

    label: str
    ref: OrderRef | None  # None: KIS refused it, or its answer named no order
    price: int
    quantity: int
    answered: float  # time.monotonic() of its answer
    cancel: bool = False  # a cancel, not an order
    done: bool = False  # filled whole, or cancelled
    filled: int = 0


@dataclass
class Snapshot:
    """The account at one moment: the symbol's holding, the totals, the orderable cash."""

    holding: dict[str, Any] = field(default_factory=dict)
    totals: dict[str, Any] = field(default_factory=dict)
    orderable: dict[str, Any] = field(default_factory=dict)

    @property
    def held(self) -> int:
        return _int(self.holding.get("hldg_qty")) or 0


async def probe_kis_orders(
    settings: Settings,
    now: datetime,
    *,
    calendar: MarketCalendar | None = None,
    servers: Servers = MOCK,
    timing: Timing = DEFAULT_TIMING,
    out: Out = print,
) -> int:
    """Run the probe at ``now`` and print its report; its exit code."""
    secrets = secrets_of(settings)

    def say(line: str) -> None:
        out(redact(line, secrets))

    local = now.astimezone(KST)
    say(f"{TITLE}: {local:%Y-%m-%d %H:%M:%S} KST, {SYMBOL} {QUANTITY}주")
    say("")
    say("1. 설정")
    if not Servers(settings.trader_kis_rest_url, settings.trader_kis_ws_url).same_as(servers):
        say(
            "  - KRX_TRADER_KIS_REST_URL·KRX_TRADER_KIS_WS_URL이 KIS 모의투자 서버가 아니어서 시작하지 "
            "않았다. 주문은 하나도 보내지 않았다. 이 프로브는 모의투자 서버에만 주문한다. 서버 .env에서 "
            "두 값을 지우면 모의투자 서버다."
        )
        return REFUSED
    say("  - 서버: KIS 모의투자(REST, WebSocket)")
    missing = [name for name, value in zip(KEYS, _keys(settings), strict=True) if not value]
    if missing:
        say(
            f"  - 모의 키가 없어 주문하지 않았다: {', '.join(missing)}이(가) 서버 .env에 없다. 사용자가 "
            "KIS 모의투자를 신청하고 모의 앱키를 발급받아 서버 .env에 넣으면(docs/operations.md 1장 "
            '"KIS 모의투자 키") 다음 거래일 장중에 다시 돈다.'
        )
        return NO_KEYS
    try:
        account_no = AccountNo.parse(settings.trader_kis_account or "")
    except ValueError:
        say(
            "  - KRX_TRADER_KIS_ACCOUNT의 형식이 맞지 않아 주문하지 않았다: 계좌번호 8자리와 상품코드 "
            "2자리다(예: 12345678-01). 서버 .env를 고치면 다음 거래일 장중에 다시 돈다."
        )
        return NO_KEYS
    say("  - 앱키, 앱시크릿, 계좌: 있음(계좌는 8자리와 상품코드 2자리)")
    hts_id = settings.trader_kis_hts_id
    say(
        "  - HTS ID: 있음, 체결 통보를 받는다"
        if hts_id
        else "  - HTS ID(KRX_TRADER_KIS_HTS_ID): 없음, 체결 통보(6·13절)는 건너뛴다"
    )
    calendar = calendar or calendar_store(settings).calendar
    if not calendar.is_trading_day(local.date()) or not OPEN_FROM <= local.time() < OPEN_UNTIL:
        say(
            f"  - 장중(거래일 {OPEN_FROM:%H:%M}–{OPEN_UNTIL:%H:%M})이 아니어서 주문하지 않았다. 모의투자 "
            "서버는 장중에만 체결한다."
        )
        return CLOSED
    async with kis.rest_session() as session:
        probe = _Probe(session, settings, servers, account_no, hts_id, local.date(), timing, say)
        try:
            return await probe.run()
        finally:
            await probe.close()


def _keys(settings: Settings) -> tuple[str | None, ...]:
    return settings.trader_kis_app_key, settings.trader_kis_app_secret, settings.trader_kis_account


class _Probe:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        settings: Settings,
        servers: Servers,
        account_no: AccountNo,
        hts_id: str | None,
        day: date,
        timing: Timing,
        say: Out,
    ) -> None:
        self.session = session
        self.servers = servers
        self.key = settings.trader_kis_app_key or ""
        self.secret = settings.trader_kis_app_secret or ""
        self.account_no = account_no
        self.hts_id = hts_id
        self.day = day
        self.timing = timing
        self.say = say
        self.began = time.monotonic()
        self.token = ""
        self.account = self._account(RateLimiter(1, timing.pace, margin=0.1 if timing.pace else 0))
        self.burst = self._account(RateLimiter(BURST, 1.0, margin=0))
        self.placed: list[Placed] = []
        self.notices = Notices()
        self.listening: asyncio.Task[None] | None = None
        self.done: dict[str, bool] = {}

    def _account(self, limiter: RateLimiter) -> Account:
        async def token() -> str:
            return self.token

        return Account(
            self.session,
            self.servers.rest,
            self.key,
            self.secret,
            self.account_no,
            token,
            mock=True,
            limiter=limiter,
        )

    async def close(self) -> None:
        if self.listening is not None:
            self.listening.cancel()
            await asyncio.gather(self.listening, return_exceptions=True)

    async def run(self) -> int:
        """Every step in turn; on an error, the orders still open are cancelled before it stops."""
        try:
            return await self.steps()
        except Exception as e:
            self.say(f"오류로 멈췄다: {_short(e)}")
            traceback.print_exc()
            try:
                await self.tidy()
            except Exception as again:
                self.say(f"열린 주문을 취소하지 못했다: {_short(again)}")
            self.summary()
            return 1

    async def steps(self) -> int:
        say = self.say
        say("")
        if not await self.tokens():
            self.summary()
            return INCOMPLETE
        await self.limit()
        quote = await self.quotes()
        before = await self.snapshot("5. 시작 전 계좌", quote)
        await self.subscribe()
        if quote is None:
            say("주문하지 않았다: 시세(4절)에서 매도·매수 1호가를 읽지 못했다.")
            self.summary()
            return INCOMPLETE
        await self.refusals(quote, before)
        self.done["far"] = await self.far(quote)
        bought = await self.marketable("9. 즉시 체결 지정가 매수", buy=True, quantity=QUANTITY)
        self.done["buy"] = bought.filled >= QUANTITY
        unit = bought.filled * bought.price
        after_buy = await self.snapshot("10. 매수 뒤 계좌", quote, before, unit)
        if bought.filled:
            sold = await self.marketable("11. 즉시 체결 지정가 매도", buy=False, quantity=bought.filled)
            self.done["sell"] = sold.filled >= bought.filled
            await self.snapshot("12. 매도 뒤 계좌", quote, after_buy, sold.filled * sold.price)
        else:
            say("11. 즉시 체결 지정가 매도")
            say("  - 산 것이 없어 팔지 않았다.")
            say("")
            self.done["sell"] = False
        await self.tidy()
        await self.notices_seen()
        self.summary()
        return 0 if all(self.done.values()) else INCOMPLETE

    # 2. The token.

    async def tokens(self) -> bool:
        say = self.say
        say("2. 토큰: 접근토큰을 두 번, 곧바로 이어서 발급")
        first = await issue_token(self.session, self.servers.rest, self.key, self.secret)
        second = await issue_token(self.session, self.servers.rest, self.key, self.secret)
        tokens = [_token_of(a) for a in (first, second)]
        for label, answer, token in (("첫 발급", first, tokens[0]), ("곧바로 다시", second, tokens[1])):
            line = f"  - {label}: {_token_said(answer)}"
            if token and label == "곧바로 다시" and tokens[0]:
                line += ", 첫 토큰과 " + ("같다" if token == tokens[0] else "다르다")
            say(line)
        self.token = tokens[0] or tokens[1] or ""
        self.done["token"] = bool(self.token)
        if not self.token:
            say("  - 토큰이 없어 여기서 멈춘다.")
        say("")
        return bool(self.token)

    # 3. The per-second limit.

    async def limit(self) -> None:
        say = self.say
        say(f"3. 초당 한도: 매수가능조회 {BURST}건을 한꺼번에")
        answers = await asyncio.gather(*(self.burst.orderable(SYMBOL, None) for _ in range(BURST)))
        kinds = Counter(_kind(a) for a in answers)
        took = sorted(a.took for a in answers)
        say(f"  - {', '.join(f'{kind} {n}건' for kind, n in kinds.most_common())}")
        say(f"  - 응답 시간 {took[0]:.2f}–{took[-1]:.2f}초")
        busy = next((a for a in answers if a.code == BUSY), None)
        if busy is not None:
            say(f'  - 한도를 넘은 응답: HTTP {busy.status} · "{busy.message}"')
        say("")
        await asyncio.sleep(self.timing.burst_rest)

    # 4. The quotes.

    async def quotes(self) -> Quote | None:
        say = self.say
        say("4. 시세(KRX)")
        price = await self.read(lambda: self.account.price(SYMBOL))
        say(f"  - 현재가: {said(price)}")
        say(f"    필드: {fields_of(price.rows())}")
        book = await self.read(lambda: self.account.book(SYMBOL))
        say(f"  - 호가: {said(book)}")
        say(f"    output1 필드: {fields_of(book.rows('output1'))}")
        p = price.rows()[0] if price.rows() else {}
        b = book.rows("output1")[0] if book.rows("output1") else {}
        now, ask, bid = _int(p.get("stck_prpr")), _int(b.get("askp1")), _int(b.get("bidp1"))
        if not (now and ask and bid):
            say("  - 현재가나 매도·매수 1호가가 없다.")
            say("")
            return None
        unit = _int(p.get("aspr_unit"))
        tick = unit or tick_size(now)
        quote = Quote(now, ask, bid, _int(p.get("stck_mxpr")), _int(p.get("stck_llam")), tick)
        say(
            f"  - 호가 단위 aspr_unit {unit if unit else '없음'}"
            + ("" if not unit else f"(pricing.tick_size와 {'같다' if unit == tick_size(now) else '다르다'})")
        )
        say(
            f"  - 매도 1호가 = 현재가 {_ticks(ask - now, tick)}, "
            f"매수 1호가 = 현재가 {_ticks(bid - now, tick)}; "
            f"상한가 {'있음' if quote.upper else '없음'}, 하한가 {'있음' if quote.lower else '없음'}"
        )
        say("")
        return quote

    # 5, 10, 12. The account.

    async def snapshot(
        self, title: str, quote: Quote | None, before: Snapshot | None = None, unit: int = 0
    ) -> Snapshot:
        say = self.say
        say(title)
        holdings: list[dict[str, Any]] = []
        totals: list[dict[str, Any]] = []
        context = ("", "")
        first: Answer | None = None
        for page in range(PAGES):
            answer = await self.read(partial(self.account.balance, context))
            first = first or answer
            holdings += answer.rows("output1")
            totals += answer.rows("output2")
            if not answer.more or page == PAGES - 1:
                say(f"  - 잔고: {said(answer)}, {page + 1}쪽" + (" (더 있음)" if answer.more else ""))
                break
            context = _context(answer)
        mine = [r for r in holdings if str(r.get("pdno", "")).strip() == SYMBOL]
        say(f"    보유 {len(holdings)}행, {SYMBOL} {'있음' if mine else '없음'}")
        if before is None:
            say(f"    output1 필드: {fields_of(holdings)}")
            say(f"    output2 필드: {fields_of(totals)} ({len(totals)}행)")
        snap = Snapshot(mine[0] if mine else {}, totals[0] if totals else {})
        if mine:
            say(f"    {SYMBOL}: {values_of(mine[0], ref=quote.price if quote else None)}")
        at = quote.ask if quote else None
        orderable = await self.read(lambda: self.account.orderable(SYMBOL, at))
        snap.orderable = orderable.rows()[0] if orderable.rows() else {}
        what = "지정가(매도 1호가)" if at else "시장가"
        say(f"  - 매수가능조회({what}): {said(orderable)}")
        if before is None:
            say(f"    필드: {fields_of(orderable.rows())}")
            fills = await self.read(lambda: self.account.fills(self.day))
            say(f"  - 오늘 주문 체결 조회: {said(fills)}, {len(fills.rows('output1'))}행")
            say(f"    output1 필드: {fields_of(fills.rows('output1'))}")
            say(f"    output2 필드: {fields_of(fills.rows('output2'))}")
        else:
            say(f"  - 바뀐 것(체결금액 = 1로): {changes(before, snap, unit)}")
        say("")
        return snap

    # 6. The fill notice.

    async def subscribe(self) -> None:
        say = self.say
        say("6. 체결 통보 구독")
        if not self.hts_id:
            say("  - HTS ID가 없어 건너뛴다.")
            say("")
            return
        try:
            approval = await kis.issue_approval_key(self.session, self.servers.rest, self.key, self.secret)
        except (kis.KisError, aiohttp.ClientError, OSError, TimeoutError) as e:
            say(f"  - 승인 키를 받지 못했다: {_short(e)}")
            say("")
            return
        say("  - 승인 키: 받음")
        self.listening = asyncio.create_task(
            listen_fill_notices(self.session, self.servers.ws, approval, self.hts_id, self.notices, mock=True)
        )
        try:
            await asyncio.wait_for(self.notices.subscribed.wait(), self.timing.subscribe_wait)
        except TimeoutError:
            pass
        answer = self.notices.answer
        if answer is None:
            say(
                f"  - 구독 응답이 {self.timing.subscribe_wait:g}초 안에 오지 않았다"
                + (f": {self.notices.error}" if self.notices.error else "")
            )
        else:
            body = answer.get("body") or {}
            key_iv = self.notices.key_iv
            say(
                f"  - 구독 응답: rt_cd {body.get('rt_cd')} · msg_cd {body.get('msg_cd')} · "
                f'"{str(body.get("msg1", "")).strip()}" · '
                + (f"key {len(key_iv[0])}자, iv {len(key_iv[1])}자" if key_iv else "key·iv 없음")
            )
        say("")

    # 7. Orders KIS should refuse.

    async def refusals(self, quote: Quote, before: Snapshot) -> None:
        say = self.say
        say("7. 거부되어야 할 주문(체결될 수 없는 가격)")
        low = quote.lower or far_below(quote.bid, quote.tick)
        high = quote.upper or quote.ask + 20 * quote.tick
        await self.order(f"a. 호가 단위가 아닌 가격(하한가 + 1원) 매수 {QUANTITY}주", True, QUANTITY, low + 1)
        orderable = await self.read(lambda: self.account.orderable(SYMBOL, low))
        most = _int(orderable.rows()[0].get("nrcvb_buy_qty")) if orderable.rows() else None
        if most is None:
            say(f"  - b. 건너뜀: 하한가의 매수가능조회에 nrcvb_buy_qty가 없다({said(orderable)})")
        else:
            await self.order(
                "b. 미수 없는 매수 수량(nrcvb_buy_qty) + 1주를 하한가에 매수", True, most + 1, low
            )
        if before.held:
            say(f"  - c. 건너뜀: {SYMBOL}을 이미 {before.held}주 들고 있다")
        else:
            await self.order(f"c. 들고 있지 않은 주식(상한가) 매도 {QUANTITY}주", False, QUANTITY, high)
        say("")

    async def order(self, label: str, buy: bool, quantity: int, price: int) -> Placed:
        """Place an order that should be refused; one accepted is cancelled at once."""
        answer = await (self.account.buy if buy else self.account.sell)(SYMBOL, quantity, price)
        placed = self._placed(label, answer, price, quantity)
        self.say(f"  - {label}: {said(answer)}")
        if answer.ok:
            self.say(f"    받아들였다(output 필드: {fields_of(answer.rows())}): 바로 취소한다")
            await self.cancel(placed)
        return placed

    # 8. A buy that does not fill, and its cancel.

    async def far(self, quote: Quote) -> bool:
        say = self.say
        price = quote.lower or far_below(quote.bid, quote.tick)
        say(
            f"8. 체결되지 않을 지정가 매수 {QUANTITY}주("
            + ("하한가" if quote.lower else "매수 1호가 −20틱")
            + ")와 취소"
        )
        answer = await self.account.buy(SYMBOL, QUANTITY, price)
        placed = self._placed("하한가 매수", answer, price, QUANTITY)
        say(f"  - 주문: {said(answer)}")
        say(f"    output 필드: {fields_of(answer.rows())}")
        if answer.rows():
            say(f"    {values_of(answer.rows()[0], ref=price)}")
        if placed.ref is None:
            say("  - 주문번호(ODNO)와 KRX 전송 주문조직번호(KRX_FWDG_ORD_ORGNO)가 없어 취소하지 못한다.")
            say("")
            return False
        await asyncio.sleep(self.timing.settle)
        cancelable = await self.read(self.account.cancelable)
        say(f"  - 정정취소가능주문조회: {said(cancelable)}, {len(cancelable.rows())}행")
        if cancelable.rows():
            say(f"    필드: {fields_of(cancelable.rows())}")
        rows = await self.rows_of(placed.ref)
        say(f"  - 그 주문의 조회: {len(rows)}행")
        for row in rows:
            say(f"    {values_of(row, ref=price)}")
        if rows:
            say(f"    필드: {fields_of(rows)}")
        cancel = await self.cancel(placed)
        if cancel is None or cancel.ref is None:
            say("")
            return False
        await asyncio.sleep(self.timing.settle)
        day = await self.read(lambda: self.account.fills(self.day, SYMBOL))
        related = [
            r
            for r in day.rows("output1")
            if _same_no(r.get("odno"), placed.ref.number) or _same_no(r.get("orgn_odno"), placed.ref.number)
        ]
        say(f"  - 취소 뒤 오늘 {SYMBOL} 조회: {said(day)}, 그 주문과 취소의 행 {len(related)}")
        for row in related:
            role = (
                "원주문" if _same_no(row.get("odno"), placed.ref.number) else "취소 주문(orgn_odno = 원주문)"
            )
            say(f"    {role}: {values_of(row, ref=price)}")
        say("")
        return placed.done

    async def cancel(self, placed: Placed) -> Placed | None:
        """Cancel what is left of ``placed``; the cancel, None if there was nothing to name."""
        if placed.ref is None:
            return None
        answer = await self.account.cancel(placed.ref)
        cancel = self._placed(f"{placed.label}의 취소", answer, placed.price, placed.quantity, cancel=True)
        self.say(f"  - 취소: {said(answer)}")
        self.say(f"    output 필드: {fields_of(answer.rows())}")
        if answer.ok:
            placed.done = True
            cancel.done = True
        return cancel

    # 9, 11. A marketable buy, a marketable sell.

    async def marketable(self, title: str, *, buy: bool, quantity: int) -> Placed:
        say = self.say
        side = "매도 1호가" if buy else "매수 1호가"
        say(f"{title} {quantity}주({side})")
        placed = Placed(title, None, 0, quantity, time.monotonic())
        for attempt in ("주문", "다시(새 호가로)"):
            book = await self.read(lambda: self.account.book(SYMBOL))
            row = book.rows("output1")[0] if book.rows("output1") else {}
            price = _int(row.get("askp1" if buy else "bidp1"))
            if not price:
                say(f"  - {attempt}: {side}를 읽지 못해 주문하지 않았다({said(book)})")
                break
            answer = await (self.account.buy if buy else self.account.sell)(SYMBOL, quantity, price)
            placed = self._placed(title, answer, price, quantity)
            say(f"  - {attempt}: {said(answer)}")
            if attempt == "주문":
                say(f"    output 필드: {fields_of(answer.rows())}")
            if placed.ref is None:
                break
            await self.wait_fill(placed)
            if placed.filled >= quantity:
                break
            cancel = await self.cancel(placed)
            # What filled meanwhile: a fill between the last inquiry and the cancel counts, and
            # another order goes only if the cancel took and nothing filled (never two shares).
            rows = await self.rows_of(placed.ref)
            placed.filled = max([placed.filled, *(_int(r.get("tot_ccld_qty")) or 0 for r in rows)])
            if placed.filled or cancel is None or not cancel.done:
                if placed.filled:
                    say(f"  - 취소 뒤 조회: {placed.filled}주 체결")
                break
        say("")
        return placed

    async def wait_fill(self, placed: Placed) -> None:
        """Ask the inquiry until ``placed`` is filled whole, at most ``Timing.fill_wait``."""
        assert placed.ref is not None
        began = time.monotonic()
        asked = 0
        rows: list[dict[str, Any]] = []
        while True:
            await asyncio.sleep(self.timing.poll)
            rows = await self.rows_of(placed.ref)
            asked += 1
            placed.filled = max((_int(r.get("tot_ccld_qty")) or 0 for r in rows), default=0)
            if placed.filled >= placed.quantity or time.monotonic() - began >= self.timing.fill_wait:
                break
        after = time.monotonic() - placed.answered
        if placed.filled >= placed.quantity:
            placed.done = True
            line = f"  - 체결: {placed.filled}주, 조회 {asked}번째(주문 응답 뒤 {after:.1f}초 안)"
        else:
            line = (
                f"  - {self.timing.fill_wait:g}초 안에 {placed.filled}/{placed.quantity}주 체결"
                f"(조회 {asked}번)"
            )
        self.say(line)
        if placed.filled and placed.quantity > 1:
            self.say("    일부 체결은 tot_ccld_qty < ord_qty, rmn_qty > 0으로 보인다(가설)")
        elif placed.quantity == 1:
            self.say("    1주라 일부 체결은 생기지 않는다")
        for row in rows:
            self.say(f"    {values_of(row, ref=placed.price)}")
        price = next((_int(r.get("avg_prvs")) for r in rows if _int(r.get("avg_prvs"))), None)
        if placed.filled and price:
            placed.price = price

    # The end.

    async def tidy(self) -> None:
        """Cancel the orders still open (none, when every step went as it should)."""
        left = [p for p in self.placed if p.ref is not None and not p.done and not p.cancel]
        if not left:
            return
        self.say("정리: 아직 열린 주문을 취소한다")
        for placed in left:
            self.say(f"  - {placed.label}")
            await self.cancel(placed)
        self.say("")

    async def notices_seen(self) -> None:
        say = self.say
        say("13. 체결 통보")
        if self.listening is None:
            say("  - 받지 않았다(6절).")
            say("")
            return
        accepted = [(p, p.ref) for p in self.placed if p.ref is not None]
        began = time.monotonic()
        while not self.listening.done():
            # Cleared before the frames are read: a frame after the read sets it again.
            self.notices.changed.clear()
            told = [r for _, r in self._records()[0]]
            if all(any(_same_no(r.get("ODER_NO"), ref.number) for r in told) for _, ref in accepted):
                break
            left = self.timing.notice_wait - (time.monotonic() - began)
            if left <= 0:
                break
            try:
                await asyncio.wait_for(self.notices.changed.wait(), left)
            except TimeoutError:
                break
        timed, odd = self._records()
        records = [r for _, r in timed]
        frames = self.notices.frames
        encrypted = sum(1 for _, text in frames if text.startswith("1"))
        pings = sum(1 for _, text in self.notices.controls if "PINGPONG" in text)
        say(f"  - 프레임 {len(frames)}개(암호화 {encrypted}), PINGPONG {pings}번")
        if self.notices.error:
            say(f"  - 연결: {self.notices.error}")
        for line in odd:
            say(f"  - {line}")
        if not records:
            say("")
            return
        say(f"  - 필드 {len(FILL_NOTICE_COLUMNS)}개로 나뉜 레코드 {len(records)}개")
        kinds = Counter(tuple(r.get(k, "") for k in KIND) for r in records)
        say(
            f"  - 종류({'·'.join(KIND)}): "
            + ", ".join(f"{'·'.join(k)} {n}개" for k, n in kinds.most_common())
        )
        for placed, ref in accepted:
            mine = [(at, r) for at, r in timed if _same_no(r.get("ODER_NO"), ref.number)]
            if not mine:
                say(f"  - {placed.label}: 통보 없음")
                continue
            parts = [
                f"{'·'.join(r.get(k, '') for k in KIND)} {at - placed.answered:+.1f}초" for at, r in mine
            ]
            say(f"  - {placed.label}: {', '.join(parts)}(주문 응답 뒤)")
            say(f"    {values_of(mine[-1][1], ref=placed.price)}")
        say(f"  - 필드: {fields_of(records)}")
        say("")

    def _records(self) -> tuple[list[tuple[float, dict[str, str]]], list[str]]:
        """The notices' records by field name, decrypted, each with when its frame came; and lines
        on the frames that could not be read so."""
        key_iv = self.notices.key_iv
        records: list[tuple[float, dict[str, str]]] = []
        odd: Counter[str] = Counter()
        for at, text in list(self.notices.frames):
            parts = text.split("|", 3)
            if len(parts) != 4:
                odd["모양이 다른 프레임"] += 1
                continue
            payload = parts[3]
            if parts[0] == "1":
                if key_iv is None:
                    odd["key·iv가 없어 풀지 못한 프레임"] += 1
                    continue
                try:
                    payload = decrypt(key_iv[0], key_iv[1], payload)
                except (ValueError, UnicodeDecodeError):
                    odd["풀리지 않은 프레임"] += 1
                    continue
            values = payload.split("^")
            if len(values) != len(FILL_NOTICE_COLUMNS):
                odd[f"필드가 {len(values)}개인 프레임"] += 1
                continue
            records.append((at, dict(zip(FILL_NOTICE_COLUMNS, values, strict=True))))
        return records, [f"{what} {n}개" for what, n in odd.items()]

    def summary(self) -> None:
        say = self.say
        answers = [*self.account.answers, *self.burst.answers]
        kinds = Counter(_kind(a) for a in answers)
        say("14. 요약")
        shown = ", ".join(f"{k} {n}" for k, n in kinds.most_common())
        say(f"  - REST 호출 {len(answers)}번(토큰 제외): {shown}")
        say(f"  - 초당 한도({BUSY}) {sum(1 for a in answers if a.code == BUSY)}번")
        names = {"token": "토큰", "far": "하한가 매수와 취소", "buy": "매수 체결", "sell": "매도 체결"}
        say("  - " + ", ".join(f"{names[k]} {'됨' if v else '안 됨'}" for k, v in self.done.items()))
        say(f"  - 걸린 시간 {time.monotonic() - self.began:.0f}초")

    # Helpers.

    def _placed(
        self, label: str, answer: Answer, price: int, quantity: int, *, cancel: bool = False
    ) -> Placed:
        ref = order_ref(answer) if answer.ok else None
        placed = Placed(label, ref, price, quantity, time.monotonic(), cancel=cancel)
        self.placed.append(placed)
        return placed

    async def read(self, call: Callable[[], Awaitable[Answer]]) -> Answer:
        """A read, sent once more a pace later if KIS refused it for its per-second limit."""
        answer = await call()
        if answer.code == BUSY:
            await asyncio.sleep(self.timing.pace)
            answer = await call()
        return answer

    async def rows_of(self, ref: OrderRef) -> list[dict[str, Any]]:
        """The inquiry's rows of the order ``ref`` (KIS's own filter by order number is not
        relied on)."""
        answer = await self.read(lambda: self.account.fills(self.day, SYMBOL, ref.number))
        return [r for r in answer.rows("output1") if _same_no(r.get("odno"), ref.number)]


def said(answer: Answer) -> str:
    """An answer's status line: HTTP status, rt_cd, msg_cd, msg1 and time; or why there is none."""
    if answer.status is None:
        return f"응답 없음({answer.error}) · {answer.took:.2f}초"
    if not isinstance(answer.body, dict):
        return f"HTTP {answer.status} · 본문이 JSON이 아니다({answer.error}) · {answer.took:.2f}초"
    tr_cont = answer.headers.get("tr_cont", "")
    return (
        f"HTTP {answer.status} · rt_cd {answer.body.get('rt_cd', '없음')} · msg_cd {answer.code or '없음'} · "
        f'"{answer.message}" · {answer.took:.2f}초' + (f" · tr_cont {tr_cont}" if tr_cont else "")
    )


def fields_of(rows: Sequence[Mapping[str, Any]]) -> str:
    """The rows' fields by their look, never their values (``probe_fields.describe``)."""
    return describe(flags(rows)) if rows else "없음"


def values_of(row: Mapping[str, Any], *, ref: int | None) -> str:
    """The row's ``SHOWN`` fields with their values, and its ``PRICES`` against ``ref`` in ticks."""
    parts = []
    for name, value in row.items():
        low = str(name).lower()
        text = str(value).strip()
        if low in SHOWN:
            parts.append(f"{name} {text if text else '(빈칸)'}")
        elif low in PRICES:
            number = _int(text)
            if not present(value) or number is None:
                parts.append(f"{name} (빈칸)" if not present(value) else f"{name} (숫자 아님)")
            elif number == 0:
                parts.append(f"{name} 0")
            elif ref:
                parts.append(f"{name} 주문가 {_ticks(number - ref, tick_size(ref))}")
            else:
                parts.append(f"{name} (값)")
    return " · ".join(parts) if parts else "보일 값 없음"


def changes(before: Snapshot, after: Snapshot, unit: int) -> str:
    """The numbers that changed between two snapshots (the symbol's holding, the totals, the
    orderable cash): quantities (a name ending in ``qty``) as they are, prices as changed, amounts
    as a multiple of ``unit`` (the fill's amount); and how many did not. Codes are left out."""
    parts = []
    same = 0
    for part, old, new in (
        ("보유", before.holding, after.holding),
        ("합계", before.totals, after.totals),
        ("매수가능", before.orderable, after.orderable),
    ):
        for name in dict.fromkeys([*old, *new]):
            a, b = _number(old.get(name)), _number(new.get(name))
            if a is None and b is None:
                continue
            diff = (b or 0.0) - (a or 0.0)
            low = str(name).lower()
            if low in SHOWN and not low.endswith("qty"):
                continue  # a code (pdno), not an amount
            if diff == 0:
                same += 1
            elif low.endswith("qty"):
                parts.append(f"{part}.{name} {diff:+g}")
            elif low in PRICES:
                parts.append(f"{part}.{name} 바뀜")
            elif unit:
                parts.append(f"{part}.{name} {diff / unit:+.5f}")
            else:
                parts.append(f"{part}.{name} 바뀜")
    return (", ".join(parts) or "없음") + f"; 그대로 {same}개"


def far_below(bid: int, tick: int) -> int:
    """A price 20 ticks under ``bid``, on the tick."""
    price = bid - 20 * tick
    return price - price % tick_size(price)


def _ticks(diff: int, tick: int) -> str:
    if diff == 0:
        return "+0틱"
    count = diff / tick
    return f"{count:+g}틱"


def _int(value: Any) -> int | None:
    try:
        return int(float(str(value).strip()))
    except ValueError:
        return None


def _number(value: Any) -> float | None:
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def _same_no(a: Any, b: Any) -> bool:
    """Two order numbers alike, their leading zeros aside; never when one is empty."""
    x, y = str(a or "").strip().lstrip("0"), str(b or "").strip().lstrip("0")
    return bool(x) and x == y


def _context(answer: Answer) -> tuple[str, str]:
    body = answer.body if isinstance(answer.body, dict) else {}
    low = {str(k).lower(): str(v) for k, v in body.items()}
    return low.get("ctx_area_fk100", ""), low.get("ctx_area_nk100", "")


def _kind(answer: Answer) -> str:
    if answer.status is None:
        return "응답 없음"
    return f"HTTP {answer.status} {answer.code or '-'}"


def _token_of(answer: Answer) -> str | None:
    body = answer.body if isinstance(answer.body, dict) else {}
    token = body.get("access_token")
    return str(token) if answer.status == 200 and token else None


def _token_said(answer: Answer) -> str:
    if answer.status is None:
        return f"응답 없음({answer.error})"
    body = answer.body if isinstance(answer.body, dict) else {}
    if answer.status == 200 and body.get("access_token"):
        expires = str(body.get("access_token_token_expired", ""))
        try:
            until = datetime.strptime(expires, "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
            hours = (until - datetime.now(KST)).total_seconds() / 3600
            when = f'만료 형식 "YYYY-MM-DD HH:MM:SS", 지금부터 {hours:.1f}시간'
        except ValueError:
            when = f"만료 {'형식이 다르다' if expires else '없음'}"
        return (
            f"HTTP 200 · token_type {body.get('token_type', '없음')} · "
            f"expires_in {body.get('expires_in', '없음')} · {when} · {answer.took:.2f}초"
        )
    code = body.get("error_code") or body.get("msg_cd") or "없음"
    message = str(body.get("error_description") or body.get("msg1") or "").strip()
    return f'HTTP {answer.status} · {code} · "{message}" · {answer.took:.2f}초'


def _short(error: BaseException) -> str:
    return f"{type(error).__name__}: {str(error)[:200]}"
