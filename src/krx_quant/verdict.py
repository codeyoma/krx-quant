"""The verdict (#676): one BUY / SELL / WAIT per symbol at each bar close, from every algorithm's
barrier forecasts, horizon by horizon. The engine makes one at each bar close once its algorithms
are done (``engine.py``), stores it (``predictions.verdicts``) and serves it
(``GET /v1/verdict``), so the screen and the trader read the same answer.

The user's decisions (2026-11-10, #676):

- **Per horizon, on the native barrier forecasts.** The horizons are the barrier kinds' (4, 15,
  30 and 60 minutes: ``barrier``, ``barrier_15m``, ``barrier_30m``, ``barrier_60m``), each read as
  its own three probabilities: P(up first), P(timeout), P(down first) at that kind's width. The
  direction's and the range's forecasts are shown beside it (``side``), never converted and mixed
  in: the direction's "up" is an upper touch within four minutes, the barrier's "up" is the upper
  barrier first, and their widths differ.
- **Every algorithm with a forecast at the bar close takes part, whatever its status** (active,
  probation, bench): each member shows its status and its evidence (its predictions recorded and
  answered). An algorithm waiting for its pre-validation, or refused, is not run, so it has no
  forecast and takes no part. The baselines are not members.
- **Families vote as one.** Each family weighs the same at a horizon; its members there share its
  weight equally. A consensus is never counted beside its own members: while any of them takes part
  it is shown and not counted (weight 0).
- **Less strict.** Each member keeps its own probabilities and shows its **lean**: up when its up
  first beats its down first by at least ``Rules.lean_floor``, down the other way, none between; and
  the lean's strength by that margin (``none``, ``weak``, ``moderate``, ``strong``), so that a tiny
  difference reads weak, not as conviction.

**The pool** at a horizon: the mean of the families' means of their members' probabilities.

**The expected net** of a long entered at a horizon, per share in won: its target is the anchor
(the bar close's mid-point) plus w ticks of the anchor's tick size, its stop the anchor less w ticks,
w the kind's width there, as the trader's barrier cells set them (``trader/paper.py``). The entry is
the executable ask when the verdict is made, after the members are done (the trader's decision
comes then); the exits are executable bids: the target itself (the take-profit rests there and fills
once a bid reaches it), the stop's first bid below it (one adverse tick, as ``money.at_risk``), and at
timeout the bid now (the mid-point held). Each outcome's net is the trader's round trip
(``money.net_of``: both commissions and the sell tax), and the expected net their mean weighted by
the pool. The spread is in the prices alone (bought at the ask, sold at a bid), never also a cost.

**The verdict**:

- **BUY** when nothing is held, the horizon with the greatest expected net has it above 0 and its
  pooled up first beats its down first by the margin ``Rules.margin``. A horizon whose ask is not
  between its stop and its target (the price moved past a barrier while the members worked) has no
  expected net: no trade to plan there. The BUY's horizon is the maximum holding time: the verdict's
  deadline is the bar close plus it, with its target and stop.
- **SELL** means **exit** while the trader holds the symbol (``Held``) and the pool at the remaining
  horizon (the shortest pooled horizon at least the time left to the position's time barrier, else
  the longest pooled) has down first beating up first by the margin; and **avoid** buying while
  nothing is held, not a BUY, and some horizon's pool leans down by the margin (the most against).
- **WAIT** otherwise (``no_edge``, or ``holding`` a position the pool does not turn against), and
  whenever no member has a forecast (``no_members``).

The margin, the lean's grades, the family map and the consensus map are versioned constants of this
module (``Rules``, ``VERDICT_VERSION``), stored with each verdict: changing one is a new version,
never a silent edit.

**Version 2** (#697, the user's decisions of 2026-11-12): the pool of a horizon counts only the
algorithms the night's **ranking** made members of its kind (``ranking.py``: every algorithm whose
log-loss skill against the kind's climate has its lower bound above 0, entering and leaving after three
rankings running), whatever their status, a shadow one's too (#696: the engine runs a ranked one before
the verdict). Every other forecast at the bar close is shown and not counted (``ranked`` false, weight
0). A horizon without a ranked member has no pool: it says WAIT. The rules above are v1's, over those
members: the expected net, the cost, BUY and SELL, the families and the consensus.

Beside the barrier horizons it shows the **day and the week** (``open_to_close``, ``open_to_close_5d``:
the daily kinds' forecasts of today, made at 08:50), **advisory** (``Advisory``): each pooled the same
way from its ranked members, and called up when its up beats its down by the margin, down the other way,
flat between, ``wait`` without a ranked member. They are shown, never acted on: BUY and SELL rest on the
barrier horizons alone, whose shape the trader reads (#677) is v1's.

> **바뀜** (2026-11-12, #704): every algorithm with a forecast at the bar close took part, whatever its
> status (v1) → only the ranking's members of each kind (v2), and the day and the week are shown beside
> as advisory. 이유: the user's decision (#697): the raw hit rate rewards always saying timeout, and the
> algorithms that showed skill against the baseline vote. v1 stays a version (``RULES_V1``) the replay
> can run (#679).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from krx_quant.feeds.base import KST
from krx_quant.pricing import tick_size
from krx_quant.trader.money import net_of

VERDICT_VERSION = 2
BUY, SELL, WAIT = "buy", "sell", "wait"
EXIT, AVOID = "exit", "avoid"  # what a SELL means: held, or not
NO_MEMBERS, NO_EDGE, HOLDING = "no_members", "no_edge", "holding"  # why a WAIT
UP, DOWN, NONE = "up", "down", "none"  # a lean
FLAT = "flat"  # an advisory horizon's call between up and down (#697)
WEAK, MODERATE, STRONG = "weak", "moderate", "strong"  # its strength, beside NONE

# Algorithms of one family (#676): the Breiman of each barrier horizon is one model of trees, its
# kind's. An algorithm not listed is a family of its own.
FAMILIES: Mapping[str, str] = {
    "breiman": "breiman",
    "breiman-15m": "breiman",
    "breiman-30m": "breiman",
    "breiman-60m": "breiman",
}
# A consensus and its members (#676): none pools barrier forecasts yet. The direction's consensus
# (``consensus.py``) pools the direction's, which the verdict does not mix in.
CONSENSUS: Mapping[str, frozenset[str]] = {}


@dataclass(frozen=True)
class Rules:
    """The verdict's constants, of one version (module doc)."""

    version: int = VERDICT_VERSION
    margin: float = 0.05  # m: up first over down first (or the other way) for a BUY or a SELL
    lean_floor: float = 0.02  # a member's lean below this margin is none
    moderate: float = 0.05  # a margin from here is moderate, below it weak
    strong: float = 0.15  # from here strong
    ranked: bool = True  # v2 (#697): a horizon pools only its kind's ranking members
    families: Mapping[str, str] = field(default_factory=lambda: dict(FAMILIES))
    consensus: Mapping[str, frozenset[str]] = field(default_factory=lambda: dict(CONSENSUS))

    def family(self, name: str) -> str:
        return self.families.get(name, name)

    def strength(self, margin: float) -> str:
        """A margin's strength, by its size."""
        size = abs(margin)
        if size < self.lean_floor:
            return NONE
        return WEAK if size < self.moderate else MODERATE if size < self.strong else STRONG

    def lean(self, margin: float) -> str:
        if margin >= self.lean_floor:
            return UP
        return DOWN if margin <= -self.lean_floor else NONE

    def as_json(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "margin": self.margin,
            "lean_floor": self.lean_floor,
            "moderate": self.moderate,
            "strong": self.strong,
            "ranked": self.ranked,
            "families": dict(sorted(self.families.items())),
            "consensus": {name: sorted(members) for name, members in sorted(self.consensus.items())},
        }


