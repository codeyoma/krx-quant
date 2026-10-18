"""The daily kinds' features (#264): what an algorithm of ``open_to_close`` may know at 08:50,
each from data before that time only.

A feature is a number of the day (``DailyFeature``) computed from the day's context
(``DayContext``): the symbol's KRX days before the day, the day's band, the overnight US moves,
the previous trading day's flows, the NXT pre-market price, and the calendar. The context is
made the same way live (``LiveFeatures``: at 08:50, from the stored files and the trades the
engine received) and in training (``samples``: every past day, from the stored histories), so
a model is trained on the values it is given:

- **US overnight** (``us_sox``, ``us_nvda``, ``us_mu``, ``vix``): live, the 07:30 macro read of
  the day (``macro.reading_before``, before 08:50), else the stored US daily closes; in
  training, the stored closes (``us_daily.move_before``, the macro read's rule).
- **Flows** (``foreign_net``, ``institution_net``): the previous trading day's net buys from
  KIS's daily supply (``state/supply``, #226; stored at 20:30 that day), as a share of that
  day's integrated volume (the KRX one where there is none). Toss's values are not read.
- **The days before** (``oc_1``, ``cc_1``, ``gap_1``, ``range_1``, ``oc_5``, ``vol_ratio``,
  ``sigma``, ``climate_up``, ``climate_down``): the previous trading day's open→close,
  close→close, overnight gap and range, the last five days' mean open→close, each over the
  day's σ̂ (``open_to_close``); the volatility regime (the last 20 days' root mean square of
  the open→close change over the last 250's, and σ̂'s level); the class shares of the last
  250 answered days. A day whose previous trading day's candle is not stored has none of the
  previous day's.
- **NXT pre-market** (``premarket_gap``): the last integrated trade from 08:40 to 08:50, else
  from 08:00 to 08:30, over the previous KRX close, in σ̂. Those minutes have NXT's trades
  only: KRX's pre-open trades (08:30–08:40) are at the previous close. Live from the trades
  received before 08:50; in training from the one-minute bars kept (about a year: before, and
  before NXT opened on 2025-03-04, it is missing).
- **The calendar** (``weekday``, ``month_end``, ``month_start``): the weekday, the month's
  last trading day, its first three.
- **Night futures** (``night_futures``): the KOSPI200 night session (#264 part 1, ``H0MFCNT0``,
  18:00–06:00) of the night before the day, the one that began the previous trading day's
  evening: its last trade's change from the previous close (KIS's ``change_pct``), as a
  fraction. Live from the trades received, else the recording; in training from the recording
  (``events/date=…/feed=H0MFCNT0``). The recording began with #290, so every day before it has
  none, and the model learns it only as it accumulates.

The KRX expected opening price is not a feature: the engine's book records keep no expected
price (``feeds.stock``). A missing value is None; the model handles it (``algorithms.cox``).
"""

from __future__ import annotations

import math
from bisect import bisect_left
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from krx_quant.backfill import MINUTE_BARS_TR, read_bars
from krx_quant.candles import read_candles
from krx_quant.cards import Meaning
from krx_quant.daily import (
    Band,
    DailyAlgorithm,
    DailyKind,
    DailySample,
    DayPrices,
    StoredDays,
    history_before,
)
from krx_quant.features import FeatureVector
from krx_quant.feeds import NIGHT_FUTURES, TRADE
from krx_quant.feeds.base import KST
from krx_quant.kinds import Answer
from krx_quant.macro import Observation, read_partition, reading_before
from krx_quant.market_calendar import MarketCalendar
from krx_quant.supply import KIS, read_supply
from krx_quant.us_daily import US_DAILY, level_before, move_before, read_us_daily

# The night futures' session: from 18:00 of an evening to 06:00 the next morning (#264).
NIGHT_START, NIGHT_END = time(18, 0), time(6, 0)
# The NXT pre-market's minutes read, the later first: KRX's pre-open trades (08:30–08:40) are
# at the previous close.
PREMARKET = ((time(8, 40), time(8, 50)), (time(8, 0), time(8, 30)))
CLIMATE_DAYS = 250
VOL_SHORT, VOL_LONG = 20, 250
TURN_FIRST_DAYS = 3  # the month's first trading days that are its start


