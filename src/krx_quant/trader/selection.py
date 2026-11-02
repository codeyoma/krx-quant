"""The cells the trader trades, picked from the engine's cost map (#395, #393 "매매 대상 칸").

**Where from.** The trader asks the Engine API for 005930's cost map (``GET /v1/cost-map``,
``cost_map.py``, #307), of the last 20 days with predictions (``COST_MAP_PERIOD``), the special
situations left out, at ``KRX_TRADER_ENGINE_URL`` (compose: ``http://engine:8080``, inside the
compose network). The engine computes it as the cost screen's (its leaderboards, its kinds,
baselines and consensus), so the trader neither repeats that nor holds every scored prediction in
its own memory. Without the engine there are no quotes either (the collector streams them), so the
trader depends on nothing more.

**Which cells.** A cell is a (kind, predictor) of the traded kinds (``TRADED_KINDS``) and the cost
map's three predictors (``ROLES``): the final consensus (the direction's only), the kind's
first-ranked algorithm, its strongest baseline. Every cell the cost map names is kept with its
figures (``Selection.named``, a figure the cost map lacks None): the champion's cells trade whatever
their verdict (#472, ``policy.apply``: the nightly fit judged each on its replayed one-share net). A
cell whose verdict at a market order's round trip (``verdict.market``) is ``pass`` (its hit rate's
lower bound above the breakeven p*) is also a pick (``Selection.picks``): what the cost map alone
would trade, kept as a diagnosis (``no_pass`` when none passes) and no longer a condition.

> **바뀜** (2026-11-03, #480): a cell traded only when it passed (``fail``, ``few`` and no verdict
> never traded), and a pass whose f* was not above 0 was not picked → the cost map's verdict is a
> diagnosis; the champion's cells trade. 이유: spec #468 3 "진입": the nightly fit enters a cell by
> the lower bound of its replayed one-share net instead.

**Rank.** The picks are ranked by their hit rate's lower bound less p* (``Pick.priority``),
greatest first; of two alike, by the kinds' order, then the predictors'. Under a policy its own order
ranks its cells (``policy.apply``). Of the rises of one bar close, the first ranked buys
(``paper.py``).

**Kelly.** Each pick's Kelly fraction of the capital, from its hit rate's lower bound p and the
row's width W and market cost C (ticks) at its reference price: a call right gains W − C ticks,
one wrong loses W + C (the target and the stop are W ticks off, the cost paid both ways), as
fractions of the price g = (W − C) × tick ÷ price and ℓ = (W + C) × tick ÷ price. The growth
p log(1 + f g) + (1 − p) log(1 − f ℓ) is greatest at f* = p ÷ ℓ − (1 − p) ÷ g, positive exactly
when p > p* = (W + C) ÷ 2W. It is a diagnosis alone (``Pick.kelly``, #471): a trade's loss budget
sizes the buy (``paper.size``).

> **바뀜** (2026-11-03, #478): the trader bought ``KELLY_FRACTION`` (a half) of f*, cut to 0–1
> (``Pick.fraction``), of the capital → f* sizes nothing. 이유: spec #468 4 "크기": with stops a few
> ticks away f* is mostly far above 1, the whole capital.

**Practice** (#427, ``Selection.practisable``). Each cost map also names the cells the mock book may
practise on a day no champion cell trades (``desk.Desk``, ``KRX_TRADER_MOCK_PRACTICE``): the cells
with a verdict at a market order's round trip (``pass`` or ``fail``; ``few`` and a cell without a
predictor or a p* are left out), their rank's figures finite (the lower bound, p*) and their row's
barrier sound (W and the tick above 0, C at least 0, its reference price above 0), in the picks'
order. Each buys one share (``PRACTICE_SHARES``, ``Pick.quantity``: its Kelly is mostly at most 0),
within the cash and the capital still. The nightly job freezes one of them, at a θ, for a whole
review cycle (#474, ``practice.py``: the greatest lower bound of the net a signal replayed). It is
no pick of the champion's: its trades carry no policy and count in no record.

> **바뀜** (2026-11-03, #479): each cost map named the practice cell (``Selection.practice``: the
> greatest lower bound less p* of these), which every pick took, at ``KRX_TRADER_THRESHOLD`` → it
> names the cells that may be practised, and the nightly job freezes one for a review cycle at its
> own θ. 이유: spec #468 6 "연습" (the user's choice, 2026-11-02): a practice re-picked at every pick
> was no fixed strategy.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

import aiohttp

from krx_quant.trader import COST_MAP_PERIOD, ROLES, SYMBOL, TRADED_KINDS
from krx_quant.trader.cells import Cell

log = logging.getLogger(__name__)

PASS = "pass"
# Why no cell trades.
NO_PASS = "no_pass"  # the cost map was read: no cell's verdict passes (and no champion's cell trades)
NO_COST_MAP = "no_cost_map"  # it could not be read
NOT_IN_POLICY = (
    "not_in_policy"  # cells pass, none of them the champion's, none of its trading (``policy.apply``)
)
NO_POLICY = "no_policy"  # the champion policy could not be read (#399): nothing trades
NO_CHAMPION = "no_champion"  # no champion yet (#469): nothing trades before the first
# Why a policy's cell does not trade (#469, ``policy.apply``): its predictor stopped predicting (for
# the rest of the review cycle), or (#472) the cost map could not be read (``NO_COST_MAP``).
GONE = "gone"
# Why it did not trade before #472, kept by the picks of then: the cost map did not name it that day
# (another predictor held its role). Since #472 such a cell trades (``Pick.unmapped``).
OFF_MAP = "not_on_cost_map"
# A cell with one of these verdicts may be practised (#427): ``few`` and none are left out.
FAIL = "fail"
PRACTISED = (PASS, FAIL)
# The shares a practice buy takes (#427).
PRACTICE_SHARES = 1
# The cost map can take a while: the engine computes it over 20 days, in a worker thread.
TIMEOUT_SECONDS = 120.0


class CostMapError(Exception):
    """The cost map could not be read, or is not one."""


@dataclass(frozen=True)
class Pick:
    """A cell of the cost map, with its figures there (#472: each None the cost map lacks, every one
    for a champion's cell it does not name)."""

    kind: str
    role: str  # consensus, top or baseline
    predictor: str  # its name: consensus-final, an algorithm's, a baseline's
    p_star: float | None  # the breakeven hit rate at a market order's round trip
    low: float | None  # its hit rate's lower bound (the Wilson 95%)
    calls: int
    width: float | None  # the row's W, ticks
    cost: float | None  # the row's market round trip C, ticks
    reference_price: float | None  # where W and C were read
    tick: float | None
    # f*, uncut, a diagnosis alone (#471); None for a practice cell whose call right does not pay (#427)
    kelly: float | None
    # The rise's probability that buys, the policy's for this cell (#399), or the practice cell's frozen
    # for the review cycle (#474); None: the trader's ``Rules.threshold``.
    threshold: float | None = None
    # The shares it buys, within the cash and the capital, in place of a loss budget's (#427: a practice
    # cell's one); None: the loss budget's (#471, ``paper.size``).
    quantity: int | None = None
    # The mock book's practice cell (#427, module doc), not a champion's.
    practice: bool = False

    @property
    def cell(self) -> Cell:
        return Cell(self.kind, self.predictor)

    @property
    def priority(self) -> float | None:
        """Its hit rate's lower bound less p*; None without either."""
        return None if self.low is None or self.p_star is None else self.low - self.p_star

    @classmethod
    def unmapped(cls, kind: str, role: str, predictor: str) -> Pick:
        """A champion's cell the cost map does not name today (#472: it trades all the same): no
        figure."""
        return cls(kind, role, predictor, None, None, 0, None, None, None, None, None)

    def as_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "role": self.role,
            "predictor": self.predictor,
            "p_star": self.p_star,
            "low": self.low,
            "priority": self.priority,
            "calls": self.calls,
            "width": self.width,
            "cost": self.cost,
            "reference_price": self.reference_price,
            "tick": self.tick,
            "kelly": self.kelly,
            "threshold": self.threshold,
            "quantity": self.quantity,
            "practice": self.practice,
        }


