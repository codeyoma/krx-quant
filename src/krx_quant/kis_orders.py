"""The trader's KIS account calls (#393, #403): cash orders and their cancel, the order and fill
inquiry, the balance and the orderable cash, the quote an order is priced from, and the real-time
fill notice.

This is the one file of the repository the forbidden-pattern check lets hold KIS order TR IDs and
order endpoints (``forbidden.ALLOWED_IN``, ``scripts/check_forbidden.py``): they are written here
and nowhere else, and every other module reaches them through this one. The collector never
imports it. The order probe (``order_probe.py``) does, in a scheduled job's own process, and so
will the trader (#404).

Each TR ID comes in two, the live server's and the mock (모의투자) server's (``Tr``): an
``Account`` is made for one of them (``mock``), with the server it calls.

What KIS's answers mean is not known here. The paths, TR IDs and fields come from KIS's examples
(koreainvestment/open-trading-api, ``examples_llm/domestic_stock``, read on 2026-10-29) and are
hypotheses until the probe's comment on #403 shows them (CODING_STANDARDS.md, "External data is
probed before it is used"). So every call returns KIS's answer as it came (``Answer``): nothing is
read into it, and nothing is sent again. An order sent twice is two orders, so a call whose
connection fails is not retried (aiohttp sends a POST once: it retries idempotent methods only),
and the caller learns what became of it from the inquiry.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import aiohttp
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from krx_quant import kis
from krx_quant.config import KIS_MOCK_REST_URL, KIS_MOCK_WS_URL
from krx_quant.ratelimit import RateLimiter


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
# a live TR ID only, so the mock is asked the same one and its answer shows whether it has it.
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

# The fill notice's fields, in order (KIS's example, ``notice_cols``).
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
