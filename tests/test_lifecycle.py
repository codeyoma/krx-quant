"""Algorithm status (probation, active, bench, retired, clone) through the Replay Harness.

Input: scored history put in the prediction store (one sure call per bar close, as in the
leaderboard tests) and status rows as of earlier days; then a replayed day, whose daily
review runs at 15:40, after the close. Output: each algorithm's status and history in
the Engine API, the alerts sent, and what the engine predicted.

Every rule reads the statistics the leaderboard shows: the touch label (#170), special
predictions left out, the weakest lift over the three baselines, and only with enough
scored predictions and pairs (``min_sample``, 20 here); of an algorithm, only the
predictions of models trained on the touch target (#170).
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest

from krx_quant.algorithms import Model
from krx_quant.cli import main
from krx_quant.clock import FakeClock
from krx_quant.feeds import KST
from krx_quant.lifecycle import Status, target_changed
from krx_quant.market_calendar import MarketCalendar
from krx_quant.predictions import DB_NAME, NewPrediction, PredictionStore, Score
from krx_quant.pricing import Direction
from krx_quant.replay import replay_into
from krx_quant.touch import TARGET_VERSION
from tests.algorithm_doubles import FixedAlgorithm
from tests.api_schema import check
from tests.replay_events import DAY, api, at, get_json, run
from tests.test_leaderboard_api import OUTCOMES as FORTY_OUTCOMES
from tests.test_leaderboard_api import SURE, touch

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
    """``algorithm``'s status since the morning of ``since``, on the touch target already (#170:
    the engine does not restart it)."""
    with stored(db) as store:
        store.add_status(algorithm, morning(since), None, Status(status), "set by the test")
        store.mark_target(TARGET_VERSION, morning(since))


def put_days(
    db: Path,
    days: list[date],
    calls: dict[str, list[Direction]],
    *,
    with_baselines: bool = True,
    model: str = "v1",
    target_version: int | None = TARGET_VERSION,
) -> None:
    """Each algorithm's ``calls`` on each of ``days`` (one sure call per bar close from 09:01,
    by ``model``, trained on ``target_version``: the touch target unless said), scored against
    OUTCOMES: the bar closes' touch labels (and, unread, the old bases' classes), with the
    baselines' unless they are there.
    """
    with stored(db) as store:
        store.record_models([(a, model, target_version) for a in calls])
        if with_baselines:
            calls = {**calls, **BASELINE_CALLS}
        for d in days:
            made = []
            for algorithm, directions in calls.items():
                version = "v1" if algorithm in BASELINE_CALLS else model
                for i, call in enumerate(directions):
                    t = datetime.combine(d, time(9, 1), KST) + timedelta(minutes=i)
                    at_target = t + timedelta(minutes=4)
                    made.append(
                        NewPrediction(
                            "005930", algorithm, version, t, t, SURE[call], at_target, 81_250, 81_250, 2
                        )
                    )
            pending = store.add_all(made).pending
            scores = []
            for p in pending:
                outcome = OUTCOMES[
                    (p.target_at - datetime.combine(d, time(9, 5), KST)) // timedelta(minutes=1)
                ]
                mid = 81_250 + {Direction.UP: 300, Direction.FLAT: 0, Direction.DOWN: -300}[outcome]
                scores.append((p.prediction_id, Score(mid, outcome, outcome, None)))
            store.score_all(scores, datetime.combine(d, time(15, 30), KST))
            first = datetime.combine(d, time(9, 1), KST)
            store.add_touches(
                [touch("005930", first + timedelta(minutes=i), o) for i, o in enumerate(OUTCOMES)]
            )


def last_change(algorithm: dict[str, Any]) -> tuple[str | None, str, str]:
    change = algorithm["status"]["history"][-1]
    return change["from"], change["to"], change["reason"]


async def test_probation_ends_after_ten_days_better_than_every_baseline(tmp_path: Path) -> None:
    db = tmp_path / "engine.db"
    history = weekdays_before(DAY, 9)  # with the replayed day, ten days on probation
    for name in ("alpha", "gamma"):
        put_status(db, name, "probation", history[0])
    put_status(db, "beta", "probation", history[-3])  # four days so far
    # Over always-flat, its weakest baseline, alpha gains 0.25 a day missing the downs and 0.5
    # right: over the nine days, a mean of 0.3889 and a standard deviation of 0.1318, so a
    # 98.3% lower bound of 0.3889 - 3.0028 (t at 99.15%, 8 degrees of freedom) x 0.1318 / 3
    # = 0.2570.
    put_days(db, history[:4], {"alpha": MISSES_DOWNS, "gamma": FLAT})
    put_days(db, history[4:], {"alpha": RIGHT, "gamma": FLAT})
    put_days(db, history[-3:], {"beta": MISSES_DOWNS}, with_baselines=False)

    engine = await review_day(db, [ALPHA, BETA, GAMMA])

    statuses = await listed(engine)
    assert last_change(statuses["alpha"])[:2] == ("probation", "active")
    assert last_change(statuses["alpha"])[2] == (
        "10 days on probation; weakest lift low 0.2570 by trading day at 98.3% (9 days scored)"
    )
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


async def test_probation_is_judged_on_its_tenth_twentieth_and_fortieth_days_only(tmp_path: Path) -> None:
    judged = {}
    for days in (11, 20, 30):  # with the replayed day
        db = tmp_path / f"{days}.db"
        history = weekdays_before(DAY, days - 1)
        put_status(db, "alpha", "probation", history[0])
        put_days(db, history, {"alpha": RIGHT})
        judged[days] = (await listed(await review_day(db, [ALPHA])))["alpha"]["status"]["status"]

    # Better than every baseline each day, but judged on the twentieth day only.
    assert judged == {11: "probation", 20: "active", 30: "probation"}


async def test_past_its_last_judged_day_probation_is_a_retirement_candidate(tmp_path: Path) -> None:
    db = tmp_path / "engine.db"
    history = weekdays_before(DAY, 39)  # with the replayed day, forty days on probation
    put_status(db, "alpha", "probation", history[0])
    put_days(db, history, {"alpha": FLAT})  # never better than always-flat
    outbox = Outbox()

    engine = await review_day(db, [ALPHA], notify=outbox)

    assert (await listed(engine))["alpha"]["status"]["status"] == "probation"  # retired only by command
    [alert] = outbox.sent
    assert "alpha 은퇴 후보: 수습 40거래일 동안 정식이 되지 못했습니다" in alert
    assert "krx-quant retire alpha" in alert


async def test_probation_needs_its_lift_day_after_day_not_on_a_few_days(tmp_path: Path) -> None:
    db = tmp_path / "engine.db"
    history = weekdays_before(DAY, 9)
    put_status(db, "alpha", "probation", history[0])
    # Right on two days, always-flat's calls on the other seven: over always-flat, a gain of
    # 0.5 on two days and none on the rest. Counted bar close by bar close (72 pairs), its
    # lower bound is above 0; counted day by day, it is not.
    put_days(db, history[:7], {"alpha": FLAT})
    put_days(db, history[7:], {"alpha": RIGHT})

    engine = await review_day(db, [ALPHA])

    assert (await listed(engine))["alpha"]["status"]["status"] == "probation"
    async with api(engine) as client:
        board = await get_json(client, "/v1/leaderboard?period=20d")
    [alpha] = [e for e in board["ranked"] + board["unranked"] if e["algorithm"] == "alpha"]
    assert alpha["lift"]["low"] > 0
    assert alpha["lift"]["day_low"] <= 0


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
    days = weekdays_before(DAY, 59)  # with the replayed day, sixty days on the bench: judged
    put_status(db, "alpha", "bench", days[0])
    put_status(db, "beta", "bench", days[0])
    put_days(db, days[:-20], {"alpha": WRONG, "beta": WRONG})
    put_days(db, days[-20:], {"alpha": WRONG, "beta": RIGHT})  # beta is good again
    outbox = Outbox()
    outbox.down = True  # Telegram is unreachable on the first day

    engine = await review_day(db, [ALPHA, BETA], notify=outbox)

    statuses = await listed(engine)
    assert last_change(statuses["beta"]) == (
        "bench",
        "active",
        "recovered: weakest lift low 0.5000 by trading day at 98.3% (19 days scored) over the last 20 days,"
        " 60 on the bench",
    )
    assert statuses["alpha"]["status"]["status"] == "bench"  # retired only by command
    [alert] = outbox.sent
    assert "alpha 은퇴 후보: 벤치에서 60거래일 동안 회복하지 못했습니다" in alert
    assert "krx-quant retire alpha" in alert

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


async def test_a_benched_algorithm_recovers_only_on_its_lift_day_after_day(tmp_path: Path) -> None:
    db = tmp_path / "engine.db"
    days = weekdays_before(DAY, 39)  # with the replayed day, forty days on the bench: judged
    put_status(db, "alpha", "bench", days[0])
    # Right on the last three days, always-flat's calls before: over the last 20 days (19 of
    # them scored), its lower bound over always-flat is above 0 bar close by bar close, not
    # day by day.
    put_days(db, days[:-3], {"alpha": FLAT})
    put_days(db, days[-3:], {"alpha": RIGHT})

    engine = await review_day(db, [ALPHA])

    assert (await listed(engine))["alpha"]["status"]["status"] == "bench"
    async with api(engine) as client:
        board = await get_json(client, "/v1/leaderboard?period=20d")
    [alpha] = [e for e in board["ranked"] + board["unranked"] if e["algorithm"] == "alpha"]
    assert alpha["lift"]["low"] > 0
    assert alpha["lift"]["day_low"] <= 0


async def test_the_bench_is_judged_on_its_twentieth_fortieth_and_sixtieth_days_only(tmp_path: Path) -> None:
    db = tmp_path / "engine.db"
    for name, n in (("alpha", 21), ("beta", 40)):  # with the replayed day
        days = weekdays_before(DAY, n - 1)
        put_status(db, name, "bench", days[0])
        put_days(db, days, {name: RIGHT}, with_baselines=name == "alpha")

    engine = await review_day(db, [ALPHA, BETA])

    statuses = await listed(engine)
    assert statuses["alpha"]["status"]["status"] == "bench"  # good again, not a day it is judged on
    assert last_change(statuses["beta"])[:2] == ("bench", "active")


async def test_the_stage_shows_the_days_counted_the_next_review_and_the_lift_it_judges_by(
    tmp_path: Path,
) -> None:
    history = weekdays_before(DAY, 9)  # with the replayed day, ten days on probation: judged at 15:40

    def with_history(db: Path) -> Path:
        put_status(db, "alpha", "probation", history[0])
        put_days(db, history[:4], {"alpha": MISSES_DOWNS})
        put_days(db, history[4:], {"alpha": RIGHT})
        return db

    # At 15:00, before the day's review: the nine days reviewed, and the tenth judged today.
    before = await run(
        with_history(tmp_path / "before.db"), [], until=at("15:00:00"), algorithms=[ALPHA], min_sample=20
    )
    stage = (await listed(before))["alpha"]["stage"]
    weakest = stage.pop("weakest")
    assert stage == {
        "status": "probation",
        "began": str(history[0]),
        "counted_from": str(history[0]),
        "days": 9,
        "review_days": [10, 20, 40],
        "next_review": 10,
        "next_review_on": str(DAY),
        "lift_days": 9,
        "level": 0.983,
        "enough": True,  # 72 scored, and as many pairs with each baseline
        "simulated": 0,  # none of them simulated (#298)
    }
    # The lower bound the review works out (test_probation_ends_after_ten_days_better_than_every_baseline).
    assert (weakest["baseline"], weakest["n"]) == ("baseline-flat", 9)
    assert weakest["value"] == pytest.approx(0.3889, abs=5e-5)
    assert weakest["low"] == pytest.approx(0.2570, abs=5e-5)

    after = await review_day(with_history(tmp_path / "after.db"), [ALPHA])
    alpha = (await listed(after))["alpha"]
    assert last_change(alpha)[2] == (
        f"10 days on probation; weakest lift low {weakest['low']:.4f} by trading day at 98.3%"
        f" ({weakest['n']} days scored)"
    )
    assert alpha["stage"] is None, "active: no review ahead to be made active by"


async def test_the_stage_counts_on_after_a_review_and_on_the_bench_takes_the_last_20_days(
    tmp_path: Path,
) -> None:
    db = tmp_path / "engine.db"
    history = weekdays_before(DAY, 29)
    put_status(db, "beta", "bench", history[0])  # with the replayed day, thirty days on the bench
    put_days(db, history, {"beta": RIGHT})
    put_status(db, "alpha", "probation", history[-6])  # and seven on probation
    put_days(db, history[-6:], {"alpha": RIGHT}, with_baselines=False)

    engine = await review_day(db, [ALPHA, BETA])  # the review at 15:40 judged neither

    async with api(engine) as client:
        body = await get_json(client, "/v1/algorithms")
    check("algorithms", body)
    stages = {a["name"]: a["stage"] for a in body["algorithms"]}
    assert stages["alpha"]["status"] == "probation"
    # Counted through today's review; the next counts the next trading day, Tuesday the 13th.
    assert (stages["alpha"]["days"], stages["alpha"]["next_review"]) == (7, 10)
    assert stages["alpha"]["next_review_on"] == "2026-10-15"
    assert (stages["alpha"]["lift_days"], stages["alpha"]["weakest"]["n"]) == (7, 6), "today has no outcomes"
    assert stages["alpha"]["enough"] is True
    assert stages["beta"]["status"] == "bench"
    assert (stages["beta"]["began"], stages["beta"]["days"]) == (str(history[0]), 30)
    assert (stages["beta"]["review_days"], stages["beta"]["next_review"]) == ([20, 40, 60], 40)
    assert stages["beta"]["next_review_on"] == "2026-10-26"
    # Its lift over the last 20 days with predictions, as the bench's review takes it.
    beta = stages["beta"]["weakest"]
    assert (stages["beta"]["lift_days"], beta["n"], beta["baseline"]) == (20, 19, "baseline-flat")
    assert beta["low"] == beta["value"] == 0.5


async def test_a_status_set_after_the_review_counts_from_the_next_day(tmp_path: Path) -> None:
    db = tmp_path / "engine.db"
    history = weekdays_before(DAY, 9)
    # Ten days with predictions, but the first one's review had run when probation began.
    with stored(db) as store:
        store.add_status("alpha", at("16:00:00", history[0]), None, Status.PROBATION, "set by the test")
        store.mark_target(TARGET_VERSION, at("16:00:00", history[0]))
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


# The switch to the touch target (#170): the deploy restarts every algorithm on probation, and
# its probation counts from the trading day it first runs a model trained on the new target.

OLD_MODEL, NEW_MODEL = "old", "new"


def old_engine_db(db: Path, statuses: dict[str, str], since: date) -> None:
    """A database an engine from before #170 wrote: ``statuses`` since the morning of ``since``,
    and no mark of the touch target."""
    with stored(db) as store:
        for name, status in statuses.items():
            store.add_status(name, morning(since), None, Status(status), "set by an older engine")


async def test_the_switch_restarts_every_algorithm_not_retired_on_probation_once(tmp_path: Path) -> None:
    """Input: an older engine's database, alpha active, beta benched, gamma retired, delta a
    clone. Output: the first engine of #170 to open it puts alpha, beta and delta back on
    probation, the reason saying why, gamma stays retired; an engine opening it again leaves
    them be."""
    db = tmp_path / "engine.db"
    since = weekdays_before(DAY, 30)[0]
    old_engine_db(db, {"alpha": "active", "beta": "bench", "gamma": "retired", "delta": "clone"}, since)

    deployed = await run(db, [], start=at("06:00:00"), until=at("06:01:00"), algorithms=[ALPHA, BETA, DELTA])
    again = await run(db, [], start=at("06:30:00"), until=at("06:31:00"), algorithms=[ALPHA, BETA, DELTA])

    statuses = await listed(again)
    for name, before in (("alpha", "active"), ("beta", "bench"), ("delta", "clone")):
        assert last_change(statuses[name]) == (
            before,
            "probation",
            target_changed("direction", TARGET_VERSION),
        )
        assert statuses[name]["status"]["since"] == "2026-10-12T06:00:00+09:00", "once, at the deploy"
    assert deployed.store.statuses()["gamma"].status is Status.RETIRED
    assert statuses["alpha"]["stage"]["counted_from"] is None, "waiting for a model of the touch target"


async def test_probation_counts_from_the_trading_day_the_swap_installed_a_model_of_the_target(
    tmp_path: Path,
) -> None:
    """Input: alpha, active for weeks on a model of the four-minute move, right throughout.
    Deployed on the morning of D0 (ten trading days before DAY): it predicts D0 with its old
    model; the morning swap of D1 installs a model of the touch target, right too. Output:
    DAY's review is its tenth day on probation (D1 to DAY), so it is judged and made active.
    Had D0 counted, DAY would be its eleventh, not a day it is judged on."""
    db = tmp_path / "engine.db"
    days = weekdays_before(DAY, 10)
    deploy = days[0]
    old_engine_db(db, {"alpha": "active"}, weekdays_before(deploy, 20)[0])
    put_days(db, weekdays_before(deploy, 15), {"alpha": RIGHT}, model=OLD_MODEL, target_version=1)
    calendar = MarketCalendar(dict.fromkeys([deploy, DAY], True))
    await run(db, [], start=at("06:00:00", deploy), until=at("06:01:00", deploy), calendar=calendar)
    put_days(db, days[:1], {"alpha": RIGHT}, model=OLD_MODEL, target_version=1)  # D0: the old model
    put_days(db, days[1:], {"alpha": RIGHT}, model=NEW_MODEL)  # from D1: the touch target's

    engine = await review_day(db, [ALPHA])

    alpha = (await listed(engine))["alpha"]
    assert [h["to"] for h in alpha["status"]["history"]] == ["active", "probation", "active"]
    assert last_change(alpha)[2].startswith("10 days on probation;")


async def test_an_algorithm_waits_until_the_swap_runs_a_model_of_the_target(tmp_path: Path) -> None:
    """Input: alpha and beta active on models of the four-minute move; deployed on DAY. The
    night's training replaced alpha's model (the morning swap of NEXT_DAY at 08:00) and failed
    for beta. Output: after DAY's review neither counts a day; after NEXT_DAY's, alpha counts
    from NEXT_DAY (one day, the next review on its tenth) and beta still waits. The engine
    shows each model's target."""
    db = tmp_path / "engine.db"
    old_engine_db(db, {"alpha": "active", "beta": "active"}, weekdays_before(DAY, 20)[0])
    old = {name: Model(f"{name}-old", {}, target_version=1) for name in ("alpha", "beta")}

    deploy_day = await run(
        db,
        [],
        start=at("06:00:00"),
        until=at("15:45:00"),
        calendar=THREE_DAYS,
        algorithms=[ALPHA, BETA],
        kind_algorithms=(),
        models=old,
    )
    waiting = await listed(deploy_day)
    assert {name: (a["stage"]["counted_from"], a["stage"]["days"]) for name, a in waiting.items()} == {
        "alpha": (None, 0),
        "beta": (None, 0),
    }
    assert waiting["alpha"]["stage"]["next_review_on"] is None

    engine = await run(
        db,
        [],
        start=at("07:00:00", NEXT_DAY),
        until=at("08:00:00", NEXT_DAY),
        calendar=THREE_DAYS,
        algorithms=[ALPHA, BETA],
        kind_algorithms=(),
        models=old,
    )
    assert engine.switch_models(
        {"alpha": Model("alpha-new", {}, target_version=TARGET_VERSION)}
    )  # the morning swap
    assert isinstance(engine.clock, FakeClock)
    await replay_into(engine, engine.clock, [], until=at("15:45:00", NEXT_DAY))

    now = await listed(engine)
    alpha, beta = now["alpha"], now["beta"]
    assert (alpha["stage"]["counted_from"], alpha["stage"]["days"]) == (str(NEXT_DAY), 1)
    assert alpha["stage"]["next_review"] == 10
    assert (beta["stage"]["counted_from"], beta["stage"]["days"]) == (None, 0)
    assert (alpha["model"]["target_version"], beta["model"]["target_version"]) == (TARGET_VERSION, 1)
    async with api(engine) as client:
        check("algorithms", await get_json(client, "/v1/algorithms"))


