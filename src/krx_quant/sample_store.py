"""Sample store (#370): each day's training samples, kept in the state folder, so that a night
makes only the days that are new or whose samples could have changed.

Making a day's samples (``training.training_day``) replays a recorded day's events through the
engine's Market View. On the server that took 11–21 minutes a recorded day and 4–6 seconds a
backfilled one (the night of 2026-10-23: ten recorded days 8,337 s of the night's 2 h 27 min); most
of a recorded day's time follows the answer windows (the touch target's four minutes, the range's,
the barrier's and its 15-, 30- and 60-minute ladder's), the features a few per cent. A finished day's
samples change only when what they are made of does, so each is made once and kept:

- ``<root>/date=<day>/<key>.json.gz``: the day's ``TrainingDay`` as the night reads it (every
  algorithm's features at each bar close, the outcome and the touch target's terms, the kinds'
  answers, the bar closes left out), as gzipped JSON with the key it was made under. Nothing of
  the code is stored (no pickle).

The key (``day_key``) is a digest of everything the samples are made of:

- the day; the symbols, in order; the features asked for, in order; the fee rate; the leaders; the
  target version; the kinds read (each one's name, classes, answer version, horizon, whether it
  counts crossed quotes, and its answer, bar and width rules by name and arguments);
- the files the day is read from (``read_files``: its recorded partitions, or its backfilled
  bars and the leaders'; the 20 trading days' bars before it; its macro regime), each by its
  SHA-256, by its path under the data directory: the night's copy of the store and the server's
  own data give the same key when they hold the same files;
- what the samples ask of the calendar: each day's session from ``CALENDAR_REACH`` before the day
  to the day (the 20 trading days before it and theirs), the trading days to the options expiry
  after it (``days_to_expiry``, the only thing they ask of later days), and the first day it
  knows when that is within reach;
- the code that makes samples (``code_digest``): whole modules where the module is about it, else
  its functions and classes, without docstrings and comments, with the constants they read; and
  the Python, NumPy and PyArrow versions.

A day is read from the store only when the stored key is the one asked for, whole. A file cut
short (it is written aside and renamed, so only a disk's crash leaves one), unreadable or of
another key is a miss: the day is made again and its file replaced. A day is stored only if
reading it back gives every sample, value and answer exactly as made (``fingerprint``); one that
would not (a value JSON changes, say a tuple in an answer's path) is made every night instead.

Pre-validation and the simulated probation (``prevalidation._WalkForward``) read the recorded days
the night stored, by the night's key (``night_reads``), and keep the features and the answers they
read (``TrainingDay.keeping``): on a recorded day the same samples as making the day with those
alone gives, as the Market View follows every feed whatever is asked and each feature is computed
from the bar close's snapshot alone (``tests/test_sample_store.py``). A backfilled day they make
themselves, in seconds: the same samples too, as its leaders' bars are read for every feature that
reads them, whatever else is asked (``training.needs_leaders``; #373: before, for the lead features
alone, so a day made for Kalman had no futures' moves in ``kospi200f_moves``). They never write.

The night removes the days no step reads any more, the other keys of the days it read, and files
left half-written (``SampleStore.prune``).
"""

from __future__ import annotations

import ast
import contextlib
import dataclasses
import enum
import functools
import gzip
import hashlib
import importlib
import inspect
import json
import logging
import os
import sys
import types
import zlib
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from pathlib import Path
from typing import Any

import numpy
import pyarrow

from krx_quant.algorithms import Algorithm, KindAlgorithm
from krx_quant.algorithms.contract import Sample
from krx_quant.archive import partition_files
from krx_quant.backfill import MINUTE_BARS_TR, bars_key
from krx_quant.contracts import days_to_expiry
from krx_quant.features import SLOT_RETURN_DAYS, FeatureVector, Leaders, TouchTerms
from krx_quant.feeds.base import KST
from krx_quant.kind_registry import kind_named
from krx_quant.kinds import Answer, Kind, table_baselines
from krx_quant.macro import regime_prefix
from krx_quant.market_calendar import MarketCalendar
from krx_quant.object_store import file_digest
from krx_quant.pricing import Direction
from krx_quant.replay import recorded_partitions
from krx_quant.training import (
    TrainingDay,
    declared,
    is_recorded,
    leader_series,
    needs_leaders,
    needs_previous_day,
    training_day,
)