@dataclass(frozen=True)
class DayContext:
    """What is known of a symbol at a day's prediction time (module doc)."""

    symbol: str
    day: date
    history: Sequence[DayPrices]  # the KRX days before ``day``, oldest first
    labels: Sequence[str | None]  # their answers' classes, aligned with ``history``
    previous_day: date | None  # the trading day before ``day``
    band: Band | None
    us: Mapping[str, float | None] = field(default_factory=dict)  # sox, nvda, mu: moves; vix: level
    flows: Mapping[str, float | None] = field(default_factory=dict)  # foreign, institution: shares
    premarket: float | None = None  # the NXT pre-market's last price (won)
    month_end: bool = False
    month_start: bool = False
    night_futures: float | None = None  # #264 part 1's hook: no history yet

    @property
    def previous(self) -> DayPrices | None:
        """The previous trading day's KRX day, if its candle is stored."""
        if self.history and self.history[-1].day == self.previous_day:
            return self.history[-1]
        return None

    @property
    def sigma(self) -> float | None:
        return None if self.band is None or self.band.sigma <= 0 else self.band.sigma


@dataclass(frozen=True)
class DailyFeature:
    meaning: Meaning
    value: Callable[[DayContext], float | None]


def _over_sigma(x: float | None, ctx: DayContext) -> float | None:
    sigma = ctx.sigma
    return None if x is None or sigma is None else x / sigma


def _oc_1(ctx: DayContext) -> float | None:
    prev = ctx.previous
    return None if prev is None else _over_sigma(prev.change, ctx)


def _before_previous(ctx: DayContext) -> DayPrices | None:
    return ctx.history[-2] if ctx.previous is not None and len(ctx.history) >= 2 else None


def _cc_1(ctx: DayContext) -> float | None:
    prev, before = ctx.previous, _before_previous(ctx)
    return None if prev is None or before is None else _over_sigma(prev.close / before.close - 1, ctx)


def _gap_1(ctx: DayContext) -> float | None:
    prev, before = ctx.previous, _before_previous(ctx)
    return None if prev is None or before is None else _over_sigma(prev.open / before.close - 1, ctx)


def _range_1(ctx: DayContext) -> float | None:
    prev = ctx.previous
    return None if prev is None else _over_sigma((prev.high - prev.low) / prev.open, ctx)


def _oc_5(ctx: DayContext) -> float | None:
    if ctx.previous is None or len(ctx.history) < 5:
        return None
    return _over_sigma(sum(d.change for d in ctx.history[-5:]) / 5, ctx)


def _rms(days: Sequence[DayPrices]) -> float | None:
    return math.sqrt(sum(d.change**2 for d in days) / len(days)) if days else None


def _vol_ratio(ctx: DayContext) -> float | None:
    if len(ctx.history) < VOL_LONG:
        return None
    short, long = _rms(ctx.history[-VOL_SHORT:]), _rms(ctx.history[-VOL_LONG:])
    if not short or not long:
        return None
    return math.log(short / long)


def _sigma_level(ctx: DayContext) -> float | None:
    return None if ctx.sigma is None else math.log(ctx.sigma)


def _shares(ctx: DayContext) -> tuple[float, float]:
    recent = [label for label in ctx.labels if label is not None][-CLIMATE_DAYS:]
    n = len(recent)
    return (recent.count("up") + 1) / (n + 3), (recent.count("down") + 1) / (n + 3)


def _premarket_gap(ctx: DayContext) -> float | None:
    prev = ctx.previous
    if prev is None or ctx.premarket is None or ctx.premarket <= 0:
        return None
    return _over_sigma(math.log(ctx.premarket / prev.close), ctx)


def _vix(ctx: DayContext) -> float | None:
    level = ctx.us.get("vix")
    return None if level is None or level <= 0 else math.log(level)


