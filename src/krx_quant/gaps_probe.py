"""``krx-quant probe-gaps``: the recording's gaps day by day, and what the closing auction left
in it (#344).

On 2026-10-21 the silence watchdog (#311) closed the WebSocket at 15:30:33: no frame, data or
PINGPONG, had come since 15:29:33, around the closing auction's print (15:30:00, random end to
15:30:30). The watchdog has run since the 2026-10-20 deploy, so whether that silence was one day's
or comes at every closing auction is not known. If it comes every day, the watchdog cuts the
connection at every close, and its 60 seconds (``docs/kis-feeds.md`` "왜 60초인가") need another
look. That decision is the user's; this probe gives it the counts, over every recorded day (a
``raw`` date with a WebSocket feed's frames, or a ``gaps`` date):

1. a line a day: its gaps by reason, and in the closing auction window the longest silence between
   WebSocket frames and the gaps that overlap it;
2. on each trading day, the closing auction window (the session's ``closing_auction`` to a minute
   after its ``close``: 15:20–15:31 on most days): the last frame received in it per WebSocket feed
   (``raw``, every key; ``_system`` holds KIS's PINGPONGs and replies, which the watchdog counts
   too) and how many; the longest silence between any two of them that overlaps the window, from
   the day's last frame before the window (one that began before it shows its start; with none
   that day, it is said so) to ``PRINT_WITHIN`` after the close, or to the run's time if earlier
   (one that runs past the window shows its end, and a run at 15:35 does not count the minutes to
   come); the gaps that overlap the window; for
   each symbol of the KRX (``H0STCNT0``) and the integrated (``H0UNCNT0``) trades
   (``events``), the first trade at or after the close within the window, with its exchange and
   receipt times and its raw codes, or when the first one after the close came; and whether
   ``bars`` holds the day's one-minute bars of the symbol (the backfill's fill), with the bars in
   the window;
3. the day's gaps (``gaps``), by start: the reason, the start, the length and the feeds and keys.
   A disconnect opens one gap per subscription at once, all with its start and reason
   (``FeedHealth.connection_lost``): they are one line with the feed and key counts, the feeds
   grouped by the keys they share, and the range of lengths (each ends when KIS acknowledges its
   subscription again).

**The closing auction's trade.** The docs give no code that marks it: the trade feeds'
``session_code`` (``NEW_MKOP_CLS_CODE``) and ``hour_cls`` (``HOUR_CLS_CODE``) are kept raw, their
meaning not measured (``docs/events.md`` "체결"). KRX takes orders from 15:20 and prints at 15:30
with no trade between (``docs/kis-feeds.md``, the ``J`` bars), so the first trade at or after the
close is taken as the auction's, and the output says so; its codes are shown, to be read later.
The integrated feed does not tag a trade's venue, so its first trade may be NXT's after-market.

**What is read.** Columns by the names of the schemas the collector writes (``health.GAP_SCHEMA``,
``RAW_SCHEMA``, the trade feeds' schema, ``backfill.BAR_SCHEMA``); a file without one, or with a
time column of another type, is counted and not read, never guessed at. A gap still open is not in
the files: it is written when it closes, or without an end when the collector stops.

It prints counts, times, codes, feed names and keys only, no market value, so its output can go on
the public issue (the scheduled job ``probe-gaps-344``). It reads files only, beside the running
collector.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from krx_quant.archive import partition_files
from krx_quant.auctions import PRINT_WITHIN
from krx_quant.backfill import BAR_SCHEMA, MINUTE_BARS_TR, stock_bars_path
from krx_quant.config import Settings
from krx_quant.feeds import KRX_TRADE, RAW_SCHEMA, TRADE, WS_FEEDS
from krx_quant.feeds.base import KST
from krx_quant.health import GAP_SCHEMA, GapKind
from krx_quant.market_calendar import MarketCalendar, Session, calendar_store

TITLE = "WebSocket 결측과 종가 단일가 녹화 (#344)"
# The collector's raw partitions of WebSocket frames that are not a feed's data: KIS's control
# frames (PINGPONG, the replies to registrations) and the frames it could not parse.
SYSTEM, UNPARSED = "_system", "_unparsed"
WS_RAW = (*sorted(WS_FEEDS), SYSTEM, UNPARSED)
# The closing auction window ends this long after the close (15:31): the print's random end is at
# most 30 seconds, and ``auctions.recorded_prints`` reads the recorded print to here too.
AFTER_CLOSE = timedelta(minutes=1)
LINES_A_DAY = 30  # gap lines shown a day, the earliest; the rest are counted by reason
KEYS_SHOWN = 6  # a feed's keys named in a gap line, up to this many; else counted
_DISCONNECTED = GapKind.DISCONNECTED.value

NOTES = (
    "읽은 것: gaps(결측 구간), raw의 WebSocket 프레임을 받은 시각,"
    f" events의 체결({KRX_TRADE.tr_id} KRX, {TRADE.tr_id} 통합), bars({MINUTE_BARS_TR} 1분봉)."
    " 시세 값은 싣지 않는다(개수, 시각, 코드, 피드 이름, 키만).",
    "종가 단일가 체결: 문서에 그것을 가리는 코드가 없다"
    "(체결의 장운영·시간 구분 코드는 뜻을 잰 적이 없다)."
    " 그래서 장 마감(보통 15:30:00) 뒤 1분 안의 첫 체결을 그것으로 본다"
    "(KRX는 15:20–15:30에 체결이 없다)."
    f" 통합({TRADE.tr_id})은 체결의 거래소를 가리지 않아 NXT 애프터마켓의 체결일 수 있다.",
    "결측: 한 끊김은 구독마다 구간을 하나씩, 같은 시작과 이유로 연다."
    " 그것을 한 줄로 묶어 피드·키 수와 길이의 범위를 보인다(구독마다 KIS가 다시 승인한 때 끝난다)."
    " 아직 열린 구간은 닫힐 때(수집기가 멈추면 끝 없이) 기록되므로 없다.",
)
# The columns read, by the names of the schemas the collector writes: a schema without one fails
# here, at import, not in a night's run.
_RECEIVED = RAW_SCHEMA.field("received_at").name
_TRADE_TIMES = tuple(KRX_TRADE.schema.field(c).name for c in ("event_time", "received_at"))
_TRADE_CODES = tuple(KRX_TRADE.schema.field(c).name for c in ("session_code", "hour_cls"))
_BAR_TIME = BAR_SCHEMA.field("bar_time").name

SECTIONS = (
    "1. 날마다 요약",
    "2. 종가 단일가 구간 (거래일마다, 그날 거래 일정의 마감 10분 전부터 마감 1분 뒤까지)",
    "3. 결측 구간 (gaps, 날마다 시작 순)",
)


@dataclass(frozen=True)
class Gap:
    """One row of the ``gaps`` layer."""

    feed: str
    key: str
    start: datetime
    end: datetime | None  # None: still open when the collector stopped
    reason: str


@dataclass
class Group:
    """The gaps of one day with one start and reason: a disconnect's, one per subscription."""

    start: datetime
    reason: str
    gaps: list[Gap] = field(default_factory=list)

    @property
    def disconnect(self) -> bool:
        return self.reason.split(":", 1)[0] == _DISCONNECTED

    def overlaps(self, start: datetime, end: datetime) -> bool:
        return any(g.start < end and (g.end is None or g.end > start) for g in self.gaps)


