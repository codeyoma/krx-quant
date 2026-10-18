"""``krx-quant probe-night``: what the collector recorded of a KOSPI200 night session (#264).

The collector registers the night futures' trades (``H0MFCNT0``) and book (``H0MFASP0``)
all day; KRX trades them from 18:00 to 06:00 the next morning. What only the server can
see is whether KIS streams them for this app key, under the contract code the collector
asks for, in the layout KIS's example gives. So after the first night the collector
subscribed for, a scheduled job (``scheduled.NIGHT_PROBE``) runs this and posts it on #264.

For a night (the evening of a trading day, from 18:00 to 06:00 the next morning, read from
the two days' partitions) it prints:

- KIS's answers to the night feeds' registrations that day (``raw``, ``_system``);
- for each night feed: the frames and records received, by partition and key, the fields
  per record, when they came (first, last, by hour, the longest silence), the time field's
  values (the session crosses midnight), the events recorded and their times against the
  times received, the fields that were always blank or zero;
- the gaps recorded for the night feeds, and whether the day futures' feed (``H0IFCNT0``)
  delivered that night too;
- the KIS masters' KOSPI200 futures, and the head of KIS's night futures master
  (``fo_cme_code.mst``), next to the contract the expiry rules chose;
- one line for each other night subscribed for in the week before.

It prints counts, times, field names and contract codes: no price, volume or other market
value, so its output can go on the public issue. It reads files and the public master
files only, beside the running collector.
"""

from __future__ import annotations

import statistics
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import aiohttp
import pyarrow as pa
import pyarrow.parquet as pq

from krx_quant import kis
from krx_quant.archive import partition_files
from krx_quant.config import Settings
from krx_quant.contracts import KOSPI200, NIGHT_END, night_front_month
from krx_quant.feeds import KOSPI200_FUTURES, NIGHT_FUTURES, NIGHT_FUTURES_BOOK, Feed
from krx_quant.feeds.base import KST
from krx_quant.market_calendar import MarketCalendar, calendar_store
from krx_quant.masters import MasterError, download

NIGHT_OPEN = time(18, 0)
# Records received this long after 06:00 still count as the night's (a closing record).
LATE = timedelta(minutes=10)
LOOK_BACK_DAYS = 7  # the nights looked for before the one probed
FEEDS = (NIGHT_FUTURES, NIGHT_FUTURES_BOOK)
NIGHT_TR_IDS = frozenset(f.tr_id for f in FEEDS)
# The night futures master KIS publishes beside the index one (stocks_info/domestic_cme_future_code.py:
# "CME연계 야간선물"), read to see what it lists now that KRX runs its own night session.
NIGHT_MASTER = "fo_cme_code.mst.zip"
INDEX_MASTER = "fo_idx_code_mts.mst.zip"
MASTER_LINES = 10


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


def _batches(partition: Path, columns: Sequence[str]) -> Iterator[dict[str, list[Any]]]:
    """A partition's rows, a batch at a time, each row once (the merged file, then the parts it
    does not hold)."""
    for path in partition_files(partition):
        try:
            for batch in pq.ParquetFile(path).iter_batches(columns=list(columns)):
                yield batch.to_pydict()
        except (FileNotFoundError, pa.ArrowInvalid, OSError):
            continue  # merged away in between, or written as it was read: the rest still counts


def acknowledgements(data_dir: Path, day: date) -> list[Ack]:
    """KIS's answers to the night feeds' registrations recorded on ``day``, in order."""
    root = data_dir / "raw" / f"date={day.isoformat()}" / "feed=_system"
    acks: list[Ack] = []
    for partition in sorted(root.glob("symbol=*")):
        if partition.name == "symbol=_":  # heartbeats
            continue
        for batch in _batches(partition, ("received_at", "message")):
            for at, text in zip(batch["received_at"], batch["message"], strict=True):
                try:
                    message = kis.parse_message(text)
                except kis.KisError:
                    continue
                if isinstance(message, kis.SystemMessage) and message.tr_id in NIGHT_TR_IDS:
                    acks.append(Ack(at, message.tr_id, message.tr_key or "", message.ok, message.message))
    return sorted(acks, key=lambda a: a.received_at)


