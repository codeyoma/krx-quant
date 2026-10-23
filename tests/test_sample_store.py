"""The sample store (#370): each day's training samples made once and read back on later nights.

Input: eight trading days of backfilled bars (the symbol's, an ETF's, the two futures'), the last
four also recorded mornings with every feed the engine reads, archived in an Object Store. Output:
the samples read back (exactly as made), the key each day is stored under, the files in the store,
and the models a night trains (the same with the store as without).
"""

from __future__ import annotations

import asyncio
import dataclasses
import gzip
import importlib
import inspect
import json
import shutil
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import krx_quant
from krx_quant import features as features_module
from krx_quant import sample_store
from krx_quant.algorithms import ALGORITHMS, KIND_ALGORITHMS
from krx_quant.algorithms.contract import Model
from krx_quant.archive import archive
from krx_quant.backfill import BAR_SCHEMA, FUTURES_BARS_TR, bars_key
from krx_quant.barriers import barrier_width_rule
from krx_quant.config import Settings
from krx_quant.contracts import KOSPI200, USD, days_to_expiry
from krx_quant.engine import Event
from krx_quant.features import FEATURES, LEAD_FEATURES, LEADER_FEATURES, Leaders, Snapshot
from krx_quant.feeds import FOREIGN_ESTIMATE, NXT_TRADE, USD_FUTURES
from krx_quant.kind_registry import KINDS, kind_named
from krx_quant.kinds import DIRECTION_KIND, Kind
from krx_quant.market_calendar import MarketCalendar, calendar_store
from krx_quant.nightly import train_night
from krx_quant.object_store import LocalObjectStore
from krx_quant.prevalidation import _WalkForward, prevalidate
from krx_quant.reference import Bar
from krx_quant.sample_store import (
    CALENDAR_REACH,
    STORE_DIR,
    SampleStore,
    day_key,
    fingerprint,
    night_reads,
    read_day,
    read_files,
)
from krx_quant.touch import TARGET_VERSION
from krx_quant.training import TrainingDay, needs_leaders, needs_previous_day, training_day
from tests.algorithm_doubles import FrequencyAlgorithm
from tests.replay_events import at as at_time
from tests.replay_events import (
    estimates,
    every,
    foreign_members,
    futures_trade,
    index_tick,
    krx_trade,
    option_trade,
    program,
    trade,
    us_trade,
    vi,
    with_sides,
)
from tests.test_nightly import DAYS, FEE, FINISHED, NIGHT, archive_bars
from tests.test_prevalidation import AFTER, ALPHA, Contrarian
from tests.test_prevalidation import DAYS as PREVALIDATED_DAYS
from tests.test_prevalidation import settings as prevalidation_settings
from tests.test_replay import busy_morning
from tests.test_replay_cli import SCHEMAS, record

SYMBOLS = ("005930",)
LEADERS = Leaders(indices=("0013",), us=("NVDA",), etfs=("133690",))
RECORDED = DAYS[-4:]
CALENDAR = MarketCalendar(dict.fromkeys(DAYS, True))
NIGHT_READS = night_reads(ALGORITHMS, KIND_ALGORITHMS, KINDS)


def wander(when: datetime, size: float) -> float:
    """A deterministic walk of −5..+5 steps of ``size``, by the second."""
    return ((int(when.timestamp()) * 7919) % 11 - 5) * size


def rich_morning(day: date, end: str = "10:00:00") -> list[Event]:
    """Every feed the engine reads, 09:00–``end`` (the futures from 08:45): the symbol's book and
    integrated trades with their sides, its KRX and NXT trades, program trading, foreign member
    firms, the investor estimates, a VI, KOSPI and another index, both futures, the ETF, a US
    daytime ticker and two options near the money."""
    a, z = at_time("09:00:00", day), at_time(end, day)
    return [
        *with_sides(busy_morning(day, end)),
        *every(a, z, 13, lambda w: krx_trade(w, 81_200, bid=81_200, ask=81_300)),
        *every(a, z, 19, lambda w: trade(w, 81_250, 2, feed=NXT_TRADE)),
        *every(a, z, 23, lambda w: program(w, int(wander(w, 1e7)))),
        *every(a + timedelta(seconds=1), z, 29, lambda w: foreign_members(w, int(wander(w, 100)))),
        *estimates(at_time("09:31:00", day), {"1": (12_000, -3_000)}),
        vi(at_time("09:40:05", day), "09:40:00", "09:42:00"),
        *every(a, z, 5, lambda w: index_tick(w, "0001", 2500 + wander(w, 0.5))),
        *every(a, z, 5, lambda w: index_tick(w, "0013", 3000 + wander(w, 0.5))),
        *every(at_time("08:45:00", day), z, 7, lambda w: futures_trade(w, 350 + wander(w, 0.05))),
        *every(
            at_time("08:45:00", day),
            z,
            11,
            lambda w: futures_trade(w, 1340 + wander(w, 0.1), feed=USD_FUTURES, contract="A75610"),
        ),
        *every(a, z, 17, lambda w: krx_trade(w, 9000 + int(wander(w, 5)), bid=0, ask=0, symbol="133690")),
        *every(a, z, 9, lambda w: us_trade(w, "NVDA", 180 + wander(w, 0.01))),
        *every(a, z, 31, lambda w: option_trade(w, "B01612355", 15 + wander(w, 0.1), 0.5)),
        *every(a, z, 37, lambda w: option_trade(w, "C01612355", 16 + wander(w, 0.1), -0.5)),
    ]


def archive_futures(root: Path, day: date) -> None:
    """Both futures' continuous one-minute bars, where the backfill puts them."""
    bars = pq.read_table(root / bars_key(day, "FHKST03010230", "005930")).to_pylist()
    for series, scale in ((KOSPI200.series, 0.0043), (USD.series, 0.0165)):
        rows = [
            {**r, "symbol": series, **{k: round(r[k] * scale) for k in ("open", "high", "low", "close")}}
            for r in bars
        ]
        path = root / bars_key(day, FUTURES_BARS_TR, series)
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.Table.from_pylist(rows, schema=BAR_SCHEMA), path)