@dataclass
class Unread:
    """Files unlike the schema the collector writes them in: without a column it names, or with
    one of another type. Counted, never guessed at."""

    files: Counter[str] = field(default_factory=Counter)  # "<layer> <feed> <column> <what>" -> files

    def lines(self) -> list[str]:
        return [f"{what}: 파일 {n}개" for what, n in sorted(self.files.items())]


def _batches(
    partition: Path, columns: Sequence[str], unread: Unread, optional: Sequence[str] = ()
) -> Iterator[Any]:
    """A partition's rows, a batch at a time, each row once (the merged file, then the parts it
    does not hold), with ``columns`` and those of ``optional`` the file has. A file without one of
    ``columns`` is not read; it and one without an ``optional`` column are counted in ``unread``."""
    label = f"{partition.parent.parent.parent.name} {partition.parent.name.removeprefix('feed=')}"
    for path in partition_files(partition):
        try:
            names = pq.read_schema(path).names
            absent = [c for c in columns if c not in names]
            for column in absent:
                unread.files[f"{label} {column} 없음(읽지 않음)"] += 1
            for column in (c for c in optional if c not in names):
                unread.files[f"{label} {column} 없음(그 열 없이 읽음)"] += 1
            if absent:
                continue
            present = [*columns, *(c for c in optional if c in names)]
            yield from pq.ParquetFile(path).iter_batches(columns=present)
        except (FileNotFoundError, pa.ArrowInvalid, OSError):
            continue  # merged away in between, or written as it was read: the rest still counts


