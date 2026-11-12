"""``krx-quant recording-quality``: a trading day's recording quality, reported every trading night
(#570, the scheduled job ``recording-quality-570`` on the standing issue #571).

Nothing checked the recording day by day: the collector's alerts are live (#488, #516), ``doctor``
runs when asked, and only the trader's replay and re-fit read the ``gaps`` layer. Over the ten
trading days to 2026-11-05 the WebSocket dropped 31 times, 12 of them in the session, more often
lately, twice silent for 60 seconds; a gap shorter than that leaves its windows scored, and the
night feeds never go stale (``health.NIGHT_FEEDS``). So after the nightly training the report reads
the day's files and the engine's own status, in five sections:

1. **Disconnects in the session** (``gaps``, the session's open to its close, 09:00–15:30 on most
   days): each disconnect that overlaps it on one line (``group_line``: a disconnect
   opens a gap per subscription, one start and reason), counted by length (under ``SHORT``, to
   ``LONG``, ``LONG`` and over: a book older than 60 s leaves its windows unscored), the ones outside
   the session counted; and per feed, its disconnects and stale gaps in the session, how many and
   the seconds one of its keys was in one (the union of its keys' gaps, cut to the session). A gap
   written without an end (the collector stopped) is counted apart, its length unknown.
2. **Recorded bars against KIS's** (``bars``, ``FHKST03010230``, filled at 20:30): for each stock
   and ETF set, the minutes of continuous trading (the open to the closing auction, the 09:00–15:19
   bars, a bar's time its minute's start) of KIS's bars against those built from the recorded
   trades as the engine builds them (``reference.BarBuilder``: by exchange time, a minute's close
   the last trade received in its last second): the integrated trades (``H0UNCNT0``) for a bar file
   of the integrated market (``UN``), the KRX trades (``H0STCNT0``) for one of KRX (``KRX``: the
   ETFs NXT does not trade). A minute is missing where KIS's bar traded and the recording has no
   trade, extra the other way, and where both traded its volume or close may differ; each is
   counted, and those outside every gap of the trade feed's symbol apart: those are losses nothing
   recorded. A row that repeats an earlier one's exchange time, price, quantity and cumulative
   volume (an increasing count, so two trades never share it) may be one trade recorded twice. The
   minutes that differ outside the gaps are counted by the causes they show (``CAUSES``, #631): a
   minute's edge, an auction, a trade of another hour or session code, trades KIS's cumulative
   volume counts that never came, the order within a second; with the day's cumulative-volume chain
   (the trades in its order, each adding its own quantity, and where it does not).
3. **Frames** (``raw``, the day's): those the collector could not parse (``_unparsed``), and the
   data frames of a known WebSocket feed whose fields are fewer than their records need (kept raw
   only, ``kis.ShortFrame``, #381) or more, counted by feed with arrow's string kernels (the header's
   record count, the ``^`` between fields), not parsed a frame at a time.
4. **REST failures** (``/v1/status``'s ``kis.polls``, #441): the engine counts each poll's calls
   and failures by kind for today (KST) since it started, in memory only. They are the report's day
   only while the engine's day is it: after midnight they are the next day's, and said unknown.
5. **The night's recording** (``night_window``: the evening of the trading day before, 18:00
   to 06:00 and its ten late minutes): whether KIS took the night trades' registration by 18:00, the
   night trades' records (the frames' header counts) and events, against the median of the nights
   subscribed for in the week before it (``subscribed_nights``). The night book (``H0MFASP0``) is not
   recorded since #694, so the report no longer expects it.

**When it calls an agent.** The report's second line is #402's ``에이전트 차례:`` line
(``trader.report.AGENT``), and the job labels the issue only on a night whose line names a reason
(``scheduled.probes``, ``Job.label_if``) or whose run failed. Each threshold and why is beside its
constant below; a reason says the figure and its threshold.

**What it prints.** Counts, seconds, rates, times, feed names, keys and contract codes: no price, no
volume, so its output can go on the public issue. It reads files and the engine's status only,
beside the running collector, and calls no KIS API. A raw day the archive pruned on the server, older
than 30 days (#607), is read back from the Object Store (``raw_days.RawDays``): a run for such a day
(``--day``) downloads its frames, and fails if they cannot be read back.

**Memory and time.** A day's files are read a batch at a time (arrow's batches of a file), a
symbol's trades at a time for the bars, held as arrow columns and numpy arrays (#631), and a night's
frames for their counts alone; what stays is counts and a day's bars of one symbol. The run's peak
resident memory, each section's and its seconds are the last section. Its peak is checked after each
batch read (``Budget``): once over ``MEMORY_MB`` the run stops in that section, leaves the rest
unread and says so, a reason that calls an agent, so that the job does not push the engine into swap.
The job's time limit is its bound (``scheduled.probes``).
"""

from __future__ import annotations

import functools
import statistics
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from time import monotonic
from typing import Any

import aiohttp
import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from krx_quant import kis
from krx_quant.archive import partition_batches
from krx_quant.backfill import BAR_SCHEMA, BARS_FINAL_AT, MINUTE_BARS_TR, stock_bars_path
from krx_quant.collector import SHORT_FRAMES_EXPECTED
from krx_quant.config import Settings
from krx_quant.contracts import NIGHT_END, NIGHT_OPEN
from krx_quant.doctor import Status, api_candidates, fetch_status
from krx_quant.feeds import KRX_TRADE, NIGHT_FUTURES, RAW_SCHEMA, TRADE, WS_FEEDS, Feed
from krx_quant.feeds.base import KST
from krx_quant.footprint import PEAK_START, Meter, give_back
from krx_quant.health import Gap, GapKind, Unread, read_batches, read_gaps
from krx_quant.market_calendar import MarketCalendar, Session, calendar_store
from krx_quant.object_store import open_store
from krx_quant.poll_outages import SUSTAINED_FAILURES
from krx_quant.raw_days import SYSTEM, UNPARSED, RawDays
from krx_quant.reference import BAR, Bar, BarBuilder
from krx_quant.trader.report import AGENT, NO_CALL

TITLE = "기록 품질 (#570)"
WEEKDAYS = "월화수목금토일"

# The thresholds that call an agent, and why.
#
# A day's disconnects in the session. Over the ten trading days 2026-10-23 – 11-05 the integrated
# book's disconnects in the session were 0–2 a day on eight of them, and 3 (11-03) and 4 (11-05) on
# the two that showed them rising (Loki, the collector's ``gap`` lines).
DISCONNECTS = 3
# A disconnect this long: a book this old leaves its windows unscored (``reference.MAX_QUOTE_AGE``,
# the silence watchdog's ``KRX_WS_SILENCE_SECONDS``). Two in those ten days (10-29 09:50, 11-03
# 14:02, 62 s each); the rest were 1.7–2.9 s, under ``SHORT``.
LONG = timedelta(seconds=60)
SHORT = timedelta(seconds=5)
# Minutes KIS's bars traded in and the recording has none, outside every gap of the trade feed's
# symbol: trades lost with nothing recorded to say so. One calls an agent.
MISSING_MINUTES = 1
# The share of a symbol's minutes compared, outside its gaps, whose volume or close differ from KIS's
# bar. A trade near a minute's edge may move a few; the agreement is not measured yet (the first
# nights' reports are its baseline), so 1%: about four of a day's 380 minutes.
DIFFERING = 0.01
# Rows that may be one trade recorded twice: one calls an agent (a trade's cumulative volume only
# grows, so no two trades share all four values).
DUPLICATES = 1
# Frames short of fields on the feeds that send them now and then (``collector.SHORT_FRAMES_EXPECTED``,
# the member feed, #381): 9 in the 14 days to 2026-10-29, one or two a day, all at 08:00
# (``probe-member-frames-381``), and two on 11-03 (Loki). More than this calls an agent; on any other
# feed, one does, as does a frame unparsed or with more fields than its records need: none came from
# 2026-10-23 to 11-06 (Loki).
SHORT_EXPECTED = 2
# A day's failed REST calls, or a key failing ``SUSTAINED_FAILURES`` times in a row (the Telegram
# alert's count, #441). 2026-10-23 – 11-05 had 0 or 1 a day but two days: 28 on 10-23 (the VI status,
# ``Server disconnected``) and 41 on 10-31 (the investor estimates on a Saturday, #488).
POLL_FAILURES = 10
# A night's records under this share of the median of the nights before it, or none: a feed gone or
# the wrong month after a roll (the first is 2026-12-10's evening) leaves next to nothing. How much a
# night differs from the next is not measured yet, so a quarter. Records not normalized: any (the
# probed night, 2026-10-19, had none of 796,849, #264).
NIGHT_SHARE = 0.25
# The run's peak resident memory: over it, the run stops where it is (``Budget``, #631). The server has
# 2 GB and the jobs run one at a time beside the collector; the CLI's imports alone hold about 125 MB
# (#470). 2026-11-06's run peaked at 1,279 MB. On a made-up day of its size (the trades of 005930,
# 000660 and 009150: 1.2 million, 670,000 and 255,000 rows; 3.1 million raw frames; the night's book
# of 2 million records and two nights before of 1 million), in the server's image (Linux), the bars
# raised the peak by 476 MB over the imports' 139 (a Python tuple a row, and a set of them for the
# repeats), the frames by 149 and the night by 140; with the trades held as columns, the bars by 236:
# a peak of 375 MB.
MEMORY_MB = 500.0

