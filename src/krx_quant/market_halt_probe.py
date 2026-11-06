"""``krx-quant probe-market-halt``: what the recorded frames say of a market-wide halt (#625).

On 2026-11-06 a circuit breaker halted the whole market from 09:03:33: the books came back twenty
minutes later (the single-price auction's orders) and the trades ten minutes after that. The engine
knew of it only by the exchange's rules, from the KOSPI index (``special.py``): late, and with an end
that was not the reopening's. It alerted on each silent feed as an outage. Before the engine reads a
halt from the feeds (CODING_STANDARDS.md "External data is probed before it is used"), a scheduled
job (``probe-market-halt-625``) reads that morning's raw frames and posts on #625 which fields of the
feeds already registered say that trading stopped and came back, and when their values changed.

For a day's window (by receipt, KST) it prints:

1. the registrations KIS answered that day (``raw``, ``_system``), by TR, and whether a
   market-operation feed (``MARKET_OPERATION``) was registered or recorded;
2. the records received each minute, by feed;
3. each key's records and its longest silence: the records before and after it, each with the time
   it carries;
4. each feed's code and flag fields (``LAYOUTS``), by key: the values in the order received, each
   run with its first and last receipt. Two more are made here from the trades' and the books' best
   quotes and the books' expected fill, neither a price: whether both sides are quoted (and whether
   they touch or cross), and whether an expected fill is given;
5. the REST answers recorded in the window, by TR and key, and the VI status's rows;
6. when the exchange's rules call a breaker and a sidecar from the KOSPI index's and the KOSPI200
   futures' ticks as the engine reads them, without the five minutes after the open the engine
   leaves out;
7. the run's time and peak memory.

It prints field names, code values, counts and times: no price, quantity, index or other market
value, so its output can go on the public issue. It reads files only, beside the running collector
(a raw day the archive pruned on the server, #607, is read back from the Object Store:
``raw_days.RawDays``), and writes nothing. Its exit code is 0, or 3 when no record was received in
the window.
"""

from __future__ import annotations

import json
import math
import time as clock
from array import array
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pyarrow.compute as pc

from krx_quant import kis
from krx_quant.archive import partition_batches
from krx_quant.config import Settings
from krx_quant.feeds import (
    BOOK,
    INDEX,
    KOSPI200_FUTURES,
    KRX_BOOK,
    KRX_TRADE,
    NXT_TRADE,
    OPTION,
    PROGRAM,
    RAW_FEEDS,
    RAW_SCHEMA,
    TRADE,
    US_TRADE,
    USD_FUTURES,
    VI_STATUS,
    WS_FEEDS,
    Feed,
    RawFeed,
)
from krx_quant.feeds.base import KST, RECEIVED_AT, to_float, to_int
from krx_quant.feeds.stock import KRX_BOOK_OLD_WIDTH
from krx_quant.footprint import Meter
from krx_quant.inverse_book_probe import acknowledgements
from krx_quant.object_store import open_store
from krx_quant.raw_days import RawDays
from krx_quant.special import BREAKER_STAGES, HOLD, KOSPI, SIDECAR_MOVE

TITLE = "시장 전체 매매 정지 확인 (#625)"
NO_RECORDS = 3  # the exit code when no record was received in the window

# KIS's market-operation feeds (국내주식 장운영정보: KRX, integrated, NXT), which say a halt in
# fields of their own. None is in the subscription plan (``collector.subscription_plan``): the
# session's 41 registrations are all used (docs/kis-feeds.md "구독 계획").
MARKET_OPERATION = ("H0STMKO0", "H0UNMKO0", "H0NXMKO0")
SYSTEM, UNPARSED = "_system", "_unparsed"
# Raw frames are read this many rows a batch, as the recording quality report reads them
# (``recording_quality.RAW_BATCH_ROWS``: a larger batch only raises the peak memory).
BATCH_ROWS = 8_192
MAX_RUNS = 12  # a field's runs of one value shown in order; with more, its values are counted
MAX_HOLDS = 5  # the stretches a rule's condition held shown
UNREAD = "?"  # a field of a record that is not in its feed's layout, or not a number where one is due
ABSENT = "(없음)"  # a field the record's layout does not have
BEST = "1호가(만든 값)"
EXPECTED = "예상 체결가(만든 값)"
_US = 1_000_000