def _timestamps(batch: Any, column: str, unread: Unread, label: str) -> Any | None:
    """``batch``'s ``column`` if it is a timestamp column, else None (counted in ``unread``)."""
    values = batch.column(batch.schema.get_field_index(column))
    if not pa.types.is_timestamp(values.type):
        unread.files[f"{label} {column} 타입 {values.type}(읽지 않음)"] += 1
        return None
    return values


def _between(values: Any, start: datetime, end: datetime | None) -> Any:
    """A mask of ``values`` (a timestamp column) at or after ``start`` and, if any, before ``end``."""
    mask = pc.greater_equal(values, pa.scalar(start, type=values.type))
    if end is None:
        return mask
    return pc.and_(mask, pc.less(values, pa.scalar(end, type=values.type)))


def read_gaps(data_dir: Path, day: date, unread: Unread) -> list[Gap]:
    """The gaps that started on ``day`` (the ``gaps`` partition's date is the start's)."""
    root = data_dir / "gaps" / f"date={day.isoformat()}"
    names = GAP_SCHEMA.names
    out = []
    for partition in sorted(root.glob("feed=*/symbol=*")):
        for batch in _batches(partition, names, unread):
            rows = batch.to_pydict()
            for feed, key, start, end, reason in zip(*(rows[c] for c in names), strict=True):
                if start is None:
                    unread.files[f"gaps {feed} start 빈 값(그 행을 읽지 않음)"] += 1
                    continue
                out.append(Gap(str(feed), str(key), start, end, str(reason)))
    return out


def groups_of(gaps: Sequence[Gap]) -> list[Group]:
    """The gaps grouped by start and reason, in order of start."""
    by: dict[tuple[datetime, str], Group] = {}
    for gap in gaps:
        by.setdefault((gap.start, gap.reason), Group(gap.start, gap.reason)).gaps.append(gap)
    return sorted(by.values(), key=lambda g: (g.start, g.reason))


def recorded_days(data_dir: Path, now: datetime) -> list[date]:
    """Every day up to ``now`` with a WebSocket feed's raw frames or with gaps, oldest first."""
    days: set[date] = set()
    for layer in ("raw", "gaps"):
        for path in (data_dir / layer).glob("date=*"):
            try:
                day = date.fromisoformat(path.name.removeprefix("date="))
            except ValueError:
                continue
            if layer == "gaps" or any((path / f"feed={feed}").is_dir() for feed in WS_RAW):
                days.add(day)
    today = now.astimezone(KST).date()
    return sorted(d for d in days if d <= today)