log = logging.getLogger(__name__)

STORE_DIR = "training-samples"  # under the state directory
FORMAT = 1  # the file's layout; another is a miss
# How far before a day the key holds the calendar's sessions: the 20 trading days whose bars the slot
# returns read (#265), with holidays, and their previous days, are well inside. Of the days after it
# the samples ask only the trading days to the options expiry, which the key holds as they are.
CALENDAR_REACH = timedelta(days=50)

# The code a day's samples are made with: ``training.training_day`` and what it calls. Whole modules
# where the module is about making samples; elsewhere the functions and classes it runs, and the
# names of data it reads. ``tests/test_sample_store.py`` traces ``training_day`` on recorded and
# backfilled days with every feature and kind, and fails on a function it runs outside these (and on
# one this code names, read from its source), on a file it reads outside ``read_files``, and on a
# day it asks of the calendar outside the key's.
_WHOLE = ("training", "features", "market_view", "touch", "kinds", "special", "reference")
_PARTS: dict[str, tuple[str, ...]] = {
    "algorithms.contract": ("Sample", "KindSample"),
    "archive": ("partition_files", "_held_parts", "MERGED"),
    "backfill": ("PreviousDay", "read_bars", "_stored_bar", "bars_key"),
    "barriers": ("barrier_answer", "bar_barrier", "barrier_width_at", "barrier_width_rule", "_first", "_at"),
    "consensus": ("slot",),
    "contracts": ("days_to_expiry", "last_trading_day", "nominal_expiry", "Product"),
    "engine": ("partitions", "ENGINE_FEEDS"),
    "feeds.base": ("kst_day",),
    "macro": ("day_regime", "_rows", "read_partition", "_read_partition", "regime_prefix"),
    "market_calendar": ("MarketCalendar", "Session"),
    "pricing": ("classify", "cost_ticks", "flat_ticks", "tick_size", "Direction"),
    "ranges": ("range_answer", "bar_range", "range_class", "_at"),
    "replay": ("recorded_events", "recorded_partitions", "_keys", "_partition_rows"),
}
# Imported by the whole modules for what does not make samples: the registries (the kinds a day is
# read with are in its key themselves, the algorithms only by their features).
_NOT_SAMPLE_DATA = frozenset({"ALGORITHMS", "KIND_ALGORITHMS", "KINDS"})


# --- the key -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class DayKey:
    """What a day's samples are made of (module doc), and its digest, the file's name."""

    day: date
    parts: Mapping[str, Any]  # JSON values only

    @functools.cached_property
    def canonical(self) -> str:
        return json.dumps(self.parts, sort_keys=True, separators=(",", ":"))

    @functools.cached_property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical.encode()).hexdigest()[:32]


def day_key(
    data_dir: Path,
    day: date,
    calendar: MarketCalendar,
    symbols: Sequence[str],
    names: Sequence[str],
    fee_rate: float,
    leaders: Leaders,
    *,
    target_version: int,
    kinds: Sequence[Kind],
    digests: Mapping[str, str] | None = None,
) -> DayKey:
    """The key of ``day``'s samples as ``training_day`` makes them with these arguments.
    ``digests`` gives the SHA-256 of files already known (by path under ``data_dir``, as the
    store keys them): the night's copies of the store's objects; the others are read."""
    inputs = []
    for path in read_files(data_dir, day, calendar, symbols, names, leaders, kinds):
        relative = path.relative_to(data_dir).as_posix()
        inputs.append([relative, (digests or {}).get(relative) or file_digest(path)])
    first = calendar.first_known()
    parts = {
        "format": FORMAT,
        "day": day.isoformat(),
        "symbols": list(symbols),
        "names": list(names),
        "fee_rate": repr(float(fee_rate)),
        "leaders": [list(leaders.indices), list(leaders.us), list(leaders.etfs)],
        "target_version": target_version,
        "kinds": [_kind_part(k) for k in kinds],
        "inputs": inputs,
        "calendar": {
            "sessions": [_session(calendar, d) for d in _reach(day)],
            "expiry": days_to_expiry(day, calendar),
            "first_known": first.isoformat() if first is not None and first >= day - CALENDAR_REACH else None,
        },
        "code": code_digest(),
    }
    return DayKey(day, parts)


