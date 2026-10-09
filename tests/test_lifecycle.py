"""Algorithm status (probation, active, bench, retired, clone) through the Replay Harness.

Input: scored history put in the prediction store (one sure call per bar close, as in the
leaderboard tests) and status rows as of earlier days; then a replayed day, whose daily
review runs at 15:40, after the close. Output: each algorithm's status and history in
the Engine API, the alerts sent, and what the engine predicted.

Every rule reads the statistics the leaderboard shows: basis B, special predictions left
out, the weakest lift over the three baselines, and only with enough scored predictions
and pairs (``min_sample``, 20 here).
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest

from krx_quant.cli import main
from krx_quant.feeds import KST
from krx_quant.lifecycle import Status
from krx_quant.market_calendar import MarketCalendar
from krx_quant.predictions import DB_NAME, NewPrediction, PredictionStore, Score
from krx_quant.pricing import Direction
from tests.algorithm_doubles import FixedAlgorithm
from tests.replay_events import DAY, api, at, get_json, run
from tests.test_leaderboard_api import OUTCOMES as FORTY_OUTCOMES
from tests.test_leaderboard_api import SURE

ALPHA = FixedAlgorithm("alpha", features=("velocity",), unique=("velocity",))
BETA = FixedAlgorithm("beta", features=("acceleration",), unique=("acceleration",))
GAMMA = FixedAlgorithm("gamma", features=("relative_volume",), unique=("relative_volume",))
DELTA = FixedAlgorithm("delta", features=("vwap_z",), unique=("vwap_z",))
NEXT_DAY, THIRD_DAY = date(2026, 10, 13), date(2026, 10, 14)
THREE_DAYS = MarketCalendar({DAY: True, NEXT_DAY: True, THIRD_DAY: True})
# What happened at each day's first eight bar closes from 09:01: up, flat, down, flat, twice.
# Enough for min_sample 20 within three days, and few enough to fill 65 days fast.
OUTCOMES = FORTY_OUTCOMES[:8]
BASELINE_CALLS = {
    "baseline-flat": [Direction.FLAT] * len(OUTCOMES),
    "baseline-persist": [Direction.UP] * len(OUTCOMES),
    "baseline-random": [Direction.DOWN] * len(OUTCOMES),
}
RIGHT = OUTCOMES  # every call right
FLAT = [Direction.FLAT] * len(OUTCOMES)  # the same calls as always-flat: no lift
# Right but on the downs, which it calls flat: better than every baseline, and its p_up − p_down
# moves with RIGHT's at a correlation of 0.82 (0.25 / √(0.5 × 0.1875)), not a clone.
MISSES_DOWNS: list[Direction] = [Direction.FLAT if o is Direction.DOWN else o for o in OUTCOMES]


def weekdays_before(day: date, n: int) -> list[date]:
    """The ``n`` weekdays before ``day``, oldest first."""
    days: list[date] = []
    d = day
    while len(days) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            days.append(d)
    return days[::-1]


def morning(day: date) -> datetime:
    return datetime.combine(day, time(8), KST)


async def review_day(db: Path, algorithms: list[FixedAlgorithm], **options: Any) -> Any:
    """Replay DAY past its 15:40 review, with no market events."""
    return await run(db, [], until=at("15:45:00"), algorithms=algorithms, min_sample=20, **options)


async def listed(engine: Any) -> dict[str, Any]:
    async with api(engine) as client:
        body = await get_json(client, "/v1/algorithms")
    return {a["name"]: a for a in body["algorithms"]}


async def test_a_new_algorithm_starts_on_probation(tmp_path: Path) -> None:
    engine = await review_day(tmp_path / "engine.db", [ALPHA])

    alpha = (await listed(engine))["alpha"]
    assert alpha["status"]["status"] == "probation"
    [entered] = alpha["status"]["history"]
    assert (entered["from"], entered["to"], entered["reason"]) == (None, "probation", "registered")
    assert entered["at"] == "2026-10-12T00:00:00+09:00"  # when the engine first ran it


@contextlib.contextmanager
def stored(db: Path) -> Iterator[PredictionStore]:
    store = PredictionStore(db)
    try:
        yield store
    finally:
        store.close()


def put_status(db: Path, algorithm: str, status: str, since: date) -> None:
    with stored(db) as store:
        store.add_status(algorithm, morning(since), None, Status(status), "set by the test")


def put_days(
    db: Path, days: list[date], calls: dict[str, list[Direction]], *, with_baselines: bool = True
) -> None:
    """Each algorithm's ``calls`` on each of ``days`` (one sure call per bar close from 09:01,
    scored against OUTCOMES on both bases), with the baselines' unless they are there.
    """
    if with_baselines:
        calls = {**calls, **BASELINE_CALLS}
    with stored(db) as store:
        for d in days:
            made = []
            for algorithm, directions in calls.items():
                for i, call in enumerate(directions):
                    t = datetime.combine(d, time(9, 1), KST) + timedelta(minutes=i)
                    at_target = t + timedelta(minutes=4)
                    made.append(
                        NewPrediction(
                            "005930", algorithm, "v1", t, t, SURE[call], at_target, 81_250, 81_250, 2
                        )
                    )
            pending = store.add_all(made)
            scores = []
            for p in pending:
                outcome = OUTCOMES[
                    (p.target_at - datetime.combine(d, time(9, 5), KST)) // timedelta(minutes=1)
                ]
                mid = 81_250 + {Direction.UP: 300, Direction.FLAT: 0, Direction.DOWN: -300}[outcome]
                scores.append((p.prediction_id, Score(mid, outcome, outcome, None)))
            store.score_all(scores, datetime.combine(d, time(15, 30), KST))


def last_change(algorithm: dict[str, Any]) -> tuple[str | None, str, str]:
    change = algorithm["status"]["history"][-1]
    return change["from"], change["to"], change["reason"]


async def test_probation_ends_after_ten_days_better_than_every_baseline(tmp_path: Path) -> None:
    db = tmp_path / "engine.db"
    history = weekdays_before(DAY, 9)  # with the replayed day, ten days on probation
    for name in ("alpha", "gamma"):
        put_status(db, name, "probation", history[0])
    put_status(db, "beta", "probation", history[-3])  # four days so far
    put_days(db, history, {"alpha": RIGHT, "gamma": FLAT})
    put_days(db, history[-3:], {"beta": MISSES_DOWNS}, with_baselines=False)

    engine = await review_day(db, [ALPHA, BETA, GAMMA])

    statuses = await listed(engine)
    assert last_change(statuses["alpha"])[:2] == ("probation", "active")
    assert "lift" in last_change(statuses["alpha"])[2]
    assert statuses["alpha"]["status"]["since"] == "2026-10-12T15:40:00+09:00"
    assert statuses["beta"]["status"]["status"] == "probation"  # too soon
    assert statuses["gamma"]["status"]["status"] == "probation"  # no better than always-flat
    async with api(engine) as client:
        board = await get_json(client, "/v1/leaderboard?period=20d")
    assert {e["algorithm"]: e["status"] for e in board["ranked"] + board["unranked"]} == {
        "alpha": "active",
        "beta": "probation",
        "gamma": "probation",
    }


# Wrong where it matters: down on the ups, up on the downs, flat on the flats. It beats no
# baseline.
WRONG: list[Direction] = [
    {Direction.UP: Direction.DOWN, Direction.DOWN: Direction.UP}.get(o, o) for o in OUTCOMES
]


async def test_an_active_algorithm_no_better_than_a_baseline_is_benched_and_still_predicts(
    tmp_path: Path,
) -> None:
    db = tmp_path / "engine.db"
    days = weekdays_before(DAY, 25)
    put_status(db, "alpha", "active", days[0])
    put_status(db, "beta", "active", days[0])
    # Good for its first days, then wrong for the last twenty.
    put_days(db, days[:5], {"alpha": RIGHT, "beta": MISSES_DOWNS})
    put_days(db, days[5:], {"alpha": WRONG, "beta": MISSES_DOWNS})

    engine = await review_day(db, [ALPHA, BETA])

    statuses = await listed(engine)
    assert last_change(statuses["alpha"])[:2] == ("active", "bench")
    assert "last 20 days" in last_change(statuses["alpha"])[2]
    assert statuses["beta"]["status"]["status"] == "active"
    async with api(engine) as client:
        body = await get_json(client, "/v1/symbols/005930/predictions?algorithm=alpha&limit=5")
    assert len(body["predictions"]) == 5  # a benched algorithm is still run


class Outbox:
    """Alerts sent, as Telegram would take them, or refuse them while ``down``."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.down = False

    async def __call__(self, text: str) -> bool:
        self.sent.append(text)
        return not self.down


