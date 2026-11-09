"""``krx-quant fill-notice-columns`` (#648): whether the mock server's fill notice tells column 10
(``CNTG_QTY``, a fill's quantity by KIS's example) from column 17 (``ODER_QTY``, the order's), and
whether the recorder's hourly disconnections cost a notice; for the user to decide whether the
recorder (``fill_notices.py``) stays.

**Why orders of more than one share.** #531 named the 23 columns (``kis_orders.MOCK_FILL_NOTICE_COLUMNS``)
on the night measure of 2026-11-04, but every order was of one share: the two columns held 1 on all
108 records, the cancels' among them, and so did their matches. They differ only on an order of more
than one share or one partly filled: a fill of part of the order tells the part in one and the
order in the other. The trader's mock orders are its practice's, one share each
(``selection.PRACTICE_SHARES``), until a champion with cells trades by its loss budget; so the night
job ``fill-notice-columns-648`` waits for ``APART`` such orders (``apart_orders``), not for a number
of days (#648's comment of 2026-11-09).

**The orders** are those ``notice_layout.gather`` joins the records to (the trader's ledger and KIS's
day order inquiry, read-only, the same join): an order telling the columns apart (``telling``) is a
buy or a sell (not a cancel, whose own quantity KIS's inquiry and the notice may count otherwise) of
more than one share that something filled of, wholly or in part (a partial fill is of such an order).

**The measure**, on the records of the mock server's width (23) joined to those orders, each column
by position against its order, never a value: the records it equals the order's quantity on (all of
them, and the acceptances: ``CNTG_YN`` 1), and the orders whose fills' records (``CNTG_YN`` 2) sum in
it to what filled of them. KIS's example holds when column 17 is the order's quantity on every
record and column 10 sums over the fills to what filled, on every order something filled of, with
at least one record where the two differ (``verdict``).

**The disconnections.** Each order of the recorded days with no record joined to it, and whether it
was placed (KIS's order time) while no connection was subscribed within the recording window
(``notice_layout.uncovered``): the hourly WARNING (``fill notice recorder: the fill notice's
WebSocket ended``) matters to the decision only if such orders lost their notice.

**The output** holds counts, column numbers, dates and spans of time: no value, order number,
account, HTS ID or price (it goes on the public issue #648). Its exit code: ``DONE`` (0) once
measured, ``UNREADABLE`` (4) when the records, the ledger or a day's inquiry could not be read.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import Counter
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

from krx_quant.config import Settings
from krx_quant.feeds.base import KST
from krx_quant.kis_orders import MOCK as MOCK_SERVERS
from krx_quant.kis_orders import MOCK_FILL_NOTICE_COLUMNS, Servers
from krx_quant.trader.fill_notices import DB_NAME, StoredRecord
from krx_quant.trader.notice_layout import (
    CANCEL,
    KINDS,
    UNREADABLE,
    Gathered,
    Layout,
    Order,
    _duration,
    _int,
    _key,
    _why,
    gather,
    ledger_of,
    uncovered,
)
from krx_quant.trader.report import WEEKDAYS
from krx_quant.trader.store import DB_NAME as TRADER_DB

log = logging.getLogger(__name__)

TITLE = "체결 통보 칸 10과 17 가르기 (#648)"
DONE = 0
# The orders telling the two columns apart the job waits for: 20 make one order that goes against a
# reading 5% of them, and each brings two records or more (its acceptance and its fills), so a
# column that matches all 20 is no chance. A champion trading by its loss budget (several shares an
# order) brings that many in one to two weeks of the practice's pace of buys.
APART = 20
WIDTH = len(MOCK_FILL_NOTICE_COLUMNS)
FILL_QTY = MOCK_FILL_NOTICE_COLUMNS.index("CNTG_QTY")  # column 10
ORDER_QTY = MOCK_FILL_NOTICE_COLUMNS.index("ODER_QTY")  # column 17
TOLD = MOCK_FILL_NOTICE_COLUMNS.index("CNTG_YN")  # column 14, measured (#531): 1 an acceptance, 2 a fill
ACCEPTANCE, FILL = "1", "2"
EXAMPLE, REVERSED, NEITHER, NOT_APART, TOO_FEW = (
    "example",
    "reversed",
    "neither",
    "not_apart",
    "too_few",
)


def telling(order: Order) -> bool:
    """An order whose notices may tell column 10 from 17: a buy or a sell of more than one share that
    something filled of (wholly or in part: an order of one share cannot fill in part, and an order of
    which nothing filled tells its quantity alone)."""
    return order.kind != CANCEL and order.quantity > 1 and order.filled > 0


def apart_orders(state_dir: Path) -> int:
    """How many of the trader's mock orders, on the days the recorder kept records of, tell the two
    columns apart by the ledger alone (``telling``: more than one share, something filled), for the
    job's condition (``scheduled.Facts.fill_notice_apart``). Read-only; 0 without either file or
    with one that cannot be read."""
    notices, ledger = state_dir / DB_NAME, state_dir / TRADER_DB
    if not notices.exists() or not ledger.exists():
        return 0
    try:
        with _read_only(notices) as db:
            days = [date.fromisoformat(d) for (d,) in db.execute("SELECT DISTINCT day FROM records")]
        with _read_only(ledger) as db:
            db.row_factory = sqlite3.Row
            return sum(1 for d in days for o in ledger_of(db, d).values() if o.quantity > 1 and o.filled > 0)
    except (sqlite3.Error, ValueError) as e:
        log.warning("cannot count the orders that tell the fill notice's columns apart: %s", e)
        return 0


def _read_only(path: Path) -> closing[sqlite3.Connection]:
    return closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True))


# The measure.


@dataclass(frozen=True)
class Told:
    """An order telling the columns apart, with its records of the mock server's width."""

    order: Order
    records: tuple[StoredRecord, ...]

    def of(self, kind: str) -> list[StoredRecord]:
        return [r for r in self.records if r.fields[TOLD].strip() == kind]


