"""``krx-quant night-book-bytes``: the bytes a night of the KOSPI200 night futures' book (``H0MFASP0``)
takes, and on 2026-11-20 whether to keep recording it, by a rule set in advance (#605).

The book is about 780,000 records a night (781,668 on 2026-10-19 from 21:40, against the trades'
15,181, #264). It takes one of the session's 41 registrations and is recorded while the nightly
training runs, and no live model reads it: the recording-quality report (#570) and the night probe do.
The user chose to record it on 2026-10-18 (#264) and, on 2026-11-06, to measure it and settle it so:

- **Measure** seven complete nights (``NIGHTS``) from 2026-11-06's evening (``FIRST_EVENING``), each
  whole: the bytes of the raw layer and of the parsed one (``events``), each as the merged files hold
  them (``archive``'s ``data.parquet``, what is kept on the server and in the Object Store).
- **Judge** on ``VERDICT_DAY``: raw and parsed together, a median over ``LIMIT`` (100 MiB a night, about
  3 GiB in 30 days) and no live model reading the book stop its subscription; otherwise it stays. The
  night trades (``H0MFCNT0``) and what was recorded stay either way, and the stopping change is a PR of
  its own by the agent that reads the verdict.

**A night's bytes.** The collector files a row by the KST day it came, so the night begun on an evening
(18:00, ``contracts.NIGHT_OPEN``, to 06:00, ``contracts.NIGHT_END``) is in two days' partitions, each
shared with another night. The book delivers only at night, so a night's rows are those received from
noon on its evening to noon the next day (``ROWS_FROM``), and its bytes their share of the merged files:
each row group's compressed size by the share of its rows that are the night's, and a file's footer by
the share of the file's. A merged file's row groups are 256,000 rows in the order received, so the two
nights of a file share one row group at most.

**Complete.** A night counts once:

1. the book came all night: from 18:00 to 06:00 no stretch of ``SILENCE`` without a raw frame, counting
   18:00 to the first and the last to 06:00 (the 2026-10-19 night's longest silence was 82.8 s, #264);
2. every row of it is merged: one still in a part file waits for the archive (21:00 every evening,
   ``archive.NIGHTLY_ARCHIVE``), and the night is measured on a later run;
3. every file of it could be read.

A night is measured afresh on each run: nothing is kept but the state file (``STATE``), which holds each
night's figures and whose count of complete nights the measuring job's condition reads
(``scheduled.Facts.night_book_nights``). The collector reads that count, so this module imports no probe
(#552).

**Who reads the book.** The verdict reads the syntax tree of every module of the installed package and
lists those that name the book: ``H0MFASP0`` in a string that is not a docstring, or a name in
``BOOK_NAMES``. Those that record, check or report it are ``RECORDING``; any other may feed a live
model, and the rule then keeps the book. A module that reads it without naming it (through every feed
at once) is not seen: the agent that stops the book checks that in its PR.

**What it prints.** Bytes, rows, times, dates and module names: no price or other market value, so its
output can go on the public issue. It reads files only, beside the running collector, and writes only
its state file. A raw night the archive pruned on the server, 30 days on (#607), is read back from the
Object Store (``raw_days.RawDays``), byte for byte the merged file it was.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from time import monotonic
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

import krx_quant
from krx_quant.archive import RAW, partition_files
from krx_quant.config import Settings
from krx_quant.contracts import NIGHT_END, NIGHT_OPEN
from krx_quant.feeds import NIGHT_FUTURES, NIGHT_FUTURES_BOOK, RAW_SCHEMA
from krx_quant.feeds.base import KST
from krx_quant.market_calendar import MarketCalendar, calendar_store
from krx_quant.object_store import open_store
from krx_quant.raw_days import RawDays
from krx_quant.recorder import PART_PREFIX

log = logging.getLogger(__name__)

TITLE = "야간 호가 기록량 (#605)"
VERDICT_TITLE = "야간 호가 판정 (#605)"
FEED = NIGHT_FUTURES_BOOK
NIGHTS = 7  # the complete nights measured and judged
FIRST_EVENING = date(2026, 11, 6)  # the first night measured: the evening this was written
VERDICT_DAY = date(2026, 11, 20)
MIB = 1024 * 1024
# The threshold: a median of raw and parsed together over this a night (about 3 GiB in 30 days).
LIMIT = 100 * MIB
DAYS_KEPT = 30  # the days the threshold's month is
# A stretch without a raw frame this long in 18:00–06:00 leaves the night out: about seven times the
# longest of the 2026-10-19 night (82.8 s at 05:53, #264); a collector stopped that long missed a
# share of the night the bytes would not show.
SILENCE = timedelta(minutes=10)
ROWS_FROM = time(12, 0)  # a night's rows: received from noon on its evening to noon the next day
STATE = "night-book-605.json"
# The column the night's rows are told by, in both layers (the raw schema's and the book's events'):
# a schema without it fails here, at import.
RECEIVED = FEED.schema.field(RAW_SCHEMA.field("received_at").name).name

# The names that read the book, beside its TR ID: its feed, its schema and columns, and the night feeds
# together (``health.NIGHT_FEEDS``, ``night_probe.FEEDS`` imported under that name).
BOOK_NAMES = frozenset({"NIGHT_FUTURES_BOOK", "NIGHT_BOOK_SCHEMA", "NIGHT_BOOK_COLUMNS", "NIGHT_FEEDS"})
# The modules that name the book to record, check or report it, not to predict with it (paths under the
# package), and what they do with it.
RECORDING = {
    "feeds/market.py": "피드 정의",
    "feeds/__init__.py": "피드 목록",
    "collector.py": "구독과 녹화",
    "health.py": "결측 구간(밤 피드는 stale을 보지 않는다)",
    "night_probe.py": "야간선물 녹화 확인(#264)",
    "recording_quality.py": "기록 품질 리포트(#570)",
    "night_book.py": "이 측정(#605)",
    "scheduled/probes.py": "이 측정의 예약 작업(#605)",
}


@dataclass
class Layer:
    """One layer's files of a night: the night's share of the merged files' bytes and its rows."""

    bytes: float = 0.0
    rows: int = 0  # in merged files
    unmerged: int = 0  # in part files the archive has not merged yet
    unread: list[str] = field(default_factory=list)  # files that could not be read, and why


