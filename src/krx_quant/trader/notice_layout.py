"""``krx-quant fill-notice-layout`` (#484, #403): the mock server's fill notice records, kept by the
trader's recorder (``fill_notices.py``, ``state/fill_notices.sqlite3``), measured column by column
against the orders they tell of, never showing a value: the night job ``fill-notice-layout-403``'s,
for an agent to name the mock server's 23 columns (``kis_orders.FILL_NOTICE_COLUMNS`` has KIS's
example's 26, ``docs/kis-feeds.md`` "주문 (#403)"). Its run of 2026-11-04 named them
(``kis_orders.MOCK_FILL_NOTICE_COLUMNS``); the measure itself reads no name, by position alone.

**The orders.** For each trading day a record was kept on, that day's orders: the trader's ledger
(``state/trader.sqlite3``, mode ``mock``, read-only: each order with KIS's number, ``orders.broker_id``,
its side, limit, quantity and fills, ``fills``; each buy KIS took of which nothing filled,
``unfilled_buys``) and KIS's day order inquiry, read with the trader's own key alone
(``kis_orders.trader_broker``, as ``check-mock-trades`` reads it: every symbol, the cancels' rows
among them, each with its original order, ``orgn_odno``). An order is a buy (매수, a trade's), a
sell (매도), a cancel (취소, KIS's row alone: the ledger keeps no cancel's number), an unfilled buy
(미체결 매수) or outside the ledger (장부 밖). A day KIS's inquiry could not be read is measured on the
ledger alone, and the run ends ``UNREADABLE``.

**The join** (``candidates``). A record is joined to the order of its day whose number one of its
fields is (``kis_orders.same_number``: leading zeros aside; a field of three digits or more), only
when exactly one order is: a cancel whose own number and its order's are both in the record is that
cancel. A record of more is ambiguous (모호), of none unjoined (묶이지 않음); neither is measured.

**The measure.** The records are grouped by their width (the fields a record has). For each column:
its shape (``probe_fields.flag``), its lengths, how many values it took, and how many joined records
it equals of their order: its number, its original's (a cancel's), its side's code, its quantity, its
limit (a cancel's: its order's), its symbol, its time (HHMMSS within ``SECONDS`` of the order's time
on KIS, and of when the record came), the account (its 10 digits or the first 8) and the HTS ID. The
ledger keeps a fill when the trader saw it and at the inquiry's average, so the fills are matched by
order, not by record: a column's values over an order's records summed equal what filled of it
(``fill_sums``), over all its records or those of one value of a code column (a column of two to four
values of two characters at most: a fill notice tells an order's acceptance with its quantity too),
and the column that sums so best weighs a price column's values to the fills' average price within
``PRICE_TOLERANCE`` won (``fill_prices``).

**The output** holds shapes, counts, matches, dates and the window's times only: no value, no order
number, account, HTS ID or price, for it goes on the public issue (#403), masked again there
(``redaction.py``). Its exit code: ``DONE`` (0) once a buy, a sell and a cancel each had a record
joined to it, and the job runs no more; ``PARTIAL`` (3) before, and the job runs again on a trading
night with new records; ``UNREADABLE`` (4) when the records, the ledger or a day's inquiry could not be
read, and it runs again the next trading night. It writes nothing but the trader's KIS token cache.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from krx_quant import kis
from krx_quant.config import Settings, SettingsError
from krx_quant.feeds.base import KST
from krx_quant.kis_orders import MOCK as MOCK_SERVERS
from krx_quant.kis_orders import (
    SIDE_BUY,
    SIDE_SELL,
    BrokerError,
    OrderRow,
    Servers,
    same_number,
    trader_broker,
)
from krx_quant.market_calendar import MarketCalendar, calendar_store
from krx_quant.probe_fields import flag
from krx_quant.trader import MOCK, SYMBOL
from krx_quant.trader.fill_notices import (
    CAP,
    CLOSED_BY_KIS,
    DB_NAME,
    MALFORMED,
    NO_KEY,
    QUEUE,
    REFUSED,
    SILENT,
    SIZE,
    TR_ID,
    UNDECRYPTED,
    UNDIVIDED,
    WINDOW_ENDED,
    Recorded,
    StoredConnection,
    StoredRecord,
    read_recorded,
    recording_window,
)
from krx_quant.trader.report import WEEKDAYS, report_day
from krx_quant.trader.store import DB_NAME as TRADER_DB

log = logging.getLogger(__name__)

TITLE = "체결 통보 칸 재기 (#403, #484)"
DONE, PARTIAL, UNREADABLE = 0, 3, 4
BUY, SELL, CANCEL, UNFILLED, OUTSIDE = "buy", "sell", "cancel", "unfilled", "outside"
KINDS = {BUY: "매수", SELL: "매도", CANCEL: "취소", UNFILLED: "미체결 매수", OUTSIDE: "장부 밖"}
NEEDED = (BUY, SELL, CANCEL)  # each joined once: the job is done
SECONDS = 5  # a time field near the order's or the record's, within this
PRICE_TOLERANCE = 1.0  # won: a weighted price against the fills' average (KIS may round it)
_SIDES = {"buy": SIDE_BUY, "sell": SIDE_SELL}


@dataclass(frozen=True)
class Order:
    """An order of a day, as the join and the measure read it."""

    kind: str  # BUY, SELL, CANCEL, UNFILLED, OUTSIDE
    number: str
    original: str  # a cancel's order; "" for another
    side: str  # SIDE_BUY or SIDE_SELL
    symbol: str
    quantity: int
    price: int  # the limit; a cancel's: its order's; 0 unknown
    filled: int
    average: float  # the fills' average price; 0 without one
    at: str  # HHMMSS (KIS's order time, else the ledger's); "" unknown


@dataclass(frozen=True)
class Ledgered:
    """An order of the trader's ledger, by KIS's number."""

    kind: str  # BUY, SELL, UNFILLED
    number: str
    side: str
    price: int
    quantity: int
    filled: int
    average: float
    at: str  # HHMMSS, when the trader sent it


def _key(number: str) -> str:
    return number.strip().lstrip("0")


def number_like(value: str) -> bool:
    """A field that may be an order number: digits, three or more (a one- or two-digit code is not)."""
    text = value.strip()
    return len(text) > 2 and text.isdigit()


@contextmanager
def _reading(path: str) -> Iterator[sqlite3.Connection]:
    db = sqlite3.connect(path, uri=True)
    db.row_factory = sqlite3.Row
    try:
        yield db
    finally:
        db.close()


def _has(db: sqlite3.Connection, table: str) -> bool:
    found = db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()
    return found is not None


def ledger_of(db: sqlite3.Connection, day: date) -> dict[str, Ledgered]:
    """The mock orders of ``day`` the ledger holds with KIS's number, by that number (leading zeros
    aside): its trades' orders, and its buys of which nothing filled (``unfilled_buys``, open or
    cancelled: one that filled later is its trade's order)."""
    found: dict[str, Ledgered] = {}
    if _has(db, "orders"):
        rows = db.execute(
            "SELECT o.side, o.price, o.quantity, o.placed_at, o.broker_id,"
            " coalesce(sum(f.quantity), 0) AS filled, coalesce(sum(f.price * f.quantity), 0) AS amount"
            " FROM orders o LEFT JOIN fills f ON f.order_id = o.id"
            " WHERE o.mode = ? AND o.broker_id IS NOT NULL AND o.broker_id != ''"
            " AND substr(o.placed_at, 1, 10) = ? GROUP BY o.id",
            (MOCK, day.isoformat()),
        )
        for r in rows:
            filled = int(r["filled"])
            found[_key(str(r["broker_id"]))] = Ledgered(
                BUY if r["side"] == "buy" else SELL,
                str(r["broker_id"]),
                _SIDES.get(r["side"], ""),
                round(r["price"]),
                int(r["quantity"]),
                filled,
                float(r["amount"]) / filled if filled else 0.0,
                _hhmmss(r["placed_at"]),
            )
    if _has(db, "unfilled_buys"):
        rows = db.execute(
            "SELECT price, quantity, placed_at, broker_id FROM unfilled_buys WHERE mode = ?"
            " AND broker_id != '' AND status IN ('open', 'cancelled') AND substr(placed_at, 1, 10) = ?",
            (MOCK, day.isoformat()),
        )
        for r in rows:
            found.setdefault(
                _key(str(r["broker_id"])),
                Ledgered(
                    UNFILLED,
                    str(r["broker_id"]),
                    SIDE_BUY,
                    round(r["price"]),
                    int(r["quantity"]),
                    0,
                    0.0,
                    _hhmmss(r["placed_at"]),
                ),
            )
    return found