RULES = Rules()
RULES_V1 = Rules(version=1, ranked=False)  # every forecast at the bar close pooled (#676)


@dataclass(frozen=True)
class Member:
    """An algorithm's barrier forecast at the bar close: its status (None: none recorded), its
    evidence (its predictions of the kind answered) and its three probabilities, the kind's order
    (up first, timeout, down first)."""

    name: str
    kind: str
    status: str | None
    evidence: int
    probabilities: tuple[float, float, float]


@dataclass(frozen=True)
class Horizon:
    """A barrier kind at the bar close: its name, its time barrier in minutes, its width there (None
    without a reference price), its members' forecasts and its ranking's members (#697: v2 pools only
    those)."""

    kind: str
    minutes: int
    width_ticks: int | None
    members: Sequence[Member]
    ranked: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Advisory:
    """A daily kind beside the verdict (#697, module doc): its name, the sessions its window runs, its
    classes (rise, middle, fall), today's forecasts (each in the kind's order) and its ranking's
    members."""

    kind: str
    sessions: int
    classes: tuple[str, str, str]
    members: Sequence[Member]
    ranked: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Market:
    """The prices the verdict reads: the bar close's mid-point (the barriers' anchor) and the best
    quote when the verdict is made (the executable bid and ask), and the commission rate."""

    anchor: float | None
    bid: float | None
    ask: float | None
    fee_rate: float