# Raw frames are read this many rows a batch. On a made-up day of 1.7 million frames (the book's 570,000
# of three stocks among them, #470's server-sized day) in merged files (``archive``'s row groups of
# 256,000), on a Mac, reading them at arrow's 65,536 rows a batch raised the peak resident memory by
# about 220 MB over the CLI's imports, at 8,192 by about 110 MB, in about the same time.
RAW_BATCH_ROWS = 8_192
DAY_END = time(23, 59, 59)
NIGHT_BASELINE = 3  # the nights before shown, the latest; the median is of every one found

NOTES = (
    "읽은 것: gaps(결측 구간), bars(KIS 1분봉, 20:30에 채운다), events의 통합·KRX 체결, raw의 프레임,"
    " 엔진의 /v1/status. 시세 값(가격, 거래량)은 싣지 않는다(개수, 초, 비율, 시각, 피드 이름, 키만).",
    "기준을 넘은 것은 위 줄에 까닭과 기준을 적고 이슈에 라벨을 붙인다."
    ' 기준과 그 까닭: docs/operations.md "기록 품질 리포트 (#570)".',
)

# The columns read, by the names of the schemas the collector writes: a schema without one fails
# here, at import, not in a night's run.
_RECEIVED = RAW_SCHEMA.field("received_at").name
_MESSAGE = RAW_SCHEMA.field("message").name
_TRADE_COLUMNS = tuple(TRADE.schema.field(c).name for c in ("event_time", "received_at", "price", "qty"))
_CUM_VOLUME = TRADE.schema.field("cum_volume").name
_CODES = tuple(TRADE.schema.field(c).name for c in ("hour_cls", "session_code"))
_TRADE_OPTIONAL = (_CUM_VOLUME, *_CODES)
# The trades as the bars read them, a file's own types cast to these (``_trades_of``).
_TRADES = pa.schema([TRADE.schema.field(c) for c in (*_TRADE_COLUMNS, *_TRADE_OPTIONAL)])
_BUILD_ROWS = 65_536  # the trades turned into Python objects at once, for the bar builder
_BAR_COLUMNS = tuple(BAR_SCHEMA.field(c).name for c in ("bar_time", "venue", "close", "volume"))
# The bars' venue -> the recorded trades they are built from (``backfill.MARKETS``).
TRADES_OF_VENUE = {"UN": TRADE, "KRX": KRX_TRADE}
# A data frame's header (``kis.parse_message``): ``<encrypted>|<tr_id>|<records>|``.
_HEADER = r"^(?P<encrypted>[01])\|(?P<tr_id>[^|]*)\|(?P<records>[0-9]+)\|"


class OverMemory(Exception):
    """The run's peak resident memory went over its bound: it stops reading (#631)."""

    def __init__(self, peak_mb: float, bound_mb: float) -> None:
        super().__init__(f"peak {peak_mb:.0f} MB over {bound_mb:.0f} MB")
        self.peak_mb = peak_mb


class Budget:
    """The run's memory (#631): each section's peak resident memory (``Meter``, reset at the section's
    start where the system lets it be), and the bound ``check`` holds the reading to, after each batch
    read. Without a bound (the default, as a test or another caller reads a day) it measures nothing."""

    def __init__(self, bound_mb: float | None = None, meter: Meter | None = None) -> None:
        self.bound_mb = bound_mb
        self._meter = meter if meter is not None else Meter()
        self.peaks: dict[str, float | None] = {}  # by section, in the order read

    def check(self) -> None:
        """Raise ``OverMemory`` once the peak is over the bound."""
        if self.bound_mb is None:
            return
        peak = self._meter.peak_mb()
        if peak is not None and peak > self.bound_mb:
            raise OverMemory(peak, self.bound_mb)

    @contextmanager
    def section(self, name: str) -> Iterator[None]:
        """Keep ``name``'s own peak (the process's since it started where the peak cannot be reset), and
        hold it to the bound once the section is read, before the next section's reset forgets it: what
        the section's last batch took after its check counts too."""
        if self.bound_mb is None:
            yield
            return
        self.peaks.setdefault("시작", self._meter.peak_mb())  # the imports and what came before
        self._meter.reset()
        try:
            yield
        finally:
            self.peaks[name] = self._meter.peak_mb()
        self.check()

    @property
    def since_start(self) -> bool:
        """Whether each peak is the process's since it started, not its section's own."""
        return self._meter.peak_since == PEAK_START

    def peak_mb(self) -> float | None:
        """The run's peak: the highest of its sections' and before them, else the process's."""
        known = [p for p in self.peaks.values() if p is not None]
        return max(known) if known else self._meter.peak_mb()


@dataclass
class Found:
    """What a run found: the reasons it calls an agent, and its sections' lines."""

    day: date
    session: Session
    reasons: list[str] = field(default_factory=list)
    sections: list[tuple[str, list[str]]] = field(default_factory=list)
    unread: Unread = field(default_factory=Unread)
    budget: Budget = field(default_factory=Budget)
    stopped: str | None = None  # the section the memory bound stopped the run in


# 1. Disconnects in the session.


# A disconnect's gaps, one line for each (``gaps_probe`` prints them too, #344).
KEYS_SHOWN = 6  # a feed's keys named in a gap line, up to this many; else counted
_DISCONNECTED = GapKind.DISCONNECTED.value


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


def groups_of(gaps: Sequence[Gap]) -> list[Group]:
    """The gaps grouped by start and reason, in order of start."""
    by: dict[tuple[datetime, str], Group] = {}
    for gap in gaps:
        by.setdefault((gap.start, gap.reason), Group(gap.start, gap.reason)).gaps.append(gap)
    return sorted(by.values(), key=lambda g: (g.start, g.reason))


def length_text(span: timedelta) -> str:
    """A gap's length: seconds with a tenth under a minute, else minutes and seconds, or hours and
    minutes."""
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
        low, high = length_text(spans[0]), length_text(spans[-1])
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
    """A disconnect's line: its start and reason, the feeds and keys it opened a gap for, and their
    lengths."""
    feeds = len({g.feed for g in group.gaps})
    return (
        f"{_time(group.start, day)} {group.reason} · 피드 {feeds}개, 키 {len(group.gaps)}개 · "
        f"길이 {_lengths(group)} · {_covered(group)}"
    )


def _clip(gap: Gap, start: datetime, end: datetime) -> tuple[datetime, datetime] | None:
    """``gap`` cut to ``start``–``end``; None if it has no end or lies outside."""
    if gap.end is None or gap.end <= start or gap.start >= end:
        return None
    return max(gap.start, start), min(gap.end, end)


