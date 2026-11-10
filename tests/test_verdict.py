"""The verdict (#676, ``verdict.py``): one BUY / SELL / WAIT a symbol from every algorithm's barrier
forecasts, horizon by horizon, through its one call ``verdict``. Prices are made up."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from krx_quant.feeds.base import KST
from krx_quant.verdict import (
    AVOID,
    BUY,
    DOWN,
    EXIT,
    HOLDING,
    MODERATE,
    NO_EDGE,
    NO_MEMBERS,
    NONE,
    SELL,
    STRONG,
    UP,
    WAIT,
    WEAK,
    Held,
    Horizon,
    Market,
    Member,
    Rules,
    Side,
    verdict,
)

T = datetime(2026, 10, 12, 10, 0, tzinfo=KST)
# Mid-point 81,250 (tick 100) between 81,200 and 81,300; no commission unless a test sets one, the
# 0.20% sell tax always.
MARKET = Market(anchor=81_250, bid=81_200, ask=81_300, fee_rate=0.0)
W = 8  # the width: target 82,050, stop 80,450 (its first bid below: 80,350)


def member(name: str, p: tuple[float, float, float], kind: str = "barrier", status: str = "active") -> Member:
    return Member(name, kind, status, 120, p)


def horizon(minutes: int, *members: Member, width: int | None = W) -> Horizon:
    kind = "barrier" if minutes == 4 else f"barrier_{minutes}m"
    return Horizon(
        kind, minutes, width, [Member(m.name, kind, m.status, m.evidence, m.probabilities) for m in members]
    )


def by_minutes(v: object) -> dict[int, dict[str, object]]:
    body = v.as_json()  # type: ignore[attr-defined]
    return {h["minutes"]: h for h in body["horizons"]}


def test_without_a_member_it_waits() -> None:
    """Input: the four horizons, none with a member (each Breiman waiting for its pre-validation, as
    on the server on 2026-11-10). Output: WAIT, ``no_members``; every horizon listed without a pool,
    its target and stop still priced."""
    found = verdict("005930", T, [horizon(m) for m in (4, 15, 30, 60)], MARKET)

    body = found.as_json()
    assert (body["verdict"], body["reason"], body["strength"], body["horizon"]) == (
        WAIT,
        NO_MEMBERS,
        NONE,
        None,
    )
    assert [h["probabilities"] for h in body["horizons"]] == [None] * 4
    assert body["horizons"][0]["target"] == 82_050 and body["horizons"][0]["stop"] == 80_450
    assert body["members"] == []


def test_families_weigh_alike_and_share_their_weight() -> None:
    """Input: at 15 minutes two members of one family, (0.6, 0.2, 0.2) and (0.4, 0.2, 0.4), and one
    of its own, (0.2, 0.6, 0.2). Output: the family's mean (0.5, 0.2, 0.3) and the other weigh half
    each: (0.35, 0.4, 0.25); the family's members a quarter each."""
    rules = Rules(families={"a1": "a", "a2": "a"})
    found = verdict(
        "005930",
        T,
        [
            horizon(
                15, member("a1", (0.6, 0.2, 0.2)), member("a2", (0.4, 0.2, 0.4)), member("b", (0.2, 0.6, 0.2))
            )
        ],
        MARKET,
        rules=rules,
    )

    pooled = by_minutes(found)[15]
    assert pooled["probabilities"] == pytest.approx(
        {"p_up_first": 0.35, "p_timeout": 0.4, "p_down_first": 0.25}
    )
    assert (pooled["families"], pooled["members"]) == (2, 3)
    weights = {m["name"]: (m["family"], m["weight"]) for m in found.as_json()["members"]}
    assert weights == {"a1": ("a", 0.25), "a2": ("a", 0.25), "b": ("b", 0.5)}


def test_a_consensus_is_never_counted_beside_its_own_members() -> None:
    """Input: a consensus ``c`` of ``a1``, at 4 minutes with ``a1``, then with ``b`` alone. Output:
    beside ``a1`` it is shown with weight 0 and the pool is ``a1``'s; without any of its members it
    counts as a family of its own."""
    rules = Rules(consensus={"c": frozenset({"a1"})})
    up, down = (0.6, 0.2, 0.2), (0.2, 0.2, 0.6)

    beside = verdict("005930", T, [horizon(4, member("a1", up), member("c", down))], MARKET, rules=rules)
    apart = verdict("005930", T, [horizon(4, member("b", up), member("c", down))], MARKET, rules=rules)

    shown = {m["name"]: (m["counted"], m["weight"]) for m in beside.as_json()["members"]}
    assert shown == {"a1": (True, 1.0), "c": (False, 0.0)}
    assert by_minutes(beside)[4]["probabilities"] == pytest.approx(
        {"p_up_first": 0.6, "p_timeout": 0.2, "p_down_first": 0.2}
    )
    assert by_minutes(apart)[4]["probabilities"] == pytest.approx(
        {"p_up_first": 0.4, "p_timeout": 0.2, "p_down_first": 0.4}
    )


