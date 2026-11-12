"""The ranking (#697, ``ranking.py``): each kind's algorithms ranked by their log-loss skill against the
kind's climate, by day, its lower bound; members above 0, entering and leaving after three rankings
running; too little evidence unranked. Made-up forecasts and answers, no price."""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from krx_quant.barriers import BARRIER, CLIMATE, DOWN_FIRST, TIMEOUT, UP_FIRST
from krx_quant.feeds.base import KST
from krx_quant.kinds import Answer
from krx_quant.predictions import NewKindPrediction, PredictionStore
from krx_quant.ranking import (
    BAR_CLOSE,
    DAILY,
    FEW_CALLS,
    FEW_DAYS,
    LIVE,
    NIGHTS,
    NO_EVIDENCE,
    RANKING_VERSION,
    WALK_FORWARD,
    DayScore,
    Evidence,
    Snapshot,
    chosen,
    rank,
    scores_of,
    text,
)

NIGHT = date(2026, 11, 12)
MADE = datetime(2026, 11, 12, 15, 40, tzinfo=KST)
DAYS = [date(2026, 10, 12) + timedelta(days=i) for i in range(30)]


def days(skills: list[float], n: int = 100, calls: int = 10, start: int = 0) -> tuple[DayScore, ...]:
    """Days of ``n`` predictions each, a day's mean skill each of ``skills``, ``calls`` calls half right."""
    return tuple(DayScore(DAYS[start + i], n, s * n, calls, calls // 2) for i, s in enumerate(skills))


def live(skills: list[float], **kw: int) -> Evidence:
    return Evidence(LIVE, days(skills, **kw))


def ranked(
    evidence: dict[str, Evidence | None], before: Snapshot | None = None, night: date = NIGHT
) -> Snapshot:
    statuses = dict.fromkeys(evidence, "shadow")
    return rank(BARRIER, night, MADE, evidence, statuses, before, BAR_CLOSE)


def row(snapshot: Snapshot, name: str) -> dict[str, object]:
    return next(p.as_json() for p in snapshot.rows if p.algorithm == name)


GOOD = [0.02, 0.03, 0.025, 0.035, 0.02, 0.03, 0.028, 0.022, 0.031, 0.026, 0.029, 0.024]
NOISY = [0.05, -0.04, 0.03, -0.05, 0.04, -0.03, 0.02, -0.02, 0.01, -0.01, 0.02, -0.03]
WORSE = [-0.02, -0.03, -0.01, -0.02, -0.025, -0.015, -0.02, -0.03, -0.01, -0.02, -0.02, -0.03]


def test_ranked_by_the_skills_lower_bound_and_members_are_those_above_zero() -> None:
    """Input: twelve days each of a steady skill (``good``), one as large on some days but noisy
    (``noisy``, mean near 0), one below the climate (``worse``), on the kind's first ranking. Output:
    ``good`` ranks first and is the one member; ``noisy`` ranks second, its lower bound below 0;
    ``worse`` last; each with its days, calls and precision."""
    found = ranked({"good": live(GOOD), "noisy": live(NOISY), "worse": live(WORSE)})

    assert [p.algorithm for p in found.rows] == ["good", "noisy", "worse"]
    assert [p.rank for p in found.rows] == [1, 2, 3]
    assert found.members == frozenset({"good"})
    good = row(found, "good")
    assert good["skill"] == pytest.approx(sum(GOOD) / len(GOOD))
    assert 0 < good["skill_low"] < good["skill"] < good["skill_high"]  # type: ignore[operator]
    assert (good["days"], good["calls"], good["precision"], good["call_rate"]) == (12, 120, 0.5, 0.1)
    assert (good["source"], good["status"], good["nights"], good["pending"]) == (LIVE, "shadow", 1, 0)
    assert row(found, "noisy")["skill_low"] < 0  # type: ignore[operator]
    assert found.through == DAYS[11]


def test_too_little_evidence_is_unranked_and_never_a_member() -> None:
    """Input: nine days (one fewer than ``BAR_CLOSE.min_days``) of a strong skill; twelve days of 2
    calls each (24 < 30); no evidence. Output: all three unranked, by why, after the ranked; none a
    member, so the horizon's verdict waits."""
    found = ranked(
        {
            "fresh": live([0.1] * 9),
            "quiet": live(GOOD, calls=2),
            "silent": None,
            "good": live(GOOD),
        }
    )

    assert [p.algorithm for p in found.rows] == ["good", "fresh", "quiet", "silent"]
    assert {p.algorithm: p.why for p in found.rows if not p.ranked} == {
        "fresh": FEW_DAYS,
        "quiet": FEW_CALLS,
        "silent": NO_EVIDENCE,
    }
    assert all(not p.member and p.rank is None and p.skill is None for p in found.rows if not p.ranked)
    assert ranked({"fresh": live([0.1] * 9)}).members == frozenset()


def test_the_walk_forward_stands_in_while_live_days_are_too_few() -> None:
    """Input: three live days and twelve walked-forward ones; then twelve live days and the walk-forward.
    Output: the walk-forward's first, live once it is enough; live as it is without a walk-forward."""
    walked = Evidence(WALK_FORWARD, days(GOOD))

    assert chosen(live(GOOD[:3]), walked, BAR_CLOSE) is walked
    assert chosen(live(GOOD), walked, BAR_CLOSE).source == LIVE  # type: ignore[union-attr]
    assert chosen(live(GOOD[:3]), None, BAR_CLOSE).source == LIVE  # type: ignore[union-attr]
    assert chosen(None, None, BAR_CLOSE) is None
    assert DAILY.min_days > len(DAYS)  # a daily kind's live days rank only after a year


def test_an_algorithm_enters_and_leaves_only_after_three_rankings_running() -> None:
    """Input: ``a`` not a member, then above 0 on three nights running; then below on two nights, above
    on one, below on three. Output: it enters on the third night above (pending 1, 2, then member), stays
    through a break of two, and leaves on the third night below running."""
    first = ranked({"a": live(WORSE), "b": live(GOOD)})
    assert first.members == {"b"}
    states = []
    before = first
    for i, skills in enumerate([GOOD, GOOD, GOOD, WORSE, WORSE, GOOD, WORSE, WORSE, WORSE]):
        before = ranked({"a": live(skills), "b": live(GOOD)}, before, NIGHT + timedelta(days=i + 1))
        p = row(before, "a")
        states.append((p["member"], p["pending"]))
    assert NIGHTS == 3
    assert states == [
        (False, 1),
        (False, 2),
        (True, 0),
        (True, 1),
        (True, 2),
        (True, 0),
        (True, 1),
        (True, 2),
        (False, 0),
    ]
    assert row(before, "a")["nights"] == 1


def test_a_new_algorithm_after_the_first_ranking_waits_its_three_nights() -> None:
    """Input: a ranking of ``b``, then ``c`` appears above 0. Output: ``c`` is not a member that night
    (the first ranking of the kind took each result as it was; a later one carries)."""
    first = ranked({"b": live(GOOD)})
    later = ranked({"b": live(GOOD), "c": live(GOOD)}, first, NIGHT + timedelta(days=1))

    assert first.members == {"b"}
    assert later.members == {"b"} and row(later, "c")["pending"] == 1


def test_the_snapshot_is_kept_once_and_read_back_whole(tmp_path: Path) -> None:
    """Input: a night's snapshot kept, then another of the same night and kind, then the next night's.
    Output: the first stays as it was; ``rankings`` reads each kind's latest, or as of a night; the body
    reads back as the snapshot, its log line names its members and its unranked."""
    store = PredictionStore(tmp_path / "engine.db")
    one = ranked({"good": live(GOOD), "fresh": live([0.1] * 3)})
    other = ranked({"worse": live(WORSE)})
    after = ranked({"good": live(GOOD)}, one, NIGHT + timedelta(days=1))

    def kept(s: Snapshot) -> tuple[date, str, int, datetime, date | None, dict[str, object]]:
        return (s.night, s.kind, s.version, s.made_at, s.through, s.as_json())

    assert store.add_rankings([kept(one)]) == 1
    assert store.add_rankings([kept(other)]) == 0
    assert store.add_rankings([kept(after)]) == 1

    assert [Snapshot.of_json(b) for b in store.rankings(RANKING_VERSION, NIGHT)] == [one]
    latest = store.rankings(RANKING_VERSION)
    assert [b["night"] for b in latest] == [(NIGHT + timedelta(days=1)).isoformat()]
    assert store.rankings(RANKING_VERSION + 1) == []
    line = text(one)
    assert "members good" in line and "fresh (few_days, 3d)" in line
    store.close()


def _stored(
    store: PredictionStore,
    day: date,
    minute: int,
    name: str,
    p: tuple[float, float, float],
    label: str,
    lifecycle: str | None = None,
) -> None:
    at = datetime.combine(day, datetime.min.time(), KST) + timedelta(hours=9, minutes=minute)
    store.add_kind_predictions(
        BARRIER.name,
        [
            NewKindPrediction(
                "005930",
                name,
                f"{name}-v1",
                at,
                at,
                dict(zip(BARRIER.p_keys(), p, strict=True)),
                lifecycle=lifecycle,
            )
        ],
    )
    store.add_kind_answers(
        BARRIER.name, BARRIER.answer_version, [("005930", at, Answer(label, None, {}), False)]
    )


def test_live_scores_read_every_lifecycle_against_the_climate(tmp_path: Path) -> None:
    """Input: on two days, two bar closes each: the climate a third each, a shadow algorithm 0.6 on the
    class that happened (up first, then down first), its model trained on the barrier's answers; one
    special answer's prediction aside. Output: its day's skill is ln 3 − ln(1/0.6) a prediction, its calls
    and hits both up and down; the climate is not ranked."""
    store = PredictionStore(tmp_path / "engine.db")
    third = (1 / 3, 1 / 3, 1 / 3)
    for day in DAYS[:2]:
        for minute, label, p in ((1, UP_FIRST, (0.6, 0.2, 0.2)), (2, DOWN_FIRST, (0.2, 0.2, 0.6))):
            _stored(store, day, minute, CLIMATE, third, label)
            _stored(store, day, minute, "brei", p, label, lifecycle="shadow")
        _stored(store, day, 3, "brei", (0.2, 0.6, 0.2), TIMEOUT, lifecycle="shadow")  # no climate there
    store.record_models([("brei", "brei-v1", BARRIER.answer_version)])

    with store.reading() as db:
        found = scores_of(db, BARRIER, DAYS[:2], {"brei"})

    assert set(found) == {"brei"}
    first = found["brei"][0]
    assert (first.day, first.n, first.calls, first.hits) == (DAYS[0], 2, 2, 2)
    assert first.skill == pytest.approx(2 * (math.log(3) - math.log(1 / 0.6)))
    store.close()


def test_the_first_ranking_with_evidence_takes_its_results_as_they_are() -> None:
    """Input: a night none could be ranked (no evidence yet, as the first night after the deploy), then a
    night ``good`` has its evidence. Output: ``good`` is a member that night: no earlier state to keep."""
    empty = ranked({"good": None})
    later = ranked({"good": live(GOOD)}, empty, NIGHT + timedelta(days=1))

    assert empty.members == frozenset() and later.members == {"good"}


def test_once_settled_a_night_without_a_ranked_algorithm_keeps_the_hysteresis() -> None:
    """Input (Codex review): ``good`` a member, then two nights it cannot be ranked (no evidence), then its
    evidence back. Output: it stays a member through the first unranked night (pending 1), leaves only on
    the third night running not above 0, and is not taken back at once when its evidence returns."""
    first = ranked({"good": live(GOOD)})
    states = []
    before = first
    for i, found in enumerate([None, None, None, live(GOOD)]):
        before = ranked({"good": found}, before, NIGHT + timedelta(days=i + 1))
        assert before.settled
        p = row(before, "good")
        states.append((p["member"], p["pending"]))

    assert states == [(True, 1), (True, 2), (False, 0), (False, 1)]
