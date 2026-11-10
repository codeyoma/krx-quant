"""Which cells may buy today (#646): the one answer the desk's books and the nightly replay ask for.

For a pick (a cost map, ``selection.choose``) and the review cycle in force (#469, ``review.py``), it
answers the cells that may buy, in their rank, each at its θ and with its quantity rule (the loss
budget's, or the practice's one share), whether it is the practice, the cells read and never bought,
and, when no cell may buy, why (``Selection.reason``). The live desk (``desk.Desk``) and the replay
(``replay.Policy``) get their cells from the same function, ``today``: a policy's cells, in its order,
less those resting for the cycle.

**What it hides.**

- **The cost map**: the cells it names and their figures (``Selection.named``); a cost map not read
  trades nothing (``NO_COST_MAP``).
- **The policy's override** (#399, #472, ``under_policy``): the champion's cells (or a challenger's),
  by their predictors' names, in its order, each at its θ, whatever the cost map's verdict; a cell the
  policy does not have is not traded (``NOT_IN_POLICY``), and nothing trades before the first champion
  (``NO_CHAMPION``) nor while it cannot be read (``NO_POLICY``).
- **The cycle's rest** (``Rest``, ``rest_of``, ``inactive_days``): a cell whose predictor is gone
  (#469, ``cells.gone``) trades no more for the rest of the cycle; one whose predictor is not active
  (#603, ``cells.inactive``) is read and buys nothing for the rest of the cycle, though it be active
  again. The desk learns it at each pick (the engine's database now, and the picks of the cycle kept in
  the store); the replay from the statuses the engine kept (``inactive_days``), at each day's pick.
- **The practice** (#427, #474, ``practised``): in mock mode alone, at a pick that read the cost map
  and the champion (none yet counts as read) but found no champion cell to trade, the practice cell
  kept for the cycle, one share at its θ; none while none is kept (``practice.NOT_FROZEN``), nor once
  its predictor was found gone or not active in the cycle.
- **The verdict** (#677, ``verdicted``, ``verdicts.py``): in mock mode alone, on the same picks as the
  practice (a cost map and a champion read, no champion cell to trade), the verdict policy's horizons,
  ranked first, before the practice cell if one is practised; sized by the loss budget.

**What it does not change.** The practice and the verdict are for the mock mode alone (``champion``
refuses them in any other); the champion is read at the picks only and changes on a review night alone
(``promotion.py``); the hard loss limits are the books' own (``limits.py``) and nothing here reads
them.

**What the replay keeps apart on purpose.** A recorded gap leaves its signals out where the live book
would stall (``replay.py``), and a policy's replay spaces no practice buy (``book.spaced``): neither
is a question of which cells may buy.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Generic, Protocol, TypeVar

from krx_quant.feeds.base import KST
from krx_quant.market_calendar import Session
from krx_quant.trader import MOCK, PICK_AT, SYMBOL, review
from krx_quant.trader.cells import ACTIVE, Cell, History, gone, inactive, status_at
from krx_quant.trader.selection import (
    GONE,
    NO_CHAMPION,
    NO_COST_MAP,
    NO_PASS,
    NO_POLICY,
    NOT_IN_POLICY,
    PREDICTOR_INACTIVE,
    Pick,
    Selection,
)

if TYPE_CHECKING:
    from krx_quant.trader import practice as practices
    from krx_quant.trader.policy import Policy
    from krx_quant.trader.store import TraderStore

log = logging.getLogger(__name__)


class Ranked(Protocol):
    """A policy's cell as ranked: a ``policy.PolicyCell`` on the desk, a ``replay.Ranked`` in the
    replay."""

    @property
    def cell(self) -> Cell: ...

    @property
    def threshold(self) -> float: ...


R = TypeVar("R", bound=Ranked)


@dataclass(frozen=True)
class Rest:
    """A policy's cells that buy nothing more in the review cycle (module doc): those whose predictor is
    gone (#469), and those whose predictor is not active (#603: read, never bought)."""

    gone: frozenset[Cell] = frozenset()
    inactive: frozenset[Cell] = frozenset()


# No cell rests.
NO_REST = Rest()


@dataclass(frozen=True)
class Today(Generic[R]):
    """A policy's cells today (``today``): those that may buy (``buys``, in its order), those read and
    never bought (``held``, #603), and each cell with why it does not buy (``rows``, in its order; None:
    it buys)."""

    buys: tuple[R, ...]
    held: tuple[Cell, ...]
    rows: tuple[tuple[R, str | None], ...]

    @property
    def why(self) -> Mapping[Cell, str | None]:
        return {r.cell: why for r, why in self.rows}


def today(cells: Sequence[R], rest: Rest = NO_REST, *, cost_map: bool = True) -> Today[R]:
    """The cells of a policy (``cells``, its order) that may buy, less those of ``rest`` (module doc):
    ``GONE`` (its predictor stopped predicting), ``PREDICTOR_INACTIVE`` (its predictor is not active:
    read, never bought; so even when the cost map could not be read, so that the pick keeps it for the
    cycle), ``NO_COST_MAP`` (``cost_map`` False: none buys). The desk's books and the replay alike."""
    buys: list[R] = []
    held: list[Cell] = []
    rows: list[tuple[R, str | None]] = []
    for ranked in cells:
        why = None
        if cost_map and ranked.cell in rest.gone:
            why = GONE
        elif ranked.cell in rest.inactive:  # #603: before the cost map, so that the pick keeps it
            why = PREDICTOR_INACTIVE
            held.append(ranked.cell)
        elif not cost_map:
            why = NO_COST_MAP
        else:
            buys.append(ranked)
        rows.append((ranked, why))
    return Today(tuple(buys), tuple(held), tuple(rows))


def under_policy(kept: Policy | None, selection: Selection, rest: Rest = NO_REST) -> Selection:
    """``selection`` (today's cost map) under the policy ``kept`` (module doc): its
    cells that may buy (``today``), in its order, each at its θ, whatever the cost map's verdict
    (#472), with the cost map's figures where it names them (``Pick.unmapped`` where it does not); the
    cells the cost map passes that it does not have marked as not in it. Each of its cells kept with
    whether it trades and why not (``Selection.cells``, #469), those read and never bought in
    ``Selection.inactive`` (#603). Without a policy nothing trades (``NO_CHAMPION``), and a cost map
    that could not be read stays as it is."""
    if kept is None:
        if selection.reason == NO_COST_MAP:
            return selection
        unpicked = tuple(
            {**seen, "picked": False, "why": NO_CHAMPION} if seen.get("picked") else seen
            for seen in selection.considered
        )
        return replace(selection, picks=(), considered=unpicked, reason=NO_CHAMPION, named=())
    # The cost map's figures of each cell it names: its passing picks are among them.
    named = {p.cell: p for p in (*selection.picks, *selection.named)}
    found = today(kept.cells, rest, cost_map=selection.reason != NO_COST_MAP)
    picks = [
        replace(
            named.get(c.cell) or Pick.unmapped(c.kind, c.role, c.predictor),
            threshold=c.threshold,
        )
        for c in found.buys
    ]
    whys = found.why
    cells = [
        {
            "kind": c.kind,
            "role": c.role,
            "predictor": c.predictor,
            "threshold": c.threshold,
            "traded": why is None,
            "why": why,
        }
        for c, why in found.rows
    ]
    traded = {p.cell for p in picks}
    considered = []
    for seen in selection.considered:
        named_cell = Cell(seen["kind"], seen.get("predictor") or "")
        if named_cell in traded:  # #472: a champion's cell trades whatever its verdict
            seen = {**seen, "picked": True}
        elif seen.get("picked"):
            why = whys.get(named_cell) or f"not in policy {kept.version}"
            seen = {**seen, "picked": False, "why": why}
        considered.append(seen)
    reason = None if picks else selection.reason or NOT_IN_POLICY
    return replace(
        selection,
        picks=tuple(picks),
        considered=tuple(considered),
        reason=reason,
        policy=kept.version,
        cells=tuple(cells),
        named=(),
        inactive=found.held,
    )


def under_champion(
    store: TraderStore,
    selection: Selection,
    rest_for: Callable[[Policy], Rest] | None = None,
) -> Selection:
    """``selection`` under the champion policy kept now (#399), its cells ``rest_for`` it resting (#469:
    gone; #603: not active, read and never bought); with no cell before the first (``NO_CHAMPION``,
    #469) and when the champion cannot be read (``NO_POLICY``)."""
    from krx_quant.trader.policy import PolicyError

    try:
        champion = store.champion()
    except (PolicyError, sqlite3.Error) as e:
        log.error("trader: cannot read the champion policy (%s): no cell trades until the next pick", e)
        return replace(selection, picks=(), reason=NO_POLICY, detail=str(e))
    if champion is None:
        log.info("trader: no champion policy yet: no cell trades before the first (#469)")
        return under_policy(None, selection)
    log.info(
        "trader: under the champion, policy %s (%s): %s cell(s)",
        champion.version,
        champion.created_at.strftime("%Y-%m-%d %H:%M"),
        len(champion.cells),
    )
    return under_policy(champion, selection, NO_REST if rest_for is None else rest_for(champion))


def rest_of(
    kept: Policy,
    cycle: review.Cycle | None,
    at: datetime,
    *,
    store: TraderStore,
    engine: Path | None,
) -> Rest:
    """The cells of ``kept`` that buy nothing more in ``cycle`` (module doc): their predictor gone now
    in the engine's database (at ``engine``), or found gone by a pick earlier in the cycle (#469); and
    those whose predictor is not active now, or was found so by a pick earlier in the cycle (#603, the
    picks ``store`` kept). Logged; what cannot be read is logged and rests nothing."""
    found: set[Cell] = set()
    statuses: dict[Cell, str] = {}
    cells = [c.cell for c in kept.cells]
    if engine is not None:
        try:
            found = gone(engine, SYMBOL, cells, at)
        except sqlite3.Error as e:
            log.warning("trader: cannot tell if policy %s's predictors predict (%s)", kept.version, e)
        try:
            statuses = inactive(engine, cells, at)
        except sqlite3.Error as e:
            log.warning("trader: cannot tell if policy %s's predictors are active (%s)", kept.version, e)
    held: set[Cell] = set(statuses)
    if cycle is not None:
        try:
            found |= store.gone(kept.version, cycle.evaluation, cycle.number)
            held |= store.inactive(kept.version, cycle.evaluation, cycle.number)
        except (sqlite3.Error, ValueError, KeyError, TypeError) as e:
            log.warning("trader: cannot read policy %s's cells rested this cycle (%s)", kept.version, e)
    for cell in sorted(found & set(cells), key=lambda c: (c.kind, c.predictor)):
        log.warning(
            "trader: policy %s's cell %s %s is %s: its predictor no longer predicts; it trades no"
            " more until the next review, and no other cell takes its place",
            kept.version,
            cell.kind,
            cell.predictor,
            GONE,
        )
    for cell in sorted((held - found) & set(cells), key=lambda c: (c.kind, c.predictor)):
        now = statuses.get(cell)
        log.warning(
            "trader: policy %s's cell %s %s is %s: its predictor %s; it buys nothing until the next"
            " review, its position sold by its own rules, and no other cell takes its place",
            kept.version,
            cell.kind,
            cell.predictor,
            PREDICTOR_INACTIVE,
            f"is {now}, not active" if now is not None else "was not active at a pick earlier this cycle",
        )
    return Rest(frozenset(found), frozenset(held))


def inactive_days(
    history: History,
    days: Collection[date],
    sessions: Callable[[date], Session | None],
    evaluation: review.Evaluation | None = None,
) -> frozenset[date]:
    """The ``days`` a pick would have held a cell off new buys (#603, module doc "The cycle's rest"),
    its predictor's status changes ``history``: its status at the day's pick (``PICK_AT``) not active,
    or (with ``evaluation``, from its first day) not active at the pick of a trading day of the same
    review cycle before it, counted on ``sessions``. None for a predictor without a status."""
    if not history:
        return frozenset()

    def held(day: date) -> bool:
        status = status_at(history, datetime.combine(day, PICK_AT, KST))
        return status is not None and status != ACTIVE

    out = set()
    for day in days:
        if held(day):
            out.add(day)
        elif evaluation is not None and day >= evaluation.first_day:
            first = review.cycle_of(evaluation, day, sessions).first
            earlier = (first + timedelta(days=n) for n in range((day - first).days))
            if any(sessions(d) is not None and held(d) for d in earlier):
                out.add(day)
    return frozenset(out)


# The practice (#427, #474).


def practice_state(enabled: bool, kept: practices.Frozen | None = None) -> dict[str, Any]:
    """``GET /v1/trader``'s ``practice`` (#427): whether it is on, whether the book practises now and
    on which cell (set by the pick), the cell kept for the review cycle (#474, ``frozen``) and why it
    does not practise (``why``, ``practice.py``; null while it does, and with the practice off)."""
    return {
        "enabled": enabled,
        "active": False,
        "cell": None,
        "frozen": None if kept is None else kept.as_json(),
        "why": None,
    }


def practised(
    under: Selection,
    cycle: review.Cycle | None,
    at: datetime,
    *,
    store: TraderStore,
    engine: Path | None,
    enabled: bool,
) -> tuple[Selection, dict[str, Any]]:
    """``under`` (the champion's pick), or with the practice ``enabled`` and no champion cell to trade
    on a cost map and a champion read (none yet included), the practice cell kept for ``cycle`` (#474,
    module doc); with ``GET /v1/trader``'s ``practice`` (#427)."""
    from krx_quant.trader import practice

    if not enabled:
        return under, practice_state(False)
    kept = frozen_now(cycle, at, store=store, engine=engine)
    state = practice_state(True, kept)
    why: str | None = None
    if under.picks:
        why = practice.CHAMPION
    elif under.reason not in (NO_PASS, NOT_IN_POLICY, NO_CHAMPION):
        why = under.reason
    elif kept is None:
        why = practice.NOT_FROZEN
        log.info(
            "trader: no practice cell is kept for review cycle %s yet (the nightly job chooses one):"
            " no practice until one is",
            "?" if cycle is None else cycle.number,
        )
    elif kept.rested:
        why = GONE if kept.gone_at is not None else PREDICTOR_INACTIVE
        log.info(
            "trader: the practice cell %s %s is %s: no practice until review cycle %s ends",
            kept.pick.kind,
            kept.pick.predictor,
            why,
            kept.cycle,
        )
    if why is not None or kept is None:
        return under, state | {"why": why}
    # The champion's cells not active are the shadow's to read (#603): the practice pick is its own.
    chosen = replace(under, picks=(kept.pick,), inactive=())
    return chosen, state | {"active": True, "cell": kept.pick.as_json()}


def frozen_now(
    cycle: review.Cycle | None, at: datetime, *, store: TraderStore, engine: Path | None
) -> practices.Frozen | None:
    """The practice cell kept for ``cycle`` (#474), found gone at ``at`` if its predictor no longer
    predicts (``cells.gone``, as a policy's cell, #469), or not active if it is not (#603,
    ``cells.inactive``): kept, so that it rests for the rest of the cycle; None without one, or when
    the store cannot say (logged)."""
    from krx_quant.trader import practice

    if cycle is None:
        return None
    try:
        kept = practice.frozen(store.db, cycle.evaluation, cycle.number)
        if kept is None or kept.rested or engine is None:
            return kept
        if kept.cell in gone(engine, SYMBOL, [kept.cell], at):
            why, said = GONE, "no longer predicts"
        elif (status := inactive(engine, [kept.cell], at).get(kept.cell)) is not None:
            why, said = PREDICTOR_INACTIVE, f"is {status}, not active"
        else:
            return kept
        log.warning(
            "trader: the practice cell %s %s is %s: its predictor %s; no practice until review cycle %s"
            " ends, and no other cell takes its place",
            kept.pick.kind,
            kept.pick.predictor,
            why,
            said,
            cycle.number,
        )
        practice.rest(store.db, cycle.evaluation, cycle.number, at, why)
        return practice.frozen(store.db, cycle.evaluation, cycle.number)
    except (sqlite3.Error, ValueError, KeyError, TypeError) as e:
        log.error("trader: cannot read the practice cell of review cycle %s (%s): none", cycle.number, e)
        return None


# The verdict policy (#677).


def verdicted(under: Selection, chosen: Selection, *, enabled: bool) -> Selection:
    """``chosen`` (the champion's pick, or the practice cell), with the verdict ``enabled`` and no
    champion cell to trade on a cost map and a champion read (none yet included): the verdict policy's
    horizons before its picks (#677, module doc)."""
    from krx_quant.trader import verdicts

    if not enabled or under.picks or under.reason not in (NO_PASS, NOT_IN_POLICY, NO_CHAMPION):
        return chosen
    return replace(chosen, picks=(*verdicts.picks(), *chosen.picks), inactive=())


# The desk's questions.


@dataclass(frozen=True)
class Champion:
    """The champion's pick (``champion``): under its policy (``under``: its paper shadow's), what its
    book trades (``chosen``: ``under``, or the practice cell, #427, and the verdict's horizons, #677),
    and ``GET /v1/trader``'s ``practice``."""

    under: Selection
    chosen: Selection
    practice: dict[str, Any] = field(default_factory=lambda: practice_state(False))


def champion(
    store: TraderStore,
    selection: Selection,
    cycle: review.Cycle | None,
    at: datetime,
    *,
    engine: Path | None,
    mode: str,
    practice: bool = False,
    verdict: bool = False,
) -> Champion:
    """The cost map's cells (``selection``) at ``at`` under the champion kept in ``store``, less those
    resting in ``cycle`` (``rest_of``), and with ``practice`` the practice cell on a pick no champion
    cell trades (``practised``), with ``verdict`` the verdict policy's horizons before it
    (``verdicted``): in mock mode alone (#427, #677; ValueError in another ``mode``)."""
    if practice and mode != MOCK:  # #427: the settings refuse it before (``settings.py``)
        raise ValueError(f"the mock practice is for the mock mode alone, not {mode}")
    if verdict and mode != MOCK:  # #677: likewise
        raise ValueError(f"the verdict policy is for the mock mode alone, not {mode}")

    def rest_for(kept: Policy) -> Rest:
        return rest_of(kept, cycle, at, store=store, engine=engine)

    under = under_champion(store, selection, rest_for)
    chosen, state = practised(under, cycle, at, store=store, engine=engine, enabled=practice)
    return Champion(under, verdicted(under, chosen, enabled=verdict), state)


def challenger(
    store: TraderStore,
    kept: Policy,
    selection: Selection,
    cycle: review.Cycle | None,
    at: datetime,
    *,
    engine: Path | None,
) -> Selection:
    """The cost map's cells (``selection``) at ``at`` under challenger ``kept``, less those resting in
    ``cycle`` (``rest_of``); a challenger never practises."""
    return under_policy(kept, selection, rest_of(kept, cycle, at, store=store, engine=engine))
