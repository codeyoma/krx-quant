"""The mock day's trades against KIS (#405, its third acceptance criterion): the champion's orders of
one trading day in the trader's ledger (``state/trader.sqlite3``, mode ``mock``), each against its
row on KIS's day order inquiry (``kis_orders.Broker.orders``, the trader's own key), matched by
KIS's order number: its side, quantity, what filled and the fills' average price.

``krx-quant check-mock-trades`` prints it, the scheduled job ``check-mock-trades-405``'s, run on the
night of the first trading day the trader ran in mock mode (``mock_days``), and again each such
night until a day had orders to compare. It writes nothing but the trader's KIS token cache. The
output holds counts, quantities, sides, order times and price differences in ticks: never an order
number, an account number, a key or a price.

KIS's orders the ledger does not hold are listed. One placed while the order probe (#403,
``probe-kis-orders-403``) ran that day on the same mock account, by the scheduler's state
(``state/scheduled.json``), is the probe's and does not count against the ledger.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

from krx_quant import kis
from krx_quant.config import Settings
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
from krx_quant.market_calendar import calendar_store
from krx_quant.pricing import tick_size
from krx_quant.trader import MOCK, SYMBOL
from krx_quant.trader.promotion import mode_at
from krx_quant.trader.report import WEEKDAYS, report_day
from krx_quant.trader.store import DB_NAME, champion_of

log = logging.getLogger(__name__)

# The exit codes: the ledger's orders all as KIS has them (and KIS no other), something differs, no
# order of the day to compare (the job runs again the next mock trading night), the ledger or KIS
# could not be read (again too).
SAME, NO_ORDERS, UNREADABLE, DIFFERENT = 0, 3, 4, 5
# The order probe's job (#403), whose orders on the same mock account are its own.
PROBE_JOB = "probe-kis-orders-403"
PROBE_LIMIT = timedelta(minutes=15)  # its time limit: its window when the state has no duration
SIDES = {"buy": SIDE_BUY, "sell": SIDE_SELL}
SIDE_NAMES = {SIDE_BUY: "매수", SIDE_SELL: "매도"}
PURPOSES = {
    "entry": "진입",
    "take_profit": "익절",
    "stop_loss": "손절",
    "time": "시간",
    "pause": "정지 매도",
    "daily_limit": "하루 한도 매도",
    "cumulative_limit": "누적 한도 매도",
}


@dataclass(frozen=True)
class LedgerOrder:
    """An order of the champion's mock trades, as the ledger keeps it."""

    trade: int | None  # None: a buy of which nothing filled, which opened no trade
    side: str  # SIDE_BUY or SIDE_SELL
    purpose: str
    quantity: int
    filled: int
    average: float  # the fills kept, on average; 0 without one
    number: str  # KIS's order number
    placed_at: datetime


@dataclass(frozen=True)
class Compared:
    """A ledger order and its KIS row."""

    order: LedgerOrder
    row: OrderRow

    @property
    def ticks(self) -> float:
        """The ledger's fill price less KIS's, in ticks; 0 without a fill on either side."""
        if not self.order.filled or not self.row.filled:
            return 0.0
        return (self.order.average - self.row.average) / tick_size(self.row.average)

    @property
    def same(self) -> bool:
        return (
            self.order.side == self.row.side
            and self.order.quantity == self.row.quantity
            and self.order.filled == self.row.filled
            and abs(self.ticks) < 0.01
        )


@contextmanager
def _reading(path: Path) -> Iterator[sqlite3.Connection | None]:
    if not path.exists():
        yield None
        return
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        yield db
    finally:
        db.close()