def read_files(
    data_dir: Path,
    day: date,
    calendar: MarketCalendar,
    symbols: Sequence[str],
    names: Sequence[str],
    leaders: Leaders,
    kinds: Sequence[Kind],
) -> list[Path]:
    """The files ``training_day`` may read for ``day`` with these features and kinds, as it reads
    them: the macro regime's partition; the bars of the 20 trading days before (``PreviousDay``,
    when a feature or kind reads them); the recorded partitions (``replay.recorded_partitions``) of
    a recorded day, else the symbols' backfilled bars and, for the features that read them
    (``training.needs_leaders``), the leaders'."""
    files: list[Path] = []
    if calendar.session(day) is not None:
        files += partition_files(data_dir / regime_prefix(day) / "symbol=market")
    if needs_previous_day(names, kinds):
        before = day
        for _ in range(max(SLOT_RETURN_DAYS)):  # the previous day's is the first (#181, #183, #265)
            before = calendar.previous_trading_day(before)
            files += [p for s in symbols if (p := data_dir / bars_key(before, MINUTE_BARS_TR, s)).is_file()]
    if is_recorded(data_dir, day, symbols):
        files += [f for _, found in recorded_partitions(data_dir, day, symbols, leaders) for f in found]
    else:
        series = [(MINUTE_BARS_TR, s) for s in symbols]
        if needs_leaders(names):
            series += leader_series(leaders)
        files += [p for feed, name in series if (p := data_dir / bars_key(day, feed, name)).is_file()]
    return files


def _reach(day: date) -> Iterator[date]:
    d = day - CALENDAR_REACH
    while d <= day:
        yield d
        d += timedelta(days=1)


def _session(calendar: MarketCalendar, day: date) -> list[str] | None:
    session = calendar.session(day)
    return None if session is None else [session.open.isoformat(), session.close.isoformat()]


def _kind_part(kind: Kind) -> list[Any]:
    return [
        kind.name,
        list(kind.classes),
        kind.answer_version,
        kind.horizon.total_seconds(),
        kind.crossed,
        _stable(kind.answer),
        _stable(kind.bar_answer),
        _stable(kind.width),
    ]


@functools.cache
def code_digest() -> str:
    """A digest of the code that makes samples (``_WHOLE``, ``_PARTS``), without docstrings and
    comments, with the constants it reads; and of the Python, NumPy and PyArrow versions."""
    h = hashlib.sha256()
    for name in _WHOLE:
        module = importlib.import_module(f"krx_quant.{name}")
        source = Path(_file(module)).read_text(encoding="utf-8")
        h.update(f"\0module {name}\0{_normalized(source)}".encode())
        h.update(_imported_data(module, ast.parse(source)).encode())
    for name, objects in sorted(_PARTS.items()):
        module = importlib.import_module(f"krx_quant.{name}")
        for object_name in objects:
            h.update(f"\0{name}.{object_name}\0{_object_digest(module, object_name, objects)}".encode())
    runtime = f"{sys.version_info[:3]} numpy {numpy.__version__} pyarrow {pyarrow.__version__}"
    h.update(f"\0runtime\0{runtime}".encode())
    return h.hexdigest()[:16]


def _file(module: types.ModuleType) -> str:
    if module.__file__ is None:
        raise ValueError(f"{module.__name__}: no source file to digest")
    return module.__file__


