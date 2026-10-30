"""The trader's KIS account calls (#393, #403, #404): cash orders and their cancel, the order and
fill inquiry, the balance and the orderable cash, the quotes an order is priced from, and the
real-time fill notice.

This is the one file of the repository the forbidden-pattern check lets hold KIS order TR IDs and
order endpoints (``forbidden.ALLOWED_IN``, ``scripts/check_forbidden.py``): they are written here
and nowhere else, and every other module reaches them through this one. The collector never
imports it. The order probe (``order_probe.py``) does, in a scheduled job's own process, and so
will the trader's mock mode (#405).

Each TR ID comes in two, the live server's and the mock (모의투자) server's (``Tr``): an
``Account`` is made for one of them (``mock``), with the server it calls.

Two layers:

- ``Account`` sends each call once and returns KIS's answer as it came (``Answer``): nothing is
  read into it, and nothing is sent again. The order probe uses it, to show what KIS answers.
- ``Broker`` (#404) is the trader's: it reads the answers by the fields the probe measured on the
  mock server (the scheduled job ``probe-kis-orders-403``, its comment on #403 of 2026-10-30,
  ``PROBE_COMMENT``; CODING_STANDARDS.md, "External data is probed before it is used"), with the
  trader's own key and cached token (``trader_broker``), paced (``PER_SECOND``), and retried by
  the rules below. Its fills come from the day's order inquiry, polled after each order
  (``Broker.take``), never from the real-time fill notice: on the mock server each notice record
  had 23 fields where KIS's example lists 26 (``FILL_NOTICE_COLUMNS``), so its fields are not
  known yet (``docs/kis-feeds.md``, "주문 (#403)").

What is sent again: a call KIS refused for its per-second limit (``BUSY``) after ``BUSY_PAUSE``,
and one it refused for its token (``TOKEN_REJECTED``) once with a new token. Both are KIS's
gateway refusing the call before it is taken. Nothing else: an order KIS refused (``rt_cd`` 1,
``REFUSALS``) stays refused, and an order sent twice is two orders, so one whose connection failed
or timed out is not sent again (aiohttp sends a POST once: it retries idempotent methods only). The
caller learns what became of it from the inquiry (``Sent.outcome`` ``UNKNOWN``).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from functools import partial
from typing import Any, Protocol

import aiohttp
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from krx_quant import kis
from krx_quant.auth import TokenManager
from krx_quant.config import KIS_MOCK_REST_URL, KIS_MOCK_WS_URL, Settings, SettingsError
from krx_quant.ratelimit import RateLimiter

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Tr:
    """A TR ID on the live server and on the mock one."""

    live: str
    mock: str

    def of(self, mock: bool) -> str:
        return self.mock if mock else self.live


# Cash orders (주식주문(현금)), one path for both sides.
BUY = Tr("TTTC0012U", "VTTC0012U")
SELL = Tr("TTTC0011U", "VTTC0011U")
# Revise or cancel (주식주문(정정취소)); only the cancel is used.
CANCEL = Tr("TTTC0013U", "VTTC0013U")
# The day's orders and fills (주식일별주문체결조회), for the last three months.
FILLS = Tr("TTTC0081R", "VTTC0081R")
# The balance (주식잔고조회) and the orderable cash (매수가능조회).
BALANCE = Tr("TTTC8434R", "VTTC8434R")
ORDERABLE = Tr("TTTC8908R", "VTTC8908R")
# The orders that can still be revised or cancelled (주식정정취소가능주문조회): KIS's example names
# a live TR ID only, so the mock was asked the same one. It is not a mock TR (``NOT_MOCK``, #403):
# the broker finds the open orders on the day's inquiry instead (``Broker.open_orders``).
CANCELABLE = Tr("TTTC0084R", "TTTC0084R")
# The real-time fill notice (국내주식 실시간체결통보), asked by the HTS ID; its frames are encrypted.
FILL_NOTICE = Tr("H0STCNI0", "H0STCNI9")
# The quotes an order is priced from, the same TR ID on both servers.
PRICE = "FHKST01010100"  # 주식현재가 시세: the price, the day's limits and the tick (aspr_unit)
BOOK = "FHKST01010200"  # 주식현재가 호가/예상체결: the best ask and bid (askp1, bidp1)

ORDER_PATH = "/uapi/domestic-stock/v1/trading/order-cash"
CANCEL_PATH = "/uapi/domestic-stock/v1/trading/order-rvsecncl"
FILLS_PATH = "/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
BALANCE_PATH = "/uapi/domestic-stock/v1/trading/inquire-balance"
ORDERABLE_PATH = "/uapi/domestic-stock/v1/trading/inquire-psbl-order"
CANCELABLE_PATH = "/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl"
PRICE_PATH = "/uapi/domestic-stock/v1/quotations/inquire-price"
BOOK_PATH = "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn"

LIMIT, MARKET = "00", "01"  # ORD_DVSN: 지정가, 시장가
KRX = "KRX"  # EXCG_ID_DVSN_CD: the trader trades on KRX alone (#393)
QUOTES_MARKET = "J"  # FID_COND_MRKT_DIV_CODE: KRX's own quotes
CANCEL_CODE = "02"  # RVSE_CNCL_DVSN_CD: 01 revise, 02 cancel
GENERAL_SELL = "01"  # SLL_TYPE: 01 일반매도
SIDE_SELL, SIDE_BUY = "01", "02"  # sll_buy_dvsn_cd of the inquiry's rows (#403: 01 매도, 02 매수)

# The probe's comment the fields read here are from (the mock server, 2026-10-30, #403).
PROBE_COMMENT = "https://github.com/codeyoma/krx-quant/issues/403#issuecomment-6512294559"

# What the mock server answered (msg_cd, ``PROBE_COMMENT``). An accepted order is told by its
# ``rt_cd`` 0 and the order it names (``order_ref``), not by these, which may differ on the live
# server.
BOUGHT = "40600000"  # 모의투자 매수주문이 완료 되었습니다.
SOLD = "40590000"  # 모의투자 매도주문이 완료 되었습니다.
CANCELLED = "40630000"  # 모의투자 취소주문이 완료 되었습니다.
INQUIRED = "20310000"  # 모의투자 조회가 완료되었습니다. (the balance, the orderable cash, the inquiry)
NO_ROWS = "70070000"  # 모의투자 조회할 내역(자료)이 없습니다. (rt_cd 0: the inquiry of a day with none)
QUOTED = "MCA00000"  # 정상처리 되었습니다. (the price, the book)
# KIS's gateway: the call was refused before KIS took it.
BUSY = "EGW00201"  # HTTP 500, 초당 거래건수를 초과하였습니다.: 3 of 5 sent at once were answered
TOKEN_TOO_SOON = "EGW00133"  # HTTP 403 to a token asked within a minute of the last (1분당 1회)
NOT_MOCK = "EGW02006"  # HTTP 500, 모의투자 TR 이 아닙니다.: the cancellable-orders inquiry
# A token KIS no longer takes (expired, or replaced): the collector's client's codes
# (``kis._TOKEN_REJECTED``), not seen on the mock server yet.
TOKEN_REJECTED = frozenset({"EGW00121", "EGW00123"})
# The orders KIS refused (rt_cd 1) and why. A refused order is not sent again: the same order would
# be refused the same way. A code not here is a refusal all the same, its message as KIS gave it.
REFUSALS = {
    "40030000": "the price is off the tick (호가단위 오류)",
    "40240000": "no shares held to sell (잔고내역이 없습니다)",
}
# Not a refusal: the mock server accepted a buy of the shares the cash buys without credit
# (``nrcvb_buy_qty``) and one more, at the day's lower limit (BOUGHT). So the trader sizes a buy
# from the orderable cash itself (``Orderable.most``); KIS does not stop it.

# The pace (``PER_SECOND``): every call of the broker through one limiter, two a second over a
# 1.1 s window. Of five reads sent at once the mock server answered three and refused two
# (``BUSY``); none of the calls the probe paced a second apart was refused. A call refused for the
# limit goes again ``BUSY_PAUSE`` later, at most ``BUSY_RETRIES`` times.
PER_SECOND = 2
BUSY_PAUSE = 1.0
BUSY_RETRIES = 3
# A marketable order (``Broker.take``): its fills are read on the day's inquiry every ``POLL``
# seconds, and what is left after ``TAKE_WAIT`` is cancelled. The mock server's fills of one share
# were on the first inquiry, 1.3 s and 1.8 s after the order's answer.
POLL = 1.0
TAKE_WAIT = 5.0
# The most pages of an inquiry read (``tr_cont`` M or F: another page); a day of more is an error.
PAGES = 20
# The trader's token, cached apart from the collector's (``state/kis_token.json``).
TOKEN_FILE = "trader_kis_token.json"

# The fill notice's fields, in order (KIS's example, ``notice_cols``): 26, the live server's layout
# as the example gives it. On the mock server each record had 23 fields (#403), which the probe
# shows by position until they are named; the broker does not read the notice.
FILL_NOTICE_COLUMNS = (
    "CUST_ID ACNT_NO ODER_NO OODER_NO SELN_BYOV_CLS RCTF_CLS ODER_KIND ODER_COND STCK_SHRN_ISCD "
    "CNTG_QTY CNTG_UNPR STCK_CNTG_HOUR RFUS_YN CNTG_YN ACPT_YN BRNC_NO ODER_QTY ACNT_NAME "
    "ORD_COND_PRC ORD_EXG_GB POPUP_YN FILLER CRDT_CLS CRDT_LOAN_DATE CNTG_ISNM40 ODER_PRC"
).split()


@dataclass(frozen=True)
class Servers:
    """The REST and WebSocket servers an account calls."""

    rest: str
    ws: str

    def same_as(self, other: Servers) -> bool:
        """Both servers the same, a trailing slash or the case of the scheme and host aside."""
        return _url(self.rest) == _url(other.rest) and _url(self.ws) == _url(other.ws)


MOCK = Servers(KIS_MOCK_REST_URL, KIS_MOCK_WS_URL)


def _url(text: str) -> str:
    return text.strip().rstrip("/").lower()


@dataclass(frozen=True)
class AccountNo:
    """An account: its 8 digits (``CANO``) and its 2-digit product code (``ACNT_PRDT_CD``)."""

    number: str
    product: str

    @classmethod
    def parse(cls, text: str) -> AccountNo:
        """``12345678-01`` or ``1234567801``; ValueError for anything else."""
        digits = text.strip().replace("-", "")
        if len(digits) != 10 or not digits.isdigit():
            raise ValueError("an account is 8 digits and a 2-digit product code, e.g. 12345678-01")
        return cls(digits[:8], digits[8:])

    @property
    def fields(self) -> dict[str, str]:
        return {"CANO": self.number, "ACNT_PRDT_CD": self.product}


@dataclass(frozen=True)
class Answer:
    """KIS's answer to one call, as it came: no field is read into it."""

    tr_id: str
    status: int | None  # None: no answer (the connection failed or the call timed out)
    body: Any  # the JSON body; None without an answer or when it is not JSON
    headers: Mapping[str, str] = field(default_factory=dict)  # tr_cont, when KIS sends it
    error: str | None = None  # why there is no answer
    took: float = 0.0  # seconds from sending to the answer

    @property
    def ok(self) -> bool:
        return isinstance(self.body, dict) and self.body.get("rt_cd") == "0"

    @property
    def code(self) -> str:
        return str(self.body.get("msg_cd", "")) if isinstance(self.body, dict) else ""

    @property
    def message(self) -> str:
        return str(self.body.get("msg1", "")).strip() if isinstance(self.body, dict) else ""

    @property
    def more(self) -> bool:
        """KIS has another page (``tr_cont`` M or F)."""
        return self.headers.get("tr_cont", "") in ("M", "F")

    @property
    def context(self) -> tuple[str, str]:
        """What the next page is asked with (``CTX_AREA_FK100``, ``CTX_AREA_NK100``)."""
        body = self.body if isinstance(self.body, dict) else {}
        low = {str(k).lower(): str(v) for k, v in body.items()}
        return low.get("ctx_area_fk100", ""), low.get("ctx_area_nk100", "")

    def part(self, name: str = "output") -> Any:
        return self.body.get(name) if isinstance(self.body, dict) else None

    def rows(self, name: str = "output") -> list[dict[str, Any]]:
        """The part ``name`` as rows: a list of objects, or one object as one row."""
        found = self.part(name)
        if isinstance(found, dict):
            return [found]
        if isinstance(found, list):
            return [row for row in found if isinstance(row, dict)]
        return []


