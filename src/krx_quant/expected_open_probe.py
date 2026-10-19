"""``krx-quant probe-expected-open``: what KIS sends of the auctions' expected fill (#304).

The integrated book (``H0UNASP0``) carries the single-price auction's expected fill
(``ANTC_CNPR`` price, ``ANTC_CNQN`` quantity, ``ANTC_VOL`` volume, ``ANTC_EXCH_CLS_CODE`` the
venue it is for). The collector normalized none of it before #304, but the ``raw`` layer
keeps every frame as received, so the days already recorded show what KIS sends. The
pre-open briefing (#304) shows the expected opening price from 08:30 to 09:00; what only
the server can see is whether the integrated feed carries it then, and whether it is KRX's
auction (the price the open settles at) rather than NXT's.

For each of the last recorded trading days and each symbol it prints:

- the book records received in each part of the session (before the opening auction, the
  opening auction, continuous trading, the closing auction, after the close) and how many
  carry an expected price;
- the venue codes and time codes (``HOUR_CLS_CODE``) of those records;
- when an expected price was first and last seen in each auction;
- how the last expected price before each auction's end compares with that day's KRX day
  candle (open, close), in ticks;
- the REST book snapshots (``FHKST01010200``, recorded when the feed is cut) and whether
  their answers carry ``antc_cnpr``;
- the normalized book rows' new columns (``expected_px``), once the collector records them.

It prints counts, times, codes and tick differences: no price, volume or other market
value, so its output can go on the public issue. It reads files only, beside the running
collector.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from krx_quant import kis
from krx_quant.archive import partition_files
from krx_quant.candles import read_krx_days
from krx_quant.config import Settings
from krx_quant.feeds import BOOK, BOOK_SNAPSHOT
from krx_quant.feeds.base import KST
from krx_quant.market_calendar import OPENING_AUCTION, MarketCalendar, Session, calendar_store
from krx_quant.pricing import tick_size

DAYS = 10  # the recorded trading days read, latest first
PARTS = ("장전", "시가 단일가", "연속매매", "종가 단일가", "장 마감 뒤")
OPENING, CLOSING = PARTS[1], PARTS[3]
_COLUMN = {name: i for i, name in enumerate(BOOK.columns)}
_PRICE, _EXCH, _HOUR = _COLUMN["ANTC_CNPR"], _COLUMN["ANTC_EXCH_CLS_CODE"], _COLUMN["HOUR_CLS_CODE"]


def part_of(session: Session, at: datetime) -> str:
    """The part of ``session`` that ``at`` falls in."""
    local = at.astimezone(KST)
    if local < session.open - OPENING_AUCTION:
        return PARTS[0]
    if local < session.open:
        return OPENING
    if local < session.closing_auction:
        return PARTS[2]
    if local < session.close:
        return CLOSING
    return PARTS[4]


def _batches(partition: Path, columns: Sequence[str]) -> Iterator[dict[str, list[Any]]]:
    """A partition's rows, a batch at a time, each row once (the merged file, then the parts it
    does not hold)."""
    for path in partition_files(partition):
        try:
            schema = pq.read_schema(path)
            present = [c for c in columns if c in schema.names]
            for batch in pq.ParquetFile(path).iter_batches(columns=present):
                rows = batch.to_pydict()
                yield {c: rows.get(c, [None] * batch.num_rows) for c in columns}
        except (FileNotFoundError, pa.ArrowInvalid, OSError):
            continue  # merged away in between, or written as it was read: the rest still counts


def _partition(data_dir: Path, layer: str, day: date, tr_id: str, symbol: str) -> Path:
    return data_dir / layer / f"date={day.isoformat()}" / f"feed={tr_id}" / f"symbol={symbol}"


@dataclass
class Book:
    """What one symbol's integrated book frames carried of the expected fill on one day."""

    records: Counter[str] = field(default_factory=Counter)  # by part
    priced: Counter[str] = field(default_factory=Counter)  # with an expected price, by part
    venues: dict[str, Counter[str]] = field(default_factory=dict)  # ANTC_EXCH_CLS_CODE, by part
    hours: dict[str, Counter[str]] = field(default_factory=dict)  # HOUR_CLS_CODE, by part
    first: dict[str, datetime] = field(default_factory=dict)  # an expected price first seen, by part
    last: dict[str, tuple[datetime, int]] = field(default_factory=dict)  # last seen, and the price
    unreadable: int = 0

    def record(self, part: str, at: datetime, fields: Sequence[str]) -> None:
        self.records[part] += 1
        self.hours.setdefault(part, Counter())[fields[_HOUR]] += 1
        price = _price(fields[_PRICE])
        if not price:
            return
        self.priced[part] += 1
        self.venues.setdefault(part, Counter())[fields[_EXCH].strip() or "(빈칸)"] += 1
        self.first.setdefault(part, at)
        if part not in self.last or at >= self.last[part][0]:
            self.last[part] = (at, price)