def mock_days(state_dir: Path) -> tuple[date, ...]:
    """The trading days the trader ran in mock mode: those with a mock ledger (``days``, which it
    writes on a trading day while it runs). None without the store."""
    try:
        with _reading(state_dir / DB_NAME) as db:
            if db is None or not _has(db, "days"):
                return ()
            rows = db.execute("SELECT day FROM days WHERE mode = ? ORDER BY day", (MOCK,))
            return tuple(date.fromisoformat(r[0]) for r in rows)
    except sqlite3.Error as e:
        log.warning("cannot read the trader's mock days (%s)", e)
        return ()


def trader_in_mock(state_dir: Path) -> bool:
    """Whether the trader last started in mock mode (its store's ``runs``): it may then hold 005930
    and order on the mock account in the session (#405). False without the store."""
    try:
        with _reading(state_dir / DB_NAME) as db:
            return db is not None and mode_at(db) == MOCK
    except sqlite3.Error as e:
        log.warning("cannot read the trader's mode (%s): taken as mock", e)
        return True


def ledger_orders(db: sqlite3.Connection, day: date, mode: str = MOCK) -> list[LedgerOrder]:
    """The champion's ``mode`` orders (mock; live, #408) placed on ``day`` with a KIS order, oldest
    first: its trades' orders, and its buys of which nothing filled (``unfilled_buys``: no trade,
    ``trade`` None)."""
    found: list[LedgerOrder] = []
    if _has(db, "orders"):
        rows = db.execute(
            "SELECT o.id, o.trade_id, o.side, o.purpose, o.quantity, o.placed_at, o.broker_id,"
            " coalesce(sum(f.quantity), 0) AS filled, coalesce(sum(f.price * f.quantity), 0) AS amount"
            " FROM orders o JOIN trades t ON t.id = o.trade_id LEFT JOIN fills f ON f.order_id = o.id"
            " WHERE o.mode = ? AND o.broker_id IS NOT NULL AND substr(o.placed_at, 1, 10) = ?"
            f" AND {champion_of(db)}"
            " GROUP BY o.id ORDER BY o.placed_at, o.id",
            (mode, day.isoformat()),
        )
        found += [
            LedgerOrder(
                trade=r["trade_id"],
                side=SIDES[r["side"]],
                purpose=r["purpose"],
                quantity=r["quantity"],
                filled=int(r["filled"]),
                average=float(r["amount"]) / r["filled"] if r["filled"] else 0.0,
                number=str(r["broker_id"]),
                placed_at=datetime.fromisoformat(r["placed_at"]),
            )
            for r in rows
        ]
    if _has(db, "unfilled_buys"):
        rows = db.execute(
            # One that filled later (its cancel had failed) is its trade's order now (#405). One sent
            # whose answer was not had, refused, not sent or not on KIS is no order KIS has (#406).
            "SELECT quantity, placed_at, broker_id FROM unfilled_buys"
            " WHERE mode = ? AND substr(placed_at, 1, 10) = ? AND status IN ('open', 'cancelled')"
            " AND broker_id != ''",
            (mode, day.isoformat()),
        )
        found += [
            LedgerOrder(
                None,
                SIDE_BUY,
                "entry",
                r["quantity"],
                0,
                0.0,
                str(r["broker_id"]),
                datetime.fromisoformat(r["placed_at"]),
            )
            for r in rows
        ]
    return sorted(found, key=lambda o: o.placed_at)


def failures(db: sqlite3.Connection, day: date, mode: str = MOCK) -> list[sqlite3.Row]:
    if not _has(db, "failures"):
        return []
    return db.execute(
        "SELECT at, what, outcome, code FROM failures WHERE mode = ? AND day = ? ORDER BY id",
        (mode, day.isoformat()),
    ).fetchall()


def probe_window(state_dir: Path, day: date) -> tuple[time, time] | None:
    """When the order probe (#403) ran on ``day``, by the scheduler's state; None if it did not.
    Read-only: an unreadable state is no window."""
    try:
        data = json.loads((state_dir / "scheduled.json").read_text())
        record = data["jobs"][PROBE_JOB]
        if date.fromisoformat(record["day"]) != day:
            return None
        start = datetime.fromisoformat(record["started_at"]).astimezone(KST)
        seconds = record.get("seconds")
        end = start + (PROBE_LIMIT if seconds is None else timedelta(seconds=float(seconds)))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return (start - timedelta(seconds=5)).time(), (end + timedelta(seconds=5)).time()