def _normalized(source: str) -> str:
    """``source``'s syntax tree without docstrings (or any statement that is a bare string) and
    comments: what runs."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list):
            kept = [s for s in body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
            node.body = kept or [ast.Pass()]  # type: ignore[attr-defined]
    return ast.dump(tree, include_attributes=False)


def _imported_data(module: types.ModuleType, tree: ast.Module) -> str:
    """The values of the data a whole module imports from krx_quant: a constant defined elsewhere."""
    out = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("krx_quant"):
            for alias in node.names:
                local = alias.asname or alias.name
                value = module.__dict__.get(local)
                if local not in _NOT_SAMPLE_DATA and _is_data(value):
                    out.append(f"{local}={_stable(value)}")
    return "\0".join(out)


def _object_digest(module: types.ModuleType, name: str, listed: Collection[str]) -> str:
    """A function's or class's source (normalized) and the values of the data it reads from its
    module; a constant's value."""
    value = getattr(module, name)
    if not (inspect.isfunction(value) or inspect.isclass(value)):
        return _stable(value)
    source = _normalized(inspect.getsource(value))
    read = sorted(
        n
        for n in _names_read(value)
        if n not in listed and n in module.__dict__ and _is_data(module.__dict__[n])
    )
    return source + "".join(f"\0{n}={_stable(module.__dict__[n])}" for n in read)


def _names_read(value: Any) -> set[str]:
    """The global names a function's code (or a class's methods') reads."""
    codes = []
    if inspect.isfunction(value):
        codes.append(value.__code__)
    else:
        for member in vars(value).values():
            for f in (
                member,
                getattr(member, "__func__", None),
                getattr(member, "fget", None),
                getattr(member, "func", None),
            ):
                if inspect.isfunction(f):
                    codes.append(f.__code__)
    names: set[str] = set()
    while codes:
        code = codes.pop()
        names.update(code.co_names)
        codes += [c for c in code.co_consts if isinstance(c, types.CodeType)]
    return names


def _is_data(value: Any) -> bool:
    """A value whose change changes what code reading it does: not code, a module or a type
    (an enumeration is data: its members)."""
    if isinstance(value, type):
        return issubclass(value, enum.Enum)
    if isinstance(value, (types.ModuleType, types.GenericAlias, types.UnionType)):
        return False
    if type(value).__module__ in ("typing", "typing_extensions"):
        return False
    return not callable(value) or dataclasses.is_dataclass(value)


def _stable(value: Any, depth: int = 0) -> str:
    """A text of ``value`` that is the same in every process for the same value: functions by
    name and closure, unknown objects by their type alone."""
    if depth > 8:
        return "…"
    deeper = functools.partial(_stable, depth=depth + 1)
    if value is None or isinstance(value, (bool, int, float, str, bytes)):
        return repr(value)
    if isinstance(value, (datetime, date, time, timedelta, tzinfo, range)):
        return repr(value)
    if isinstance(value, enum.Enum):
        return f"{type(value).__qualname__}.{value.name}={value.value!r}"
    if isinstance(value, type):
        if issubclass(value, enum.Enum):
            return f"{value.__qualname__}({', '.join(deeper(m) for m in value)})"
        return f"{value.__module__}.{value.__qualname__}"
    if isinstance(value, functools.partial):
        return f"partial({deeper(value.func)}, {deeper(value.args)}, {deeper(value.keywords)})"
    if inspect.isfunction(value):
        cells = [deeper(c.cell_contents) for c in value.__closure__ or ()]
        return f"{value.__module__}.{value.__qualname__}[{', '.join(cells)}]"
    if callable(value) and not dataclasses.is_dataclass(value):
        named = getattr(value, "__qualname__", type(value).__qualname__)
        return f"{getattr(value, '__module__', '?')}.{named}"
    if isinstance(value, (set, frozenset)):
        return f"{type(value).__name__}({', '.join(sorted(deeper(v) for v in value))})"
    if isinstance(value, Mapping):
        return "{" + ", ".join(f"{deeper(k)}: {deeper(v)}" for k, v in value.items()) + "}"
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        fields = ", ".join(f"{f.name}={deeper(getattr(value, f.name))}" for f in dataclasses.fields(value))
        return f"{type(value).__qualname__}({fields})"
    if isinstance(value, (tuple, list)):
        return f"{type(value).__qualname__}[{', '.join(deeper(v) for v in value)}]"
    return f"<{type(value).__module__}.{type(value).__qualname__}>"