async def test_a_benched_algorithm_recovers_or_after_sixty_days_is_a_retirement_candidate(
    tmp_path: Path,
) -> None:
    db = tmp_path / "engine.db"
    days = weekdays_before(DAY, 65)
    put_status(db, "alpha", "bench", days[0])
    put_status(db, "beta", "bench", days[0])
    put_days(db, days[:-20], {"alpha": WRONG, "beta": WRONG})
    put_days(db, days[-20:], {"alpha": WRONG, "beta": RIGHT})  # beta is good again
    outbox = Outbox()
    outbox.down = True  # Telegram is unreachable on the first day

    engine = await review_day(db, [ALPHA, BETA], notify=outbox)

    statuses = await listed(engine)
    assert last_change(statuses["beta"])[:2] == ("bench", "active")
    assert statuses["alpha"]["status"]["status"] == "bench"  # retired only by command
    [alert] = outbox.sent
    assert "alpha 은퇴 후보" in alert and "krx-quant retire alpha" in alert

    # Not sent, so asked again the next day; sent then, so not the day after.
    outbox.down = False
    for day in (NEXT_DAY, THIRD_DAY):
        await run(
            db,
            [],
            until=at("15:45:00", day),
            calendar=THREE_DAYS,
            algorithms=[ALPHA, BETA],
            min_sample=20,
            notify=outbox,
        )
    assert len(outbox.sent) == 2