def test_the_expected_net_counts_the_target_the_stop_and_the_timeout_by_hand() -> None:
    """Input: (0.6, 0.2, 0.2) at w = 8: bought at the ask 81,300; the target 82,050 nets 750 − the
    tax 164.1 = 585.9; the stop's first bid 80,350 nets −950 − 160.7 = −1,110.7; at timeout the bid
    81,200 nets −100 − 162.4 = −262.4. Expected 0.6 × 585.9 − 0.2 × 1,110.7 − 0.2 × 262.4 = 76.92 won a
    share (0.7692 ticks). At 0.01% a side each outcome pays the commissions on the ask and its exit
    too: 0.0001 × (81,300 + 81,540, the exits' mean) = 16.284 less."""
    p = (0.6, 0.2, 0.2)
    free = by_minutes(verdict("005930", T, [horizon(4, member("a", p))], MARKET))[4]
    paid = by_minutes(
        verdict("005930", T, [horizon(4, member("a", p))], Market(81_250, 81_200, 81_300, 0.0001))
    )[4]

    assert free["expected_net"] == pytest.approx(76.92)
    assert free["expected_net_ticks"] == pytest.approx(0.7692)
    assert paid["expected_net"] == pytest.approx(76.92 - 16.284)
    # All timeout: the spread and the tax alone, counted once.
    stuck = by_minutes(verdict("005930", T, [horizon(4, member("a", (0.0, 1.0, 0.0)))], MARKET))[4]
    assert stuck["expected_net"] == pytest.approx(-262.4)


def test_it_buys_the_horizon_with_the_greatest_expected_net_until_its_deadline() -> None:
    """Input: at 4 minutes (0.6, 0.2, 0.2) at w = 8 (76.92 won), at 15 minutes (0.7, 0.1, 0.2) at
    w = 12 (target 82,450, stop 80,050; its first bid 79,950): 0.7 × (1,150 − 164.9) − 0.2 × (1,350 +
    159.9) − 0.1 × 262.4 = 361.35 won. Output: BUY at 15 minutes, strength strong (0.5), its deadline
    10:15, its target and stop."""
    found = verdict(
        "005930",
        T,
        [horizon(4, member("a", (0.6, 0.2, 0.2))), horizon(15, member("a", (0.7, 0.1, 0.2)), width=12)],
        MARKET,
    )

    body = found.as_json()
    assert by_minutes(found)[15]["expected_net"] == pytest.approx(361.35)
    assert (body["verdict"], body["meaning"], body["reason"]) == (BUY, None, None)
    assert body["horizon"] == {"kind": "barrier_15m", "minutes": 15}
    assert (body["strength"], body["margin"]) == (STRONG, pytest.approx(0.5))
    assert body["deadline"] == "2026-10-12T10:15:00+09:00"
    assert (body["target"], body["stop"], body["held"]) == (82_450, 80_050, False)


def test_a_positive_expected_net_needs_the_margin_too() -> None:
    """Input: (0.6, 0.2, 0.2), 76.92 won expected, its margin 0.4, under rules asking 0.5. Output:
    WAIT, ``no_edge``, on that horizon."""
    found = verdict("005930", T, [horizon(4, member("a", (0.6, 0.2, 0.2)))], MARKET, rules=Rules(margin=0.5))

    body = found.as_json()
    assert (body["verdict"], body["reason"], body["margin"], body["strength"]) == (WAIT, NO_EDGE, None, NONE)
    assert body["horizon"] == {"kind": "barrier", "minutes": 4}


def test_no_buy_once_the_ask_reached_the_target_or_without_a_quote() -> None:
    """Input: (0.9, 0.05, 0.05) at w = 1 (target 81,350), first with the ask at 81,350, then with no
    quote. Output: WAIT both times (no expected net without a quote)."""
    p = (0.9, 0.05, 0.05)
    reached = verdict("005930", T, [horizon(4, member("a", p), width=1)], Market(81_250, 81_300, 81_350, 0.0))
    unquoted = verdict("005930", T, [horizon(4, member("a", p), width=1)], Market(81_250, None, None, 0.0))

    assert reached.as_json()["verdict"] == WAIT
    assert unquoted.as_json()["verdict"] == WAIT
    assert by_minutes(unquoted)[4]["expected_net"] is None