@dataclass(frozen=True)
class Column:
    """A column's matches on the telling orders (never a value)."""

    quantity: int  # records equal to their order's quantity
    accepted: int  # acceptances equal to their order's quantity
    sums: int  # filled orders whose fills sum in it to what filled


def told_orders(layout: Layout) -> list[Told]:
    """The telling orders with a record of the mock server's width joined to them, in order."""
    group = layout.groups.get(WIDTH)
    by: dict[tuple[date, str], tuple[Order, list[StoredRecord]]] = {}
    for j in group.joined if group else ():
        if telling(j.order):
            by.setdefault((j.record.day, _key(j.order.number)), (j.order, []))[1].append(j.record)
    return [Told(o, tuple(rs)) for o, rs in by.values()]


def column_of(told: Sequence[Told], column: int) -> Column:
    quantity = accepted = sums = 0
    for t in told:
        q = t.order.quantity
        quantity += sum(1 for r in t.records if _int(r.fields[column]) == q)
        accepted += sum(1 for r in t.of(ACCEPTANCE) if _int(r.fields[column]) == q)
        if t.order.filled > 0:
            values = [_int(r.fields[column]) for r in t.of(FILL)]
            whole = [v for v in values if v is not None]
            if whole and len(whole) == len(values) and sum(whole) == t.order.filled:
                sums += 1
    return Column(quantity, accepted, sums)


@dataclass(frozen=True)
class Counts:
    """The telling orders' totals a column's matches are out of."""

    orders: int
    filled: int  # orders something filled of
    records: int
    accepted: int  # their acceptances (CNTG_YN 1)
    fills: int  # their fills (CNTG_YN 2)
    apart: int  # records whose columns 10 and 17 differ (as whole numbers, or one not a number)


def counts_of(told: Sequence[Told]) -> Counts:
    records = [r for t in told for r in t.records]
    return Counts(
        len(told),
        sum(1 for t in told if t.order.filled > 0),
        len(records),
        sum(len(t.of(ACCEPTANCE)) for t in told),
        sum(len(t.of(FILL)) for t in told),
        sum(1 for r in records if _int(r.fields[FILL_QTY]) != _int(r.fields[ORDER_QTY])),
    )


def _holds(fill: Column, order: Column, n: Counts) -> bool:
    """``order`` is the order's quantity on every record, and ``fill`` sums over every filled order's
    fills to what filled."""
    return order.quantity == n.records and fill.sums == n.filled > 0