@dataclass(frozen=True)
class OrderRef:
    """What a cancel names an order by: the KRX forwarding branch and the order number."""

    branch: str  # KRX_FWDG_ORD_ORGNO
    number: str  # ODNO


def order_ref(answer: Answer) -> OrderRef | None:
    """The order an accepted order's answer names; None if it names none."""
    for row in answer.rows():
        low = {str(k).lower(): str(v).strip() for k, v in row.items()}
        branch, number = low.get("krx_fwdg_ord_orgno", ""), low.get("odno", "")
        if branch and number:
            return OrderRef(branch, number)
    return None


Token = Callable[[], Awaitable[str]]


class Account:
    """One account's calls with one app key, on one server (``mock``: the mock server's TR IDs).

    Each call is sent once through ``limiter`` and its answer returned as it came (``Answer``),
    also kept in ``answers``, in order.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        rest_url: str,
        app_key: str,
        app_secret: str,
        account: AccountNo,
        token: Token,
        *,
        mock: bool,
        limiter: RateLimiter,
        timeout: float = 15.0,
    ) -> None:
        self._session = session
        self._rest_url = rest_url.rstrip("/")
        self._account = account
        self._token = token
        self.mock = mock
        self._limiter = limiter
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._headers = {
            "appkey": app_key,
            "appsecret": app_secret,
            "custtype": "P",
            "content-type": "application/json; charset=utf-8",
        }
        self.answers: list[Answer] = []

    # Orders: each a POST, sent once.

    async def buy(self, symbol: str, quantity: int, price: int) -> Answer:
        """A limit buy of ``quantity`` shares at ``price`` on KRX."""
        return await self._order(BUY, symbol, quantity, price)

    async def sell(self, symbol: str, quantity: int, price: int) -> Answer:
        """A limit sell (일반매도) of ``quantity`` shares at ``price`` on KRX."""
        return await self._order(SELL, symbol, quantity, price, {"SLL_TYPE": GENERAL_SELL})

    async def cancel(self, order: OrderRef) -> Answer:
        """Cancel what is left of ``order``, all of it."""
        body = {
            **self._account.fields,
            "KRX_FWDG_ORD_ORGNO": order.branch,
            "ORGN_ODNO": order.number,
            "ORD_DVSN": LIMIT,
            "RVSE_CNCL_DVSN_CD": CANCEL_CODE,
            "ORD_QTY": "0",
            "ORD_UNPR": "0",
            "QTY_ALL_ORD_YN": "Y",
            "EXCG_ID_DVSN_CD": KRX,
        }
        return await self.post(CANCEL.of(self.mock), CANCEL_PATH, body)

    async def _order(
        self, tr: Tr, symbol: str, quantity: int, price: int, extra: Mapping[str, str] | None = None
    ) -> Answer:
        body = {
            **self._account.fields,
            "PDNO": symbol,
            "ORD_DVSN": LIMIT,
            "ORD_QTY": str(quantity),
            "ORD_UNPR": str(price),
            "EXCG_ID_DVSN_CD": KRX,
            **(extra or {}),
        }
        return await self.post(tr.of(self.mock), ORDER_PATH, body)

    # Inquiries: GETs, one page each (the caller asks the next with the answer's context).

    async def fills(
        self, day: date, symbol: str = "", order_no: str = "", context: tuple[str, str] = ("", "")
    ) -> Answer:
        """The orders of ``day`` and their fills (``output1``) and their totals (``output2``),
        oldest first; of ``symbol`` and ``order_no`` when they are given."""
        ymd = day.strftime("%Y%m%d")
        params = {
            **self._account.fields,
            "INQR_STRT_DT": ymd,
            "INQR_END_DT": ymd,
            "SLL_BUY_DVSN_CD": "00",  # both sides
            "PDNO": symbol,
            "CCLD_DVSN": "00",  # filled or not
            "INQR_DVSN": "01",  # oldest first
            "INQR_DVSN_3": "00",
            "ORD_GNO_BRNO": "",
            "ODNO": order_no,
            "INQR_DVSN_1": "",
            "CTX_AREA_FK100": context[0],
            "CTX_AREA_NK100": context[1],
            "EXCG_ID_DVSN_CD": KRX,
        }
        return await self.get(FILLS.of(self.mock), FILLS_PATH, params, "N" if any(context) else "")

    async def balance(self, context: tuple[str, str] = ("", "")) -> Answer:
        """The holdings by symbol (``output1``) and the account's totals (``output2``)."""
        params = {
            **self._account.fields,
            "AFHR_FLPR_YN": "N",
            "OFL_YN": "",
            "INQR_DVSN": "02",  # by symbol
            "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "00",  # with the day's trades
            "CTX_AREA_FK100": context[0],
            "CTX_AREA_NK100": context[1],
        }
        return await self.get(BALANCE.of(self.mock), BALANCE_PATH, params, "N" if any(context) else "")

    async def orderable(self, symbol: str, price: int | None) -> Answer:
        """What can be bought of ``symbol``: at ``price`` (a limit), or at the market (None)."""
        params = {
            **self._account.fields,
            "PDNO": symbol,
            "ORD_UNPR": "" if price is None else str(price),
            "ORD_DVSN": MARKET if price is None else LIMIT,
            "CMA_EVLU_AMT_ICLD_YN": "N",
            "OVRS_ICLD_YN": "N",
        }
        return await self.get(ORDERABLE.of(self.mock), ORDERABLE_PATH, params)

    async def cancelable(self) -> Answer:
        """The orders that can still be cancelled, by order."""
        params = {
            **self._account.fields,
            "INQR_DVSN_1": "0",
            "INQR_DVSN_2": "0",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }
        return await self.get(CANCELABLE.of(self.mock), CANCELABLE_PATH, params)

    async def price(self, symbol: str) -> Answer:
        params = {"FID_COND_MRKT_DIV_CODE": QUOTES_MARKET, "FID_INPUT_ISCD": symbol}
        return await self.get(PRICE, PRICE_PATH, params)

    async def book(self, symbol: str) -> Answer:
        params = {"FID_COND_MRKT_DIV_CODE": QUOTES_MARKET, "FID_INPUT_ISCD": symbol}
        return await self.get(BOOK, BOOK_PATH, params)

    # The two ways a call is sent.

    async def get(self, tr_id: str, path: str, params: Mapping[str, str], tr_cont: str = "") -> Answer:
        return await self._send("GET", tr_id, path, params=dict(params), tr_cont=tr_cont)

    async def post(self, tr_id: str, path: str, body: Mapping[str, str]) -> Answer:
        return await self._send("POST", tr_id, path, body=dict(body))

    async def _send(
        self,
        method: str,
        tr_id: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        body: dict[str, str] | None = None,
        tr_cont: str = "",
    ) -> Answer:
        headers = {**self._headers, "authorization": f"Bearer {await self._token()}", "tr_id": tr_id}
        if tr_cont:
            headers["tr_cont"] = tr_cont
        await self._limiter.acquire()
        began = time.monotonic()
        try:
            async with self._session.request(
                method,
                f"{self._rest_url}{path}",
                params=params,
                data=None if body is None else json.dumps(body),
                headers=headers,
                timeout=self._timeout,
            ) as response:
                text = await response.text()
                try:
                    parsed: Any = json.loads(text)
                except ValueError:
                    parsed = None
                answer = Answer(
                    tr_id,
                    response.status,
                    parsed,
                    {"tr_cont": response.headers.get("tr_cont", "")},
                    None if parsed is not None else f"not JSON ({len(text)} characters)",
                    time.monotonic() - began,
                )
        except (aiohttp.ClientError, TimeoutError, OSError) as e:
            answer = Answer(tr_id, None, None, {}, f"{type(e).__name__}: {e}", time.monotonic() - began)
        self.answers.append(answer)
        return answer