def _union(spans: Iterable[tuple[datetime, datetime]]) -> timedelta:
    """How long at least one of ``spans`` covers."""
    total, reach = timedelta(), None
    for start, end in sorted(spans):
        if reach is None or start > reach:
            total += end - start
            reach = end
        elif end > reach:
            total += end - reach
            reach = end
    return total


def longest(group: Group) -> timedelta | None:
    """A disconnect's length: until KIS took its last subscription back; None if one never came back
    (its gap was written without an end when the collector stopped), however soon the rest did."""
    ends = [g.end for g in group.gaps]
    if any(end is None for end in ends):
        return None
    return max(end - g.start for g, end in zip(group.gaps, ends, strict=True) if end is not None)


def _bucket(span: timedelta | None) -> str:
    if span is None:
        return "끝 없음"
    if span < SHORT:
        return f"{SHORT.seconds}초 미만"
    if span < LONG:
        return f"{SHORT.seconds}–{LONG.seconds}초"
    return f"{LONG.seconds}초 이상"


@dataclass
class FeedGaps:
    """One feed's gaps in the session: disconnects (their starts) and stale gaps (each key's)."""

    disconnects: set[datetime] = field(default_factory=set)
    disconnected: list[tuple[datetime, datetime]] = field(default_factory=list)
    stale: int = 0
    staled: list[tuple[datetime, datetime]] = field(default_factory=list)
    endless: int = 0


def disconnects_section(found: Found, gaps: Sequence[Gap]) -> list[str]:
    """Section 1: the session's disconnects, each and by feed; their reasons to call an agent."""
    session, day = found.session, found.day
    start, end = session.open, session.close
    groups = [g for g in groups_of(gaps) if g.disconnect]
    inside = [g for g in groups if g.overlaps(start, end)]
    buckets = Counter(_bucket(longest(g)) for g in inside)
    order = (
        f"{SHORT.seconds}초 미만",
        f"{SHORT.seconds}–{LONG.seconds}초",
        f"{LONG.seconds}초 이상",
        "끝 없음",
    )
    lines = [
        f"장중 끊김 {len(inside)}번("
        + ", ".join(f"{b} {buckets[b]}" for b in order if buckets[b] or b != "끝 없음")
        + f") · 장 밖 끊김 {len(groups) - len(inside)}번"
    ]
    lines += [f"{group_line(g, day)} · {_bucket(longest(g))}" for g in inside]
    by_feed: dict[str, FeedGaps] = {}
    for gap in gaps:
        if not (gap.start < end and (gap.end is None or gap.end > start)):
            continue
        feed = by_feed.setdefault(gap.feed, FeedGaps())
        clipped = _clip(gap, start, end)
        if gap.end is None:
            feed.endless += 1
        if gap.reason.split(":", 1)[0] == GapKind.DISCONNECTED.value:
            feed.disconnects.add(gap.start)
            if clipped is not None:
                feed.disconnected.append(clipped)
        else:
            feed.stale += 1
            if clipped is not None:
                feed.staled.append(clipped)
    if by_feed:
        lines.append("피드별(장중 끊김 횟수와 그 피드의 키가 하나라도 끊긴 초 · stale 구간 수와 초):")
        for name in sorted(by_feed):
            feed = by_feed[name]
            line = (
                f"  {name}: 끊김 {len(feed.disconnects)}번 {_union(feed.disconnected).total_seconds():.1f}초"
                f" · stale {feed.stale}개 {_union(feed.staled).total_seconds():.1f}초"
            )
            if feed.endless:
                line += f" · 끝 없음(수집기가 멈춤, 길이 모름) {feed.endless}개"
            lines.append(line)
    else:
        lines.append("장중 결측 구간 없음")
    if len(inside) >= DISCONNECTS:
        found.reasons.append(f"장중 끊김 {len(inside)}번(기준 {DISCONNECTS}번 이상)")
    long = [g for g in inside if (span := longest(g)) is None or span >= LONG]
    if long:
        times = ", ".join(g.start.astimezone(KST).strftime("%H:%M:%S") for g in long)
        found.reasons.append(f"{LONG.seconds}초 이상이거나 끝 없는 장중 끊김 {len(long)}번({times})")
    return lines


# 2. Recorded bars against KIS's.


@dataclass
class KisBar:
    close: int
    volume: int


def kis_bars(path: Path, unread: Unread) -> dict[str, dict[datetime, KisBar]] | None:
    """A day's stored one-minute bars of a symbol by venue and minute; None without a file."""
    if not path.exists():
        return None
    label = f"bars {MINUTE_BARS_TR}"
    try:
        names = pq.read_schema(path).names
        absent = [c for c in _BAR_COLUMNS if c not in names]
        if absent:
            unread.files[f"{label} {' '.join(absent)} 없음(읽지 않음)"] += 1
            return {}
        table = pq.read_table(path, columns=list(_BAR_COLUMNS))
    except (pa.ArrowInvalid, OSError):
        unread.files[f"{label} 읽을 수 없음"] += 1
        return {}
    times = table.column("bar_time")
    if not pa.types.is_timestamp(times.type):
        unread.files[f"{label} bar_time 타입 {times.type}(읽지 않음)"] += 1
        return {}
    out: dict[str, dict[datetime, KisBar]] = {}
    for at, venue, close, volume in zip(*(table.column(c).to_pylist() for c in _BAR_COLUMNS), strict=True):
        if at is None or close is None or volume is None:
            continue
        out.setdefault(str(venue), {})[at.astimezone(KST)] = KisBar(int(close), int(volume))
    return out


@dataclass
class Signs:
    """What a recorded minute's trades show of why its bar may differ from KIS's (#631)."""

    skipped: int = 0  # shares the cumulative volume rose by beyond the trades' own quantities
    overlapped: int = 0  # shares the quantities hold beyond what the cumulative volume rose by
    odd: int = 0  # trades whose hour or session code is not the day's in continuous trading
    last_second: int = 0  # trades in the minute's last traded second


@dataclass
class Chain:
    """The day's trades in the order of their cumulative volume: each should add its own quantity."""

    rows: int = 0
    skips: int = 0  # steps over the trade's quantity: shares counted with no trade received for them
    overlaps: int = 0  # steps under it


@dataclass
class Recorded:
    """A symbol's recorded trades of a day: its bars as the engine builds them, and its rows."""

    bars: dict[datetime, Bar]  # by minute
    rows: int
    duplicates: int | None  # None: the files hold no cumulative volume to tell
    signs: dict[datetime, Signs] = field(default_factory=dict)  # by minute, those with any
    chain: Chain | None = None  # None: no cumulative volume
    coded: bool = False  # every file holds the hour and session codes


def _trades_of(batch: Any, unread: Unread, label: str) -> pa.Table | None:
    """A batch's trades in ``_TRADES``' types (a column the file lacks all null), the rows with a
    time, receipt, price and quantity; None if a column cannot be read so (counted in ``unread``)."""
    names = batch.schema.names
    columns = []
    for column in _TRADES:
        if column.name not in names:
            columns.append(pa.nulls(batch.num_rows, column.type))
            continue
        values = batch.column(names.index(column.name))
        try:
            columns.append(pc.cast(values, column.type))
        except (pa.ArrowInvalid, pa.ArrowNotImplementedError):
            unread.files[f"{label} {column.name} 타입 {values.type}(읽지 않음)"] += 1
            return None
    table = pa.Table.from_arrays(columns, schema=_TRADES)
    valid = [pc.is_valid(table.column(c)) for c in _TRADE_COLUMNS]
    return table.filter(functools.reduce(pc.and_, valid))


