"""The Stats Engine of a kind besides the direction (#179), on a small example worked by hand.

The ten predictions of ``tests/test_stats.py``, of a kind whose classes are small, medium
and large (the direction's up, flat and down in place), its action classes the two ends:

| # | p_small/p_medium/p_large | predicted | answer | correct |
|---|---|---|---|---|
| 1 | .6 .3 .1 | small | small | yes |
| 2 | .5 .4 .1 | small | medium | no |
| 3 | .7 .2 .1 | small | large | no |
| 4 | .1 .3 .6 | large | large | yes |
| 5 | .2 .3 .5 | large | small | no |
| 6 | .2 .6 .2 | medium | medium | yes |
| 7 | .1 .8 .1 | medium | medium | yes |
| 8 | .3 .4 .3 | medium | small | no |
| 9 | .25 .5 .25 | medium | large | no |
| 10 | 0 1 0 | medium | medium | yes |

- precision of the action classes 2/5 (Wilson 95%: 0.1176–0.7693), coverage 5/10, accuracy
  5/10, Brier 0.5375, as the direction's;
- with the middle class the only action class: precision 3/5 (#6, 7, 10 of #6–10), coverage
  5/10;
- against the kind's baseline "always medium" (right on #2, 6, 7, 10): lift 0.1; repeated
  three times (30 pairs) its 95% interval is 0.1 ± 0.196 and its one-sided p 0.1587.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

import pytest

from krx_quant.feeds import KST
from krx_quant.kind_stats import entry, entry_json, kind_scoring, leaderboard, scored_of_kind, summarize
from krx_quant.kinds import Kind, KindScored
from krx_quant.pricing import Direction
from krx_quant.stats import Scored
from krx_quant.stats import entry as direction_entry
from krx_quant.stats import summarize as direction_summarize
from tests.kind_doubles import drift_of, share_of_rises

T0 = datetime(2026, 10, 12, 9, 31, tzinfo=KST)
RANGE = Kind(
    name="sizes",
    title="test",
    classes=("small", "medium", "large"),
    class_titles=("a", "b", "c"),
    action=("small", "large"),
    answer_version=1,
    answer=drift_of,
)
MIDDLE = Kind(
    name="middle",
    title="test",
    classes=("small", "medium", "large"),
    class_titles=("a", "b", "c"),
    action=("medium",),
    answer_version=1,
    answer=drift_of,
    stats=share_of_rises,
)
EXAMPLE = [
    ((0.6, 0.3, 0.1), 0),
    ((0.5, 0.4, 0.1), 1),
    ((0.7, 0.2, 0.1), 2),
    ((0.1, 0.3, 0.6), 2),
    ((0.2, 0.3, 0.5), 0),
    ((0.2, 0.6, 0.2), 1),
    ((0.1, 0.8, 0.1), 1),
    ((0.3, 0.4, 0.3), 0),
    ((0.25, 0.5, 0.25), 2),
    ((0.0, 1.0, 0.0), 1),
]


def records(
    name: str = "alg", repeat: int = 1, calls: list[tuple[float, float, float]] | None = None
) -> list[KindScored]:
    out = []
    for i in range(repeat * len(EXAMPLE)):
        p, outcome = EXAMPLE[i % len(EXAMPLE)]
        probabilities = calls[i % len(calls)] if calls else p
        out.append(
            KindScored(name, "v1", "005930", T0 + timedelta(minutes=i), probabilities, outcome, {}, {})
        )
    return out


def always_medium(repeat: int = 1) -> list[KindScored]:
    return records("medium-always", repeat, [(0.0, 1.0, 0.0)])


def test_precision_and_coverage_count_the_kind_s_action_classes() -> None:
    ends = summarize(records(), RANGE)
    middle = summarize(records(), MIDDLE)

    assert (ends.n, ends.accuracy and ends.accuracy.value) == (10, 0.5)
    assert ends.precision is not None and ends.precision.value == 0.4
    assert ends.precision.low == pytest.approx(0.1176, abs=1e-4)
    assert ends.precision.high == pytest.approx(0.7693, abs=1e-4)
    assert ends.coverage is not None and ends.coverage.value == 0.5
    assert ends.brier is not None and ends.brier.value == pytest.approx(0.5375)
    assert middle.precision is not None and middle.precision.value == 0.6
    assert middle.coverage is not None and middle.coverage.value == 0.5
    assert list(ends.calibration_by_class) == ["small", "medium", "large"]


def test_the_lift_is_over_the_kind_s_own_baselines() -> None:
    e = entry("alg", records(repeat=3), {"medium-always": always_medium(3)}, RANGE)

    lift = e.lifts["medium-always"].lift
    assert lift.n == 30
    assert lift.value == pytest.approx(0.1)
    assert lift.low == pytest.approx(0.1 - 1.959964 * 0.1, abs=1e-6)
    assert lift.p == pytest.approx(1 - 0.841345, abs=1e-6)
    assert e.weakest_lift == pytest.approx(0.1)


def test_a_kind_with_the_direction_s_classes_scores_as_the_direction() -> None:
    """The same probabilities and answers as the direction's records (up, flat, down in the
    small, medium, large places): every statistic the same."""
    direction = [Direction.UP, Direction.FLAT, Direction.DOWN]
    scored = [
        Scored("alg", "v1", "005930", r.bar_close, *r.probabilities, direction[r.outcome])
        for r in records(repeat=3)
    ]
    flat = [
        Scored("flat", "v1", "005930", r.bar_close, 0.0, 1.0, 0.0, direction[r.outcome])
        for r in records(repeat=3)
    ]
    theirs = direction_summarize(scored, 0.00015)
    ours = summarize(records(repeat=3), RANGE)

    assert (ours.accuracy, ours.precision, ours.coverage, ours.brier) == (
        theirs.accuracy,
        theirs.precision,
        theirs.coverage,
        theirs.brier,
    )
    assert ours.calibration == theirs.calibration
    assert [ours.calibration_by_class[c] for c in RANGE.classes] == [
        theirs.calibration_by_class[d] for d in direction
    ]
    by_direction = direction_entry("alg", scored, {"flat": flat}, 0.00015, 0.983)
    by_kind = entry("alg", records(repeat=3), {"flat": always_medium(3)}, RANGE, 0.983)
    assert by_kind.lifts == by_direction.lifts


def test_a_kind_s_own_statistics_come_with_its_entry() -> None:
    e = entry("alg", records(), {}, MIDDLE)

    assert e.summary.extra == {"rises": 3, "rise_share": 0.3}  # class 2 answered on #3, 4, 9
    body = entry_json(e)
    assert body["extra"] == {"rises": 3, "rise_share": 0.3}
    assert "pnl" not in body, "the touch P&L is the direction's"


def test_a_kind_s_leaderboard_ranks_its_algorithms_against_its_baselines_with_one_fdr() -> None:
    sure_right = [(1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)]
    right = [
        KindScored("right", "v1", "005930", r.bar_close, sure_right[r.outcome], r.outcome, {}, {})
        for r in records(repeat=3)
    ]
    board = leaderboard(
        {"alg": records(repeat=3), "right": right}, {"medium-always": always_medium(3)}, 20, RANGE
    )

    assert [e.algorithm for e in board.ranked] == ["right", "alg"]
    assert [e.rank for e in board.ranked] == [1, 2]
    assert all(t.q is not None for e in board.ranked for t in e.lifts.values())
    assert [e.algorithm for e in board.baselines] == ["medium-always"]


def test_stored_rows_of_a_kind_are_scored_on_their_answer_and_judged_by_its_scoring() -> None:
    """Input: rows as the store reads them (output and answer). Output: records by class place;
    the lifecycle's clone measure is the first class's probability less the third's."""
    rows: list[dict[str, Any]] = [
        {
            "algorithm": "alg",
            "model_version": "v1",
            "symbol": "005930",
            "bar_close": "2026-10-12T09:31:00+09:00",
            "output": {"p_small": 0.6, "p_medium": 0.3, "p_large": 0.1},
            "answer_class": "large",
            "answer_path": {"r": 3},
        },
        {
            "algorithm": "alg",
            "model_version": "v1",
            "symbol": "005930",
            "bar_close": "2026-10-12T09:32:00+09:00",
            "output": {"p_small": 0.6, "p_medium": 0.3, "p_large": 0.1},
            "answer_class": None,  # unscorable
            "answer_path": {},
        },
    ]
    [record] = scored_of_kind(rows, RANGE)["alg"]
    assert (record.probabilities, record.outcome, record.predicted, record.path) == (
        (0.6, 0.3, 0.1),
        2,
        0,
        {"r": 3},
    )
    scoring = kind_scoring(RANGE)
    assert math.isclose(scoring.lean(rows[0]), 0.5)
    assert (scoring.lean_name, scoring.target_version) == ("p_small - p_large", 1)
