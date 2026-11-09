"""Neyman's variants judged on backfilled days (#532, ``krx-quant neyman-variants --backfill``, the
scheduled job ``neyman-backfill-532``): #455's three comparisons, as registered, on past days no arm
was ever judged on, as a backtest.

#455's discovery (the night of 2026-11-03) set the live window's N at 206 recorded days (D − A's s_d
0.0133, a lift of 0.3 points with 80% power at 98.333%): a verdict in August–September 2027. On
2026-11-04 the user decided not to wait: each past day predicted by the arms fitted on the days
before it, judged by the rule #455 fixed. Codex agreed (2026-11-04). The three changes were chosen on
the discovery's days (2026-10-12–10-30), and the arms that predicted those were trained on the 20
trading days before them: neither can judge. The backfilled days before them were never a day an arm
was judged on: they can (``neyman_variants``'s arms, settings and seeds; neyman's code untouched,
its digest as its pre-validation passed it).

**The days** (``Listed``). The backfilled days (no recording) before the first of the discovery's
training days (``first_training_day``: the 20th day with data before 2026-10-12, as the walk-forward
took its window), each with ``WARMUP`` (40) days with data before it: the 20 trading days it trains
on, and the 20 whose bars the first of those reads (``slot_return_20d``, ``PREPARED``). A day is in
when it has at least the discovery's ``min_scored`` (200) bar closes every arm scored: its bar closes
with a label, the one-minute bars' touch label (``touch.bar_touch``), which reads no feature, so they
are counted from the day made with no feature, one at a time and let go (``labelled_count``), before
any arm is fitted. The list is fixed then and saved in the report: a later run (after a failure, say)
reads the same days.

**Training** (as registered): each day predicted by the four arms fitted on the 20 trading days
before it, with neyman's pre-validation's samples (``prevalidation._WalkForward``), settings and seeds
(``neyman_variants.walk_into``). The sample store is only read: a day a night stored is read from it
(the backfilled days near the discovery's), the older ones are made and let go when they leave the
window; nothing is written there, so the nightly training's days stay (#512).

**The verdict** (as #455 registered it, fixed before the run): B, C and D each against A; a day's
mean paired gain in hit rate is one value, every day the same weight; a two-sided 98.333% Student t
interval (Bonferroni over the three); **a candidate (backfill)** when the paired lift is above 0 and
the lower bound by trading day too. Judged on N_avail days, the days in, fewer than 206 or not: the
lower bound guards against a false candidate, N changes the power only. The report gives the power
(``power``) to find a lift of 0.3 and 0.5 points with N_avail days at the discovery's s_d (the rule's)
and at these days' (described). A comparison that did not pass with less than 80% power at 0.3 points
by the discovery's s_d has **no conclusion**; one with more is **not a candidate**.

**Described, never judged**: each arm's hit rate, up and down call share and their precision; the
lifts by month, by 30-minute slot (``consensus.slot``) and by σ̂₄'s tercile (each symbol's cuts from
these days' bar closes); and the caveats (``CAVEATS``): the answers are the bars' touch labels, not the
book's of the recorded days; no book, order flow, tape or IV input, and no index nor US leader has
backfilled bars; another market. A candidate (backfill) is one on past data: it does not say the live
hit rate rises. The live window's smaller check stays (``neyman_variants.Plan``: D − A alone, 0.5
points at 95%), and this run moves it into the experiment's report if it is not there yet, before any
backfilled day is judged: the backfill's results never change it.

**The report** (``state/experiments/neyman-backfill-532.json``): the list (``listed``), then the
verdict (``verdict``) with its lines. A run after the verdict prints it again and computes nothing.
Dates, counts, rates, lifts and powers only: no price, σ̂₄ or cut.

**Memory and cost.** A pre-validation's training phase's, as the variants' walk (#383): the window's
days are kept, the older let go, a day's memory logged and freed (``footprint.give_back``). The four
arms' calls are kept small (``Hit``: hundreds of days of ``Scored`` would not fit the server's 2 GB). A
day fits ten tree models on its 20 days (about 51 s on the server: the discovery's 15 days took 11
minutes) and makes the one backfilled day that enters its window (4–6 s on the server; the sample
store keeps only the nights' days, about 61 trading days); counting makes each listed day once more,
with no feature. The backfill reaches 365 calendar days back, so about 200 days: about three hours
and a half.

**By day across nights** (#643). The night after the nightly training is about three hours and a half
long, less on a long training: the run goes on by day. Told when to stop (``until``: the night's 05:30,
``--until``), it starts no day it could not end by then (by the slowest of tonight's days), and each
day it ended is kept (``CHECKPOINT``: its fold, and each bar close's symbol, time, answer, the four
arms' calls and σ̂₄), so the next night's run goes on from the first day left. The kept days are read
only with the same code (``code_key``: this module's, ``neyman_variants``', ``training``'s,
``features``' and ``touch``'s, neyman's package's digest and the walk-forward's class), the same list
and the same input (``input_key``: the settings that make the samples and each data file of the days
up to the last listed, by its size and time); with any other, they are set aside and the run starts
from the first day. A day kept gives the tally the walk gave it, so the verdict on days walked over
several nights is the verdict on them walked in one. The verdict comes once, after the last day, by the
same rule.
"""

