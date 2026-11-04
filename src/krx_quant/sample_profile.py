"""Where making a recorded day's samples spends its time (#512).

A night after the code that makes samples changed makes every day again (``sample_store``): newton's
step took 9,013 s for 23 days on the night of 2026-10-26 and 11,600 s on 10-29, turing's 240 s and
248 s for 40. Those are the steps' times, not their features': a night reads each day once, with every
algorithm's features and every kind's answers (``sample_store.night_reads``), so the first step that
reads a day (newton, the first registered) makes it for every step, and the later ones read it from
the day cache. ``krx-quant sample-profile`` makes one recorded day from scratch, as a night does, and
says where the time goes, in runs one after the other:

- the night's reads: what the night pays for the day, by stage;
- each algorithm asked for (newton and turing by default), with its own features alone (and its kind's
  answers, for a kind's algorithm): what the day would cost were it the only reader;
- the night's reads again under cProfile: the functions the time goes to, by their own time and, of
  krx_quant's, by their cumulative time.

The stages: reading the files (the recorded partitions' parquet batches merged by receipt time, the
macro regime, the previous trading days' bars), replaying the events into the Market View (its bars,
book, flows and reference prices, and the special situations), the features at each bar close (the
bars as they stood, the snapshot, the Feature Store), the answers (following each bar close's touch
window, and the kinds' quotes, through the quotes and trades after it; the barriers' widths; the
labels) and storing the day (encoded, read back to check it, gzipped and written, into a temporary
sample store). What is in none (the bar closes' loop, the special situations' lookups, the samples
built) is the rest. The day's key (the input files' SHA-256) is reckoned once, outside the runs: a
night takes the digests the Object Store keeps.

The code that makes samples is not changed for this: its digest keys the sample store
(``sample_store.code_digest``), and a change would make every day again on the next night. The stages
are timed from outside instead: for a run, the methods ``_TIMED`` lists are wrapped (and put back after
it), and the events reach ``training.recorded_day`` through a reader that times them, as
``training.training_day`` hands them over for a recorded day. A wrapped call charges its stage until it
returns, a wrapped call inside it the inner one's, so the stages add up to the run's time. A wrapped
call costs about 0.3 µs more on a Mac (the server is about three times slower), three or four of them
an event: seconds of a recorded day's ten to twenty minutes. The profiler costs several times the run.

It reads the data directory and the state's market calendar, and writes only into a temporary
directory, never the live sample store: it may run beside the engine. A run still going at the time
limit stops where it is (its figures so far are printed), and the runs after it do not start. Its
output holds seconds, counts, sizes, the day, feeds' and kinds' names and function names: no price.
"""

from __future__ import annotations

import cProfile
import functools
import pstats
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from krx_quant.algorithms import ALGORITHMS, KIND_ALGORITHMS
from krx_quant.backfill import PreviousDay
from krx_quant.config import Settings
from krx_quant.engine import Event
from krx_quant.features import FeatureStore, Leaders
from krx_quant.kind_registry import KINDS, kind_named
from krx_quant.kinds import Kind
from krx_quant.macro import day_regime
from krx_quant.market_calendar import MarketCalendar, calendar_store
from krx_quant.market_view import MarketView
from krx_quant.object_store import file_digest
from krx_quant.replay import recorded_events
from krx_quant.sample_store import DayKey, SampleStore, day_key, night_reads, read_files
from krx_quant.special import SpecialSituations
from krx_quant.training import TrainingDay, finished_days, is_recorded, needs_previous_day, recorded_day

# The algorithms profiled alone by default: newton, the first step and the slow one on the nights that
# make every day again, and turing, a cheap step there that reads nearly every feature.
PROFILED = ("newton", "turing")
# The command's time limit: the scheduled job's (``scheduled.research.SAMPLE_PROFILE``) is ten minutes
# more, so the command prints what it has before the scheduler stops it. A recorded day took 515–1,258
# s to make with the night's reads on the night of 2026-10-29 (Loki's ``read <day>`` lines); the
# profiled run takes a few times that.
LIMIT = timedelta(minutes=170)
TOP = 25  # functions listed by own time; krx_quant's by cumulative time, the first 15

READ = "reading the files"
REPLAY = "replaying the events"
FEATURES = "the features"
ANSWERS = "the answers"
STORE = "storing the day"
REST = "the rest"
STAGES = (READ, REPLAY, FEATURES, ANSWERS, STORE, REST)