def verdict(told: Sequence[Told]) -> str:
    """``TOO_FEW`` under ``APART`` orders, ``NOT_APART`` when no record's columns 10 and 17 differ,
    ``EXAMPLE`` when 17 is the order's quantity and 10 the fill's (``_holds``), ``REVERSED`` the other
    way round, else ``NEITHER``."""
    n = counts_of(told)
    if n.orders < APART:
        return TOO_FEW
    if not n.apart:
        return NOT_APART
    ten, seventeen = column_of(told, FILL_QTY), column_of(told, ORDER_QTY)
    if _holds(ten, seventeen, n):
        return EXAMPLE
    if _holds(seventeen, ten, n):
        return REVERSED
    return NEITHER


SAID = {
    EXAMPLE: "가려졌다, 예제대로다: 칸 10은 체결 수량(`CNTG_QTY`), 칸 17은 주문 수량(`ODER_QTY`)",
    REVERSED: "가려졌다, 예제와 반대다: 칸 10이 주문 수량, 칸 17이 체결 수량으로 맞는다",
    NEITHER: "두 칸이 다른 레코드는 있지만 어느 쪽 읽기도 모두 맞지는 않는다: 위 일치 수를 본다",
    NOT_APART: "가려지지 않았다: 두 칸이 다른 레코드가 없다",
    TOO_FEW: f"가르기에 모자란다: 가르는 주문이 {APART}건에 못 미친다",
}


# The disconnections.


@dataclass(frozen=True)
class Missing:
    orders: int  # every order of the recorded days
    unnoticed: int  # with no record joined
    during: int  # of those, placed while no connection was subscribed within the window
    unknown: int  # of those, without an order time
    placed_during: int  # every order placed then


def missing_of(gathered: Gathered) -> Missing:
    spans, _ = uncovered(gathered.layout.recorded.connections, gathered.calendar)
    joined = {(j.record.day, _key(j.order.number)) for g in gathered.layout.groups.values() for j in g.joined}
    orders = unnoticed = during = unknown = placed = 0
    for day, day_orders in gathered.orders.items():
        for o in day_orders:
            orders += 1
            when = _placed(day, o.at)
            gap = when is not None and any(a <= when < b for a, b in spans)
            placed += gap
            if (day, _key(o.number)) in joined:
                continue
            unnoticed += 1
            if when is None:
                unknown += 1
            elif gap:
                during += 1
    return Missing(orders, unnoticed, during, unknown, placed)


def _placed(day: date, hhmmss: str) -> datetime | None:
    text = hhmmss.strip()
    if len(text) != 6 or not text.isdigit():
        return None
    try:
        return datetime.combine(day, time(int(text[:2]), int(text[2:4]), int(text[4:])), KST)
    except ValueError:
        return None


# The output.