@dataclass
class Frames:
    """The day's WebSocket frames around the closing auction window."""

    by_feed: dict[str, list[datetime]]  # receipt times read, by feed (every key), sorted
    before: datetime | None  # the day's last frame before the read, of any feed; None: none


def frames(data_dir: Path, day: date, start: datetime, end: datetime, unread: Unread) -> Frames:
    """The receipt times of the day's WebSocket frames from ``start`` to before ``end``, by feed
    (a feed recorded that day without one there has an empty list), and the day's last frame
    before ``start``, so a silence that began before the read shows its start."""
    by_feed: dict[str, list[datetime]] = {}
    before: datetime | None = None
    for feed in WS_RAW:
        root = data_dir / "raw" / f"date={day.isoformat()}" / f"feed={feed}"
        if not root.is_dir():
            continue
        times: list[datetime] = []
        for partition in sorted(root.glob("symbol=*")):
            for batch in _batches(partition, (_RECEIVED,), unread):
                values = _timestamps(batch, _RECEIVED, unread, f"raw {feed}")
                if values is None:
                    continue
                times += pc.filter(values, _between(values, start, end)).to_pylist()
                earlier = pc.max(pc.filter(values, pc.less(values, pa.scalar(start, type=values.type))))
                last = earlier.as_py()
                if last is not None and (before is None or last > before):
                    before = last
        by_feed[feed] = sorted(times)
    return Frames(by_feed, before)


# A silence: its first frame (None: no frame that day before it) and the frame that ended it
# (None: none came before the read's end).
Silence = tuple[datetime | None, datetime | None]


def longest_silence(
    times: Sequence[datetime], before: datetime | None, start: datetime, end: datetime, until: datetime
) -> Silence:
    """The longest silence that overlaps ``start`` to ``end``, between the frames ``times`` (sorted,
    read from ``start`` to ``until``) and ``before``, the day's last frame before them. A silence
    with no frame before it is counted from the day's start."""
    opened = datetime.combine(start.astimezone(KST).date(), datetime.min.time(), KST)
    edges: list[datetime | None] = [before, *times, None]
    best: tuple[timedelta, Silence] | None = None
    for first, after in zip(edges, edges[1:], strict=False):
        if first is not None and first >= end:
            break
        if after is not None and after <= start:
            continue
        span = (after or until) - (first or opened)
        if best is None or span > best[0]:
            best = (span, (first, after))
    assert best is not None, "the edges overlap the window"
    return best[1]


@dataclass(frozen=True)
class Trade:
    """A recorded trade's times and raw codes, never its price."""

    at: datetime  # exchange time (event_time)
    received: datetime
    session_code: str | None  # None: not in the file
    hour_cls: str | None


def first_trade(
    data_dir: Path, day: date, feed: str, symbol: str, since: datetime, unread: Unread
) -> tuple[bool, Trade | None]:
    """Whether ``symbol``'s ``feed`` trades of ``day`` are recorded (any row), and the first at or
    after ``since`` by exchange time (then by receipt)."""
    partition = data_dir / "events" / f"date={day.isoformat()}" / f"feed={feed}" / f"symbol={symbol}"
    recorded, best = False, None
    exchange, received = _TRADE_TIMES
    for batch in _batches(partition, _TRADE_TIMES, unread, optional=_TRADE_CODES):
        recorded = recorded or batch.num_rows > 0
        values = _timestamps(batch, exchange, unread, f"events {feed}")
        if values is None or _timestamps(batch, received, unread, f"events {feed}") is None:
            continue
        later = batch.filter(_between(values, since, None))
        if not later.num_rows:
            continue
        order = pc.sort_indices(later, sort_keys=[(exchange, "ascending"), (received, "ascending")])
        row = later.take(order.slice(0, 1)).to_pylist()[0]
        codes = [row.get(c) for c in _TRADE_CODES]
        found = Trade(row[exchange], row[received], *codes)
        if best is None or (found.at, found.received) < (best.at, best.received):
            best = found
    return recorded, best