# --- the files -------------------------------------------------------------------------------------


def encode(key: DayKey, found: TrainingDay, names: Sequence[str]) -> dict[str, Any]:
    """``found`` (its samples with the features ``names``, in that order) as the file holds it."""
    return {
        "format": FORMAT,
        "key": key.parts,
        "reference": found.reference,
        "names": list(names),
        "samples": [
            [
                s.symbol,
                s.bar_close.isoformat(),
                None if s.outcome is None else s.outcome.value,
                s.change_ticks,
                s.reference,
                None if s.features.touch is None else [s.features.touch.half_spread],
                [_value(s.features.as_dict()[n]) for n in names],
            ]
            for s in found.samples
        ],
        "answers": [
            [kind, symbol, at.isoformat(), None if a is None else [a.label, a.unscorable, dict(a.path)]]
            for (kind, symbol, at), a in found.answers.items()
        ],
        "left_out": dict(found.left_out),
    }


def decode(data: Mapping[str, Any]) -> TrainingDay:
    names = list(data["names"])
    held = declared(names)
    times: dict[str, datetime] = {}

    def at(text: str) -> datetime:
        if text not in times:
            times[text] = datetime.fromisoformat(text).astimezone(KST)
        return times[text]

    samples = []
    for symbol, bar_close, outcome, change, reference, touch, values in data["samples"]:
        features = FeatureVector(
            {n: tuple(v) if isinstance(v, list) else v for n, v in zip(names, values, strict=True)},
            held,
            None if touch is None else TouchTerms(*touch),
        )
        direction = None if outcome is None else Direction(outcome)
        samples.append(Sample(symbol, at(bar_close), features, direction, change, reference))
    answers = {
        (kind, symbol, at(when)): None if a is None else Answer(a[0], a[1], a[2])
        for kind, symbol, when, a in data["answers"]
    }
    return TrainingDay(data["reference"], samples, answers, dict(data["left_out"]))


def _value(value: Any) -> Any:
    return list(value) if isinstance(value, tuple) else value


def fingerprint(found: TrainingDay) -> str:
    """Everything of a day a step reads, as text: each sample's fields, its features' names in
    order, their values and the touch terms; each answer; the bar closes left out."""
    head = dataclasses.replace(found, samples=[], answers={}, left_out={})
    samples = [
        (
            dataclasses.replace(s, features=None),  # type: ignore[arg-type]
            list(s.features),
            s.features.as_dict(),
            s.features.touch,
        )
        for s in found.samples
    ]
    return repr((head, samples, list(found.answers.items()), list(found.left_out.items())))


