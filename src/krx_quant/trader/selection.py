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
first-ranked algorithm, its strongest baseline. A cell is picked when its verdict at a market
order's round trip (``verdict.market``) is ``pass``: its hit rate's lower bound above the
breakeven p*. ``fail`` and ``few`` are not traded, nor a cell whose verdict is missing (no p*).

**Rank.** The picks are ranked by their hit rate's lower bound less p* (``Pick.priority``),
greatest first; of two alike, by the kinds' order, then the predictors'. Of the rises of one bar
close, the first ranked buys (``paper.py``).

**Kelly.** Each pick's Kelly fraction of the capital, from its hit rate's lower bound p and the
row's width W and market cost C (ticks) at its reference price: a call right gains W − C ticks,
one wrong loses W + C (the target and the stop are W ticks off, the cost paid both ways), as
fractions of the price g = (W − C) × tick ÷ price and ℓ = (W + C) × tick ÷ price. The growth
p log(1 + f g) + (1 − p) log(1 − f ℓ) is greatest at f* = p ÷ ℓ − (1 − p) ÷ g, positive exactly
when p > p* = (W + C) ÷ 2W. The trader buys ``KELLY_FRACTION`` of it, cut to 0–1
(``Pick.fraction``): with stops a few ticks away f* is mostly far above 1, the whole capital.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import aiohttp

from krx_quant.trader import COST_MAP_PERIOD, KELLY_FRACTION, ROLES, SYMBOL, TRADED_KINDS
from krx_quant.trader.cells import Cell

log = logging.getLogger(__name__)

PASS = "pass"
# Why no cell trades.
NO_PASS = "no_pass"  # the cost map was read: no cell's verdict passes
NO_COST_MAP = "no_cost_map"  # it could not be read
NOT_IN_POLICY = "not_in_policy"  # cells pass, none of them the champion policy's (#399, ``policy.apply``)
NO_POLICY = "no_policy"  # the champion policy could not be read (#399): nothing trades
# The cost map can take a while: the engine computes it over 20 days, in a worker thread.
TIMEOUT_SECONDS = 120.0


class CostMapError(Exception):
    """The cost map could not be read, or is not one."""


@dataclass(frozen=True)
class Pick:
    """A cell the cost map passes, with what it was picked on."""

    kind: str
    role: str  # consensus, top or baseline
    predictor: str  # its name: consensus-final, an algorithm's, a baseline's
    p_star: float  # the breakeven hit rate at a market order's round trip
    low: float  # its hit rate's lower bound (the Wilson 95%)
    calls: int
    width: float  # the row's W, ticks
    cost: float  # the row's market round trip C, ticks
    reference_price: float  # where W and C were read
    tick: float
    kelly: float  # f*, uncut
    # λ × f*, cut to 0–1: the share of the capital it buys (λ: KELLY_FRACTION, or the policy's, #399).
    fraction: float
    # The rise's probability that buys, the policy's for this cell (#399); None: the trader's
    # ``Rules.threshold`` (before the first policy).
    threshold: float | None = None

    @property
    def cell(self) -> Cell:
        return Cell(self.kind, self.predictor)

    @property
    def priority(self) -> float:
        return self.low - self.p_star

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
            "fraction": self.fraction,
            "threshold": self.threshold,
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

    def describe(self) -> str:
        """A line for the log."""
        if self.picks:
            under = "" if self.policy is None else f"policy {self.policy}: "
            return under + "; ".join(
                f"{p.kind} {p.predictor} ({p.role}: low {p.low:.3f} − p* {p.p_star:.3f} = {p.priority:+.3f},"
                f" Kelly {p.kelly:.2f} → {p.fraction:.2f}"
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
        if self.reason == NOT_IN_POLICY:
            return f"none of policy {self.policy}'s cells passes: " + "; ".join(seen)
        return "none passes: " + "; ".join(seen)


def kelly(low: float, width: float, cost: float, tick: float, price: float) -> float | None:
    """f* = p ÷ ℓ − (1 − p) ÷ g (module doc); None when a call right does not pay its cost
    (W ≤ C) or a figure is not there."""
    if not (width > cost >= 0 and tick > 0 and price > 0):
        return None
    gain = (width - cost) * tick / price
    loss = (width + cost) * tick / price
    return low / loss - (1 - low) / gain


def choose(body: Mapping[str, Any], kelly_fraction: float = KELLY_FRACTION) -> Selection:
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
            if verdict is None:
                seen["why"] = row.get("breakeven_missing") or "no p*"
                continue
            if verdict != PASS:
                continue
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
            if f is None or f <= 0:
                seen["why"] = "its pass cannot be sized" if f is None else "Kelly at most 0"
                continue
            seen["picked"] = True
            picks.append(
                Pick(
                    kind=kind,
                    role=role,
                    predictor=predictor["algorithm"],
                    calls=calls or 0,
                    kelly=f,
                    fraction=min(1.0, max(0.0, kelly_fraction * f)),
                    **known,
                )
            )
    picks.sort(key=lambda p: (-p.priority, TRADED_KINDS.index(p.kind), ROLES.index(p.role)))
    unique: dict[Cell, Pick] = {}
    for p in picks:
        unique.setdefault(p.cell, p)
    return Selection(tuple(unique.values()), tuple(considered), as_of, None if unique else NO_PASS)


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
