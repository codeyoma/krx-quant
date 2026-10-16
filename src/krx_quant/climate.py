"""The climate baseline's table (``baseline-climate``, #169): each symbol's most frequent
touch label in each 30-minute slot of the latest training days.

With the touch target (``KRX_TARGET_VERSION=3``, 2 before #187) the baseline calls, at every bar close,
the direction its symbol and slot took most often over the latest ``CLIMATE_DAYS``
training days (recorded days by their quotes, the others by the one-minute bar rule),
with certainty. It knows the time of day and nothing else: an algorithm that only learned
when the market moves does not beat it.

The table is a model like an algorithm's (``Model``): its parameters are
``<symbol>.<slot>`` (``"005930.09:00"`` holds the bar closes 09:01–09:30) with the
direction's code, 1 up, 0 flat, −1 down, and ``<symbol>.all``, the symbol's most frequent
label over the whole day. A slot the table lacks backs off to its symbol's day, then to
flat (a baseline calls every bar close, so the lifts pair it with every forecast). The
nightly training refits it every night and always installs it (``nightly.py``); the
engine reads ``state/models/baseline-climate.json`` and its defaults (no table: flat
everywhere) without one. A tie goes to flat, then up, as a forecast's most likely class
does.

The kinds' baselines that predict from a table (#180, ``kinds.TableBaseline``: the range's
climate) are kept the same way, each in ``state/models/<name>.json`` (``load_tables``).
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from krx_quant.algorithms.contract import Model, Sample, model_path
from krx_quant.consensus import slot
from krx_quant.feeds.base import KST
from krx_quant.kinds import Kind, table_baselines
from krx_quant.pricing import Direction
from krx_quant.touch import TARGET_VERSION

log = logging.getLogger(__name__)

CLIMATE = "baseline-climate"
CLIMATE_DAYS = 20  # the latest training days it counts, as most algorithms train on
DEFAULT = Model(f"{CLIMATE}-default", {})
_CODES = {Direction.UP: 1.0, Direction.FLAT: 0.0, Direction.DOWN: -1.0}
# Ties: flat, then up, then down (``pricing.most_likely``).
_TIES = (Direction.FLAT, Direction.UP, Direction.DOWN)


ALL_DAY = "all"  # the slot of a symbol's whole day, the table's back-off


def key(symbol: str, bar_close: datetime) -> str:
    """The table's parameter for ``symbol`` at ``bar_close``: ``<symbol>.<slot>``."""
    return f"{symbol}.{slot(bar_close)}"


def fit_climate(samples: Sequence[Sample]) -> Model:
    """The table of ``samples``' labels: each symbol and slot's most frequent one. The
    defaults when none has a label."""
    counts: dict[str, Counter[Direction]] = {}
    days: dict[str, Counter[Direction]] = {}  # by symbol, the whole day: the back-off
    for s in samples:
        if s.outcome is not None:
            counts.setdefault(key(s.symbol, s.bar_close), Counter())[s.outcome] += 1
            days.setdefault(f"{s.symbol}.{ALL_DAY}", Counter())[s.outcome] += 1
    if not counts:
        return DEFAULT
    params = {k: _CODES[_most_frequent(c)] for k, c in sorted({**counts, **days}.items())}
    labelled = [s for s in samples if s.outcome is not None]
    first = min(s.bar_close for s in labelled).astimezone(KST).date()
    last = max(s.bar_close for s in labelled).astimezone(KST).date()
    hits = sum(c[_most_frequent(c)] for c in counts.values())
    share = hits / len(labelled)
    canonical = json.dumps(params, sort_keys=True)
    digest = hashlib.sha256(f"{canonical}|{first}|{last}|{len(labelled)}".encode()).hexdigest()[:6]
    return Model(
        version=f"{CLIMATE}-{last:%Y%m%d}-{digest}",
        params=params,
        trained=f"{first}..{last}, {len(labelled)} samples, {share:.1%} on each slot's most frequent",
        trained_until=last,
        target_version=TARGET_VERSION,  # the touch target's labels, always (#170)
    )


def _most_frequent(counts: Counter[Direction]) -> Direction:
    best = max(counts.values())
    return next(d for d in _TIES if counts[d] == best)


def direction(model: Model, symbol: str, bar_close: datetime) -> Direction | None:
    """The table's direction for ``symbol`` at ``bar_close``: its slot's, else its symbol's
    whole day's; None if it has neither."""
    code = model.params.get(key(symbol, bar_close), model.params.get(f"{symbol}.{ALL_DAY}"))
    if code is None:
        return None
    return Direction.UP if code > 0 else Direction.DOWN if code < 0 else Direction.FLAT


def load_climate(state_dir: Path) -> Model:
    """The table the server runs (``state/models/baseline-climate.json``), else the defaults."""
    path = model_path(state_dir, CLIMATE)
    if not path.exists():
        return DEFAULT
    try:
        return Model.from_json(path.read_text())
    except (OSError, ValueError, KeyError, TypeError) as e:
        log.error("cannot read %s (%s); %s runs its defaults", path, e, CLIMATE)
        return DEFAULT


def load_tables(state_dir: Path, kinds: Sequence[Kind]) -> dict[str, Model]:
    """The tables the server runs for the ``kinds``' table baselines (#180), by name: each
    ``state/models/<name>.json``, else (missing or unreadable) its defaults."""
    tables: dict[str, Model] = {}
    for _, baseline in table_baselines(kinds):
        path = model_path(state_dir, baseline.name)
        tables[baseline.name] = baseline.default_table()
        if not path.exists():
            continue
        try:
            tables[baseline.name] = Model.from_json(path.read_text())
        except (OSError, ValueError, KeyError, TypeError) as e:
            log.error("cannot read %s (%s); %s runs its defaults", path, e, baseline.name)
    return tables