# The touch target's version 3 (#187): crossed quotes count. The deploy restarts the direction's
# algorithms once more, as #170 did, and probation counts from a model of version 3, not 2.


def version_2_engine_db(db: Path, statuses: dict[str, str], since: date) -> None:
    """A database an engine from #170 to #187 wrote: ``statuses`` since the morning of ``since``,
    restarted for the touch target's version 2 then (its mark)."""
    old_engine_db(db, statuses, since)
    with stored(db) as store:
        store.mark_target(2, morning(since))


async def test_version_3_restarts_every_direction_algorithm_not_retired_once(tmp_path: Path) -> None:
    """Input: a database of version 2 (#170 to #187): alpha active, beta benched, gamma retired,
    delta a clone. Output: the first engine of version 3 puts alpha, beta and delta back on
    probation, the reason naming version 3, gamma stays retired; an engine opening it again
    leaves them be; both versions' marks are kept."""
    db = tmp_path / "engine.db"
    since = weekdays_before(DAY, 30)[0]
    version_2_engine_db(db, {"alpha": "active", "beta": "bench", "gamma": "retired", "delta": "clone"}, since)

    deployed = await run(db, [], start=at("06:00:00"), until=at("06:01:00"), algorithms=[ALPHA, BETA, DELTA])
    again = await run(db, [], start=at("06:30:00"), until=at("06:31:00"), algorithms=[ALPHA, BETA, DELTA])

    statuses = await listed(again)
    for name, before in (("alpha", "active"), ("beta", "bench"), ("delta", "clone")):
        assert last_change(statuses[name]) == (before, "probation", target_changed("direction", 3))
        assert statuses[name]["status"]["since"] == "2026-10-12T06:00:00+09:00", "once, at the deploy"
        assert len(statuses[name]["status"]["history"]) == 2
    assert deployed.store.statuses()["gamma"].status is Status.RETIRED
    assert statuses["alpha"]["stage"]["counted_from"] is None, "waiting for a model of version 3"
    with stored(db) as store:
        assert store.target_marked(2) and store.target_marked(TARGET_VERSION)