from __future__ import annotations

import hashlib
import importlib
import inspect
import json
import logging
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import partial
from pathlib import Path
from statistics import NormalDist
from typing import Any

from krx_quant.algorithms import ALGORITHMS, KIND_ALGORITHMS, Algorithm, KindAlgorithm
from krx_quant.algorithms.contract import Model, Sample
from krx_quant.algorithms.forecasts import number
from krx_quant.algorithms.neyman import FEATURES, TRAINING_DAYS, Neyman
from krx_quant.config import Settings
from krx_quant.consensus import slot
from krx_quant.features import BARRIER_SIGMA, BARRIER_WIDTH, Leaders
from krx_quant.feeds.base import KST, kst_day
from krx_quant.footprint import give_back
from krx_quant.kind_registry import KINDS
from krx_quant.kinds import Kind
from krx_quant.market_calendar import MarketCalendar, calendar_store
from krx_quant.neyman_variants import (
    ARMS,
    DELTA,
    LABELS,
    LEVEL,
    LIVE_DELTA,
    NO_SIGMA,
    VARIANTS,
    VOLATILITY,
    Z_LEVEL,
    A,
    Fit,
    Fold,
    Plan,
    Spread,
    Verdict,
    _cell_lines,
    _cells,
    _number,
    _sigma_cell,
    _signed,
    _verdict,
    _write,
    forecasts,
    migrated,
    percent,
    plan_lines,
    read_report,
    registered_plan,
    spread,
    walk_into,
)
from krx_quant.neyman_variants import (
    REPORT as VARIANTS_REPORT,
)
from krx_quant.predictions import iso
from krx_quant.prevalidation import _WalkForward, code_digest
from krx_quant.pricing import Direction, most_likely
from krx_quant.sample_store import night_reads
from krx_quant.stats import Interval, wilson
from krx_quant.training import finished_days, is_recorded, previous_days_needed, training_day

log = logging.getLogger(__name__)

ISSUE = 455  # where the result goes: #455's experiment (#532 is the work)
WORK = 532
EXPERIMENT = "neyman-backfill-532"  # the scheduled job's name
REPORT = f"experiments/{EXPERIMENT}.json"  # under the state directory
# The days walked so far (#643), under the state directory: a line each, read by the next night's run.
# Never posted nor backed up (it holds σ̂₄ at each bar close); gone once the verdict is in.
CHECKPOINT = f"experiments/{EXPERIMENT}.days.jsonl"

# Fixed in #532 before the run.
PREPARED = previous_days_needed(FEATURES)  # the trading days of bars a day's features read before it: 20
WARMUP = TRAINING_DAYS + PREPARED  # the days with data a judged day needs before it: 40
POWER = 0.8  # the power #455's N was set for: below it at DELTA, a comparison not passed concludes nothing
DELTAS = (DELTA, LIVE_DELTA)  # the lifts whose power the report gives: 0.3 and 0.5 points
CANDIDATE = "a candidate (backfill)"
NOT_A_CANDIDATE = "not a candidate"
NO_CONCLUSION = "no conclusion"
CAVEATS = (
    "the backfilled days' answers are the one-minute bars' touch labels (touch.bar_touch), not the book's"
    " of the recorded days",
    "they have no book, order flow, tape or IV input, and no index nor US leader has backfilled bars",
    "another market: the days come before any recorded day",
)

UP, FLAT, DOWN = Direction.UP, Direction.FLAT, Direction.DOWN


# The days


def first_training_day(available: Sequence[date], start: date) -> date | None:
    """The first of the days the discovery trained on: of the days with data before ``start`` (its
    first day), the ``TRAINING_DAYS``-th from the end, as the walk-forward took its window; the oldest
    with fewer; None without one."""
    before = [d for d in available if d < start]
    return before[-TRAINING_DAYS:][0] if before else None


def candidates(
    available: Sequence[date], recorded: set[date] | frozenset[date], first: date, warmup: int = WARMUP
) -> list[date]:
    """The backfilled days (not ``recorded``) before ``first`` with ``warmup`` days with data before
    each, oldest first."""
    return [d for d in available[warmup:] if d < first and d not in recorded]


def labelled_count(settings: Settings, calendar: MarketCalendar, leaders: Leaders, day: date) -> int:
    """``day``'s bar closes with a label, as many as every arm scores of it: the day made from its data
    with no feature (its labels read none) and let go."""
    s = settings
    found = training_day(
        s.data_dir, day, calendar, s.symbols, (), s.fee_rate, leaders, target_version=s.target_version
    )
    count = 0 if found is None else sum(1 for x in found.samples if x.outcome is not None)
    del found
    give_back()
    return count