@dataclass(frozen=True)
class Held:
    """The trader's open position in the symbol: its entry price, target, stop and time barrier."""

    entry_price: float
    target: float
    stop: float
    time_barrier: datetime


@dataclass(frozen=True)
class Side:
    """A forecast shown beside the verdict, not in it: the direction's or the range's."""

    name: str
    kind: str
    status: str | None
    output: Mapping[str, float]


def _probabilities_json(p: Sequence[float]) -> dict[str, float]:
    up, timeout, down = p
    return {"p_up_first": up, "p_timeout": timeout, "p_down_first": down}


def _classes_json(classes: Sequence[str], p: Sequence[float]) -> dict[str, float]:
    return {f"p_{c}": v for c, v in zip(classes, p, strict=True)}


@dataclass(frozen=True)
class Pooled:
    """A horizon's pool and what a long there would net (module doc)."""

    kind: str
    minutes: int
    width_ticks: int | None
    probabilities: tuple[float, float, float] | None  # None without a member
    families: int
    members: int
    target: float | None
    stop: float | None
    expected_net: float | None  # won a share; None without the prices
    tick: float | None

    @property
    def margin(self) -> float | None:
        """Up first less down first; None without a member."""
        return None if self.probabilities is None else self.probabilities[0] - self.probabilities[2]

    def as_json(self, rules: Rules) -> dict[str, Any]:
        margin = self.margin
        return {
            "kind": self.kind,
            "minutes": self.minutes,
            "width_ticks": self.width_ticks,
            "probabilities": None if self.probabilities is None else _probabilities_json(self.probabilities),
            "families": self.families,
            "members": self.members,
            "lean": None if margin is None else rules.lean(margin),
            "strength": None if margin is None else rules.strength(margin),
            "margin": margin,
            "target": self.target,
            "stop": self.stop,
            "expected_net": self.expected_net,
            "expected_net_ticks": None
            if self.expected_net is None or not self.tick
            else self.expected_net / self.tick,
        }


@dataclass(frozen=True)
class Verdict:
    symbol: str
    bar_close: datetime
    action: str  # BUY, SELL, WAIT
    meaning: str | None  # a SELL's: EXIT or AVOID
    reason: str | None  # a WAIT's: NO_MEMBERS, NO_EDGE, HOLDING
    horizon: Pooled | None  # the horizon it rests on
    held: Held | None
    horizons: Sequence[Pooled]
    members: Sequence[dict[str, Any]]
    side: Sequence[Side]
    rules: Rules
    advisory: Sequence[dict[str, Any]] = ()  # the day and the week (#697), as served

    @property
    def strength(self) -> str:
        if self.horizon is None or self.horizon.margin is None or self.action == WAIT:
            return NONE
        return self.rules.strength(self.horizon.margin)

    @property
    def deadline(self) -> datetime | None:
        """A BUY's maximum holding time; an exit's, the position's time barrier."""
        if self.action == BUY and self.horizon is not None:
            return self.bar_close + timedelta(minutes=self.horizon.minutes)
        if self.meaning == EXIT and self.held is not None:
            return self.held.time_barrier
        return None

    def as_json(self) -> dict[str, Any]:
        horizon = self.horizon
        deadline = self.deadline
        buying = self.action == BUY and horizon is not None
        return {
            "symbol": self.symbol,
            "bar_close": self.bar_close.astimezone(KST).isoformat(),
            "version": self.rules.version,
            "verdict": self.action,
            "meaning": self.meaning,
            "reason": self.reason,
            "strength": self.strength,
            "margin": None if horizon is None or self.action == WAIT else horizon.margin,
            "horizon": None if horizon is None else {"kind": horizon.kind, "minutes": horizon.minutes},
            "deadline": None if deadline is None else deadline.astimezone(KST).isoformat(),
            "target": horizon.target if buying and horizon is not None else None,
            "stop": horizon.stop if buying and horizon is not None else None,
            "held": self.held is not None,
            "horizons": [p.as_json(self.rules) for p in self.horizons],
            "members": list(self.members),
            "side": [
                {"name": s.name, "kind": s.kind, "status": s.status, "output": dict(s.output)}
                for s in self.side
            ],
            "advisory": list(self.advisory),
            "rules": self.rules.as_json(),
        }