@dataclass(frozen=True)
class NightBytes:
    """A night's measure."""

    evening: date
    raw: Layer
    parsed: Layer
    frames: int  # raw frames received from 18:00 to 06:00
    silence: timedelta  # the longest stretch without one there (the whole night without any)
    silence_from: datetime  # where it began

    @property
    def total(self) -> float:
        return self.raw.bytes + self.parsed.bytes

    @property
    def why_not(self) -> list[str]:
        """Why the night does not count, or not yet; empty once it is complete."""
        why = []
        unread = self.raw.unread + self.parsed.unread
        if unread:
            why.append(f"읽지 못한 파일 {len(unread)}개")
        if not self.frames:
            why.append("18:00–06:00 프레임 없음(KRX가 쉰 밤이거나 받지 못했다)")
        elif self.silence >= SILENCE:
            why.append(
                f"{_when(self.silence_from, self.evening)}부터 {_duration(self.silence)} 동안 프레임 없음"
                f"(기준 {_duration(SILENCE)})"
            )
        if self.raw.unmerged or self.parsed.unmerged:
            why.append(
                f"아직 합치지 않음(part 파일에 raw {self.raw.unmerged:,}행,"
                f" events {self.parsed.unmerged:,}행)"
            )
        return why

    @property
    def complete(self) -> bool:
        return not self.why_not


def _between(values: Any, start: datetime, end: datetime) -> Any:
    low = pc.greater_equal(values, pa.scalar(start, type=values.type))
    return pc.fill_null(pc.and_(low, pc.less(values, pa.scalar(end, type=values.type))), False)


def _count(mask: Any) -> int:
    return int(pc.sum(pc.cast(mask, pa.int64())).as_py() or 0)


@dataclass
class _File:
    bytes: float = 0.0
    rows: int = 0
    received: list[np.ndarray] = field(default_factory=list)  # in the coverage window, microseconds


