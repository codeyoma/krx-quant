"""``krx-quant probe-inverse-book``: what the collector recorded of the inverse ETF's KRX book (#606).

The collector registers the KRX-only real-time book (``H0STASP0``) of 005930's inverse ETF, 0193L0,
in the session's last slot (``collector.INVERSE_BOOKS``), and records its frames raw only
(``feeds.KRX_BOOK``). What only the server can see is whether KIS streams it for this app key, and in
which layout: KIS's example gave 59 fields until 2026-09-28 and 63 since. A replay of down calls
through the ETF (#606) is to be built on these frames, so before any event is made of them
(CODING_STANDARDS.md "External data is probed before it is used") a scheduled job
(``probe-inverse-book-606``) runs this on the first trading night after the deploy that brings it,
and posts it on #606.

For a trading day it prints:

1. KIS's answers to the book's registration, and every registration KIS refused for the session
   limit that day (``raw``, ``_system``);
2. the frames: how many, records per frame, fields per record against the example's 63 and 59, keys,
   by phase and hour, the ones that are not data frames or are encrypted;
3. the fields of the records in one of the example's layouts, by its names: how many blank, zero and
   not a number, and the code fields' values;
4. the time field: its form, the values that are not a time of day, the record's time against its
   receipt;
5. the best quotes in the continuous session: both sides quoted, the spread in the ETF's ticks
   (``ETF_TICK``, checked here), levels out of order, how often the best quote changed, the longest
   silence, and how much of the session, and how many bar closes, had no record within the last 5
   seconds (``FRESH_SECONDS``: the replay leaves such quotes out);
6. the gaps the collector recorded for it;
7. the recording's burden: the bytes on disk, and the day's frames handled again on this server:
   read only (the control), as the collector handles them (parsed, recorded raw with its buffer of
   ``KRX_FLUSH_ROWS`` rows or ``KRX_FLUSH_SECONDS``), then also turned into best-quote events. For
   each recording pass: the CPU seconds and their share of one core, and its resident memory's
   growth from just before it, reading included (an upper bound on what recording adds), and less
   the read-only pass's (a rough estimate), against #606's bounds (``RSS_BOUND_MB``, ``CPU_BOUND``):
   within only under the upper bound, and an RSS that cannot be read is said to be unmeasured, never
   within (``verdict``).

It prints counts, rates, seconds, ticks, field names and codes: no price, quantity or other market
value, so its output can go on the public issue. It reads files only, beside the running collector (a
raw day the archive pruned on the server, #607, is read back from the Object Store:
``raw_days.RawDays``), and writes the burden's records to a temporary directory it removes. Its exit
code is 0, or 3 when no frame of the book came that day.
"""

from __future__ import annotations

import bisect
import statistics
import sys
import tempfile
import time as clock
from array import array
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa

from krx_quant import kis
from krx_quant.archive import partition_batches
from krx_quant.config import Settings
from krx_quant.feeds import KRX_BOOK, RAW_SCHEMA
from krx_quant.feeds.base import EVENT_TIME, KST, RECEIVED_AT, kst
from krx_quant.feeds.stock import KRX_BOOK_OLD_WIDTH
from krx_quant.footprint import PEAK_STEP, Meter, give_back
from krx_quant.health import Unread, read_gaps
from krx_quant.market_calendar import MarketCalendar, Phase, Session, calendar_store
from krx_quant.object_store import open_store
from krx_quant.products import INVERSE_PRODUCTS
from krx_quant.raw_days import RawDays
from krx_quant.recorder import ParquetRecorder
from krx_quant.trader import SYMBOL as TRADED

TITLE = "인버스 ETF 호가 확인 (#606)"
NO_FRAMES = 3  # the exit code when no frame of the book came that day

# The traded symbol's inverse ETF, whose book the collector registers (``collector.INVERSE_BOOKS``).
ETF = INVERSE_PRODUCTS[TRADED].code
LAYOUTS = (len(KRX_BOOK.columns), KRX_BOOK_OLD_WIDTH)  # the example's since 2026-09-28, and before
# The fields whose values are codes: their values are printed, the rest only counted.
CODE_FIELDS = ("MKSC_SHRN_ISCD", "HOUR_CLS_CODE", "ANTC_CNTG_VRSS_SIGN", "STCK_DEAL_CLS_CODE")
CODE_FIELDS_NEW = ("MIDP_CLS_CODE", "MARKET_CLS_CODE")
MAX_CODES = 20  # a code field's values printed; the rest are counted together
LEVELS = range(1, 11)
_INDEX = {name: i for i, name in enumerate(KRX_BOOK.columns)}

# The replay leaves out a quote older than this at the moment it would trade (#606).
FRESH_SECONDS = 5.0
# #606's bounds on what recording the book may cost the engine: resident memory and one core's CPU.
RSS_BOUND_MB = 32.0
CPU_BOUND = 0.05
# The tick an ETF's price moves by, as this probe takes it (KRX: 1 won below 2,000 won, 5 won from
# 2,000 won; the stock table, ``pricing.tick_size``, does not cover ETFs). Checked here: section 5
# counts the best quotes that are not a whole number of these ticks.
ETF_TICK_FROM = 2_000