@dataclass(frozen=True)
class Layout:
    """How a feed's records are read here: by the names of its columns."""

    columns: tuple[str, ...]
    widths: tuple[int, ...]  # the fields a record may have
    own_time: str | None = None  # the field that carries the record's time of day
    codes: tuple[str, ...] = ()  # the code and flag fields shown
    best: tuple[str, str] | None = None  # the best ask's and bid's fields (``BEST``)
    expected: str | None = None  # the expected fill's price field (``EXPECTED``)
    change: str | None = None  # the change from the previous close (%): what the rules read

    @property
    def names(self) -> tuple[str, ...]:
        """The fields shown over time: the codes, then the ones made here."""
        made = ((BEST,) if self.best else ()) + ((EXPECTED,) if self.expected else ())
        return self.codes + made

    def at(self, name: str) -> int:
        return self.columns.index(name)


def _layout(
    feed: Feed | RawFeed,
    *,
    old_width: int | None = None,
    own_time: str | None = None,
    codes: tuple[str, ...] = (),
    best: tuple[str, str] | None = None,
    expected: str | None = None,
    change: str | None = None,
) -> Layout:
    widths = (len(feed.columns),) if old_width is None else (len(feed.columns), old_width)
    return Layout(feed.columns, widths, own_time, codes, best, expected, change)


def _trades(feed: Feed) -> Layout:
    """The trades' fields: the market-operation code (신 장운영 구분), the halt flag (거래정지
    여부), the time code (시간 구분), ``MRKT_TRTM_CLS_CODE`` (KIS's example of the market-operation
    feed, where the same name is, glosses it 임의연장 구분), the venue code and the trade's side
    code (체결 구분: 1 a buy, 3 before the open, 5 a sell). What their values mean is not measured:
    this probe is the first look."""
    return _layout(
        feed,
        own_time="STCK_CNTG_HOUR",
        codes=(
            "NEW_MKOP_CLS_CODE",
            "TRHT_YN",
            "HOUR_CLS_CODE",
            "MRKT_TRTM_CLS_CODE",
            "MARKET_CLS_CODE",
            "CNTG_CLS_CODE",
        ),
        best=("ASKP1", "BIDP1"),
    )


def _derivatives(feed: Feed, change: str | None = None) -> Layout:
    """The futures' and options' one flag: whether the dynamic price limit is on."""
    return _layout(feed, own_time="bsop_hour", codes=("dynm_prc_limt_yn",), change=change)


# Every feed recorded from the WebSocket (``feeds.WS_FEEDS``, ``feeds.RAW_FEEDS``), in the order
# shown. A feed without code fields is counted only; one not named here (the member flow, the night
# futures, which send nothing in the day session) is counted without the time its records carry.
LAYOUTS: dict[str, Layout] = {
    TRADE.tr_id: _trades(TRADE),
    KRX_TRADE.tr_id: _trades(KRX_TRADE),
    NXT_TRADE.tr_id: _trades(NXT_TRADE),
    BOOK.tr_id: _layout(
        BOOK,
        own_time="BSOP_HOUR",
        codes=(
            "HOUR_CLS_CODE",
            "STCK_DEAL_CLS_CODE",
            "KMID_CLS_CODE",
            "NMID_CLS_CODE",
            "ANTC_EXCH_CLS_CODE",
        ),
        best=("ASKP1", "BIDP1"),
        expected="ANTC_CNPR",
    ),
    KRX_BOOK.tr_id: _layout(
        KRX_BOOK,
        old_width=KRX_BOOK_OLD_WIDTH,
        own_time="BSOP_HOUR",
        codes=("HOUR_CLS_CODE", "STCK_DEAL_CLS_CODE", "MIDP_CLS_CODE", "MARKET_CLS_CODE"),
        best=("ASKP1", "BIDP1"),
        expected="ANTC_CNPR",
    ),
    INDEX.tr_id: _layout(INDEX, own_time="bsop_hour", change="prdy_ctrt"),
    KOSPI200_FUTURES.tr_id: _derivatives(KOSPI200_FUTURES, change="futs_prdy_ctrt"),
    USD_FUTURES.tr_id: _derivatives(USD_FUTURES),
    OPTION.tr_id: _derivatives(OPTION),
    PROGRAM.tr_id: _layout(PROGRAM, own_time="STCK_CNTG_HOUR"),
    US_TRADE.tr_id: _layout(US_TRADE, own_time="KHMS"),
}
_RECORDED: tuple[Feed | RawFeed, ...] = (*WS_FEEDS.values(), *RAW_FEEDS.values())
for _feed in _RECORDED:
    LAYOUTS.setdefault(_feed.tr_id, _layout(_feed))