@dataclass(frozen=True)
class Listed:
    """The days fixed before any arm was fitted: the backfilled days before ``first`` (the discovery's
    first training day; ``start`` its first day) with ``warmup`` days with data before each, ``days``
    those with ``min_scored`` scored bar closes, ``short`` the others. ``oldest`` the oldest day with
    data on the server."""

    listed_at: datetime
    oldest: date | None
    first: date | None
    start: date
    warmup: int
    min_scored: int
    days: list[date]
    short: list[date]

    def as_dict(self) -> dict[str, Any]:
        return {
            "listed_at": self.listed_at.isoformat(),
            "oldest": None if self.oldest is None else self.oldest.isoformat(),
            "first_training_day": None if self.first is None else self.first.isoformat(),
            "discovery_start": self.start.isoformat(),
            "warmup": self.warmup,
            "min_scored": self.min_scored,
            "days": [d.isoformat() for d in self.days],
            "short": [d.isoformat() for d in self.short],
        }

    @classmethod
    def read(cls, data: Mapping[str, Any]) -> Listed:
        def day(text: str | None) -> date | None:
            return None if text is None else date.fromisoformat(text)

        return cls(
            datetime.fromisoformat(data["listed_at"]),
            day(data.get("oldest")),
            day(data.get("first_training_day")),
            date.fromisoformat(data["discovery_start"]),
            int(data["warmup"]),
            int(data["min_scored"]),
            [date.fromisoformat(d) for d in data["days"]],
            [date.fromisoformat(d) for d in data["short"]],
        )


def fix_list(
    available: Sequence[date],
    recorded: set[date] | frozenset[date],
    start: date,
    count: Callable[[date], int],
    now: datetime,
    *,
    warmup: int = WARMUP,
    min_scored: int,
) -> Listed:
    """The days (``Listed``): the ``candidates`` before the discovery's first training day, each
    counted by ``count`` (its data alone) and in with ``min_scored`` scored bar closes."""
    first = first_training_day(available, start)
    days: list[date] = []
    short: list[date] = []
    for day in [] if first is None else candidates(available, recorded, first, warmup):
        (days if count(day) >= min_scored else short).append(day)
    return Listed(now, available[0] if available else None, first, start, warmup, min_scored, days, short)


# The arms' calls, kept small


@dataclass(frozen=True, slots=True)
class Hit:
    """An arm's call at a bar close and what happened, as a lift pairs it: what the backfill keeps of
    a forecast (its hundreds of days of ``Scored`` would not fit the server's memory)."""

    symbol: str
    bar_close: datetime
    predicted: Direction
    outcome: Direction

    @property
    def correct(self) -> bool:
        return self.predicted is self.outcome


class Tally:
    """The arms' calls of the days walked (``Hit``), the days, and σ̂₄ at each bar close (its tercile
    only)."""

    def __init__(self) -> None:
        self.hits: dict[str, list[Hit]] = {arm: [] for arm in ARMS}
        self.folds: list[Fold] = []
        self.sigmas: dict[tuple[str, str], float] = {}

    def add(self, day: date, fit: Fit | None, climate: Model, samples: Sequence[Sample]) -> None:
        """``day``'s bar closes with a label (``samples``), each arm's call of them (``walk_into``'s:
        the climate is the variants' description, not the backfill's)."""
        made = forecasts(fit, samples)
        for arm, made_by in made.by_arm.items():
            hits = self.hits[arm]
            for s, f in zip(samples, made_by, strict=True):
                assert s.outcome is not None
                hits.append(Hit(s.symbol, s.bar_close, most_likely(f.p_up, f.p_flat, f.p_down), s.outcome))
        for s in samples:
            width, ratio = number(s.features[BARRIER_WIDTH]), number(s.features[BARRIER_SIGMA])
            if math.isfinite(width) and math.isfinite(ratio) and ratio > 0:
                self.sigmas[(s.symbol, iso(s.bar_close))] = width / ratio
        composition = "-" if fit is None else fit.composition
        self.folds.append(Fold(day, 0 if fit is None else fit.trained_on, composition, len(samples)))


# The days kept across nights (#643)

# The modules whose code makes a day's calls, beside neyman's package and the walk-forward's class.
CODE = (
    "krx_quant.neyman_backfill",
    "krx_quant.neyman_variants",
    "krx_quant.training",
    "krx_quant.features",
    "krx_quant.touch",
)
_CALL = {UP: "u", FLAT: "f", DOWN: "d"}
_DIRECTION = {v: k for k, v in _CALL.items()}


def code_key() -> str:
    """A digest of the code that makes a day's calls (``CODE``, neyman's package by
    ``prevalidation.code_digest``, and ``_WalkForward``'s class): kept days are read only with it."""
    digest = hashlib.sha256()
    for name in CODE:
        module = importlib.import_module(name)
        assert module.__file__ is not None
        digest.update(name.encode() + b"\0" + Path(module.__file__).read_bytes())
    digest.update(b"\0neyman\0" + code_digest(Neyman()).encode())
    digest.update(b"\0walk\0" + inspect.getsource(_WalkForward).encode())
    return digest.hexdigest()[:16]