_KNOWN = "08:50에 안다."
DAILY_FEATURES: dict[str, DailyFeature] = {
    "us_sox": DailyFeature(
        Meaning(
            "밤사이 필라델피아 반도체지수(SOX)의 변화: 전 거래일 아침에 알던 종가에서 그날 아침에 아는 "
            "종가까지(07:30 매크로 읽기와 같은 규칙).",
            "비율",
            "그날 07:30 매크로 읽기. 학습은 Yahoo 일봉 10년.",
        ),
        lambda c: c.us.get("sox"),
    ),
    "us_nvda": DailyFeature(
        Meaning("밤사이 엔비디아(NVDA)의 변화(규칙은 us_sox와 같다).", "비율", "그날 07:30 매크로 읽기."),
        lambda c: c.us.get("nvda"),
    ),
    "us_mu": DailyFeature(
        Meaning("밤사이 마이크론(MU)의 변화(규칙은 us_sox와 같다).", "비율", "그날 07:30 매크로 읽기."),
        lambda c: c.us.get("mu"),
    ),
    "vix": DailyFeature(
        Meaning("그날 아침에 아는 VIX 종가의 로그.", "로그", "그날 07:30 매크로 읽기."), _vix
    ),
    "foreign_net": DailyFeature(
        Meaning(
            "전 거래일 외국인 순매수량 ÷ 그날 통합 거래량(KIS 일별 수급, #226).",
            "비율",
            "전 거래일 20:30에 받는다.",
        ),
        lambda c: c.flows.get("foreign"),
    ),
    "institution_net": DailyFeature(
        Meaning("전 거래일 기관 순매수량 ÷ 그날 통합 거래량.", "비율", "전 거래일 20:30에 받는다."),
        lambda c: c.flows.get("institution"),
    ),
    "oc_1": DailyFeature(Meaning("전 거래일 시가 단일가 → 종가 단일가 변화 ÷ 그날의 σ̂.", "σ̂", _KNOWN), _oc_1),
    "cc_1": DailyFeature(Meaning("전 거래일 종가 → 종가 변화 ÷ σ̂.", "σ̂", _KNOWN), _cc_1),
    "gap_1": DailyFeature(Meaning("전 거래일의 시가 갭(그 전 종가 → 시가) ÷ σ̂.", "σ̂", _KNOWN), _gap_1),
    "range_1": DailyFeature(Meaning("전 거래일 (고가 − 저가) ÷ 시가 ÷ σ̂.", "σ̂", _KNOWN), _range_1),
    "oc_5": DailyFeature(Meaning("최근 5거래일 시가 → 종가 변화의 평균 ÷ σ̂.", "σ̂", _KNOWN), _oc_5),
    "vol_ratio": DailyFeature(
        Meaning(
            "변동성 국면: 최근 20거래일과 250거래일 시가 → 종가 변화의 제곱평균제곱근 비의 로그.",
            "로그",
            _KNOWN,
        ),
        _vol_ratio,
    ),
    "sigma": DailyFeature(Meaning("그날 보합 폭의 σ̂(최근 60거래일)의 로그.", "로그", _KNOWN), _sigma_level),
    "climate_up": DailyFeature(
        Meaning("최근 250거래일 정답 중 상승의 비율(각 클래스에 1을 더해).", "비율", _KNOWN),
        lambda c: _shares(c)[0],
    ),
    "climate_down": DailyFeature(
        Meaning("최근 250거래일 정답 중 하락의 비율(각 클래스에 1을 더해).", "비율", _KNOWN),
        lambda c: _shares(c)[1],
    ),
    "premarket_gap": DailyFeature(
        Meaning(
            "NXT 프리마켓의 마지막 통합 체결가(08:40–08:50, 없으면 08:00–08:30) ÷ 전 거래일 KRX 종가의 "
            "로그 ÷ σ̂.",
            "σ̂",
            "08:50 전에 받은 체결. 학습은 1분봉(약 1년, 그 전은 없음).",
        ),
        _premarket_gap,
    ),
    "weekday": DailyFeature(Meaning("요일(월 0 … 금 4).", "요일", _KNOWN), lambda c: float(c.day.weekday())),
    "month_end": DailyFeature(
        Meaning("그달의 마지막 거래일이면 1, 아니면 0.", "0/1", _KNOWN), lambda c: float(c.month_end)
    ),
    "month_start": DailyFeature(
        Meaning("그달의 첫 3거래일이면 1, 아니면 0.", "0/1", _KNOWN), lambda c: float(c.month_start)
    ),
    "night_futures": DailyFeature(
        Meaning(
            "KOSPI200 야간선물(#264 1부)의 전 거래일 저녁 18:00부터 06:00까지 마지막 체결의 전일 대비 "
            "등락률(KIS `change_pct`). 녹화가 #290 배포부터라 그 전 날은 없다.",
            "비율",
            "06:00에 그 밤이 끝난다. 실시간은 받은 체결, 학습은 녹화.",
        ),
        lambda c: c.night_futures,
    ),
}


