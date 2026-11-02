"""Challengers enter as a cohort and are judged once, on the review night (#473, spec #468 5 "도전
정책", ``trader/cohort.py``, ``promotion.promote``).

Input: the trader's store as the trader and the nightly job write it (``tests/trader_doubles.py``,
``tests/test_trader_promotion.py``'s records): the evaluation version begun on the night of 08-31,
its first review cycle from 09-01 to 09-28 (its 20th weekday; the calendar guesses the weekdays
trade), the next from 09-29 to 10-26. Every P&L is made up.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

from krx_quant.feeds.base import KST
from krx_quant.market_calendar import MarketCalendar
from krx_quant.trader import cohort, promotion, review
from krx_quant.trader import policy as policies
from krx_quant.trader.promotion import BETWEEN_REVIEWS, FEW_DAYS, NO_COHORT, REVIEWED
from krx_quant.trader.refit import form_now
from krx_quant.trader.store import DB_NAME, read_trader
from tests.api_schema import check
from tests.test_trader_promotion import CLOSE_CALL, LONG, WEEKDAYS, day_ran, judged_at, night, ran, setup
from tests.trader_doubles import BARRIER, CALENDAR, at, keep, policy_cell

# The first cycle's review day and the next cycle's first and review days.
REVIEW, NEXT, NEXT_REVIEW = WEEKDAYS[19], WEEKDAYS[20], LONG[39]


def connected(state: Path) -> sqlite3.Connection:
    return policies.connect(state / DB_NAME)


def tonight(state: Path, now: datetime) -> cohort.Tonight:
    db = connected(state)
    try:
        return cohort.form_tonight(db, now, CALENDAR.session)
    finally:
        db.close()


def verdicts(state: Path) -> list[tuple[int, int, str, str]]:
    with closing(sqlite3.connect(state / DB_NAME)) as db:
        return [tuple(r) for r in db.execute("SELECT * FROM cohort_verdicts ORDER BY evaluation, cycle")]


# When it is judged.


def test_no_night_of_the_cycle_judges_and_its_review_night_judges_once(tmp_path: Path) -> None:
    """Policy 2 beats the champion from its tenth common day (``CLOSE_CALL``: lower bound +63 won),
    but the nights of the cycle before its review judge nothing and keep no verdict; its review night
    judges it once and keeps the verdict; run again that night, or the next night, nothing more."""
    setup(tmp_path, CLOSE_CALL)

    before = [judged_at(tmp_path, night(d)) for d in WEEKDAYS[9:19]]
    assert {(b.deferred, b.judgments, b.verdict) for b in before} == {(BETWEEN_REVIEWS, (), None)}
    assert verdicts(tmp_path) == []

    done = judged_at(tmp_path, night(REVIEW))
    again = judged_at(tmp_path, night(REVIEW).replace(hour=23))
    after = judged_at(tmp_path, night(NEXT))

    assert (done.promoted, done.verdict is not None) == (2, True)
    assert done.verdict is not None and (done.verdict.cycle, done.verdict.outcome) == (1, cohort.PROMOTED)
    assert (again.deferred, again.verdict) == (REVIEWED, done.verdict), "the verdict as it was kept"
    assert (after.deferred, after.review) == (BETWEEN_REVIEWS, NEXT_REVIEW)
    assert len(verdicts(tmp_path)) == 1


def test_a_review_night_without_a_cohort_judges_nothing(tmp_path: Path) -> None:
    """A cycle that began without a cohort (the first after the deploy: the trader begun it): its
    review night has nothing to judge, and the champion stays."""
    keep(tmp_path, at("2026-08-31 22:31:00"), policy_cell(BARRIER, 0.8))
    ran(tmp_path, 1, [0.0] * 20, champion=True)

    done = judged_at(tmp_path, night(REVIEW))

    assert (done.deferred, done.promoted, done.review) == (NO_COHORT, None, REVIEW)
    assert "이 주기에 돈 도전 정책 묶음이 없다" in promotion.report(done)


# The cohort.


def test_a_cohort_takes_the_three_newest_with_their_own_values_and_fixes_its_champion(
    tmp_path: Path,
) -> None:
    """On the review night six policies wait (kept as the nightly re-fit keeps them, ``keep``; their
    θ made up): 7, the newest, has the champion's values; 6, 5 and 3 enter (newest first; 4 has 5's
    values, alike); 2, older, is passed over. Every one passed over is dropped. The champion it is
    judged against is the champion then, 1."""
    keep(tmp_path, at("2026-08-31 22:31:00"), policy_cell(BARRIER, 0.8))
    for n, threshold in enumerate((0.41, 0.42, 0.43, 0.43, 0.45, 0.8)):  # versions 2–7
        keep(tmp_path, at(f"{WEEKDAYS[n]} 22:31:00"), policy_cell(BARRIER, threshold))

    found = tonight(tmp_path, night(REVIEW) + timedelta(minutes=5))

    assert found.formed and found.cohort is not None
    c = found.cohort
    assert (c.evaluation, c.cycle, c.first, c.review, c.champion) == (2, 2, NEXT, NEXT_REVIEW, 1)
    assert c.members == (6, 5, 3)
    assert [(p["version"], p["why"]) for p in c.passed_over] == [
        (7, cohort.CHAMPION_VALUES),
        (4, cohort.ALIKE),
        (2, cohort.OLDER),
    ]
    with closing(connected(tmp_path)) as db:
        assert policies.challenger_versions(db) == [3, 5, 6]
        assert policies.waiting_versions(db) == []
        assert policies.running_versions(db, REVIEW, CALENDAR.session) == [], "not in the cycle reviewed"
        assert policies.running_versions(db, NEXT, CALENDAR.session) == [3, 5, 6], (
            "from the next cycle's first day"
        )
        assert policies.running_versions(db, NEXT_REVIEW, CALENDAR.session) == [3, 5, 6]
        assert policies.running_versions(db, LONG[40], CALENDAR.session) == [], "nor after its review day"
    again = tonight(tmp_path, night(REVIEW) + timedelta(minutes=9))
    assert (again.formed, again.cohort) == (False, c), "one cohort a cycle"


def test_a_policy_fitted_during_a_cycle_waits_for_the_next_cohort(tmp_path: Path) -> None:
    """Policy 3 kept on the fifth night of the cycle: no cohort is formed then (the cycle began), it
    does not run, and it enters on the review night's end, after policy 2's cohort was judged."""
    setup(tmp_path, [0.0] * 10)
    keep(tmp_path, at(f"{WEEKDAYS[4]} 22:31:00"), policy_cell(BARRIER, 0.45))

    mid = tonight(tmp_path, at(f"{WEEKDAYS[4]} 22:35:00"))
    with closing(connected(tmp_path)) as db:
        running = policies.running_versions(db, WEEKDAYS[5], CALENDAR.session)
    judged_at(tmp_path, night(REVIEW))
    end = tonight(tmp_path, night(REVIEW) + timedelta(minutes=5))

    assert mid.formed is False and mid.cohort is not None and mid.cohort.members == (2,)
    assert mid.waiting == (3,) and running == [2]
    assert end.formed and end.cohort is not None and end.cohort.members == (3,)


def test_the_night_s_end_forms_no_cohort_in_a_cycle_begun_without_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The first cycle after the deploy began at the trader's start (the evaluation begun at 06:05 of
    09-01): its nights form none, and the fits wait for its review night. A fresh store whose
    evaluation the nightly job began has its first cycle ahead: its night forms the first cohort."""
    begun_at = at("2026-09-01 06:05:00")
    with closing(connected(tmp_path)) as db:
        review.begin(db, begun_at, CALENDAR.session)
    keep(tmp_path, at("2026-09-01 22:31:00"), policy_cell(BARRIER, 0.8))
    keep(tmp_path, at("2026-09-01 22:32:00"), policy_cell(BARRIER, 0.45))

    form_now(tmp_path / DB_NAME, CALENDAR.session, at("2026-09-01 22:40:00"))

    out = capsys.readouterr().out
    assert (
        "도전 정책 묶음 (#473): 주기 1(2026-09-01–2026-09-28)은 묶음 없이 시작했다(주기 중간에는 넣지 "
        "않는다) · 다음 묶음을 기다리는 정책 1개: 검토 날 2026-09-28 밤에 새것부터 셋까지 들어간다" in out
    )
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    keep(fresh, at("2026-08-31 22:31:00"), policy_cell(BARRIER, 0.8))  # begins it: first day 09-01
    keep(fresh, at("2026-08-31 22:32:00"), policy_cell(BARRIER, 0.45))
    form_now(fresh / DB_NAME, CALENDAR.session, at("2026-08-31 22:40:00"))
    assert (
        "도전 정책 묶음 (#473): 주기 1(2026-09-01–2026-09-28)의 묶음을 만들었다: 정책 2(새것부터) · "
        "비교할 챔피언 정책 1(고정) · 주기 동안 모의로 돌고 검토 날 2026-09-28 밤에 한 번 판정한다"
        in capsys.readouterr().out
    )