def etf_tick(price: int) -> int:
    return 5 if price >= ETF_TICK_FROM else 1


def probed_day(calendar: MarketCalendar, now: datetime) -> date:
    """The trading day a run at ``now`` reads: today's once its session is over, else the trading
    day before; a run after midnight (the scheduled jobs run until 05:30) reads the evening's."""
    local = now.astimezone(KST)
    day = (local - timedelta(hours=6)).date()
    session = calendar.session(day)
    if session is not None and local >= session.close:
        return day
    return calendar.previous_trading_day(day)


@dataclass(frozen=True)
class Ack:
    """KIS's answer to a registration, as recorded raw."""

    received_at: datetime
    tr_id: str
    key: str
    ok: bool
    limit: bool  # refused for the session limit
    message: str


def acknowledgements(raw: RawDays, day: date) -> list[Ack]:
    """KIS's answers to the registrations recorded on ``day``, in order (the heartbeats left out)."""
    root = raw.feed(day, "_system")
    acks: list[Ack] = []
    for partition in sorted(root.glob("symbol=*")):
        if partition.name == "symbol=_":  # PINGPONG
            continue
        for at, text in _rows(partition):
            try:
                message = kis.parse_message(text)
            except kis.KisError:
                continue
            if isinstance(message, kis.SystemMessage) and not message.is_pingpong:
                key = message.tr_key or ""
                acks.append(
                    Ack(at, message.tr_id, key, message.ok, message.is_limit_rejection, message.message)
                )
    return sorted(acks, key=lambda a: a.received_at)


def _rows(partition: Path) -> Iterator[tuple[datetime, str]]:
    """A raw partition's rows (received, message), each row once (``archive.partition_batches``)."""
    for _, batch in partition_batches(partition, RAW_SCHEMA.names):
        rows = batch.to_pydict()
        yield from zip(rows["received_at"], rows["message"], strict=True)


def _partitions(raw: RawDays, day: date) -> list[Path]:
    """``day``'s book partitions, one read back from the Object Store if the archive pruned it (#607)."""
    return sorted(raw.feed(day, KRX_BOOK.tr_id).glob("symbol=*"))


@dataclass
class _Codes:
    values: Counter[str] = field(default_factory=Counter)

    def add(self, value: str) -> None:
        if value in self.values or len(self.values) < MAX_CODES:
            self.values[value] += 1
        else:
            self.values["\0"] += 1  # another value

    def line(self) -> str:
        shown = [(v, n) for v, n in self.values.most_common() if v != "\0"]
        text = ", ".join(f"{v.strip() or '(빈 값)'} {n}" for v, n in shown)
        other = self.values["\0"]
        return text + (f", 그 밖의 값 {other}" if other else "")


@dataclass
class _Fields:
    """The fields of the records in one layout, by the example's names."""

    width: int
    records: int = 0
    blank: list[int] = field(default_factory=list)
    zero: list[int] = field(default_factory=list)
    text: list[int] = field(default_factory=list)
    codes: dict[str, _Codes] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.blank, self.zero, self.text = ([0] * self.width for _ in range(3))
        names = KRX_BOOK.columns[: self.width]
        self.codes = {name: _Codes() for name in (*CODE_FIELDS, *CODE_FIELDS_NEW) if name in names}

    def add(self, record: Sequence[str]) -> None:
        self.records += 1
        for j, value in enumerate(record):
            text = value.strip()
            if not text:
                self.blank[j] += 1
                continue
            try:
                if float(text) == 0:
                    self.zero[j] += 1
            except ValueError:
                self.text[j] += 1
        for name, codes in self.codes.items():
            codes.add(record[_INDEX[name]])