def _weights(members: Sequence[Member], rules: Rules, ranked: frozenset[str]) -> dict[str, float]:
    """Each member's weight in its horizon's pool (module doc): v2 only the ranking's members (#697);
    the families alike, a family's members alike within it, a consensus 0 while one of its members
    takes part."""
    weights = {m.name: 0.0 for m in members}
    pooled = [m for m in members if not rules.ranked or m.name in ranked]
    present = {m.name for m in pooled}
    counted = [m for m in pooled if not (rules.consensus.get(m.name, frozenset()) & present)]
    by_family: dict[str, list[str]] = {}
    for m in counted:
        by_family.setdefault(rules.family(m.name), []).append(m.name)
    for names in by_family.values():
        for name in names:
            weights[name] = 1 / len(by_family) / len(names)
    return weights


def _pool(horizon: Horizon, market: Market, rules: Rules) -> tuple[Pooled, list[dict[str, Any]]]:
    weights = _weights(horizon.members, rules, horizon.ranked)
    counted = [m for m in horizon.members if weights[m.name] > 0]
    probabilities: tuple[float, float, float] | None = None
    if counted:
        up, timeout, down = (sum(weights[m.name] * m.probabilities[i] for m in counted) for i in range(3))
        probabilities = (up, timeout, down)
    tick = None if market.anchor is None else float(tick_size(market.anchor))
    target = stop = None
    if market.anchor is not None and tick is not None and horizon.width_ticks is not None:
        target = market.anchor + horizon.width_ticks * tick
        stop = market.anchor - horizon.width_ticks * tick
    expected = None
    if probabilities is not None and target is not None and stop is not None:
        expected = _expected_net(probabilities, market, target, stop)
    pooled = Pooled(
        horizon.kind,
        horizon.minutes,
        horizon.width_ticks,
        probabilities,
        len({rules.family(m.name) for m in counted}),
        len(counted),
        target,
        stop,
        expected,
        tick,
    )
    shown = []
    for m in horizon.members:
        margin = m.probabilities[0] - m.probabilities[2]
        shown.append(
            {
                "name": m.name,
                "kind": m.kind,
                "family": rules.family(m.name),
                "status": m.status,
                "evidence": m.evidence,
                "probabilities": _probabilities_json(m.probabilities),
                "lean": rules.lean(margin),
                "strength": rules.strength(margin),
                "margin": margin,
                "weight": weights[m.name],
                "counted": weights[m.name] > 0,
                "ranked": m.name in horizon.ranked,
            }
        )
    return pooled, shown