async def issue_token(
    session: aiohttp.ClientSession, rest_url: str, app_key: str, app_secret: str, timeout: float = 15.0
) -> Answer:
    """An access token's issuance (``/oauth2/tokenP``), answered as it came: ``kis.issue_access_token``
    raises on a refusal instead, and ``auth.TokenManager`` caches and spaces them."""
    body = {"grant_type": "client_credentials", "appkey": app_key, "appsecret": app_secret}
    began = time.monotonic()
    try:
        async with session.post(
            f"{rest_url.rstrip('/')}/oauth2/tokenP", json=body, timeout=aiohttp.ClientTimeout(total=timeout)
        ) as response:
            text = await response.text()
            try:
                parsed: Any = json.loads(text)
            except ValueError:
                parsed = None
            return Answer("tokenP", response.status, parsed, {}, None, time.monotonic() - began)
    except (aiohttp.ClientError, TimeoutError, OSError) as e:
        return Answer("tokenP", None, None, {}, f"{type(e).__name__}: {e}", time.monotonic() - began)


# The trader's broker (#404): KIS's answers read by the fields the probe measured (``PROBE_COMMENT``).

ACCEPTED, REFUSED, UNKNOWN = "accepted", "refused", "unknown"


class BrokerError(Exception):
    """A broker call that did not go as asked. Its text names the call and KIS's code and message,
    never a value of the account."""