def bars_in(
    data_dir: Path, day: date, symbol: str, start: datetime, end: datetime, unread: Unread
) -> int | None:
    """How many of ``symbol``'s stored one-minute bars of ``day`` start in the window; None if the
    day has no bars file (not filled yet)."""
    path = stock_bars_path(data_dir, day, symbol)
    if not path.exists():
        return None
    column = _BAR_TIME
    try:
        if column not in pq.read_schema(path).names:
            unread.files[f"bars {MINUTE_BARS_TR} {column} 없음(읽지 않음)"] += 1
            return 0
        table = pq.read_table(path, columns=[column])
    except (pa.ArrowInvalid, OSError):
        unread.files[f"bars {MINUTE_BARS_TR} 읽을 수 없음"] += 1
        return 0
    values = table.column(column)
    if not pa.types.is_timestamp(values.type):
        unread.files[f"bars {MINUTE_BARS_TR} {column} 타입 {values.type}(읽지 않음)"] += 1
        return 0
    return int(pc.sum(_between(values, start, end)).as_py() or 0)


def _time(at: datetime, day: date) -> str:
    """``at`` as a KST time, with its date when it is not ``day``."""
    local = at.astimezone(KST)
    return local.strftime("%H:%M:%S" if local.date() == day else "%m-%d %H:%M:%S")


def _length(span: timedelta) -> str:
    seconds = span.total_seconds()
    if seconds < 60:
        return f"{seconds:.1f}초"
    hours, rest = divmod(round(seconds), 3600)
    minutes, seconds_left = divmod(rest, 60)
    return f"{hours}시간 {minutes}분" if hours else f"{minutes}분 {seconds_left}초"


def _lengths(group: Group) -> str:
    """The group's lengths: one, or their range, and those with no end."""
    spans = sorted(g.end - g.start for g in group.gaps if g.end is not None)
    endless = len(group.gaps) - len(spans)
    parts = []
    if spans:
        low, high = _length(spans[0]), _length(spans[-1])
        parts.append(low if low == high else f"{low}–{high}")
    if endless:
        parts.append(f"끝 없음(수집기가 멈춤) {endless}")
    return ", ".join(parts)


def _covered(group: Group) -> str:
    """The feeds and keys of a group: the feeds sharing a set of keys together."""
    keys: dict[str, set[str]] = {}
    for gap in group.gaps:
        keys.setdefault(gap.feed, set()).add(gap.key)
    feeds_of: dict[tuple[str, ...], list[str]] = {}
    for feed, of_feed in keys.items():
        feeds_of.setdefault(tuple(sorted(of_feed)), []).append(feed)
    parts = []
    for held, feeds in sorted(feeds_of.items(), key=lambda kv: sorted(kv[1])):
        named = " ".join(held) if len(held) <= KEYS_SHOWN else f"키 {len(held)}개"
        parts.append(f"{' '.join(sorted(feeds))}: {named}")
    return " / ".join(parts)


def group_line(group: Group, day: date) -> str:
    feeds = len({g.feed for g in group.gaps})
    return (
        f"{_time(group.start, day)} {group.reason} · 피드 {feeds}개, 키 {len(group.gaps)}개 · "
        f"길이 {_lengths(group)} · {_covered(group)}"
    )


def _by_reason(groups: Sequence[Group]) -> str:
    """The groups and gaps of each reason."""
    starts: Counter[str] = Counter(g.reason for g in groups)
    gaps: Counter[str] = Counter()
    for group in groups:
        gaps[group.reason] += len(group.gaps)
    return " · ".join(f"{reason} {starts[reason]}번(구간 {gaps[reason]}개)" for reason in sorted(starts))


@dataclass
class Closing:
    """What one trading day's closing auction window held: its longest silence, said, and the
    lines."""

    silence: str
    lines: list[str]