# The methods timed for a run, and the stage each charges (the module doc). Their names are the
# Market View's, the Feature Store's, the special situations' and the previous days' own: one renamed
# fails the run (``_timing``) and ``tests/test_sample_profile.py``.
_TIMED: tuple[tuple[type[Any], str, str], ...] = (
    (MarketView, "on_event", REPLAY),
    (SpecialSituations, "event", REPLAY),
    (MarketView, "at_close", FEATURES),
    (MarketView, "snapshot", FEATURES),
    (FeatureStore, "compute", FEATURES),
    (MarketView, "_open_windows", ANSWERS),
    (MarketView, "_follow", ANSWERS),
    (MarketView, "touch", ANSWERS),
    (MarketView, "kind_answer", ANSWERS),
    (PreviousDay, "_of", READ),
    (PreviousDay, "_returns_of", READ),
)


class _OutOfTime(Exception):
    """The time limit passed while a run read its events."""


class _Stages:
    """The stage a run is in, and the seconds and calls charged to each: a wrapped call charges its
    stage until it returns, a wrapped call inside it the inner one's; the time in none is the rest's."""

    def __init__(self) -> None:
        self.seconds = dict.fromkeys(STAGES, 0.0)
        self.calls = dict.fromkeys(STAGES, 0)
        self._stage = REST
        self._since = time.perf_counter()

    def enter(self, stage: str) -> str:
        now = time.perf_counter()
        self.seconds[self._stage] += now - self._since
        previous, self._stage, self._since = self._stage, stage, now
        self.calls[stage] += 1
        return previous

    def leave(self, previous: str) -> None:
        now = time.perf_counter()
        self.seconds[self._stage] += now - self._since
        self._stage, self._since = previous, now

    @contextmanager
    def charged(self, stage: str) -> Iterator[None]:
        previous = self.enter(stage)
        try:
            yield
        finally:
            self.leave(previous)