def test_a_stale_challenger_and_a_policy_waiting_from_before_the_evaluation_are_dropped(
    tmp_path: Path,
) -> None:
    """The store as #400 left it: policy 1 the champion, policy 2 admitted to a running slot, policy 3
    waiting, all from before the evaluation version began (#469). The first night after the market
    drops 2 and 3: neither can enter a cohort; a policy kept since waits."""
    db = connected(tmp_path)
    try:
        for n in range(3):
            policies.add(
                db, at(f"2026-08-2{n} 22:31:00"), "#400", [policy_cell(BARRIER, 0.6 + n / 10)], [], {}
            )
        with db:
            db.execute("UPDATE policies SET admitted_at = ? WHERE id = 2", ("2026-08-21T22:40:00+09:00",))
        review.begin(db, at("2026-08-31 06:05:00"), CALENDAR.session)
    finally:
        db.close()
    keep(tmp_path, at("2026-08-31 22:31:00"), policy_cell(BARRIER, 0.45))

    done = judged_at(tmp_path, at("2026-08-31 22:35:00"))

    assert done.retired == (2, 3)
    with closing(connected(tmp_path)) as db:
        assert (policies.challenger_versions(db), policies.waiting_versions(db)) == ([], [4])
    assert (
        "묶음에 들 수 없는 정책을 내렸다(묶음 밖에서 돌던 도전 정책, 평가 버전 전에 맞춘 기다리는 정책): "
        in (promotion.report(done))
    )


