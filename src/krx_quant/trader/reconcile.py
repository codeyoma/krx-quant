"""The KIS account against the ledger (#406, #393 "KIS 주문"): what a mock start compares before
the trader buys or sells anything, and which KIS order a buy whose answer was lost is.

The mock book (``mock.MockTrader.start``) first reads its own orders on KIS, each on the day it was
placed (an order of a day gone by is over: a KRX limit order is a day order), and looks up each buy
it sent whose answer it never had (``store.SENT``, ``candidates``). Then ``compare`` holds the
account's balance and the day's order inquiry against what the ledger holds:

- **005930 held**: the ledger's open trade's shares not sold yet, give or take what its orders still
  open on KIS may fill between the reads (a buy's ``rmn_qty`` more, a sell's fewer). More is held
  outside the ledger; fewer is missing from the account.
- **Any other symbol held**: outside the ledger (the trader trades 005930 alone).
- **The day's orders with something left** (``rmn_qty`` above 0) that the trader does not follow
  (its open buy, its resting take-profit, a sale's sell): outside the ledger, but for those placed
  while the order probe ran that day (``mock_check.probe_window``, as the mock day's check tells
  them apart).
- **A lost buy** more than one KIS order could be, or one still open on KIS (its cancel would name
  KIS's branch, ``ord_gno_brno``, which the probe has not measured yet), or one while another trade
  is open: doubtful, left as sent.

What is outside the ledger is the account's, not the trader's, once accepted: the last reconcile's
that found the account as the ledger has it, or whose pause a resume lifted
(``store.accepted_outside``). A finding is ``clean`` when nothing outside goes beyond that, nothing
is missing and nothing is doubtful; else the trader touches none of it, pauses new entries and says
why on Telegram (``notice``), until ``krx-quant trade resume`` on the server accepts the account as
it is. Shares missing from the account are never accepted: a later start finds them again.

Nothing here reads KIS or the store: the mock book passes what it read.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from krx_quant.feeds.base import KST
from krx_quant.kis_orders import SIDE_BUY, Balance, OrderRow, same_number
from krx_quant.trader import SYMBOL
from krx_quant.trader.mock_check import PROBE_JOB
from krx_quant.trader.store import Placed

# A lost buy is a KIS buy placed (by KIS's ``ord_tmd``) from this long before it was sent to this
# long after: KIS's clock and the trader's are both KST, a few seconds apart at most.
LOST_BEFORE = timedelta(seconds=10)
LOST_AFTER = timedelta(minutes=2)

Window = tuple[time, time] | None  # when the order probe ran that day (``mock_check.probe_window``)


@dataclass(frozen=True)
class Outside:
    """What the account holds or has open outside the ledger: shares by symbol, and KIS's numbers of
    the day's orders with something left."""

    holdings: Mapping[str, int] = field(default_factory=dict)
    orders: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.holdings) or bool(self.orders)

    def beyond(self, accepted: Outside) -> Outside:
        """What of it goes beyond ``accepted``: more shares of a symbol, an order it did not hold."""
        holdings = {
            s: q - accepted.holdings.get(s, 0)
            for s, q in self.holdings.items()
            if q > accepted.holdings.get(s, 0)
        }
        orders = tuple(n for n in self.orders if not any(same_number(n, a) for a in accepted.orders))
        return Outside(holdings, orders)

    def as_json(self) -> dict[str, Any]:
        return {"holdings": dict(sorted(self.holdings.items())), "orders": list(self.orders)}

    @classmethod
    def of(cls, data: Mapping[str, Any]) -> Outside:
        holdings = {str(s): int(q) for s, q in dict(data.get("holdings") or {}).items()}
        return cls(holdings, tuple(str(n) for n in data.get("orders") or ()))