def _price(text: str) -> int:
    try:
        return int(float(text.strip() or 0))
    except ValueError:
        return 0


def read_book(data_dir: Path, session: Session, symbol: str) -> Book:
    """The expected fill in ``symbol``'s raw integrated book frames on ``session``'s day."""
    book = Book()
    partition = _partition(data_dir, "raw", session.day, BOOK.tr_id, symbol)
    for batch in _batches(partition, ("received_at", "message")):
        for at, text in zip(batch["received_at"], batch["message"], strict=True):
            try:
                message = kis.parse_message(text)
                if not isinstance(message, kis.RealtimeFrame) or message.encrypted:
                    raise kis.KisError("not a data frame")
                records = message.records(len(BOOK.columns))
            except kis.KisError:
                book.unreadable += 1
                continue
            part = part_of(session, at)
            for fields in records:
                book.record(part, at, fields)
    return book


@dataclass
class Snapshots:
    """The REST book snapshots recorded raw on one day, and what their answers carried."""

    answers: Counter[str] = field(default_factory=Counter)  # by part
    with_field: Counter[str] = field(default_factory=Counter)  # output2 has antc_cnpr
    priced: Counter[str] = field(default_factory=Counter)  # and it is not 0


def read_snapshots(data_dir: Path, session: Session, symbol: str) -> Snapshots:
    seen = Snapshots()
    partition = _partition(data_dir, "raw", session.day, BOOK_SNAPSHOT.tr_id, symbol)
    for batch in _batches(partition, ("received_at", "message")):
        for at, text in zip(batch["received_at"], batch["message"], strict=True):
            try:
                quote = json.loads(text)["body"]["output2"]
            except (ValueError, KeyError, TypeError):
                continue
            part = part_of(session, at)
            seen.answers[part] += 1
            if isinstance(quote, dict) and "antc_cnpr" in quote:
                seen.with_field[part] += 1
                seen.priced[part] += bool(_price(str(quote["antc_cnpr"])))
    return seen


def read_events(data_dir: Path, session: Session, symbol: str) -> tuple[int, int, Counter[str]]:
    """The normalized book rows of the day: all, those with the new columns, and those with an
    expected price by part."""
    rows = with_columns = 0
    priced: Counter[str] = Counter()
    partition = _partition(data_dir, "events", session.day, BOOK.tr_id, symbol)
    for batch in _batches(partition, ("received_at", "expected_px")):
        for at, price in zip(batch["received_at"], batch["expected_px"], strict=True):
            rows += 1
            if price is None:
                continue
            with_columns += 1
            if price:
                priced[part_of(session, at)] += 1
    return rows, with_columns, priced


def recorded_days(data_dir: Path, calendar: MarketCalendar, now: datetime, count: int) -> list[date]:
    """The latest ``count`` trading days, over by ``now``, with raw integrated book frames; oldest
    first."""
    days = []
    for path in (data_dir / "raw").glob(f"date=*/feed={BOOK.tr_id}"):
        try:
            day = date.fromisoformat(path.parent.name.removeprefix("date="))
        except ValueError:
            continue
        session = calendar.session(day)
        if session is not None and session.close <= now:
            days.append(day)
    return sorted(days)[-count:]


def tick_gap(expected: int, actual: int) -> str:
    """How far ``expected`` is from ``actual``, in ticks of ``actual``'s band, as a bucket."""
    ticks = round(abs(expected - actual) / tick_size(actual))
    if ticks == 0:
        return "같음"
    if ticks == 1:
        return "1틱"
    return "2–5틱" if ticks <= 5 else "6틱 이상"


GAPS = ("같음", "1틱", "2–5틱", "6틱 이상", "예상가 없음", "일봉 없음")
SECTIONS = (
    "1. 통합 호가 레코드와 예상가가 있는 레코드 (raw, 시간대별)",
    "2. 예상가가 있는 레코드의 ANTC_EXCH_CLS_CODE (시간대별)",
    "3. HOUR_CLS_CODE (시간대별, 모든 레코드)",
    "4. 예상가를 처음·마지막으로 본 시각 (단일가마다)",
    "5. 단일가 끝 직전 마지막 예상가와 그날 KRX 일봉의 차이 (틱)",
    f"6. REST 호가 스냅샷 {BOOK_SNAPSHOT.tr_id} 응답 (raw)",
    "7. 정규화한 호가 행의 새 열 expected_px (events)",
)