def _charging(stages: _Stages, stage: str, method: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(method)
    def charged(*args: Any, **kwargs: Any) -> Any:
        previous = stages.enter(stage)
        try:
            return method(*args, **kwargs)
        finally:
            stages.leave(previous)

    return charged


@contextmanager
def _timing(stages: _Stages) -> Iterator[None]:
    """The methods ``_TIMED`` lists charging their stages to ``stages`` while the block runs; put back
    after it, whatever happens."""
    originals = [(owner, name, vars(owner)[name]) for owner, name, _ in _TIMED]
    try:
        for (owner, name, stage), (_, _, original) in zip(_TIMED, originals, strict=True):
            setattr(owner, name, _charging(stages, stage, original))
        yield
    finally:
        for owner, name, original in originals:
            setattr(owner, name, original)


def _timed_read(
    events: Iterable[Event], stages: _Stages, feeds: Counter[str], deadline: float
) -> Iterator[Event]:
    """``events``, each one's reading charged to ``READ`` and counted by feed; ``_OutOfTime`` once
    ``deadline`` (``time.perf_counter``'s) has passed."""
    read = iter(events)
    while True:
        if time.perf_counter() > deadline:
            raise _OutOfTime
        previous = stages.enter(READ)
        try:
            event = next(read)
        except StopIteration:
            return
        finally:
            stages.leave(previous)
        feeds[event[0]] += 1
        yield event


def _read_until(events: Iterable[Event], feeds: Counter[str], deadline: float) -> Iterator[Event]:
    """``events`` counted by feed, ``_OutOfTime`` once ``deadline`` has passed: the profiled run's
    reader, which times nothing (the profiler does)."""
    for event in events:
        if time.perf_counter() > deadline:
            raise _OutOfTime
        feeds[event[0]] += 1
        yield event


@dataclass(frozen=True)
class Reads:
    """What a run makes the day with: its title, the features and the kinds whose answers it reads."""

    title: str
    names: tuple[str, ...]
    kinds: tuple[Kind, ...]


@dataclass(frozen=True)
class Made:
    """A run: the seconds and calls by stage, the events read by feed, the day made (None if the time
    limit cut it) and its stored file's size (None: not stored)."""

    reads: Reads
    seconds: Mapping[str, float]
    calls: Mapping[str, int]
    feeds: Mapping[str, int]
    found: TrainingDay | None
    stored_bytes: int | None

    @property
    def cut(self) -> bool:
        return self.found is None

    @property
    def total(self) -> float:
        """The run's seconds: its stages' together, from its start to its end."""
        return sum(self.seconds.values())


def make_day(
    data_dir: Path,
    day: date,
    calendar: MarketCalendar,
    symbols: Sequence[str],
    reads: Reads,
    fee_rate: float,
    leaders: Leaders,
    *,
    target_version: int,
    key: DayKey,
    deadline: float,
    profiler: cProfile.Profile | None = None,
) -> Made:
    """The recorded ``day``'s samples made from scratch with ``reads``, as ``training.training_day``
    makes a recorded day, and stored under ``key`` into a temporary sample store; timed by stage, or
    with ``profiler`` profiled instead. Stopped where it is once ``deadline`` (``time.perf_counter``'s)
    has passed."""
    feeds: Counter[str] = Counter()
    found: TrainingDay | None = None
    stored_bytes: int | None = None
    with tempfile.TemporaryDirectory(prefix="krx-quant-sample-profile-") as tmp:
        store = SampleStore(Path(tmp))
        stages = _Stages()  # the run's time starts here
        if profiler is not None:
            profiler.enable()
        try:
            with _timing(stages) if profiler is None else nullcontext():
                session = calendar.session(day)
                with stages.charged(READ):
                    regime = day_regime(data_dir, day, session.open) if session is not None else None
                previous = (
                    PreviousDay(data_dir, calendar) if needs_previous_day(reads.names, reads.kinds) else None
                )
                events = recorded_events(data_dir, day, symbols, leaders)
                made, answers, left_out = recorded_day(
                    _read_until(events, feeds, deadline)
                    if profiler is not None
                    else _timed_read(events, stages, feeds, deadline),
                    calendar,
                    day,
                    symbols,
                    reads.names,
                    fee_rate,
                    leaders,
                    regime=regime,
                    target_version=target_version,
                    kinds=reads.kinds,
                    previous=previous,
                )
                day_made = TrainingDay("mid", made, answers, left_out)
                with stages.charged(STORE):
                    stored = store.put(key, day_made, reads.names)
            found = day_made
            stored_bytes = store.path(key).stat().st_size if stored else None
        except _OutOfTime:
            pass
        finally:
            if profiler is not None:
                profiler.disable()
            stages.leave(REST)  # and ends here: the rest up to now
    return Made(reads, stages.seconds, stages.calls, dict(feeds), found, stored_bytes)


def night_reads_of(kinds: Sequence[Kind] = KINDS) -> Reads:
    """What a night makes each day with: every algorithm's features, every kind's answers it reads."""
    night = night_reads(ALGORITHMS, KIND_ALGORITHMS, kinds)
    return Reads("the night's reads", night.names, night.kinds)


def algorithm_reads(name: str, kinds: Sequence[Kind] = KINDS) -> Reads | None:
    """What a day made for the algorithm ``name`` alone reads: its features, and its kind's answers for
    a kind's algorithm (as ``krx-quant fit`` makes its days); None for a name not registered."""
    for algorithm in ALGORITHMS:
        if algorithm.name == name:
            return Reads(f"{name} alone", tuple(algorithm.features), ())
    for kind_algorithm in KIND_ALGORITHMS:
        if kind_algorithm.name == name and (kind := kind_named(kind_algorithm.kind, kinds)) is not None:
            return Reads(f"{name} alone", tuple(kind_algorithm.features), (kind,))
    return None


def latest_recorded(data_dir: Path, symbols: Sequence[str], now: datetime) -> date | None:
    """The latest finished day every symbol's trades were recorded on; None without one."""
    recorded = [d for d in finished_days(data_dir, symbols, now) if is_recorded(data_dir, d, symbols)]
    return recorded[-1] if recorded else None


def sample_profile(
    settings: Settings,
    now: datetime,
    out: Callable[[str], None],
    *,
    day: date | None = None,
    algorithms: Sequence[str] = PROFILED,
    limit: timedelta = LIMIT,
    top: int = TOP,
) -> int:
    """``krx-quant sample-profile``: the runs (the module doc), each printed through ``out`` as it ends.
    Exit 0 when every run ran to its end, 1 when one was cut or none could run (no recorded day), 2 for
    an algorithm not registered."""
    deadline = time.perf_counter() + limit.total_seconds()
    alone = [(name, algorithm_reads(name)) for name in algorithms]
    unknown = [name for name, reads in alone if reads is None]
    if unknown:
        registered = ", ".join([*(a.name for a in ALGORITHMS), *(a.name for a in KIND_ALGORITHMS)])
        out(f"sample profile (#512): unknown algorithm {', '.join(unknown)}; registered: {registered}")
        return 2
    symbols = settings.symbols
    chosen = day or latest_recorded(settings.data_dir, symbols, now)
    if chosen is None or not is_recorded(settings.data_dir, chosen, symbols):
        which = "no finished day" if chosen is None else f"not {chosen}"
        out(f"sample profile (#512): {which} has every symbol's trades recorded; nothing to make")
        return 1
    calendar = calendar_store(settings).calendar
    leaders = Leaders.of(settings)
    night = night_reads_of()
    runs = [night, *(reads for _, reads in alone if reads is not None)]
    out(
        f"sample profile (#512): {chosen}, "
        + ("the day asked for" if day is not None else "the latest finished recorded day")
        + f", made from scratch as a night makes a recorded day; {len(symbols)} symbol"
        + ("s" if len(symbols) != 1 else "")
        + ", target version"
        f" {settings.target_version}, time limit {round(limit.total_seconds() / 60)} min"
    )
    out(
        "  a night reads each day once, with every algorithm's features and every kind's answers"
        f" ({len(night.names)} features; kinds {', '.join(k.name for k in night.kinds) or 'none'}): the"
        f" first step that reads a day makes it for every step, {ALGORITHMS[0].name} (the first registered)"
    )
    out(
        "  runs: "
        + "; ".join(
            [
                *(_described(r) for r in runs),
                f"{night.title} under cProfile (the top {top} functions by own time)",
            ]
        )
    )
    digests, files, size, hashed = _digests(settings.data_dir, chosen, calendar, symbols, night, leaders)
    out(
        f"  the key: {files:,} input files, {_size(size)}, hashed in {hashed:.1f} s (not in the runs: a night"
        " takes the Object Store's digests)"
    )
    code = 0
    for reads in [*runs, None]:
        title = f"{night.title} under cProfile" if reads is None else reads.title
        if time.perf_counter() > deadline:
            out("")
            out(f"{title}: not run, the time limit ({round(limit.total_seconds() / 60)} min) has passed")
            code = 1
            continue
        profiler = cProfile.Profile() if reads is None else None
        made = make_day(
            settings.data_dir,
            chosen,
            calendar,
            symbols,
            night if reads is None else reads,
            settings.fee_rate,
            leaders,
            target_version=settings.target_version,
            key=_key(settings, chosen, calendar, night if reads is None else reads, leaders, digests),
            deadline=deadline,
            profiler=profiler,
        )
        out("")
        if profiler is None:
            lines = _run_lines(made, by_feed=reads is night)
        else:
            lines = _profile_lines(made, profiler, top)
        for line in lines:
            out(line)
        if made.cut:
            code = 1
        del made  # its samples: the next run makes its own
    return code


def _described(reads: Reads) -> str:
    kinds = ", ".join(k.name for k in reads.kinds) or "no kind"
    return f"{reads.title} ({len(reads.names)} features; {kinds})"


def _digests(
    data_dir: Path,
    day: date,
    calendar: MarketCalendar,
    symbols: Sequence[str],
    night: Reads,
    leaders: Leaders,
) -> tuple[dict[str, str], int, int, float]:
    """The SHA-256 of the files the night's reads read (every run's are among them), by path under
    ``data_dir``, as the key holds them; their number and size, and the seconds they took."""
    began = time.perf_counter()
    files = read_files(data_dir, day, calendar, symbols, night.names, leaders, night.kinds)
    digests = {p.relative_to(data_dir).as_posix(): file_digest(p) for p in files}
    return digests, len(files), sum(p.stat().st_size for p in files), time.perf_counter() - began


def _key(
    settings: Settings,
    day: date,
    calendar: MarketCalendar,
    reads: Reads,
    leaders: Leaders,
    digests: Mapping[str, str],
) -> DayKey:
    return day_key(
        settings.data_dir,
        day,
        calendar,
        settings.symbols,
        reads.names,
        settings.fee_rate,
        leaders,
        target_version=settings.target_version,
        kinds=reads.kinds,
        digests=digests,
    )


def _run_lines(made: Made, *, by_feed: bool = True) -> list[str]:
    """A timed run: its day's counts, the seconds and calls by stage, and with ``by_feed`` the events by
    feed (every run reads the same)."""
    out = [f"{_described(made.reads)}: {made.total:.1f} s" + _cut(made)]
    found = made.found
    if found is not None:
        left = ", ".join(f"{why} {n:,}" for why, n in sorted(found.left_out.items()))
        stored = "not stored (it does not read back as made)"
        if made.stored_bytes is not None:
            stored = f"stored {_size(made.stored_bytes)}"
        out.append(
            f"  {len(found.samples):,} samples, {len(found.answers):,} answers, bar closes left out"
            f" {sum(found.left_out.values()):,} ({left or 'none'}); {sum(made.feeds.values()):,} events;"
            f" {stored}"
        )
    else:
        out.append(f"  {sum(made.feeds.values()):,} events read when the time limit passed")
    out.append(f"  {'stage':<22} {'seconds':>9} {'share':>7} {'calls':>13}")
    for stage in STAGES:
        seconds = made.seconds[stage]
        share = seconds / made.total if made.total else 0.0
        calls = f"{made.calls[stage]:,}" if stage != REST else ""
        out.append(f"  {stage:<22} {seconds:>9.1f} {share:>7.1%} {calls:>13}")
    out.append(f"  {'total':<22} {made.total:>9.1f} {1:>7.1%}")
    if by_feed:
        out.append("  events by feed: " + _by_feed(made.feeds))
    return out


def _profile_lines(made: Made, profiler: cProfile.Profile, top: int) -> list[str]:
    """The profiled run: its time and the functions it went to."""
    stats = pstats.Stats(profiler).stats  # type: ignore[attr-defined]
    rows = [(_function(key), calls, own, cumulative) for key, (_, calls, own, cumulative, _) in stats.items()]
    total = sum(own for _, _, own, _ in rows) or 1.0
    out = [
        f"{made.reads.title} under cProfile: {made.total:.1f} s" + _cut(made),
        f"  {sum(made.feeds.values()):,} events; the top {top} functions by own time"
        " (the profiler slows each call: the shares, not the seconds, compare with the timed run)",
        f"  {'own s':>9} {'share':>7} {'cum s':>9} {'calls':>13}  function",
    ]
    for name, calls, own, cumulative in sorted(rows, key=lambda r: r[2], reverse=True)[:top]:
        out.append(f"  {own:>9.1f} {own / total:>7.1%} {cumulative:>9.1f} {calls:>13,}  {name}")
    ours = [r for r in rows if r[0].startswith("krx_quant/")]
    out.append(
        f"  krx_quant's first {min(15, len(ours))} functions by cumulative time (share: the cumulative's)"
    )
    for name, calls, own, cumulative in sorted(ours, key=lambda r: r[3], reverse=True)[:15]:
        out.append(f"  {own:>9.1f} {cumulative / total:>7.1%} {cumulative:>9.1f} {calls:>13,}  {name}")
    return out


def _cut(made: Made) -> str:
    return " (cut: the time limit passed; the figures are up to there)" if made.cut else ""


def _by_feed(feeds: Mapping[str, int]) -> str:
    return (
        ", ".join(f"{feed} {n:,}" for feed, n in sorted(feeds.items(), key=lambda f: (-f[1], f[0]))) or "none"
    )


def _size(size: int) -> str:
    return f"{size / 1e6:,.1f} MB" if size >= 1e5 else f"{size / 1e3:,.1f} KB"


def _function(key: tuple[str, int, str]) -> str:
    """A profiled function's name: built-ins as cProfile names them, krx_quant's and the libraries'
    by their path inside the package, the standard library's by file."""
    file, line, name = key
    if file == "~":
        return name
    path = Path(file).as_posix()
    if "/krx_quant/" in path:
        path = "krx_quant/" + path.rsplit("/krx_quant/", 1)[1]
    elif "/site-packages/" in path:
        path = path.split("/site-packages/", 1)[1]
    else:
        path = Path(file).name
    return f"{path}:{line}({name})"