def vector(names: Sequence[str], ctx: DayContext) -> FeatureVector:
    """The features ``names`` of the day."""
    return FeatureVector({n: DAILY_FEATURES[n].value(ctx) for n in names}, names)


def band_of(answer: Answer) -> Band | None:
    """The band an ``open_to_close`` answer was made with (its path's), None without one."""
    value, sigma, scale = (answer.path.get(k) for k in ("band", "sigma", "scale"))
    if value is None or sigma is None or scale is None:
        return None
    return Band(float(value), float(sigma), float(scale))


def month_flags(day: date, trades: Callable[[date], bool]) -> tuple[bool, bool]:
    """Whether ``day`` is its month's last trading day, and one of its first three."""
    following = day + timedelta(days=1)
    end = True
    while following.month == day.month:
        if trades(following):
            end = False
            break
        following += timedelta(days=1)
    first = day.replace(day=1)
    count = sum(1 for i in range((day - first).days + 1) if trades(first + timedelta(days=i)))
    return end, count <= TURN_FIRST_DAYS


def night_window(evening: date) -> tuple[datetime, datetime]:
    """The night session begun on ``evening``: [18:00, 06:00 the next morning), KST."""
    start = datetime.combine(evening, NIGHT_START, KST)
    return start, datetime.combine(evening + timedelta(days=1), NIGHT_END, KST)


def night_move(data_dir: Path, evening: date) -> float | None:
    """The recorded night session begun on ``evening``: its last trade's change from the previous
    close (a fraction), by when received; None without a trade recorded. A night spans the
    evening's partitions and the next day's (by the day received, ``docs/events.md``)."""
    start, end = night_window(evening)
    last: tuple[datetime, float] | None = None
    for day in (evening, evening + timedelta(days=1)):
        root = data_dir / "events" / f"date={day.isoformat()}" / f"feed={NIGHT_FUTURES.tr_id}"
        for partition in sorted(root.glob("symbol=*")):
            table = read_partition(partition, ("received_at", "change_pct"))
            if table is None:
                continue
            for row in table.to_pylist():
                at, change = row["received_at"], row["change_pct"]
                if change is None or not start <= at < end:
                    continue
                if last is None or at >= last[0]:
                    last = (at, float(change) / 100)
    return None if last is None else last[1]


def premarket_from_bars(data_dir: Path, day: date, symbol: str) -> float | None:
    """The NXT pre-market's last price of ``day`` from its kept one-minute bars (``PREMARKET``)."""
    bars = read_bars(data_dir, day, MINUTE_BARS_TR, symbol)
    if not bars:
        return None
    for start, end in PREMARKET:
        within = [b for b in bars if start <= b.start.astimezone(KST).time() < end]
        if within:
            return float(max(within, key=lambda b: b.start).close)
    return None


def flows_by_day(data_dir: Path, state_dir: Path, symbol: str) -> dict[date, dict[str, float | None]]:
    """Each day's foreign and institution net buys from KIS's daily supply, as shares of that
    day's integrated volume (the KRX one where there is none)."""
    volumes = {r["date"]: r["volume"] for r in read_candles(data_dir, "day", symbol) if r.get("volume")}
    out: dict[date, dict[str, float | None]] = {}
    for row in read_supply(state_dir, symbol, KIS):
        volume = volumes.get(row["date"])
        if not volume:
            continue
        out[row["date"]] = {
            "foreign": None if row.get("foreign_net") is None else row["foreign_net"] / volume,
            "institution": None if row.get("institution_net") is None else row["institution_net"] / volume,
        }
    return out


@dataclass
class DailyFiles:
    """The stored histories training reads (#264): the KRX days, the US closes, KIS's supply
    and the kept one-minute bars, on this server."""

    data_dir: Path
    state_dir: Path

    def __post_init__(self) -> None:
        self._days = StoredDays(self.data_dir)
        self._us: dict[str, list[Observation]] | None = None

    def days(self, symbol: str) -> list[DayPrices]:
        return self._days.days(symbol)

    def us(self) -> dict[str, list[Observation]]:
        if self._us is None:
            self._us = {name: read_us_daily(self.data_dir, name) for name in US_DAILY}
        return self._us

    def nights(self) -> dict[date, float | None]:
        """Each recorded night session's move, by the evening it began (``night_move``)."""
        evenings = {
            date.fromisoformat(p.parent.name.removeprefix("date=")) - timedelta(days=back)
            for p in (self.data_dir / "events").glob(f"date=*/feed={NIGHT_FUTURES.tr_id}")
            for back in (0, 1)
        }
        return {e: night_move(self.data_dir, e) for e in sorted(evenings)}