async def test_after_version_3_probation_counts_from_the_swap_of_a_model_of_it_not_of_version_2(
    tmp_path: Path,
) -> None:
    """Input: alpha, active for weeks on a model of version 2, right throughout. Version 3 is
    deployed on the morning of D0 (ten trading days before DAY): alpha predicts D0 with its
    version 2 model; the morning swap of D1 installs one of version 3, right too. Output: DAY's
    review is its tenth day on probation (D1 to DAY): judged and made active. D0's predictions,
    a model of version 2's, are not counted."""
    db = tmp_path / "engine.db"
    days = weekdays_before(DAY, 10)
    deploy = days[0]
    version_2_engine_db(db, {"alpha": "active"}, weekdays_before(deploy, 20)[0])
    put_days(db, weekdays_before(deploy, 15), {"alpha": RIGHT}, model="v2-model", target_version=2)
    calendar = MarketCalendar(dict.fromkeys([deploy, DAY], True))
    await run(db, [], start=at("06:00:00", deploy), until=at("06:01:00", deploy), calendar=calendar)
    put_days(db, days[:1], {"alpha": RIGHT}, model="v2-model", target_version=2)  # D0: version 2's
    put_days(db, days[1:], {"alpha": RIGHT}, model=NEW_MODEL)  # from D1: version 3's

    engine = await review_day(db, [ALPHA])

    alpha = (await listed(engine))["alpha"]
    assert [h["to"] for h in alpha["status"]["history"]] == ["active", "probation", "active"]
    assert alpha["status"]["history"][1]["reason"] == target_changed("direction", TARGET_VERSION)
    assert last_change(alpha)[2].startswith("10 days on probation;")


