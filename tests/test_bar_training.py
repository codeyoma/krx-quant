"""Training on a day with backfilled bars only, with the touch target (#169): the one-minute
bar rule's labels (``touch.bar_touch``).

Input: a hand-made day of one-minute bars. Output: the training samples' labels.

Every bar trades at 81,200 alone (k = 2), so each close's side is unknown (the next open is
the same price) and the targets are 81,500 and 80,900, except:
- 09:05 has no bar (no trade in that minute);
- 09:10 reaches 81,500 and closes back at 81,200, its low: a close on the bid.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from krx_quant.features import DEFAULT_HALF_SPREAD
from krx_quant.feeds import KST
from krx_quant.market_calendar import MarketCalendar
from krx_quant.pricing import Direction
from krx_quant.reference import Bar
from krx_quant.touch import TARGET_VERSION
from krx_quant.training import samples

DAY = date(2026, 10, 12)
CALENDAR = MarketCalendar({DAY: True})
FEE = 0.015 / 100


def at(hh: int, mm: int) -> datetime:
    return datetime.combine(DAY, time(hh, mm), KST)


def day_of_bars() -> list[Bar]:
    bars = []
    for minute in range(0, 41):
        if minute == 5:
            continue
        start = at(9, 0) + timedelta(minutes=minute)
        high = 81_500 if minute == 10 else 81_200
        bars.append(Bar(start, 81_200, high, 81_200, 81_200, 10, 0, start, start))
    return bars


def labels(target_version: int) -> dict[str, Direction | None]:
    made = samples(
        {("005930", DAY): day_of_bars()}, CALENDAR, ("flat_ticks",), FEE, target_version=target_version
    )
    return {f"{s.bar_close:%H:%M}": s.outcome for s in made if s.bar_close <= at(9, 30)}


def test_a_backfilled_day_is_labelled_by_the_one_minute_bar_rule() -> None:
    found = labels(2)

    # The windows that hold 09:10's high of 81,500: up (the targets are 81,500 and 80,900).
    assert [found[t] for t in ("09:07", "09:08", "09:09", "09:10")] == [Direction.UP] * 4
    # 09:06: no bar ended then (no trade in 09:05), so no close to set targets from.
    assert found["09:06"] is None
    # 09:11: the close at 81,200 was 09:10's low, on the bid: up needs 81,600 now.
    assert found["09:11"] is Direction.FLAT
    # 09:05: its window lacks the 09:05 bar; the next trade (09:06's open) places the close.
    assert found["09:05"] is Direction.FLAT
    assert {found[t] for t in ("09:01", "09:02", "09:03", "09:04", "09:12", "09:30")} == {Direction.FLAT}


def test_only_the_label_changes_with_the_touch_target() -> None:
    bars = {("005930", DAY): day_of_bars()}
    touching = samples(bars, CALENDAR, ("flat_ticks",), FEE, target_version=TARGET_VERSION)
    moving = samples(bars, CALENDAR, ("flat_ticks",), FEE)

    assert [s.change_ticks for s in touching] == [s.change_ticks for s in moving]
    assert [s.features.as_dict() for s in touching] == [s.features.as_dict() for s in moving]
    # No quotes on a backfilled day: a one-tick spread is assumed for the forecast.
    assert {s.features.touch.half_spread for s in touching if s.features.touch} == {DEFAULT_HALF_SPREAD}
    assert all(s.features.touch is not None for s in touching)
    assert all(s.features.touch is None for s in moving)
    # The old outcome: every close four minutes later is 81,200, none at 09:06 (no 09:05 bar).
    old = {f"{s.bar_close:%H:%M}": s.outcome for s in moving if s.bar_close <= at(9, 30)}
    assert old.pop("09:02") is None
    assert set(old.values()) == {Direction.FLAT}