def subscribed_nights(
    data_dir: Path, calendar: MarketCalendar, before: date, back: int = LOOK_BACK_DAYS
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
            acks = acknowledgements(data_dir, day)
        except OSError:
            continue
        if any(a.ok and a.tr_id == NIGHT_FUTURES.tr_id and a.received_at <= opens for a in acks):
            found.append(day)
    return found


@dataclass
class _Feed:
    """What one night feed delivered in a night."""

    feed: Feed
    frames: int = 0
    records: int = 0
    by_date: Counter[date] = field(default_factory=Counter)
    by_key: Counter[str] = field(default_factory=Counter)
    widths: Counter[int] = field(default_factory=Counter)
    by_hour: Counter[int] = field(default_factory=Counter)
    received: list[datetime] = field(default_factory=list)
    times: list[str] = field(default_factory=list)  # the time field, in the order received
    filled: set[int] = field(default_factory=set)  # columns with a value in some record
    full_width: int = 0  # records in the example's layout
    unreadable: int = 0  # frames that are not data frames
    events: int = 0
    event_times: list[datetime] = field(default_factory=list)
    lags: list[float] = field(default_factory=list)  # event time - time received, seconds
    other_day: int = 0  # events whose time is not on the day received

    def frame(self, at: datetime, text: str, partition_day: date, key: str) -> None:
        try:
            message = kis.parse_message(text)
        except kis.KisError:
            self.unreadable += 1
            return
        if not isinstance(message, kis.RealtimeFrame) or message.count < 1:
            self.unreadable += 1
            return
        self.frames += 1
        self.records += message.count
        self.by_date[partition_day] += 1
        self.by_key[key] += 1
        self.by_hour[at.astimezone(KST).hour] += message.count
        self.received.append(at)
        fields = message.payload.split("^")
        width, rest = divmod(len(fields), message.count)
        self.widths[width if not rest else -len(fields)] += message.count
        if rest:
            return
        for i in range(message.count):
            record = fields[i * width : (i + 1) * width]
            if len(record) > 1:
                self.times.append(record[1])
            if width == len(self.feed.columns):
                self.full_width += 1
                self.filled |= {j for j, value in enumerate(record) if not _blank(value)}

    def event(self, row: dict[str, Any]) -> None:
        self.events += 1
        at, received = row["event_time"], row["received_at"]
        self.event_times.append(at)
        self.lags.append((at - received).total_seconds())
        if at.astimezone(KST).date() != received.astimezone(KST).date():
            self.other_day += 1


def _blank(value: str) -> bool:
    text = value.strip()
    if not text:
        return True
    try:
        return float(text) == 0
    except ValueError:
        return False


def _in(window: tuple[datetime, datetime], at: datetime) -> bool:
    return window[0] <= at < window[1]


def _days(day: date) -> tuple[date, date]:
    return day, day + timedelta(days=1)


def read_night(data_dir: Path, day: date, feed: Feed) -> _Feed:
    """What ``feed`` delivered in the night after ``day``'s session."""
    window = night_window(day)
    seen = _Feed(feed)
    for partition_day in _days(day):
        root = data_dir / "raw" / f"date={partition_day.isoformat()}" / f"feed={feed.tr_id}"
        for partition in sorted(root.glob("symbol=*")):
            key = partition.name.removeprefix("symbol=")
            for batch in _batches(partition, ("received_at", "message")):
                for at, text in zip(batch["received_at"], batch["message"], strict=True):
                    if _in(window, at):
                        seen.frame(at, text, partition_day, key)
        root = data_dir / "events" / f"date={partition_day.isoformat()}" / f"feed={feed.tr_id}"
        for partition in sorted(root.glob("symbol=*")):
            for batch in _batches(partition, ("event_time", "received_at")):
                for at, received in zip(batch["event_time"], batch["received_at"], strict=True):
                    if _in(window, received):
                        seen.event({"event_time": at, "received_at": received})
    return seen


def _frames_in(data_dir: Path, day: date, tr_id: str) -> int:
    """Frames of ``tr_id`` received in the night after ``day``'s session."""
    window, count = night_window(day), 0
    for partition_day in _days(day):
        root = data_dir / "raw" / f"date={partition_day.isoformat()}" / f"feed={tr_id}"
        for partition in root.glob("symbol=*"):
            for batch in _batches(partition, ("received_at",)):
                count += sum(1 for at in batch["received_at"] if _in(window, at))
    return count


def _gaps(data_dir: Path, day: date) -> list[str]:
    window, lines = night_window(day), []
    for partition_day in _days(day):
        for tr_id in sorted(NIGHT_TR_IDS):
            root = data_dir / "gaps" / f"date={partition_day.isoformat()}" / f"feed={tr_id}"
            for partition in sorted(root.glob("symbol=*")):
                columns = ("feed", "symbol", "start", "end", "reason")
                for batch in _batches(partition, columns):
                    for tr, key, start, end, reason in zip(*(batch[c] for c in columns), strict=True):
                        if start < window[1] and (end is None or end > window[0]):
                            until = _when(end, day) if end is not None else "끝 없음(수집기가 멈춤)"
                            lines.append(f"{tr} {key}: {_when(start, day)} – {until} ({reason})")
    return lines or ["없음"]


def _when(at: datetime, day: date) -> str:
    """``at`` as a time, with its date when it is not ``day``."""
    local = at.astimezone(KST)
    return local.strftime("%H:%M:%S" if local.date() == day else "%m-%d %H:%M:%S")


def _seconds(value: float) -> str:
    return f"{value:.1f}초"


def _hours(by_hour: Counter[int]) -> str:
    order = [*range(18, 24), *range(0, 7)]
    return " · ".join(f"{h:02d}시 {by_hour[h]}" for h in order if by_hour[h]) or "없음"


def _feed_section(seen: _Feed, day: date) -> list[str]:
    if not seen.frames and not seen.events:
        lines = ["받은 프레임 없음"]
        if seen.unreadable:
            lines.append(f"읽을 수 없는 프레임 {seen.unreadable}개")
        return lines
    dates = ", ".join(f"{d} {n}" for d, n in sorted(seen.by_date.items()))
    keys = ", ".join(f"{k} {n}" for k, n in seen.by_key.most_common())
    widths = ", ".join(
        f"{w}개 {n}" if w > 0 else f"레코드 수로 나뉘지 않는 프레임(필드 {-w}개) {n}"
        for w, n in sorted(seen.widths.items())
    )
    received = sorted(seen.received)
    lines = [
        f"raw: 프레임 {seen.frames} · 레코드 {seen.records} (날짜 파티션별 프레임: {dates})",
        f"키(symbol 파티션)별 프레임: {keys}",
        f"레코드당 필드 수: {widths} (KIS 예제는 {len(seen.feed.columns)}개)",
        f"수신: 첫 {_when(received[0], day)} · 마지막 {_when(received[-1], day)}",
        f"시간대별 레코드: {_hours(seen.by_hour)}",
    ]
    if len(received) > 1:
        silence, after = max((b - a, a) for a, b in zip(received, received[1:], strict=False))
        lines.append(
            f"가장 긴 침묵: {_seconds(silence.total_seconds())} "
            f"({_when(after, day)} – {_when(after + silence, day)})"
        )
    if seen.times:
        numeric = [t for t in seen.times if t.isdigit() and len(t) == 6 and t != "999999"]
        late = sum(1 for t in numeric if int(t[:2]) >= 24)
        closing = seen.times.count("999999")
        odd = len(seen.times) - len(numeric) - closing
        span = f"가장 작은 {min(numeric)} · 가장 큰 {max(numeric)}" if numeric else "숫자 시각 없음"
        lines.append(
            f"시각 필드(bsop_hour) 원문: 처음 받은 {seen.times[0]} · 마지막 {seen.times[-1]} · {span} · "
            f"24시 이상 {late} · 999999 {closing} · 그 밖의 값 {odd}"
        )
    lines.append(f"events: 행 {seen.events} (raw 레코드 중 정규화하지 못한 것 {seen.records - seen.events})")
    if seen.events:
        times = sorted(seen.event_times)
        lags = seen.lags
        lines.append(f"event_time: 첫 {_when(times[0], day)} · 마지막 {_when(times[-1], day)}")
        lines.append(
            f"event_time − 수신 시각: 중앙 {_seconds(statistics.median(lags))} · "
            f"최소 {_seconds(min(lags))} · 최대 {_seconds(max(lags))} · "
            f"받은 날과 날짜가 다른 행 {seen.other_day}"
        )
    if seen.full_width:
        blank = [c for j, c in enumerate(seen.feed.columns) if j not in seen.filled]
        lines.append(
            f"예제 배치({len(seen.feed.columns)}개)인 레코드 {seen.full_width}개에서 늘 비거나 0인 필드: "
            + (", ".join(blank) if blank else "없음")
        )
    if seen.unreadable:
        lines.append(f"읽을 수 없는 프레임 {seen.unreadable}개")
    return lines


def _ack_lines(acks: Sequence[Ack]) -> list[str]:
    grouped: dict[tuple[str, str, bool, str], list[datetime]] = {}
    for a in acks:
        grouped.setdefault((a.tr_id, a.key, a.ok, a.message), []).append(a.received_at)
    lines = []
    for (tr_id, key, ok, message), times in sorted(grouped.items()):
        day = times[0].astimezone(KST).date()
        mark = "" if ok else "거절: "
        lines.append(
            f"{tr_id} {key or '(키 없음)'}: {mark}{message} {len(times)}번"
            f"(첫 {_when(times[0], day)}, 마지막 {_when(times[-1], day)})"
        )
    return lines or ["없음: 그날 수집기가 야간선물을 구독하지 않았다"]


def master_lines(index_text: str | None, night_text: str | None, errors: Sequence[str]) -> list[str]:
    """The index master's KOSPI200 futures and the night master's head (codes and names)."""
    lines = []
    if index_text is not None:
        futures = []
        for line in index_text.splitlines():
            cols = line.split("|")
            if len(cols) >= 8 and cols[0].strip() == "1" and cols[7].strip() == "2001":
                futures.append(f"{cols[1].strip()}(월물구분 {cols[6].strip()}, {cols[3].strip()})")
        lines.append(
            f"{INDEX_MASTER.removesuffix('.zip')}의 KOSPI200 선물: " + (", ".join(futures) or "없음")
        )
    if night_text is not None:
        rows = [line.rstrip() for line in night_text.splitlines() if line.strip()]
        lines.append(
            f"{NIGHT_MASTER.removesuffix('.zip')}: {len(rows)}줄. 앞 {min(len(rows), MASTER_LINES)}줄:"
        )
        lines += [f"  {row[:120]}" for row in rows[:MASTER_LINES]]
    return lines + list(errors)


async def read_masters(master_url: str) -> tuple[str | None, str | None, list[str]]:
    """The index and night futures masters' text, and why one could not be read."""
    texts: list[str | None] = []
    errors = []
    async with aiohttp.ClientSession() as session:
        for name in (INDEX_MASTER, NIGHT_MASTER):
            try:
                texts.append((await download(session, f"{master_url}/{name}")).decode("cp949"))
            except (MasterError, aiohttp.ClientError, OSError, TimeoutError, UnicodeDecodeError) as e:
                texts.append(None)
                errors.append(f"{name.removesuffix('.zip')}를 읽지 못했다: {e}")
    return texts[0], texts[1], errors


def report(
    data_dir: Path,
    calendar: MarketCalendar,
    now: datetime,
    masters: Sequence[str],
    day: date | None = None,
) -> str:
    """The report on the night after ``day``'s session; by default the latest night over by
    ``now`` that the collector subscribed for, else the latest trading day's."""
    ended = evening(now)  # nights before this evening's are over
    nights = subscribed_nights(data_dir, calendar, ended)
    if day is None:
        day = nights[-1] if nights else calendar.previous_trading_day(ended)
    start, _ = night_window(day)
    title = f"야간선물 녹화 확인 (#264): {day} 밤 (18:00 – {day + timedelta(days=1)} 06:00)"
    try:
        contract = night_front_month(KOSPI200, start, calendar)
    except ValueError as e:  # a year the expiry rules have no code for
        contract = f"없음 ({e})"
    lines = [
        title,
        f"구독한 밤(앞 {LOOK_BACK_DAYS}일의 거래일 중 18:00 전에 KIS가 H0MFCNT0 구독을 받은 날): "
        + (", ".join(str(d) for d in nights) or "없음"),
        f"만기 규칙이 이 밤에 고르는 월물: {contract}",
    ]
    acks = [
        a for d in _days(day) for a in acknowledgements(data_dir, d) if a.received_at < night_window(day)[1]
    ]
    sections: list[tuple[str, list[str]]] = [("1. 구독 응답(KIS, raw _system)", _ack_lines(acks))]
    for n, feed in enumerate(FEEDS, start=2):
        name = "체결" if feed is NIGHT_FUTURES else "호가"
        sections.append((f"{n}. {feed.tr_id} {name}", _feed_section(read_night(data_dir, day, feed), day)))
    sections.append(("4. 결측 구간(gaps)", _gaps(data_dir, day)))
    day_frames = _frames_in(data_dir, day, KOSPI200_FUTURES.tr_id)
    sections.append(("5. 같은 밤 주간 선물 피드", [f"{KOSPI200_FUTURES.tr_id} 프레임 {day_frames}개"]))
    sections.append(("6. KIS 마스터", list(masters) or ["읽지 않았다"]))
    others = []
    for other in (d for d in nights if d != day):
        trades, book = (read_night(data_dir, other, f) for f in FEEDS)
        received = sorted(trades.received + book.received)
        span = f" · 첫 {_when(received[0], other)} · 마지막 {_when(received[-1], other)}" if received else ""
        others.append(f"{other}: 체결 레코드 {trades.records} · 호가 레코드 {book.records}{span}")
    sections.append(("7. 지난 밤들", others or ["없음"]))
    out = [*lines, ""]
    for heading, body in sections:
        out += [heading, *(f"  {line}" for line in body), ""]
    return "\n".join(out).rstrip("\n")


async def probe_night(settings: Settings, now: datetime, day: date | None = None) -> str:
    """The report, with the masters read from ``KIS_MASTER_URL``."""
    index_text, night_text, errors = await read_masters(settings.master_url)
    calendar = calendar_store(settings).calendar
    return report(settings.data_dir, calendar, now, master_lines(index_text, night_text, errors), day)
