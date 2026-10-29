"""``krx-quant probe-member-frames``: the member feed's frames whose field count is not 78 (#381).

Around 08:00 (NXT's pre-market opens) a few integrated member frames (``H0UNMBC0``) could not be
normalized: ``RealtimeFrame.records`` found fewer fields than the 78 of ``MEMBER_COLUMNS``
(2026-10-23 08:00:01: 72; 2026-10-26 08:00:00: 67 and 08:00:02: 61), and the collector kept them
raw only (``could not normalize H0UNMBC0 frame; kept raw only``, an ERROR that alerts; a counted
WARNING since #419, as this probe found no rule to read them by). Whether
those frames follow a rule (a block of fields left out) or are cut short only the raw frames show.
This probe reads them, over every recorded day:

1. a line a day: the member frames read, and those whose field count is not their record count
   × 78 (by field count and hour);
2. a line an hour (KST), every day together: the frames, those not matching, and on which days;
3. each such frame (the first ``FRAMES_SHOWN``, in receipt order): its symbol, receipt time,
   record and field counts, its place among the day's frames of the symbol and the one before it;
   the shape of each field (``shape``), and the same line for the symbol's next 78-field frame,
   with how many fields agree in shape from the start and from the end, and where a single
   missing block would fit;
4. around those frames (their minute, from the minute before to the minute after): every
   WebSocket feed's frames by receipt time, and those the collector could not split into its
   feed's columns; the control frames (``_system``) and the unparsed ones (``_unparsed``, each
   with its time and field count: a frame split in two would leave its tail there).

**What is read.** ``raw``'s columns by the names of ``RAW_SCHEMA``, as the collector writes every
WebSocket message before it parses it (``Collector._handle``): ``received_at`` (UTC) and
``message``, the text as received (``<encrypted>|<tr_id>|<count>|<fields joined by ^>``,
``kis.parse_message``), under the partition of the frame's feed and first field. A file without
one of the columns, or with one of another type, is counted and not read; so is a row with an
empty one.

It prints counts, times, symbols, field counts and shapes, never a field's value, so its output
can go on the public issue (the scheduled job ``probe-member-frames-381``). A partition key that
is not a configured symbol is shown by its shape too. It reads files only, beside the running
collector.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from krx_quant import kis
from krx_quant.archive import partition_files
from krx_quant.config import Settings
from krx_quant.feeds import MEMBER, RAW_SCHEMA, WS_FEEDS
from krx_quant.feeds.base import KST
from krx_quant.gaps_probe import SYSTEM, UNPARSED, WS_RAW

TITLE = "회원사 프레임의 필드 수 (#381)"
WIDTH = len(MEMBER.columns)  # a record's fields: 78
FRAMES_SHOWN = 40  # frames shown in section 3, the earliest; the rest are counted
WINDOWS_SHOWN = 10  # windows read in section 4, the earliest
UNPARSED_SHOWN = 10  # unparsed frames listed in a window, the earliest
AROUND = timedelta(minutes=1)  # a window: from this before a frame's minute to this after it

# A field's shape: never its value.
EMPTY, DIGITS, NUMBER, YES_NO, TEXT = "_", "9", "+", "Y", "A"
_DIGITS = re.compile(r"[0-9]+")
_NUMBER = re.compile(r"[+-]?([0-9]+\.?[0-9]*|\.[0-9]+)")
_STEM = re.compile(r"_?[0-9]+$")

# The columns read, by the names of the schema the collector writes raw rows in: a schema without
# one fails here, at import, not in a night's run.
_RECEIVED = RAW_SCHEMA.field("received_at").name
_MESSAGE = RAW_SCHEMA.field("message").name


def shape(value: str) -> str:
    """``value``'s shape (``EMPTY``, ``DIGITS``, ``NUMBER`` signed or with a decimal point,
    ``YES_NO``, else ``TEXT``), surrounding spaces aside."""
    text = value.strip()
    if not text:
        return EMPTY
    if text in ("Y", "N"):
        return YES_NO
    if _DIGITS.fullmatch(text):
        return DIGITS
    if _NUMBER.fullmatch(text):
        return NUMBER
    return TEXT


def shapes(fields: Sequence[str]) -> str:
    return "".join(shape(v) for v in fields)


def groups_of(columns: Sequence[str]) -> tuple[tuple[str, ...], ...]:
    """``columns`` in runs of one stem (the name without its trailing number: a side's five
    members), consecutive single columns together (the foreign totals)."""
    runs: list[list[str]] = []
    for name in columns:
        if runs and _STEM.sub("", runs[-1][-1]) == _STEM.sub("", name):
            runs[-1].append(name)
        else:
            runs.append([name])
    out: list[list[str]] = []
    single = False  # the last group is of single columns
    for run in runs:
        if len(run) == 1 and single:
            out[-1] += run
        else:
            out.append(run)
            single = len(run) == 1
    return tuple(tuple(g) for g in out)


GROUPS = groups_of(MEMBER.columns)


def _group_name(group: Sequence[str]) -> str:
    """``SELN2_MBCR_NAME1–5`` for a run of one stem; the first and last name of single columns."""
    if len(group) == 1:
        return group[0]
    number = _STEM.search(group[-1])
    if number is not None and len({_STEM.sub("", name) for name in group}) == 1:
        return f"{group[0]}–{number.group().lstrip('_')}"
    return f"{group[0]}…{group[-1]}({len(group)})"


def grouped(codes: str) -> str:
    """``codes`` (a frame's shapes) spaced by ``GROUPS``; a record past the first after ``|``."""
    records = []
    for start in range(0, max(len(codes), 1), WIDTH):
        record, at, words = codes[start : start + WIDTH], 0, []
        for group in GROUPS:
            if at >= len(record):
                break
            words.append(record[at : at + len(group)])
            at += len(group)
        records.append(" ".join(words))
    return " | ".join(records)


def agreement(short: str, full: str) -> tuple[int, int]:
    """How many of ``short``'s leading and trailing shapes are ``full``'s, field by field."""
    most = min(len(short), len(full))
    lead = 0
    while lead < most and short[lead] == full[lead]:
        lead += 1
    trail = 0
    while trail < most and short[-1 - trail] == full[-1 - trail]:
        trail += 1
    return lead, trail


def block_starts(fields: int, lead: int, trail: int) -> range:
    """Where a single block of ``WIDTH - fields`` missing fields could start (0-based, in the
    full layout), given the leading and trailing agreement: the fields before it agree with the
    full frame's, those from it with the full frame's after the block."""
    return range(max(0, fields - trail), min(lead, fields) + 1)


@dataclass(frozen=True)
class Frame:
    """A data frame's receipt and fields; the fields are never printed, their shapes are."""

    key: str  # the raw partition's symbol: the frame's first field
    received: datetime
    records: int
    fields: tuple[str, ...]


@dataclass
class Odd:
    """A member frame whose field count is not its record count × ``WIDTH``."""

    frame: Frame
    ordinal: int  # its place among the day's frames of the key, from 1
    before: datetime | None  # the key's frame before it that day; None: none
    after: Frame | None = None  # the key's next one-record frame of WIDTH fields that day


@dataclass
class Unread:
    """Files unlike the schema the collector writes them in, and rows with an empty column:
    counted, never guessed at. A file read twice (its day, then a window of it) counts once."""

    files: dict[str, set[Path]] = field(default_factory=dict)  # what -> the files
    rows: dict[str, dict[Path, int]] = field(default_factory=dict)  # what -> file -> most rows a read found

    def file(self, what: str, path: Path) -> None:
        self.files.setdefault(what, set()).add(path)

    def row(self, what: str, path: Path, count: int) -> None:
        seen = self.rows.setdefault(what, {})
        seen[path] = max(seen.get(path, 0), count)

    def lines(self) -> list[str]:
        return [f"{what}: 파일 {len(paths)}개" for what, paths in sorted(self.files.items())] + [
            f"{what}: 행 {sum(counts.values())}개" for what, counts in sorted(self.rows.items())
        ]


def _readable(path: Path, unread: Unread, label: str) -> bool:
    schema = pq.read_schema(path)
    for column in (_RECEIVED, _MESSAGE):
        if column not in schema.names:
            unread.file(f"{label} {column} 없음(읽지 않음)", path)
            return False
    received, message = schema.field(_RECEIVED).type, schema.field(_MESSAGE).type
    if not pa.types.is_timestamp(received):
        unread.file(f"{label} {_RECEIVED} 타입 {received}(읽지 않음)", path)
        return False
    if not (pa.types.is_string(message) or pa.types.is_large_string(message)):
        unread.file(f"{label} {_MESSAGE} 타입 {message}(읽지 않음)", path)
        return False
    return True


def _between(values: Any, start: datetime, end: datetime) -> Any:
    """A mask of ``values`` (a timestamp column) at or after ``start`` and before ``end``."""
    return pc.and_(
        pc.greater_equal(values, pa.scalar(start, type=values.type)),
        pc.less(values, pa.scalar(end, type=values.type)),
    )


def rows(
    partition: Path, unread: Unread, label: str, window: tuple[datetime, datetime] | None = None
) -> Iterator[tuple[datetime, str]]:
    """A raw partition's (receipt, text) rows, each once (the merged file, then the parts it does
    not hold), in the files' order; with ``window``, those received in it only (a file with none
    there is not read past its times)."""
    for path in partition_files(partition):
        empty = 0
        try:
            if not _readable(path, unread, label):
                continue
            if window is not None:
                times = pq.read_table(path, columns=[_RECEIVED]).column(0)
                if not pc.any(_between(times, *window)).as_py():
                    continue
            for batch in pq.ParquetFile(path).iter_batches(columns=[_RECEIVED, _MESSAGE]):
                if window is not None:
                    batch = batch.filter(_between(batch.column(0), *window))
                for received, text in zip(
                    batch.column(0).to_pylist(), batch.column(1).to_pylist(), strict=True
                ):
                    if received is None or text is None:
                        empty += 1
                        continue
                    yield received, text
        except (FileNotFoundError, pa.ArrowInvalid, OSError):
            continue  # merged away in between, or written as it was read: the rest still counts
        finally:
            if empty:
                unread.row(f"{label} 빈 값(그 행을 읽지 않음)", path, empty)


def _raw(data_dir: Path, day: date, feed: str) -> Path:
    return data_dir / "raw" / f"date={day.isoformat()}" / f"feed={feed}"


def recorded_days(data_dir: Path, now: datetime) -> list[date]:
    """Every day up to ``now`` with the member feed's raw frames, oldest first."""
    days = []
    for path in (data_dir / "raw").glob("date=*"):
        try:
            day = date.fromisoformat(path.name.removeprefix("date="))
        except ValueError:
            continue
        if (path / f"feed={MEMBER.tr_id}").is_dir():
            days.append(day)
    today = now.astimezone(KST).date()
    return sorted(d for d in days if d <= today)


@dataclass
class MemberDay:
    """One day's member frames."""

    day: date
    frames: int = 0  # data frames read
    hours: Counter[int] = field(default_factory=Counter)  # data frames by KST hour
    odd: list[Odd] = field(default_factory=list)
    encrypted: int = 0  # not normalized by the collector either
    unreadable: int = 0  # rows that are not a data frame


def read_member_day(data_dir: Path, day: date, unread: Unread) -> MemberDay:
    """``day``'s member frames, each key's in receipt order (rows received at the same instant in
    the order written, which is the order received)."""
    out = MemberDay(day)
    for partition in sorted(_raw(data_dir, day, MEMBER.tr_id).glob("symbol=*")):
        key = partition.name.removeprefix("symbol=")
        ordinal, before = 0, None
        waiting: list[Odd] = []  # odd frames without their next full frame yet
        for received, text in sorted(rows(partition, unread, f"raw {MEMBER.tr_id}"), key=lambda r: r[0]):
            try:
                message = kis.parse_message(text)
            except kis.KisError:
                out.unreadable += 1
                continue
            if not isinstance(message, kis.RealtimeFrame):
                out.unreadable += 1
                continue
            out.frames += 1
            out.hours[received.astimezone(KST).hour] += 1
            ordinal += 1
            if message.encrypted:
                out.encrypted += 1
            else:
                fields = tuple(message.payload.split("^"))
                frame = Frame(key, received, message.count, fields)
                if len(fields) != message.count * WIDTH:
                    odd = Odd(frame, ordinal, before)
                    out.odd.append(odd)
                    waiting.append(odd)
                elif message.count == 1 and waiting:
                    for odd in waiting:
                        odd.after = frame
                    waiting = []
            before = received
    out.odd.sort(key=lambda o: o.frame.received)
    return out


@dataclass
class Seen:
    """One feed's raw frames in a window."""

    frames: int = 0
    odd: Counter[int] = field(default_factory=Counter)  # field counts not the records × columns
    encrypted: int = 0
    unreadable: int = 0
    unparsed: list[tuple[datetime, int, int]] = field(default_factory=list)  # time, fields, '|'s


def windows(odd: Sequence[Odd]) -> list[tuple[datetime, datetime]]:
    """Each frame's minute from ``AROUND`` before to ``AROUND`` after it (KST), overlapping ones
    merged, in order."""
    spans = []
    for o in odd:
        minute = o.frame.received.astimezone(KST).replace(second=0, microsecond=0)
        spans.append((minute - AROUND, minute + timedelta(minutes=1) + AROUND))
    merged: list[tuple[datetime, datetime]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def read_window(data_dir: Path, start: datetime, end: datetime, unread: Unread) -> dict[str, Seen]:
    """Every WebSocket feed's raw frames received from ``start`` to before ``end``, by feed (a
    feed recorded on the day without one there has nothing seen)."""
    out: dict[str, Seen] = {}
    first, last = start.astimezone(KST).date(), (end - timedelta(microseconds=1)).astimezone(KST).date()
    days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    for day in days:
        for feed in WS_RAW:
            root = _raw(data_dir, day, feed)
            if not root.is_dir():
                continue
            seen = out.setdefault(feed, Seen())
            for partition in sorted(root.glob("symbol=*")):
                for received, text in rows(partition, unread, f"raw {feed}", (start, end)):
                    _see(seen, feed, received, text)
    for seen in out.values():
        seen.unparsed.sort()
    return out


def _see(seen: Seen, feed: str, received: datetime, text: str) -> None:
    seen.frames += 1
    if feed == UNPARSED:
        seen.unparsed.append((received, text.count("^") + 1, text.count("|")))
        return
    if feed == SYSTEM:
        return
    try:
        message = kis.parse_message(text)
    except kis.KisError:
        seen.unreadable += 1
        return
    if not isinstance(message, kis.RealtimeFrame):
        seen.unreadable += 1
        return
    if message.encrypted:
        seen.encrypted += 1
        return
    fields = message.payload.count("^") + 1
    if fields != message.count * len(WS_FEEDS[feed].columns):
        seen.odd[fields] += 1


def _time(at: datetime) -> str:
    return at.astimezone(KST).strftime("%H:%M:%S.%f")[:-3]


def _when(at: datetime) -> str:
    return f"{at.astimezone(KST).date()} {_time(at)}"


def _later(span: timedelta) -> str:
    seconds = span.total_seconds()
    if seconds < 60:
        return f"{seconds:.1f}초 뒤"
    hours, rest = divmod(round(seconds), 3600)
    minutes, left = divmod(rest, 60)
    return f"{hours}시간 {minutes}분 뒤" if hours else f"{minutes}분 {left}초 뒤"


def _counts(counter: Counter[int]) -> str:
    return ", ".join(f"{n} ×{k}" for n, k in sorted(counter.items()))


def _key(key: str, symbols: Sequence[str]) -> str:
    """A partition's key as it is if it is a configured symbol, else by its shape (it is the
    frame's first field, which could be anything)."""
    return key if key in symbols else f"설정에 없는 키(모양 {shapes([key])})"


def _column(at: int) -> str:
    """The full layout's field ``at`` (0-based), 1-based with its column."""
    return f"{at + 1}({MEMBER.columns[at]})" if at < WIDTH else f"{at + 1}(끝 뒤)"


def odd_lines(odd: Odd, symbols: Sequence[str]) -> list[str]:
    """Section 3's lines for one frame."""
    frame = odd.frame
    n, expected = len(frame.fields), frame.records * WIDTH
    diff = f"{expected}보다 {expected - n} 적다" if n < expected else f"{expected}보다 {n - expected} 많다"
    before = "앞 프레임 없음" if odd.before is None else f"앞 프레임 {_time(odd.before)}"
    own = shapes(frame.fields)
    lines = [
        f"{_when(frame.received)} {_key(frame.key, symbols)} · 레코드 {frame.records} · 필드 {n}({diff}) · "
        f"그날 이 종목의 {odd.ordinal:,}번째 프레임({before})",
        f"  이 프레임: {grouped(own)}",
    ]
    after = odd.after
    if after is None:
        lines.append(f"  다음 {WIDTH}칸 프레임: 그날 없음")
        return lines
    full = shapes(after.fields)
    when = f"{_time(after.received)}, {_later(after.received - frame.received)}"
    lines.append(f"  다음 {WIDTH}칸: {grouped(full)} ({when})")
    lead, trail = agreement(own, full)
    line = f"  앞에서 같은 모양 {lead}칸, 뒤에서 {trail}칸"
    if frame.records == 1 and n < WIDTH:
        starts = block_starts(n, lead, trail)
        missing = WIDTH - n
        if not starts:
            line += f" · 빠진 {missing}칸이 한 덩어리로는 맞지 않는다"
        elif starts.start == n:
            line += f" · 끝의 {missing}칸이 빠진 모양이다(시작 칸 {_column(n)})"
        else:
            span = _column(starts[0]) if len(starts) == 1 else f"{_column(starts[0])}–{_column(starts[-1])}"
            line += f" · 빠진 {missing}칸이 한 덩어리라면 시작 칸 후보 {span}"
    lines.append(line)
    return lines


def window_lines(start: datetime, end: datetime, seen: dict[str, Seen], odd: Sequence[Odd]) -> list[str]:
    """Section 4's lines for one window."""
    inside = [o for o in odd if start <= o.frame.received < end]
    times = ", ".join(_time(o.frame.received) for o in inside)
    head = (
        f"{start.astimezone(KST).date()} {start.astimezone(KST):%H:%M}–{end.astimezone(KST):%H:%M}"
        f"(3절의 {times})"
    )
    lines = [head]
    quiet = []
    for feed, s in seen.items():
        if not s.frames:
            quiet.append(feed)
            continue
        line = f"  {feed} 프레임 {s.frames:,}개"
        if feed == UNPARSED:
            shown = [
                f"{_time(at)}(필드 {fields}, '|' {bars}개)"
                for at, fields, bars in s.unparsed[:UNPARSED_SHOWN]
            ]
            rest = len(s.unparsed) - len(shown)
            line += ": " + ", ".join(shown) + (f" 외 {rest}개" if rest else "")
        elif feed != SYSTEM:
            line += " · 필드 수가 맞지 않는 것 " + (
                f"{sum(s.odd.values())}개({_counts(s.odd)})" if s.odd else "없음"
            )
            if s.encrypted:
                line += f" · 암호화 {s.encrypted}개"
            if s.unreadable:
                line += f" · 데이터 프레임이 아닌 것 {s.unreadable}개"
        lines.append(line)
    if quiet:
        lines.append("  그날 녹화는 있는데 이 구간에 프레임이 없는 피드: " + " ".join(quiet))
    if not seen:
        lines.append("  그날 WebSocket 피드의 raw가 없다")
    return lines


NOTES = (
    f"읽은 것: raw의 WebSocket 프레임({MEMBER.tr_id}는 녹화한 모든 날, 다른 피드는 4절의 구간)의"
    " 받은 시각과 원문. 값은 싣지 않는다(개수, 시각, 종목코드, 필드 수, 칸의 모양만).",
    f"필드 수: 원문의 셋째 '|' 뒤를 '^'로 나눈 수. 정규화는 레코드 수 × {WIDTH}(MEMBER_COLUMNS)를 바라고"
    "(kis.RealtimeFrame.records), 다르면 수집기가 raw에만 남긴다. 다른 피드는 그 피드의 칸 수로 본다.",
    f"모양: '{EMPTY}' 빈 값, '{DIGITS}' 숫자만, '{NUMBER}' 부호(+, -)나 소수점이 있는 수, '{YES_NO}' Y나 N,"
    f" '{TEXT}' 그 밖의 글자(이름 등). MEMBER_COLUMNS 순서로 묶어 띄운다: "
    + " · ".join(_group_name(g) for g in GROUPS),
    f"앞·뒤 일치: 3절의 프레임과 그 종목의 다음 {WIDTH}칸 프레임을 앞에서부터, 뒤에서부터 견주어"
    " 모양이 같은 칸 수. 빠진 칸이 한 덩어리라면 그 덩어리가 시작할 수 있는 칸을 보인다"
    "(모양으로 본 후보, 1부터 센다).",
)

SECTIONS = (
    "1. 날마다",
    "2. 시(KST)마다, 모든 날을 합쳐",
    f"3. 필드 수가 레코드 수 × {WIDTH}이 아닌 프레임(받은 순, 처음 {FRAMES_SHOWN}개)",
    "4. 같은 때의 WebSocket 피드(3절의 프레임마다 그 분의 1분 앞부터 1분 뒤까지, 받은 시각으로)",
)


def _day_line(d: MemberDay) -> str:
    line = f"{d.day}: 프레임 {d.frames:,}개 · 필드 수가 맞지 않는 것 "
    if d.odd:
        fields = Counter(len(o.frame.fields) for o in d.odd)
        hours = Counter(o.frame.received.astimezone(KST).hour for o in d.odd)
        line += f"{len(d.odd)}개(필드 {_counts(fields)} · " + ", ".join(
            f"{h:02d}시 {k}" for h, k in sorted(hours.items())
        )
        line += ")"
    else:
        line += "없음"
    if d.encrypted:
        line += f" · 암호화 {d.encrypted}개"
    if d.unreadable:
        line += f" · 데이터 프레임이 아닌 것 {d.unreadable}개"
    return line


def _hour_lines(read: Sequence[MemberDay]) -> list[str]:
    frames: Counter[int] = Counter()
    odd: Counter[int] = Counter()
    days: dict[int, set[date]] = {}
    for d in read:
        frames.update(d.hours)
        for o in d.odd:
            hour = o.frame.received.astimezone(KST).hour
            odd[hour] += 1
            days.setdefault(hour, set()).add(d.day)
    lines = []
    for hour in sorted(frames.keys() | odd.keys()):
        line = f"{hour:02d}시: 프레임 {frames[hour]:,}개 · 필드 수가 맞지 않는 것 "
        if odd[hour]:
            on = sorted(days[hour])
            line += f"{odd[hour]}개({len(on)}일: {', '.join(str(d) for d in on)})"
        else:
            line += "없음"
        lines.append(line)
    return lines


def report(data_dir: Path, symbols: Sequence[str], now: datetime) -> str:
    """The report on every day up to ``now`` with the member feed's raw frames."""
    days = recorded_days(data_dir, now)
    if not days:
        return f"{TITLE}: raw에 {MEMBER.tr_id} 프레임이 있는 날이 없다"
    unread = Unread()
    read = [read_member_day(data_dir, day, unread) for day in days]
    odd = [o for d in read for o in d.odd]
    shown = odd[:FRAMES_SHOWN]
    details = [line for o in shown for line in odd_lines(o, symbols)]
    if len(odd) > len(shown):
        details.append(f"외 {len(odd) - len(shown)}개(1·2절에 센다)")
    spans = windows(shown)
    around = []
    for start, end in spans[:WINDOWS_SHOWN]:
        around += window_lines(start, end, read_window(data_dir, start, end, unread), shown)
    if len(spans) > WINDOWS_SHOWN:
        around.append(f"외 {len(spans) - WINDOWS_SHOWN}구간은 읽지 않았다")
    total = sum(d.frames for d in read)
    out = [
        f"{TITLE}: {days[0]} – {days[-1]}, {MEMBER.tr_id}를 녹화한 날 {len(days)}일, 프레임 {total:,}개 중"
        f" 필드 수가 맞지 않는 것 {len(odd)}개",
        *NOTES,
        "",
    ]
    for heading, lines in zip(
        SECTIONS, ([_day_line(d) for d in read], _hour_lines(read), details, around), strict=True
    ):
        out += [heading, *(f"  {line}" for line in lines or ["없음"]), ""]
    if unread.files or unread.rows:
        out += ["스키마와 다른 파일, 빈 값의 행", *(f"  {x}" for x in unread.lines()), ""]
    return "\n".join(out).rstrip("\n")


def probe_member_frames(settings: Settings, now: datetime) -> str:
    return report(settings.data_dir, (*settings.symbols, *settings.etf_symbols), now)
