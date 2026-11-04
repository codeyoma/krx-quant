"""The promotion review's record for the tests (#511): the weekdays the policies run on, the days and
trades each ran as the trader keeps them, a champion and its challengers set up, the review's
judgement at a moment, and the night it runs. Every value is made up.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, time, timedelta
from pathlib import Path

from krx_quant.feeds.base import KST
from krx_quant.trader import policy as policies
from krx_quant.trader import promotion
from krx_quant.trader.promotion import promote
from krx_quant.trader.store import DB_NAME, TraderStore
from tests.trader_doubles import (
    BARRIER,
    CALENDAR,
    at,
    enter,
    keep,
    policy_cell,
)

# The weekdays from 09-01 (the calendar guesses they trade). The tests' policies are kept from 08-31's
# night (``keep``), so the evaluation version's first cycle (#469) starts on the first of them: its
# review day is the 20th (09-28), the next the 40th.
WEEKDAYS = [d for d in (date(2026, 9, 1) + timedelta(days=i) for i in range(60)) if d.weekday() < 5]


# Seven days the challenger made 2,000 won more than the champion and three it made 1,000 less:
# mean 1,100; deviations 900 (×7) and −2,100 (×3), their squares 18,900,000, the variance
# 18,900,000 ÷ 9 = 2,100,000, the standard error √(2,100,000 ÷ 10) = 458.2576.
CLOSE_CALL = [2_000.0] * 7 + [-1_000.0] * 3


COLUMNS = (
    *("mode", "symbol", "kind", "predictor", "prediction_id", "bar_close", "probability", "threshold"),
    *("anchor", "tick", "width_ticks", "target", "stop", "time_barrier", "decided_at", "entry_at"),
    *("entry_price", "quantity", "buy_fee", "exit_at", "exit_price", "exit_reason", "sell_fee", "tax"),
    *("cost", "gross", "net", "policy_id", "champion"),
)


def ran(
    state: Path, version: int, nets: Sequence[float], champion: bool = False, days: Sequence[date] = WEEKDAYS
) -> None:
    """``version`` ran on the first of ``days`` (the weekdays from 09-01), one each of ``nets``: a
    trade of that net P&L on the days it is not 0, none on the others (as the trader writes them: a
    day's row and its trades)."""
    store = TraderStore(state / DB_NAME)
    try:
        for day, net in zip(days, nets, strict=False):
            store.put_policy_day("paper", version, day, champion, False, at(f"{day} 10:00:00"))
            if net:
                entry = datetime.combine(day, time(10), KST)
                with store._db:
                    store._db.execute(
                        f"INSERT INTO trades ({', '.join(COLUMNS)}) VALUES ({', '.join('?' * len(COLUMNS))})",
                        (
                            *("paper", "005930", "barrier", "breiman", 1, entry.isoformat(), 0.7, 0.6),
                            *(70_050, 100, 2, 70_250, 69_850, (entry + timedelta(minutes=4)).isoformat()),
                            *(
                                entry.isoformat(),
                                (entry + timedelta(seconds=2)).isoformat(),
                                70_100,
                                10,
                                105.15,
                            ),
                            *(
                                (entry + timedelta(minutes=2)).isoformat(),
                                70_250,
                                "take_profit",
                                105.375,
                                1405,
                            ),
                            *(1615.525, net + 1615.525, net, version, int(champion)),
                        ),
                    )
    finally:
        store.close()


def setup(state: Path, *challengers: Sequence[float], days: Sequence[date] = WEEKDAYS) -> None:
    """Policy 1 the champion, without a trade on the first ten of ``days`` (the weekdays from 09-01);
    a challenger for each of ``challengers``, its nets on those days, all entered the night before
    the first as the cohort of the review cycle in force then (#473, ``enter``)."""
    night = datetime.combine(days[0] - timedelta(days=1), time(22, 31), KST)
    keep(state, night, policy_cell(BARRIER, 0.8))
    versions = [
        keep(state, night + timedelta(minutes=1), policy_cell(BARRIER, 0.6 - n / 100))
        for n, _ in enumerate(challengers)
    ]
    enter(state, night + timedelta(minutes=2), *versions)
    ran(state, 1, [0.0] * 10, champion=True, days=days)
    for version, nets in enumerate(challengers, start=2):
        ran(state, version, nets, days=days)


def judged_at(state: Path, now: datetime) -> promotion.Promotion:
    db = policies.connect(state / DB_NAME)
    try:
        return promote(db, now, CALENDAR.session)
    finally:
        db.close()


def day_ran(state: Path, version: int, day: date, net: float, champion: bool) -> None:
    """``version`` ran on ``day`` with a trade of ``net`` (none when 0)."""
    store = TraderStore(state / DB_NAME)
    try:
        store.put_policy_day("paper", version, day, champion, False, datetime.combine(day, time(10), KST))
    finally:
        store.close()
    if net:
        ran_trade(state, version, day, net, champion)


def ran_trade(state: Path, version: int, day: date, net: float, champion: bool) -> None:
    store = TraderStore(state / DB_NAME)
    try:
        entry = datetime.combine(day, time(10), KST)
        with store._db:
            store._db.execute(
                f"INSERT INTO trades ({', '.join(COLUMNS)}) VALUES ({', '.join('?' * len(COLUMNS))})",
                (
                    *("paper", "005930", "barrier", "breiman", 1, entry.isoformat(), 0.7, 0.6),
                    *(70_050, 100, 2, 70_250, 69_850, (entry + timedelta(minutes=4)).isoformat()),
                    *(entry.isoformat(), (entry + timedelta(seconds=2)).isoformat(), 70_100, 10, 105.15),
                    *((entry + timedelta(minutes=2)).isoformat(), 70_250, "take_profit", 105.375, 1405),
                    *(1615.525, net + 1615.525, net, version, int(champion)),
                ),
            )
    finally:
        store.close()


# Weekdays enough for three checkpoints (the calendar guesses they trade).
LONG = [d for d in (date(2026, 9, 1) + timedelta(days=i) for i in range(100)) if d.weekday() < 5]


def night(day: date) -> datetime:
    return datetime.combine(day, time(22, 31), KST)