def render(gathered: Gathered) -> str:
    """The run's output: counts, column numbers, dates and times, never a value."""
    layout, night = gathered.layout, gathered.night
    rec = layout.recorded
    told = told_orders(layout)
    n = counts_of(told)
    days = rec.days
    lines = [f"{TITLE} · {night}({WEEKDAYS[night.weekday()]}) 밤", ""]
    lines.append(f"1. 기록(state/{DB_NAME}) · 주문(state/{TRADER_DB}, mock · KIS 주문 체결 조회)")
    lines.append(f"  - 거래일 {len(days)}일" + (f": {_span_of(days)}" if days else ""))
    lines.append(
        f"  - KIS 주문 체결 조회 {len(days)}일 중 {len(days) - len(layout.unread)}일 읽음(주문"
        f" {layout.kis_orders}건, 취소 {layout.kis_cancels}건) · 장부 {layout.ledger_orders}건"
    )
    if layout.unread:
        lines.append(
            "  - 조회를 읽지 못한 날(장부로만 묶었다): " + ", ".join(d.isoformat() for d in layout.unread)
        )
    widths = Counter(len(r.fields) for r in rec.records)
    lines.append(
        f"  - 레코드 {len(rec.records)}개("
        + (", ".join(f"{w}칸 {k}개" for w, k in sorted(widths.items())) or "없음")
        + f") · 모호 {layout.ambiguous}개, 묶이지 않음 {layout.unjoined}개"
    )
    every = [o for v in gathered.orders.values() for o in v]
    candidates = [o for o in every if telling(o)]
    kinds = Counter(t.order.kind for t in told)
    lines += ["", f"2. 칸 10과 17을 가르는 주문(수량이 1주보다 많고 체결이 있는 매수·매도, {WIDTH}칸 레코드)"]
    lines.append(
        f"  - 그날들의 주문 중 {len(candidates)}건(그중 일부만 체결된 것"
        f" {sum(1 for o in candidates if o.filled < o.quantity)}건) · 레코드가 묶인 것"
        f" {n.orders}건("
        + ", ".join(f"{name} {kinds[k]}건" for k, name in KINDS.items() if kinds[k])
        + f") · 체결된 것 {n.filled}건"
    )
    lines.append(
        f"  - 그 레코드 {n.records}개: 접수(칸 14가 1) {n.accepted}개, 체결(칸 14가 2) {n.fills}개 ·"
        f" 칸 10과 17이 다른 레코드 {n.apart}개"
    )
    lines.append(
        "  - 칸마다(맞는 것이 있는 칸과 칸 10, 17): `주문 수량 m/n` 레코드가 그 주문의 주문 수량과 같다,"
        " `접수` 접수 레코드만, `체결 합` 체결 레코드의 합이 그 주문의 체결 수량과 같은 주문"
    )
    for c in range(WIDTH):
        col = column_of(told, c)
        if c not in (FILL_QTY, ORDER_QTY) and not (col.quantity or col.accepted or col.sums):
            continue
        lines.append(
            f"    칸 {c + 1}(`{MOCK_FILL_NOTICE_COLUMNS[c]}`): 주문 수량 {col.quantity}/{n.records} ·"
            f" 접수 {col.accepted}/{n.accepted} · 체결 합 {col.sums}/{n.filled}"
        )
    lines.append(f"  - 판정: {SAID[verdict(told)]}")

    missing = missing_of(gathered)
    spans, _ = uncovered(rec.connections, gathered.calendar)
    total = sum((b - a for a, b in spans), timedelta())
    subscribed = sum(1 for c in rec.connections if c.subscribed_at is not None)
    ended = Counter(_why(c.why) for c in rec.connections if c.why)
    lines += ["", "3. 끊김과 빠진 통보"]
    lines.append(
        f"  - 연결 {len(rec.connections)}번(구독된 것 {subscribed}번)"
        + (" · 끝난 까닭: " + ", ".join(f"{w} {k}번" for w, k in ended.most_common()) if ended else "")
    )
    lines.append(f"  - 기록 창 안에서 구독되지 않은 때 {len(spans)}번, 모두 {_duration(total)}")
    lines.append(
        f"  - 그날들의 주문 {missing.orders}건(취소 포함) 중 묶인 레코드가 없는 것 {missing.unnoticed}건:"
        f" 구독되지 않은 때 낸 것 {missing.during}건, 시각을 모르는 것 {missing.unknown}건 ·"
        f" 구독되지 않은 때 낸 주문은 모두 {missing.placed_during}건"
    )
    lines += [
        "",
        "4. 에이전트 차례: 이 수로 사용자에게 체결 통보 기록기를 지울지 묻는다. 끊긴 때 빠진 통보가 0건이면"
        " 매시간 끊김 WARNING은 판단에 쓰이지 않았다(INFO로 낮출지 함께 묻는다)",
    ]
    return "\n".join(lines)


def _span_of(days: Sequence[date]) -> str:
    return days[0].isoformat() if len(days) == 1 else f"{days[0].isoformat()}–{days[-1].isoformat()}"


async def fill_notice_columns(settings: Settings, now: datetime, *, servers: Servers = MOCK_SERVERS) -> int:
    """``krx-quant fill-notice-columns``: the columns 10 and 17 and the missing notices, printed
    (``render``); its exit code (``DONE``, ``UNREADABLE``)."""
    gathered = await gather(settings, now, TITLE, servers=servers)
    if isinstance(gathered, str):
        print(gathered)
        return UNREADABLE
    print(render(gathered))
    return UNREADABLE if gathered.layout.unread else DONE