def _window(session: Session) -> tuple[datetime, datetime]:
    return session.closing_auction, session.close + AFTER_CLOSE


def closing(
    data_dir: Path,
    session: Session,
    groups: Sequence[Group],
    stocks: Sequence[str],
    etfs: Sequence[str],
    now: datetime,
    unread: Unread,
) -> Closing:
    """The closing auction window of ``session``'s day (``SECTIONS`` 2), over by ``now``. Frames
    are read to ``PRINT_WITHIN`` after the close (or to ``now``, if earlier), so a silence that runs
    past the window shows its end, and the day's last frame before the window gives the start of
    one that began before it."""
    day = session.day
    start, end = _window(session)
    until = min(session.close + PRINT_WITHIN, now)
    read = frames(data_dir, day, start, until, unread)
    every = sorted(t for times in read.by_feed.values() for t in times)
    silence = _silence(longest_silence(every, read.before, start, end, until), until, day)
    within = {feed: [t for t in times if t < end] for feed, times in read.by_feed.items()}
    heard = [f"{feed} {_time(times[-1], day)} · {len(times):,}" for feed, times in within.items() if times]
    lines = ["WebSocket 프레임(피드별 마지막 받은 시각 · 수): " + (" / ".join(heard) or "없음")]
    quiet = [feed for feed, times in within.items() if not times]
    if quiet:
        lines.append("이 구간에 프레임이 없는 피드(그날 녹화는 있다): " + " ".join(quiet))
    lines.append(
        "가장 긴 침묵(모든 WebSocket 프레임 사이, 구간 앞의 마지막 프레임부터 마감 "
        f"{_minutes(PRINT_WITHIN)} 뒤까지 본다): {silence}"
    )
    overlapping = [g for g in groups if g.overlaps(start, end)]
    lines += [f"겹친 결측: {group_line(g, day)}" for g in overlapping] or ["겹친 결측: 없음"]
    since = _time(session.close, day)
    for feed, name, symbols in ((KRX_TRADE.tr_id, "KRX", [*stocks, *etfs]), (TRADE.tr_id, "통합", stocks)):
        entries = []
        for symbol in _symbols(data_dir, day, feed, symbols):
            recorded, trade = first_trade(data_dir, day, feed, symbol, session.close, unread)
            entries.append(f"{symbol} {_trade(recorded, trade, end, day)}")
        lines.append(
            f"마감({since}) 뒤 1분 안의 첫 체결 {name} {feed}: " + (" · ".join(entries) or "종목 없음")
        )
    fills = []
    for symbol in _unique([*stocks, *etfs]):
        count = bars_in(data_dir, day, symbol, start, end, unread)
        fills.append(f"{symbol} " + ("없음" if count is None else f"있음(이 구간 봉 {count}개)"))
    lines.append(f"1분봉 채움 bars {MINUTE_BARS_TR}: " + " · ".join(fills))
    return Closing(silence, lines)


def _minutes(span: timedelta) -> str:
    return f"{round(span.total_seconds() / 60)}분"


def _silence(silence: Silence, until: datetime, day: date) -> str:
    first, after = silence
    if first is None and after is None:
        return f"그날 {_time(until, day)}까지 프레임 없음"
    if first is None:
        assert after is not None
        return f"{_time(after, day)} 전에는 그날 프레임 없음"
    if after is None:
        return f"{_time(first, day)}부터 {_time(until, day)}까지 프레임 없음({_length(until - first)} 넘게)"
    # In seconds, to set against the watchdog's (``KRX_WS_SILENCE_SECONDS``, 60 by default).
    span = after - first
    said = f"{span.total_seconds():.1f}초" if span < timedelta(hours=1) else _length(span)
    return f"{said}({_time(first, day)}–{_time(after, day)})"


