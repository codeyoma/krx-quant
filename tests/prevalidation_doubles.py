"""Pre-validation's inputs for the tests (#511): ten recorded trading days of swings, the Monday
morning after them, a stand-in algorithm with fixed forecasts and one against the swings, the
settings, and the report read back. Every value is made up.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from krx_quant.algorithms import Model, Sample
from krx_quant.baselines import Forecast
from krx_quant.config import Settings
from krx_quant.engine import Event
from krx_quant.features import FeatureVector
from krx_quant.feeds import KST
from tests.algorithm_doubles import FixedAlgorithm, stand_in_card, stand_in_meanings
from tests.replay_doubles import record
from tests.replay_events import at, book, trade

# Ten trading days, the fewest that can pass: Monday 14 to Friday 25 September.
DAYS = [d for d in (date(2026, 9, 14) + timedelta(days=i) for i in range(12)) if d.weekday() < 5]
AFTER = datetime.combine(date(2026, 9, 28), time(8), KST)  # the next Monday morning
ALPHA = FixedAlgorithm("alpha", features=("velocity",), unique=("velocity",))


def swings(day: date, end: str = "10:30:00") -> list[Event]:
    """A mid-point four ticks up and down every eight minutes: what went up for four
    minutes comes down the next four. A trade at the ask every ten seconds.
    """
    events: list[Event] = []
    start = when = at("09:00:00", day)
    while when <= at(end, day):
        phase = (when - start) / timedelta(minutes=8) % 1
        level = 4 * (4 * phase - 1 if phase < 0.5 else 3 - 4 * phase)
        mid = 81_250 + 100 * round(level)
        events += [book(when, mid - 50, mid + 50), trade(when + timedelta(seconds=1), mid + 50, 3)]
        when += timedelta(seconds=10)
    return events


class Contrarian:
    """Calls the last ``lag`` minutes undone: right on a market that swings back. Its own
    feature is ``own``; it reads the minutes' moves whatever it is."""

    training_days = 2

    def __init__(self, name: str, own: str = "minute_ticks", lag: int = 4) -> None:
        self.name = name
        self.features: tuple[str, ...] = tuple(dict.fromkeys(("minute_ticks", own)))
        self.card = stand_in_card(name, "the last minutes undone", (own,), "nothing")
        self.param_meanings = stand_in_meanings()
        self._lag = lag
        self.fitted: list[list[date]] = []  # the days each fit trained on

    def default_model(self) -> Model:
        return Model(f"{self.name}-default", {})

    def fit(self, samples: Sequence[Sample]) -> Model:
        self.fitted.append(sorted({s.bar_close.astimezone(KST).date() for s in samples}))
        return Model.fitted(self.name, {}, [s for s in samples if s.outcome is not None], 0.0)

    def predict(self, features: FeatureVector, model: Model) -> Forecast:
        change = sum(features.series("minute_ticks")[-self._lag :])
        if change >= 2:
            return Forecast(0.1, 0.2, 0.7, -change)
        if change <= -2:
            return Forecast(0.7, 0.2, 0.1, -change)
        return Forecast(0.2, 0.6, 0.2, 0.0)


def settings(tmp_path: Path, days: Sequence[date] = DAYS) -> Settings:
    data = tmp_path / "data"
    for day in days:
        record(data, swings(day), day=day)
    return Settings(
        kis_app_key="",
        kis_app_secret="",
        symbols=("005930",),
        etf_symbols=(),
        index_codes=(),
        us_symbols=(),
        data_dir=data,
        state_dir=tmp_path / "state",
        min_sample=20,
    )


def report(s: Settings, name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((s.state_dir / "prevalidations" / f"{name}.json").read_text())
    return data