@dataclass
class Book:
    """What the book's frames of a day held."""

    day: date
    session: Session | None
    frames: int = 0
    records: int = 0
    unreadable: int = 0  # not data frames
    encrypted: int = 0
    other_tr: int = 0  # data frames of another TR in the partition
    keys: Counter[str] = field(default_factory=Counter)
    counts: Counter[int] = field(default_factory=Counter)  # records per frame
    widths: Counter[int] = field(default_factory=Counter)  # fields per record; -n: a frame of n fields
    phases: Counter[Phase] = field(default_factory=Counter)  # records
    hours: Counter[int] = field(default_factory=Counter)  # records
    minutes: Counter[datetime] = field(default_factory=Counter)  # records, for the busiest minute
    fields: dict[int, _Fields] = field(default_factory=dict)
    first: datetime | None = None
    last: datetime | None = None
    received: array[float] = field(default_factory=lambda: array("d"))  # every record, POSIX seconds
    times: Counter[str] = field(default_factory=Counter)  # the time field's forms
    odd_times: _Codes = field(default_factory=_Codes)  # its values that are not a time of day
    lags: array[float] = field(default_factory=lambda: array("d"))  # received - the record's time, s
    # The continuous session's records in a layout: (received, ask 1, bid 1, ask qty 1, bid qty 1).
    best: list[tuple[float, int, int, int, int]] = field(default_factory=list)
    quoted: int = 0  # both sides' best quoted
    spreads: Counter[int] = field(default_factory=Counter)  # ask 1 - bid 1, in ticks
    off_tick: int = 0  # a best quote, or the spread, not a whole number of ticks
    asks_out: int = 0  # the asks' quoted levels not rising
    bids_out: int = 0  # the bids' quoted levels not falling
    full: int = 0  # all ten levels quoted on both sides

    def __post_init__(self) -> None:
        self.fields = {w: _Fields(w) for w in LAYOUTS}

    def frame(self, at: datetime, text: str, key: str) -> None:
        try:
            message = kis.parse_message(text)
        except kis.KisError:
            self.unreadable += 1
            return
        if not isinstance(message, kis.RealtimeFrame) or message.count < 1:
            self.unreadable += 1
            return
        if message.tr_id != KRX_BOOK.tr_id:
            self.other_tr += 1
            return
        self.frames += 1
        self.keys[key] += 1
        self.counts[message.count] += 1
        self.first = at if self.first is None else min(self.first, at)
        self.last = at if self.last is None else max(self.last, at)
        if message.encrypted:
            self.encrypted += 1
            return
        fields = message.payload.split("^")
        width, rest = divmod(len(fields), message.count)
        if rest:
            self.widths[-len(fields)] += 1
            return
        local = at.astimezone(KST)
        phase = Phase.CLOSED if self.session is None else self.session.phase(local)
        for i in range(message.count):
            self.records += 1
            self.widths[width] += 1
            self.phases[phase] += 1
            self.hours[local.hour] += 1
            self.minutes[local.replace(second=0, microsecond=0)] += 1
            self.received.append(at.timestamp())
            if width in self.fields:
                self._record(local, phase, fields[i * width : (i + 1) * width])

    def _record(self, at: datetime, phase: Phase, record: list[str]) -> None:
        self.fields[len(record)].add(record)
        hhmmss = record[_INDEX["BSOP_HOUR"]].strip()
        if hhmmss.isdigit() and len(hhmmss) == 6:
            try:
                stamped = kst(at.date(), hhmmss)
            except ValueError:
                self.times["6자리, 시각이 아님"] += 1
                self.odd_times.add(hhmmss)
            else:
                self.times["HHMMSS"] += 1
                self.lags.append((at - stamped).total_seconds())
        else:
            self.times["그 밖의 꼴"] += 1
            self.odd_times.add(hhmmss)
        if phase is not Phase.CONTINUOUS:
            return
        asks = [_int(record[_INDEX[f"ASKP{i}"]]) for i in LEVELS]
        bids = [_int(record[_INDEX[f"BIDP{i}"]]) for i in LEVELS]
        ask_qty, bid_qty = (_int(record[_INDEX[f"{side}_RSQN1"]]) for side in ("ASKP", "BIDP"))
        self.best.append((at.timestamp(), asks[0], bids[0], ask_qty, bid_qty))
        quoted_asks, quoted_bids = [p for p in asks if p > 0], [p for p in bids if p > 0]
        self.asks_out += any(b <= a for a, b in zip(quoted_asks, quoted_asks[1:], strict=False))
        self.bids_out += any(b >= a for a, b in zip(quoted_bids, quoted_bids[1:], strict=False))
        self.full += len(quoted_asks) == len(quoted_bids) == len(LEVELS)
        ask, bid = asks[0], bids[0]
        if ask <= 0 or bid <= 0:
            return
        self.quoted += 1
        tick = etf_tick(bid)
        spread, off = divmod(ask - bid, tick)
        self.off_tick += bool(off) or ask % etf_tick(ask) != 0 or bid % tick != 0
        self.spreads[min(spread, 5) if spread > 0 else spread] += 1


def _int(value: str) -> int:
    try:
        return int(float(value.strip() or 0))
    except ValueError:
        return 0


def read_book(raw: RawDays, day: date, session: Session | None) -> Book:
    book = Book(day, session)
    for partition in _partitions(raw, day):
        key = partition.name.removeprefix("symbol=")
        for at, text in _rows(partition):
            book.frame(at, text, key)
    return book


# The report.


def _when(at: datetime | float | None, day: date) -> str:
    if at is None:
        return "모름"
    local = (datetime.fromtimestamp(at, UTC) if isinstance(at, float) else at).astimezone(KST)
    return local.strftime("%H:%M:%S" if local.date() == day else "%m-%d %H:%M:%S")


def _pct(part: float, whole: float) -> str:
    return f"{100 * part / whole:.1f}%" if whole else "—"


def _seconds(value: float) -> str:
    return f"{value:.1f}초"


def _mb(value: float | None) -> str:
    return "모름" if value is None else f"{value:.0f} MB"


def _mb1(value: float | None) -> str:
    return "모름" if value is None else f"{value:.1f} MB"


_PHASES = {
    Phase.PRE_OPEN: "장 전",
    Phase.OPENING_AUCTION: "시가 단일가",
    Phase.CONTINUOUS: "연속매매",
    Phase.CLOSING_AUCTION: "종가 단일가",
    Phase.AFTER_CLOSE: "장 마감 뒤",
    Phase.CLOSED: "거래일 아님",
}