def recorded_bars(
    data_dir: Path, day: date, feed: str, symbol: str, unread: Unread, budget: Budget | None = None
) -> Recorded:
    """``symbol``'s ``feed`` trades of ``day``, built into bars in the order they were received.

    The trades stay arrow columns, not Python rows: a row a tuple of five objects, with a set of them
    for the rows recorded twice, held 2026-11-06's 1.2 million trades of 005930 in about 480 MB (#631).
    They go through the builder a slice at a time; ``budget`` is checked after each batch and slice."""
    budget = budget or Budget()
    partition = data_dir / "events" / f"date={day.isoformat()}" / f"feed={feed}" / f"symbol={symbol}"
    tables: list[pa.Table] = []
    counted = coded = True
    for batch in read_batches(partition, _TRADE_COLUMNS, unread, optional=_TRADE_OPTIONAL):
        names = batch.schema.names
        counted = counted and _CUM_VOLUME in names
        coded = coded and all(c in names for c in _CODES)
        table = _trades_of(batch, unread, f"events {feed}")
        if table is not None:
            tables.append(table)
        budget.check()
    trades = pa.concat_tables(tables) if tables else _TRADES.empty_table()
    del tables
    # By receipt, stable as ``list.sort`` was: a frame's records keep their order. The builder's
    # columns are taken in that order a slice at a time, not the whole table copied.
    received = pc.sort_indices(trades.column("received_at"))
    built = trades.select(["event_time", "price", "qty"]).combine_chunks()
    builder = BarBuilder()
    for start in range(0, trades.num_rows, _BUILD_ROWS):
        piece = built.take(received.slice(start, _BUILD_ROWS))
        times, prices, qtys = (piece.column(c).to_pylist() for c in ("event_time", "price", "qty"))
        for at, price, qty in zip(times, prices, qtys, strict=True):
            builder.trade(symbol, at, price, qty)
        budget.check()
    del received, built
    closed = builder.closed(symbol, datetime.combine(day, DAY_END, KST))
    chain, signs, duplicates = _signs(trades, counted, coded)
    budget.check()
    return Recorded(
        {b.start: b for b in closed}, trades.num_rows, duplicates, signs, chain, counted and coded
    )