def _us_values(
    closes: Mapping[str, Sequence[Observation]], day: date, previous: date | None
) -> dict[str, Any]:
    out: dict[str, float | None] = {
        name: move_before(closes.get(name, []), day, previous) for name in ("sox", "nvda", "mu")
    }
    out["vix"] = level_before(closes.get("vix", []), day)
    return out


class TradingDays:
    """The market's trading days as training knows them (#264): the days any of the symbols has a
    KRX day candle (a market holiday has none; the symbols have never all been halted at once),
    and past the newest of them the calendar's (KIS's holidays, else weekdays). KIS's holiday
    calendar covers about a year, so ten years back come from the candles. A day's previous
    trading day is the market's, as the engine's calendar gives it live: a symbol's missing
    candle on it stays missing, never the candle before."""

    def __init__(self, days: Iterable[date], calendar: MarketCalendar | None = None) -> None:
        self._days = sorted(set(days))
        self._set = set(self._days)
        self._calendar = calendar

    def trades(self, day: date) -> bool:
        if self._days and day <= self._days[-1]:
            return day in self._set
        return self._calendar.is_trading_day(day) if self._calendar is not None else day.weekday() < 5

    def previous(self, day: date) -> date | None:
        """The last trading day before ``day``; None before the first known."""
        i = bisect_left(self._days, day)
        if i < len(self._days) or not self._days:
            return self._days[i - 1] if i else None
        if self._calendar is not None:
            return self._calendar.previous_trading_day(day)
        back = day - timedelta(days=1)
        while back.weekday() >= 5:
            back -= timedelta(days=1)
        return back


def samples(
    kind: DailyKind,
    files: DailyFiles,
    symbols: Sequence[str],
    names: Sequence[str],
    fee_rate: float,
    *,
    calendar: MarketCalendar | None = None,
    first: date | None = None,
    last: date | None = None,
    premarket: bool = True,
) -> list[DailySample]:
    """Every day's sample of each symbol from ``first`` to ``last`` (both included, default:
    all), oldest first: the features ``names`` as of the day's prediction time, from the days
    before it, and the day's answer by the kind's rule. A day's previous trading day and month
    flags are the market's (``TradingDays``: the symbols' candle days, then the ``calendar``),
    as the engine's calendar gives them live."""
    us = files.us()
    nights: dict[date, float | None] = {}
    out: list[DailySample] = []
    stored = {s: [d for d in files.days(s) if last is None or d.day <= last] for s in symbols}
    market = TradingDays((d.day for days in stored.values() for d in days), calendar)
    for symbol in symbols:
        days = stored[symbol]
        if not days:
            continue
        answers = kind.answers(days, fee_rate) if kind.answers is not None else []
        labels = [a.label for a in answers]
        flows = (
            flows_by_day(files.data_dir, files.state_dir, symbol)
            if "foreign_net" in names or ("institution_net" in names)
            else {}
        )
        if "night_futures" in names and not nights:
            nights.update(files.nights())
        for i, day in enumerate(days):
            if first is not None and day.day < first:
                continue
            previous = market.previous(day.day)
            end, start = month_flags(day.day, market.trades)
            ctx = DayContext(
                symbol=symbol,
                day=day.day,
                history=days[max(0, i - VOL_LONG - 5) : i],
                labels=labels[max(0, i - CLIMATE_DAYS - 5) : i],
                previous_day=previous,
                band=band_of(answers[i]),
                us=_us_values(us, day.day, previous),
                flows=flows.get(previous, {}) if previous is not None else {},
                premarket=premarket_from_bars(files.data_dir, day.day, symbol) if premarket else None,
                month_end=end,
                month_start=start,
                night_futures=nights.get(previous) if previous is not None else None,
            )
            out.append(DailySample(symbol, day.day, vector(names, ctx), answers[i], kind.answer_version))
    return sorted(out, key=lambda s: (s.day, s.symbol))