@dataclass(frozen=True)
class Finding:
    """A reconcile's: what is outside the ledger (``outside``), what of it goes beyond what was
    accepted (``new``), the ledger's shares the account does not hold (``missing``), the lost buys
    it could not adopt (``doubtful``) and the probe's open orders it left out (``probe``)."""

    outside: Outside
    new: Outside
    missing: int = 0
    doubtful: int = 0
    probe: int = 0

    @property
    def clean(self) -> bool:
        return not self.new and not self.missing and not self.doubtful

    def detail(self) -> dict[str, Any]:
        """What differed, by kind (``reconciles.detail``)."""
        return {
            "new": self.new.as_json(),
            "missing": self.missing,
            "doubtful": self.doubtful,
            "probe": self.probe,
        }

    def same_as(self, detail: Mapping[str, Any]) -> bool:
        """Whether it found what ``detail`` (a reconcile's kept) did: the same beyond what was
        accepted, missing and doubtful (the probe's orders aside)."""
        mine = self.detail()
        return all(detail.get(k) == mine[k] for k in ("new", "missing", "doubtful"))

    def notice(self, mode: str) -> str:
        """Its Telegram notice (no account, order number or price)."""
        found = []
        if self.new.holdings:
            held = ", ".join(f"{s} {q}주" for s, q in sorted(self.new.holdings.items()))
            hint = (
                f" (주문 프로브 {PROBE_JOB}가 남긴 주식일 수 있습니다)" if SYMBOL in self.new.holdings else ""
            )
            found.append(f"장부에 없는 보유 {held}{hint}")
        if self.new.orders:
            found.append(f"장부에 없는 미체결 주문 {len(self.new.orders)}건")
        if self.missing:
            found.append(f"장부의 포지션 가운데 {self.missing}주가 계좌에 없습니다")
        if self.doubtful:
            found.append(f"응답이 없던 매수 {self.doubtful}건을 KIS의 주문 하나로 가리지 못했습니다")
        return (
            f"자동 매매({mode}): 시작할 때 KIS 계좌가 장부와 다릅니다: {'; '.join(found)}."
            " 장부 밖의 것은 건드리지 않고, 새로 사지 않습니다(새 진입 정지). 장부의 포지션은 원래 규칙"
            "(목표가, 손절가, 시간)대로 팝니다. 서버에서 KIS 모의투자 계좌를 확인한 뒤"
            " krx-quant trade resume으로 다시 켭니다: 그때 장부 밖의 보유와 주문은 trader의 것이 아닌"
            " 것으로 받아들입니다."
        )


def placed_at(row: OrderRow, day: date) -> datetime | None:
    """When KIS placed ``row`` on ``day`` (its ``ord_tmd``, KST); None if it does not say."""
    try:
        return datetime.combine(day, datetime.strptime(row.at, "%H%M%S").time(), KST)
    except ValueError:
        return None


def _probes(row: OrderRow, probe: Window) -> bool:
    """Whether ``row`` was placed while the order probe ran that day: the probe's, not the trader's."""
    try:
        at = datetime.strptime(row.at, "%H%M%S").time()
    except ValueError:
        return False
    return probe is not None and probe[0] <= at <= probe[1]


def candidates(
    rows: Sequence[OrderRow], buy: Placed, day: date, known: Iterable[str], probe: Window
) -> list[OrderRow]:
    """The KIS orders of ``day`` that a buy sent at ``buy.at`` whose answer was lost could be: buys
    of 005930 at its limit and of its quantity, placed from ``LOST_BEFORE`` before it was sent to
    ``LOST_AFTER`` after, whose numbers the ledger does not keep (``known``), not the probe's."""
    assert buy.at is not None
    numbers = list(known)
    found = []
    for r in rows:
        at = placed_at(r, day)
        if (
            r.cancel
            or r.side != SIDE_BUY
            or r.symbol != SYMBOL
            or r.price != round(buy.price)
            or r.quantity != buy.quantity
            or at is None
            or not buy.at - LOST_BEFORE <= at <= buy.at + LOST_AFTER
            or any(same_number(r.number, n) for n in numbers)
            or _probes(r, probe)
        ):
            continue
        found.append(r)
    return found


def compare(
    balance: Balance,
    today: Sequence[OrderRow],
    followed: Iterable[str],
    held: tuple[int, int],
    accepted: Outside,
    probe: Window,
    doubtful: int = 0,
) -> Finding:
    """The account (``balance``, the day's inquiry ``today``) against the ledger: ``followed`` the
    KIS numbers of the orders the trader follows, ``held`` the fewest and the most shares of 005930
    the ledger can hold (its open trade's shares not sold yet, less what its open sells may still
    sell, plus what its open buy may still buy); ``accepted`` what was accepted outside it, ``probe``
    when the order probe ran that day, ``doubtful`` the lost buys not adopted (module doc)."""
    low, high = held
    shares = balance.held(SYMBOL)
    holdings = {s: h.quantity for s, h in balance.holdings.items() if s != SYMBOL and h.quantity > 0}
    if shares > high:
        holdings[SYMBOL] = shares - high
    numbers = list(followed)
    open_rows = [
        r for r in today if not r.cancel and r.left > 0 and not any(same_number(r.number, n) for n in numbers)
    ]
    probes = [_probes(r, probe) for r in open_rows]
    outside = Outside(holdings, tuple(r.number for r, p in zip(open_rows, probes, strict=True) if not p))
    return Finding(outside, outside.beyond(accepted), max(0, low - shares), doubtful, sum(probes))