async def test_a_model_of_another_target_neither_replaces_one_of_it_nor_joins_the_consensus(
    tmp_path: Path,
) -> None:
    """Input: alpha and beta active; alpha runs a model of the touch target, beta one of the
    four-minute move (a rollback by hand, say). Output: beta sits out of the consensus; alpha
    does not switch back to an old-target model or to its defaults (#170)."""
    db = tmp_path / "engine.db"
    store = PredictionStore(db)  # both active on the touch target already
    for name in ("alpha", "beta"):
        store.add_status(name, morning(weekdays_before(DAY, 30)[0]), None, Status.ACTIVE, "set by the test")
    store.mark_target(TARGET_VERSION, morning(weekdays_before(DAY, 30)[0]))
    store.close()
    engine = await run(
        db,
        [],
        start=at("06:00:00"),
        until=at("08:00:00"),
        calendar=THREE_DAYS,
        algorithms=[ALPHA, BETA],
        models={
            "alpha": Model("alpha-touch", {}, target_version=TARGET_VERSION),
            "beta": Model("beta-old", {}, target_version=1),
        },
        target_version=TARGET_VERSION,
    )

    assert engine.members() == ["alpha"]
    assert not engine.switch_models({"alpha": Model("alpha-old", {}, target_version=1)})
    assert not engine.switch_models({"alpha": Model("alpha-default", {})})
    assert engine.models["alpha"].version == "alpha-touch"
    assert engine.switch_models({"beta": Model("beta-touch", {}, target_version=TARGET_VERSION)})
    assert engine.members() == ["alpha", "beta"]