@dataclass(frozen=True)
class Selection:
    """The cells picked from one cost map, ranked; with every cell considered and, when none
    trades, why (``NO_PASS``, ``NO_COST_MAP`` with ``detail``, ``policy.NOT_IN_POLICY``). Under a
    policy (#399, ``policy.apply``), the policy's cells among them, in its order, and its version."""

    picks: tuple[Pick, ...]
    considered: tuple[dict[str, Any], ...] = ()
    as_of: str | None = None  # the cost map's
    reason: str | None = None
    detail: str | None = None
    policy: int | None = None  # the policy version applied; None: none yet (#399)
    # The cells the mock book may practise on this cost map (#427, module doc), in the picks' order;
    # the nightly job freezes one for a review cycle (#474, ``practice.py``).
    practisable: tuple[Pick, ...] = ()
    # Under a policy (#469, ``policy.apply``): each of its cells in its order, whether it trades and,
    # if not, why (``GONE``, ``NO_COST_MAP``; ``OFF_MAP`` or the cost map's verdict before #472).
    cells: tuple[dict[str, Any], ...] = ()
    # The evaluation version and the review cycle the pick is made in (#469, ``review.py``); None
    # before the evaluation was begun.
    evaluation: int | None = None
    cycle: int | None = None
    # Every cell the cost map names, with its figures, whatever its verdict (#472): what a policy's
    # cells trade on (``policy.apply``); empty under a policy and for a cost map not read.
    named: tuple[Pick, ...] = ()

    @property
    def practising(self) -> bool:
        """Whether its picks are the practice cell (#427, ``desk.Desk``): no champion cell trades."""
        return any(p.practice for p in self.picks)

    def describe(self) -> str:
        """A line for the log."""
        if self.practising:
            why = replace(self, picks=()).describe()
            p = self.picks[0]
            theta = "" if p.threshold is None else f" at θ {p.threshold:g}"
            return f"practice on {p.kind} {p.predictor}{theta}, a share at a time ({why})"
        if self.picks:
            under = "" if self.policy is None else f"policy {self.policy}: "
            return under + "; ".join(
                f"{p.kind} {p.predictor} ({p.role}: {_figures(p)}"
                + ("" if p.threshold is None else f", θ {p.threshold:g}")
                + ")"
                for p in self.picks
            )
        if self.reason == NO_COST_MAP:
            return f"no cost map ({self.detail})"
        if self.reason == NO_POLICY:
            return f"the champion policy cannot be read ({self.detail})"
        seen = [
            f"{c['kind']} {c['role']} {c['predictor'] or '-'} {c['verdict'] or c['why']}"
            + (f" ({c['why']})" if c["verdict"] and c.get("why") else "")
            for c in self.considered
        ]
        if self.reason == NO_CHAMPION:
            return "no champion yet: nothing trades before the first (#469); " + "; ".join(seen)
        if self.reason == NOT_IN_POLICY:
            return f"none of policy {self.policy}'s cells trades: " + "; ".join(seen) + self._missing()
        return "none passes: " + "; ".join(seen) + self._missing()

    def _missing(self) -> str:
        """The policy's cells gone (#469), for the log line."""
        found = [f"{c['kind']} {c['predictor']} {c['why']}" for c in self.cells if not c["traded"]]
        return f" (policy {self.policy}: " + "; ".join(found) + ")" if found else ""