class NoToken(BrokerError):
    """No access token could be had, so the call was not sent."""


class Unanswered(BrokerError):
    """A read KIS did not answer with its data: no answer, or not ``rt_cd`` 0."""

    def __init__(self, what: str, answer: Answer) -> None:
        if answer.status is None:
            said = f"no answer ({answer.error})"
        else:
            said = f"HTTP {answer.status} {answer.code or '-'} {answer.message}".rstrip()
        super().__init__(f"{what}: {said}")
        self.answer = answer


class Unexpected(BrokerError):
    """An answer short of a field the probe measured, or with one not as measured."""


def same_number(a: Any, b: Any) -> bool:
    """Two order numbers alike, their leading zeros aside; never when one is empty."""
    x, y = str(a or "").strip().lstrip("0"), str(b or "").strip().lstrip("0")
    return bool(x) and x == y


@dataclass(frozen=True)
class Sent:
    """What became of an order or a cancel: KIS accepted it, naming the order (``ref``; a cancel's is
    the cancel's own number); refused it (``reason``); or it is not known (``UNKNOWN``: no answer, or
    an acceptance naming no order). Whether an unknown one went in, the day's inquiry tells."""

    outcome: str
    answer: Answer
    ref: OrderRef | None = None

    @property
    def accepted(self) -> bool:
        return self.outcome == ACCEPTED

    @property
    def code(self) -> str:
        return self.answer.code

    @property
    def reason(self) -> str:
        """Why KIS refused it: the meaning of its code (``REFUSALS``), else KIS's message; empty
        unless it was refused."""
        if self.outcome != REFUSED:
            return ""
        return REFUSALS.get(self.code) or self.answer.message or f"HTTP {self.answer.status}"