@dataclass
class _Symbol:
    """One symbol's days added up."""

    symbol: str
    records: Counter[str] = field(default_factory=Counter)
    priced: Counter[str] = field(default_factory=Counter)
    venues: dict[str, Counter[str]] = field(default_factory=dict)
    hours: dict[str, Counter[str]] = field(default_factory=dict)
    unreadable: int = 0
    seen: list[str] = field(default_factory=list)  # section 4, a line a day
    gaps: dict[str, Counter[str]] = field(default_factory=lambda: {OPENING: Counter(), CLOSING: Counter()})
    snapshots: Snapshots = field(default_factory=Snapshots)
    rows: int = 0
    with_columns: int = 0
    rows_priced: Counter[str] = field(default_factory=Counter)

    def add(self, data_dir: Path, session: Session, candle: dict[str, Any] | None) -> None:
        book = read_book(data_dir, session, self.symbol)
        self.records.update(book.records)
        self.priced.update(book.priced)
        self.unreadable += book.unreadable
        for part, counter in book.venues.items():
            self.venues.setdefault(part, Counter()).update(counter)
        for part, counter in book.hours.items():
            self.hours.setdefault(part, Counter()).update(counter)
        spans = []
        for part, column in ((OPENING, "open"), (CLOSING, "close")):
            if part not in book.last:
                self.gaps[part]["예상가 없음"] += 1
                continue
            spans.append(f"{part} {_time(book.first[part])}–{_time(book.last[part][0])}")
            if candle is None:
                self.gaps[part]["일봉 없음"] += 1
            else:
                self.gaps[part][tick_gap(book.last[part][1], candle[column])] += 1
        self.seen.append(f"{self.symbol} {session.day}: " + (", ".join(spans) or "예상가 없음"))
        rest = read_snapshots(data_dir, session, self.symbol)
        self.snapshots.answers.update(rest.answers)
        self.snapshots.with_field.update(rest.with_field)
        self.snapshots.priced.update(rest.priced)
        rows, with_columns, priced = read_events(data_dir, session, self.symbol)
        self.rows += rows
        self.with_columns += with_columns
        self.rows_priced.update(priced)

    def sections(self) -> list[list[str]]:
        """This symbol's lines, one list per section of ``SECTIONS``."""
        name = self.symbol
        records = " · ".join(f"{p} {self.records[p]}(예상가 {self.priced[p]})" for p in PARTS)
        if self.unreadable:
            records += f" · 읽을 수 없는 프레임 {self.unreadable}"
        rest = self.snapshots
        answers = " · ".join(
            f"{p} 응답 {rest.answers[p]}(antc_cnpr 있음 {rest.with_field[p]}, 0이 아님 {rest.priced[p]})"
            for p in PARTS
            if rest.answers[p]
        )
        return [
            [f"{name}: {records}"],
            [f"{name}: {_by_part(self.venues)}"],
            [f"{name}: {_by_part(self.hours)}"],
            self.seen,
            [f"{name} {part}: {_counts(counter, GAPS)}" for part, counter in self.gaps.items()],
            [f"{name}: {answers or '응답 없음'}"],
            [
                f"{name}: 행 {self.rows} · 새 열이 있는 행 {self.with_columns} · 예상가가 있는 행 "
                + _counts(self.rows_priced, PARTS)
            ],
        ]


def _counts(counter: Counter[str], order: Sequence[str]) -> str:
    return " · ".join(f"{k} {counter[k]}" for k in order if counter[k]) or "없음"


def _by_part(codes: dict[str, Counter[str]]) -> str:
    """Codes and their counts, part by part."""
    parts = []
    for p in PARTS:
        if codes.get(p):
            parts.append(f"{p} " + " · ".join(f"'{code}' {n}" for code, n in sorted(codes[p].items())))
    return " / ".join(parts) or "없음"


def _time(at: datetime) -> str:
    return at.astimezone(KST).strftime("%H:%M:%S")


def report(
    data_dir: Path, calendar: MarketCalendar, symbols: Sequence[str], now: datetime, days: int = DAYS
) -> str:
    """The report on the latest ``days`` recorded trading days over by ``now``."""
    recorded = recorded_days(data_dir, calendar, now, days)
    title = "예상 체결 녹화 확인 (#304)"
    if not recorded:
        return f"{title}: raw에 통합 호가({BOOK.tr_id})가 있는 거래일이 없다"
    sections: list[list[str]] = [[] for _ in SECTIONS]
    for symbol in symbols:
        candles = {row["date"]: row for row in read_krx_days(data_dir, symbol)}
        seen = _Symbol(symbol)
        for day in recorded:
            session = calendar.session(day)
            assert session is not None  # recorded_days keeps trading days only
            seen.add(data_dir, session, candles.get(day))
        for lines, more in zip(sections, seen.sections(), strict=True):
            lines += more
    out = [
        f"{title}: {recorded[0]} – {recorded[-1]}, 거래일 {len(recorded)}일, 종목 {' '.join(symbols)}",
        "시간대: " + ", ".join(PARTS) + " (거래 일정의 그날 장 시각으로 나눈다)",
        "",
    ]
    for heading, lines in zip(SECTIONS, sections, strict=True):
        out += [heading, *(f"  {line}" for line in lines or ["없음"]), ""]
    return "\n".join(out).rstrip("\n")


def probe_expected_open(settings: Settings, now: datetime, days: int = DAYS) -> str:
    calendar = calendar_store(settings).calendar
    return report(settings.data_dir, calendar, settings.symbols, now, days)