# How it is judged.


def test_days_without_a_trade_and_days_a_limit_stopped_count_and_bonferroni_is_the_cohort_s(
    tmp_path: Path,
) -> None:
    """A cohort of one: policy 2 (``CLOSE_CALL``: seven days +2,000, three −1,000, as a day its
    simulated daily limit stopped it would be, #471: never ``stopped``; the champion trading on none
    of them, 0 each) against the champion. Its lower bound at 95% (one member: no correction) is
    +63.35 won: crowned. Policies kept meanwhile and waiting (3, 4) are not in the cohort, and do not
    widen the correction."""
    setup(tmp_path, CLOSE_CALL)
    for n in range(2):
        keep(tmp_path, at(f"{WEEKDAYS[n]} 22:31:00"), policy_cell(BARRIER, 0.45 - n / 100))

    done = judged_at(tmp_path, night(REVIEW))

    [j] = done.judgments
    assert (j.days, j.challengers, j.level, j.review) == (10, 1, 0.95, REVIEW)
    assert j.low == pytest.approx(63.35, abs=0.01)
    assert done.promoted == 2
    assert done.verdict is not None
    assert done.verdict.members == (
        {
            "version": 2,
            "days": 10,
            "mean": pytest.approx(1_100),
            "low": pytest.approx(63.35, abs=0.01),
            "verdict": "promoted",
        },
    )


def test_a_day_the_champion_was_paused_is_left_out_of_the_cohort_s_judgment(tmp_path: Path) -> None:
    """Ten days of +1,000, the champion paused on one (``stopped``): nine common days, undecided."""
    setup(tmp_path, [1_000.0] * 10)
    with closing(sqlite3.connect(tmp_path / DB_NAME)) as db:
        db.execute(
            "UPDATE policy_days SET stopped = 1 WHERE policy_id = 1 AND day = ?", (WEEKDAYS[4].isoformat(),)
        )
        db.commit()

    done = judged_at(tmp_path, night(REVIEW))

    [j] = done.judgments
    assert (j.days, j.why, done.promoted, done.dropped) == (9, FEW_DAYS, None, (2,))


# Undecided, missed: dropped, never judged again.