def archive_rich(root: Path, day: date, work: Path) -> None:
    data = work / f"recorded-{day}"
    schemas = {**SCHEMAS, FOREIGN_ESTIMATE.tr_id: FOREIGN_ESTIMATE.schema}
    record(data, sorted(rich_morning(day), key=lambda e: e[1]["received_at"]), schemas, day=day)
    asyncio.run(archive(data, LocalObjectStore(root)))


@pytest.fixture(scope="module")
def market(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The Object Store's days, made once for the module (a directory laid out as a data
    directory, too)."""
    work = tmp_path_factory.mktemp("market")
    root = work / "store"
    for day in DAYS:
        archive_bars(root, day)
        archive_bars(root, day, symbol="133690")
        archive_futures(root, day)
    for day in RECORDED:
        archive_rich(root, day, work)
    return root


@pytest.fixture
def store_root(market: Path, tmp_path: Path) -> Path:
    """A copy of the market's store this test may write to."""
    return Path(shutil.copytree(market, tmp_path / "store"))


def night(
    root: Path,
    cache: Path,
    samples: Path | None,
    algorithms: Sequence[Any] = ALGORITHMS,
    kinds: Sequence[Kind] = KINDS,
    target_version: int = TARGET_VERSION,
    **more: Any,
) -> Any:
    return train_night(
        LocalObjectStore(root),
        cache,
        symbols=SYMBOLS,
        fee_rate=FEE,
        now=lambda: FINISHED,
        algorithms=algorithms,
        leaders=LEADERS,
        target_version=target_version,
        kinds=kinds,
        samples=samples,
        **more,
    )


def made(
    market: Path,
    day: date,
    names: Sequence[str] = NIGHT_READS.names,
    kinds: Sequence[Kind] = NIGHT_READS.kinds,
    *,
    target_version: int = TARGET_VERSION,
    calendar: MarketCalendar = CALENDAR,
) -> TrainingDay:
    """``day`` made from the files, as ``training_day`` makes it."""
    found = training_day(
        market,
        day,
        calendar,
        SYMBOLS,
        names,
        FEE,
        LEADERS,
        target_version=target_version,
        kinds=kinds,
    )
    assert found is not None
    return found


def read_it(
    store: SampleStore, market: Path, day: date = NIGHT, names: Sequence[str] = NIGHT_READS.names
) -> Any:
    """``day`` through the store, as the night reads it."""
    return read_day(
        store,
        market,
        day,
        CALENDAR,
        SYMBOLS,
        names,
        FEE,
        LEADERS,
        target_version=TARGET_VERSION,
        kinds=NIGHT_READS.kinds if names == NIGHT_READS.names else (),
    )


def key_of(data: Path, day: date = NIGHT, **change: Any) -> str:
    args: dict[str, Any] = {
        "calendar": CALENDAR,
        "symbols": SYMBOLS,
        "names": NIGHT_READS.names,
        "fee_rate": FEE,
        "leaders": LEADERS,
        "target_version": TARGET_VERSION,
        "kinds": NIGHT_READS.kinds,
        **change,
    }
    return day_key(
        data,
        day,
        args["calendar"],
        args["symbols"],
        args["names"],
        args["fee_rate"],
        args["leaders"],
        target_version=args["target_version"],
        kinds=args["kinds"],
    ).digest


def stored_files(samples: Path) -> dict[str, list[str]]:
    return {d.name: sorted(f.name for f in d.iterdir()) for d in sorted(samples.glob("date=*"))}


def without_resources(report: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in report.items() if k != "resources"}


# --- reading back ----------------------------------------------------------------------------


@pytest.mark.parametrize("day", [NIGHT, DAYS[2]], ids=["recorded", "backfilled"])
def test_a_stored_day_reads_back_exactly_as_it_was_made(market: Path, tmp_path: Path, day: date) -> None:
    """Every sample's fields, features (names in order, values, series as tuples, the touch terms),
    every kind's answer with its path, the bar closes left out."""
    store = SampleStore(tmp_path / STORE_DIR)
    first = read_it(store, market, day)
    again = read_it(store, market, day)

    assert (first.stored, first.made_stored, again.stored) == (False, True, True)
    assert first.found is not None and again.found is not None
    assert fingerprint(again.found) == fingerprint(first.found) == fingerprint(made(market, day))
    assert first.found.samples and first.found.answers
    if day == NIGHT:
        assert first.found.reference == "mid" and first.found.left_out["vi"] > 0, "the VI's bar closes"
        assert any(
            isinstance(v, tuple) and v for s in again.found.samples for v in s.features.as_dict().values()
        )