def ack_lines(acks: Sequence[Ack]) -> list[str]:
    book: dict[tuple[str, bool, str], list[datetime]] = {}
    for a in acks:
        if a.tr_id == KRX_BOOK.tr_id:
            book.setdefault((a.key, a.ok, a.message), []).append(a.received_at)
    lines = []
    for (key, ok, message), times in sorted(book.items()):
        day = times[0].astimezone(KST).date()
        mark = "" if ok else "거절: "
        lines.append(
            f"{KRX_BOOK.tr_id} {key or '(키 없음)'}: {mark}{message} {len(times)}번"
            f"(첫 {_when(times[0], day)}, 마지막 {_when(times[-1], day)})"
        )
    if not lines:
        lines.append(f"{KRX_BOOK.tr_id}: 응답 없음(그날 수집기가 이 호가를 구독하지 않았다)")
    limited = Counter((a.tr_id, a.key) for a in acks if a.limit)
    lines.append(
        "세션 한도로 거절된 구독: "
        + (", ".join(f"{tr} {key} {n}번" for (tr, key), n in sorted(limited.items())) or "없음")
    )
    return lines


def frame_lines(book: Book) -> list[str]:
    if not book.frames:
        lines = ["받은 프레임 없음"]
        if book.unreadable or book.other_tr:
            lines.append(f"데이터 프레임이 아닌 행 {book.unreadable}개, 다른 TR {book.other_tr}개")
        return lines
    widths = ", ".join(
        f"{w}개 {n}" if w > 0 else f"레코드 수로 나뉘지 않는 프레임(필드 {-w}개) {n}"
        for w, n in sorted(book.widths.items())
    )
    counts = ", ".join(f"{c}개 {n}" for c, n in sorted(book.counts.items()))
    phases = " · ".join(f"{_PHASES[p]} {book.phases[p]}" for p in Phase if book.phases[p])
    hours = " · ".join(f"{h:02d}시 {n}" for h, n in sorted(book.hours.items()))
    busiest = max(book.minutes.items(), key=lambda x: (x[1], x[0]), default=None)
    day = book.day
    lines = [
        f"프레임 {book.frames} · 레코드 {book.records} · 프레임당 레코드: {counts}",
        "키(symbol 파티션)별 프레임: " + ", ".join(f"{k} {n}" for k, n in book.keys.most_common()),
        f"레코드당 필드 수: {widths} (KIS 예제는 2026-09-28부터 {LAYOUTS[0]}개, 그 전 {LAYOUTS[1]}개)",
        f"수신: 첫 {_when(book.first, day)} · 마지막 {_when(book.last, day)}",
        f"때(받은 시각)별 레코드: {phases}",
        f"시간대별 레코드: {hours}",
    ]
    if busiest is not None:
        lines.append(f"레코드가 가장 많은 1분: {busiest[0]:%H:%M} {busiest[1]}개")
    if book.encrypted or book.unreadable or book.other_tr:
        lines.append(
            f"암호화된 프레임 {book.encrypted}개 · 데이터 프레임이 아닌 행 {book.unreadable}개 · "
            f"다른 TR {book.other_tr}개"
        )
    return lines


def field_lines(book: Book) -> list[str]:
    lines = []
    for width, seen in book.fields.items():
        if not seen.records:
            lines.append(f"필드 {width}개인 레코드 없음")
            continue
        names = KRX_BOOK.columns[:width]
        lines.append(f"필드 {width}개인 레코드 {seen.records}개 (예제의 이름으로):")
        always = [n for j, n in enumerate(names) if seen.blank[j] + seen.zero[j] == seen.records]
        lines.append("  늘 비거나 0인 필드: " + (", ".join(always) or "없음"))
        for j, name in enumerate(names):
            if name in seen.codes or name in always:
                continue
            blank, zero, text = seen.blank[j], seen.zero[j], seen.text[j]
            if blank or zero or text:
                lines.append(f"  {name}: 빈 값 {blank} · 0 {zero} · 수가 아님 {text}")
        lines.append("  그 밖의 필드(코드 필드는 아래)는 모든 레코드에 0이 아닌 수가 있다")
        for name, codes in seen.codes.items():
            lines.append(f"  {name} 값: {codes.line()}")
    return lines


def time_lines(book: Book) -> list[str]:
    if not sum(book.times.values()):
        return ["읽은 레코드 없음"]
    forms = ", ".join(f"{form} {n}" for form, n in book.times.most_common())
    lines = [f"BSOP_HOUR의 꼴: {forms}"]
    if book.odd_times.values:
        lines.append(f"시각이 아닌 값: {book.odd_times.line()}")
    lags = sorted(book.lags)
    if lags:
        p99 = lags[min(len(lags) - 1, int(0.99 * len(lags)))]
        late = len(lags) - bisect.bisect_right(lags, FRESH_SECONDS)
        early = bisect.bisect_left(lags, -1.0)
        lines.append(
            f"받은 시각 − 레코드의 시각(초 단위로 잘린 시각): 중앙 {_seconds(statistics.median(lags))} · "
            f"99% {_seconds(p99)} · 최대 {_seconds(lags[-1])} · {FRESH_SECONDS:g}초보다 늦게 받은 레코드 "
            f"{late}({_pct(late, len(lags))}) · 레코드의 시각이 받은 때보다 1초 넘게 늦은 것 {early}"
        )
    return lines


