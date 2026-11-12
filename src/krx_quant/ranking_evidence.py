"""The ranking's walk-forward evidence (#697, ``ranking.py``): what an algorithm with too few live days
is ranked on, read from what its pre-validation left on the server, never trained again at the ranking.

- **A bar-close kind's algorithm**: its pre-validation's replay (``state/prevalidation-<name>.sqlite3``,
  the engine's tables, its model retrained before each day on the days before it), its latest
  ``ranking.RECENT_DAYS`` days answered before the night, scored as the live rows are
  (``ranking.scores_of``), while its report (``state/prevalidations/<name>.json``) is of the code and
  answers it runs now.
- **A daily kind's algorithm**: its walk-forward over the KRX days (``daily_training.walk_forward``,
  about seven years out of sample), a day's score each (``ranking.DayScore``) against the kind's climate,
  kept in ``state/rankings/<name>.json`` with the code, answer version and inputs it was made with, and
  read only while they are the algorithm's now (``prevalidation._stale``). Its pre-validation writes it
  (``daily_training.prevalidate_daily``), and ``krx-quant ranking-evidence`` (a scheduled job,
  ``scheduled/research.py``) walks forward each daily algorithm whose file is missing or stale: one
  pre-validated before #697, or whose code, answers or inputs changed since. That command changes no
  status, report or history: it only writes the file.
"""

from __future__ import annotations

import json
import logging
import time as timer
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from krx_quant.algorithm_days import climate_of
from krx_quant.algorithms import DAILY_ALGORITHMS, KindAlgorithm
from krx_quant.config import Settings
from krx_quant.daily import DailyAlgorithm, DailyKind
from krx_quant.feeds.base import KST, kst_day
from krx_quant.kind_registry import DAILY_KINDS, KINDS
from krx_quant.kinds import Kind, KindScored
from krx_quant.predictions import read_only
from krx_quant.prevalidation import _read_report, _stale, code_digest
from krx_quant.ranking import (
    RECENT_DAYS,
    WALK_FORWARD,
    DayScore,
    Evidence,
    answered_days,
    score_day,
    scores_of,
)

log = logging.getLogger(__name__)

EVIDENCE_DIR = "rankings"


def evidence_path(state_dir: Path, name: str) -> Path:
    """A daily algorithm's walk-forward evidence (module doc)."""
    return state_dir / EVIDENCE_DIR / f"{name}.json"


def replay_path(state_dir: Path, name: str) -> Path:
    """A bar-close algorithm's pre-validation replay (``prevalidation.py``)."""
    return state_dir / f"prevalidation-{name}.sqlite3"


def daily_scores(records: Mapping[str, Sequence[KindScored]], name: str, kind: Kind) -> list[DayScore]:
    """``name``'s walk-forward days (oldest first) against the kind's climate among ``records`` (the
    walk-forward's, by predictor); none without the climate's."""
    climate = climate_of(kind, kind.baseline_names())
    if climate is None:
        return []
    by_day: dict[date, dict[str, list[KindScored]]] = {}
    for who in (name, climate):
        for r in records.get(who, ()):
            by_day.setdefault(kst_day(r.bar_close), {}).setdefault(who, []).append(r)
    out = []
    for day in sorted(by_day):
        found = score_day(day, by_day[day], climate, kind).get(name)
        if found is not None:
            out.append(found)
    return out


def keep_daily(
    state_dir: Path,
    algorithm: DailyAlgorithm,
    kind: DailyKind,
    records: Mapping[str, Sequence[KindScored]],
    inputs: Mapping[str, int],
    now: datetime,
) -> Path:
    """Write ``algorithm``'s walk-forward evidence (module doc) from its walk-forward's ``records`` (its
    own and the kind's baselines', by predictor), made on the data versions ``inputs``; its path."""
    days = daily_scores(records, algorithm.name, kind)
    body = {
        "algorithm": algorithm.name,
        "kind": kind.name,
        "code": code_digest(algorithm),
        "target_version": kind.answer_version,
        "inputs": dict(inputs),
        "made_at": now.astimezone(KST).isoformat(),
        "baseline": climate_of(kind, kind.baseline_names()),
        "days": [d.as_list() for d in days],
    }
    path = evidence_path(state_dir, algorithm.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(body, separators=(",", ":")))
    tmp.replace(path)
    return path


def _read(path: Path) -> dict[str, Any] | None:
    try:
        found = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        log.warning("ranking evidence %s not read (%s)", path, e)
        return None
    return found if isinstance(found, dict) else None