def _figures(p: Pick) -> str:
    """A pick's cost map figures for the log line (#472: off the cost map, none)."""
    if p.priority is None:
        return "not on the cost map" if p.low is None and p.p_star is None else "no lower bound or p*"
    assert p.low is not None and p.p_star is not None
    return f"low {p.low:.3f} − p* {p.p_star:.3f} = {p.priority:+.3f}, f* {p.kelly or 0:.2f}"


def kelly(low: float, width: float, cost: float, tick: float, price: float) -> float | None:
    """f* = p ÷ ℓ − (1 − p) ÷ g (module doc); None when a call right does not pay its cost
    (W ≤ C) or a figure is not there."""
    if not (width > cost >= 0 and tick > 0 and price > 0):
        return None
    gain = (width - cost) * tick / price
    loss = (width + cost) * tick / price
    return low / loss - (1 - low) / gain


def choose(body: Mapping[str, Any]) -> Selection:
    """The cells ``body``, a cost map of 005930, passes (module doc); ``CostMapError`` when it is
    not the cost map asked for."""
    if not isinstance(body, Mapping) or not isinstance(body.get("rows"), list):
        raise CostMapError("no rows in the answer")
    if body.get("symbol") != SYMBOL or body.get("period") != COST_MAP_PERIOD:
        asked = f"{SYMBOL}'s of {COST_MAP_PERIOD}"
        raise CostMapError(f"the cost map of {body.get('symbol')!r}, {body.get('period')!r}: not {asked}")
    as_of = body.get("as_of") if isinstance(body.get("as_of"), str) else None
    rows = {r.get("kind"): r for r in body["rows"] if isinstance(r, Mapping)}
    picks: list[Pick] = []
    named: dict[Cell, Pick] = {}  # every cell named, the first of a predictor named twice (#472)
    practised: list[Pick] = []  # the cells that may be practised (#427, module doc)
    considered: list[dict[str, Any]] = []
    for kind in TRADED_KINDS:
        row = rows.get(kind)
        for role in ROLES:
            if role == "consensus" and kind != "direction":
                continue  # the kinds have no consensus
            seen: dict[str, Any] = {"kind": kind, "role": role, "predictor": None, "verdict": None}
            seen |= {"low": None, "p_star": None, "calls": None, "picked": False, "why": None}
            considered.append(seen)
            if row is None:
                seen["why"] = "no row"
                continue
            predictor = row.get(role)
            if not isinstance(predictor, Mapping) or not isinstance(predictor.get("algorithm"), str):
                seen["why"] = "no predictor"
                continue
            calls = predictor.get("calls")
            seen["predictor"] = predictor["algorithm"]
            seen["calls"] = calls = calls if isinstance(calls, int) and not isinstance(calls, bool) else None
            low = _number(_dig(predictor, "precision", "low"))
            p_star = _number(_dig(row, "breakeven", "market", "p_star"))
            seen["low"], seen["p_star"] = low, p_star
            verdict = _dig(predictor, "verdict", "market")
            seen["verdict"] = verdict
            figures = {
                "low": low,
                "p_star": p_star,
                "width": _number(row.get("width")),
                "cost": _number(_dig(row, "cost", "market")),
                "reference_price": _number(row.get("reference_price")),
                "tick": _number(row.get("tick")),
            }
            known = {name: v for name, v in figures.items() if v is not None}
            f = None
            if len(known) == len(figures):
                f = kelly(
                    known["low"], known["width"], known["cost"], known["tick"], known["reference_price"]
                )
            name = predictor["algorithm"]
            cell = _pick(kind, role, name, calls or 0, figures, f)
            named.setdefault(cell.cell, cell)
            if verdict is None:
                seen["why"] = row.get("breakeven_missing") or "no p*"
                continue
            if verdict in PRACTISED and _sound(known, len(figures)):
                practised.append(
                    _pick(kind, role, name, calls or 0, figures, f, PRACTICE_SHARES, practice=True)
                )
            if verdict != PASS:
                continue
            seen["picked"] = True
            picks.append(cell)
    picks.sort(key=_rank)
    unique: dict[Cell, Pick] = {}
    for p in picks:
        unique.setdefault(p.cell, p)
    practisable: dict[Cell, Pick] = {}
    for p in sorted(practised, key=_rank):
        practisable.setdefault(p.cell, p)
    return Selection(
        tuple(unique.values()),
        tuple(considered),
        as_of,
        None if unique else NO_PASS,
        practisable=tuple(practisable.values()),
        named=tuple(named.values()),
    )