def _read_file(path: Path, rows: tuple[datetime, datetime], cover: tuple[datetime, datetime] | None) -> _File:
    """A file's rows in ``rows`` and their share of its bytes; the times of those in ``cover``."""
    seen = _File()
    with path.open("rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        parquet = pq.ParquetFile(handle)
        schema = parquet.schema_arrow
        if RECEIVED not in schema.names or not pa.types.is_timestamp(schema.field(RECEIVED).type):
            raise ValueError(f"{RECEIVED} 열이 없거나 시각이 아니다")
        meta = parquet.metadata
        groups = [meta.row_group(i) for i in range(meta.num_row_groups)]
        sizes = [sum(g.column(c).total_compressed_size for c in range(g.num_columns)) for g in groups]
        for i, (group, group_size) in enumerate(zip(groups, sizes, strict=True)):
            if not group.num_rows:
                continue
            received = parquet.read_row_group(i, columns=[RECEIVED]).column(0)
            inside = _count(_between(received, *rows))
            seen.bytes += group_size * inside / group.num_rows
            seen.rows += inside
            if cover is not None:
                covered = pc.filter(received, _between(received, *cover))
                seen.received += [c.to_numpy() for c in pc.cast(covered, pa.int64()).chunks]
        if meta.num_rows:  # the footer and the file's marks, by the file's rows
            seen.bytes += max(size - sum(sizes), 0) * seen.rows / meta.num_rows
    return seen


def _layer(
    data_dir: Path,
    layer: str,
    evening: date,
    rows: tuple[datetime, datetime],
    cover: tuple[datetime, datetime] | None,
    received: list[np.ndarray],
    raw: RawDays | None = None,
) -> Layer:
    """The night's rows of ``layer`` in its two days' partitions: in merged files, their bytes; in part
    files, their count. The times in ``cover`` go to ``received`` from either. ``raw`` gives the raw
    layer's partitions (a pruned one read back from the store)."""
    seen = Layer()
    for day in (evening, evening + timedelta(days=1)):
        if raw is not None and layer == RAW:
            root = raw.feed(day, FEED.tr_id)
        else:
            root = data_dir / layer / f"date={day.isoformat()}" / f"feed={FEED.tr_id}"
        base = root.parents[2]  # the data directory, or the copy's: ``<base>/<layer>/date=…/feed=…``
        for partition in sorted(root.glob("symbol=*")):
            try:
                files = partition_files(partition)
            except (OSError, ValueError, pa.ArrowInvalid) as e:
                seen.unread.append(f"{partition.relative_to(base)}: {e}")
                continue
            for path in files:
                try:
                    found = _read_file(path, rows, cover)
                except (OSError, ValueError, pa.ArrowInvalid) as e:
                    seen.unread.append(f"{path.relative_to(base)}: {e}")
                    continue
                if path.name.startswith(PART_PREFIX):
                    seen.unmerged += found.rows
                else:
                    seen.bytes += found.bytes
                    seen.rows += found.rows
                received += found.received
    return seen


def night_rows(evening: date) -> tuple[datetime, datetime]:
    """The times a night's rows were received in: noon on its evening to noon the next day."""
    start = datetime.combine(evening, ROWS_FROM, KST)
    return start, start + timedelta(days=1)


def night_session(evening: date) -> tuple[datetime, datetime]:
    """The night session begun on ``evening``: 18:00 to 06:00 the next morning."""
    return (
        datetime.combine(evening, NIGHT_OPEN, KST),
        datetime.combine(evening + timedelta(days=1), time(0), KST) + NIGHT_END,
    )


def measure_night(data_dir: Path, evening: date, raw_days: RawDays | None = None) -> NightBytes:
    """The bytes and rows of the book's night begun on ``evening``, and its longest silence. ``raw_days``
    reads the raw layer (by default the data directory's, a pruned night failing)."""
    rows, session = night_rows(evening), night_session(evening)
    received: list[np.ndarray] = []
    raw = _layer(data_dir, "raw", evening, rows, session, received, raw_days or RawDays(data_dir))
    parsed = _layer(data_dir, "events", evening, rows, None, [])
    start, end = (int(at.timestamp() * 1_000_000) for at in session)
    times = np.sort(np.concatenate(received)) if received else np.empty(0, dtype=np.int64)
    edges = np.concatenate(([start], times, [end]))
    steps = np.diff(edges)
    longest = int(np.argmax(steps))
    since = datetime.fromtimestamp(int(edges[longest]) / 1_000_000, KST)
    return NightBytes(evening, raw, parsed, len(times), timedelta(microseconds=int(steps[longest])), since)


def evenings(calendar: MarketCalendar, now: datetime) -> list[date]:
    """The trading days from ``FIRST_EVENING`` whose night's rows are all in by ``now`` (noon the next
    day), oldest first: the evenings of the nights to measure."""
    out, day = [], FIRST_EVENING
    while night_rows(day)[1] <= now:
        if calendar.is_trading_day(day):
            out.append(day)
        day += timedelta(days=1)
    return out


def measure(
    data_dir: Path, calendar: MarketCalendar, now: datetime, raw: RawDays | None = None
) -> list[NightBytes]:
    """Each night from ``FIRST_EVENING`` over by ``now``, up to the one that makes ``NIGHTS`` complete."""
    nights: list[NightBytes] = []
    for evening in evenings(calendar, now):
        nights.append(measure_night(data_dir, evening, raw))
        if len(counted(nights)) == NIGHTS:
            break
    return nights


def counted(nights: Sequence[NightBytes]) -> list[NightBytes]:
    """The nights the rule counts: the first ``NIGHTS`` complete ones."""
    return [n for n in nights if n.complete][:NIGHTS]


def median(values: Sequence[float]) -> float | None:
    return statistics.median(values) if values else None


# The state file.


def save(state_dir: Path, nights: Sequence[NightBytes], now: datetime) -> None:
    """Each night's figures, and which count, written whole."""
    data = {
        "measured_at": now.astimezone(KST).isoformat(),
        "first_evening": FIRST_EVENING.isoformat(),
        "nights": [
            {
                "evening": n.evening.isoformat(),
                "complete": n.complete,
                "raw_bytes": round(n.raw.bytes),
                "parsed_bytes": round(n.parsed.bytes),
                "why_not": n.why_not,
            }
            for n in nights
        ],
    }
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / STATE
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.replace(path)


def complete_nights(state_dir: Path) -> int:
    """How many complete nights the last measure found (``scheduled.Facts.night_book_nights``): 0 without
    a state file or with one that cannot be read, so that the measure runs and writes it again."""
    try:
        data = json.loads((state_dir / STATE).read_text())
        return sum(1 for n in data["nights"] if n["complete"] is True)
    except (OSError, ValueError, KeyError, TypeError) as e:
        if not isinstance(e, FileNotFoundError):
            log.warning("cannot read %s: %s", state_dir / STATE, e)
        return 0


# Who names the book.


def _docstrings(tree: ast.AST) -> set[int]:
    """The string statements' constants (docstrings, and the strings that document an assignment)."""
    return {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }


def names_book(source: str) -> bool:
    """Whether a module's source names the book: its TR ID in a string other than a docstring, or one of
    ``BOOK_NAMES`` as a name, an attribute or an import."""
    tree = ast.parse(source)
    docs = _docstrings(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if FEED.tr_id in node.value and id(node) not in docs:
                return True
        elif isinstance(node, ast.Name) and node.id in BOOK_NAMES:
            return True
        elif isinstance(node, ast.Attribute) and node.attr in BOOK_NAMES:
            return True
        elif isinstance(node, ast.alias) and {node.name.rsplit(".", 1)[-1], node.asname} & BOOK_NAMES:
            return True
    return False


@dataclass(frozen=True)
class Readers:
    """The package's modules that name the book (paths under it), and those that could not be read."""

    modules: tuple[str, ...]
    unread: tuple[str, ...]
    files: int  # the modules read

    @property
    def others(self) -> list[str]:
        """Those outside ``RECORDING``: a live model's input, maybe."""
        return [m for m in self.modules if m not in RECORDING]


def readers(package: Path | None = None) -> Readers:
    """The modules of ``package`` (the installed ``krx_quant``) that name the book."""
    root = Path(krx_quant.__file__).parent if package is None else package
    modules, unread, files = [], [], 0
    for path in sorted(root.rglob("*.py")):
        name = path.relative_to(root).as_posix()
        try:
            source = path.read_text(encoding="utf-8")
            named = names_book(source)
        except (OSError, UnicodeDecodeError, SyntaxError, ValueError) as e:
            unread.append(f"{name}: {e}")
            continue
        files += 1
        if named:
            modules.append(name)
    return Readers(tuple(modules), tuple(unread), files)


# The report.


def _mib(value: float) -> str:
    return f"{value / MIB:,.1f} MiB"


def _duration(span: timedelta) -> str:
    seconds = span.total_seconds()
    if seconds < 60:
        return f"{seconds:.1f}초"
    minutes, second = divmod(round(seconds), 60)
    hours, minute = divmod(minutes, 60)
    return (f"{hours}시간 " if hours else "") + f"{minute}분" + (f" {second}초" if second else "")


def _when(at: datetime, evening: date) -> str:
    local = at.astimezone(KST)
    return local.strftime("%H:%M:%S" if local.date() == evening else "%m-%d %H:%M:%S")


WEEKDAYS = "월화수목금토일"


def night_line(night: NightBytes) -> str:
    why = night.why_not
    state = "완전" if not why else "세지 않음: " + "; ".join(why)
    return (
        f"{night.evening}({WEEKDAYS[night.evening.weekday()]}) {state} · raw {_mib(night.raw.bytes)} · "
        f"parsed {_mib(night.parsed.bytes)} · 합 {_mib(night.total)} · raw 행 {night.raw.rows:,} · "
        f"events 행 {night.parsed.rows:,} · 18:00–06:00 프레임 {night.frames:,} · 가장 긴 침묵 "
        f"{_duration(night.silence)}({_when(night.silence_from, night.evening)}부터)"
    )


def summary(nights: Sequence[NightBytes]) -> list[str]:
    """The counted nights' medians against the threshold."""
    taken = counted(nights)
    if not taken:
        return [f"완전한 밤 0/{NIGHTS}: 중앙값 없음"]
    total = median([n.total for n in taken])
    assert total is not None
    raw, parsed = median([n.raw.bytes for n in taken]), median([n.parsed.bytes for n in taken])
    assert raw is not None and parsed is not None
    side = "넘는다" if total > LIMIT else "넘지 않는다"
    return [
        f"완전한 밤 {len(taken)}/{NIGHTS}({', '.join(str(n.evening) for n in taken)})의 중앙값:"
        f" raw {_mib(raw)} · parsed {_mib(parsed)} · 합 {_mib(total)}(기준 {_mib(LIMIT)}를 {side})"
        f" · {DAYS_KEPT}일이면 {total * DAYS_KEPT / (1024 * MIB):.2f} GiB"
    ]


NOTES = (
    "읽은 것: raw와 events의 H0MFASP0 파티션(밤이 시작한 날과 다음 날). 바이트는 합친 파일(data.parquet)의"
    " row group마다 압축 크기에 그 밤의 행(그 저녁 12:00부터 다음 날 12:00까지 받은 것)의 비율을 곱한 것이다."
    " 시세 값은 없다(바이트, 행 수, 시각, 모듈 이름만).",
    f"완전한 밤: 18:00–06:00에 raw 프레임 없이 {_duration(SILENCE)} 넘게 지난 적이 없고(18:00부터 첫 프레임,"
    " 마지막 프레임부터 06:00까지 포함), 그 밤의 행이 모두 합쳐졌고, 파일을 다 읽은 밤. 아직 합치지 않은 밤은"
    " 다음 실행이 다시 잰다.",
    f"규칙: {VERDICT_DAY}에 완전한 밤 {NIGHTS}개의 raw+parsed 합 중앙값이 {_mib(LIMIT)}를 넘고 이 호가를 읽는"
    " 실전 모델이 없으면 H0MFASP0 구독을 멈춘다(PR은 따로). 그렇지 않으면 계속 기록한다."
    " 야간 체결(H0MFCNT0)과 지난 기록은 어느 경우든 그대로 둔다.",
)

Section = tuple[str, list[str]]


def measure_sections(nights: Sequence[NightBytes]) -> list[Section]:
    """A line a night, then the counted nights' medians."""
    each = [night_line(n) for n in nights] or [f"없음({FIRST_EVENING} 밤부터 잰다)"]
    each += [f"{n.evening} 읽지 못한 파일: {x}" for n in nights for x in n.raw.unread + n.parsed.unread]
    return [("1. 밤마다 (저녁 날짜, MiB = 2^20 바이트)", each), ("2. 합", summary(nights))]


def render(head: Sequence[str], sections: Sequence[Section], seconds: float | None) -> str:
    out = [*head, *NOTES, ""]
    if seconds is not None:
        sections = [*sections, (f"{len(sections) + 1}. 실행", [f"{seconds:.1f}초"])]
    for heading, lines in sections:
        out += [heading, *(f"  {line}" for line in lines), ""]
    return "\n".join(out).rstrip("\n")


def report(nights: Sequence[NightBytes], seconds: float | None = None) -> str:
    """The measure."""
    span = f"{nights[0].evening} – {nights[-1].evening}의 밤 {len(nights)}개" if nights else "끝난 밤 없음"
    sections = measure_sections(nights)
    if len(counted(nights)) == NIGHTS:
        sections[-1][1].append(
            f"완전한 밤 {NIGHTS}개를 다 쟀다: 측정 작업은 더 돌지 않는다. 판정은 {VERDICT_DAY} 밤이다."
        )
    return render([f"{TITLE}: {span}, 완전한 밤 {len(counted(nights))}/{NIGHTS}"], sections, seconds)


@dataclass(frozen=True)
class Verdict:
    stop: bool
    why: str


def judge(nights: Sequence[NightBytes], found: Readers) -> Verdict:
    """The rule (#605): a median over ``LIMIT`` of ``NIGHTS`` complete nights and no live model naming the
    book stop it; anything else keeps it."""
    taken = counted(nights)
    if len(taken) < NIGHTS:
        return Verdict(False, f"완전한 밤이 {len(taken)}개뿐이라(기준 {NIGHTS}개) 규칙의 중앙값이 없다")
    total = median([n.total for n in taken])
    assert total is not None
    if total <= LIMIT:
        return Verdict(False, f"합 중앙값 {_mib(total)}가 기준 {_mib(LIMIT)}를 넘지 않는다")
    if found.unread or not found.files:
        return Verdict(
            False,
            f"합 중앙값 {_mib(total)}가 기준을 넘지만, 코드를 다 읽지 못해 이 호가를 읽는 실전 모델이 없는지"
            " 모른다",
        )
    if found.others:
        return Verdict(
            False,
            f"합 중앙값 {_mib(total)}가 기준을 넘지만, 기록·리포트 밖의 모듈이 이 호가를 부른다"
            f"({', '.join(found.others)})",
        )
    return Verdict(
        True, f"합 중앙값 {_mib(total)}가 기준 {_mib(LIMIT)}를 넘고, 이 호가를 읽는 실전 모델이 없다"
    )


def verdict_report(
    nights: Sequence[NightBytes], found: Readers, day: date, seconds: float | None = None
) -> str:
    """The verdict: the conclusion and what follows, the measure, and the modules that name the book."""
    verdict = judge(nights, found)
    if verdict.stop:
        conclusion = f"{FEED.tr_id} 구독을 멈춘다({verdict.why})"
        then = (
            f"할 일(에이전트, PR 하나): 수집기의 구독 계획(collector.subscription_plan)에서"
            f" {FEED.tr_id}를 빼고, 기록 품질 리포트(#570)가 기대하는 야간 피드(recording_quality 5절이 읽는"
            ' night_probe.FEEDS)와 health.NIGHT_FEEDS, 문서(kis-feeds.md의 구독 표와 "야간선물 (#264)",'
            f" events.md, operations.md)를 맞춘다. 야간 체결({NIGHT_FUTURES.tr_id})과 지난 기록은 그대로"
            " 둔다. 그 PR이 이 이슈를 닫는다."
        )
    else:
        conclusion = f"계속 기록한다({verdict.why})"
        then = (
            f"할 일: 없음. {FEED.tr_id}를 그대로 기록한다. 에이전트가 이 결론을 이슈에 적고 닫는다"
            "(완전한 밤이 모자랐으면 더 잴지 사용자에게 묻는다)."
        )
    named = [f"{m}: {RECORDING[m]}" for m in found.modules if m in RECORDING]
    named.append("기록·리포트 밖(실전 모델의 입력일 수 있다): " + (", ".join(found.others) or "없음"))
    named += [f"읽지 못한 모듈: {x}" for x in found.unread]
    heading = (
        f"3. 이 호가를 이름으로 부르는 모듈(설치된 krx_quant의 모듈 {found.files}개의 구문 트리:"
        f" docstring 밖의 {FEED.tr_id} 문자열, {', '.join(sorted(BOOK_NAMES))})"
    )
    head = [f"{VERDICT_TITLE}: {day}", f"결론: {conclusion}", then]
    return render(head, [*measure_sections(nights), (heading, named)], seconds)


def night_book_bytes(settings: Settings, now: datetime, *, verdict: bool = False) -> str:
    """The measure (or, with ``verdict``, the rule applied to it); the state file written."""
    started = monotonic()
    calendar = calendar_store(settings).calendar
    with RawDays(settings.data_dir, open_store(settings)) as raw:
        nights = measure(settings.data_dir, calendar, now, raw)
    save(settings.state_dir, nights, now)
    if not verdict:
        return report(nights, monotonic() - started)
    found = readers()
    return verdict_report(nights, found, now.astimezone(KST).date(), monotonic() - started)