def advise(advisory: Advisory, rules: Rules) -> dict[str, Any]:
    """A daily kind beside the verdict, as served (module doc): its pool of its ranked members, its call
    (up, flat, down; wait without a ranked member) and every forecast with its weight."""
    weights = _weights(advisory.members, rules, advisory.ranked)
    counted = [m for m in advisory.members if weights[m.name] > 0]
    pooled: tuple[float, float, float] | None = None
    if counted:
        first, middle, third = (sum(weights[m.name] * m.probabilities[i] for m in counted) for i in range(3))
        pooled = (first, middle, third)
    margin = None if pooled is None else pooled[0] - pooled[2]
    call = (
        WAIT
        if margin is None
        else UP
        if margin >= rules.margin
        else DOWN
        if margin <= -rules.margin
        else FLAT
    )
    forecasters = []
    for m in advisory.members:
        own = m.probabilities[0] - m.probabilities[2]
        forecasters.append(
            {
                "name": m.name,
                "family": rules.family(m.name),
                "status": m.status,
                "probabilities": _classes_json(advisory.classes, m.probabilities),
                "lean": rules.lean(own),
                "strength": rules.strength(own),
                "margin": own,
                "weight": weights[m.name],
                "counted": weights[m.name] > 0,
                "ranked": m.name in advisory.ranked,
            }
        )
    return {
        "kind": advisory.kind,
        "sessions": advisory.sessions,
        "call": call,
        "probabilities": None if pooled is None else _classes_json(advisory.classes, pooled),
        "families": len({rules.family(m.name) for m in counted}),
        "members": len(counted),
        "lean": None if margin is None else rules.lean(margin),
        "strength": None if margin is None else rules.strength(margin),
        "margin": margin,
        "forecasters": forecasters,
    }


def _expected_net(
    probabilities: tuple[float, float, float], market: Market, target: float, stop: float
) -> float | None:
    """A share's expected net of a long bought at the ask now (module doc); None without a quote, or
    with the ask not between the stop and the target (the price moved past a barrier while the members
    worked: no trade to plan, as ``money.size`` refuses a stop not below the ask)."""
    ask, bid, fee = market.ask, market.bid, market.fee_rate
    if ask is None or bid is None or not 0 < stop < ask < target:
        return None
    up, timeout, down = probabilities
    stopped = stop - tick_size(stop)  # the first bid below the stop sells (``money.at_risk``)
    return (
        up * net_of(ask, target, 1, fee)
        + down * net_of(ask, stopped, 1, fee)
        + timeout * net_of(ask, bid, 1, fee)
    )


def verdict(
    symbol: str,
    bar_close: datetime,
    horizons: Sequence[Horizon],
    market: Market,
    held: Held | None = None,
    side: Sequence[Side] = (),
    rules: Rules = RULES,
    advisory: Sequence[Advisory] = (),
) -> Verdict:
    """The verdict of ``symbol`` at ``bar_close`` (module doc) from its barrier ``horizons``, the
    prices (``market``), the trader's position in it (``held``), the forecasts shown beside it
    (``side``) and the day and the week, advisory (#697)."""
    pools: list[Pooled] = []
    members: list[dict[str, Any]] = []
    for h in sorted(horizons, key=lambda h: h.minutes):
        pooled, shown = _pool(h, market, rules)
        pools.append(pooled)
        members += shown
    seen = [p for p in pools if p.margin is not None]
    advised = [advise(a, rules) for a in advisory]

    def made(action: str, meaning: str | None, reason: str | None, on: Pooled | None) -> Verdict:
        return Verdict(
            symbol, bar_close, action, meaning, reason, on, held, pools, members, side, rules, advised
        )

    if not seen:
        return made(WAIT, None, NO_MEMBERS, None)
    if held is not None:
        left = (held.time_barrier - bar_close).total_seconds() / 60
        covering = [p for p in seen if p.minutes >= left]
        at = covering[0] if covering else seen[-1]
        if (at.margin or 0.0) <= -rules.margin:
            return made(SELL, EXIT, None, at)
        return made(WAIT, None, HOLDING, at)
    priced = [p for p in seen if p.expected_net is not None and math.isfinite(p.expected_net)]
    best = max(priced, key=lambda p: p.expected_net or 0.0, default=None)
    if best is not None and (best.expected_net or 0.0) > 0 and (best.margin or 0.0) >= rules.margin:
        return made(BUY, None, None, best)
    against = min(seen, key=lambda p: p.margin or 0.0)
    if (against.margin or 0.0) <= -rules.margin:
        return made(SELL, AVOID, None, against)
    return made(WAIT, None, NO_EDGE, best or seen[0])