def test_an_undecided_member_is_dropped_and_its_window_is_never_stretched(tmp_path: Path) -> None:
    """Policy 2 ran nine days of the cycle, each +1,000 (the trader down the rest): undecided at the
    review, dropped. Days it would have run after the review (none: it does not run) never judge it."""
    setup(tmp_path, [1_000.0] * 9)

    done = judged_at(tmp_path, night(REVIEW))
    for day in LONG[20:30]:
        day_ran(tmp_path, 1, day, 0.0, champion=True)
    later = [judged_at(tmp_path, night(d)) for d in LONG[20:40]]

    assert (done.verdict is not None, done.promoted, done.dropped) == (True, None, (2,))
    assert done.verdict is not None and done.verdict.members[0]["verdict"] == cohort.UNDECIDED
    assert {n.deferred for n in later} == {BETWEEN_REVIEWS, NO_COHORT}
    with closing(connected(tmp_path)) as db:
        assert (
            policies.running_versions(db, NEXT, CALENDAR.session) == []
            and policies.challenger_versions(db) == []
        )
    assert len(verdicts(tmp_path)) == 1


def test_a_missed_review_night_closes_the_cohort_unjudged_and_the_champion_stays(tmp_path: Path) -> None:
    """The job did not run on the review night: the next night closes policy 2's cohort unjudged
    (``missed``), its members dropped, though it would have passed; it never runs nor is judged on a
    later night, and the champion stays until the next review."""
    setup(tmp_path, CLOSE_CALL)

    after = judged_at(tmp_path, night(NEXT))
    review_night = judged_at(tmp_path, night(NEXT_REVIEW))

    [missed] = after.missed
    assert (missed.cycle, missed.outcome, missed.promoted) == (1, cohort.MISSED, None)
    assert missed.members == ({"version": 2, "days": None, "mean": None, "low": None, "verdict": "missed"},)
    assert after.deferred == BETWEEN_REVIEWS
    assert "주기 1의 묶음은 검토 날 밤에 판정하지 못해 판정 없이 내렸다(창을 늘리지 않는다): 정책 2" in (
        promotion.report(after)
    )
    assert review_night.deferred == NO_COHORT and review_night.missed == ()
    with closing(connected(tmp_path)) as db:
        assert policies.champion_version(db) == 1
        assert (
            policies.running_versions(db, NEXT, CALENDAR.session) == []
            and policies.challenger_versions(db) == []
        )


def test_a_holiday_announced_after_the_cohort_was_formed_moves_its_review_and_its_running(
    tmp_path: Path,
) -> None:
    """Policy 2's cohort was formed for the cycle from 09-01, its review day then 09-28 (its 20th
    weekday). 09-25 is announced closed afterwards: on the calendar now the 20th trading day is 09-29.
    The night of 09-28 judges nothing and closes nothing; policy 2 runs on 09-29; the night of 09-29
    judges it, never ``missed``. The cohort as formed stays kept; the API counts its days on the
    calendar now."""
    setup(tmp_path, CLOSE_CALL)
    later = MarketCalendar({date(2026, 9, 25): False})
    moved = date(2026, 9, 29)

    db = connected(tmp_path)
    try:
        before = promotion.promote(db, night(REVIEW), later.session)
        running = policies.running_versions(db, moved, later.session)
        done = promotion.promote(db, night(moved), later.session)
        kept = cohort.of(db, 2, 1)
    finally:
        db.close()

    assert (before.deferred, before.missed, before.review) == (BETWEEN_REVIEWS, (), moved)
    assert running == [2], "it runs to its review day on the calendar now"
    assert done.missed == () and done.verdict is not None
    assert (done.verdict.outcome, done.promoted, done.review) == (cohort.PROMOTED, 2, moved)
    assert [j.review for j in done.judgments] == [moved]
    assert kept is not None and kept.review == REVIEW, "the cohort as formed"
    [shown] = policies.read_policies(tmp_path / DB_NAME, 10, sessions=later.session)["cohorts"]
    assert shown["review"] == moved.isoformat()
    assert (
        "주기 1 묶음의 검토 날 2026-09-29"
        in policies.read_policies(tmp_path / DB_NAME, 10)["champions"][0]["reason"]
    )


# Kept for good.