@dataclass
class Series:
    """A key's records received in the window, in the order received once ``sort`` is called."""

    feed: str
    key: str
    received: array[int] = field(default_factory=lambda: array("q"))  # µs since the epoch
    own: array[int] = field(default_factory=lambda: array("q"))  # the record's HHMMSS; -1: none
    state: array[int] = field(default_factory=lambda: array("L"))  # which of the feed's ``states``
    change: array[float] = field(default_factory=lambda: array("d"))  # NaN: not read
    frames: int = 0  # data frames
    odd: int = 0  # rows that are not a data frame in the feed's layout

    def __len__(self) -> int:
        return len(self.received)

    def add(self, at: int, own: int, state: int, change: float = math.nan) -> None:
        self.received.append(at)
        self.own.append(own)
        self.state.append(state)
        self.change.append(change)

    def sort(self) -> None:
        """Put the records in the order received. A partition's files are read in the order written
        (``archive.partition_files``), which is that order; this holds where it is not (two parts
        of one modification time are read by name)."""
        times = self.received
        if all(times[i] <= times[i + 1] for i in range(len(times) - 1)):
            return
        order = sorted(range(len(times)), key=times.__getitem__)
        self.received = array("q", (self.received[i] for i in order))
        self.own = array("q", (self.own[i] for i in order))
        self.state = array("L", (self.state[i] for i in order))
        self.change = array("d", (self.change[i] for i in order))


class States:
    """A feed's distinct values of its shown fields, each a tuple in ``Layout.names``' order."""

    def __init__(self) -> None:
        self.values: list[tuple[str, ...]] = []
        self._index: dict[tuple[str, ...], int] = {}

    def of(self, values: tuple[str, ...]) -> int:
        found = self._index.get(values)
        if found is None:
            found = self._index[values] = len(self.values)
            self.values.append(values)
        return found


@dataclass
class Found:
    """What the window's raw rows held."""

    day: date
    start: datetime
    end: datetime
    series: dict[tuple[str, str], Series] = field(default_factory=dict)  # the WebSocket feeds'
    rest: dict[tuple[str, str], Series] = field(default_factory=dict)  # the REST answers'
    states: dict[str, States] = field(default_factory=dict)  # by feed
    unparsed: int = 0  # frames the collector could not parse
    folders: list[str] = field(default_factory=list)  # the feeds recorded raw that day

    @property
    def records(self) -> int:
        return sum(len(s) for s in self.series.values())


def _rows(partition: Path, start: datetime, end: datetime) -> Iterator[tuple[int, str]]:
    """A raw partition's rows received from ``start`` to before ``end``: (received in µs, message),
    each row once (``archive.partition_batches``)."""
    for _, batch in partition_batches(partition, RAW_SCHEMA.names, batch_rows=BATCH_ROWS):
        received = batch.column("received_at").cast(RECEIVED_AT)
        inside = pc.fill_null(pc.and_(pc.greater_equal(received, start), pc.less(received, end)), False)
        if not pc.any(inside).as_py():
            continue
        times = pc.filter(received, inside).cast("int64").to_pylist()
        yield from zip(times, pc.filter(batch.column("message"), inside).to_pylist(), strict=True)