def _at(row: OrderRow) -> time | None:
    try:
        return datetime.strptime(row.at, "%H%M%S").time()
    except ValueError:
        return None


@dataclass(frozen=True)
class Check:
    day: date
    ledger: list[LedgerOrder]
    rows: list[OrderRow]  # KIS's, the cancels' rows among them
    probe: tuple[time, time] | None
    failed: int  # the ledger's failed orders that day

    @property
    def compared(self) -> list[Compared]:
        orders = [r for r in self.rows if not r.cancel]
        found = []
        for o in self.ledger:
            row = next((r for r in orders if same_number(r.number, o.number)), None)
            if row is not None:
                found.append(Compared(o, row))
        return found

    @property
    def missing(self) -> list[LedgerOrder]:
        """The ledger's orders KIS does not show."""
        orders = [r for r in self.rows if not r.cancel]
        return [o for o in self.ledger if not any(same_number(r.number, o.number) for r in orders)]

    def extra(self) -> tuple[list[OrderRow], list[OrderRow]]:
        """KIS's orders the ledger does not hold: the probe's (placed while it ran), and the others."""
        probe, others = [], []
        for r in self.rows:
            if r.cancel or any(same_number(r.number, o.number) for o in self.ledger):
                continue
            at = _at(r)
            if self.probe is not None and at is not None and self.probe[0] <= at <= self.probe[1]:
                probe.append(r)
            else:
                others.append(r)
        return probe, others

    @property
    def code(self) -> int:
        _, others = self.extra()
        if not self.ledger and not others:
            return NO_ORDERS
        if self.missing or others or not all(c.same for c in self.compared):
            return DIFFERENT
        return SAME


def render(check: Check) -> str:
    day = check.day
    lines = [f"KIS 모의 매매 대조 (#405) · {day}({WEEKDAYS[day.weekday()]}) · {SYMBOL}"]
    ledger = check.ledger
    trades = len({o.trade for o in ledger if o.trade is not None})
    lines.append(
        f"- 장부(state/trader.sqlite3, mock, 챔피언의 매매): 매매 {trades}건, 주문 {len(ledger)}건"
        f"({_sides(ledger)}), 체결 {_filled(ledger)}"
    )
    orders = [r for r in check.rows if not r.cancel]
    cancels = len(check.rows) - len(orders)
    lines.append(
        f"- KIS 주문 체결 조회: 주문 {len(orders)}건({_sides(orders)}), 취소 {cancels}건,"
        f" 체결 {_filled(orders)}"
    )
    lines.append(f"- 장부의 주문 실패: {check.failed}건")
    lines += ["", "주문마다(장부 ↔ KIS. 주문번호로 맞췄고, 번호는 보이지 않는다)"]
    compared = check.compared
    if not compared:
        lines.append("- 없음")
    for i, c in enumerate(compared, 1):
        o, r = c.order, c.row
        lines.append(
            f"{i}. {SIDE_NAMES.get(o.side, o.side)}({PURPOSES.get(o.purpose, o.purpose)})"
            f" {o.placed_at.astimezone(KST):%H:%M:%S} · 주문 {o.quantity}주 ↔ {r.quantity}주"
            f" · 체결 {o.filled}주 ↔ {r.filled}주 · 체결가 차이 {c.ticks:+.2f}틱"
            + ("" if o.side == r.side else f" · 매수·매도가 다르다(KIS {SIDE_NAMES.get(r.side, r.side)})")
            + f": {'같다' if c.same else '다르다'}"
        )
    lines += ["", "KIS에 없는 장부의 주문"]
    lines += [
        f"- {SIDE_NAMES.get(o.side, o.side)}({PURPOSES.get(o.purpose, o.purpose)})"
        f" {o.placed_at.astimezone(KST):%H:%M:%S} {o.quantity}주, 체결 {o.filled}주"
        for o in check.missing
    ] or ["- 없음"]
    probe, others = check.extra()
    lines += ["", "장부에 없는 KIS의 주문"]
    lines += [f"- {_row(r)}" for r in others] or ["- 없음"]
    if probe:
        lines.append(
            f"- 주문 프로브(#403, {PROBE_JOB})가 돈 동안의 주문 {len(probe)}건은 프로브의 것이라"
            " 세지 않는다: " + ", ".join(_row(r) for r in probe)
        )
    code = check.code
    verdict = {
        SAME: f"같다: 장부의 주문 {len(ledger)}건이 모두 KIS 주문 체결 조회와 같다",
        NO_ORDERS: (
            "견줄 주문이 없다: 그날 장부에도 KIS에도 trader의 주문이 없다. 다음 mock 거래일 밤에 다시 돈다"
        ),
        DIFFERENT: "다르다: 위에서 '다르다'인 주문, KIS에 없는 장부의 주문, 장부에 없는 KIS의 주문을 본다",
    }[code]
    lines += ["", f"결과: {verdict}"]
    return "\n".join(lines)