def _pick(
    kind: str,
    role: str,
    predictor: str,
    calls: int,
    figures: Mapping[str, float | None],
    f: float | None,
    quantity: int | None = None,
    practice: bool = False,
) -> Pick:
    """A cell on a row's ``figures`` (each None the row lacks)."""
    return Pick(
        kind=kind,
        role=role,
        predictor=predictor,
        p_star=figures["p_star"],
        low=figures["low"],
        calls=calls,
        width=figures["width"],
        cost=figures["cost"],
        reference_price=figures["reference_price"],
        tick=figures["tick"],
        kelly=f,
        quantity=quantity,
        practice=practice,
    )


def _rank(p: Pick) -> tuple[float, int, int]:
    """The picks' order (module doc): the greatest lower bound less p* first (one without either
    last), then the kinds', then the predictors'."""
    priority = p.priority
    return (math.inf if priority is None else -priority, TRADED_KINDS.index(p.kind), ROLES.index(p.role))


def _sound(known: Mapping[str, float], wanted: int) -> bool:
    """Whether a row's figures can rank and bound a practice cell (#427, module doc): every one there
    and finite, W and the tick above 0, C at least 0, the reference price above 0."""
    return (
        len(known) == wanted
        and known["width"] > 0
        and known["tick"] > 0
        and known["cost"] >= 0
        and known["reference_price"] > 0
    )


async def read_cost_map(session: aiohttp.ClientSession, engine_url: str) -> dict[str, Any]:
    """005930's cost map of ``COST_MAP_PERIOD`` from the Engine API at ``engine_url``."""
    url = engine_url.rstrip("/") + "/v1/cost-map"
    params = {"symbol": SYMBOL, "period": COST_MAP_PERIOD, "special": "exclude"}
    try:
        async with session.get(
            url, params=params, timeout=aiohttp.ClientTimeout(total=TIMEOUT_SECONDS)
        ) as response:
            text = await response.text()
            if response.status != 200:
                raise CostMapError(f"HTTP {response.status} from {url}: {text[:200]}")
    except (TimeoutError, aiohttp.ClientError) as e:
        raise CostMapError(f"{url}: {e.__class__.__name__} {e}".strip()) from e
    try:
        body = json.loads(text)
    except ValueError as e:
        raise CostMapError(f"{url} answered what is not JSON") from e
    if not isinstance(body, dict):
        raise CostMapError(f"{url} answered what is not an object")
    return body


async def pick(session: aiohttp.ClientSession, engine_url: str) -> Selection:
    """The cells of the cost map the engine gives now; none, with why, when it cannot be read."""
    try:
        return choose(await read_cost_map(session, engine_url))
    except CostMapError as e:
        return Selection((), reason=NO_COST_MAP, detail=str(e))


def _dig(value: Any, *keys: str) -> Any:
    """``value[k1][k2]...``; None where a step is missing or not an object."""
    for key in keys:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return float(value)