def _own(text: str) -> int:
    """A record's time of day as a number (HHMMSS); -1 for anything else."""
    text = text.strip()
    return int(text) if len(text) == 6 and text.isdigit() else -1


def best_quotes(ask: str, bid: str) -> str:
    """Whether a record quotes both sides, and whether they touch or cross: no price."""
    try:
        asked, offered = to_int(ask), to_int(bid)
    except ValueError:
        return UNREAD
    if asked > 0 and offered > 0:
        return "양쪽" if asked > offered else "맞닿음" if asked == offered else "뒤집힘"
    return "한쪽" if asked > 0 or offered > 0 else "없음"


def _expected(price: str) -> str:
    try:
        return "있음" if to_int(price) > 0 else "없음"
    except ValueError:
        return UNREAD


def _change(text: str) -> float:
    try:
        return to_float(text)
    except ValueError:
        return math.nan


class _Reader:
    """A feed's records, read by its layout into a key's series."""

    def __init__(self, layout: Layout, states: States) -> None:
        self._layout = layout
        self._states = states
        self._codes = [layout.at(name) for name in layout.codes]
        self._own = None if layout.own_time is None else layout.at(layout.own_time)
        self._best = None if layout.best is None else (layout.at(layout.best[0]), layout.at(layout.best[1]))
        self._expected = None if layout.expected is None else layout.at(layout.expected)
        self._change = None if layout.change is None else layout.at(layout.change)
        self._unread = states.of((UNREAD,) * len(layout.names))

    def take(self, series: Series, at: int, text: str) -> None:
        try:
            frame = kis.parse_message(text)
        except kis.KisError:
            series.odd += 1
            return
        if not isinstance(frame, kis.RealtimeFrame) or frame.encrypted or frame.count < 1:
            series.odd += 1
            return
        series.frames += 1
        fields = frame.payload.split("^")
        width, left = divmod(len(fields), frame.count)
        if left or width not in self._layout.widths:
            series.odd += 1  # another layout: its records are counted, their fields not read
            for _ in range(frame.count):
                series.add(at, -1, self._unread)
            return
        for i in range(frame.count):
            record = fields[i * width : (i + 1) * width]
            values = [record[j] if j < width else ABSENT for j in self._codes]
            if self._best is not None:
                values.append(best_quotes(record[self._best[0]], record[self._best[1]]))
            if self._expected is not None:
                values.append(_expected(record[self._expected]))
            series.add(
                at,
                -1 if self._own is None else _own(record[self._own]),
                self._states.of(tuple(values)),
                math.nan if self._change is None else _change(record[self._change]),
            )


def _vi_rows(text: str) -> str:
    """A VI status answer's rows and how many of them are on (``vi_cls_code`` not ``N``, as the
    engine reads it): no time or price of theirs."""
    try:
        rows = json.loads(text)["body"].get("output") or []
        if isinstance(rows, dict):
            rows = [rows]
        on = sum(1 for r in rows if r.get("vi_cls_code", "N") != "N")
    except (ValueError, KeyError, TypeError, AttributeError):
        return UNREAD
    return f"{len(rows)}행, 걸린 것 {on}개"


