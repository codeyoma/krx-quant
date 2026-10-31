"""The live account's answers, read before the live mode trusts them (#408, CODING_STANDARDS.md
"External data is probed before it is used").

The trader's broker (``kis_orders.Broker``) reads the live server's answers by the fields the order
probe measured on the mock server (#403). ``krx-quant probe-live-account`` reads the live account
once with the trader's live key (``KRX_TRADER_LIVE_KIS_*``, ``kis_orders.trader_live_broker``) by
inquiry calls alone: KRX's price of 005930, the balance, the orderable cash at that price, and the
day order inquiry of the evening's trading day. It places no order and sends no order TR: this
module holds no TR ID, and reads through ``kis_orders.Account``'s inquiry methods alone.

For each answer it records whether KIS answered it with its data (HTTP 200, ``rt_cd`` 0), its code
and its pages, and each field the broker reads (``EXPECTED``) by its look, never its value: present
in every row or not, a whole number, a number or text as the broker reads it. A part with rows is
checked; a part that may hold none (the holdings of an account holding nothing, the orders of a
day without one) is "not seen", which passes: the order rows' fields, and the fill price and
quantity a real fill alone shows, are checked on the first live day instead, before the first period
counts it (``trader/live.py``, ``fill_problems``).

The run is kept in the trader's store (``live_probes``): the live mode's checks read the last one
(``live.readiness``'s ``live_probe``), and the live mode orders only once one passed. The scheduled
job ``probe-live-account-408`` runs it every trading night once the live key is set, until a run
passes (docs/operations.md, "예약 작업"). Its output holds codes, pages, field names and their looks,
never a key, an account, an order number or a value.

The exit codes: ``PASSED`` (0) every answer as the broker reads it; ``REFUSED`` the live key is the
collector's or the mock one, or the account the mock one (nothing sent); ``NO_KEYS`` the live key,
secret or account is missing (nothing sent); ``UNREADABLE`` a call KIS did not answer with its data;
``DIFFERENT`` an answer short of a field the broker reads, or with one not as it reads it.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from krx_quant import kis
from krx_quant.config import Settings, SettingsError
from krx_quant.feeds.base import KST
from krx_quant.kis_orders import LIVE, Answer, Servers, trader_live_broker
from krx_quant.market_calendar import MarketCalendar, calendar_store
from krx_quant.order_probe import said
from krx_quant.redaction import redact, secrets_of
from krx_quant.trader.report import report_day
from krx_quant.trader.store import DB_NAME, TraderStore

TITLE = "KIS 실전 계좌 읽기 프로브 (#408)"
SYMBOL = "005930"
PASSED, REFUSED, NO_KEYS, UNREADABLE, DIFFERENT = 0, 2, 3, 4, 5
KEYS = ("KRX_TRADER_LIVE_KIS_APP_KEY", "KRX_TRADER_LIVE_KIS_APP_SECRET", "KRX_TRADER_LIVE_KIS_ACCOUNT")
# How the broker reads a field (``kis_orders._field``, ``_count``, ``_amount``).
COUNT, AMOUNT, TEXT = "count", "amount", "text"
LOOKS = {COUNT: "정수", AMOUNT: "숫자", TEXT: "글자"}


@dataclass(frozen=True)
class Expected:
    """A part of an answer the broker reads: its name in the answer, the fields it reads and how, and
    whether it may hold no row (then not seen passes)."""

    title: str
    part: str
    fields: Mapping[str, str]
    may_be_empty: bool = False


PRICE = Expected(
    "현재가", "output", {"stck_prpr": COUNT, "stck_mxpr": COUNT, "stck_llam": COUNT, "aspr_unit": COUNT}
)
HOLDINGS = Expected(
    "잔고의 보유",
    "output1",
    {"pdno": TEXT, "hldg_qty": COUNT, "ord_psbl_qty": COUNT, "pchs_avg_pric": AMOUNT},
    may_be_empty=True,
)
TOTALS = Expected(
    "잔고의 합계",
    "output2",
    {"dnca_tot_amt": COUNT, "prvs_rcdl_excc_amt": COUNT, "tot_evlu_amt": COUNT, "thdt_tlex_amt": COUNT},
)
ORDERABLE = Expected(
    "매수가능조회", "output", {"ord_psbl_cash": COUNT, "nrcvb_buy_qty": COUNT, "nrcvb_buy_amt": COUNT}
)
ORDERS = Expected(
    "주문 체결 조회의 행",
    "output1",
    {
        "odno": TEXT,
        "orgn_odno": TEXT,
        "sll_buy_dvsn_cd": TEXT,
        "pdno": TEXT,
        "ord_qty": COUNT,
        "ord_unpr": COUNT,
        "tot_ccld_qty": COUNT,
        "rmn_qty": COUNT,
        "rjct_qty": COUNT,
        "avg_prvs": AMOUNT,
        "cncl_yn": TEXT,
        "ord_tmd": TEXT,
    },
    may_be_empty=True,
)


def look(value: Any, how: str) -> bool:
    """Whether ``value`` reads as the broker reads it: text present; a finite number; a whole one."""
    if value is None:
        return False
    if how == TEXT:
        return True
    try:
        number = float(str(value).strip())
    except ValueError:
        return False
    return math.isfinite(number) and (how == AMOUNT or number.is_integer())


@dataclass(frozen=True)
class Part:
    """A part of an answer checked: its rows' count, and the fields missing or not as read."""

    expected: Expected
    rows: int
    missing: tuple[str, ...] = ()
    wrong: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        if self.rows == 0:
            return self.expected.may_be_empty
        return not self.missing and not self.wrong

    def line(self) -> str:
        e = self.expected
        if self.rows == 0:
            return f"  - {e.title}({e.part}): 행 없음 → " + (
                "보지 못함(통과: 첫 실전 날에 본다)" if e.may_be_empty else "다르다(행이 있어야 한다)"
            )
        reads = ", ".join(f"{name}({LOOKS[how]})" for name, how in e.fields.items())
        out = f"  - {e.title}({e.part}) {self.rows}행: trader가 읽는 {reads}"
        if self.missing:
            out += f" · 없는 것: {', '.join(self.missing)}"
        if self.wrong:
            out += f" · 꼴이 다른 것: {', '.join(self.wrong)}"
        return out + (" → 같다" if self.passed else " → 다르다")

    def as_json(self) -> dict[str, Any]:
        return {
            "part": self.expected.part,
            "rows": self.rows,
            "missing": list(self.missing),
            "wrong": list(self.wrong),
            "passed": self.passed,
        }