@dataclass(frozen=True)
class WalkForwardEvidence:
    """The walk-forward evidence on the server (module doc), as the engine's ranking asks it: an
    algorithm's days before the night, or None without current evidence."""

    state_dir: Path
    target_version: int
    kinds: Sequence[Kind] = KINDS

    def __call__(
        self, algorithm: KindAlgorithm | DailyAlgorithm, kind: Kind, before: date
    ) -> Evidence | None:
        try:
            if isinstance(kind, DailyKind):
                return self._daily(algorithm, kind, before)
            return self._replayed(algorithm, kind, before)
        except Exception:  # a file that cannot be read is no evidence; the ranking goes on
            log.warning("%s: its walk-forward evidence not read", algorithm.name, exc_info=True)
            return None

    def current(self, algorithm: KindAlgorithm | DailyAlgorithm, kept: Mapping[str, Any] | None) -> bool:
        return kept is not None and _stale(kept, algorithm, self.target_version, self.kinds) is None

    def _daily(self, algorithm: KindAlgorithm | DailyAlgorithm, kind: Kind, before: date) -> Evidence | None:
        kept = _read(evidence_path(self.state_dir, algorithm.name))
        if kept is None or not self.current(algorithm, kept):
            return None
        days = tuple(d for d in (DayScore.of_list(v) for v in kept.get("days", ())) if d.day < before)
        return Evidence(WALK_FORWARD, days) if days else None

    def _replayed(
        self, algorithm: KindAlgorithm | DailyAlgorithm, kind: Kind, before: date
    ) -> Evidence | None:
        if not self.current(algorithm, _read_report(self.state_dir, algorithm.name)):
            return None
        with read_only(replay_path(self.state_dir, algorithm.name)) as db:
            if db is None:
                return None
            days = answered_days(db, kind, before, RECENT_DAYS)
            found = scores_of(db, kind, days, {algorithm.name}).get(algorithm.name, [])
        return Evidence(WALK_FORWARD, tuple(found)) if found else None


def stale_daily(
    state_dir: Path,
    target_version: int,
    algorithms: Sequence[DailyAlgorithm] = DAILY_ALGORITHMS,
    kinds: Sequence[DailyKind] = DAILY_KINDS,
) -> list[DailyAlgorithm]:
    """The daily algorithms of a kind that runs whose evidence file is missing or not of the code,
    answers and inputs they have now: those ``ranking-evidence`` walks forward."""
    gate = WalkForwardEvidence(state_dir, target_version)
    running = {k.name for k in kinds if k.runs}
    return [
        a
        for a in algorithms
        if a.kind in running and not gate.current(a, _read(evidence_path(state_dir, a.name)))
    ]


def walk_stale(settings: Settings, now: datetime) -> tuple[list[str], int]:
    """``krx-quant ranking-evidence`` (module doc): each stale daily algorithm walked forward, one at a
    time, and its evidence written; the report's lines (counts and dates, no price) and the exit code (1
    when one failed)."""
    from krx_quant.daily_features import DailyFiles, stored_versions
    from krx_quant.daily_training import walk_forward
    from krx_quant.market_calendar import calendar_store

    stale = stale_daily(settings.state_dir, settings.target_version)
    lines = [
        f"ranking evidence (#697): {len(stale)} daily algorithm(s) without current walk-forward evidence"
    ]
    if not stale:
        return lines, 0
    files = DailyFiles(settings.data_dir, settings.state_dir)
    calendar = calendar_store(settings).calendar
    kinds = {k.name: k for k in DAILY_KINDS}
    failed = 0
    for algorithm in stale:
        kind = kinds[algorithm.kind]
        started = timer.monotonic()
        try:
            inputs = stored_versions(algorithm.features, settings.state_dir, settings.symbols)
            run = walk_forward(algorithm, kind, files, settings.symbols, settings.fee_rate, calendar=calendar)
            days = daily_scores(run.records, algorithm.name, kind)
            if days:  # none (no KRX days yet): nothing kept, so it is walked again once they are in
                keep_daily(settings.state_dir, algorithm, kind, run.records, inputs, now)
        except Exception as e:  # the others go on
            failed += 1
            log.exception("%s: walk-forward evidence not made", algorithm.name)
            lines.append(f"  {algorithm.name} ({kind.name}): failed ({type(e).__name__})")
            continue
        if not days:
            lines.append(f"  {algorithm.name} ({kind.name}): no day scored (no KRX days?); nothing kept")
            continue
        span = f"{days[0].day}..{days[-1].day}" if days else "no day"
        lines.append(
            f"  {algorithm.name} ({kind.name}): {len(days)} days scored against the climate, {span},"
            f" {sum(d.calls for d in days)} calls, {timer.monotonic() - started:.0f} s"
        )
    return lines, 1 if failed else 0