async def test_the_reviews_see_only_predictions_of_models_of_the_target(tmp_path: Path) -> None:
    """Input: alpha active on the touch target. Over the last 20 days with predictions (DAY's
    own among them, unscored) it was never right on the first ten with a model of the
    four-minute move, always right on the last nine with one of the touch target. Output: the
    active review takes the last 20 days' lift from the touch target's model alone (+0.5 over
    always-flat) and keeps it active; counted, the wrong days would bring its weakest lift to
    (9 − 10) × 0.5 / 19 < 0 and bench it."""
    worse = [Direction.DOWN, Direction.UP, Direction.UP, Direction.UP] * 2  # never right
    days = weekdays_before(DAY, 20)

    async def reviewed(db: Path, old_target: int) -> dict[str, Any]:
        put_status(db, "alpha", "active", weekdays_before(DAY, 30)[0])
        put_days(db, days[:11], {"alpha": worse}, model=OLD_MODEL, target_version=old_target)
        put_days(db, days[11:], {"alpha": RIGHT}, model=NEW_MODEL)
        listed_now: dict[str, Any] = (await listed(await review_day(db, [ALPHA])))["alpha"]
        return listed_now

    assert (await reviewed(tmp_path / "switched.db", 1))["status"]["status"] == "active"
    counted = await reviewed(tmp_path / "counted.db", TARGET_VERSION)  # as if the old model were the target's
    assert last_change(counted)[:2] == ("active", "bench")