async def test_a_status_set_after_the_review_counts_from_the_next_day(tmp_path: Path) -> None:
    db = tmp_path / "engine.db"
    history = weekdays_before(DAY, 9)
    # Ten days with predictions, but the first one's review had run when probation began.
    with stored(db) as store:
        store.add_status("alpha", at("16:00:00", history[0]), None, Status.PROBATION, "set by the test")
    put_days(db, history, {"alpha": RIGHT})

    engine = await review_day(db, [ALPHA])

    assert (await listed(engine))["alpha"]["status"]["status"] == "probation"


async def test_with_too_little_to_judge_the_status_stays(tmp_path: Path) -> None:
    db = tmp_path / "engine.db"
    put_status(db, "alpha", "active", weekdays_before(DAY, 1)[0])
    put_days(db, weekdays_before(DAY, 1), {"alpha": WRONG})  # 8 scored, below min_sample 20

    engine = await review_day(db, [ALPHA])

    assert (await listed(engine))["alpha"]["status"]["status"] == "active"


async def test_a_review_missed_while_the_engine_was_down_runs_when_it_starts(tmp_path: Path) -> None:
    db = tmp_path / "engine.db"
    history = weekdays_before(DAY, 10)
    put_status(db, "alpha", "probation", history[0])
    put_days(db, history, {"alpha": RIGHT})
    with stored(db) as store:
        store.add_run(
            "review", at("15:40:00", history[-1]), None, "the last review before the engine stopped"
        )

    # Down from before DAY's close until 16:00.
    engine = await run(db, [], start=at("16:00:00"), until=at("16:01:00"), algorithms=[ALPHA], min_sample=20)

    alpha = (await listed(engine))["alpha"]
    assert last_change(alpha)[:2] == ("probation", "active")
    assert alpha["status"]["since"] == "2026-10-12T15:40:00+09:00"  # as of the review it missed


async def test_an_algorithm_on_probation_moving_with_an_earlier_one_is_a_clone(tmp_path: Path) -> None:
    db = tmp_path / "engine.db"
    days = weekdays_before(DAY, 5)
    for name in ("alpha", "beta", "gamma"):
        put_status(db, name, "probation", days[0])
    put_status(db, "delta", "clone", days[0])
    # beta says what alpha says; gamma does not; delta stopped copying anyone.
    put_days(db, days, {"alpha": RIGHT, "beta": RIGHT, "gamma": MISSES_DOWNS, "delta": WRONG})

    engine = await review_day(db, [ALPHA, BETA, GAMMA, DELTA])

    statuses = await listed(engine)
    assert last_change(statuses["beta"])[:2] == ("probation", "clone")
    assert "alpha" in last_change(statuses["beta"])[2]
    assert statuses["alpha"]["status"]["status"] == "probation"  # the earlier one is not the clone
    assert statuses["gamma"]["status"]["status"] == "probation"
    assert last_change(statuses["delta"])[:2] == ("clone", "probation")


async def test_retiring_takes_a_command_stops_the_algorithm_and_keeps_its_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "state"
    db = state / DB_NAME
    state.mkdir()
    days = weekdays_before(DAY, 3)
    put_status(db, "alpha", "bench", days[0])
    put_days(db, days, {"alpha": WRONG})
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KRX_STATE_DIR", str(state))

    assert main(["retire", "alpha"]) == 0
    assert main(["retire", "nobody"]) == 1
    assert "unknown algorithm" in capsys.readouterr().err
    engine = await review_day(db, [ALPHA, BETA])

    alpha = (await listed(engine))["alpha"]
    assert last_change(alpha) == ("bench", "retired", "retired by command")
    with stored(db) as store:
        made = [p for p in store.recent("005930", 1000, "alpha")]
    assert made and all(p["bar_close"] < "2026-10-12" for p in made)  # nothing today, the rest kept

    assert main(["unretire", "alpha"]) == 0
    with stored(db) as store:
        assert store.statuses()["alpha"].status is Status.PROBATION