def test_a_day_that_would_not_read_back_exactly_is_not_stored(
    market: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A feature whose value JSON would change (a list read back as a tuple): the day is made every
    night, never stored."""
    monkeypatch.setitem(features_module.FEATURES, "velocity", lambda s: [1.0, 2.0])
    store = SampleStore(tmp_path / STORE_DIR)

    read = read_it(store, market, DAYS[2], ("velocity",))

    assert read.found is not None and not read.stored and not read.made_stored
    assert read.key is not None and not store.path(read.key).exists()
    assert "do not read back as made" in caplog.text


@pytest.mark.parametrize(
    "broken",
    [
        lambda whole: whole[: len(whole) // 2],
        lambda whole: b"",
        lambda whole: b"not gzip at all",
        lambda whole: gzip.compress(b'{"format": 1, "key": '),
        lambda whole: gzip.compress(json.dumps({**json.loads(gzip.decompress(whole)), "key": {}}).encode()),
    ],
    ids=["cut short", "empty", "not gzip", "not JSON", "another key"],
)
def test_a_file_cut_short_unreadable_or_of_another_key_is_made_again_and_replaced(
    market: Path, tmp_path: Path, broken: Callable[[bytes], bytes]
) -> None:
    store = SampleStore(tmp_path / STORE_DIR)

    first = read_it(store, market)
    path = store.path(first.key)
    whole = path.read_bytes()
    path.write_bytes(broken(whole))

    assert store.get(first.key) is None
    again = read_it(store, market)
    assert (again.stored, again.made_stored) == (False, True)
    assert fingerprint(again.found) == fingerprint(first.found)
    assert path.read_bytes() == whole, "replaced by the day made again"
    assert read_it(store, market).stored


def test_a_file_left_half_written_is_never_read_and_the_night_removes_it(
    market: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash while writing leaves the file aside (``.<key>.json.gz.<pid>.tmp``), never under the
    key's name: the day is a miss, and the next prune removes what was left."""
    store = SampleStore(tmp_path / STORE_DIR)

    def crash(fd: int) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("os.fsync", crash)
    first = read_it(store, market)
    assert first.found is not None and not first.made_stored
    assert not store.path(first.key).exists() and store.get(first.key) is None
    monkeypatch.undo()
    left = store.path(first.key).with_name(f".{store.path(first.key).name}.4242.tmp")
    left.write_bytes(b"\x1f\x8b half")

    second = read_it(store, market)
    assert second.made_stored and store.get(second.key) is not None
    store.prune({NIGHT}, {NIGHT: second.key.digest})
    assert stored_files(store.root) == {f"date={NIGHT}": [f"{second.key.digest}.json.gz"]}


# --- the key ---------------------------------------------------------------------------------


def with_holiday(day: date) -> MarketCalendar:
    return MarketCalendar({**dict.fromkeys(DAYS, True), day: False})


KIND = NIGHT_READS.kinds[-1]  # the 60-minute barrier

CHANGES: dict[str, Callable[[], dict[str, Any]]] = {
    "the day": lambda: {"day": DAYS[-2]},
    "the symbols": lambda: {"symbols": ("005930", "000660")},
    "the features' order": lambda: {"names": tuple(reversed(NIGHT_READS.names))},
    "a feature fewer": lambda: {"names": NIGHT_READS.names[1:]},
    "the fee rate": lambda: {"fee_rate": FEE * 2},
    "the leaders": lambda: {"leaders": Leaders(indices=("0013",), us=("MU",), etfs=("133690",))},
    "the target version": lambda: {"target_version": TARGET_VERSION - 1},
    "a kind's answer version": lambda: {
        "kinds": (*NIGHT_READS.kinds[:-1], dataclasses.replace(KIND, answer_version=KIND.answer_version + 1))
    },
    "a kind's horizon": lambda: {
        "kinds": (*NIGHT_READS.kinds[:-1], dataclasses.replace(KIND, horizon=KIND.horizon * 2))
    },
    "a kind's width rule": lambda: {
        "kinds": (*NIGHT_READS.kinds[:-1], dataclasses.replace(KIND, width=barrier_width_rule(45)))
    },
    "a kind fewer": lambda: {"kinds": NIGHT_READS.kinds[:-1]},
    "a holiday within reach": lambda: {"calendar": with_holiday(NIGHT - CALENDAR_REACH + timedelta(days=3))},
    "a holiday before the options expiry": lambda: {"calendar": with_holiday(date(2026, 10, 7))},
}


@pytest.mark.parametrize("change", list(CHANGES))
def test_each_thing_the_samples_are_made_of_changes_their_key(market: Path, change: str) -> None:
    args = CHANGES[change]()
    day = args.pop("day", NIGHT)
    assert key_of(market, day, **args) != key_of(market)


def rewrite_close(path: Path) -> None:
    rows = pq.read_table(path).to_pylist()
    rows[-1]["close"] += 100
    pq.write_table(pa.Table.from_pylist(rows, schema=BAR_SCHEMA), path)


@pytest.mark.parametrize(
    "change",
    [
        "a previous day's bars changed",
        "a previous day's bars appearing",
        "a recorded part appearing",
        "the code",
    ],
)
def test_the_files_and_the_code_the_samples_are_made_of_change_their_key(
    store_root: Path, change: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = bars_key(DAYS[-2], "FHKST03010230", "005930")
    if change == "a previous day's bars appearing":
        held = (store_root / before).read_bytes()
        (store_root / before).unlink()
        missing = key_of(store_root)
        (store_root / before).write_bytes(held)
        assert key_of(store_root) != missing
        return
    original = key_of(store_root)
    if change == "a previous day's bars changed":
        rewrite_close(store_root / before)
    elif change == "a recorded part appearing":
        partition = store_root / "events" / f"date={NIGHT}" / "feed=H0UNCNT0" / "symbol=005930"
        shutil.copy(partition / "data.parquet", partition / "part-99999999.parquet")
    else:
        monkeypatch.setattr(sample_store, "code_digest", lambda: "another")
    assert key_of(store_root) != original


@pytest.mark.parametrize(
    "change",
    [
        "a holiday beyond reach",
        "a holiday after the options expiry",
        "another symbol's bars",
        "the day's own bars on a recorded day",
        "a feed not read",
    ],
)
def test_what_the_samples_are_not_made_of_leaves_their_key(store_root: Path, change: str) -> None:
    original = key_of(store_root)
    if change == "a holiday beyond reach":  # a Friday the calendar knows either way
        old = NIGHT - CALENDAR_REACH - timedelta(days=4)
        known = MarketCalendar({**dict.fromkeys(DAYS, True), old: True})
        assert key_of(store_root, calendar=known) == key_of(store_root, calendar=with_holiday(old))
        return
    if change == "a holiday after the options expiry":  # 2026-10-08, the Thursday before
        assert key_of(store_root, calendar=with_holiday(date(2026, 10, 12))) == original
        return
    if change == "another symbol's bars":
        archive_bars(store_root, NIGHT, symbol="000660")
    elif change == "the day's own bars on a recorded day":
        rewrite_close(store_root / bars_key(NIGHT, "FHKST03010230", "005930"))
    else:
        night_futures = store_root / "events" / f"date={NIGHT}" / "feed=H0MFCNT0" / "symbol=A01612"
        night_futures.mkdir(parents=True)
        (night_futures / "data.parquet").write_bytes(b"not read")
    assert key_of(store_root) == original


def test_a_docstring_or_a_comment_leaves_the_code_digest() -> None:
    code = 'def f(x):\n    """Twice."""\n    return 2 * x  # doubled\n'
    assert sample_store._normalized(code) == sample_store._normalized(
        'def f(x):\n    """Doubles."""\n    return 2 * x\n'
    )
    assert sample_store._normalized(code) != sample_store._normalized("def f(x):\n    return 3 * x\n")


# --- what the key holds: the guards ----------------------------------------------------------


def _module_of(filename: str) -> str:
    relative = Path(filename).relative_to(Path(krx_quant.__file__).parent).with_suffix("")
    parts = [p for p in relative.parts if p != "__init__"]
    return ".".join(parts)


def _covered(module: str, qualname: str) -> bool:
    if module in sample_store._WHOLE:
        return True
    return qualname.split(".")[0] in sample_store._PARTS.get(module, ())


# Traced, the days take some seconds more than the 30 s default on a loaded runner (#327).
@pytest.mark.timeout(120)
def test_every_function_that_makes_samples_is_in_the_code_digest(market: Path) -> None:
    """A recorded day (with the touch target and the four-minute move) and a backfilled one, with
    every feature and kind, traced: each function of krx_quant they run is in a module the digest
    takes whole, or is one it takes by name."""
    codes: set[Any] = set()

    def profile(frame: Any, event: str, arg: Any) -> None:
        codes.add(frame.f_code)

    sys.setprofile(profile)
    try:
        made(market, NIGHT)
        made(market, NIGHT, target_version=1)
        made(market, DAYS[2])
    finally:
        sys.setprofile(None)

    package = str(Path(krx_quant.__file__).parent)
    ran = {
        (_module_of(c.co_filename), c.co_qualname)
        for c in codes
        if c.co_filename.startswith(package) and c.co_name != "<module>"
    }
    outside = sorted(f"{m}: {q}" for m, q in ran if not _covered(m, q))
    assert not outside, f"run while making samples, not in sample_store._WHOLE or _PARTS: {outside}"
    assert len(ran) > 100


# Of the whole modules, what names code outside the digest and makes no samples: the ``krx-quant fit``
# command, and the leaders as the settings give them.
NOT_SAMPLES = frozenset({"training.fit_algorithm", "features.Leaders"})


def test_what_the_digest_s_code_names_is_in_the_digest_too() -> None:
    """Beside the trace, by reading the code (branches the fixture's days never take included):
    each krx_quant function or class that a function or class of the digest names is in it too."""
    outside = []
    units: list[tuple[str, str, Any]] = []
    for name in sample_store._WHOLE:
        module = importlib.import_module(f"krx_quant.{name}")
        units += [
            (name, o, v)
            for o, v in vars(module).items()
            if (inspect.isfunction(v) or inspect.isclass(v)) and v.__module__ == module.__name__
        ]
    for name, objects in sample_store._PARTS.items():
        module = importlib.import_module(f"krx_quant.{name}")
        units += [(name, o, getattr(module, o)) for o in objects]
    for name, o, value in units:
        if f"{name}.{o}" in NOT_SAMPLES or not (inspect.isfunction(value) or inspect.isclass(value)):
            continue
        module = importlib.import_module(f"krx_quant.{name}")
        for read in sample_store._names_read(value):
            named = module.__dict__.get(read)
            if not (inspect.isfunction(named) or inspect.isclass(named)) or sample_store._is_data(named):
                continue
            where = getattr(named, "__module__", "").removeprefix("krx_quant.")
            if getattr(named, "__module__", "").startswith("krx_quant") and not _covered(
                where, named.__qualname__
            ):
                outside.append(f"{name}.{o} names {where}.{named.__qualname__}")
    assert not outside, outside


@pytest.mark.parametrize(
    ("day", "algorithm"),
    [(NIGHT, None), (DAYS[2], None), (DAYS[2], "kalman"), (DAYS[2], "newton")],
    ids=["recorded", "backfilled", "backfilled, kalman's features", "backfilled, newton's features"],
)
def test_every_file_a_day_reads_is_in_its_key(
    market: Path, day: date, algorithm: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With every algorithm's features and kinds, as the night reads a day, or with one
    algorithm's alone (#373: one that reads the leaders' bars without a lead feature, one that
    reads none)."""
    names, kinds = NIGHT_READS.names, NIGHT_READS.kinds
    if algorithm is not None:
        names, kinds = next(a for a in ALGORITHMS if a.name == algorithm).features, ()
    opened: set[Path] = set()

    def seen(real: Callable[..., Any]) -> Callable[..., Any]:
        def reading(source: Any, *args: Any, **kwargs: Any) -> Any:
            opened.add(Path(source).resolve())
            return real(source, *args, **kwargs)

        return reading

    for name in ("read_table", "ParquetFile", "read_schema"):
        monkeypatch.setattr(pq, name, seen(getattr(pq, name)))
    made(market, day, names, kinds)
    monkeypatch.undo()

    files = read_files(market, day, CALENDAR, SYMBOLS, names, LEADERS, kinds)
    keyed = {p.resolve() for p in files}
    assert opened and opened <= keyed, sorted(str(p) for p in opened - keyed)


class Watched(Mapping[str, Any]):
    """A mapping that notes, under ``name`` in ``seen``, that something read it."""

    def __init__(self, values: Mapping[str, Any], seen: set[str], name: str) -> None:
        self._values, self._seen, self._name = values, seen, name

    def __getitem__(self, key: str) -> Any:
        self._seen.add(self._name)
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        self._seen.add(self._name)
        return iter(self._values)

    def __len__(self) -> int:
        self._seen.add(self._name)
        return len(self._values)


class WatchedList(Sequence[Any]):
    """A sequence that notes, under ``name`` in ``seen``, that something read it."""

    def __init__(self, values: Sequence[Any], seen: set[str], name: str) -> None:
        self._values, self._seen, self._name = values, seen, name

    def __getitem__(self, index: Any) -> Any:
        self._seen.add(self._name)
        return self._values[index]

    def __len__(self) -> int:
        self._seen.add(self._name)
        return len(self._values)


def _moving_bars(day: date, start: str, end: str, price: float, step: float) -> list[Bar]:
    """A bar a minute from ``start`` to ``end``, its close stepping up and down around ``price``."""
    out, t, stop = [], at_time(start, day), at_time(end, day)
    while t < stop:
        close = price + step * ((int(t.timestamp()) // 60 * 7919) % 7 - 3)
        out.append(Bar(t, close, close + step, close - step, close, 10, 1, t, t))
        t += timedelta(minutes=1)
    return out


PREVIOUS_FIELDS = ("previous_ranges", "previous_bars", "previous_slot_returns")


def _what_each_feature_reads() -> dict[str, set[str]]:
    """Each feature computed on a bar close that has everything (the day's bars, every lead
    group's, the previous days' ranges, bars and slot returns): which of the leaders' bars and the
    previous days' fields it read."""
    day = DAYS[2]
    session = CALENDAR.session(day)
    assert session is not None
    bars = _moving_bars(day, "09:00:00", "10:00:00", 81_200, 100)
    base = Snapshot.at(
        "005930",
        at_time("10:00:00", day),
        bars,
        bars[-1].close,
        FEE,
        session_open=session.open,
        regime="calm",
    )
    groups = {
        "peers": [_moving_bars(day, "09:00:00", "10:00:00", 190_000, 500)],
        "kospi200f": [_moving_bars(day, "08:45:00", "10:00:00", 350, 0.05)],
        "usdf": [_moving_bars(day, "08:45:00", "10:00:00", 1340, 0.1)],
        "indices": [_moving_bars(day, "09:00:00", "10:00:00", 3000, 0.5)],
        "us": [_moving_bars(day, "09:00:00", "10:00:00", 180, 0.01)],
        "etfs": [_moving_bars(day, "09:00:00", "10:00:00", 9000, 5)],
    }
    previous = _moving_bars(DAYS[1], "09:00:00", "15:20:00", 81_000, 100)
    ranges = {f"{h:02d}:{m:02d}": 150.0 for h in range(9, 16) for m in (0, 30)}
    slots = [{f"{h:02d}:{m:02d}": 1.5 for h in range(9, 16) for m in range(0, 60, 10)}] * 20
    out: dict[str, set[str]] = {}
    for name, feature in FEATURES.items():
        seen: set[str] = set()
        snapshot = dataclasses.replace(  # a fresh one: its cached moves and width are not shared
            base,
            leaders=Watched(groups, seen, "leaders"),
            previous_ranges=Watched(ranges, seen, "previous_ranges"),
            previous_bars=WatchedList(previous, seen, "previous_bars"),
            previous_slot_returns=WatchedList(slots, seen, "previous_slot_returns"),
        )
        feature(snapshot)
        out[name] = seen
    return out


def test_a_backfilled_day_loads_what_a_feature_reads_for_it_whatever_else_is_asked() -> None:
    """#373. A backfilled day reads the leaders' bars (``needs_leaders``) and the previous days'
    (``needs_previous_day``) only when a feature asked for reads them, so the features that read
    them must be exactly those the gates name: else a feature would have them in the night's day
    (every algorithm's features) and not in one made for its algorithm alone (pre-validation's).
    Each feature is computed on a bar close that has everything and each read noted."""
    reads = _what_each_feature_reads()

    leading = {name for name, seen in reads.items() if "leaders" in seen}
    assert leading == LEADER_FEATURES == {name for name in FEATURES if needs_leaders([name])}
    assert leading == LEAD_FEATURES | {"kospi200f_moves"}
    before = {name for name, seen in reads.items() if seen & set(PREVIOUS_FIELDS)}
    assert before == {name for name in FEATURES if needs_previous_day([name])}
    assert before  # the previous day's range, the barrier's width, the slot returns


class Asked(MarketCalendar):
    """A calendar that keeps what it was asked from outside (not by its own methods), but for the
    options expiry (``days_to_expiry``: the key holds its answer)."""

    def __init__(self, open_days: dict[date, bool]) -> None:
        super().__init__(open_days)
        self.asked: list[tuple[str, tuple[Any, ...], Any]] = []
        self._depth = 0
        self.expiring = False


def _asking(name: str) -> Callable[..., Any]:
    def method(self: Asked, *args: Any) -> Any:
        self._depth += 1
        try:
            result = getattr(MarketCalendar, name)(self, *args)
        finally:
            self._depth -= 1
        if self._depth == 0 and not self.expiring:
            self.asked.append((name, args, result))
        return result

    return method


_ASKED = ("session", "is_trading_day", "previous_trading_day", "known", "first_known", "trading_days")
for _name in (*_ASKED, "knows", "known_days", "phase"):
    setattr(Asked, _name, _asking(_name))


@pytest.mark.parametrize("day", [NIGHT, DAYS[2]], ids=["recorded", "backfilled"])
def test_the_calendar_a_day_asks_of_is_within_its_key_s_reach(
    market: Path, day: date, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sessions from ``CALENDAR_REACH`` before the day to the day, the first day known and
    the trading days to the options expiry: nothing else of the calendar."""
    calendar = Asked(dict.fromkeys(DAYS, True))
    expiries: list[int] = []

    def expiry(on: date, of: MarketCalendar) -> int:
        calendar.expiring = True
        try:
            expiries.append(days_to_expiry(on, of))
        finally:
            calendar.expiring = False
        return expiries[-1]

    for module in ("training", "market_view"):
        monkeypatch.setattr(f"krx_quant.{module}.days_to_expiry", expiry)
    made(market, day, calendar=calendar)

    def days_in(value: Any) -> list[date]:
        if isinstance(value, datetime):
            return [value.date()]
        if isinstance(value, date):
            return [value]
        if isinstance(value, (tuple, list)):
            return [d for v in value for d in days_in(v)]
        return []

    asked = {name for name, _, _ in calendar.asked}
    assert asked and not asked & {"known", "known_days", "trading_days"}, (
        "the whole calendar is not in the key"
    )
    far = [
        (name, args, result)
        for name, args, result in calendar.asked
        if name != "first_known"
        and any(not day - CALENDAR_REACH <= d <= day for d in days_in(args) + days_in(result))
    ]
    assert not far
    assert expiries and set(expiries) == {days_to_expiry(day, calendar)}


# --- the nights ------------------------------------------------------------------------------


# Every registered algorithm and kind trained three times on eight days: about a minute on a laptop,
# more on a loaded CI runner (#327).
@pytest.mark.timeout(400)
def test_a_night_trains_the_same_models_from_stored_days_as_from_days_made_anew(
    store_root: Path, tmp_path: Path
) -> None:
    """#370. The night without the store, with it empty (every day made and stored) and with it
    full (every day read back): the same models of every algorithm, table and kind, and the same
    reports but for their time and memory."""
    roots = [Path(shutil.copytree(store_root, tmp_path / f"store-{i}")) for i in range(3)]
    samples = tmp_path / "state" / STORE_DIR

    runs = [
        night(roots[0], tmp_path / "cache-0", None),
        night(roots[1], tmp_path / "cache-1", samples),
        night(roots[2], tmp_path / "cache-2", samples),
    ]

    made_days = [sum(r["resources"]["days_made"] for r in run.reports) for run in runs]
    read_days = [sum(r["resources"]["days_read"] for r in run.reports) for run in runs]
    assert made_days[0] == read_days[0] == made_days[1] == read_days[2] == len(DAYS)
    assert made_days[2] == 0, "every day read back from the store"
    models = [
        {
            p.relative_to(root).as_posix(): json.loads(p.read_text())
            for p in sorted((root / "models").rglob("*.json"))
        }
        for root in roots
    ]
    assert models[0] and models[1] == models[0] and models[2] == models[0]
    reports = [[without_resources(r) for r in run.reports] for run in runs]
    assert reports[1] == reports[0] and reports[2] == reports[0]
    assert [run.promoted for run in runs[1:]] == [runs[0].promoted] * 2


def test_the_next_night_makes_only_its_new_day(
    store_root: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO", logger="krx_quant")
    samples = tmp_path / "state" / STORE_DIR
    learners = [FrequencyAlgorithm("learner", "velocity"), FrequencyAlgorithm("tape", "trade_imbalance")]
    night(store_root, tmp_path / "cache", samples, learners, (DIRECTION_KIND,), day=DAYS[-2])
    caplog.clear()

    result = night(store_root, tmp_path / "cache", samples, learners, (DIRECTION_KIND,))

    first = result.reports[0]["resources"]
    assert (first["days_read"], first["days_made"]) == (7, 1)
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("read 2026-")]
    assert [line for line in lines if line.endswith("(made, stored)")] == [
        next(line for line in lines if line.startswith(f"read {NIGHT}:"))
    ]
    # The learner's six days before it, and the climate's oldest (#169: its twenty days are all eight).
    assert sum(line.endswith("(from the sample store)") for line in lines) == 7
    assert "learner: " in caplog.text and "7 days read (1 made)" in caplog.text


def test_the_night_removes_the_days_no_step_reads_and_older_keys(store_root: Path, tmp_path: Path) -> None:
    """The store keeps the days some step reads (those of steps a resumed night skips too), one
    file each, the key the night read it by; the rest goes, half-written files with it."""
    samples = tmp_path / "state" / STORE_DIR
    old = samples / "date=2026-01-02"
    old.mkdir(parents=True)
    (old / f"{'a' * 32}.json.gz").write_bytes(b"")
    stale = samples / f"date={NIGHT}" / f"{'b' * 32}.json.gz"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"")
    (stale.parent / ".x.json.gz.7.tmp").write_bytes(b"")
    wide = FrequencyAlgorithm("wide", "velocity")  # its four training days and three validation days
    narrow = FrequencyAlgorithm("narrow", "volatility")
    narrow.training_days = 1  # DAYS[-4] and the validation days

    night(store_root, tmp_path / "cache", samples, [wide, narrow], (DIRECTION_KIND,), target_version=1)

    window = [f"date={d}" for d in DAYS[-7:]]
    assert sorted(stored_files(samples)) == window
    assert all(len(files) == 1 for files in stored_files(samples).values())
    # A night cut short after wide: run again, narrow reads its four days alone; wide's are kept.
    (store_root / "promotions" / f"{NIGHT}.json").unlink()
    (store_root / "reports" / str(NIGHT) / "narrow.json").unlink()
    night(store_root, tmp_path / "cache", samples, [wide, narrow], (DIRECTION_KIND,), target_version=1)
    assert sorted(stored_files(samples)) == window


# --- pre-validation and the simulated probation -----------------------------------------------


def asked_alone() -> list[tuple[tuple[str, ...], tuple[Kind, ...]]]:
    """What pre-validation and the simulated probation ask of a day: every registered algorithm's
    features (with its kind), the climate's labels (nothing) and each kind's tables' answers."""
    asked: list[tuple[tuple[str, ...], tuple[Kind, ...]]] = [((), ())]
    asked += [(a.features, ()) for a in ALGORITHMS]
    for a in KIND_ALGORITHMS:
        kind = kind_named(a.kind, KINDS)
        assert kind is not None
        asked += [(a.features, (kind,)), ((), (kind,))]
    return list(dict.fromkeys(asked))


def walk_settings(market: Path, tmp_path: Path) -> Settings:
    """Settings of the market's days, as pre-validation reads them."""
    return Settings(
        kis_app_key="",
        kis_app_secret="",
        symbols=SYMBOLS,
        etf_symbols=LEADERS.etfs,
        index_codes=LEADERS.indices,
        us_symbols=LEADERS.us,
        data_dir=market,
        state_dir=tmp_path / "state",
        fee_rate=FEE,
        target_version=TARGET_VERSION,
    )


@pytest.mark.parametrize("target_version", [1, TARGET_VERSION])
@pytest.mark.parametrize("day", [NIGHT, DAYS[2]], ids=["recorded", "backfilled"])
def test_a_day_kept_to_some_features_and_kinds_is_that_day_made_with_them_alone(
    market: Path, day: date, target_version: int
) -> None:
    """What pre-validation and the simulated probation read of the night's day (``keeping``) is
    what making the day with the candidate's features and kind alone gives: a recorded day read
    from the store, a backfilled one made (#373: its leaders' bars were read for the lead features
    alone, so a candidate without one, as Kalman, had no futures' moves there)."""
    whole = made(market, day, target_version=target_version)
    for names, kinds in asked_alone():
        alone = made(market, day, names, kinds, target_version=target_version)
        kept = whole.keeping(names, [k.name for k in kinds])
        assert fingerprint(kept) == fingerprint(alone), (names, [k.name for k in kinds])


def test_a_backfilled_day_made_with_kalman_s_features_alone_has_the_night_s_futures_moves(
    market: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#373. Kalman reads the futures' moves (``kospi200f_moves``) and no lead feature: made with
    its features alone, a backfilled day has the futures' moves the night's day (every algorithm's
    features) has. So pre-validation reads a backfilled day from the store too, as a recorded one:
    both the night's, kept to Kalman's features."""
    kalman = next(a for a in ALGORITHMS if a.name == "kalman")
    assert not LEAD_FEATURES & set(kalman.features)
    whole, alone = made(market, DAYS[2]), made(market, DAYS[2], kalman.features, ())
    moves = [s.features.series("kospi200f_moves") for s in whole.keeping(kalman.features, ()).samples]
    assert any(moves), "the fixture's futures move"
    assert [s.features.series("kospi200f_moves") for s in alone.samples] == moves

    s = walk_settings(market, tmp_path)
    store = SampleStore(s.state_dir / STORE_DIR)
    for day in (DAYS[2], NIGHT):
        assert read_it(store, market, day).made_stored
    calls: list[date] = []

    def counted(data_dir: Path, day: date, *args: Any, **kwargs: Any) -> Any:
        calls.append(day)
        return training_day(data_dir, day, *args, **kwargs)

    monkeypatch.setattr("krx_quant.prevalidation.training_day", counted)
    trainer = _WalkForward(s, kalman, DAYS, CALENDAR, LEADERS, night=NIGHT_READS)

    backfilled, recorded = trainer._read(DAYS[2], kalman.features), trainer._read(NIGHT, kalman.features)
    assert backfilled is not None
    assert fingerprint(backfilled) == fingerprint(whole.keeping(kalman.features, ()))
    assert recorded is not None
    assert fingerprint(recorded) == fingerprint(made(market, NIGHT).keeping(kalman.features, ()))
    assert calls == [], "both days from the store"


class FuturesReader(FrequencyAlgorithm):
    """A stand-in that learns the futures' last move from every day, backfilled ones too (Kalman
    learns from the book's days alone): its model holds the mean over the samples that have one,
    beside the class frequencies it forecasts."""

    training_days = len(DAYS) - 1

    def fit(self, samples: Sequence[Any]) -> Any:
        scored = [s for s in samples if s.outcome is not None]
        last = [m[-1] for s in scored if (m := s.features.series("kospi200f_moves"))]
        moves = {"moves": float(len(last)), "mean": sum(last) / len(last) if last else 0.0}
        return Model.fitted(self.name, {**super().fit(samples).params, **moves}, scored, 0.0)


def test_pre_validation_trains_a_futures_reader_on_the_night_s_samples(market: Path, tmp_path: Path) -> None:
    """#373. The walk-forward's model before the night's day is the one fitted on the night's days
    (every algorithm's features), kept to the candidate's: the backfilled days' futures' moves
    included."""
    reader = FuturesReader("futures-reader", "kospi200f_moves")
    s = walk_settings(market, tmp_path)
    nights = {d: made(market, d) for d in DAYS[:-1]}  # the window before the night's day
    backfilled = [x for d in nights if d not in RECORDED for x in nights[d].samples]
    assert any(x.features.series("kospi200f_moves") for x in backfilled), "the backfilled days' futures move"
    nightly = reader.fit([x for found in nights.values() for x in found.of(reader.features)])

    model = _WalkForward(s, reader, DAYS, CALENDAR, LEADERS, night=NIGHT_READS).before(NIGHT)

    assert (model.version, model.params) == (nightly.version, nightly.params)

    def moving(days: Sequence[date]) -> int:
        return sum(
            1
            for d in days
            for x in nights[d].samples
            if x.outcome is not None and x.features.series("kospi200f_moves")
        )

    assert nightly.params["moves"] == moving(list(nights)) > moving([d for d in nights if d in RECORDED])


# Two nights' worth of days made, and two pre-validations replaying the four recorded mornings.
@pytest.mark.timeout(240)
async def test_pre_validation_reads_backfilled_days_a_night_stored_and_judges_the_same(
    store_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#373. The market's days (four backfilled, four recorded) pre-validated twice for a candidate
    that learns the futures' moves on every day, beside one that reads a lead feature (so the
    night's days are made with one): once beside the night's stored days (none made again, the
    backfilled ones included), once without. The same models before each day, checks and
    statistics."""
    algorithms: list[Any] = [
        FrequencyAlgorithm("leader", "lead_kospi200f_1"),
        FuturesReader("futures-reader", "kospi200f_moves"),
    ]
    states = {name: walk_settings(store_root, tmp_path / name) for name in ("with", "without")}
    known = {(DAYS[0] - timedelta(days=60) + timedelta(days=i)) for i in range(80)}
    open_days = {d.isoformat(): d.weekday() < 5 for d in sorted(known)}
    for s in states.values():
        s.state_dir.mkdir(parents=True)
        (s.state_dir / "market_calendar.json").write_text(
            json.dumps({"refreshed_on": None, "open_days": open_days})
        )
    calendar = calendar_store(states["with"]).calendar
    await asyncio.to_thread(
        night,
        Path(shutil.copytree(store_root, tmp_path / "night-store")),
        tmp_path / "cache",
        states["with"].state_dir / STORE_DIR,
        algorithms,
        (DIRECTION_KIND,),
        kind_algorithms=(),
        calendar=calendar,
    )
    assert len(list((states["with"].state_dir / STORE_DIR).glob("date=*"))) == len(DAYS)
    calls: list[date] = []

    def counted(data_dir: Path, day: date, *args: Any, **kwargs: Any) -> Any:
        calls.append(day)
        return training_day(data_dir, day, *args, **kwargs)

    monkeypatch.setattr("krx_quant.prevalidation.training_day", counted)
    results = {}
    for name, s in states.items():
        calls.clear()
        result = await prevalidate(
            s,
            "futures-reader",
            algorithms=algorithms,
            kind_algorithms=(),
            kinds=(DIRECTION_KIND,),
            now=FINISHED,
        )
        results[name] = ({k: v for k, v in result.as_dict().items() if k != "db"}, list(calls))

    (stored, made_with), (fresh, made_without) = results["with"], results["without"]
    assert made_with == [], "every day read from the night's store"
    assert {d for d in made_without if d not in RECORDED}, "without it, the backfilled days are made"
    assert stored == fresh
    assert stored["models"] and stored["checks"]
    assert len(set(stored["models"].values())) > 1, "retrained before each replayed day"


async def prevalidation_state(tmp_path: Path, name: str) -> Settings:
    """Ten recorded mornings (the collector's files, archived) and a calendar that knows them."""
    s = dataclasses.replace(prevalidation_settings(tmp_path / name), target_version=TARGET_VERSION)
    first = PREVALIDATED_DAYS[0] - timedelta(days=120)
    known = {
        (first + timedelta(days=i)).isoformat(): (first + timedelta(days=i)).weekday() < 5 for i in range(200)
    }
    s.state_dir.mkdir(parents=True, exist_ok=True)
    (s.state_dir / "market_calendar.json").write_text(json.dumps({"refreshed_on": None, "open_days": known}))
    await archive(s.data_dir, LocalObjectStore(tmp_path / name / "store"))
    return s


@pytest.mark.timeout(240)
async def test_pre_validation_reads_the_days_a_night_stored_and_judges_the_same(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#370. The same ten recorded days pre-validated twice: once beside a night's stored days
    (none made again), once without. The same models before each day, checks and statistics."""
    with_store, without = (
        await prevalidation_state(tmp_path, "with"),
        await prevalidation_state(tmp_path, "without"),
    )
    algorithms: list[Any] = [ALPHA, Contrarian("contrarian")]
    await asyncio.to_thread(
        train_night,
        LocalObjectStore(tmp_path / "with" / "store"),
        with_store.state_dir / "training-cache",
        symbols=with_store.symbols,
        fee_rate=with_store.fee_rate,
        now=lambda: AFTER,
        algorithms=algorithms,
        calendar=calendar_store(with_store).calendar,
        target_version=TARGET_VERSION,
        kind_algorithms=(),
        kinds=(DIRECTION_KIND,),
        samples=with_store.state_dir / STORE_DIR,
    )
    assert len(list((with_store.state_dir / STORE_DIR).glob("date=*"))) == len(PREVALIDATED_DAYS)
    calls: list[date] = []
    real = training_day

    def counted(data_dir: Path, day: date, *args: Any, **kwargs: Any) -> Any:
        calls.append(day)
        return real(data_dir, day, *args, **kwargs)

    monkeypatch.setattr("krx_quant.prevalidation.training_day", counted)

    results = {}
    for name, s in (("with", with_store), ("without", without)):
        calls.clear()
        result = await prevalidate(
            s,
            "contrarian",
            algorithms=[ALPHA, Contrarian("contrarian")],
            kind_algorithms=(),
            kinds=(DIRECTION_KIND,),
            now=AFTER,
        )
        results[name] = ({k: v for k, v in result.as_dict().items() if k not in ("db",)}, list(calls))

    (stored, made_with), (fresh, made_without) = results["with"], results["without"]
    assert made_with == [], "every day read from the night's store"
    assert len(made_without) >= len(PREVALIDATED_DAYS) - 1
    assert stored == fresh
    assert stored["models"] and stored["checks"]