def test_a_tiny_lean_reads_weak_and_none_below_its_floor() -> None:
    """Input: members leaning by 0.01, 0.03, 0.10, 0.20 up and 0.20 down. Output: none (under 0.02),
    weak, moderate, strong; up, up, up and down; every one keeps its own probabilities."""
    members = [
        member("tiny", (0.34, 0.33, 0.33)),
        member("small", (0.35, 0.33, 0.32)),
        member("some", (0.40, 0.30, 0.30)),
        member("big", (0.45, 0.30, 0.25)),
        member("down", (0.25, 0.30, 0.45)),
    ]
    found = verdict("005930", T, [horizon(4, *members)], MARKET)

    shown = {m["name"]: (m["lean"], m["strength"]) for m in found.as_json()["members"]}
    assert shown == {
        "tiny": (NONE, NONE),
        "small": (UP, WEAK),
        "some": (UP, MODERATE),
        "big": (UP, STRONG),
        "down": (DOWN, STRONG),
    }
    assert found.as_json()["members"][0]["probabilities"] == {
        "p_up_first": 0.34,
        "p_timeout": 0.33,
        "p_down_first": 0.33,
    }


def test_nothing_held_a_view_against_reads_avoid() -> None:
    """Input: nothing held; at 4 minutes a weak up lean, at 30 minutes (0.2, 0.2, 0.6). Output: SELL,
    meaning avoid buying, on the 30 minutes, strength strong; no target or deadline."""
    found = verdict(
        "005930",
        T,
        [horizon(4, member("a", (0.36, 0.33, 0.31))), horizon(30, member("a", (0.2, 0.2, 0.6)))],
        MARKET,
    )

    body = found.as_json()
    assert (body["verdict"], body["meaning"], body["strength"]) == (SELL, AVOID, STRONG)
    assert body["horizon"] == {"kind": "barrier_30m", "minutes": 30}
    assert (body["target"], body["stop"], body["deadline"]) == (None, None, None)


def test_held_it_reads_the_horizon_left_and_exits_when_it_turns_against() -> None:
    """Input: a position whose time barrier is 10 minutes away; at 4 minutes the pool leans down, at
    15 minutes up; then at 15 minutes down. Output: the 15 minutes is the horizon left (the shortest
    at least 10 minutes): WAIT ``holding`` first, though the 4 minutes leans down; then SELL, meaning
    exit, until the position's time barrier. Held, it never buys."""
    held = Held(entry_price=81_300, target=82_050, stop=80_450, time_barrier=T + timedelta(minutes=10))
    down, up = (0.1, 0.2, 0.7), (0.7, 0.2, 0.1)

    holding = verdict(
        "005930", T, [horizon(4, member("a", down)), horizon(15, member("a", up))], MARKET, held
    )
    turned = verdict("005930", T, [horizon(4, member("a", up)), horizon(15, member("a", down))], MARKET, held)

    first, second = holding.as_json(), turned.as_json()
    assert (first["verdict"], first["reason"], first["held"]) == (WAIT, HOLDING, True)
    assert first["horizon"] == {"kind": "barrier_15m", "minutes": 15}
    assert (second["verdict"], second["meaning"]) == (SELL, EXIT)
    assert second["deadline"] == "2026-10-12T10:10:00+09:00"
    assert second["horizon"]["minutes"] == 15


def test_past_every_horizon_held_reads_the_longest() -> None:
    """Input: a position 90 minutes from its time barrier, the 60 minutes pooled leaning down. Output:
    SELL exit on the 60 minutes, the longest pooled."""
    held = Held(81_300, 82_050, 80_450, T + timedelta(minutes=90))
    found = verdict(
        "005930",
        T,
        [horizon(15, member("a", (0.3, 0.4, 0.3))), horizon(60, member("a", (0.2, 0.2, 0.6)))],
        MARKET,
        held,
    )

    body = found.as_json()
    assert (body["verdict"], body["meaning"], body["horizon"]["minutes"]) == (SELL, EXIT, 60)


def test_the_side_panel_and_the_rules_go_with_it() -> None:
    """The direction's and the range's forecasts are shown, not pooled; the rules and their version
    are stored with every verdict."""
    side = [Side("consensus-final", "direction", None, {"p_up": 0.5, "p_flat": 0.3, "p_down": 0.2})]
    found = verdict("005930", T, [horizon(4)], MARKET, side=side)

    body = found.as_json()
    assert body["side"] == [
        {
            "name": "consensus-final",
            "kind": "direction",
            "status": None,
            "output": {"p_up": 0.5, "p_flat": 0.3, "p_down": 0.2},
        }
    ]
    assert body["version"] == body["rules"]["version"] == 1
    assert body["rules"]["margin"] == 0.05
    assert body["rules"]["families"]["breiman-15m"] == "breiman"