def _signs(
    trades: pa.Table, counted: bool, coded: bool
) -> tuple[Chain | None, dict[datetime, Signs], int | None]:
    """Each minute's ``Signs``, the day's ``Chain`` and the rows that repeat an earlier row's exchange
    time, price, quantity and cumulative volume (of those with a cumulative volume over 0); the chain
    and the repeats None without a cumulative volume. In numpy over the exchange times' seconds, not
    a Python object a row (a hash of the four columns held about 100 MB more)."""
    signs: dict[datetime, Signs] = {}
    if not trades.num_rows:
        return (Chain(), signs, 0) if counted else (None, signs, None)

    def sign(minute: int) -> Signs:
        return signs.setdefault(datetime.fromtimestamp(minute, KST), Signs())

    seconds = pc.cast(trades.column("event_time"), pa.int64()).to_numpy()
    minutes = seconds - seconds % 60
    # The trades of each minute's last traded second.
    traded, many = np.unique(seconds, return_counts=True)
    ends = np.append(traded[1:] // 60 != traded[:-1] // 60, True)
    for second, n in zip(traded[ends].tolist(), many[ends].tolist(), strict=True):
        sign(second - second % 60).last_second = n
    if coded:
        odd = np.zeros(trades.num_rows, dtype=bool)
        for code in _CODES:
            values = trades.column(code)
            odd |= pc.fill_null(pc.not_equal(values, _usual(values)), True).to_numpy(zero_copy_only=False)
        at, n = np.unique(minutes[odd], return_counts=True)
        for minute, count in zip(at.tolist(), n.tolist(), strict=True):
            sign(minute).odd = count
    if not counted:
        return None, signs, None
    cums = pc.fill_null(trades.column(_CUM_VOLUME), 0).to_numpy()
    qtys = trades.column("qty").to_numpy()
    prices = trades.column("price").to_numpy()
    keyed = np.flatnonzero(cums > 0)
    # By cumulative volume, then time, price and quantity (a repeat lies next to what it repeats),
    # then the order read.
    order = keyed[np.lexsort((keyed, qtys[keyed], prices[keyed], seconds[keyed], cums[keyed]))]
    chain = Chain(rows=len(order))
    duplicates = 0
    if len(order) > 1:
        cum = cums[order]
        same = (cum[1:] == cum[:-1]) & (seconds[order[1:]] == seconds[order[:-1]])
        same &= (prices[order[1:]] == prices[order[:-1]]) & (qtys[order[1:]] == qtys[order[:-1]])
        duplicates = int(same.sum())
        steps = cum[1:] - cum[:-1] - qtys[order[1:]]
        chain.skips, chain.overlaps = int((steps > 0).sum()), int((steps < 0).sum())
        moved = steps != 0
        for minute, step in zip(minutes[order[1:]][moved].tolist(), steps[moved].tolist(), strict=True):
            if step > 0:
                sign(minute).skipped += step
            else:
                sign(minute).overlapped -= step
    return chain, signs, duplicates


def _usual(values: Any) -> pa.Scalar:
    """The most common of a string column's values (the first seen of those as common), null if none."""
    counts = pc.value_counts(pc.drop_null(values))
    if not len(counts):
        return pa.scalar(None, pa.string())
    best = int(pc.index(counts.field("counts"), pc.max(counts.field("counts"))).as_py())
    return counts.field("values")[best]


@dataclass
class Compared:
    """One symbol's minutes: each kind counted, and those outside its feed's gaps."""

    minutes: int = 0  # both traded
    minutes_outside: int = 0  # both traded, outside the gaps
    differing: list[datetime] = field(
        default_factory=list
    )  # of those, the minutes whose volume or close differ
    missing: list[bool] = field(default_factory=list)  # in a gap, for each
    extra: list[bool] = field(default_factory=list)
    short: list[bool] = field(default_factory=list)  # recorded volume under KIS's
    over: list[bool] = field(default_factory=list)  # over KIS's
    close: list[bool] = field(default_factory=list)
    recorded_volume: int = 0
    kis_volume: int = 0

    def outside(self, kind: list[bool]) -> int:
        return sum(not in_gap for in_gap in kind)


def _in_gap(minute: datetime, spans: Sequence[tuple[datetime, datetime | None]]) -> bool:
    end = minute + BAR
    return any(start < end and (stop is None or stop > minute) for start, stop in spans)


def compare(
    kis: Mapping[datetime, KisBar],
    recorded: Mapping[datetime, Bar],
    session: Session,
    spans: Sequence[tuple[datetime, datetime | None]],
) -> Compared:
    """KIS's bars of continuous trading against the recorded ones, minute by minute."""
    start, end = session.open, session.closing_auction
    out = Compared()
    traded = {m: b for m, b in kis.items() if start <= m < end and b.volume > 0}
    built = {m: b for m, b in recorded.items() if start <= m < end}
    for minute in sorted(traded.keys() | built.keys()):
        in_gap = _in_gap(minute, spans)
        theirs, ours = traded.get(minute), built.get(minute)
        if theirs is not None:
            out.kis_volume += theirs.volume
        if ours is not None:
            out.recorded_volume += ours.volume
        if ours is None:
            out.missing.append(in_gap)
            continue
        if theirs is None:
            out.extra.append(in_gap)
            continue
        out.minutes += 1
        out.minutes_outside += not in_gap
        differs = False
        if ours.volume < theirs.volume:
            out.short.append(in_gap)
            differs = True
        elif ours.volume > theirs.volume:
            out.over.append(in_gap)
            differs = True
        if ours.close != theirs.close:
            out.close.append(in_gap)
            differs = True
        if differs and not in_gap:
            out.differing.append(minute)
    return out


# Why a minute's bar may differ (#631). A differing minute outside the gaps is counted under each
# one it shows, and under "어느 것도 아님" if it shows none; the report says the counts only.
# - swapped: its volume is short (or over) by what a minute beside it is over (or short) by: a trade
#   on the edge of two minutes, put in the other by one side.
# - auction: the session's first minute (the opening auction's trade is in it), or a minute after one
#   KIS's bars have no trade in (a halt, such as a circuit breaker's or a VI's, resumed by an auction).
# - odd: it holds a trade whose hour code (``HOUR_CLS_CODE``) or session code (``NEW_MKOP_CLS_CODE``)
#   is not the day's most common: a trade of another kind than continuous trading's.
# - skipped: its volume is short by exactly what the trades' cumulative volume (``ACML_VOL``, in its
#   order) rose by beyond their quantities in it: trades KIS counted that never came as a record.
# - overlapped: over by exactly what the quantities hold beyond the cumulative volume's rise.
# - last_second: the volumes agree and the closes do not, with trades in the minute's last second:
#   the order of trades within a second.
CAUSES = (
    ("swapped", "분 경계(옆 분과 맞바뀜)"),
    ("auction", "단일가(개장 분, 멈춘 뒤 연 분)"),
    ("odd", "다른 시간·장운영 구분의 체결"),
    ("skipped", "누적 거래량에만 있는 체결"),
    ("overlapped", "누적 거래량보다 더 센 체결"),
    ("last_second", "같은 초의 순서(종가만)"),
    ("none", "어느 것도 아님"),
)


def causes(
    seen: Compared, kis: Mapping[datetime, KisBar], recorded: Recorded, session: Session
) -> Counter[str]:
    """The differing minutes outside the gaps, counted by the causes they show (``CAUSES``)."""
    start, end = session.open, session.closing_auction
    traded = {m: b for m, b in kis.items() if start <= m < end and b.volume > 0}

    def gap(minute: datetime) -> int | None:
        ours, theirs = recorded.bars.get(minute), traded.get(minute)
        return None if ours is None or theirs is None else ours.volume - theirs.volume

    out: Counter[str] = Counter()
    for minute in seen.differing:
        ours, theirs = recorded.bars[minute], traded[minute]
        signs = recorded.signs.get(minute, Signs())
        diff = ours.volume - theirs.volume
        shown = {
            "swapped": diff != 0 and any(gap(minute + d) == -diff for d in (-BAR, BAR)),
            "auction": minute == start or minute - BAR not in traded,
            "odd": signs.odd > 0,
            "skipped": diff < 0 and recorded.chain is not None and signs.skipped == -diff,
            "overlapped": diff > 0 and recorded.chain is not None and signs.overlapped == diff,
            "last_second": diff == 0 and signs.last_second > 1,
        }
        out.update(k for k, v in shown.items() if v)
        if not any(shown.values()):
            out["none"] += 1
    return out


def _counted(kind: list[bool]) -> str:
    """A kind's minutes, and how many of them lie outside the gaps."""
    return f"{len(kind)}(결측 구간 밖 {sum(not in_gap for in_gap in kind)})"


def bars_section(found: Found, data_dir: Path, symbols: Sequence[str], gaps: Sequence[Gap]) -> list[str]:
    """Section 2: each symbol's recorded bars against KIS's, the differing minutes by cause; their
    reasons to call an agent."""
    day, session = found.day, found.session
    lines: list[str] = []
    no_bars: list[str] = []
    for symbol in dict.fromkeys(symbols):
        by_venue = kis_bars(stock_bars_path(data_dir, day, symbol), found.unread)
        if by_venue is None:
            no_bars.append(symbol)
            lines.append(f"{symbol}: KIS 1분봉 없음(그날 파일이 아직 없다)")
            continue
        if not by_venue:
            lines.append(f"{symbol}: KIS 1분봉을 읽지 못했다(아래 스키마와 다른 파일)")
            continue
        for venue in sorted(by_venue):
            feed = TRADES_OF_VENUE.get(venue)
            if feed is None:
                lines.append(f"{symbol}: KIS 1분봉의 시장 '{venue}'(견줄 체결 피드를 모른다)")
                continue
            recorded = recorded_bars(data_dir, day, feed.tr_id, symbol, found.unread, found.budget)
            spans = [(g.start, g.end) for g in gaps if g.feed == feed.tr_id and g.key == symbol]
            seen = compare(by_venue[venue], recorded.bars, session, spans)
            lines.append(_bars_line(symbol, venue, feed.tr_id, recorded, seen))
            if seen.differing:
                lines.append(_causes_line(seen, causes(seen, by_venue[venue], recorded, session), recorded))
            _bars_reasons(found, symbol, recorded, seen)
    if no_bars:
        found.reasons.append(f"KIS 1분봉 없음: {' '.join(no_bars)}")
    return lines or ["종목 없음"]


def _bars_line(symbol: str, venue: str, tr_id: str, recorded: Recorded, seen: Compared) -> str:
    name = "통합" if venue == "UN" else "KRX"
    ratio = f"{seen.recorded_volume / seen.kis_volume:.2%}" if seen.kis_volume else "KIS 거래량 없음"
    duplicates = "모름(누적 거래량 열 없음)" if recorded.duplicates is None else f"{recorded.duplicates}"
    return (
        f"{symbol} {name}({tr_id} ↔ {MINUTE_BARS_TR} {venue}): 녹화 행 {recorded.rows:,} · 둘 다 거래한 분"
        f" {seen.minutes} · 빠진 분 {_counted(seen.missing)} · KIS에 없는 분 {_counted(seen.extra)} · 거래량"
        f" 모자람 {_counted(seen.short)} · 거래량 넘침 {_counted(seen.over)} · 종가 다름"
        f" {_counted(seen.close)} · 두 번 기록됐을 수 있는 행 {duplicates} · 녹화 거래량 ÷ KIS {ratio}"
    )


def _causes_line(seen: Compared, counted: Counter[str], recorded: Recorded) -> str:
    """The differing minutes outside the gaps by cause (#631), and the day's cumulative-volume chain."""
    chain = recorded.chain
    known = {
        "skipped": chain is not None,
        "overlapped": chain is not None,
        "odd": recorded.coded,
    }
    shown = " · ".join(f"{label} {counted[key] if known.get(key, True) else '모름'}" for key, label in CAUSES)
    line = f"  다른 분 {len(seen.differing)}개의 원인 후보(한 분이 여럿에 들 수 있다): {shown}"
    if chain is None:
        return line + " · 누적 거래량 사슬: 모름(누적 거래량 열 없음)"
    return (
        line + f" · 누적 거래량 사슬(누적 거래량 순으로 늘어놓은 체결 {chain.rows:,}행): 체결량보다 더 뛴 곳"
        f" {chain.skips:,} · 덜 늘어난 곳 {chain.overlaps:,}"
    )


def _bars_reasons(found: Found, symbol: str, recorded: Recorded, seen: Compared) -> None:
    missing = seen.outside(seen.missing)
    if missing >= MISSING_MINUTES:
        found.reasons.append(f"{symbol} 결측 구간 밖의 빠진 분 {missing}개(기준 {MISSING_MINUTES}개 이상)")
    compared, differing = seen.minutes_outside, len(seen.differing)
    if compared and differing / compared > DIFFERING:
        found.reasons.append(
            f"{symbol} 결측 구간 밖에서 거래량이나 종가가 다른 분 {differing}/{compared}"
            f"({differing / compared:.1%}, 기준 {DIFFERING:.0%} 넘게)"
        )
    if recorded.duplicates is not None and recorded.duplicates >= DUPLICATES:
        found.reasons.append(f"{symbol} 두 번 기록됐을 수 있는 행 {recorded.duplicates}개")


# 3. Frames.


@dataclass
class FeedFrames:
    """One WebSocket feed's raw frames of a day, by shape."""

    frames: int = 0
    short: int = 0
    long: int = 0
    encrypted: int = 0
    odd: int = 0  # rows not in a data frame's shape
    short_keys: set[str] = field(default_factory=set)
    first_short: datetime | None = None


def _strings(batch: Any, column: str, unread: Unread, label: str) -> Any | None:
    values = batch.column(batch.schema.get_field_index(column))
    if not (pa.types.is_string(values.type) or pa.types.is_large_string(values.type)):
        unread.files[f"{label} {column} 타입 {values.type}(읽지 않음)"] += 1
        return None
    return values


def _timestamps(batch: Any, column: str, unread: Unread, label: str) -> Any | None:
    values = batch.column(batch.schema.get_field_index(column))
    if not pa.types.is_timestamp(values.type):
        unread.files[f"{label} {column} 타입 {values.type}(읽지 않음)"] += 1
        return None
    return values


@dataclass(frozen=True)
class Shapes:
    """Arrow masks and counts over a batch's raw messages, row for row (no message is copied)."""

    data: Any  # a data frame's shape: ``<encrypted>|<tr_id>|<records>|<fields>``
    plain: Any  # a data frame not encrypted
    records: Any  # the header's record count (0 for a row of another shape)
    short: Any  # a plain frame with fewer fields than ``width`` times its records
    long: Any  # with more


def shapes(messages: Any, width: int) -> Shapes:
    """``messages``' shapes, read as ``kis.parse_message`` and ``RealtimeFrame.records`` read them,
    with arrow's string kernels: the header by a regular expression, the fields by the ``^``
    between them."""
    header = pc.extract_regex(messages, _HEADER)
    data = pc.fill_null(pc.is_valid(header), False)
    records = pc.cast(pc.if_else(data, pc.struct_field(header, "records"), "0"), pa.int64())
    plain = pc.and_(data, pc.equal(pc.struct_field(header, "encrypted"), "0"))
    fields = pc.add(pc.cast(pc.fill_null(pc.count_substring(messages, "^"), 0), pa.int64()), 1)
    expected = pc.multiply(records, width)
    short = pc.and_(plain, pc.less(fields, expected))
    long = pc.and_(plain, pc.greater(fields, expected))
    return Shapes(data, plain, records, short, long)


def _true(mask: Any) -> int:
    return int(pc.sum(pc.cast(mask, pa.int64())).as_py() or 0)


def frames_of_day(
    raw: RawDays, day: date, unread: Unread, budget: Budget | None = None
) -> tuple[dict[str, FeedFrames], int, Any]:
    """The day's WebSocket frames by feed (``WS_FEEDS``' partitions of ``raw``), and the frames the
    collector could not parse (``_unparsed``): how many and the first's receipt."""
    budget = budget or Budget()
    by_feed: dict[str, FeedFrames] = {}
    unparsed, first_unparsed = 0, None
    for tr_id in raw.feeds(day):
        feed = WS_FEEDS.get(tr_id)
        if tr_id != UNPARSED and feed is None:
            continue  # the control frames (``_system``) and the REST answers
        for partition in sorted(raw.feed(day, tr_id).glob("symbol=*")):
            key = partition.name.removeprefix("symbol=")
            for batch in read_batches(partition, (_RECEIVED, _MESSAGE), unread, batch_rows=RAW_BATCH_ROWS):
                budget.check()
                received = _timestamps(batch, _RECEIVED, unread, f"raw {tr_id}")
                if received is None:
                    continue
                if feed is None:
                    unparsed += batch.num_rows
                    earliest = pc.min(received).as_py()
                    if earliest is not None and (first_unparsed is None or earliest < first_unparsed):
                        first_unparsed = earliest
                    continue
                messages = _strings(batch, _MESSAGE, unread, f"raw {tr_id}")
                if messages is None:
                    continue
                seen = by_feed.setdefault(tr_id, FeedFrames())
                shaped = shapes(messages, len(feed.columns))
                count = _true(shaped.data)
                seen.frames += count
                seen.odd += batch.num_rows - count
                seen.encrypted += count - _true(shaped.plain)
                shorts, longs = _true(shaped.short), _true(shaped.long)
                seen.short += shorts
                seen.long += longs
                if shorts:
                    seen.short_keys.add(key)
                    first = pc.min(pc.filter(received, shaped.short)).as_py()
                    if first is not None and (seen.first_short is None or first < seen.first_short):
                        seen.first_short = first
    return by_feed, unparsed, first_unparsed


def frames_section(found: Found, raw: RawDays) -> list[str]:
    """Section 3: the day's frames by shape; their reasons to call an agent."""
    by_feed, unparsed, first_unparsed = frames_of_day(raw, found.day, found.unread, found.budget)
    day = found.day
    total = sum(f.frames for f in by_feed.values())
    lines = [f"읽은 데이터 프레임 {total:,}개(피드 {len(by_feed)}개)"]
    since = f"(첫 {_time(first_unparsed, day)})" if first_unparsed is not None else ""
    lines.append(f"해석하지 못한 프레임(_unparsed) {unparsed}개{since}")
    short = [(n, f) for n, f in sorted(by_feed.items()) if f.short]
    lines.append(
        "필드가 모자란 프레임(raw에만 남김, #381): "
        + (
            " · ".join(
                f"{n} {f.short}개(키 {len(f.short_keys)}개, 첫 {_time(f.first_short, day)})" for n, f in short
            )
            or "없음"
        )
    )
    long = [(n, f) for n, f in sorted(by_feed.items()) if f.long]
    lines.append("필드가 남는 프레임: " + (" · ".join(f"{n} {f.long}개" for n, f in long) or "없음"))
    odd = [(n, f) for n, f in sorted(by_feed.items()) if f.odd or f.encrypted]
    if odd:
        lines.append(
            "데이터 프레임 꼴이 아니거나 암호화된 행: "
            + " · ".join(f"{n} 꼴 아님 {f.odd}개, 암호화 {f.encrypted}개" for n, f in odd)
        )
    if unparsed:
        found.reasons.append(f"해석하지 못한 프레임 {unparsed}개")
    for name, feed in short:
        limit = SHORT_EXPECTED if name in SHORT_FRAMES_EXPECTED else 0
        if feed.short > limit:
            found.reasons.append(f"{name} 필드가 모자란 프레임 {feed.short}개(기준 {limit}개 넘게)")
    if long:
        found.reasons.append("필드가 남는 프레임 " + " ".join(f"{n} {f.long}개" for n, f in long))
    return lines


# 4. REST failures.


@dataclass(frozen=True)
class Polls:
    """The engine's REST polls today (``/v1/status``'s ``kis.polls``, #441), or why they are not the
    report's day's."""

    rows: tuple[Mapping[str, Any], ...] = ()
    started_at: datetime | None = None
    unknown: str | None = None


def polls_of(status: Status, day: date) -> Polls:
    """The polls from the engine's status, if its today is ``day``."""
    body = status.body
    if body is None:
        return Polls(unknown="엔진 API가 답하지 않았다(" + "; ".join(status.errors) + ")")
    kis = body.get("kis")
    if not isinstance(kis, Mapping) or not isinstance(kis.get("polls"), list):
        return Polls(unknown=f"/v1/status에 kis.polls가 없다(mode {body.get('mode')})")
    now = _parsed(body.get("now"))
    if now is None or now.astimezone(KST).date() != day:
        when = "모름" if now is None else now.astimezone(KST).strftime("%Y-%m-%d %H:%M")
        return Polls(
            unknown=f"엔진의 오늘이 {day}가 아니다(엔진 시각 {when}): 그날의 수는 자정에 0부터 다시 센다"
        )
    rows = tuple(r for r in kis["polls"] if isinstance(r, Mapping))
    return Polls(rows, _parsed(body.get("started_at")))


def _parsed(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _number(row: Mapping[str, Any], name: str) -> int:
    value = row.get(name)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def polls_section(found: Found, polls: Polls) -> list[str]:
    """Section 4: the day's REST calls and failures by endpoint; their reasons to call an agent."""
    if polls.unknown is not None:
        return [f"모른다: {polls.unknown}"]
    day = found.day
    lines = []
    if polls.started_at is not None:
        started = polls.started_at.astimezone(KST)
        late = started.date() == day and started > found.session.open - timedelta(minutes=30)
        lines.append(
            f"엔진이 {_time(started, day)}에 시작한 뒤의 수"
            + ("(장 시작 30분 전 뒤에 시작해 그 앞의 조회는 세지 않았다)" if late else "")
        )
    by_feed: dict[str, list[Mapping[str, Any]]] = {}
    for row in polls.rows:
        by_feed.setdefault(str(row.get("feed")), []).append(row)
    failures, runs = 0, []
    for feed, rows in sorted(by_feed.items()):
        attempts = sum(_number(r, "attempts") for r in rows)
        failed = sum(_number(r, "failures") for r in rows)
        kinds: Counter[str] = Counter()
        for r in rows:
            errors = r.get("errors")
            if isinstance(errors, Mapping):
                kinds.update({str(k): n for k, n in errors.items() if isinstance(n, int)})
        run = max((_number(r, "max_consecutive_failures") for r in rows), default=0)
        failing = sum(1 for r in rows if _number(r, "failures"))
        line = f"{feed}(키 {len(rows)}개): 호출 {attempts:,} · 실패 {failed}"
        if failed:
            line += (
                "(" + ", ".join(f"{k} {n}" for k, n in kinds.most_common()) + f") · 실패한 키 {failing}개"
                f" · 가장 길게 잇단 실패 {run}번"
            )
        lines.append(line)
        failures += failed
        if run >= SUSTAINED_FAILURES:
            runs.append(f"{feed} {run}번")
    lines.append(f"모두: 실패 {failures}번")
    if failures >= POLL_FAILURES:
        found.reasons.append(f"REST 실패 {failures}번(기준 {POLL_FAILURES}번 이상)")
    if runs:
        found.reasons.append(f"REST 조회가 잇달아 실패({', '.join(runs)}, 기준 {SUSTAINED_FAILURES}번)")
    return lines


# 5. The night's recording.


# The night after a trading day's session, and KIS's answers to its feeds' registrations
# (``night_probe`` reads them too, #264). The night's feeds are its trades alone: the book left with #694.
# Records received this long after 06:00 still count as the night's (a closing record).
LATE = timedelta(minutes=10)
LOOK_BACK_DAYS = 7  # the nights looked for before the one read
NIGHT_FEEDS = (NIGHT_FUTURES,)
NIGHT_TR_IDS = frozenset(f.tr_id for f in NIGHT_FEEDS)


def night_window(day: date) -> tuple[datetime, datetime]:
    """The night after ``day``'s session: 18:00 to 06:00 the next morning (and ``LATE``)."""
    start = datetime.combine(day, NIGHT_OPEN, KST)
    return start, datetime.combine(day + timedelta(days=1), time(0), KST) + NIGHT_END + LATE


def evening(at: datetime) -> date:
    """The evening whose night is going on at ``at``, or the day itself before 18:00."""
    return (at.astimezone(KST) - NIGHT_END).date()


@dataclass(frozen=True)
class Ack:
    """KIS's answer to a registration, as recorded raw."""

    received_at: datetime
    tr_id: str
    key: str
    ok: bool
    message: str


def acknowledgements(raw: RawDays, day: date) -> list[Ack]:
    """KIS's answers to the night feeds' registrations recorded on ``day``, in order."""
    root = raw.feed(day, SYSTEM)
    acks: list[Ack] = []
    for partition in sorted(root.glob("symbol=*")):
        if partition.name == "symbol=_":  # heartbeats
            continue
        for _, table in partition_batches(partition, ("received_at", "message")):
            batch = table.to_pydict()
            for at, text in zip(batch["received_at"], batch["message"], strict=True):
                try:
                    message = kis.parse_message(text)
                except kis.KisError:
                    continue
                if isinstance(message, kis.SystemMessage) and message.tr_id in NIGHT_TR_IDS:
                    acks.append(Ack(at, message.tr_id, message.tr_key or "", message.ok, message.message))
    return sorted(acks, key=lambda a: a.received_at)


def subscribed_nights(
    raw: RawDays, calendar: MarketCalendar, before: date, back: int = LOOK_BACK_DAYS
) -> list[date]:
    """The trading days of the ``back`` days before ``before`` whose night the collector had
    subscribed for when it began: KIS accepted its night trades' registration that day by
    18:00. Oldest first. A day whose files cannot be read is left out."""
    found = []
    for n in range(back, 0, -1):
        day = before - timedelta(days=n)
        if not calendar.is_trading_day(day):
            continue
        opens = datetime.combine(day, NIGHT_OPEN, KST)
        try:
            acks = acknowledgements(raw, day)
        except OSError:
            continue
        if any(a.ok and a.tr_id == NIGHT_FUTURES.tr_id and a.received_at <= opens for a in acks):
            found.append(day)
    return found


@dataclass
class Night:
    """One night feed's recording in a night's window."""

    frames: int = 0
    records: int = 0
    events: int = 0


def read_night(
    data_dir: Path,
    raw: RawDays,
    evening_of: date,
    feed: Feed,
    unread: Unread,
    events: bool,
    budget: Budget | None = None,
) -> Night:
    """The frames and records of ``feed`` received in the night after ``evening_of`` (both days'
    partitions, ``night_window``), and, if asked, its events."""
    budget = budget or Budget()
    tr_id = feed.tr_id
    start, end = night_window(evening_of)
    seen = Night()
    for partition_day in (evening_of, evening_of + timedelta(days=1)):
        for partition in sorted(raw.feed(partition_day, tr_id).glob("symbol=*")):
            for batch in read_batches(partition, (_RECEIVED, _MESSAGE), unread, batch_rows=RAW_BATCH_ROWS):
                budget.check()
                received = _timestamps(batch, _RECEIVED, unread, f"raw {tr_id}")
                messages = _strings(batch, _MESSAGE, unread, f"raw {tr_id}")
                if received is None or messages is None:
                    continue
                shaped = shapes(messages, len(feed.columns))
                inside = pc.and_(shaped.data, _between(received, start, end))
                seen.frames += _true(inside)
                seen.records += int(pc.sum(pc.if_else(inside, shaped.records, 0)).as_py() or 0)
        if not events:
            continue
        root = data_dir / "events" / f"date={partition_day.isoformat()}" / f"feed={tr_id}"
        for partition in sorted(root.glob("symbol=*")):
            for batch in read_batches(partition, (_RECEIVED,), unread):
                budget.check()
                received = _timestamps(batch, _RECEIVED, unread, f"events {tr_id}")
                if received is not None:
                    seen.events += _true(_between(received, start, end))
    return seen


def _between(values: Any, start: datetime, end: datetime) -> Any:
    low = pc.greater_equal(values, pa.scalar(start, type=values.type))
    return pc.fill_null(pc.and_(low, pc.less(values, pa.scalar(end, type=values.type))), False)


def night_section(
    found: Found, data_dir: Path, raw: RawDays, calendar: MarketCalendar
) -> tuple[str, list[str]]:
    """Section 5: the night that ended this morning against the nights before; its reasons."""
    unread = found.unread
    last = calendar.previous_trading_day(found.day)
    start, end = night_window(last)
    heading = (
        f"5. 야간 기록량 ({last}({WEEKDAYS[last.weekday()]}) {NIGHT_OPEN:%H:%M} – "
        f"{(end - LATE).astimezone(KST):%m-%d %H:%M}, 받은 시각)"
    )
    opens = datetime.combine(last, NIGHT_OPEN, KST)
    keys = sorted(
        {
            a.key
            for a in acknowledgements(raw, last)
            if a.ok and a.tr_id == NIGHT_FUTURES.tr_id and a.received_at <= opens
        }
    )
    lines = [
        f"구독: {NIGHT_FUTURES.tr_id} {' '.join(keys)}를 KIS가 {NIGHT_OPEN:%H:%M} 전에 받았다"
        if keys
        else f"구독: {NIGHT_OPEN:%H:%M} 전에 KIS가 받은 {NIGHT_FUTURES.tr_id} 구독이 없다"
    ]
    before = subscribed_nights(raw, calendar, last)
    earlier = {
        d: {
            f.tr_id: read_night(data_dir, raw, d, f, unread, events=False, budget=found.budget)
            for f in NIGHT_FEEDS
        }
        for d in before
    }
    for feed in NIGHT_FEEDS:
        name = "체결"
        seen = read_night(data_dir, raw, last, feed, unread, events=True, budget=found.budget)
        lost = seen.records - seen.events
        counts = [earlier[d][feed.tr_id].records for d in before if earlier[d][feed.tr_id].records]
        median = statistics.median(counts) if counts else None
        share = (
            f"앞선 밤 {len(counts)}개 중앙값의 {seen.records / median:.0%}" if median else "견줄 앞선 밤 없음"
        )
        lines.append(
            f"{feed.tr_id} {name}: 프레임 {seen.frames:,} · 레코드 {seen.records:,} · events {seen.events:,}"
            f"(정규화하지 못한 레코드 {max(lost, 0):,}) · {share}"
        )
        if not seen.records:
            found.reasons.append(
                f"야간 {name}({feed.tr_id}) 기록 없음({last} 밤. KRX가 쉰 밤(긴 연휴 전날)이면 그대로 둔다)"
            )
        elif median and seen.records < NIGHT_SHARE * median:
            found.reasons.append(
                f"야간 {name}({feed.tr_id}) 레코드가 앞선 밤 중앙값의 {seen.records / median:.0%}"
                f"(기준 {NIGHT_SHARE:.0%} 미만)"
            )
        if lost > 0:
            found.reasons.append(f"야간 {name}({feed.tr_id}) 정규화하지 못한 레코드 {lost:,}개")
    shown = before[-NIGHT_BASELINE:]
    lines.append(
        f"앞선 밤({NIGHT_OPEN:%H:%M} 전에 구독한 밤, 앞 7일): "
        + (" / ".join(f"{d} 체결 {earlier[d][NIGHT_FUTURES.tr_id].records:,}" for d in shown) or "없음")
    )
    return heading, lines


# The report.


def _time(at: datetime | None, day: date) -> str:
    if at is None:
        return "모름"
    local = at.astimezone(KST)
    return local.strftime("%H:%M:%S" if local.date() == day else "%m-%d %H:%M:%S")


def report_day(calendar: MarketCalendar, now: datetime) -> date:
    """The trading day a run at ``now`` reports: the night's (to 06:00 the next morning) once its
    bars are final (20:30, ``backfill.BARS_FINAL_AT``), else the trading day before."""
    local = now.astimezone(KST)
    day = evening(local)
    if calendar.is_trading_day(day) and local >= datetime.combine(day, BARS_FINAL_AT, KST):
        return day
    return calendar.previous_trading_day(day)


def measure(
    data_dir: Path,
    calendar: MarketCalendar,
    day: date,
    symbols: Sequence[str],
    polls: Polls,
    raw: RawDays | None = None,
    budget: Budget | None = None,
) -> Found | None:
    """Every section of ``day``'s report, with its reasons; None if ``day`` does not trade. ``raw``
    reads the raw days (by default the data directory's, a pruned one failing). With a bound,
    ``budget`` keeps each section's peak, and a peak over the bound stops the run in the section it
    reaches it in: the sections before it stay, and a reason says where it stopped (#631)."""
    session = calendar.session(day)
    if session is None:
        return None
    raw = RawDays(data_dir) if raw is None else raw
    found = Found(day, session, budget=budget or Budget())
    gaps = read_gaps(data_dir, day, found.unread)
    span = f"{session.open:%H:%M}–{session.close:%H:%M}"
    continuous = f"{session.open:%H:%M}–{(session.closing_auction - BAR):%H:%M}"
    steps: tuple[tuple[str, Callable[[], tuple[str, list[str]]]], ...] = (
        ("1", lambda: (f"1. 장중 끊김 (gaps, {span})", disconnects_section(found, gaps))),
        (
            "2",
            lambda: (
                f"2. 녹화한 1분봉과 KIS 1분봉 (연속매매 {continuous} 봉, 봉 시각은 그 분의 시작)",
                bars_section(found, data_dir, symbols, gaps),
            ),
        ),
        ("3", lambda: ("3. 프레임 (raw, 그날 받은 것)", frames_section(found, raw))),
        ("4", lambda: ("4. REST 조회 실패 (/v1/status의 kis.polls)", polls_section(found, polls))),
        ("5", lambda: night_section(found, data_dir, raw, calendar)),
    )
    for number, step in steps:
        done = len(found.sections)
        try:
            with found.budget.section(number):
                found.sections.append(step())
        except OverMemory as over:
            read = len(found.sections) > done  # over only once the section was read
            if read and number == steps[-1][0]:
                break  # nothing is left unread: the run's own section says the peak (``ran``)
            found.stopped = number
            bound = f"기준 {found.budget.bound_mb:.0f} MB"
            where = f"{number}절을 {'읽은 뒤' if read else '읽다가'} 최대 RSS {over.peak_mb:.0f} MB"
            unread = "그 뒤 절은" if read else "이 절과 그 뒤 절은"
            found.sections.append(
                (
                    f"{int(number) + 1 if read else number}. 메모리 기준 넘음",
                    [f"{where}({bound}): {unread} 읽지 않았다"],
                )
            )
            found.reasons.append(f"메모리 기준 넘음: {where}({bound} 넘게), 그 뒤는 읽지 않았다")
            break
        give_back()  # a section's memory freed goes back to the system before the next one reads
    return found


def ran(found: Found, seconds: float, peak_mb: float | None) -> None:
    """The run's own section: its time, peak memory and each section's, and the reason if the peak is
    over its bound (once: a run the bound stopped has said so)."""
    peak = "모름" if peak_mb is None else f"{peak_mb:.0f} MB"
    lines = [f"{seconds:.1f}초 · 최대 RSS {peak}(기준 {MEMORY_MB:.0f} MB)"]
    budget = found.budget
    if budget.peaks:
        each = " · ".join(
            f"{name if name == '시작' else name + '절'} {'모름' if mb is None else f'{mb:.0f} MB'}"
            for name, mb in budget.peaks.items()
        )
        whose = "프로세스가 시작한 뒤의 최대라 절마다 나뉘지 않는다" if budget.since_start else "그 절의 최대"
        lines.append(f"절마다 최대 RSS({whose}): {each}")
    found.sections.append(("6. 실행", lines))
    if found.stopped is None and peak_mb is not None and peak_mb > MEMORY_MB:
        found.reasons.append(f"최대 메모리 {peak_mb:.0f} MB(기준 {MEMORY_MB:.0f} MB 넘게)")


def render(found: Found) -> str:
    day = found.day
    reasons = (
        "; ".join(found.reasons) if found.reasons else f"{NO_CALL}(기준 안: 기록만, 라벨을 바꾸지 않는다)"
    )
    out = [
        f"{TITLE}: {day}({WEEKDAYS[day.weekday()]})",
        AGENT + reasons,
        *NOTES,
        "",
    ]
    for heading, lines in found.sections:
        out += [heading, *(f"  {line}" for line in lines), ""]
    if found.unread.files:
        out += [
            "스키마와 다른 파일(열이 없거나 타입이 다르다)",
            *(f"  {x}" for x in found.unread.lines()),
            "",
        ]
    return "\n".join(out).rstrip("\n")


def report(
    data_dir: Path,
    calendar: MarketCalendar,
    day: date,
    symbols: Sequence[str],
    polls: Polls,
    raw: RawDays | None = None,
) -> str:
    """``day``'s report without its run's own section (``recording_quality`` adds it)."""
    found = measure(data_dir, calendar, day, symbols, polls, raw)
    if found is None:
        return f"{TITLE}: {day}는 거래일이 아니다"
    return render(found)


async def recording_quality(settings: Settings, now: datetime, day: date | None = None) -> str:
    """The command: the engine's polls read through its API, then the files (a pruned raw day's from
    the Object Store)."""
    started = monotonic()
    calendar = calendar_store(settings).calendar
    day = report_day(calendar, now) if day is None else day
    async with aiohttp.ClientSession() as session:
        status = await fetch_status(session, api_candidates(settings))
    symbols = [*settings.symbols, *settings.etf_symbols]
    budget = Budget(MEMORY_MB)
    with RawDays(settings.data_dir, open_store(settings)) as raw:
        found = measure(settings.data_dir, calendar, day, symbols, polls_of(status, day), raw, budget)
    if found is None:
        return f"{TITLE}: {day}는 거래일이 아니다"
    ran(found, monotonic() - started, budget.peak_mb())
    return render(found)