def test_the_verdict_is_kept_and_never_changes(tmp_path: Path) -> None:
    """A cohort's verdict, and the cohort as formed, are written once: an update or a deletion is
    refused, a trade kept later for a day of its cycle changes nothing, and the review night run again
    gives it as it was."""
    setup(tmp_path, CLOSE_CALL, [0.0] * 10)
    done = judged_at(tmp_path, night(REVIEW))
    kept = verdicts(tmp_path)

    with closing(sqlite3.connect(tmp_path / DB_NAME)) as db:
        for statement in (
            "UPDATE cohort_verdicts SET promoted_id = 3",
            "DELETE FROM cohort_verdicts",
            "UPDATE cohorts SET champion_id = 3",
            "DELETE FROM cohort_members",
        ):
            with pytest.raises(sqlite3.DatabaseError, match="kept as written"):
                db.execute(statement)
    day_ran(tmp_path, 3, WEEKDAYS[5], 50_000.0, champion=False)
    again = judged_at(tmp_path, night(REVIEW).replace(hour=23))

    assert done.verdict is not None and done.verdict.level == pytest.approx(1 - 0.05 / 2)
    assert verdicts(tmp_path) == kept
    assert (again.deferred, again.verdict) == (REVIEWED, done.verdict)


def test_the_cohorts_and_their_verdicts_through_the_api(tmp_path: Path) -> None:
    """``GET /v1/trader/policies``'s ``cohorts`` (newest first, each with its verdict) and
    ``GET /v1/trader``'s ``cohort`` (the one formed last), within the /v1 contract: policy 2 (+1,000
    a day) crowned, policy 3 (three days) undecided; policy 4, kept during the cycle, the next
    cycle's cohort, against policy 2."""
    setup(tmp_path, [1_000.0] * 10, [0.0] * 3)
    keep(tmp_path, at(f"{WEEKDAYS[3]} 22:31:00"), policy_cell(BARRIER, 0.45))
    judged_at(tmp_path, night(REVIEW))
    form_now(tmp_path / DB_NAME, CALENDAR.session, night(REVIEW) + timedelta(minutes=5))

    found = policies.read_policies(tmp_path / DB_NAME, 10)
    state = read_trader(tmp_path / DB_NAME, datetime.combine(NEXT, time(10), KST))

    second, first = found["cohorts"]
    assert (second["cycle"], second["start"], second["members"], second["champion"]) == (
        2,
        NEXT.isoformat(),
        [4],
        2,
    )
    assert second["verdict"] is None
    assert (first["cycle"], first["start"], first["review"], first["members"], first["champion"]) == (
        1,
        "2026-09-01",
        "2026-09-28",
        [3, 2],
        1,
    )
    verdict = first["verdict"]
    assert (verdict["outcome"], verdict["promoted"], verdict["champion"]) == ("promoted", 2, 1)
    assert verdict["level"] == pytest.approx(0.975)
    assert verdict["members"] == [
        {"version": 2, "days": 10, "mean": 1_000.0, "low": 1_000.0, "verdict": "promoted"},
        {"version": 3, "days": 3, "mean": 0.0, "low": 0.0, "verdict": "undecided"},
    ]
    assert state["cohort"] == second
    [champion, challenger] = state["policies"]
    assert (champion["version"], challenger["version"], challenger["promotion"]["review"]) == (
        2,
        4,
        NEXT_REVIEW.isoformat(),
    )
    assert [(p["version"], p["status"]) for p in found["policies"]] == [
        (4, "challenger"),
        (3, "dropped"),
        (2, "champion"),
        (1, "former"),
    ]
    assert found["champions"][0]["detail"]["cycle"] == 1
    check("trader_policies", found)
    check("trader", state)


def test_a_store_from_before_the_cohorts_reads_without_them(tmp_path: Path) -> None:
    """A store without the cohort tables (#473; read-only, as the Engine API reads it) gives none."""
    keep(tmp_path, at("2026-08-31 22:31:00"), policy_cell(BARRIER, 0.8))
    with closing(sqlite3.connect(tmp_path / DB_NAME)) as db:
        for table in ("cohort_verdicts", "cohort_members", "cohorts"):
            db.execute(f"DROP TABLE {table}")
        db.commit()

    found = policies.read_policies(tmp_path / DB_NAME, 10)

    assert (found["champion"], found["cohorts"]) == (1, [])
    assert read_trader(tmp_path / DB_NAME, at("2026-09-01 10:00:00"))["cohort"] is None
