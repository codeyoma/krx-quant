"""The touch label through the Replay Harness (#167): recorded beside the outcomes.

Input: hand-made integrated book updates and a fake clock. Output: the touch labels stored
for each bar close and served on each prediction's outcome.

The book sits at 81,200 / 81,300 (mid 81,250, k = 2, targets 81,450 and 81,050) every 10 s,
except one update at 09:32:30 with the bid at 81,500: the windows that hold it (the bar closes
09:29 to 09:32) are up, the others flat.
"""

from __future__ import annotations

from pathlib import Path

from krx_quant.engine import Event
from krx_quant.predictions import PredictionStore
from krx_quant.touch import RESTARTED, SILENT
from tests.replay_events import SYMBOL, api, at, book, get_json, run, steady_book


def quiet_book(start: str, end: str) -> list[Event]:
    return steady_book(at(start), at(end), 81_200, 81_300, every=10)


def classes(db: Path) -> dict[str, str | None]:
    store = PredictionStore(db)
    try:
        return {r["bar_close"][11:16]: r["class"] for r in store.touches(SYMBOL)}
    finally:
        store.close()


async def test_each_bar_close_gets_one_touch_label_on_its_four_minutes(tmp_path: Path) -> None:
    db = tmp_path / "engine.sqlite3"
    events = quiet_book("09:00:30", "09:45:00") + [book(at("09:32:30"), 81_500, 81_600)]

    engine = await run(db, events, until=at("09:45:00"))

    labels = classes(db)
    assert labels["09:28"] == "flat"  # its window ends at 09:32
    assert [labels[c] for c in ("09:29", "09:30", "09:31", "09:32")] == ["up"] * 4
    assert labels["09:33"] == "flat"
    store = PredictionStore(db)
    try:
        row = next(r for r in store.touches(SYMBOL) if r["bar_close"].startswith("2026-10-12T09:31"))
    finally:
        store.close()
    assert row["path"]["upper_at"] == "2026-10-12T09:32:30+09:00"
    assert (row["path"]["anchor"], row["path"]["upper"], row["path"]["lower"]) == (81_250, 81_450, 81_050)

    # Every prediction of the bar close carries the same label on its outcome.
    async with api(engine) as client:
        made = await get_json(
            client, f"/v1/symbols/{SYMBOL}/predictions?bar_close=2026-10-12T09:31:00%2B09:00"
        )
    scored = [p for p in made["predictions"] if p["outcome"] is not None]
    assert scored
    assert {p["outcome"]["touch"]["class"] for p in scored} == {"up"}
    assert {p["outcome"]["touch"]["target_version"] for p in scored} == {2}


async def test_a_minute_without_quotes_cannot_be_scored(tmp_path: Path) -> None:
    db = tmp_path / "engine.sqlite3"
    # Nothing received from 09:35:00 to 09:36:30: 90 s.
    events = quiet_book("09:00:30", "09:35:00") + quiet_book("09:36:30", "09:45:00")

    await run(db, events, until=at("09:45:00"))

    store = PredictionStore(db)
    try:
        rows = {r["bar_close"][11:16]: r for r in store.touches(SYMBOL)}
    finally:
        store.close()
    assert rows["09:34"]["unscorable"] == SILENT  # its window, 09:34–09:38, holds the gap
    assert rows["09:34"]["class"] is None
    assert rows["09:30"]["class"] == "flat"  # ends at 09:34, before it


async def test_a_window_the_engine_restarted_in_is_not_scored(tmp_path: Path) -> None:
    db = tmp_path / "engine.sqlite3"
    events = quiet_book("09:00:30", "09:45:00")
    restart = at("09:33:30")

    await run(db, [e for e in events if e[1]["received_at"] < restart], until=restart)
    await run(db, [e for e in events if e[1]["received_at"] >= restart], until=at("09:45:00"), start=restart)

    store = PredictionStore(db)
    try:
        rows = {r["bar_close"][11:16]: r for r in store.touches(SYMBOL)}
    finally:
        store.close()
    # 09:30 to 09:33 were pending at the restart: their windows were not followed through.
    assert {rows[c]["unscorable"] for c in ("09:30", "09:31", "09:32", "09:33")} == {RESTARTED}
    assert rows["09:34"]["class"] == "flat"  # opened after the restart
    assert rows["09:29"]["class"] == "flat"  # scored at 09:33, before it