def _hhmmss(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone(KST).strftime("%H%M%S")
    except ValueError:
        return ""


def orders_of(ledger: Mapping[str, Ledgered], rows: Sequence[OrderRow] | None) -> list[Order]:
    """A day's orders: KIS's rows (``rows``; None when the inquiry was not read), each of the ledger's
    kind, and the ledger's orders KIS did not show."""
    found: list[Order] = []
    seen: set[str] = set()
    limits = {_key(r.number): r.price for r in rows or () if not r.cancel}
    for r in rows or ():
        seen.add(_key(r.number))
        if r.cancel:
            price = limits.get(_key(r.original), 0)
            found.append(
                Order(CANCEL, r.number, r.original, r.side, r.symbol, r.quantity, price, 0, 0.0, r.at)
            )
            continue
        ledgered = ledger.get(_key(r.number))
        kind = ledgered.kind if ledgered is not None else OUTSIDE
        found.append(
            Order(kind, r.number, "", r.side, r.symbol, r.quantity, r.price, r.filled, r.average, r.at)
        )
    for number, o in ledger.items():
        if number not in seen:
            found.append(
                Order(o.kind, o.number, "", o.side, SYMBOL, o.quantity, o.price, o.filled, o.average, o.at)
            )
    return found


def candidates(fields: Sequence[str], orders: Sequence[Order]) -> list[Order]:
    """The orders a record may tell of: those whose number one of its fields is, but an order whose
    cancel's number is there too (the record is the cancel's)."""
    numbers = [v for v in fields if number_like(v)]
    hits = [o for o in orders if any(same_number(v, o.number) for v in numbers)]
    explained = {_key(o.original) for o in hits if o.kind == CANCEL and o.original}
    return [o for o in hits if _key(o.number) not in explained]


# The measure.


@dataclass(frozen=True)
class Joined:
    record: StoredRecord
    order: Order


@dataclass(frozen=True)
class Known:
    """What a field may equal beside its order: the account and the HTS ID (never shown)."""

    accounts: frozenset[str]  # the account's 10 digits and its first 8
    hts_id: str


def _int(value: str) -> int | None:
    number = _number(value)
    return int(number) if number is not None and number.is_integer() else None


def _number(value: str) -> float | None:
    text = value.strip()
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return number if number == number and abs(number) != float("inf") else None


def _seconds(hhmmss: str) -> int | None:
    text = hhmmss.strip()
    if len(text) != 6 or not text.isdigit():
        return None
    h, m, s = int(text[:2]), int(text[2:4]), int(text[4:])
    return h * 3600 + m * 60 + s if h < 24 and m < 60 and s < 60 else None


def _near(value: str, hhmmss: str) -> bool:
    a, b = _seconds(value), _seconds(hhmmss)
    return a is not None and b is not None and abs(a - b) <= SECONDS


Comparison = tuple[str, Callable[[Order], bool], Callable[[str, StoredRecord, Order, Known], bool]]

COMPARISONS: tuple[Comparison, ...] = (
    ("주문번호", lambda o: True, lambda v, r, o, k: number_like(v) and same_number(v, o.number)),
    (
        "원주문번호",
        lambda o: bool(o.original),
        lambda v, r, o, k: number_like(v) and same_number(v, o.original),
    ),
    ("매수·매도 코드", lambda o: bool(o.side), lambda v, r, o, k: v.strip() == o.side),
    ("주문 수량", lambda o: o.quantity > 0, lambda v, r, o, k: _int(v) == o.quantity),
    ("주문가", lambda o: o.price > 0, lambda v, r, o, k: _int(v) == o.price),
    ("종목", lambda o: bool(o.symbol), lambda v, r, o, k: v.strip() == o.symbol),
    (f"주문 시각({SECONDS}초 안)", lambda o: bool(o.at), lambda v, r, o, k: _near(v, o.at)),
    (
        f"받은 시각({SECONDS}초 안)",
        lambda o: True,
        lambda v, r, o, k: _near(v, r.received_at.astimezone(KST).strftime("%H%M%S")),
    ),
    ("계좌", lambda o: True, lambda v, r, o, k: "".join(c for c in v if c.isdigit()) in k.accounts),
    ("HTS ID", lambda o: True, lambda v, r, o, k: bool(k.hts_id) and v.strip() == k.hts_id),
)

Pick = Callable[[StoredRecord], bool]


@dataclass(frozen=True)
class Subset:
    """Which of an order's records a fill check sums: all (``column`` None), or those whose code
    column ``column`` holds one value (never shown)."""

    column: int | None = None
    value: str = ""

    def __call__(self, record: StoredRecord) -> bool:
        return self.column is None or record.fields[self.column].strip() == self.value

    @property
    def said(self) -> str:
        return "레코드 모두" if self.column is None else f"칸 {self.column + 1}로 고른 레코드"


def subsets(joined: Sequence[Joined], width: int) -> list[Subset]:
    """All the records, then each value of each code column: one of two to four values over the joined
    records, none longer than two characters."""
    found = [Subset()]
    for column in range(width):
        values = {j.record.fields[column].strip() for j in joined}
        if 2 <= len(values) <= 4 and all(len(v) <= 2 for v in values):
            found += [Subset(column, v) for v in sorted(values)]
    return found


def filled_orders(joined: Sequence[Joined]) -> list[tuple[Order, list[StoredRecord]]]:
    """The joined orders something filled of, each with its records."""
    by: dict[tuple[date, str], tuple[Order, list[StoredRecord]]] = {}
    for j in joined:
        if j.order.filled > 0:
            by.setdefault((j.record.day, _key(j.order.number)), (j.order, []))[1].append(j.record)
    return list(by.values())


def fill_sum(column: int, subset: Subset, orders: Sequence[tuple[Order, list[StoredRecord]]]) -> int:
    """The orders whose ``subset`` of records sum, in ``column``, to what filled of them."""
    n = 0
    for order, records in orders:
        values = [_int(r.fields[column]) for r in records if subset(r)]
        whole = [v for v in values if v is not None]
        if whole and sum(whole) == order.filled:
            n += 1
    return n


def fill_sums(
    width: int, subs: Sequence[Subset], orders: Sequence[tuple[Order, list[StoredRecord]]]
) -> dict[int, tuple[int, Subset]]:
    """Each column's best fill sum (``fill_sum``): the most orders, all the records first on a tie."""
    best: dict[int, tuple[int, Subset]] = {}
    for column in range(width):
        for subset in subs:
            if subset.column == column:
                continue
            n = fill_sum(column, subset, orders)
            if n > best.get(column, (0, subset))[0]:
                best[column] = (n, subset)
    return best


def fill_prices(
    width: int,
    quantity: int,
    subset: Subset,
    orders: Sequence[tuple[Order, list[StoredRecord]]],
) -> dict[int, int]:
    """For each column but ``quantity``, the orders whose ``subset`` of records, weighed by their
    ``quantity`` column, average in it to the fills' average price (``PRICE_TOLERANCE``)."""
    found: dict[int, int] = {}
    for column in range(width):
        if column == quantity:
            continue
        n = 0
        for order, records in orders:
            pairs = [(_int(r.fields[quantity]), _number(r.fields[column])) for r in records if subset(r)]
            weighed = [(q, p) for q, p in pairs if q is not None and q > 0 and p is not None]
            total = sum(q for q, _ in weighed)
            if total and abs(sum(q * p for q, p in weighed) / total - order.average) < PRICE_TOLERANCE:
                n += 1
        if n:
            found[column] = n
    return found


def weighed(
    width: int,
    subs: Sequence[Subset],
    orders: Sequence[tuple[Order, list[StoredRecord]]],
    sums: Mapping[int, tuple[int, Subset]],
) -> tuple[int | None, dict[int, int]]:
    """The quantity column the fill prices are weighed by, and each price column's matches
    (``fill_prices``): of the columns and records that sum to the fills for the most orders, those
    that weigh some column to the fills' average for the most (the acceptance of an order filled
    whole tells its quantity too, but not the fill's price); the lowest columns on a tie."""
    most = max((n for n, _ in sums.values()), default=0)
    if not most:
        return None, {}
    best: tuple[int, int | None, dict[int, int]] = (-1, None, {})
    for quantity in sorted(sums):
        for subset in subs:
            if subset.column == quantity or fill_sum(quantity, subset, orders) != most:
                continue
            prices = fill_prices(width, quantity, subset, orders)
            top = max(prices.values(), default=0)
            if top > best[0]:
                best = (top, quantity, prices)
    return best[1], best[2]


@dataclass
class Group:
    """The records of one width: all of them (their shapes), the joined (their matches)."""

    width: int
    records: list[StoredRecord] = field(default_factory=list)
    joined: list[Joined] = field(default_factory=list)


def describe_group(group: Group, known: Known) -> list[str]:
    """A group's columns, each by its shape and its matches, never a value."""
    width, joined = group.width, group.joined
    orders = filled_orders(joined)
    subs = subsets(joined, width)
    sums = fill_sums(width, subs, orders)
    weighing, prices = weighed(width, subs, orders, sums)
    lines = [
        f"  - {width}칸 레코드 {len(group.records)}개(묶인 것 {len(joined)}개, 체결된 주문"
        f" {len(orders)}건). `이름 m/n`: 견준 n개 중 m개가 같다(나머지 n−m개는 어긋난다)"
    ]
    for column in range(width):
        values = [r.fields[column].strip() for r in group.records]
        lengths = sorted({len(v) for v in values if v})
        parts = [flag("", values)]
        if lengths:
            parts.append(f"{lengths[0]}자" if len(lengths) == 1 else f"{lengths[0]}–{lengths[-1]}자")
        parts.append(f"값 {len(set(values))}가지")
        for name, applies, match in COMPARISONS:
            compared = [j for j in joined if applies(j.order)]
            n = sum(1 for j in compared if match(j.record.fields[column], j.record, j.order, known))
            if n:
                parts.append(f"{name} {n}/{len(compared)}")
        if column in sums:
            n, subset = sums[column]
            parts.append(f"체결 수량 합 {n}/{len(orders)}({subset.said})")
        if column in prices and weighing is not None:
            parts.append(f"체결가(칸 {weighing + 1}의 수량으로 가중 평균) {prices[column]}/{len(orders)}")
        lines.append(f"    칸 {column + 1}: {' · '.join(parts)}")
    return lines


# The run.


@dataclass
class Layout:
    """What a run measured."""

    recorded: Recorded
    known: Known
    unread: list[date]  # the days KIS's inquiry was not read
    why_unread: str | None
    ledger_orders: int
    kis_orders: int
    kis_cancels: int
    groups: dict[int, Group]
    kinds: dict[str, set[tuple[date, str]]]  # the orders joined, by kind
    kind_records: Counter[str]
    ambiguous: int
    unjoined: int
    gap: timedelta  # the time within the windows no connection was subscribed
    windows: list[tuple[datetime, datetime]]
    other_tr: int  # records of another TR ID, not measured

    @property
    def missing(self) -> list[str]:
        """The kinds no record was joined to yet, of those the job waits for (``NEEDED``)."""
        return [k for k in NEEDED if not self.kinds.get(k)]

    @property
    def code(self) -> int:
        if not self.missing:
            return DONE
        return UNREADABLE if self.unread else PARTIAL


def measure(
    recorded: Recorded,
    orders_by_day: Mapping[date, Sequence[Order]],
    known: Known,
    calendar: MarketCalendar,
    *,
    unread: Sequence[date] = (),
    why_unread: str | None = None,
    ledger_orders: int = 0,
    kis_orders: int = 0,
    kis_cancels: int = 0,
) -> Layout:
    """The records joined to ``orders_by_day`` (``candidates``) and grouped by width."""
    groups: dict[int, Group] = {}
    kinds: dict[str, set[tuple[date, str]]] = {}
    kind_records: Counter[str] = Counter()
    ambiguous = unjoined = other = 0
    for record in recorded.records:
        if record.tr_id != TR_ID:
            other += 1
            continue
        group = groups.setdefault(len(record.fields), Group(len(record.fields)))
        group.records.append(record)
        found = candidates(record.fields, orders_by_day.get(record.day, ()))
        if len(found) > 1:
            ambiguous += 1
        elif not found:
            unjoined += 1
        else:
            order = found[0]
            group.joined.append(Joined(record, order))
            kinds.setdefault(order.kind, set()).add((record.day, _key(order.number)))
            kind_records[order.kind] += 1
    gap, windows = gaps(recorded.connections, calendar)
    return Layout(
        recorded,
        known,
        list(unread),
        why_unread,
        ledger_orders,
        kis_orders,
        kis_cancels,
        dict(sorted(groups.items())),
        kinds,
        kind_records,
        ambiguous,
        unjoined,
        gap,
        windows,
        other,
    )


def gaps(
    connections: Sequence[StoredConnection], calendar: MarketCalendar
) -> tuple[timedelta, list[tuple[datetime, datetime]]]:
    """The time within each day's recording window (of the days a connection was asked for on) that
    no connection was subscribed, and the windows. A connection that has no end kept (the trader was
    stopped hard) counts to when it was last heard from."""
    total = timedelta()
    windows = []
    for day in sorted({c.day for c in connections}):
        session = calendar.session(day)
        if session is None:
            continue
        start, end = recording_window(session)
        windows.append((start, end))
        covered: list[tuple[datetime, datetime]] = []
        for c in connections:
            if c.day != day or c.subscribed_at is None:
                continue
            until = c.ended_at or c.heard_at or c.subscribed_at
            a, b = max(c.subscribed_at, start), min(until, end)
            if a < b:
                covered.append((a, b))
        seen, at = timedelta(), start
        for a, b in sorted(covered):
            if b > at:
                seen += b - max(a, at)
                at = b
        total += (end - start) - seen
    return total, windows


ODD = {
    UNDIVIDED: "칸 수가 레코드 수로 나누어떨어지지 않는 것",
    NO_KEY: "key·iv가 없어 풀지 못한 것",
    UNDECRYPTED: "key·iv로 풀리지 않은 것",
    MALFORMED: "프레임의 모양이 아닌 것",
}


def render(layout: Layout, night: date) -> str:
    """The run's output: shapes, counts, matches and dates, never a value."""
    rec = layout.recorded
    days = rec.days
    lines = [f"{TITLE} · {night}({WEEKDAYS[night.weekday()]}) 밤", ""]
    lines.append(f"1. 기록(state/{DB_NAME}: mock의 trader가 받은 체결 통보)")
    lines.append(
        f"  - 거래일 {len(days)}일" + (f": {', '.join(d.isoformat() for d in days)}" if days else "")
    )
    spans = sorted({(_hm(a), _hm(b)) for a, b in layout.windows})
    subscribed = sum(1 for c in rec.connections if c.subscribed_at is not None)
    lines.append(
        f"  - 연결 {len(rec.connections)}번, 그중 구독된 것 {subscribed}번. 기록 창"
        f"({', '.join(f'{a}–{b}' for a, b in spans) or '없음'}) 안에서 구독되지 않은 시간"
        f" {_duration(layout.gap)}"
    )
    ended = Counter(_why(c.why) for c in rec.connections if c.why)
    if ended:
        lines.append("  - 연결이 끝난 까닭: " + ", ".join(f"{w} {n}번" for w, n in ended.most_common()))
    frames = {(r.connection, r.frame) for r in rec.records}
    odd = Counter(o.why for o in rec.odd)
    lines.append(
        f"  - 프레임 {len(frames) + len(rec.odd)}개: 레코드로 나뉜 것 {len(frames)}개"
        + "".join(f", {ODD.get(w, w)} {n}개" for w, n in sorted(odd.items()))
    )
    undivided = Counter(
        (o.count, len(o.fields)) for o in rec.odd if o.why == UNDIVIDED and o.fields is not None
    )
    if undivided:
        lines.append(
            "    나누어떨어지지 않는 프레임(머리의 레코드 수 · 칸 수): "
            + ", ".join(f"{c} · {n}칸 {k}개" for (c, n), k in sorted(undivided.items()))
        )
    widths = Counter(len(r.fields) for r in rec.records if r.tr_id == TR_ID)
    heads = Counter(r.count for r in rec.records if r.position == 0)
    lines.append(
        f"  - 레코드 {sum(widths.values())}개: "
        + (", ".join(f"{w}칸 {n}개" for w, n in sorted(widths.items())) or "없음")
        + (
            " · 프레임 머리의 레코드 수: " + ", ".join(f"{c}개 {n}프레임" for c, n in sorted(heads.items()))
            if heads
            else ""
        )
    )
    if layout.other_tr:
        lines.append(f"  - 다른 TR ID의 레코드 {layout.other_tr}개는 재지 않았다")
    frames_dropped: Counter[str] = Counter()
    records_dropped: Counter[str] = Counter()
    for d in rec.drops:
        frames_dropped[d.what] += d.frames
        records_dropped[d.what] += d.records
    lines.append(
        f"  - 버린 프레임 {sum(frames_dropped.values())}개, 레코드 {sum(records_dropped.values())}개"
        f"(대기열 {frames_dropped[QUEUE]}, 큰 프레임 {frames_dropped[SIZE]}, 하루 상한 {frames_dropped[CAP]})"
    )
    lines += ["", "2. 주문에 묶기(그날의 주문 하나에만 맞는 레코드만 묶는다)"]
    lines.append(
        f"  - 주문: 장부(state/{TRADER_DB}, mock) {layout.ledger_orders}건, KIS 주문 체결 조회"
        f" {len(days)}일 중 {len(days) - len(layout.unread)}일 읽음(주문 {layout.kis_orders}건,"
        f" 취소 {layout.kis_cancels}건)"
    )
    if layout.unread:
        lines.append(
            "  - 조회를 읽지 못한 날(장부로만 묶었다): "
            + ", ".join(d.isoformat() for d in layout.unread)
            + (f" ({layout.why_unread})" if layout.why_unread else "")
        )
    lines.append(
        f"  - 묶인 레코드 {sum(layout.kind_records.values())}개: "
        + ", ".join(
            f"{name} {layout.kind_records[k]}개(주문 {len(layout.kinds.get(k, ()))}건)"
            for k, name in KINDS.items()
        )
    )
    lines.append(f"  - 모호(주문 둘 이상에 맞음) {layout.ambiguous}개, 묶이지 않음 {layout.unjoined}개")
    lines += ["", "3. 칸마다(값, 주문번호, 계좌, HTS ID, 가격은 보이지 않는다)"]
    if not layout.groups:
        lines.append("  - 레코드가 없다")
    for group in layout.groups.values():
        lines += describe_group(group, layout.known)
    lines += ["", "4. 결과"]
    if not layout.missing:
        lines.append(
            "  - 묶인 매수, 매도, 취소가 모두 있다: 이 작업은 다시 돌지 않는다. 에이전트가 이것으로"
            " 칸에 이름을 붙인다"
        )
    else:
        again = (
            "조회를 읽지 못한 날이 있어 다음 거래일 밤에 다시 돈다"
            if layout.unread
            else "새 레코드가 있는 다음 거래일 밤에 다시 돈다"
        )
        lines.append(f"  - 아직 묶이지 않은 종류: {', '.join(KINDS[k] for k in layout.missing)}. {again}")
    return "\n".join(lines)


def _hm(at: datetime) -> str:
    return at.astimezone(KST).strftime("%H:%M")


ENDED = {
    WINDOW_ENDED: "기록 창이 끝남",
    CLOSED_BY_KIS: "KIS가 닫음",
    SILENT: "프레임이 오지 않음",
    REFUSED: "KIS가 구독을 거절",
}


def _why(text: str | None) -> str:
    """Why a connection ended, without its error's text (it may name a host): the recorder's own
    words (with KIS's code of a refused subscription), else the error's kind."""
    text = (text or "").strip()
    for words, said in ENDED.items():
        if text.startswith(words):
            return said + (text[len(words) :].strip() if words == REFUSED else "")
    return text.split(":", 1)[0].strip() or "까닭 없음"


def _duration(span: timedelta) -> str:
    seconds = round(span.total_seconds())
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return (
        " ".join(p for p in (f"{h}시간" if h else "", f"{m}분" if m else "", f"{s}초") if p)
        if seconds
        else "0초"
    )


def known_of(settings: Settings) -> Known:
    digits = "".join(c for c in settings.trader_kis_account or "" if c.isdigit())
    accounts = frozenset(a for a in (digits, digits[:8]) if len(a) >= 8)
    return Known(accounts, (settings.trader_kis_hts_id or "").strip())


async def fill_notice_layout(settings: Settings, now: datetime, *, servers: Servers = MOCK_SERVERS) -> int:
    """``krx-quant fill-notice-layout``: the records measured and printed (``render``); its exit code
    (``DONE``, ``PARTIAL``, ``UNREADABLE``)."""
    calendar = calendar_store(settings).calendar
    night = report_day(calendar, now)
    heading = f"{TITLE} · {night}({WEEKDAYS[night.weekday()]}) 밤"
    try:
        recorded = read_recorded(settings.state_dir)
    except sqlite3.Error as e:
        print(f"{heading}\n결과: 기록(state/{DB_NAME})을 읽지 못했다: {e}")
        return UNREADABLE
    if recorded is None:
        print(f"{heading}\n결과: 기록(state/{DB_NAME})이 없다. 다음 거래일 밤에 다시 돈다")
        return UNREADABLE
    days = recorded.days
    ledgers: dict[date, dict[str, Ledgered]] = {}
    path = settings.state_dir / TRADER_DB
    try:
        if path.exists():
            with _reading(path.resolve().as_uri() + "?mode=ro") as db:
                ledgers = {d: ledger_of(db, d) for d in days}
    except sqlite3.Error as e:
        print(f"{heading}\n결과: 장부(state/{TRADER_DB})를 읽지 못했다: {e}")
        return UNREADABLE
    rows: dict[date, list[OrderRow]] = {}
    unread: list[date] = []
    why: str | None = None
    try:
        async with kis.rest_session() as session:
            broker = trader_broker(session, settings, lambda: datetime.now(KST), servers=servers)
            for d in days:
                try:
                    rows[d] = await broker.orders(d)
                except BrokerError as e:
                    unread.append(d)
                    why = str(e)
    except SettingsError as e:
        unread, why = list(days), str(e)
    orders = {d: orders_of(ledgers.get(d, {}), rows.get(d)) for d in days}
    layout = measure(
        recorded,
        orders,
        known_of(settings),
        calendar,
        unread=unread,
        why_unread=why,
        ledger_orders=sum(len(v) for v in ledgers.values()),
        kis_orders=sum(1 for v in rows.values() for r in v if not r.cancel),
        kis_cancels=sum(1 for v in rows.values() for r in v if r.cancel),
    )
    print(render(layout, night))
    return layout.code
