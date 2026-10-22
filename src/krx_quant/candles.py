"""Day, week, month and year candles from KIS's period chart query, kept on the server for
the chart (#111, #112).

The chart's day, week, month and year views scroll back as far as KIS has candles, so the
collector keeps every predicted symbol's whole history of finished candles of each period
(``FHKST03010100``, at most 100 a query). The first run takes all of it, paging back from
the last finished trading day. Later runs ask again from the last ``REFRESH_CANDLES`` stored
candles on, replace those KIS has changed since, and add what is new. Prices are adjusted
(``FID_ORG_ADJ_PRC=0``), as on a broker's chart: when KIS adjusts the past (a split, say), the
oldest of the candles asked again no longer matches KIS's, and the whole history is taken again.

**Why the last few are asked again (#264).** A day's candle is stored the evening it ends (from
20:30), while KIS's candle of that day is still provisional: its close is the latest trade (from
2026-09-14 the KRX after-market's) until KIS finalizes it to the 15:30 closing auction. The
scheduled job ``probe-close-264`` (2026-10-20) found the stored KRX day candle's close equal to
the closing auction's on every day of the year but one per symbol, which equalled the day's last
bar. Before, a run asked only the newest stored candle again and took a change in it for an
adjustment. So a day was fixed only while it was still the newest, by taking the whole history
again.

**Checked once (#264).** A file written before this carries no ``CHECKED`` note (in the parquet
schema's metadata). Its next run asks KIS for the whole history and replaces each stored candle
KIS now has otherwise, so no provisional day kept from before stays. Each file also notes the
last ``NOTED`` candles a run replaced because KIS had changed them (the day, the run's time, the
fields). ``probe-close`` prints them: when KIS finalizes a day, and what it changes.

A candle is stored by the day its period starts (KST): the day itself, the week's Monday,
the month's 1st, the year's 1 January, whatever day KIS labels it with. Only finished
candles are stored: a day once over (after the 20:30 backfill), a week, month or year once
the last finished trading day is in a later one. The screen makes the rest (today, and the
days of a period not stored yet) from the day candles and the day's one-minute bars.

The market is the integrated one (KRX + NXT) where KIS has it, else KRX's, as for the
one-minute bars; a symbol keeps the market of its first history. The queries share the
backfill's pace inside the REST limit.

Beside them each symbol keeps its day candles of the KRX market alone (``J``, #264), in the day
folder's ``krx.parquet``: an integrated day candle opens at NXT's 08:00 pre-market open, not at
the KRX opening auction (its high, low and close are KRX's, docs/kis-feeds.md), and the daily
``open_to_close`` kind is answered and trained on the KRX auctions' open and close. They are
kept by the same rules: the whole history at first, then the last few asked again and the new
finished days added, all of it again when KIS adjusted the past.

**Their ex-date code (#310).** A KRX day candle also keeps KIS's 락 구분 of the day as KIS gives it
(``flng_cls_code``, stored as ``ex_code``): KIS's adjusted prices do not take cash dividends out, and
this code is how ``open_to_close_5d`` knows a window holds an ex-dividend day (``daily.ex_dividend_of``;
the scheduled job ``probe-dividends-310``, 2026-10-21). A KRX day file written before it is noted
``CHECKED`` only, not ``KRX_CHECKED``: its next run asks KIS for the whole history once more, as
#264's check does, and fills the code into every stored candle KIS answers, without noting those as
candles KIS changed. The integrated files keep no such column.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import aiohttp
import pyarrow as pa
import pyarrow.parquet as pq

from krx_quant.backfill import BARS_FINAL_AT, MARKETS
from krx_quant.feeds.base import KST, Row, to_int
from krx_quant.kis import KisError, RestClient
from krx_quant.market_calendar import MarketCalendar
from krx_quant.ratelimit import RateLimiter

log = logging.getLogger(__name__)

CANDLES_TR = "FHKST03010100"
CANDLES_PATH = "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
CANDLES_PER_QUERY = 100
# The periods kept, by the name the Engine API takes and KIS's code for it.
PERIODS = {"day": "D", "week": "W", "month": "M", "year": "Y"}
# Paging back from the first day KIS could have: it answers the newest candles in a range.
_FIRST_DAY = date(1900, 1, 1)
# A history is at most this many queries (60,000 candles); more means KIS keeps repeating.
_MAX_QUERIES = 600
# The KRX market's day candles (#264): beside the integrated ones, in the day folder.
KRX_DAYS_FILE = "krx.parquet"
KRX_MARKET = "J"
# Each run asks KIS again for this many of a file's newest stored candles (#264): one query, as
# before. The oldest of them is the anchor: as stored, KIS did not adjust the past, and the newer
# ones are replaced where KIS changed them (a day stored provisional the evening it ended); not,
# the whole history is taken again. A day stays in it for five trading days, ten runs or so.
REFRESH_CANDLES = 5
# The fields a replaced candle is noted with when KIS changed them.
FIELDS = ("open", "high", "low", "close", "volume", "value")
# The file's notes, in its parquet schema's metadata (#264): the whole-file check it has had, and
# the last candles a run replaced.
_NOTES_KEY = b"krx_quant"
CHECKED = 1  # a file without it (written before #264's refresh) is checked whole once
# A KRX day file's (#310): once it holds KIS's 락 구분 (``EX_CODE``). One noted less is checked whole
# once more, which fills the code into the candles KIS answers again.
KRX_CHECKED = 2
NOTED = 30  # the replaced candles a file notes, newest last

CANDLE_SCHEMA = pa.schema(
    [
        ("symbol", pa.string()),
        ("date", pa.date32()),
        ("venue", pa.string()),  # UN (integrated KRX + NXT) or KRX, as for the one-minute bars
        ("open", pa.int64()),
        ("high", pa.int64()),
        ("low", pa.int64()),
        ("close", pa.int64()),
        ("volume", pa.int64()),
        ("value", pa.int64()),  # traded value in won
    ]
)
# A KRX day candle's ex-date code (#310): KIS's 락 구분 (``flng_cls_code``) as given, "00" on a day
# without one; null on a candle stored before it that KIS has not answered again.
EX_CODE = "ex_code"
KRX_DAY_SCHEMA = CANDLE_SCHEMA.append(pa.field(EX_CODE, pa.string()))


@dataclass(frozen=True)
class Replaced:
    """A stored candle a run replaced because KIS had changed it since (#264)."""

    day: date  # the candle's date (its period's first day)
    at: datetime  # the run that replaced it
    fields: tuple[str, ...]  # which of ``FIELDS`` KIS changed

    def as_dict(self) -> dict[str, Any]:
        return {"date": self.day.isoformat(), "at": self.at.isoformat(), "fields": list(self.fields)}

    @classmethod
    def of(cls, d: dict[str, Any]) -> Replaced:
        return cls(date.fromisoformat(d["date"]), datetime.fromisoformat(d["at"]), tuple(d["fields"]))


@dataclass(frozen=True)
class Notes:
    """A candle file's notes (#264): the whole-file check it has had (0: none) and the last
    candles a run replaced, oldest first."""

    checked: int = 0
    replaced: tuple[Replaced, ...] = ()


def period_start(day: date, period: str) -> date:
    """The day the ``period`` holding ``day`` starts: the day, its week's Monday, its month's
    1st or its year's 1 January."""
    if period == "week":
        return day - timedelta(days=day.weekday())
    if period == "month":
        return day.replace(day=1)
    if period == "year":
        return date(day.year, 1, 1)
    return day


def finished(period: str, start: date, last: date) -> bool:
    """Whether the candle of ``period`` starting ``start`` is final once ``last`` (the last
    finished trading day) is: a day up to it, a longer period before the one holding it."""
    return start <= last if period == "day" else start < period_start(last, period)


def candles_path(data_dir: Path, period: str, symbol: str) -> Path:
    """A symbol's candles of ``period`` under the data directory (and in the Object Store)."""
    return data_dir / "candles" / f"period={period}" / f"symbol={symbol}" / "candles.parquet"


def krx_days_path(data_dir: Path, symbol: str) -> Path:
    """A symbol's day candles of the KRX market alone (#264), beside its day candles."""
    return candles_path(data_dir, "day", symbol).with_name(KRX_DAYS_FILE)


def read_candles(data_dir: Path, period: str, symbol: str) -> list[Row]:
    """The stored candles, oldest first; none if there is no file."""
    return _read(candles_path(data_dir, period, symbol))


def read_krx_days(data_dir: Path, symbol: str) -> list[Row]:
    """The stored KRX day candles (#264), oldest first; none if there is no file."""
    return _read(krx_days_path(data_dir, symbol))


def _read(path: Path) -> list[Row]:
    if not path.exists():
        return []
    rows: list[Row] = pq.read_table(path).to_pylist()
    return rows


def read_notes(path: Path) -> Notes:
    """A candle file's notes (#264); none for a file without them, or no file."""
    if not path.exists():
        return Notes()
    raw = (pq.read_schema(path).metadata or {}).get(_NOTES_KEY)
    if raw is None:
        return Notes()
    notes = json.loads(raw)
    return Notes(int(notes.get("checked", 0)), tuple(Replaced.of(r) for r in notes.get("replaced", ())))


def write_candles(
    data_dir: Path, period: str, symbol: str, rows: Sequence[Row], replaced: Sequence[Replaced] = ()
) -> None:
    """Replace the stored candles, whole or not at all, noted as checked with ``replaced``."""
    _write(candles_path(data_dir, period, symbol), rows, replaced)


def write_krx_days(
    data_dir: Path, symbol: str, rows: Sequence[Row], replaced: Sequence[Replaced] = ()
) -> None:
    """Replace the stored KRX day candles (#264), whole or not at all, with their ex-date codes
    (#310, ``EX_CODE``: null for a row without one), noted as checked (``KRX_CHECKED``) with
    ``replaced``."""
    _write(krx_days_path(data_dir, symbol), rows, replaced, KRX_DAY_SCHEMA, KRX_CHECKED)


def _write(
    path: Path,
    rows: Sequence[Row],
    replaced: Sequence[Replaced] = (),
    schema: pa.Schema = CANDLE_SCHEMA,
    checked: int = CHECKED,
) -> None:
    """Write the file whole (a temporary file renamed), noted as ``checked``, with the last
    ``NOTED`` of ``replaced``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    notes = {"checked": checked, "replaced": [r.as_dict() for r in list(replaced)[-NOTED:]]}
    noted = schema.with_metadata({_NOTES_KEY: json.dumps(notes).encode()})
    pq.write_table(pa.Table.from_pylist(list(rows), schema=noted), tmp)
    tmp.rename(path)


def stored_candles(
    data_dir: Path, period: str, symbol: str, before: datetime, limit: int, today: date
) -> tuple[str | None, list[Row]]:
    """The venue and the last ``limit`` stored candles that start before ``before`` (a candle
    starts at 00:00 KST of its period's first day), oldest first. The period holding ``today``
    is left out even once stored: the screen builds it from the day's one-minute bars."""
    rows = read_candles(data_dir, period, symbol)
    now = period_start(today, period)
    found = [r for r in rows if datetime.combine(r["date"], time(), KST) < before and r["date"] < now]
    return (rows[-1]["venue"] if rows else None), found[-limit:]


async def candles_page(
    client: RestClient,
    period: str,
    market: str,
    symbol: str,
    first: date,
    last: date,
    ex_code: bool = False,
) -> list[tuple[date, Row]]:
    """One query: the candles KIS labels from ``first`` to ``last`` (at most 100, the newest), each
    with KIS's label and stored by its period's first day, with its ex-date code if ``ex_code``
    (#310, a KRX day candle's); KIS's blank padding left out."""
    params = {
        "FID_COND_MRKT_DIV_CODE": market,
        "FID_INPUT_ISCD": symbol,
        "FID_INPUT_DATE_1": first.strftime("%Y%m%d"),
        "FID_INPUT_DATE_2": last.strftime("%Y%m%d"),
        "FID_PERIOD_DIV_CODE": PERIODS[period],
        "FID_ORG_ADJ_PRC": "0",  # adjusted prices
    }
    body = await client.get(CANDLES_TR, CANDLES_PATH, params)
    return [
        (d, _candle(symbol, MARKETS[market], period_start(d, period), r, ex_code))
        for r in body.get("output2") or []
        if (d := _date(r)) is not None and first <= d <= last
    ]


class CandleKeeper:
    def __init__(
        self,
        client: RestClient,
        calendar: MarketCalendar,
        data_dir: Path,
        symbols: Sequence[str],
        pace: RateLimiter,
    ) -> None:
        self._client = client
        self._calendar = calendar
        self._data_dir = data_dir
        self._symbols = tuple(symbols)
        # The backfill's own (Backfiller.pace): one spacing for both, inside the shared REST
        # limit (docs/kis-feeds.md), with no burst where one hands over to the other.
        self._pace = pace

    async def run(self, now: datetime) -> int:
        """Bring every symbol's candles of every period, and its KRX day candles (#264), up to
        the last finished trading day; return how many files changed. A symbol that fails is
        logged and left for the next run."""
        last = last_finished(self._calendar, now)
        changed = 0
        for period in PERIODS:
            for symbol in self._symbols:
                try:
                    changed += await self.update(period, symbol, last, now)
                except (KisError, aiohttp.ClientError, OSError, ValueError, KeyError, TimeoutError) as e:
                    log.warning("%s candles of %s not updated: %s", period, symbol, e)
        for symbol in self._symbols:
            try:
                changed += await self.update_krx_days(symbol, last, now)
            except (KisError, aiohttp.ClientError, OSError, ValueError, KeyError, TimeoutError) as e:
                log.warning("KRX day candles of %s not updated: %s", symbol, e)
        return changed

    async def update(self, period: str, symbol: str, last: date, now: datetime) -> bool:
        """Bring the symbol's candles of ``period`` up to ``last`` (``_keep``); True if the file
        changed. The market is the one the stored history came from."""
        return await self._keep(candles_path(self._data_dir, period, symbol), period, None, symbol, last, now)

    async def update_krx_days(self, symbol: str, last: date, now: datetime) -> bool:
        """The same for the symbol's KRX day candles (#264): the KRX market (``J``) only, so the
        open is the opening auction's; True if the file changed."""
        return await self._keep(krx_days_path(self._data_dir, symbol), "day", KRX_MARKET, symbol, last, now)

    async def _keep(
        self, path: Path, period: str, market: str | None, symbol: str, last: date, now: datetime
    ) -> bool:
        """Ask KIS again for the last ``REFRESH_CANDLES`` stored candles and the finished ones up
        to ``last``: replace the stored ones KIS changed since (a day stored provisional), add the
        new ones. If KIS changed the oldest of them (it adjusted the past), take the whole history
        again; a file not checked yet (``CHECKED``; a KRX day file, ``KRX_CHECKED``: its ex-date
        codes, #310) is compared whole once. True if it changed."""
        krx = market == KRX_MARKET  # the KRX day file (#264), with its ex-date codes (#310)
        label = "KRX day" if krx else period
        marks = (EX_CODE,) if krx else ()
        schema, level = (KRX_DAY_SCHEMA, KRX_CHECKED) if krx else (CANDLE_SCHEMA, CHECKED)
        stored = _read(path)
        notes = read_notes(path)
        if stored:
            asked = market or _market(stored[-1]["venue"])
            end = max(last, stored[-1]["date"])
            if notes.checked < level:
                fresh = await self._walk(period, asked, symbol, _FIRST_DAY, end, krx)
                if not fresh:
                    log.info("no %s candles of %s at KIS to check the stored ones against", label, symbol)
                    return False
                rows, changes, filled = _merged(stored, fresh, period, last, marks)
                log.info(
                    "%s candles of %s checked whole against KIS (#264): %s replaced, %s added%s",
                    label,
                    symbol,
                    len(changes),
                    len(rows) - len(stored),
                    f", ex-date codes filled in {filled} (#310)" if krx else "",
                )
                _write(path, rows, _noted(notes, changes, now), schema, level)
                return True
            anchor = stored[-min(REFRESH_CANDLES, len(stored))]
            fresh = await self._walk(period, asked, symbol, anchor["date"], end, krx)
            again = next((r for r in fresh if r["date"] == anchor["date"]), None)
            if again is not None and _prices(again) == _prices(anchor):
                rows, changes, _ = _merged(stored, fresh, period, last, marks)
                if rows == stored:
                    return False
                for day, fields in changes:
                    log.info(
                        "%s candle of %s on %s changed at KIS since it was stored (%s): replaced",
                        label,
                        symbol,
                        day,
                        ", ".join(fields),
                    )
                _write(path, rows, _noted(notes, changes, now), schema, level)
                return True
            log.info(
                "%s candles of %s changed at %s (adjusted?): taking the whole history again",
                label,
                symbol,
                anchor["date"],
            )
        if market is None:
            taken = await self._history(period, symbol, last)
        else:
            taken = await self._walk(period, market, symbol, _FIRST_DAY, last, krx)
        history = [r for r in taken if finished(period, r["date"], last)]
        if not history:
            log.info("no %s candles of %s at KIS yet; the next run asks again", label, symbol)
            return False
        _write(path, history, notes.replaced, schema, level)
        return True

    async def _history(self, period: str, symbol: str, last: date) -> list[Row]:
        """The whole history from the first market that has any."""
        for market in MARKETS:
            rows = await self._walk(period, market, symbol, _FIRST_DAY, last)
            if rows:
                return rows
        return []

    async def _walk(
        self, period: str, market: str, symbol: str, first: date, last: date, ex_code: bool = False
    ) -> list[Row]:
        """The candles KIS labels from ``first`` to ``last``, oldest first, paging back from
        ``last``; each by its period's first day, with its ex-date code if ``ex_code`` (#310)."""
        candles: dict[date, Row] = {}
        end = last
        for _ in range(_MAX_QUERIES):
            await self._pace.acquire()
            labelled = await candles_page(self._client, period, market, symbol, first, end, ex_code)
            new = [(d, r) for d, r in labelled if r["date"] not in candles]
            candles.update((r["date"], r) for _, r in new)
            if not new:
                break
            earliest = min(d for d, _ in new)
            if period_start(earliest, period) <= first:  # back at the first's period: no more to ask
                break
            end = earliest - timedelta(days=1)
        else:
            raise ValueError(f"{symbol} {period} candles still going after {_MAX_QUERIES} queries")
        return [candles[d] for d in sorted(candles)]


def last_finished(calendar: MarketCalendar, now: datetime) -> date:
    """The last trading day that is over: today only once the day's bars are final (20:30)."""
    local = now.astimezone(KST)
    today = local.date()
    if calendar.is_trading_day(today) and local.time() >= BARS_FINAL_AT:
        return today
    return calendar.previous_trading_day(today)


def _merged(
    stored: Sequence[Row], fresh: Sequence[Row], period: str, last: date, marks: Sequence[str] = ()
) -> tuple[list[Row], list[tuple[date, tuple[str, ...]]], int]:
    """The stored candles with KIS's finished ones over them, oldest first, the stored ones KIS
    changed (each with the fields), and how many stored ones had ``marks`` (a KRX day candle's
    ex-date code, #310) filled in: a stored candle without one (written before it) takes KIS's,
    which is not a change KIS made. A stored candle KIS did not answer stays."""
    by_day = {r["date"]: r for r in stored}
    changes: list[tuple[date, tuple[str, ...]]] = []
    filled = 0
    for r in fresh:
        if not finished(period, r["date"], last):
            continue
        old = by_day.get(r["date"])
        if old is not None:
            fields = tuple(f for f in FIELDS if old[f] != r[f]) + tuple(
                f for f in marks if old.get(f) is not None and old[f] != r.get(f)
            )
            fills = any(old.get(f) is None and r.get(f) is not None for f in marks)
            if not fields and not fills:
                continue
            if fields:
                changes.append((r["date"], fields))
            elif fills:
                filled += 1
        by_day[r["date"]] = r
    return [by_day[d] for d in sorted(by_day)], changes, filled


def _noted(notes: Notes, changes: Sequence[tuple[date, tuple[str, ...]]], now: datetime) -> list[Replaced]:
    """The file's replaced candles with this run's after them."""
    at = now.astimezone(KST)
    return [*notes.replaced, *(Replaced(day, at, fields) for day, fields in changes)]


def _market(venue: str) -> str:
    """The KIS market code a venue was stored from."""
    return next(code for code, v in MARKETS.items() if v == venue)


def _date(r: dict[str, str]) -> date | None:
    try:
        return datetime.strptime(r.get("stck_bsop_date") or "", "%Y%m%d").date()
    except ValueError:
        return None


def _candle(symbol: str, venue: str, start: date, r: dict[str, str], ex_code: bool = False) -> Row:
    row: Row = {
        "symbol": symbol,
        "date": start,
        "venue": venue,
        "open": to_int(r["stck_oprc"]),
        "high": to_int(r["stck_hgpr"]),
        "low": to_int(r["stck_lwpr"]),
        "close": to_int(r["stck_clpr"]),
        "volume": to_int(r["acml_vol"]),
        "value": to_int(r["acml_tr_pbmn"]),
    }
    if ex_code:  # a KRX day candle's (#310): KIS's 락 구분 as given
        row[EX_CODE] = r.get("flng_cls_code")
    return row


def _prices(r: Row) -> tuple[int, int, int, int]:
    return (r["open"], r["high"], r["low"], r["close"])