def read(raw: RawDays, day: date, start: time, end: time) -> Found:
    """``day``'s raw rows received from ``start`` to before ``end`` (KST)."""
    if start >= end:
        raise ValueError(f"the window's start ({start}) is not before its end ({end})")
    found = Found(day, datetime.combine(day, start, KST), datetime.combine(day, end, KST))
    found.folders = raw.feeds(day)
    for tr_id in found.folders:
        if tr_id == SYSTEM:
            continue  # KIS's answers to the registrations: the whole day's, read apart
        layout = LAYOUTS.get(tr_id)
        states = found.states.setdefault(tr_id, States())
        reader = None if layout is None else _Reader(layout, states)
        for partition in sorted(raw.feed(day, tr_id).glob("symbol=*")):
            key = partition.name.removeprefix("symbol=")
            series = Series(tr_id, key)
            for at, text in _rows(partition, found.start, found.end):
                if tr_id == UNPARSED:
                    found.unparsed += 1
                elif reader is not None:
                    reader.take(series, at, text)
                else:  # a REST answer
                    series.frames += 1
                    series.add(at, -1, states.of((_vi_rows(text),)) if tr_id == VI_STATUS.tr_id else 0)
            series.sort()
            if tr_id != UNPARSED:
                (found.rest if reader is None else found.series)[(tr_id, key)] = series
    return found


# --- the report ----------------------------------------------------------------------------------