def input_key(settings: Settings, listed: Listed) -> str:
    """A digest of what a day's samples are made from: the settings that make them, the list, and
    each file of the data directory's day folders up to the last listed day, by its path, size and
    time (``bars/``, ``events/``)."""
    s = settings
    digest = hashlib.sha256()
    made = [s.symbols, s.etf_symbols, s.index_codes, s.us_symbols, s.fee_rate, s.target_version]
    digest.update(json.dumps([list(x) if isinstance(x, tuple) else x for x in made]).encode())
    digest.update(json.dumps(listed.as_dict(), sort_keys=True).encode())
    last = max([*listed.days, *listed.short], default=None)
    for top in ("bars", "events"):
        for folder in sorted((s.data_dir / top).glob("date=*")):
            try:
                day = date.fromisoformat(folder.name.removeprefix("date="))
            except ValueError:
                continue
            if last is None or day > last:
                continue
            for f in sorted(p for p in folder.rglob("*") if p.is_file()):
                found = f.stat()
                where = f.relative_to(s.data_dir).as_posix()
                digest.update(f"{where}\0{found.st_size}\0{found.st_mtime_ns}\n".encode())
    return digest.hexdigest()[:16]


def _day_line(day: date, fold: Fold, tally: Tally, scored: int) -> dict[str, Any]:
    """``day``'s line: its fold, and of its ``scored`` bar closes (the tally's last), each one's
    symbol, time from the day's midnight, answer, the four arms' calls and σ̂₄ (None without)."""
    hits = {arm: tally.hits[arm][len(tally.hits[arm]) - scored :] for arm in ARMS}
    midnight = datetime.combine(day, time(), KST)
    symbols = sorted({h.symbol for h in hits[A]})
    return {
        "day": day.isoformat(),
        "fold": fold.as_dict(),
        "symbols": symbols,
        "symbol": [symbols.index(h.symbol) for h in hits[A]],
        "at": [(h.bar_close - midnight).total_seconds() for h in hits[A]],
        "outcome": "".join(_CALL[h.outcome] for h in hits[A]),
        "calls": {arm: "".join(_CALL[h.predicted] for h in hits[arm]) for arm in ARMS},
        "sigma": [tally.sigmas.get((h.symbol, iso(h.bar_close))) for h in hits[A]],
    }


def _restore(tally: Tally, line: Mapping[str, Any]) -> date:
    """A kept day's line back into ``tally``, as the walk added it; nothing added from a line that
    cannot be read (ValueError, KeyError, TypeError, IndexError)."""
    day = date.fromisoformat(line["day"])
    midnight = datetime.combine(day, time(), KST)
    symbols = [str(x) for x in line["symbols"]]
    at = [midnight + timedelta(seconds=float(x)) for x in line["at"]]
    who = [symbols[int(i)] for i in line["symbol"]]
    outcome = [_DIRECTION[c] for c in line["outcome"]]
    calls = {arm: [_DIRECTION[c] for c in line["calls"][arm]] for arm in ARMS}
    sigma = list(line["sigma"])
    f = line["fold"]
    fold = Fold(date.fromisoformat(f["day"]), int(f["trained_on"]), str(f["window"]), int(f["scored"]))
    sizes = {len(who), len(outcome), len(sigma), *(len(c) for c in calls.values())}
    if sizes - {len(at)}:
        raise ValueError(f"{day}: its bar closes do not line up")
    for arm in ARMS:
        hits = tally.hits[arm]
        hits.extend(Hit(s, b, c, o) for s, b, c, o in zip(who, at, calls[arm], outcome, strict=True))
    for s, b, v in zip(who, at, sigma, strict=True):
        if v is not None:
            tally.sigmas[(s, iso(b))] = float(v)
    tally.folds.append(fold)
    return day


