"""Engine assembly (#647): the engine as the server's, for the live collector, a recorded day's
replay and the demo, built in one place from the settings and a mode.

What comes from the settings (the symbols, the fee, the time budget, the leading series, the
review rules, the target version, the macro regimes and the previous trading days) is the same in
every mode, so a setting added for the live engine reaches the replays too (pre-validation, the
simulation, ``krx-quant replay``). The mode decides the rest, and only that:

- ``LIVE`` (the collector): the server's gates (pre-validations, refusals, revisions, read from
  ``state/``), the daily kinds, and what only the live engine has: the loop watch, the garbage
  collection after each bar close and the commit it runs. Its alerts are given once the collector
  has its HTTP session (``engine.notify``).
- ``REPLAY`` (a recorded day): every algorithm given runs unless gates are given, no daily kinds,
  nothing live.
- ``DEMO`` (the demo's morning): as ``REPLAY``, with the daily kinds; its gates are the demo's own.

``LIVE`` and ``DEMO`` read the trader's positions from its store (``state/trader.sqlite3``) for each
bar close's verdicts (#676, ``held``); ``REPLAY`` holds nothing.

A caller gives what is the mode's own input, not the server's configuration: the store, the clock,
the models and the climate table, the algorithms and kinds (pre-validation's candidate) and, but
live, the gates.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from functools import partial
from typing import TYPE_CHECKING, TypedDict

from krx_quant.algorithms import (
    ALGORITHMS,
    DAILY_ALGORITHMS,
    KIND_ALGORITHMS,
    Algorithm,
    KindAlgorithm,
    Model,
)
from krx_quant.backfill import PreviousDay
from krx_quant.clock import SessionClock
from krx_quant.config import Settings
from krx_quant.daily import DailyAlgorithm, DailySource, StoredDays
from krx_quant.daily import Features as DailyFeatures
from krx_quant.daily_features import DAILY_FEATURES, LiveFeatures
from krx_quant.engine import Admits, Budget, HeldNow, PredictionEngine, Refused, Regimes
from krx_quant.features import Leaders, PreviousDays
from krx_quant.garbage import Garbage
from krx_quant.kind_registry import KINDS
from krx_quant.kinds import Kind
from krx_quant.lifecycle import Rules
from krx_quant.macro import day_regime
from krx_quant.market_calendar import MarketCalendar
from krx_quant.open_to_close import OPEN_TO_CLOSE
from krx_quant.predictions import PredictionStore
from krx_quant.trader.store import DB_NAME as TRADER_DB
from krx_quant.trader.store import read_held

if TYPE_CHECKING:  # the revisions read the pre-validation, which replays through this module
    from krx_quant.revisions import RevisionGate


class Mode(Enum):
    LIVE = "live"
    REPLAY = "replay"
    DEMO = "demo"


@dataclass(frozen=True)
class Gates:
    """Which algorithms may run (``admits``), which of those kept out were refused, and the code
    each running bar-close algorithm runs (#454). Without: every registered algorithm runs."""

    admits: Admits | None = None
    refused: Refused | None = None
    revisions: RevisionGate | None = None


class EngineOptions(TypedDict):
    """``PredictionEngine``'s keyword arguments as the assembly sets them."""

    algorithms: Sequence[Algorithm]
    models: Mapping[str, Model]
    budget: Budget
    min_sample: int
    leaders: Leaders
    regimes: Regimes
    admits: Admits | None
    refused: Refused | None
    revisions: RevisionGate | None
    commit: str
    review_rules: Rules
    target_version: int
    climate: Model | None
    kinds: Sequence[Kind]
    kind_algorithms: Sequence[KindAlgorithm]
    previous: PreviousDays
    daily: DailySource | None
    daily_algorithms: Sequence[DailyAlgorithm]
    daily_features: DailyFeatures | None
    daily_feature_names: Collection[str]
    watch_loop: bool
    garbage: Garbage | None
    held: HeldNow | None


# The options each mode sets of its own (module doc); the others are the settings', in every mode.
GATES = frozenset({"admits", "refused", "revisions"})
DAILY = frozenset({"daily", "daily_algorithms", "daily_features", "daily_feature_names"})
TRADER = frozenset({"held"})  # the trader's positions (#676), live and in the demo
LIVE_ONLY = frozenset({"commit", "watch_loop", "garbage"})
MODE_OPTIONS = GATES | DAILY | TRADER | LIVE_ONLY


def live_gates(settings: Settings) -> Gates:
    """The server's gates: a new algorithm waits for its pre-validation on the target it runs, one
    its latest pre-validation refused is told once, not as waiting (#310), and a running bar-close
    algorithm whose code changed is judged again (#454)."""
    from krx_quant.prevalidation import admits, refusals
    from krx_quant.revisions import RevisionGate

    return Gates(
        admits(settings.state_dir, settings.target_version),
        refusals(settings.state_dir, settings.target_version),
        RevisionGate(settings.state_dir, settings.target_version),
    )


def engine_options(
    settings: Settings,
    mode: Mode,
    calendar: MarketCalendar,
    *,
    models: Mapping[str, Model],
    climate: Model | None = None,
    algorithms: Sequence[Algorithm] = ALGORITHMS,
    kinds: Sequence[Kind] = KINDS,
    kind_algorithms: Sequence[KindAlgorithm] = KIND_ALGORITHMS,
    gates: Gates | None = None,
) -> EngineOptions:
    """The engine's options in ``mode`` (module doc). ``gates`` are the live engine's own
    (``live_gates``): given in ``LIVE``, it refuses."""
    if mode is Mode.LIVE:
        if gates is not None:
            raise ValueError("the live engine's gates are the server's")
        gates = live_gates(settings)
    gates = gates or Gates()
    daily = mode is not Mode.REPLAY  # the KRX day candles the daily kinds are predicted from (#264)
    live = mode is Mode.LIVE
    return EngineOptions(
        algorithms=algorithms,
        models=models,
        budget=Budget.of(settings),
        min_sample=settings.min_sample,
        leaders=Leaders.of(settings),
        regimes=partial(day_regime, settings.data_dir),  # recorded by the 07:30 macro read
        admits=gates.admits,
        refused=gates.refused,
        revisions=gates.revisions,
        commit=settings.commit if live else "unknown",
        review_rules=Rules.of(settings),
        target_version=settings.target_version,  # the target learned and forecast (#169)
        climate=climate,
        kinds=kinds,
        kind_algorithms=kind_algorithms,
        # The previous trading day's backfilled bars (range_prev_day, #181).
        previous=PreviousDay(settings.data_dir, calendar),
        # The daily kinds' KRX day candles, their algorithms and their features at 08:50.
        daily=StoredDays(settings.data_dir) if daily else None,
        daily_algorithms=DAILY_ALGORITHMS if daily else (),
        daily_features=LiveFeatures(
            OPEN_TO_CLOSE,
            settings.data_dir,
            settings.state_dir,
            calendar,
            settings.symbols,
            settings.fee_rate,
        )
        if daily
        else None,
        daily_feature_names=DAILY_FEATURES if daily else (),
        # A bar close that holds the event loop over a second says what held it (#538).
        watch_loop=live,
        # A garbage collection and a freeze after each bar close, let go when idle (#621).
        garbage=Garbage() if live else None,
        # The trader's positions, for a verdict's SELL (#676): its store, live and in the demo.
        held=partial(read_held, settings.state_dir / TRADER_DB) if mode is not Mode.REPLAY else None,
    )


def build_engine(
    settings: Settings,
    mode: Mode,
    store: PredictionStore,
    clock: SessionClock,
    calendar: MarketCalendar,
    *,
    models: Mapping[str, Model],
    climate: Model | None = None,
    algorithms: Sequence[Algorithm] = ALGORITHMS,
    kinds: Sequence[Kind] = KINDS,
    kind_algorithms: Sequence[KindAlgorithm] = KIND_ALGORITHMS,
    gates: Gates | None = None,
) -> PredictionEngine:
    """The engine as the server's in ``mode`` (``engine_options``), storing into ``store`` on
    ``clock``."""
    options = engine_options(
        settings,
        mode,
        calendar,
        models=models,
        climate=climate,
        algorithms=algorithms,
        kinds=kinds,
        kind_algorithms=kind_algorithms,
        gates=gates,
    )
    return PredictionEngine(store, calendar, settings.symbols, settings.fee_rate, clock, **options)