def _at(us: int) -> str:
    """A receipt time, to the millisecond, in KST."""
    local = datetime.fromtimestamp(us // _US, KST)
    return f"{local:%H:%M:%S}.{us % _US // 1000:03d}"


def _hms(own: int) -> str:
    text = f"{own:06d}"
    return f"{text[:2]}:{text[2:4]}:{text[4:]}"


def _record(series: Series, i: int) -> str:
    """A record's receipt, with the time it carries if it carries one."""
    own = series.own[i]
    return _at(series.received[i]) + (f"(자체 {_hms(own)})" if own >= 0 else "")


def _value(value: str) -> str:
    return f"'{value}'" if value.strip() else "(빈 값)"


def _order(feeds: Sequence[str]) -> list[str]:
    """``feeds`` in the layouts' order, the others after them by name."""
    known = list(LAYOUTS)
    return sorted(set(feeds), key=lambda f: (known.index(f) if f in known else len(known), f))


def ack_lines(raw: RawDays, found: Found) -> list[str]:
    """Section 1: the registrations KIS answered on the day, and the market-operation feeds."""
    acks = acknowledgements(raw, found.day)
    accepted: dict[str, set[str]] = {}
    refused: dict[tuple[str, str], str] = {}
    for ack in acks:
        if ack.ok:
            accepted.setdefault(ack.tr_id, set()).add(ack.key)
        else:
            refused[(ack.tr_id, ack.key)] = ack.message
    total = sum(len(keys) for keys in accepted.values())
    lines = [
        f"KIS가 성공으로 답한 구독: TR {len(accepted)}개, 키 {total}건"
        + (
            ": " + ", ".join(f"{tr_id} {len(accepted[tr_id])}" for tr_id in _order(list(accepted)))
            if accepted
            else ""
        )
    ]
    lines += [f"거절: {tr_id} {key} ({message})" for (tr_id, key), message in sorted(refused.items())]
    for tr_id in MARKET_OPERATION:
        answered = "구독 응답 " + ("있음" if any(a.tr_id == tr_id for a in acks) else "없음")
        recorded = "raw 폴더 " + ("있음" if tr_id in found.folders else "없음")
        lines.append(f"장운영정보 {tr_id}: {answered} · {recorded}")
    return lines


def minute_lines(found: Found) -> list[str]:
    """Section 2: the records received each minute, by feed."""
    start = int(found.start.timestamp()) * _US
    minutes = math.ceil((found.end - found.start) / timedelta(minutes=1))
    counts: dict[str, list[int]] = {}
    for (tr_id, _), series in found.series.items():
        if len(series):
            row = counts.setdefault(tr_id, [0] * minutes)
            for at in series.received:
                row[(at - start) // (60 * _US)] += 1
    if not counts:
        return ["받은 레코드 없음"]
    feeds = _order(list(counts))
    width = max(9, *(len(f) + 1 for f in feeds))
    lines = ["시각 ".ljust(6) + "".join(f.rjust(width) for f in feeds)]
    for m in range(minutes):
        label = f"{found.start + timedelta(minutes=m):%H:%M}"
        lines.append(label.ljust(6) + "".join(f"{counts[f][m]:,}".rjust(width) for f in feeds))
    lines.append("합".ljust(5) + "".join(f"{sum(counts[f]):,}".rjust(width) for f in feeds))
    silent = [f for f in _order([tr_id for tr_id, _ in found.series]) if f not in counts]
    if silent:
        lines.append("창 안에 레코드가 없는 피드: " + ", ".join(silent))
    return lines


def longest_silence(series: Series, start: int, end: int) -> tuple[float, int | None, int | None]:
    """A key's longest silence in the window, in seconds, with the record before it and the one
    after it (None: the window's start or end)."""
    times = series.received
    if not len(times):
        return (end - start) / _US, None, None
    longest: tuple[float, int | None, int | None] = ((times[0] - start) / _US, None, 0)
    for i in range(len(times) - 1):
        if (gap := (times[i + 1] - times[i]) / _US) > longest[0]:
            longest = (gap, i, i + 1)
    if (gap := (end - times[-1]) / _US) > longest[0]:
        longest = (gap, len(times) - 1, None)
    return longest


def _silence(series: Series, found: Found) -> str:
    start, end = int(found.start.timestamp()) * _US, int(found.end.timestamp()) * _US
    seconds, before, after = longest_silence(series, start, end)
    left = "창 시작" if before is None else _record(series, before)
    right = "창 끝" if after is None else _record(series, after)
    return f"가장 긴 침묵 {seconds:,.1f}초: {left} → {right}"


def key_lines(found: Found) -> list[str]:
    """Section 3: each key's records and its longest silence."""
    lines = []
    for tr_id in _order([tr_id for tr_id, _ in found.series]):
        for (feed, key), series in sorted(found.series.items()):
            if feed != tr_id:
                continue
            if not len(series) and not series.odd:
                lines.append(f"{tr_id} {key}: 0건")
                continue
            odd = f", 배치가 다르거나 데이터 프레임이 아닌 행 {series.odd}개" if series.odd else ""
            frames = f"프레임 {series.frames:,}개{odd}"
            lines.append(f"{tr_id} {key}: {len(series):,}건({frames}) · {_silence(series, found)}")
    lines.append(f"해석하지 못한 프레임({UNPARSED}): {found.unparsed}개")
    return lines


@dataclass(frozen=True)
class Run:
    """Records in a row with one value of a field."""

    value: str
    first: int  # receipts, µs
    last: int
    count: int


def runs(series: Series, values: Sequence[str]) -> list[Run]:
    """``series``' runs of one value, in the order received; ``values`` is the field's value in
    each of the feed's states."""
    out: list[Run] = []
    state, received = series.state, series.received
    value, first, count = "", 0, 0
    for i in range(len(state)):
        now = values[state[i]]
        if count and now == value:
            count += 1
            continue
        if count:
            out.append(Run(value, first, received[i - 1], count))
        value, first, count = now, received[i], 1
    if count:
        out.append(Run(value, first, received[-1], count))
    return out


def _runs_text(found: list[Run]) -> str:
    if len(found) <= MAX_RUNS:
        return " → ".join(f"{_value(r.value)} {_at(r.first)}–{_at(r.last)} {r.count:,}건" for r in found)
    counts: Counter[str] = Counter()
    spans: dict[str, tuple[int, int]] = {}
    for r in found:
        counts[r.value] += r.count
        first, _ = spans.get(r.value, (r.first, r.last))
        spans[r.value] = (first, r.last)
    values = " · ".join(
        f"{_value(v)} {n:,}건({_at(spans[v][0])}–{_at(spans[v][1])})" for v, n in counts.most_common()
    )
    return f"값이 {len(found) - 1:,}번 바뀜: {values}"


def field_lines(series: Sequence[Series], names: Sequence[str], states: States) -> list[str]:
    """One feed's shown fields: each field's runs by key; a field with one value on every key in a
    line."""
    lines: list[str] = []
    heard = [s for s in series if len(s)]
    for j, name in enumerate(names):
        values = [state[j] for state in states.values]
        by_key = [(s.key, runs(s, values)) for s in heard]
        only = {r.value for _, found in by_key for r in found}
        feed = heard[0].feed
        if len(only) == 1:
            lines.append(f"{feed} {name}: 모든 키({len(heard)}개)에서 {_value(next(iter(only)))}뿐")
            continue
        lines.append(f"{feed} {name}")
        lines += [f"  {key}: {_runs_text(found)}" for key, found in by_key]
    return lines


def code_lines(found: Found) -> list[str]:
    """Section 4: the code and flag fields' values over time."""
    lines: list[str] = []
    for tr_id in _order([tr_id for tr_id, _ in found.series]):
        names = LAYOUTS[tr_id].names
        series = [s for (feed, _), s in sorted(found.series.items()) if feed == tr_id and len(s)]
        if names and series:
            lines += field_lines(series, names, found.states[tr_id])
    return lines or ["코드 필드가 있는 피드의 레코드 없음"]


def rest_lines(found: Found) -> list[str]:
    """Section 5: the REST answers recorded in the window."""
    lines = []
    for (tr_id, key), series in sorted(found.rest.items()):
        line = f"{tr_id} {key}: 답 {len(series):,}개"
        if len(series):
            line += f" · {_silence(series, found)}"
        if tr_id == VI_STATUS.tr_id and len(series):
            values = [state[0] for state in found.states[tr_id].values]
            line += f" · 행: {_runs_text(runs(series, values))}"
        lines.append(line)
    return lines or ["기록된 REST 답 없음"]


@dataclass(frozen=True)
class Hold:
    """A stretch of ticks a rule's condition was met on."""

    since: int  # the first tick it was met on
    called: int | None  # the first tick a minute (``special.HOLD``) or more after that one, still met
    until: int | None  # the first tick it was not met on; None: met to the last tick


def holds(times: Sequence[datetime], met: Sequence[bool]) -> list[Hold]:
    """The stretches ``met`` held over ticks at ``times``, as ``special._Held`` follows one."""
    out: list[Hold] = []
    since: int | None = None
    called: int | None = None
    for i, ok in enumerate(met):
        if not ok:
            if since is not None:
                out.append(Hold(since, called, i))
            since = called = None
            continue
        if since is None:
            since = i
        if called is None and times[i] - times[since] >= HOLD:
            called = i
    if since is not None:
        out.append(Hold(since, called, None))
    return out


def _ticks(series: Series, day: date) -> tuple[list[int], list[datetime]]:
    """The records with a change read, and each one's time as the engine takes it
    (``feeds.base.kst_or_received``): the time it carries on ``day``, else the second received."""
    kept, times = [], []
    for i in range(len(series)):
        if math.isnan(series.change[i]):
            continue
        own = series.own[i]
        try:
            at = datetime.combine(day, time(own // 10000, own // 100 % 100, own % 100), KST)
        except ValueError:
            at = datetime.fromtimestamp(series.received[i] // _US, KST)
        kept.append(i)
        times.append(at)
    return kept, times


def _rule(series: Series, day: date, name: str, met: Sequence[bool]) -> str:
    kept, times = _ticks(series, day)
    found = holds(times, [met[i] for i in kept])
    if not found:
        return f"{name}: 창 안에 없음"
    parts = []
    for hold in found[:MAX_HOLDS]:
        until = "마지막 틱까지" if hold.until is None else f"{times[hold.until]:%H:%M:%S} 전까지"
        part = f"{times[hold.since]:%H:%M:%S}(받음 {_at(series.received[kept[hold.since]])})부터 {until}"
        if hold.called is None:
            part += ", 1분을 채운 틱 없음"
        else:
            part += (
                f", 부르는 시각 {times[hold.since] + HOLD:%H:%M:%S}"
                f"(그 틱 {times[hold.called]:%H:%M:%S}, 받음 {_at(series.received[kept[hold.called]])})"
            )
        parts.append(part)
    more = f" · 그 밖 {len(found) - MAX_HOLDS}번" if len(found) > MAX_HOLDS else ""
    return f"{name}: " + " · ".join(parts) + more


def rule_lines(found: Found) -> list[str]:
    """Section 6: when the exchange's rules call a breaker and a sidecar from the recorded ticks."""
    lines = []
    index = found.series.get((INDEX.tr_id, KOSPI))
    if index is None or not len(index):
        lines.append(f"KOSPI 지수({INDEX.tr_id} {KOSPI}): 창 안에 틱 없음")
    else:
        lines.append(f"KOSPI 지수({INDEX.tr_id} {KOSPI}): 틱 {len(index):,}건")
        for stage, drop in enumerate(BREAKER_STAGES, start=1):
            met = [change <= -drop for change in index.change]
            lines.append(
                "  " + _rule(index, found.day, f"전일 대비 -{drop:g}% 이하({stage}단계의 기준)", met)
            )
    futures = [s for (tr_id, _), s in sorted(found.series.items()) if tr_id == KOSPI200_FUTURES.tr_id]
    if not any(len(s) for s in futures):
        lines.append(f"KOSPI200 선물({KOSPI200_FUTURES.tr_id}): 창 안에 틱 없음")
    for series in futures:
        if len(series):
            lines.append(f"KOSPI200 선물({series.feed} {series.key}): 틱 {len(series):,}건")
            met = [abs(change) >= SIDECAR_MOVE for change in series.change]
            lines.append(
                "  " + _rule(series, found.day, f"전일 대비 ±{SIDECAR_MOVE:g}% 이상(사이드카의 기준)", met)
            )
    return lines


def report(
    settings: Settings, day: date, start: time, end: time, raw: RawDays | None = None
) -> tuple[str, int]:
    """The report of ``day``'s window from ``start`` to before ``end`` (KST), and the exit code;
    ``raw`` reads the raw days (by default the data directory's alone)."""
    started, meter = clock.monotonic(), Meter()
    raw = RawDays(settings.data_dir) if raw is None else raw
    found = read(raw, day, start, end)
    sections = [
        (f"1. 구독(그날 raw {SYSTEM}의 응답)", ack_lines(raw, found)),
        ("2. 분마다 받은 레코드(피드별, 키를 합쳐)", minute_lines(found)),
        ("3. 키마다 레코드 수와 가장 긴 침묵", key_lines(found)),
        ("4. 코드 필드의 값(받은 순서로, 값마다 첫·마지막 받은 시각과 레코드 수)", code_lines(found)),
        ("5. REST 답(raw)", rest_lines(found)),
        (
            "6. 거래소 규칙의 조건을 틱에서 본 시각"
            "(엔진처럼 읽되 개장 뒤 5분도 센다. 틱의 시각, 괄호는 받은 시각)",
            rule_lines(found),
        ),
    ]
    peak = meter.peak_mb()
    rss = "모름" if peak is None else f"{peak:.0f} MB"
    sections.append(("7. 실행", [f"{clock.monotonic() - started:.1f}초 · 최대 RSS {rss}"]))
    weekday = "월화수목금토일"[day.weekday()]
    out = [
        f"{TITLE}: {day}({weekday}) {start:%H:%M}–{end:%H:%M}에 받은 raw 프레임",
        "필드 이름, 코드 값, 개수, 시각만 보인다(가격, 수량, 지수 같은 시세 값은 없다)."
        " 시각은 받은 시각(KST)이다.",
        f"'{UNREAD}'는 배치가 달라 읽지 못한 값, {ABSENT}은 그 배치에 없는 필드다.",
        "",
    ]
    for heading, lines in sections:
        out += [heading, *(f"  {line}" for line in lines), ""]
    return "\n".join(out).rstrip("\n"), 0 if found.records else NO_RECORDS


def probe_market_halt(settings: Settings, day: date, start: time, end: time) -> tuple[str, int]:
    """The command: the report of ``day``'s window, a pruned raw day read back from the Object Store."""
    with RawDays(settings.data_dir, open_store(settings)) as raw:
        return report(settings, day, start, end, raw)