class Checkpoint:
    """The days walked so far (``CHECKPOINT``): a first line with the key they were walked with
    (``code_key``, ``input_key``), then a line a day (``_day_line``)."""

    def __init__(self, path: Path, key: Mapping[str, str]) -> None:
        self.path = path
        self.key = dict(key)

    def restore(self, tally: Tally, listed: Listed) -> tuple[int, str | None]:
        """The days kept, back into ``tally`` (the listed days' first ones, in order): how many, and
        why none were (None if there were none to read). Those of another key are set aside; a line
        cut short (a run killed as it wrote) ends the kept days, and is dropped."""
        try:
            text = self.path.read_text()
        except FileNotFoundError:
            return 0, None
        lines = text.splitlines(keepends=True)
        try:
            head = json.loads(lines[0]) if lines else None
        except ValueError:
            head = None
        if not isinstance(head, dict) or head.get("key") != self.key:
            was = head.get("key") if isinstance(head, dict) else None
            changed = [k for k in self.key if not isinstance(was, dict) or was.get(k) != self.key[k]]
            self.path.unlink()
            return 0, f"set aside: another {' and '.join(changed) or 'file'}"
        kept = 0
        for line in lines[1:]:
            if kept >= len(listed.days) or not line.endswith("\n"):
                break
            try:
                found = json.loads(line)
                if date.fromisoformat(found["day"]) != listed.days[kept]:
                    break
                _restore(tally, found)
            except (ValueError, KeyError, TypeError, IndexError):
                break
            kept += 1
        if kept < len(lines) - 1:  # what follows the days read is dropped: they go on from there
            self._rewrite(lines[: kept + 1])
            dropped = len(lines) - 1 - kept
            log.warning("neyman backfill: %d lines after the %d days kept dropped", dropped, kept)
        return kept, None

    def _rewrite(self, lines: Sequence[str]) -> None:
        tmp = self.path.with_name(f".{self.path.name}.tmp")
        tmp.write_text("".join(lines))
        tmp.replace(self.path)

    def keep(self, line: Mapping[str, Any]) -> None:
        """A day walked, after those kept (the key first, on the first)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            if f.tell() == 0:
                f.write(json.dumps({"key": self.key}) + "\n")
            f.write(json.dumps(line) + "\n")
            f.flush()

    def drop(self) -> None:
        self.path.unlink(missing_ok=True)


class Until:
    """Whether the walk stops before its next day (``walk_into``'s ``stop``): when that day, as long
    as the slowest of tonight's, would end after ``until``; the first day always starts unless
    ``until`` has passed."""

    def __init__(self, until: datetime | None, clock: Callable[[], datetime]) -> None:
        self.until = until
        self.clock = clock
        self.last: datetime | None = None
        self.slowest = timedelta(0)

    def __call__(self) -> bool:
        now = self.clock()
        if self.last is not None:
            self.slowest = max(self.slowest, now - self.last)
        self.last = now
        return self.until is not None and now + self.slowest >= self.until


@dataclass(frozen=True)
class Progress:
    """Where the backfill stands, as its scheduled job's condition reads it (``Facts.neyman_backfill``)."""

    read: bool = False  # read tonight: without, the job waits
    judged: bool = False  # its verdict is in the report


def progress(settings: Settings) -> Progress:
    """The backfill's ``Progress`` from its report (a small file). One that cannot be read says not
    judged (logged): the job then runs, and its command says why it cannot."""
    try:
        report = read_report(settings.state_dir / REPORT, "the fixed list of days, never listed again")
    except (OSError, ValueError):
        log.exception("could not read where the neyman backfill (#532) stands")
        return Progress(read=True)
    return Progress(True, bool(report and report.get("verdict")))


# The verdict


def power(days: int, sd: float | None, delta: float, z_level: float = Z_LEVEL) -> float | None:
    """The chance of finding a lift of ``delta`` on ``days`` days whose mean paired gains spread ``sd``:
    Φ(δ√n ÷ s_d − z), two-sided at ``z_level`` (98.333%) by the normal approximation #455's N was set
    with (``neyman_variants.required_days`` is its inverse at 80%); the far tail left out. None without
    a day or a spread."""
    if days < 1 or sd is None or sd <= 0:
        return None
    return NormalDist().cdf(delta * math.sqrt(days) / sd - z_level)


def conclusion(verdict: Verdict, at_delta: float | None) -> str:
    """A comparison's conclusion: a candidate (backfill) when it passed; else no conclusion when its
    power to find 0.3 points (``at_delta``) is below ``POWER`` (or unknown), not a candidate above."""
    if verdict.candidate:
        return CANDIDATE
    return NO_CONCLUSION if at_delta is None or at_delta < POWER else NOT_A_CANDIDATE


def _key(delta: float) -> str:
    return f"{delta:g}"


def _month(h: Hit) -> str:
    return kst_day(h.bar_close).isoformat()[:7]


def _iv(i: Interval | None) -> dict[str, Any] | None:
    return None if i is None else {"value": i.value, "low": i.low, "high": i.high, "n": i.n}


def hit_stats(hits: Sequence[Hit]) -> dict[str, Any]:
    """An arm's hit rate, its up and down calls' share and their precision, with 95% Wilson intervals."""
    directional = [h for h in hits if h.predicted is not FLAT]
    return {
        "n": len(hits),
        "accuracy": _iv(wilson(sum(h.correct for h in hits), len(hits))),
        "coverage": _iv(wilson(len(directional), len(hits))),
        "directional_precision": _iv(wilson(sum(h.correct for h in directional), len(directional))),
    }


@dataclass(frozen=True)
class Judged:
    """The verdict on the listed days: each comparison's (``Verdict`` at ``LEVEL``), its power at the
    discovery's s_d (``sds``) and at these days' (``spreads``), its conclusion, and what is
    described."""

    made_at: datetime
    folds: list[Fold]
    arms: dict[str, dict[str, Any]]
    verdicts: dict[str, Verdict]
    sds: dict[str, float | None]
    powers: dict[str, dict[str, float | None]]
    spreads: dict[str, Spread]
    own_powers: dict[str, dict[str, float | None]]
    conclusions: dict[str, str]
    described: dict[str, Any]

    @property
    def days(self) -> int:
        """N_avail: the days judged."""
        return len(self.folds)

    @property
    def candidates(self) -> list[str]:
        return [arm for arm in VARIANTS if self.verdicts[arm].candidate]

    def as_dict(self) -> dict[str, Any]:
        return {
            "made_at": self.made_at.isoformat(),
            "n_avail": self.days,
            "level": LEVEL,
            "verdict": {
                arm: self.verdicts[arm].as_dict()
                | {"candidate": self.verdicts[arm].candidate, "conclusion": self.conclusions[arm]}
                for arm in VARIANTS
            },
            "candidates": self.candidates,
            "power": {
                arm: {"discovery_sd": self.sds[arm], "at_discovery_sd": self.powers[arm]}
                | {"these_days": self.spreads[arm].as_dict(), "at_these_days_sd": self.own_powers[arm]}
                for arm in VARIANTS
            },
            "arms": self.arms,
            "described": self.described,
            "folds": [f.as_dict() for f in self.folds],
        }


def judge(tally: Tally, spreads: Mapping[str, Mapping[str, Any]], now: datetime) -> Judged:
    """The verdict on ``tally``'s days, as registered (module doc); ``spreads`` the discovery's."""
    hits = tally.hits
    days = len(tally.folds)
    verdicts = {arm: Verdict.of(hits[arm], hits[A], LEVEL) for arm in VARIANTS}
    sds: dict[str, float | None] = {}
    for arm in VARIANTS:
        sd = (spreads.get(arm) or {}).get("sd")
        sds[arm] = None if sd is None else float(sd)
    powers = {arm: {_key(d): power(days, sds[arm], d) for d in DELTAS} for arm in VARIANTS}
    own = {arm: spread(hits[arm], hits[A]) for arm in VARIANTS}
    own_powers = {arm: {_key(d): power(days, own[arm].sd, d) for d in DELTAS} for arm in VARIANTS}
    conclusions = {arm: conclusion(verdicts[arm], powers[arm][_key(DELTA)]) for arm in VARIANTS}
    sigma = _sigma_cell(tally.sigmas)
    months = sorted({_month(h) for h in hits[A]})
    slots = sorted({slot(h.bar_close) for h in hits[A]})
    described = {
        "by_month": _cells(hits, _month, months),
        "by_slot": _cells(hits, lambda h: slot(h.bar_close), slots),
        "by_volatility": {
            "with_sigma": sum(1 for h in hits[A] if sigma(h) != NO_SIGMA),
            "cells": _cells(hits, sigma, VOLATILITY),
        },
        "caveats": list(CAVEATS),
    }
    arms = {arm: hit_stats(hits[arm]) for arm in ARMS}
    return Judged(
        now, list(tally.folds), arms, verdicts, sds, powers, own, own_powers, conclusions, described
    )


# The run

JUDGED, JUDGED_BEFORE, GOES_ON = "judged", "judged before", "goes on"
# What a run that stopped before the last day says (#643): the job's issue is not called then.
GOES_ON_LINE = "it goes on the next night from"


@dataclass(frozen=True)
class Backfill:
    stage: str  # JUDGED, JUDGED_BEFORE or GOES_ON
    lines: list[str]
    listed: Listed | None = None
    judged: Judged | None = None


def neyman_backfill(
    settings: Settings,
    *,
    algorithms: Sequence[Algorithm] = ALGORITHMS,
    kind_algorithms: Sequence[KindAlgorithm] = KIND_ALGORITHMS,
    kinds: Sequence[Kind] = KINDS,
    now: datetime | None = None,
    warmup: int = WARMUP,
    live_delta: float = LIVE_DELTA,
    count: Callable[[date], int] | None = None,
    until: datetime | None = None,
    clock: Callable[[], datetime] | None = None,
) -> Backfill:
    """The backfill's verdict (module doc), its report saved: the live window's plan moved into the
    experiment's report first if it is not there, then the days listed (``count`` counts a day;
    ``labelled_count`` by default) and saved before any arm is fitted, unless a run saved them
    already, then walked and judged once. With ``until`` (#643), no day starts that would end after
    it, by the slowest of the run's days on ``clock``: a run that stops before the last day keeps
    the days it walked and says where the next goes on (``GOES_ON``), and the next run goes on
    from there. A run after the verdict prints it and computes nothing. Raises FileNotFoundError
    without a discovery, ValueError for a report that cannot be read or a listed day counted again
    with fewer bar closes."""
    now = now or datetime.now(KST)
    main = settings.state_dir / VARIANTS_REPORT
    saved = read_report(main)
    if not saved or not saved.get("discovery"):
        raise FileNotFoundError(
            f"no discovery in {main}: the backfill judges the days before its training (run neyman-variants)"
        )
    saved, moved = migrated(saved, live_delta)
    if moved:
        _write(main, saved)  # before any backfilled day is judged: their results never change it
    discovery = saved["discovery"]
    head = [*plan_lines(Plan.read(saved["plan"]), registered_plan(discovery), moved=moved), ""]
    path = settings.state_dir / REPORT
    report = read_report(path, "the fixed list of days, never listed again")
    if report and report.get("verdict"):
        made = str(report["verdict"].get("made_at", "?"))[:10]
        line = f"neyman variants on backfilled days (#{WORK}): judged on the night of {made}; it stands"
        return Backfill(JUDGED_BEFORE, [*head, line, "", *report.get("lines", []), f"report: {path}"])
    available = finished_days(settings.data_dir, settings.symbols, now)
    calendar = calendar_store(settings).calendar
    leaders = Leaders.of(settings)
    if report and report.get("listed"):
        listed = Listed.read(report["listed"])
    else:
        recorded = {d for d in available if is_recorded(settings.data_dir, d, settings.symbols)}
        start = date.fromisoformat(discovery["period"][0])
        counter = count or partial(labelled_count, settings, calendar, leaders)
        listed = fix_list(
            available, recorded, start, counter, now, warmup=warmup, min_scored=int(discovery["min_scored"])
        )
        _write(path, {"experiment": EXPERIMENT, "issue": ISSUE, "work": WORK, "listed": listed.as_dict()})
    # In the log as well: a run its time limit cuts still says which days it read.
    log.info(
        "neyman backfill: days with data from %s, the discovery's training from %s; %s",
        listed.oldest,
        listed.first,
        _list_head(listed),
    )
    tally = Tally()
    checkpoint = Checkpoint(
        settings.state_dir / CHECKPOINT, {"code": code_key(), "input": input_key(settings, listed)}
    )
    kept, set_aside = checkpoint.restore(tally, listed)

    def add(day: date, fit: Fit | None, climate: Model, samples: Sequence[Sample]) -> None:
        tally.add(day, fit, climate, samples)
        checkpoint.keep(_day_line(day, tally.folds[-1], tally, len(samples)))

    night = night_reads(algorithms, kind_algorithms, kinds)
    trainer = _WalkForward(settings, Neyman(), available, calendar, leaders, night=night)
    stop = Until(until, clock or (lambda: datetime.now(KST)))
    left = walk_into(add, trainer, listed.days[kept:], listed.min_scored, name="neyman backfill", stop=stop)
    if left:
        raise ValueError(f"neyman backfill: {', '.join(map(str, left))} counted again with fewer bar closes")
    walked = progress_lines(listed, kept, len(tally.folds), set_aside)
    if len(tally.folds) < len(listed.days):
        done = len(tally.folds)
        went = {"walked": done, "left": len(listed.days) - done, "next": listed.days[done].isoformat()}
        _write(
            path,
            {"experiment": EXPERIMENT, "issue": ISSUE, "work": WORK, "listed": listed.as_dict()}
            | {"progress": went | {"at": now.isoformat()}},
        )
        return Backfill(GOES_ON, [*head, *walked, f"report: {path}"], listed)
    judged = judge(tally, discovery["spreads"], now)
    lines = backfill_lines(listed, judged, int(discovery["n"]))
    _write(
        path,
        {"experiment": EXPERIMENT, "issue": ISSUE, "work": WORK, "lines": lines, "verdict": judged.as_dict()}
        | {"listed": listed.as_dict()},
    )
    checkpoint.drop()
    return Backfill(JUDGED, [*head, *walked, "", *lines, f"report: {path}"], listed, judged)


# The command's lines: dates, counts, rates, lifts and powers; no price, σ̂₄ or cut.


def _span(days: Sequence[date]) -> str:
    return f"{days[0]}..{days[-1]}" if days else "-"


def _list_head(listed: Listed) -> str:
    every = sorted([*listed.days, *listed.short])
    return (
        f"{len(every)} backfilled days can judge ({_span(every)}), {len(listed.days)} with at least"
        f" {listed.min_scored} bar closes every arm scored (N_avail); listed on {listed.listed_at.date()}"
    )


def progress_lines(listed: Listed, kept: int, done: int, set_aside: str | None) -> list[str]:
    """How far the walk is (#643): the listed days walked and left, and this run's share of them."""
    total = len(listed.days)
    tonight = listed.days[kept:done]
    if kept:
        before = f"after the {kept} days kept from earlier runs ({_span(listed.days[:kept])})"
    else:
        before = "from the first day" + (f" (the days kept were {set_aside})" if set_aside else "")
    out = [
        f"neyman backfill (#{WORK}, by day across nights, #643): {done} of {total} listed days walked"
        f" ({_span(listed.days[:done])}), {total - done} left ({_span(listed.days[done:])})",
        f"  this run: {len(tonight)} days ({_span(tonight)}), {before}",
    ]
    if done < total:
        out.append(
            f"  {GOES_ON_LINE} {listed.days[done]}; the verdict comes once, after the last day, by the rule"
            " fixed before the run"
        )
    return out


def _interval_dict(i: Mapping[str, Any] | None) -> str:
    if i is None:
        return "-"
    if i["low"] is None or i["high"] is None:
        return f"{i['value']:.4f}"
    return f"{i['value']:.4f} ({i['low']:.4f}..{i['high']:.4f})"


def _value_dict(i: Mapping[str, Any] | None) -> str:
    return "-" if i is None else f"{i['value']:.4f}"


def _power_pair(powers: Mapping[str, float | None]) -> str:
    return " / ".join("-" if powers[_key(d)] is None else f"{powers[_key(d)]:.2f}" for d in DELTAS)


def backfill_lines(listed: Listed, j: Judged, registered: int) -> list[str]:
    """The verdict's lines; ``registered`` the live window's N #455 registered (206)."""
    left = ", ".join(map(str, listed.short)) or "none"
    total = sum(f.scored for f in j.folds)
    out = [
        f"neyman variants on backfilled days (#{WORK}, job {EXPERIMENT}): #{ISSUE}'s three comparisons as"
        " registered, on the backfilled days before any the discovery read",
        f"  days with data from {listed.oldest or '-'} (the oldest on the server); the discovery trained from"
        f" {listed.first or '-'} on (the {TRAINING_DAYS} days with data before {listed.start}), and no day"
        " before it was ever judged",
        f"  a day needs {listed.warmup} days with data before it: the {TRAINING_DAYS} trading days it trains"
        f" on and the {PREPARED} whose bars the first of them reads (slot_return_20d)",
        f"  {_list_head(listed)}; left out: {left}. Fixed before any arm was fitted: a later run reads the"
        " same days",
        f"  each day predicted by the four arms fitted on the {TRAINING_DAYS} trading days before it"
        f" (neyman's pre-validation's samples, settings and seeds); {total} bar closes, every arm on each",
        "",
        f"{'arm':<44} {'n':<8} {'hit rate (95%)':<26} {'coverage':<9} dir. prec.",
    ]
    for arm in ARMS:
        a = j.arms[arm]
        out.append(
            f"{LABELS[arm]:<44} {a['n']:<8} {_interval_dict(a['accuracy']):<26}"
            f" {_value_dict(a['coverage']):<9} {_value_dict(a['directional_precision'])}"
        )
    out += [
        "",
        f"verdict (as #{ISSUE} registered it, fixed before the run): B, C and D each against A; a day's mean"
        " paired gain in hit rate is one value, every day the same weight; a two-sided"
        f" {percent(LEVEL)} Student t interval (Bonferroni over the three); {CANDIDATE} when the paired lift"
        " is above 0 and the lower bound by trading day too",
    ]
    for arm in VARIANTS:
        v = j.verdicts[arm]
        out.append(f"  {LABELS[arm]} - A: {_verdict(v)} -> {'passed' if v.candidate else 'did not pass'}")
    out += [
        f"power with N_avail = {j.days} days (#{ISSUE}'s N was {registered}: fewer change the power only),"
        f" two-sided {percent(LEVEL)} (normal): to find {DELTA * 100:g} / {LIVE_DELTA * 100:g} points at the"
        " discovery's s_d (the rule's), and at these days' (described)",
    ]
    for arm in VARIANTS:
        own = j.spreads[arm]
        out.append(
            f"  {arm} - A: {_power_pair(j.powers[arm])} at s_d {_number(j.sds[arm])}; these days' s_d"
            f" {_number(own.sd)} (mean {_signed(own.mean)}): {_power_pair(j.own_powers[arm])}"
        )
    out.append(
        f"conclusions ({CANDIDATE} when it passed; {NO_CONCLUSION} when it did not with less than"
        f" {POWER:.0%} power to find {DELTA * 100:g} points at the discovery's s_d; else {NOT_A_CANDIDATE}):"
    )
    out += [f"  {LABELS[arm]}: {j.conclusions[arm]}" for arm in VARIANTS]
    out.append(
        f"{CANDIDATE} is a candidate on past data: it does not say the live hit rate rises. None goes into"
        " neyman from this (#454, then the user decides); the live window's check stays (the plan above)"
    )
    out += ["", "caveats (described; the verdict reads none of them):"]
    out += [f"  - {c}" for c in CAVEATS]
    found = j.described
    out += ["", f"described, not judged (every interval at {percent(LEVEL)}):"]
    out.append("by month: hit rates; each variant - A, day low")
    out += _cell_lines(found["by_month"])
    out.append("by 30-minute slot (09:00 holds the bar closes 09:01-09:30)")
    out += _cell_lines(found["by_slot"])
    by_sigma = found["by_volatility"]
    out.append(
        "by σ̂₄'s tercile at the bar close (σ̂₄ = w / barrier_sigma, each symbol's cuts from these days' bar"
        f" closes; {by_sigma['with_sigma']} of {total} with σ̂₄, the rest in none)"
    )
    out += _cell_lines(by_sigma["cells"])
    return out