def best_lines(book: Book) -> list[str]:
    session = book.session
    if session is None:
        return ["거래일이 아니다"]
    best = sorted(book.best)
    if not best:
        return ["연속매매 시간의 레코드 없음"]
    spreads = ", ".join(
        f"{'5틱 이상' if s == 5 else f'{s}틱'} {n}" for s, n in sorted(book.spreads.items()) if s > 0
    )
    locked, crossed = book.spreads[0], sum(n for s, n in book.spreads.items() if s < 0)
    price = qty = same = 0
    for (_, a0, b0, aq0, bq0), (_, a1, b1, aq1, bq1) in zip(best, best[1:], strict=False):
        if (a0, b0) != (a1, b1):
            price += 1
        elif (aq0, bq0) != (aq1, bq1):
            qty += 1
        else:
            same += 1
    lines = [
        f"연속매매 레코드 {len(best)}개 중 매도·매수 1호가가 다 있는 것 {book.quoted}"
        f"({_pct(book.quoted, len(best))})",
        f"스프레드(매도 1호가 − 매수 1호가, ETF 호가단위로): {spreads or '없음'} · 0(맞물림) {locked} · "
        f"음수(뒤집힘) {crossed}",
        f"호가단위({ETF_TICK_FROM:,}원 미만 1원, 이상 5원)의 배수가 아닌 1호가나 스프레드: {book.off_tick}",
        f"호가 순서가 어긋난 레코드: 매도 {book.asks_out} · 매수 {book.bids_out} · "
        f"10단계가 양쪽 다 있는 것 {book.full}",
        f"앞 레코드와 견준 1호가: 가격이 바뀜 {price} · 잔량만 바뀜 {qty} · 그대로 {same}",
    ]
    start, end = session.open.timestamp(), session.closing_auction.timestamp()
    received = sorted(book.received)
    silence, after = _longest_silence(received, start, end)
    lines.append(
        f"가장 긴 침묵(연속매매 안, 모든 레코드): {_seconds(silence)}(그 앞 레코드 {_when(after, book.day)})"
    )
    stale = _stale_seconds(received, start, end)
    closes = _bar_closes(session)
    stale_closes = sum(1 for t in closes if not _fresh(received, t))
    lines.append(
        f"마지막 레코드가 {FRESH_SECONDS:g}초보다 오래된 시간: {_seconds(stale)}"
        f"(연속매매의 {_pct(stale, end - start)}) · "
        f"봉 마감(매분 0초) {len(closes)}번 중 {stale_closes}번({_pct(stale_closes, len(closes))})"
    )
    return lines


def _longest_silence(received: Sequence[float], start: float, end: float) -> tuple[float, float]:
    """The longest stretch of [start, end) with no record, and when it began (a record's time, or
    ``start``)."""
    inside = [t for t in received if start <= t < end]
    edges = [start, *inside, end]
    return max(((b - a, a) for a, b in zip(edges, edges[1:], strict=False)), default=(0.0, start))


def _stale_seconds(received: Sequence[float], start: float, end: float) -> float:
    """The seconds of [start, end) when the latest record (from any time before) is more than
    ``FRESH_SECONDS`` old, or there is none yet."""
    i = bisect.bisect_left(received, start)
    stale_from = received[i - 1] + FRESH_SECONDS if i else float("-inf")
    total, at = 0.0, start
    for t in received[i:]:
        if t >= end:
            break
        total += max(0.0, t - max(at, stale_from))
        at, stale_from = t, t + FRESH_SECONDS
    return total + max(0.0, end - max(at, stale_from))