def sent_of(answer: Answer) -> Sent:
    """An order's or a cancel's answer read: accepted (``rt_cd`` 0 and the order it names), refused
    (another ``rt_cd``), else unknown."""
    if not isinstance(answer.body, dict) or answer.status is None:
        return Sent(UNKNOWN, answer)
    if answer.ok:
        ref = order_ref(answer)
        return Sent(ACCEPTED, answer, ref) if ref is not None else Sent(UNKNOWN, answer)
    return Sent(REFUSED, answer) if "rt_cd" in answer.body else Sent(UNKNOWN, answer)


@dataclass(frozen=True)
class OrderRow:
    """An order of the day as the inquiry shows it (``output1``), or a cancel of one (``cancel``: its
    own row, ``cncl_yn`` Y, ``original`` the order it cancels, ``price`` 0).

    As measured on the mock server: an order's ``rmn_qty`` (``left``) falls to 0 once it is filled
    or cancelled; ``tot_ccld_qty`` is what filled, at ``avg_prvs`` on average (0 before a fill),
    which is the fills' price, not the order's: a buy at the best ask filled half a tick under
    it, a sell at the best bid half a tick over it. A cancelled order keeps its row, ``cncl_yn`` N.
    """

    number: str  # odno
    original: str  # orgn_odno: the order a cancel is of; "" for an order (KIS sends 0)
    side: str  # sll_buy_dvsn_cd: SIDE_BUY, SIDE_SELL
    symbol: str  # pdno
    quantity: int  # ord_qty
    price: int  # ord_unpr
    filled: int  # tot_ccld_qty
    left: int  # rmn_qty
    rejected: int  # rjct_qty
    average: float  # avg_prvs
    cancel: bool  # cncl_yn Y
    at: str  # ord_tmd, HHMMSS

    @classmethod
    def of(cls, row: Mapping[str, Any]) -> OrderRow:
        original = _field(row, "orgn_odno")
        return cls(
            number=_field(row, "odno"),
            original="" if not original.strip("0") else original,
            side=_field(row, "sll_buy_dvsn_cd"),
            symbol=_field(row, "pdno"),
            quantity=_count(row, "ord_qty"),
            price=_count(row, "ord_unpr"),
            filled=_count(row, "tot_ccld_qty"),
            left=_count(row, "rmn_qty"),
            rejected=_count(row, "rjct_qty"),
            average=_amount(row, "avg_prvs"),
            cancel=_field(row, "cncl_yn") == "Y",
            at=_field(row, "ord_tmd"),
        )

    @property
    def cancelled(self) -> int:
        """What of an order a cancel took: neither filled, left nor rejected (0 on a cancel's row).
        Not ``cncl_cfrm_qty``, whose values the probe did not show."""
        return 0 if self.cancel else max(0, self.quantity - self.filled - self.left - self.rejected)

    def of_order(self, ref: OrderRef) -> bool:
        """The row is the order ``ref`` (not a cancel of it)."""
        return not self.cancel and same_number(self.number, ref.number)


@dataclass(frozen=True)
class Holding:
    """A symbol held (the balance's ``output1``)."""

    symbol: str  # pdno
    quantity: int  # hldg_qty
    sellable: int  # ord_psbl_qty: less what open sells hold
    average: float  # pchs_avg_pric: the price paid on average


@dataclass(frozen=True)
class Balance:
    """The account (the balance's ``output2``) and what it holds. Measured on the mock server: a fill
    leaves ``dnca_tot_amt`` (the deposit) as it was until it settles, and moves
    ``prvs_rcdl_excc_amt`` (the deposit two days on) by its amount and costs; a symbol sold out
    has no row."""

    holdings: dict[str, Holding]  # by symbol, those of a quantity above 0
    deposit: int  # dnca_tot_amt
    settled: int  # prvs_rcdl_excc_amt
    total: int  # tot_evlu_amt
    costs: int  # thdt_tlex_amt: the day's commission and tax

    def held(self, symbol: str) -> int:
        holding = self.holdings.get(symbol)
        return holding.quantity if holding is not None else 0


@dataclass(frozen=True)
class Orderable:
    """What can be bought at a limit price (매수가능조회). Measured on the mock server: a fill lowers
    ``ord_psbl_cash`` by about its amount, and ``nrcvb_buy_qty`` by its shares."""

    cash: int  # ord_psbl_cash
    quantity: int  # nrcvb_buy_qty: the shares the cash buys at the price without credit
    amount: int  # nrcvb_buy_amt

    def most(self, price: int, fee_rate: float) -> int:
        """The most shares to buy at ``price``: what the orderable cash pays with the commission
        (``fee_rate`` a side), and no more than KIS's own count without credit. The mock server
        accepted a buy beyond that count (#403), so the trader keeps to it itself."""
        if price <= 0:
            return 0
        return max(0, min(self.quantity, math.floor(self.cash / (price * (1 + fee_rate)))))


