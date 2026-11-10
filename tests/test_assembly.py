"""The engine's assembly (#647, ``assembly.py``): one function builds the engine as the server's in
each mode, and ``target_version`` is never left to a default.

Input: settings unlike the defaults (symbols, fee, budget, review rules, leading series, target
version, commit), in a temporary state and data directory. Output: the options each mode gives the
engine, and the engines built from them.
"""

from __future__ import annotations

import inspect
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from krx_quant.assembly import (
    DAILY,
    GATES,
    LIVE_ONLY,
    MODE_OPTIONS,
    TRADER,
    EngineOptions,
    Gates,
    Mode,
    build_engine,
    engine_options,
)
from krx_quant.clock import FakeClock
from krx_quant.config import TOUCH_TARGET, Settings
from krx_quant.engine import PredictionEngine
from krx_quant.market_calendar import MarketCalendar
from krx_quant.predictions import PredictionStore
from krx_quant.replay import replay
from tests.replay_events import at

CALENDAR = MarketCalendar()

# The engine's keyword arguments the assembly leaves to their defaults in every mode, and why. A new
# one is either set from the settings (then every mode has it) or named here.
LEFT_TO_DEFAULTS = {
    "feature_store": "a fresh one per engine",
    "notify": "the collector's alerts, given once it has its HTTP session",
    "signals": "likewise",
    "consensus_timing": "no setting",
    "daily_kinds": "every daily kind; none run without the KRX days (``daily``)",
}


def settings_of(tmp_path: Path) -> Settings:
    return Settings(
        kis_app_key="key",
        kis_app_secret="secret",
        symbols=("005930", "000660"),
        index_codes=("0001",),
        us_symbols=("NVDA",),
        etf_symbols=("133690",),
        fee_rate=0.0002,
        algorithm_seconds=4.0,
        cpu_budget_seconds=12.0,
        min_sample=7,
        probation_review_days=(5, 10),
        bench_review_days=(10, 20),
        review_confidence=0.9,
        target_version=TOUCH_TARGET,
        commit="abc1234",
        data_dir=tmp_path / "data",
        state_dir=tmp_path / "state",
    )


def options(settings: Settings, mode: Mode, gates: Gates | None = None) -> EngineOptions:
    return engine_options(settings, mode, CALENDAR, models={}, gates=gates)


def comparable(value: Any) -> Any:
    """``value`` as compared between modes: a function bound to the settings by what it is bound
    to, an object without an equality of its own by its type and attributes (the previous days,
    the daily kinds' sources); a function, by itself (the live gates are made anew each time)."""
    if isinstance(value, partial):
        return (value.func, tuple(comparable(v) for v in value.args), value.keywords)
    if inspect.isfunction(value) or inspect.ismethod(value):
        return value
    if "__eq__" not in vars(type(value)) and hasattr(value, "__dict__"):
        return (type(value), {k: comparable(v) for k, v in vars(value).items()})
    return value


def differing(a: EngineOptions, b: EngineOptions) -> set[str]:
    assert a.keys() == b.keys()
    return {k for k in a if comparable(a[k]) != comparable(b[k])}  # type: ignore[literal-required]


def test_every_keyword_of_the_engine_is_set_by_the_assembly_or_left_to_its_default_on_purpose() -> None:
    keywords = {
        name
        for name, p in inspect.signature(PredictionEngine.__init__).parameters.items()
        if p.kind is inspect.Parameter.KEYWORD_ONLY
    }
    set_by_assembly = set(EngineOptions.__annotations__)

    assert keywords == set_by_assembly | set(LEFT_TO_DEFAULTS)
    assert not set_by_assembly & set(LEFT_TO_DEFAULTS)
    assert MODE_OPTIONS <= set_by_assembly


def test_the_live_and_the_replayed_engine_differ_only_in_what_the_mode_sets(tmp_path: Path) -> None:
    settings = settings_of(tmp_path)

    live = options(settings, Mode.LIVE)
    replayed = options(settings, Mode.REPLAY)

    assert differing(live, replayed) == MODE_OPTIONS
    assert replayed["target_version"] == TOUCH_TARGET
    assert replayed["budget"].per_algorithm == 4.0 and replayed["min_sample"] == 7
    assert replayed["leaders"].indices == ("0001",) and replayed["review_rules"].confidence == 0.9


def test_a_setting_reaches_every_mode(tmp_path: Path) -> None:
    settings = settings_of(tmp_path)
    # Another target than the one settings allow (#659): a value only to see it reach every mode.
    changed = replace(settings, target_version=TOUCH_TARGET + 1, min_sample=3)

    for mode in Mode:
        # The live gates are made anew each time: they always differ.
        assert differing(options(settings, mode), options(changed, mode)) - GATES == {
            "target_version",
            "min_sample",
        }, mode


def test_only_the_live_engine_has_the_live_parts_and_the_replay_no_daily_kinds(tmp_path: Path) -> None:
    settings = settings_of(tmp_path)

    live, replayed, demo = (options(settings, m) for m in (Mode.LIVE, Mode.REPLAY, Mode.DEMO))

    assert live["watch_loop"] and live["garbage"] is not None
    assert live["commit"] == "abc1234" and live["signal_rule"] == settings.signal_thresholds
    assert live["admits"] is not None and live["refused"] is not None and live["revisions"] is not None
    for other in (replayed, demo):
        assert not other["watch_loop"] and other["garbage"] is None
        assert other["commit"] == "unknown" and other["signal_rule"] is None
        assert other["admits"] is None and other["refused"] is None and other["revisions"] is None
    assert replayed["daily"] is None and not replayed["daily_algorithms"]
    assert demo["daily"] is not None and demo["daily_algorithms"] == live["daily_algorithms"]
    assert differing(demo, replayed) == DAILY | TRADER
    assert replayed["held"] is None and demo["held"] is not None, "the trader's positions (#676)"
    assert differing(live, demo) == GATES | LIVE_ONLY


def test_the_gates_are_the_server_s_live_and_the_caller_s_otherwise(tmp_path: Path) -> None:
    settings = settings_of(tmp_path)
    gates = Gates(admits=lambda a: False)

    assert options(settings, Mode.REPLAY, gates)["admits"] is gates.admits
    assert options(settings, Mode.DEMO, gates)["admits"] is gates.admits
    with pytest.raises(ValueError, match="server's"):
        options(settings, Mode.LIVE, gates)


def test_the_built_engine_runs_on_the_settings(tmp_path: Path) -> None:
    settings = settings_of(tmp_path)
    engine = build_engine(
        settings,
        Mode.LIVE,
        PredictionStore(tmp_path / "engine.db"),
        FakeClock(at("08:00:00")),
        CALENDAR,
        models={},
    )
    try:
        assert engine.symbols == settings.symbols and engine.fee_rate == settings.fee_rate
        assert engine.target_version == TOUCH_TARGET and engine.commit == "abc1234"
        assert engine.garbage is not None and engine.min_sample == 7
        assert {a.name for a in engine.daily_registered}, "the daily kinds run live"
    finally:
        engine.store.close()


@pytest.mark.parametrize("function", [PredictionEngine.__init__, replay])
def test_the_target_version_has_no_default(function: Any) -> None:
    parameter = inspect.signature(function).parameters["target_version"]

    assert parameter.default is inspect.Parameter.empty
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