class LiveFeatures:
    """A daily algorithm's features at the prediction time, in the engine (#264): the same
    context as training's, from the stored files and the trades received before it."""

    def __init__(
        self,
        kind: DailyKind,
        data_dir: Path,
        state_dir: Path,
        calendar: MarketCalendar,
        symbols: Sequence[str],
        fee_rate: float,
    ) -> None:
        self.kind = kind
        self.data_dir = data_dir
        self.state_dir = state_dir
        self.calendar = calendar
        self.symbols = frozenset(symbols)
        self.fee_rate = fee_rate
        # (symbol, day, window) -> the last price received in that window of the pre-market.
        self._premarket: dict[tuple[str, date, int], tuple[datetime, float]] = {}
        # The evening a night session began -> its last trade received, and its move (#264).
        self._nights: dict[date, tuple[datetime, float]] = {}

    def on_event(self, feed: str, row: Mapping[str, Any]) -> None:
        """Keep the integrated trades of the pre-market's minutes received before 08:50, and the
        night futures' last trade of each night."""
        if feed == NIGHT_FUTURES.tr_id:
            self._night(row)
            return
        if feed != TRADE.tr_id or row.get("symbol") not in self.symbols or not row.get("price"):
            return
        at: datetime = row["event_time"]
        local = at.astimezone(KST)
        received: datetime = row["received_at"]
        cut = datetime.combine(local.date(), self.kind.predict_at, KST)
        if received >= cut:
            return
        for i, (start, end) in enumerate(PREMARKET):
            if start <= local.time() < end:
                key = (row["symbol"], local.date(), i)
                kept = self._premarket.get(key)
                if kept is None or at >= kept[0]:
                    self._premarket[key] = (at, float(row["price"]))

    def _night(self, row: Mapping[str, Any]) -> None:
        if row.get("change_pct") is None:
            return
        received: datetime = row["received_at"]
        evening = (received.astimezone(KST) - timedelta(hours=NIGHT_END.hour)).date()
        start, end = night_window(evening)
        if not start <= received < end:
            return
        kept = self._nights.get(evening)
        if kept is None or received >= kept[0]:
            self._nights[evening] = (received, float(row["change_pct"]) / 100)

    def night(self, evening: date) -> float | None:
        """The night session begun on ``evening``: as received, else as recorded."""
        kept = self._nights.get(evening)
        return kept[1] if kept is not None else night_move(self.data_dir, evening)

    def premarket(self, symbol: str, day: date) -> float | None:
        for i in range(len(PREMARKET)):
            if (kept := self._premarket.get((symbol, day, i))) is not None:
                return kept[1]
        return None

    def __call__(
        self,
        algorithm: DailyAlgorithm,
        symbol: str,
        day: date,
        history: Sequence[DayPrices],
        band: Band | None,
    ) -> FeatureVector:
        previous = self.calendar.previous_trading_day(day)
        before = history_before(history, day)
        answers = self.kind.answers(before, self.fee_rate) if before and self.kind.answers else []
        end, start = month_flags(day, self.calendar.is_trading_day)
        ctx = DayContext(
            symbol=symbol,
            day=day,
            history=before[-(VOL_LONG + 5) :],
            labels=[a.label for a in answers][-(CLIMATE_DAYS + 5) :],
            previous_day=previous,
            band=band,
            us=self._us(day, previous),
            flows=flows_by_day(self.data_dir, self.state_dir, symbol).get(previous, {})
            if {"foreign_net", "institution_net"} & set(algorithm.features)
            else {},
            premarket=self.premarket(symbol, day),
            month_end=end,
            month_start=start,
            night_futures=self.night(previous) if "night_futures" in algorithm.features else None,
        )
        return vector(algorithm.features, ctx)

    def _us(self, day: date, previous: date) -> dict[str, float | None]:
        """The morning's US moves: the macro read before the prediction time, else the closes."""
        cut = datetime.combine(day, self.kind.predict_at, KST)
        stored: dict[str, list[Observation]] = {}
        out: dict[str, float | None] = {}
        for name in US_DAILY:
            reading = reading_before(self.data_dir, day, name, cut)
            if reading is not None and reading.get("value") is not None:
                if name == "vix":
                    out[name] = float(reading["value"])
                    continue
                if reading.get("change_pct") is not None:
                    out[name] = float(reading["change_pct"]) / 100
                    continue
            closes = stored.setdefault(name, read_us_daily(self.data_dir, name))
            out[name] = level_before(closes, day) if name == "vix" else move_before(closes, day, previous)
        return out