@dataclass(frozen=True)
class Price:
    """KRX's price of a symbol (주식현재가 시세)."""

    price: int  # stck_prpr
    upper: int  # stck_mxpr: the day's upper limit
    lower: int  # stck_llam: its lower limit
    tick: int  # aspr_unit: measured equal to ``pricing.tick_size``


@dataclass(frozen=True)
class Best:
    """KRX's best ask and bid of a symbol (주식현재가 호가)."""

    ask: int  # askp1
    bid: int  # bidp1
    ask_size: int  # askp_rsqn1
    bid_size: int  # bidp_rsqn1
    at: str  # aspr_acpt_hour, HHMMSS


@dataclass(frozen=True)
class Taken:
    """A marketable limit order's end (``Broker.take``): the order sent, its row on the inquiry at
    the end, and the cancel of what was left, if one was sent."""

    sent: Sent
    row: OrderRow | None = None
    cancel: Sent | None = None

    @property
    def filled(self) -> int:
        return self.row.filled if self.row is not None else 0

    @property
    def average(self) -> float:
        """The fills' average price; 0 without a fill."""
        return self.row.average if self.row is not None else 0.0

    @property
    def left(self) -> int | None:
        """What may still fill: 0 once it is filled or cancelled whole, or was refused; None when
        what became of the order is not known (no answer to it, or never seen on the inquiry)."""
        if self.sent.outcome == REFUSED:
            return 0
        return self.row.left if self.row is not None else None


class Tokens(Protocol):
    """The access token a broker's calls carry (``auth.TokenManager``)."""

    async def get(self) -> str: ...

    def invalidate(self) -> None: ...


# What a call to the token can raise (``TokenManager.get``): no token, so nothing is sent.
_TOKEN_FAILED = (kis.KisError, aiohttp.ClientError, OSError, TimeoutError, ValueError, KeyError)