def _sides(rows: Sequence[LedgerOrder | OrderRow]) -> str:
    buys = sum(1 for r in rows if r.side == SIDE_BUY)
    return f"매수 {buys}, 매도 {len(rows) - buys}"


def _filled(rows: Sequence[LedgerOrder | OrderRow]) -> str:
    bought = sum(r.filled for r in rows if r.side == SIDE_BUY)
    sold = sum(r.filled for r in rows if r.side == SIDE_SELL)
    return f"매수 {bought}주 · 매도 {sold}주"


def _row(r: OrderRow) -> str:
    at = _at(r)
    when = at.strftime("%H:%M:%S") if at is not None else "시각 없음"
    return f"{when} {SIDE_NAMES.get(r.side, r.side)} {r.quantity}주, 체결 {r.filled}주"


def _has(db: sqlite3.Connection, table: str) -> bool:
    found = db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()
    return found is not None


async def check_mock_trades(
    settings: Settings, now: datetime, day: date | None = None, *, servers: Servers = MOCK_SERVERS
) -> int:
    """``krx-quant check-mock-trades``: ``day`` (else the trading day whose night ``now`` is in)
    compared and printed; its exit code (``SAME``, ``DIFFERENT``, ``NO_ORDERS``, ``UNREADABLE``).
    ``SettingsError`` when the trader's broker cannot be made (its key, or servers not the mock ones)."""
    day = day or report_day(calendar_store(settings).calendar, now)
    heading = f"KIS 모의 매매 대조 (#405) · {day}({WEEKDAYS[day.weekday()]}) · {SYMBOL}"
    try:
        with _reading(settings.state_dir / DB_NAME) as db:
            if db is None:
                print(f"{heading}\n결과: trader의 저장소(state/trader.sqlite3)가 없다")
                return UNREADABLE
            ledger = ledger_orders(db, day)
            failed = len(failures(db, day))
    except sqlite3.Error as e:
        print(f"{heading}\n결과: trader의 저장소를 읽지 못했다: {e}")
        return UNREADABLE
    async with kis.rest_session() as session:
        broker = trader_broker(session, settings, lambda: datetime.now(KST), servers=servers)
        try:
            rows = await broker.orders(day, SYMBOL)
        except BrokerError as e:
            print(f"{heading}\n결과: KIS 주문 체결 조회를 읽지 못했다: {e}")
            return UNREADABLE
    check = Check(day, ledger, rows, probe_window(settings.state_dir, day), failed)
    print(render(check))
    return check.code