class SampleStore:
    """The days' samples under ``root`` (``state/training-samples``), one file a day and key."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def path(self, key: DayKey) -> Path:
        return self.root / f"date={key.day.isoformat()}" / f"{key.digest}.json.gz"

    def get(self, key: DayKey) -> TrainingDay | None:
        """The day stored under ``key``; None if none is, or the file cannot be read whole."""
        path = self.path(key)
        if not path.is_file():
            return None
        try:
            data = json.loads(gzip.decompress(path.read_bytes()))
            if (
                data.get("format") != FORMAT
                or json.dumps(data.get("key"), sort_keys=True, separators=(",", ":")) != key.canonical
            ):
                log.warning("%s holds another key; %s is made again", path, key.day)
                return None
            return decode(data)
        except (OSError, EOFError, zlib.error, ValueError, KeyError, TypeError, IndexError) as e:
            log.warning("%s cannot be read (%s: %s); %s is made again", path, type(e).__name__, e, key.day)
            return None

    def put(self, key: DayKey, found: TrainingDay, names: Sequence[str]) -> bool:
        """Store ``found`` under ``key``: written aside, then renamed over the file. Not stored
        (False) when reading it back would not give it exactly, or the disk refuses."""
        text = json.dumps(encode(key, found, names), separators=(",", ":"))
        if fingerprint(decode(json.loads(text))) != fingerprint(found):
            log.warning("%s's samples do not read back as made: not stored, made every night", key.day)
            return False
        path = self.path(key)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tmp.open("wb") as f:
                f.write(gzip.compress(text.encode(), compresslevel=6, mtime=0))
                f.flush()
                os.fsync(f.fileno())
            tmp.replace(path)
        except OSError as e:
            log.warning("could not store %s's samples (%s: %s)", key.day, type(e).__name__, e)
            tmp.unlink(missing_ok=True)
            return False
        return True

    def prune(self, keep: Collection[date], current: Mapping[date, str]) -> None:
        """Remove the days not in ``keep``, the files of the days in ``current`` other than its key's
        (an older key's), and the files left half-written."""
        for folder in sorted(self.root.glob("date=*")):
            try:
                day = date.fromisoformat(folder.name.removeprefix("date="))
            except ValueError:
                continue
            for f in list(folder.iterdir()):
                stale = day not in keep or (day in current and f.name != f"{current[day]}.json.gz")
                if stale or f.name.endswith(".tmp"):
                    f.unlink(missing_ok=True)
            with contextlib.suppress(OSError):
                folder.rmdir()  # once empty


# --- reading a day ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Read:
    """A day's samples (None: no data), the key they are stored under, and whence they came."""

    found: TrainingDay | None
    key: DayKey | None
    stored: bool  # read from the store
    made_stored: bool = False  # made, then stored


def read_day(
    store: SampleStore | None,
    data_dir: Path,
    day: date,
    calendar: MarketCalendar,
    symbols: Sequence[str],
    names: Sequence[str],
    fee_rate: float,
    leaders: Leaders,
    *,
    target_version: int,
    kinds: Sequence[Kind],
    digests: Mapping[str, str] | None = None,
) -> Read:
    """``day``'s samples as ``training_day`` makes them: from ``store`` under their key, else made
    and stored there (none: always made)."""

    def made() -> TrainingDay | None:
        return training_day(
            data_dir,
            day,
            calendar,
            symbols,
            names,
            fee_rate,
            leaders,
            target_version=target_version,
            kinds=kinds,
        )

    if store is None:
        return Read(made(), None, False)
    key = day_key(
        data_dir,
        day,
        calendar,
        symbols,
        names,
        fee_rate,
        leaders,
        target_version=target_version,
        kinds=kinds,
        digests=digests,
    )
    found = store.get(key)
    if found is not None:
        return Read(found, key, True)
    found = made()
    return Read(found, key, False, found is not None and store.put(key, found, names))


@dataclass(frozen=True)
class NightReads:
    """The features and kinds a night reads each day with (``nightly._Trainer``): its days' key."""

    names: tuple[str, ...]
    kinds: tuple[Kind, ...]


def night_reads(
    algorithms: Iterable[Algorithm],
    kind_algorithms: Iterable[KindAlgorithm],
    kinds: Sequence[Kind],
) -> NightReads:
    """Every algorithm's features (the kinds' algorithms of a registered kind among them), sorted;
    the kinds those predict and those whose baselines are tables (#180), in that order, once each."""
    of_kinds = [(a, k) for a in kind_algorithms if (k := kind_named(a.kind, kinds)) is not None]
    tabled = [k for k, _ in table_baselines([k for k in kinds if k.answer is not None])]
    read = tuple({k.name: k for k in [*(k for _, k in of_kinds), *tabled]}.values())
    everyone: list[Algorithm | KindAlgorithm] = [*algorithms, *(a for a, _ in of_kinds)]
    return NightReads(tuple(sorted({f for a in everyone for f in a.features})), read)


def stored_night_day(
    store: SampleStore,
    night: NightReads,
    data_dir: Path,
    day: date,
    calendar: MarketCalendar,
    symbols: Sequence[str],
    fee_rate: float,
    leaders: Leaders,
    *,
    target_version: int,
) -> TrainingDay | None:
    """The day as a night stored it, if one did with these settings and these files (its key
    whole); None otherwise. Read only."""
    if not store.root.is_dir():
        return None
    key = day_key(
        data_dir,
        day,
        calendar,
        symbols,
        night.names,
        fee_rate,
        leaders,
        target_version=target_version,
        kinds=night.kinds,
    )
    return store.get(key)