class Broker:
    """The trader's KIS account (#404): orders, cancels, the day's orders and fills, the balance, the
    orderable cash and the REST quotes, each answer read by the fields the probe measured
    (``PROBE_COMMENT``). ``trader_broker`` makes it with the trader's own key and token.

    Every call goes through ``account``'s limiter. A call refused for the per-second limit
    (``BUSY``) goes again after ``pause``, ``retries`` times at most; one refused for its token
    (``TOKEN_REJECTED``) once more, with a new token (``tokens``, issued at most once a minute).
    Nothing else is sent again (the module's docstring). A read that is not answered with its data
    raises ``Unanswered``; one short of a measured field ``Unexpected``; without a token ``NoToken``.
    An order or a cancel returns ``Sent`` instead: accepted, refused or unknown.

    Fills are read on the day's inquiry (``FILLS``), polled after each order (``take``, ``order``),
    not on the real-time fill notice, whose fields on the mock server are not known yet (#403).
    """

    def __init__(
        self, account: Account, tokens: Tokens, *, pause: float = BUSY_PAUSE, retries: int = BUSY_RETRIES
    ) -> None:
        self.account = account
        self._tokens = tokens
        self._pause = pause
        self._retries = retries

    # Orders.

    async def buy(self, symbol: str, quantity: int, price: int) -> Sent:
        """A limit buy of ``quantity`` shares at ``price`` on KRX."""
        return sent_of(await self._call("buy", partial(self.account.buy, symbol, quantity, price)))

    async def sell(self, symbol: str, quantity: int, price: int) -> Sent:
        """A limit sell of ``quantity`` shares at ``price`` on KRX."""
        return sent_of(await self._call("sell", partial(self.account.sell, symbol, quantity, price)))

    async def cancel(self, order: OrderRef) -> Sent:
        """Cancel what is left of ``order``."""
        return sent_of(await self._call("cancel", partial(self.account.cancel, order)))

    async def take(
        self,
        symbol: str,
        side: str,
        quantity: int,
        price: int,
        day: date,
        *,
        wait: float = TAKE_WAIT,
        poll: float = POLL,
    ) -> Taken:
        """A limit order meant to fill at once (a buy at the best ask, a sell at the best bid): sent,
        its fills read on ``day``'s inquiry every ``poll`` seconds until it is filled whole or
        ``wait`` has passed, what is left then cancelled, and the inquiry read again until nothing
        is left (a fill can come before the cancel: KIS then finds less or nothing to cancel). What
        filled is the row's ``filled``, at its ``average``; a partial fill is a position of that.

        A refused order ends there. One without an answer, or accepted without naming its order, is
        not followed: whether it went in, the inquiry shows (``Taken.left`` None)."""
        order = self.buy if side == SIDE_BUY else self.sell
        sent = await order(symbol, quantity, price)
        if not sent.accepted or sent.ref is None:
            return Taken(sent)
        row = await self._follow(day, symbol, sent.ref, wait, poll)
        if row is not None and row.left == 0:
            return Taken(sent, row)
        cancel = await self.cancel(sent.ref)
        row = await self._follow(day, symbol, sent.ref, wait, poll) or row
        return Taken(sent, row, cancel)

    async def _follow(
        self, day: date, symbol: str, ref: OrderRef, wait: float, poll: float
    ) -> OrderRow | None:
        """The order's row, read until nothing is left of it or ``wait`` has passed: the last read
        (None if none showed it). A read that fails is said and read again at the next poll."""
        deadline = time.monotonic() + wait
        row: OrderRow | None = None
        while True:
            try:
                row = await self.order(day, symbol, ref) or row
            except BrokerError as e:
                log.warning("the inquiry of a trader's order failed (%s); read again in %gs", e, poll)
            if (row is not None and row.left == 0) or time.monotonic() >= deadline:
                return row
            await asyncio.sleep(poll)

    # Inquiries.

    async def orders(self, day: date, symbol: str = "", order_no: str = "") -> list[OrderRow]:
        """The orders of ``day`` and the cancels of them, oldest first, every page (``PAGES`` at
        most); of ``symbol`` and ``order_no`` when they are given."""
        rows: list[OrderRow] = []
        context = ("", "")
        for _ in range(PAGES):
            answer = await self._read("orders", partial(self.account.fills, day, symbol, order_no, context))
            rows += [OrderRow.of(row) for row in answer.rows("output1")]
            if not answer.more:
                return rows
            context = answer.context
        raise Unexpected(f"orders: more than {PAGES} pages")

    async def order(self, day: date, symbol: str, ref: OrderRef) -> OrderRow | None:
        """The order ``ref``'s own row on ``day``'s inquiry; None if it shows none. The rows are
        matched here by the order number, KIS's own filter by it aside."""
        return next((row for row in await self.orders(day, symbol, ref.number) if row.of_order(ref)), None)

    async def open_orders(self, day: date, symbol: str = "") -> list[OrderRow]:
        """The orders of ``day`` with something left to fill. The cancellable-orders inquiry is not
        a mock TR (``NOT_MOCK``), so they are read off the day's inquiry."""
        return [row for row in await self.orders(day, symbol) if not row.cancel and row.left > 0]

    async def balance(self) -> Balance:
        """The account's balance, every page."""
        holdings: dict[str, Holding] = {}
        totals: list[dict[str, Any]] = []
        context = ("", "")
        for _ in range(PAGES):
            answer = await self._read("balance", partial(self.account.balance, context))
            for row in answer.rows("output1"):
                held = Holding(
                    _field(row, "pdno"),
                    _count(row, "hldg_qty"),
                    _count(row, "ord_psbl_qty"),
                    _amount(row, "pchs_avg_pric"),
                )
                if held.quantity > 0:
                    holdings[held.symbol] = held
            totals += answer.rows("output2")
            if not answer.more:
                break
            context = answer.context
        else:
            raise Unexpected(f"balance: more than {PAGES} pages")
        if not totals:
            raise Unexpected("balance: no output2")
        total = totals[0]
        return Balance(
            holdings,
            _count(total, "dnca_tot_amt"),
            _count(total, "prvs_rcdl_excc_amt"),
            _count(total, "tot_evlu_amt"),
            _count(total, "thdt_tlex_amt"),
        )

    async def orderable(self, symbol: str, price: int) -> Orderable:
        """What can be bought of ``symbol`` at the limit ``price``."""
        answer = await self._read("orderable", partial(self.account.orderable, symbol, price))
        row = _one(answer, "orderable", "output")
        return Orderable(
            _count(row, "ord_psbl_cash"), _count(row, "nrcvb_buy_qty"), _count(row, "nrcvb_buy_amt")
        )

    async def price(self, symbol: str) -> Price:
        """KRX's price of ``symbol`` now, with the day's limits and the tick."""
        answer = await self._read("price", partial(self.account.price, symbol))
        row = _one(answer, "price", "output")
        return Price(
            _count(row, "stck_prpr"),
            _count(row, "stck_mxpr"),
            _count(row, "stck_llam"),
            _count(row, "aspr_unit"),
        )

    async def best(self, symbol: str) -> Best:
        """KRX's best ask and bid of ``symbol`` now (the REST quote, when the stream is down, #407)."""
        answer = await self._read("book", partial(self.account.book, symbol))
        row = _one(answer, "book", "output1")
        return Best(
            _count(row, "askp1"),
            _count(row, "bidp1"),
            _count(row, "askp_rsqn1"),
            _count(row, "bidp_rsqn1"),
            _field(row, "aspr_acpt_hour"),
        )

    # Sending.

    async def _call(self, what: str, send: Callable[[], Awaitable[Answer]]) -> Answer:
        """``send`` once, and again by the rules above: the last answer."""
        busy, renewed = 0, False
        while True:
            try:
                answer = await send()
            except _TOKEN_FAILED as e:
                raise NoToken(f"{what}: no access token ({type(e).__name__}: {e}), nothing sent") from e
            if answer.code == BUSY and busy < self._retries:
                busy += 1
                log.warning(
                    "KIS refused the trader's %s for its per-second limit (%s); sent again in %gs",
                    what,
                    BUSY,
                    self._pause,
                )
                await asyncio.sleep(self._pause)
                continue
            if answer.code in TOKEN_REJECTED and not renewed:
                renewed = True
                log.warning(
                    "KIS refused the trader's token for its %s (%s); sent again with a new one",
                    what,
                    answer.code,
                )
                self._tokens.invalidate()
                continue
            return answer

    async def _read(self, what: str, send: Callable[[], Awaitable[Answer]]) -> Answer:
        answer = await self._call(what, send)
        if not answer.ok:
            raise Unanswered(what, answer)
        return answer