def check(answer: Answer, expected: Expected) -> Part:
    """``answer``'s part ``expected`` against what the broker reads of it."""
    rows = answer.rows(expected.part)
    missing = [n for n in expected.fields if any(n not in row for row in rows)]
    wrong = [
        n
        for n, how in expected.fields.items()
        if n not in missing and not all(look(row.get(n), how) for row in rows)
    ]
    return Part(expected, len(rows), tuple(missing), tuple(wrong))


@dataclass
class Call:
    """One inquiry and its parts checked."""

    title: str
    answer: Answer
    parts: list[Part] = field(default_factory=list)

    @property
    def answered(self) -> bool:
        return self.answer.status == 200 and self.answer.ok

    @property
    def passed(self) -> bool:
        return self.answered and all(p.passed for p in self.parts)

    def as_json(self) -> dict[str, Any]:
        return {
            "answered": self.answered,
            "code": self.answer.code,
            "more": self.answer.more,
            "parts": [p.as_json() for p in self.parts],
            "passed": self.passed,
        }


Out = Callable[[str], None]


async def probe_live_account(
    settings: Settings,
    now: datetime,
    *,
    calendar: MarketCalendar | None = None,
    servers: Servers = LIVE,
    out: Out = print,
) -> int:
    """``krx-quant probe-live-account`` at ``now``: the live account read and checked, the run kept
    in the trader's store; its exit code (module doc)."""
    secrets = secrets_of(settings)

    def say(line: str) -> None:
        out(redact(line, secrets))

    local = now.astimezone(KST)
    say(f"{TITLE}: {local:%Y-%m-%d %H:%M:%S} KST, {SYMBOL}. 조회만 한다(주문 TR은 보내지 않는다)")
    say("")
    say("1. 설정")
    missing = [
        name
        for name, value in zip(
            KEYS,
            (
                settings.trader_live_kis_app_key,
                settings.trader_live_kis_app_secret,
                settings.trader_live_kis_account,
            ),
            strict=True,
        )
        if not value
    ]
    if missing:
        say(
            f"  - 실전 키가 없어 읽지 않았다: {', '.join(missing)}이(가) 서버 .env에 없다"
            "(docs/operations.md 1장 14). 키가 들어온 뒤의 거래일 밤에 다시 돈다."
        )
        return NO_KEYS
    calendar = calendar or calendar_store(settings).calendar
    day = report_day(calendar, now)
    async with kis.rest_session() as session:
        try:
            broker = trader_live_broker(session, settings, lambda: now, servers=servers)
        except SettingsError as e:
            say(f"  - 실전 키를 쓰지 않았다: {e}. 아무것도 보내지 않았다.")
            return REFUSED
        say("  - 실전 앱키, 앱시크릿, 계좌: 있음(수집기의 키·모의 키와 다르다). 서버: KIS 실전 서버")
        account = broker.account
        calls: list[Call] = []
        price_answer = await account.price(SYMBOL)
        calls.append(Call("2. 현재가(주식현재가 시세)", price_answer, [check(price_answer, PRICE)]))
        balance = await account.balance()
        calls.append(
            Call("3. 잔고(주식잔고조회)", balance, [check(balance, HOLDINGS), check(balance, TOTALS)])
        )
        price = _price_of(price_answer)
        if price is not None:
            orderable = await account.orderable(SYMBOL, price)
            calls.append(Call("4. 매수가능조회(현재가 지정가)", orderable, [check(orderable, ORDERABLE)]))
        orders = await account.fills(day, SYMBOL)
        calls.append(Call(f"5. 주문 체결 조회({day})", orders, [check(orders, ORDERS)]))
    for call in calls:
        say("")
        say(call.title)
        say(f"  - {said(call.answer)}")
        if not call.answered:
            say("  - KIS가 데이터로 답하지 않았다")
            continue
        for part in call.parts:
            say(part.line())
    if price is None:
        say("")
        say("4. 매수가능조회: 현재가를 읽지 못해 묻지 않았다")
    answered = all(c.answered for c in calls) and price is not None
    passed = answered and all(c.passed for c in calls)
    code = PASSED if passed else UNREADABLE if not answered else DIFFERENT
    detail = {"day": day.isoformat(), "calls": {c.title: c.as_json() for c in calls}}
    store = TraderStore(settings.state_dir / DB_NAME)
    try:
        store.add_live_probe(now, passed, detail)
    finally:
        store.close()
    say("")
    verdict = {
        PASSED: "같다: trader가 읽는 필드가 모두 실전의 답에 있고 그 꼴이다. live 모드의 실행 점검 "
        "`live_probe`가 통과한다. 체결가·체결 수량(주문 체결 조회의 행)은 첫 실전 날에 본다",
        UNREADABLE: "읽지 못했다: KIS가 데이터로 답하지 않은 조회가 있다. 다음 거래일 밤에 다시 돈다",
        DIFFERENT: "다르다: trader가 읽는 필드가 없거나 꼴이 다르다. live 모드는 주문하지 않는다. 에이전트가 "
        "kis_orders.py의 읽기를 고치는 PR을 연다",
    }[code]
    say(f"결과: {verdict}")
    return code


def _price_of(answer: Answer) -> int | None:
    rows = answer.rows("output")
    if not answer.ok or not rows or not look(rows[0].get("stck_prpr"), COUNT):
        return None
    price = int(float(str(rows[0]["stck_prpr"])))
    return price if price > 0 else None