def _trade(recorded: bool, trade: Trade | None, end: datetime, day: date) -> str:
    if not recorded:
        return "녹화 없음"
    if trade is None:
        return "없음(마감 뒤 체결 없음)"
    if trade.at >= end:
        return f"없음(마감 뒤 첫 체결 {_time(trade.at, day)})"
    codes = f"장운영 '{_code(trade.session_code)}' 시간 '{_code(trade.hour_cls)}'"
    return f"{_time(trade.at, day)}(받음 {_time(trade.received, day)}, {codes})"


def _code(value: str | None) -> str:
    return "열 없음" if value is None else value.strip()


def _symbols(data_dir: Path, day: date, feed: str, configured: Sequence[str]) -> list[str]:
    """The configured symbols and any other recorded for ``feed`` that day."""
    root = data_dir / "events" / f"date={day.isoformat()}" / f"feed={feed}"
    recorded = [p.name.removeprefix("symbol=") for p in sorted(root.glob("symbol=*"))]
    return _unique([*configured, *recorded])


def _unique(symbols: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(symbols))


def report(
    data_dir: Path,
    calendar: MarketCalendar,
    stocks: Sequence[str],
    etfs: Sequence[str],
    now: datetime,
) -> str:
    """The report on every recorded day up to ``now``."""
    days = recorded_days(data_dir, now)
    if not days:
        return f"{TITLE}: raw에 WebSocket 프레임이 있거나 gaps가 있는 날이 없다"
    unread = Unread()
    trading = [d for d in days if calendar.session(d) is not None]
    summary: list[str] = []
    windows: list[str] = []
    details: list[str] = []
    for day in days:
        groups = groups_of(read_gaps(data_dir, day, unread))
        session = calendar.session(day)
        line = f"{day} {'거래일' if session is not None else '휴장일'}: "
        line += (
            f"결측 구간 {sum(len(g.gaps) for g in groups)}개 · {_by_reason(groups)}"
            if groups
            else "결측 없음"
        )
        if session is not None:
            start, end = _window(session)
            span = f"{_time(start, day)[:5]}–{_time(end, day)[:5]}"
            if now < end:
                line += f" · 종가 단일가 {span}: 아직 지나지 않음"
            else:
                seen = closing(data_dir, session, groups, stocks, etfs, now, unread)
                overlapping = sum(1 for g in groups if g.disconnect and g.overlaps(start, end))
                line += f" · 종가 단일가 {span}: 가장 긴 침묵 {seen.silence}"
                line += f", 겹친 끊김 {overlapping}번" if overlapping else ", 겹친 끊김 없음"
                windows += [f"{day} {_time(start, day)}–{_time(end, day)}", *(f"  {x}" for x in seen.lines)]
        summary.append(line)
        details += [f"{day}", *(f"  {x}" for x in _gap_lines(groups, day))]
    out = [
        f"{TITLE}: {days[0]} – {days[-1]}, 녹화한 날 {len(days)}일(거래일 {len(trading)}일)",
        *NOTES,
        "",
    ]
    for heading, lines in zip(SECTIONS, (summary, windows, details), strict=True):
        out += [heading, *(f"  {line}" for line in lines or ["없음"]), ""]
    if unread.files:
        out += ["스키마와 다른 파일(열이 없거나 타입이 다르다)", *(f"  {x}" for x in unread.lines()), ""]
    return "\n".join(out).rstrip("\n")


def _gap_lines(groups: Sequence[Group], day: date) -> list[str]:
    """A line a group, the earliest ``LINES_A_DAY``; the rest counted by reason."""
    if not groups:
        return ["없음"]
    shown, rest = groups[:LINES_A_DAY], groups[LINES_A_DAY:]
    lines = [group_line(g, day) for g in shown]
    if rest:
        lines.append(f"외 {len(rest)}줄: {_by_reason(rest)}")
    return lines


def probe_gaps(settings: Settings, now: datetime) -> str:
    calendar = calendar_store(settings).calendar
    return report(settings.data_dir, calendar, settings.symbols, settings.etf_symbols, now)