def _bar_closes(session: Session) -> list[float]:
    """The continuous session's minute marks after the open, each a bar's close."""
    first = session.open + timedelta(minutes=1)
    count = int((session.closing_auction - first).total_seconds() // 60) + 1
    return [(first + timedelta(minutes=n)).timestamp() for n in range(count)]


def _fresh(received: Sequence[float], at: float) -> bool:
    i = bisect.bisect_right(received, at)
    return bool(i) and at - received[i - 1] <= FRESH_SECONDS


def gap_lines(data_dir: Path, day: date) -> list[str]:
    unread = Unread()
    lines = []
    for gap in read_gaps(data_dir, day, unread):
        if gap.feed != KRX_BOOK.tr_id:
            continue
        until = _when(gap.end, day) if gap.end is not None else "끝 없음(수집기가 멈춤)"
        lines.append(f"{gap.key}: {_when(gap.start, day)} – {until} ({gap.reason})")
    return (lines or ["없음"]) + unread.lines()


# The burden.

# What a best-quote event of the book could hold (#606): the columns are decided once this probe's
# result is in; here they stand in for the cost of making and recording one per record.
_EVENT_SCHEMA = pa.schema(
    [
        ("symbol", pa.string()),
        ("event_time", EVENT_TIME),
        ("received_at", RECEIVED_AT),
        ("hour_cls", pa.string()),
        ("ask_px_1", pa.int64()),
        ("ask_qty_1", pa.int64()),
        ("bid_px_1", pa.int64()),
        ("bid_qty_1", pa.int64()),
    ]
)


@dataclass(frozen=True)
class Pass:
    """One pass over the day's frames (``measure_burden``)."""

    cpu: float = 0.0  # seconds handling the frames; reading them is left out
    buffer_mb: float = 0.0  # the most the recorder's buffers held, as Python objects
    # Resident memory's growth over the pass, from just before it (its peak less that); None where
    # it cannot be read (no ``/proc``: not Linux).
    rss_mb: float | None = None


@dataclass
class Burden:
    """The day's frames handled again as the collector handles them, and read only."""

    frames: int = 0
    events: int = 0
    read: Pass = field(default_factory=Pass)  # read only: the control the others are measured against
    raw: Pass = field(default_factory=Pass)  # parse and record raw, as the collector does now
    all: Pass = field(default_factory=Pass)  # that, then a best-quote event per record too

    def without_reading_mb(self, mode: Pass) -> float | None:
        """``mode``'s growth less the read-only pass's (the probe's own reading): an estimate of what
        recording adds, rougher than either figure (the allocators' reuse makes it vary from run to
        run by several MB); None if either was not read."""
        if mode.rss_mb is None or self.read.rss_mb is None:
            return None
        return max(0.0, mode.rss_mb - self.read.rss_mb)


def verdict(share: float, upper_mb: float | None, own_mb: float | None) -> str:
    """#606's bounds on a recording mode, given its CPU ``share`` of a core and its pass's resident
    growth: ``upper_mb``, reading included, bounds what recording adds from above; ``own_mb`` is
    that less the read-only pass's. Within only when the CPU is within and so is the upper bound;
    over when the CPU is over or even ``own_mb`` is; otherwise the RSS is not judged. An RSS not
    read is said to be unmeasured, never passed."""
    if share > CPU_BOUND or (own_mb is not None and own_mb > RSS_BOUND_MB):
        return "기준 넘음"
    if upper_mb is None:
        return "RSS는 재지 못함(CPU는 기준 안)"
    if upper_mb > RSS_BOUND_MB:
        return "RSS는 판정 못함(읽기를 포함한 상한이 기준을 넘고, 읽기를 빼면 기준 안. CPU는 기준 안)"
    return "기준 안"


def _event(record: Sequence[str], at: datetime) -> dict[str, Any]:
    local = at.astimezone(KST)
    hhmmss = record[_INDEX["BSOP_HOUR"]]
    try:
        stamped = kst(local.date(), hhmmss)
    except ValueError:
        stamped = local.replace(microsecond=0)
    return {
        "symbol": record[_INDEX["MKSC_SHRN_ISCD"]],
        "event_time": stamped,
        "received_at": at,
        "hour_cls": record[_INDEX["HOUR_CLS_CODE"]],
        "ask_px_1": _int(record[_INDEX["ASKP1"]]),
        "ask_qty_1": _int(record[_INDEX["ASKP_RSQN1"]]),
        "bid_px_1": _int(record[_INDEX["BIDP1"]]),
        "bid_qty_1": _int(record[_INDEX["BIDP_RSQN1"]]),
    }


def _row_bytes(row: dict[str, Any]) -> int:
    return sys.getsizeof(row) + sum(sys.getsizeof(v) for v in row.values())


@dataclass
class _Buffers:
    """The rows the recorder holds by layer, counted as it holds them: a layer's buffer is written
    at ``flush_rows`` rows, every buffer at a ``write``; ``peak`` is the most bytes held at once."""

    flush_rows: int
    held: dict[str, list[int]] = field(default_factory=dict)  # layer -> [rows, bytes]
    peak: int = 0

    def add(self, layer: str, row: dict[str, Any]) -> None:
        count = self.held.setdefault(layer, [0, 0])
        count[0] += 1
        count[1] += _row_bytes(row)
        self.peak = max(self.peak, sum(b for _, b in self.held.values()))
        if count[0] >= self.flush_rows:
            self.held[layer] = [0, 0]

    def write(self) -> None:
        self.held = {}


class _Resident:
    """A pass's resident memory: read just before it, then after each batch and each write, with
    the kernel's high-water mark since the start where it can be reset (``footprint.Meter``)."""

    def __init__(self, meter: Meter) -> None:
        self._meter = meter
        give_back()  # what the pass before freed goes back to the system first
        meter.reset()
        self._start = meter.rss_mb()
        self._peak = self._start

    def sample(self) -> None:
        now = self._meter.rss_mb()
        if now is not None and (self._peak is None or now > self._peak):
            self._peak = now

    def growth(self) -> float | None:
        self.sample()
        peak = self._peak
        if self._meter.peak_since == PEAK_STEP:
            mark = self._meter.peak_mb()
            peak = mark if peak is None or (mark is not None and mark > peak) else peak
        return None if self._start is None or peak is None else max(0.0, peak - self._start)


# The rows read at a time in the burden's passes: few, so that what reading holds stays small beside
# what recording does (the read-only pass measures it, and it is taken off).
BURDEN_BATCH_ROWS = 2_000


def _warm_up(flush_rows: int, flush_seconds: float, day: date) -> None:
    """Write a row of each layer once, so that what Arrow and Parquet set up on a first write (the
    collector did it long ago) is not counted as the first pass's growth."""
    at = datetime.now(UTC)
    record = ["0"] * len(KRX_BOOK.columns)
    with tempfile.TemporaryDirectory(prefix="probe-inverse-book-") as out:
        recorder = ParquetRecorder(Path(out), flush_rows, flush_seconds)
        recorder.record("raw", day, KRX_BOOK.tr_id, ETF, {"received_at": at, "message": ""}, RAW_SCHEMA)
        recorder.record("events", day, KRX_BOOK.tr_id, ETF, _event(record, at), _EVENT_SCHEMA)
        recorder.close()


# How many times each pass runs: resident memory's growth over one varies from run to run by more
# than the bound's tenth (the allocators reuse what earlier passes freed: on made-up days of 300,000
# frames the raw pass grew 15 MB on one run and 35 MB on another), so each figure is the median of
# this many, the passes taken in turn.
REPEATS = 3

_MODES = ("read", "raw", "all")


def measure_burden(
    raw: RawDays, day: date, flush_rows: int, flush_seconds: float, meter: Meter | None = None
) -> Burden:
    """Handle the day's book frames again, timing only the handling: read only once to warm up
    (unreported: the first read sets up what the later ones reuse, and grew 45–55 MB more than they
    in a measure on made-up days), then ``REPEATS`` times in turn: read only (the control), as the
    collector does now (parse, record raw), and also as a best-quote event per record. Each pass's
    resident memory grows from just before it (``_Resident``); each figure is its mode's median. The
    recorder writes a buffer once it holds ``flush_rows`` rows, and the buffers ``flush_seconds``
    after their last write, by the times received (the collector looks each second,
    ``ParquetRecorder.flush_due``)."""
    meter = meter or Meter()
    _warm_up(flush_rows, flush_seconds, day)
    _run_pass(raw, day, "read", flush_rows, flush_seconds, meter)
    runs: dict[str, list[Pass]] = {mode: [] for mode in _MODES}
    counts: dict[str, tuple[int, int]] = {}
    for _ in range(REPEATS):
        for mode in _MODES:
            done, frames, events = _run_pass(raw, day, mode, flush_rows, flush_seconds, meter)
            runs[mode].append(done)
            counts[mode] = frames, events
    medians = {mode: _median(passes) for mode, passes in runs.items()}
    return Burden(counts["raw"][0], counts["all"][1], medians["read"], medians["raw"], medians["all"])


def _median(passes: Sequence[Pass]) -> Pass:
    grown = [p.rss_mb for p in passes]
    return Pass(
        statistics.median(p.cpu for p in passes),
        statistics.median(p.buffer_mb for p in passes),
        None if any(g is None for g in grown) else statistics.median(g for g in grown if g is not None),
    )


def _run_pass(
    raw: RawDays, day: date, mode: str, flush_rows: int, flush_seconds: float, meter: Meter
) -> tuple[Pass, int, int]:
    """One pass of ``mode`` (``read``, ``raw`` or ``all``), and the frames and events it recorded."""
    frames = events = 0
    with tempfile.TemporaryDirectory(prefix="probe-inverse-book-") as out:
        resident = _Resident(meter)
        recorder = ParquetRecorder(Path(out), flush_rows, flush_seconds)
        buffers = _Buffers(flush_rows)
        cpu, written = 0.0, None
        for partition in _partitions(raw, day):
            for _, batch in partition_batches(partition, RAW_SCHEMA.names, BURDEN_BATCH_ROWS):
                rows = batch.to_pydict()
                started = clock.process_time()
                for at, text in zip(rows["received_at"], rows["message"], strict=True):
                    if mode == "read":
                        continue
                    if written is None:
                        written = at
                    elif (at - written).total_seconds() >= flush_seconds:
                        recorder.flush()
                        buffers.write()
                        resident.sample()
                        written = at
                    try:
                        message = kis.parse_message(text)
                    except kis.KisError:
                        continue
                    if not isinstance(message, kis.RealtimeFrame):
                        continue
                    raw_row = {"received_at": at, "message": text}
                    buffers.add("raw", raw_row)
                    recorder.record("raw", day, message.tr_id, message.symbol, raw_row, RAW_SCHEMA)
                    frames += 1
                    if mode == "raw":
                        continue
                    fields = message.payload.split("^")
                    width, rest = divmod(len(fields), message.count)
                    if rest or width not in LAYOUTS or message.encrypted:
                        continue
                    for i in range(message.count):
                        row = _event(fields[i * width : (i + 1) * width], at)
                        buffers.add("events", row)
                        recorder.record("events", day, message.tr_id, row["symbol"], row, _EVENT_SCHEMA)
                        events += 1
                cpu += clock.process_time() - started
                resident.sample()
        started = clock.process_time()
        recorder.close()
        cpu += clock.process_time() - started
        return Pass(cpu, buffers.peak / 2**20, resident.growth()), frames, events


def burden_lines(raw: RawDays, day: date, book: Book, burden: Burden) -> list[str]:
    root = raw.data_dir / "raw" / f"date={day.isoformat()}"
    book_bytes = sum(f.stat().st_size for p in _partitions(raw, day) for f in p.glob("*.parquet"))
    day_bytes = sum(p.stat().st_size for p in root.rglob("*.parquet"))  # what the server holds
    lines = [
        f"디스크: raw {book_bytes / 2**20:.1f} MB(그날 raw 전체 {day_bytes / 2**20:.1f} MB의 "
        f"{_pct(book_bytes, day_bytes)})"
    ]
    if not burden.frames:
        return lines
    span = (book.last - book.first).total_seconds() if book.first and book.last else 0.0
    busiest = max(book.minutes.values(), default=0)
    per_record = burden.all.cpu / max(book.records, 1)
    read = burden.read.rss_mb
    lines.append(
        "RSS 증가는 각 실행 직전(메모리를 돌려준 뒤)부터 가장 높은 때까지다. 같은 프레임을 읽기만 한 실행은 "
        f"{_mb1(read)} 늘었다. 기록하는 실행의 증가는 읽기를 포함한 상한이고, 뺀 값은 거친 추정이다"
    )
    for name, mode in (
        ("지금처럼 raw만", burden.raw),
        (f"raw와 1호가 이벤트(레코드마다 1행, {burden.events}행)", burden.all),
    ):
        share = mode.cpu / span if span else 0.0
        own = burden.without_reading_mb(mode)
        rss = (
            "모름(이 시스템에서 읽지 못함)"
            if mode.rss_mb is None
            else f"{mode.rss_mb:.1f} MB(읽기를 빼면 {_mb1(own)})"
        )
        lines.append(
            f"{name}: CPU {mode.cpu:.2f}초(프레임당 {1e6 * mode.cpu / burden.frames:.0f}µs) · "
            f"받은 동안({_seconds(span)}) 한 코어의 {100 * share:.2f}% · RSS 증가 {rss} · "
            f"버퍼가 가장 많이 든 때 {mode.buffer_mb:.1f} MB(파이썬 객체) · "
            f"{verdict(share, mode.rss_mb, own)}"
            f"(기준 CPU {100 * CPU_BOUND:g}%, RSS 증가 {RSS_BOUND_MB:g} MiB)"
        )
    lines.append(
        f"레코드가 가장 많은 1분({busiest}개)의 CPU: 그 1분의 {100 * busiest * per_record / 60:.2f}%"
    )
    return lines


def report(
    settings: Settings,
    calendar: MarketCalendar,
    day: date,
    meter: Meter | None = None,
    raw: RawDays | None = None,
) -> tuple[str, int]:
    """``day``'s report and the exit code; ``meter`` reads the burden's resident memory, and ``raw``
    the raw days (by default the data directory's alone)."""
    started, run = clock.monotonic(), Meter()
    data_dir = settings.data_dir
    raw = RawDays(data_dir) if raw is None else raw
    session = calendar.session(day)
    book = read_book(raw, day, session)
    burden = measure_burden(raw, day, settings.flush_rows, settings.flush_seconds, meter)
    weekday = "월화수목금토일"[day.weekday()]
    sections = [
        ("1. 구독 응답(KIS, raw _system)", ack_lines(acknowledgements(raw, day))),
        (f"2. 프레임(raw {KRX_BOOK.tr_id})", frame_lines(book)),
        ("3. 필드", field_lines(book)),
        ("4. 시각", time_lines(book)),
        ("5. 연속매매의 1호가", best_lines(book)),
        ("6. 결측 구간(gaps)", gap_lines(data_dir, day)),
        ("7. 기록 부담", burden_lines(raw, day, book, burden)),
    ]
    peak = run.peak_mb()
    sections.append(
        (
            "8. 실행",
            [f"{clock.monotonic() - started:.1f}초 · 최대 RSS {_mb(peak)}"],
        )
    )
    out = [
        f"{TITLE}: {ETF} KRX 호가({KRX_BOOK.tr_id}), {day}({weekday})",
        "개수, 비율, 초, 틱, 필드 이름, 코드만 보인다(가격, 잔량 같은 시세 값은 없다).",
        "",
    ]
    for heading, lines in sections:
        out += [heading, *(f"  {line}" for line in lines), ""]
    return "\n".join(out).rstrip("\n"), 0 if book.frames else NO_FRAMES


def probe_inverse_book(settings: Settings, now: datetime, day: date | None = None) -> tuple[str, int]:
    """The command: ``day``'s report, by default the latest trading day whose session is over."""
    calendar = calendar_store(settings).calendar
    with RawDays(settings.data_dir, open_store(settings)) as raw:
        return report(settings, calendar, probed_day(calendar, now) if day is None else day, raw=raw)