def trader_broker(
    session: aiohttp.ClientSession,
    settings: Settings,
    now: Callable[[], datetime],
    *,
    servers: Servers = MOCK,
    per_second: int = PER_SECOND,
    pause: float = BUSY_PAUSE,
) -> Broker:
    """The trader's broker on KIS's mock server: the trader's own key and account
    (``KRX_TRADER_KIS_*``), never the collector's; its token cached in ``state/trader_kis_token.json``,
    reused across restarts and issued at most once a minute (``auth.TokenManager``: KIS answered a
    second token asked at once with ``TOKEN_TOO_SOON``); its calls paced at ``per_second``.

    SettingsError when a key is missing, when it is the collector's, or when the trader's servers
    are not ``servers`` (the mock ones): the trader orders on the mock server only until its live
    mode (#408)."""
    key, secret = settings.trader_kis_app_key, settings.trader_kis_app_secret
    if not (key and secret and settings.trader_kis_account):
        raise SettingsError(
            "KRX_TRADER_KIS_APP_KEY, KRX_TRADER_KIS_APP_SECRET and KRX_TRADER_KIS_ACCOUNT are needed for "
            "the trader's KIS orders"
        )
    if key == settings.kis_app_key:
        raise SettingsError(
            "KRX_TRADER_KIS_APP_KEY is the collector's KIS_APP_KEY: the trader uses a key of its own, so "
            "the two share neither the call limits nor the token"
        )
    if not Servers(settings.trader_kis_rest_url, settings.trader_kis_ws_url).same_as(servers):
        raise SettingsError(
            "KRX_TRADER_KIS_REST_URL and KRX_TRADER_KIS_WS_URL are not KIS's mock servers: the trader "
            "orders on the mock server only until its live mode (#408)"
        )
    try:
        account_no = AccountNo.parse(settings.trader_kis_account)
    except ValueError as e:
        raise SettingsError(f"KRX_TRADER_KIS_ACCOUNT: {e}") from None
    tokens = TokenManager(
        session,
        settings.trader_kis_rest_url,
        key,
        secret,
        settings.state_dir / TOKEN_FILE,
        now,
        timedelta(seconds=settings.token_refresh_margin_seconds),
        settings.token_min_interval_seconds,
    )
    account = Account(
        session,
        settings.trader_kis_rest_url,
        key,
        secret,
        account_no,
        tokens.get,
        mock=True,
        limiter=RateLimiter(per_second),
    )
    return Broker(account, tokens, pause=pause)


def _field(row: Mapping[str, Any], name: str) -> str:
    value = row.get(name)
    if value is None:
        raise Unexpected(f"no {name}")
    return str(value).strip()


def _amount(row: Mapping[str, Any], name: str) -> float:
    try:
        value = float(_field(row, name))
    except ValueError:
        raise Unexpected(f"{name} is not a number") from None
    if not math.isfinite(value):
        raise Unexpected(f"{name} is not a number")
    return value


def _count(row: Mapping[str, Any], name: str) -> int:
    value = _amount(row, name)
    if not value.is_integer():
        raise Unexpected(f"{name} is not a whole number")
    return int(value)


def _one(answer: Answer, what: str, part: str) -> Mapping[str, Any]:
    rows = answer.rows(part)
    if not rows:
        raise Unexpected(f"{what}: no {part}")
    return rows[0]


# The real-time fill notice.


def decrypt(key: str, iv: str, text: str) -> str:
    """A fill notice's fields: ``text`` is base64 of AES-256-CBC with PKCS#7 padding, under the
    key and IV the subscription's answer gave (KIS's example, ``aes_cbc_base64_dec``)."""
    decryptor = Cipher(algorithms.AES(key.encode()), modes.CBC(iv.encode())).decryptor()
    padded = decryptor.update(base64.b64decode(text)) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return (unpadder.update(padded) + unpadder.finalize()).decode("utf-8")


@dataclass
class Notices:
    """What the fill notice's WebSocket brought, as it came, with when (``time.monotonic()``)."""

    answer: dict[str, Any] | None = None  # the subscription's answer (its key and IV within)
    frames: list[tuple[float, str]] = field(default_factory=list)  # the data frames
    controls: list[tuple[float, str]] = field(default_factory=list)  # other messages (PINGPONG...)
    error: str | None = None  # why the connection ended, if it was not closed on purpose
    subscribed: asyncio.Event = field(default_factory=asyncio.Event)  # answered (or failed)
    changed: asyncio.Event = field(default_factory=asyncio.Event)  # set on each frame

    @property
    def key_iv(self) -> tuple[str, str] | None:
        output = ((self.answer or {}).get("body") or {}).get("output") or {}
        key, iv = output.get("key"), output.get("iv")
        return (str(key), str(iv)) if key and iv else None


async def listen_fill_notices(
    session: aiohttp.ClientSession,
    ws_url: str,
    approval_key: str,
    hts_id: str,
    notices: Notices,
    *,
    mock: bool,
) -> None:
    """Subscribe to the fill notice of ``hts_id`` and keep what comes in ``notices`` until
    cancelled; PINGPONG is answered as the collector answers it."""
    try:
        async with session.ws_connect(
            ws_url.rstrip("/") + kis.WS_PATH, timeout=aiohttp.ClientWSTimeout(ws_receive=None, ws_close=5.0)
        ) as ws:
            await ws.send_str(kis.subscribe_message(approval_key, FILL_NOTICE.of(mock), hts_id))
            async for message in ws:
                if message.type != aiohttp.WSMsgType.TEXT:
                    continue
                text = str(message.data)
                at = time.monotonic()
                if text[:1] in ("0", "1"):
                    notices.frames.append((at, text))
                    notices.changed.set()
                    continue
                notices.controls.append((at, text))
                try:
                    raw = json.loads(text)
                except ValueError:
                    continue
                header = raw.get("header", {}) if isinstance(raw, dict) else {}
                if header.get("tr_id") == "PINGPONG":
                    await ws.pong(text.encode())
                elif notices.answer is None and header.get("tr_id") == FILL_NOTICE.of(mock):
                    notices.answer = raw
                    notices.subscribed.set()
            notices.error = notices.error or "the connection closed"
    except (aiohttp.ClientError, OSError, TimeoutError) as e:
        notices.error = f"{type(e).__name__}: {e}"
    finally:
        notices.subscribed.set()
        notices.changed.set()
