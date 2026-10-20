"""The barrier (#178, #183): which of two barriers around the mid-point the quotes reach first
in the four minutes after a bar close. A kind of prediction (``kinds.py``) beside the
direction: López de Prado's triple barrier (*Advances in Financial Machine Learning*, 2018,
ch. 3), with horizontal barriers at a multiple (one) of a volatility estimate, never closer
than the cost, and a time barrier of four minutes. A scalper's trade read as a long: take
profit, time out, stop out; as a short (of the stock: the sell tax paid), the other way round.

**Width** (fixed at the bar close, ``features.barrier_width``, ``Kind.width``): σ̂₁ is the
exponentially weighted standard deviation (span 100) of the one-minute changes of the closes
of the one-minute bars finished before t, in ticks of the reference price, carried on from the
previous trading day's bars of continuous trading; σ̂₄ = σ̂₁ × √4; w = max(k, σ̂₄ rounded to
whole ticks), k with fewer than 20 bars. The engine fixes it when the window opens, from the
bars the bar close's snapshot holds, the same for every predictor: the answer reads it
(``Followed.width``), every prediction keeps it (``kind_predictions.width_ticks``), and the
algorithms get it as features (``barrier_width``, ``barrier_sigma``). No algorithm changes it.

**Answer** (answer version 2, ``barrier_answer``). The reference is the integrated mid-point
at the bar close t; the upper barrier U is the reference plus w ticks of its tick size, the
lower L the reference less w ticks (half a tick off the grid when the reference is). Of the
usable integrated best quotes received in [t, t + 4 minutes) (the touch label's quotes:
``kinds.Followed``, crossed ones included, ``Kind.crossed``), U is reached when one's best
bid is at or above it (one could have sold there at the market), L when one's best ask is at
or below it (one could have bought there):

- up_first: U was reached first (by received time), or L was not;
- down_first: L was reached first, or U was not;
- timeout: neither;
- both first reached at the same received time: no class (``ambiguous``): the order is unknown.
  One crossed quote can reach both (its bid at U and its ask at L): ``ambiguous`` too.

The answer versions: 1 dropped crossed quotes (#183 to #187); 2 counts them (#187), as the
touch label does (target version 3): a crossed quote's best bid and best ask are each a price
one could trade at, from two venues. Answer version 1's rows stay in the DB; no code makes
them any more.

A window the shared rules cannot score (no mid-point at t, a REST snapshot, sixty seconds
without a quote, a restart) has no class (#160). The path keeps w, σ̂₄ (``sigma``), k, the
tick, the reference, U and L, the round-trip cost in ticks (``cost``), the best quote at t
(the last received before it), when each barrier was first reached and the best quote then,
the last best quote received before t + 4 minutes, the quotes received and whether they went
silent: the barrier P&L and the diagnosis are computed from it.

**Bar rule** (``bar_barrier``), for days with one-minute bars only (training): the reference,
k and w from t's close and the bars up to it; #160's close-position rule with its one-tick
buffer (``touch.close_side``, ``touch.BAR_BUFFER``) sets the barriers' prices, and the first
of the four minutes' bars whose high reaches the upper one (low the lower one) is the minute
it was reached. The earlier minute wins; both in the same minute: no class (``same minute``),
the order within it is unknown, so training leaves it out. It is the rule of both answer
versions: counting crossed quotes (#187) changed the quote answer, not the bars.

**Output**: the three probabilities ``p_up_first``, ``p_timeout``, ``p_down_first``, and no
other field. The width is the engine's, not the forecast's: it is kept beside the output.

**Baselines.** ``barrier-climate`` (``BarrierClimate``): each symbol and 30-minute slot's class
shares over the latest 20 training days, a table the nightly training refits and the 08:00
swap installs. ``barrier-persist`` (``BarrierPersist``): the last window's answer goes on, with
certainty; not predicted when that window could not be scored.

**Horizon ladder** (#263, ``barrier_kind``, ``BARRIER_LADDER``): the same barrier with a time
barrier of 15, 30 or 60 minutes, kinds ``barrier_15m``, ``barrier_30m`` and ``barrier_60m``
(``Kind.horizon``). Everything above holds with H minutes for four: the width is σ̂₁ × √H
(``features.barrier_width``, never below k), the answer reads the quotes of [t, t + H), the
bar rule the bars of those minutes, and a bar close is predicted only when t + H is before the
closing auction. Each has its own baselines, ``barrier-climate-<H>m`` (its table counts its own
answers) and ``barrier-persist-<H>m`` (the window that ended at t, opened at t − H minutes). The
algorithms' barrier features stay the four-minute barrier's.

**Statistics** (``barrier_stats``), beside the three-class ones (action classes: up_first and
down_first): the barrier P&L (research only, ``barrier_profit``) of the calls: a long on
up_first, a short on down_first, nothing on timeout; the trades, their total and the mean per
trade with its normal 95% interval, all calls and each side. The shorts are costed as shorts of
the stock (the sell tax paid); beside them (#267, ``inverse_profit``), the shorts through the
symbol's single-stock inverse product, which pays no sell tax, and all calls with those.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from krx_quant.cards import Card
from krx_quant.consensus import slot
from krx_quant.features import BARRIER_MINUTES, Snapshot, Width, barrier_width
from krx_quant.feeds.base import KST, kst_day
from krx_quant.kinds import (
    Answer,
    Followed,
    Kind,
    KindForecast,
    KindInputs,
    KindScored,
    Quote,
    WidthRule,
    went_silent,
)
from krx_quant.pricing import SELL_TAX, flat_ticks, tick_size
from krx_quant.products import INVERSE_PRODUCTS
from krx_quant.reference import Bar
from krx_quant.stats import interval_json, mean_interval
from krx_quant.touch import BAR_BUFFER, close_side

if TYPE_CHECKING:  # the algorithm contract builds on the kinds: its types only
    from krx_quant.algorithms.contract import KindSample, Model

NAME = "barrier"
CLASSES = ("up_first", "timeout", "down_first")
UP_FIRST, TIMEOUT, DOWN_FIRST = CLASSES
ANSWER_VERSION = 2  # crossed quotes count (#187); 1 dropped them
# Why a window has no barrier class though the shared rules could score it.
AMBIGUOUS = "ambiguous"  # both barriers first reached at the same received time
SAME_MINUTE = "same minute"  # the bar rule: both reached within the same one-minute bar

CLIMATE = "barrier-climate"
PERSIST = "barrier-persist"
CLIMATE_DAYS = 20  # the latest training days its table counts, as the direction's climate
CLIMATE_DEFAULT = f"{CLIMATE}-default"
ALL_DAY = "all"  # the slot of a symbol's whole day, the table's back-off


def _at(t: datetime | None) -> str | None:
    return t.astimezone(KST).isoformat() if t is not None else None


def _first(quotes: Sequence[Quote], reached: Any) -> Quote | None:
    """The first received of ``quotes`` that ``reached`` says reached a barrier."""
    return min((q for q in quotes if reached(q)), key=lambda q: q.received_at, default=None)


def barrier_answer(window: Followed) -> Answer:
    """The barrier answer of a window (answer version 2): its class and path (module doc)."""
    width, anchor, tick = window.width, window.anchor, window.tick
    upper = lower = None
    if width is not None and anchor is not None and tick is not None:
        upper, lower = anchor + width.ticks * tick, anchor - width.ticks * tick
    up = _first(window.quotes, lambda q: q.bid >= upper) if upper is not None else None
    down = _first(window.quotes, lambda q: q.ask <= lower) if lower is not None else None
    last = window.quotes[-1] if window.quotes else None
    path: dict[str, Any] = {
        "w": width.ticks if width is not None else None,
        "sigma": width.sigma if width is not None else None,
        "k": window.k,
        "tick": tick,
        "anchor": anchor,
        "upper": upper,
        "lower": lower,
        "cost": window.cost,
        "bid_start": window.bid_start,
        "ask_start": window.ask_start,
        "up_at": _at(up.received_at) if up else None,
        "up_bid": up.bid if up else None,
        "up_ask": up.ask if up else None,
        "down_at": _at(down.received_at) if down else None,
        "down_bid": down.bid if down else None,
        "down_ask": down.ask if down else None,
        "bid_end": last.bid if last else None,
        "ask_end": last.ask if last else None,
        "quotes": len(window.quotes),
        "silent": went_silent(window.bar_close, window.end, [q.received_at for q in window.quotes]),
    }
    if upper is None:
        return Answer(None, None, path)
    if up is not None and down is not None and up.received_at == down.received_at:
        return Answer(None, AMBIGUOUS, path)
    if up is not None and (down is None or up.received_at < down.received_at):
        return Answer(UP_FIRST, None, path)
    return Answer(DOWN_FIRST if down is not None else TIMEOUT, None, path)


def bar_barrier(
    closing: Bar, after: Sequence[Bar], fee_rate: float, *, width: Width | None = None
) -> Answer | None:
    """The barrier answer of the window after a bar close (four minutes, or the kind's horizon,
    #263), from one-minute bars (training on a day without quotes): the bar that ended at the
    bar close (``closing``), the bars of the window (``after``, those with trades, oldest
    first) and the width at the bar
    close from the bars up to it (``width``). None without a trade in those minutes, or without
    a width.

    The close is the reference, k the flat band at it. As the touch label's bar rule
    (``touch.bar_touch``), a best bid reaching a price shows in the bars as a trade a tick above
    it: the upper barrier is the close plus w ticks, one tick of buffer, and one more if the
    close traded on the bid (``close_side`` −1: the mid-point was above it); the lower one the
    close less as many, one more if it traded on the ask. Each is reached in the first bar
    whose high (low) gets there; the earlier minute wins, both in the same one is unknown
    (``same minute``). The next bar's open only places the close: it is never a feature.
    """
    if not after or width is None:
        return None
    price = closing.close
    tick = tick_size(price)
    side = close_side(closing, after[0].open)
    upper = price + (width.ticks + BAR_BUFFER + (side == -1)) * tick
    lower = price - (width.ticks + BAR_BUFFER + (side == 1)) * tick
    up = next((b for b in after if b.high >= upper), None)
    down = next((b for b in after if b.low <= lower), None)
    path = {
        "w": width.ticks,
        "sigma": width.sigma,
        "k": flat_ticks(price, fee_rate),
        "tick": tick,
        "anchor": price,
        "side": side,
        "upper": upper,
        "lower": lower,
        "up_minute": _at(up.start) if up else None,
        "down_minute": _at(down.start) if down else None,
    }
    if up is not None and down is not None and up.start == down.start:
        return Answer(None, SAME_MINUTE, path)
    if up is not None and (down is None or up.start < down.start):
        return Answer(UP_FIRST, None, path)
    return Answer(DOWN_FIRST if down is not None else TIMEOUT, None, path)


def barrier_width_at(s: Snapshot) -> Width | None:
    """The barrier's width at a bar close (``Kind.width``): the snapshot's (``Snapshot.barrier``)."""
    return s.barrier


def barrier_profit(predicted: str, outcome: str, path: Mapping[str, Any]) -> float | None:
    """The barrier P&L of a call (#178, research only), in ticks of the reference price, by the
    barrier's own rule, the answer being ``outcome`` and its path ``path``:

    - up_first (a long): bought at the bar close's best ask; sold at U if U was reached first,
      at the best bid when L was reached if L was, else at the last best bid before the window
      ended;
    - down_first (a short of the stock): sold at the bar close's best bid;
      bought back at L if L was reached first, at the best ask when U was reached if U was,
      else at the last best ask;
    - timeout: no trade (None).

    Less the round-trip cost (the sell tax and both commissions, ``path["cost"]`` in ticks).
    None without the quotes it needs. Every share fills at the best quote: queues, sizes and
    delays are left out, as in the direction's touch P&L."""
    if predicted == TIMEOUT:
        return None
    if predicted == UP_FIRST:
        bought = path.get("ask_start")
        sold = {UP_FIRST: path.get("upper"), DOWN_FIRST: path.get("down_bid")}.get(
            outcome, path.get("bid_end")
        )
    else:
        sold = path.get("bid_start")
        bought = {DOWN_FIRST: path.get("lower"), UP_FIRST: path.get("up_ask")}.get(
            outcome, path.get("ask_end")
        )
    tick, cost = path.get("tick"), path.get("cost")
    if bought is None or sold is None or not tick or cost is None:
        return None
    return (float(sold) - float(bought)) / float(tick) - float(cost)


def inverse_profit(
    predicted: str, outcome: str, path: Mapping[str, Any], symbol: str, bar_close: datetime
) -> float | None:
    """A call's barrier P&L with a short traded through ``symbol``'s single-stock inverse product
    (#267, ``products.INVERSE_PRODUCTS``), as the direction's ``pnl_inverse``: the same prices and
    fills, less both commissions on the product's notional only (no sell tax on an ETF; an
    L-times inverse product gives a share's exposure for 1/|L| of its price). None for a short
    on a symbol without one, or before it was listed. A long is as ``barrier_profit``."""
    profit = barrier_profit(predicted, outcome, path)
    if profit is None or predicted != DOWN_FIRST:
        return profit
    product = INVERSE_PRODUCTS.get(symbol)
    anchor = path.get("anchor")
    if product is None or kst_day(bar_close) < product.listed or anchor is None:
        return None
    cost, tick = float(path["cost"]), float(path["tick"])
    commissions = cost - float(anchor) * SELL_TAX / tick  # both commissions on a share, ticks
    return profit + cost - commissions / product.exposure


def _pnl_json(trades: Sequence[float]) -> dict[str, Any]:
    """Trades' P&L as the direction's touch P&L shows it: how many, their total and the mean
    per trade with its normal 95% interval (ticks)."""
    return {
        "trades": len(trades),
        "total_ticks": sum(trades),
        "per_trade": interval_json(mean_interval(trades)),
    }


def barrier_stats(
    algorithm: str, records: Sequence[KindScored], baselines: Mapping[str, Sequence[KindScored]]
) -> dict[str, Any]:
    """``algorithm``'s barrier P&L (module doc), from its scored barrier predictions: of all its
    trades (``pnl``), its longs (up_first calls) and its shorts (down_first calls)."""
    sides: dict[str, list[float]] = {UP_FIRST: [], DOWN_FIRST: []}
    inverse_shorts: list[float] = []
    for r in records:
        predicted, outcome = CLASSES[r.predicted], CLASSES[r.outcome]
        if (profit := barrier_profit(predicted, outcome, r.path)) is not None:
            sides[predicted].append(profit)
        if (
            predicted == DOWN_FIRST
            and (inverse := inverse_profit(predicted, outcome, r.path, r.symbol, r.bar_close)) is not None
        ):
            inverse_shorts.append(inverse)
    return {
        "n": len(records),
        "pnl": _pnl_json([*sides[UP_FIRST], *sides[DOWN_FIRST]]),
        "long": _pnl_json(sides[UP_FIRST]),
        "short": _pnl_json(sides[DOWN_FIRST]),
        # The shorts through the inverse product (#267), and every trade with them.
        "pnl_inverse": _pnl_json([*sides[UP_FIRST], *inverse_shorts]),
        "short_inverse": _pnl_json(inverse_shorts),
    }


def _certain(label: str) -> KindForecast:
    up, timeout, down = (1.0 if c == label else 0.0 for c in CLASSES)
    return KindForecast((up, timeout, down))


class BarrierPersist:
    """The last window's barrier answer goes on (#183): the window one horizon back (four
    minutes, or a longer barrier's, #263)."""

    version = "v1"

    def __init__(self, minutes: int = BARRIER_MINUTES) -> None:
        self.minutes = minutes
        self.name = PERSIST if minutes == BARRIER_MINUTES else f"{PERSIST}-{minutes}m"
        m = minutes
        self.card = Card(
            scientist=None,
            character=f"기준 예측: 직전 {m}분 장벽",
            logic=(
                f"직전 {m}분(t − {m}분 → t) 창의 장벽 정답(그 창의 봉 마감 t − {m}분에 정한 폭과 "
                f"장벽으로, 같은 규칙)이 다음 {m}분에도 그대로라고 본다. 저장된 예측이 아니라 시장 보기가 "
                "따라간 호가로 정한다(그 봉 마감에 예측이 없었어도 된다). 그 창을 채점할 수 없으면(호가 "
                f"끊김, REST 대체 호가, 재시작, 두 장벽에 같은 받은 시각에 닿음, 그날 처음 {m}분) 예측하지 "
                "않는다."
            ),
            unique_features=(),
            training="배우지 않는다.",
            forecasting=(
                "직전 창의 클래스에 확률 1, 나머지 둘에 0을 준다. 예측하지 않은 봉 마감은 상태가 "
                "no_previous다."
            ),
            failure=(
                "추세가 꺾이거나, 조용하던 장이 움직이기 시작하거나, 움직이던 장이 잦아들 때 틀린다. 한 "
                "방향으로 이어지는 추세나 변동성 군집이 강한 장에서는 맞기 쉬운 강한 기준이다."
            ),
        )

    def predict(self, inputs: KindInputs) -> KindForecast | None:
        previous = inputs.previous
        if previous is None or previous.label is None:
            return None
        return _certain(previous.label)


class BarrierClimate:
    """Each symbol and 30-minute slot's class shares over the latest training days (#183): a
    table (``kinds.TableBaseline``) whose parameters are ``<symbol>.<slot>.<field>``
    (``"005930.09:00.p_up_first"``: the bar closes 09:01–09:30) for ``p_up_first``,
    ``p_timeout``, ``p_down_first`` and ``n`` (the answers counted), and the same of
    ``<symbol>.all``, the symbol's whole day. A slot the table lacks backs off to its symbol's
    day, then to a third each (a baseline predicts every bar close, so the lifts pair it with
    every forecast)."""

    days = CLIMATE_DAYS
    card = Card(
        scientist=None,
        character="기준 예측: 장벽 시간대 기후",
        logic=(
            "종목 × 30분 시간대(봉 마감 09:01–09:30은 09:00 칸)마다, 최근 20거래일 학습 창의 장벽 "
            "정답(녹화일은 호가, 그 전 날은 1분봉 규칙)에서 위 먼저·시간 만료·아래 먼저의 비율을 그 칸의 "
            "모든 봉 마감에 낸다. 시간대 말고는 아무 정보도 쓰지 않는다. 장벽 폭이 변동성을 따라가도 "
            "남는 시간대 버릇(개장·마감 무렵의 닿음 비율, 한쪽으로 기우는 시간대)만 맞혀서 이기는 모델을 "
            "거르는 기준이다."
        ),
        unique_features=(),
        training=(
            "밤 학습이 매일 최근 20거래일(그날까지)의 장벽 정답으로 칸마다 클래스 수를 센다. 종목마다 "
            "하루 전체의 비율도 센다. 클래스가 없는 정답(채점할 수 없거나, 1분봉 날에 같은 분에 양쪽이 "
            "닿아 순서를 모르는 창)은 세지 않는다. 성적 비교 없이 늘 새 표로 바꾸고, 다음 거래일 08:00 "
            "교체 때부터 쓴다. 학습 전에는 1/3씩이다."
        ),
        forecasting=(
            "그 칸의 클래스 비율을 세 확률로 낸다. 표에 없는 칸은 그 종목 하루 전체의 비율, 그것도 "
            "없으면 1/3씩이다(모든 봉 마감에 내야 lift가 모든 예측과 짝지어진다). 예측의 모델 버전은 그 "
            "표의 버전이다(`barrier-climate-<날짜>-<해시>`, 학습 전에는 `barrier-climate-default`)."
        ),
        failure=(
            "그날 장의 성격이 학습 창의 시간대 버릇과 다를 때 틀린다: 한 방향으로 쏠린 추세장, 뉴스로 "
            "변동성이 갑자기 바뀐 날(폭은 지난 변동성을 따라가므로 갑자기 빨라지면 닿음이, 느려지면 시간 "
            "만료가 는다). 1분봉 날의 정답은 1분봉 규칙의 근사라 녹화일의 호가 정답과 조금 다르다."
        ),
    )

    def __init__(self, table: Model | None = None, minutes: int = BARRIER_MINUTES) -> None:
        self.minutes = minutes  # its barrier's horizon (#263): its table counts that barrier's answers
        self.name = CLIMATE if minutes == BARRIER_MINUTES else f"{CLIMATE}-{minutes}m"
        if minutes != BARRIER_MINUTES:
            self.card = replace(
                self.card,
                character=f"기준 예측: 장벽 {minutes}분 시간대 기후",
                forecasting=self.card.forecasting.replace(CLIMATE, self.name),
            )
        self.table = table
        self.version = table.version if table is not None else f"{self.name}-default"

    def using(self, table: Model) -> BarrierClimate:
        return BarrierClimate(table, self.minutes)

    def default_table(self) -> Model:
        from krx_quant.algorithms.contract import Model  # the contract builds on the kinds

        return Model(f"{self.name}-default", {})

    def fit(self, samples: Sequence[KindSample]) -> Model:
        """The table of ``samples``' barrier answers (those with a class); the defaults when
        none has one."""
        from krx_quant.algorithms.contract import Model  # the contract builds on the kinds

        answered = [
            (s, s.answer.label) for s in samples if s.answer is not None and s.answer.label is not None
        ]
        if not answered:
            return self.default_table()
        cells: dict[str, list[str]] = {}
        for s, label in answered:
            for cell in (f"{s.symbol}.{slot(s.bar_close)}", f"{s.symbol}.{ALL_DAY}"):
                cells.setdefault(cell, []).append(label)
        params: dict[str, float] = {}
        for cell, seen in sorted(cells.items()):
            for c in CLASSES:
                params[f"{cell}.p_{c}"] = seen.count(c) / len(seen)
            params[f"{cell}.n"] = float(len(seen))
        first = min(s.bar_close for s, _ in answered).astimezone(KST).date()
        last = max(s.bar_close for s, _ in answered).astimezone(KST).date()
        canonical = json.dumps(params, sort_keys=True)
        digest = hashlib.sha256(f"{canonical}|{first}|{last}|{len(answered)}".encode()).hexdigest()[:6]
        return Model(
            version=f"{self.name}-{last:%Y%m%d}-{digest}",
            params=params,
            trained=f"{first}..{last}, {len(answered)} samples, {len(cells)} cells",
            trained_until=last,
            target_version=ANSWER_VERSION,  # the barrier's answers
        )

    def predict(self, inputs: KindInputs) -> KindForecast:
        params = self.table.params if self.table is not None else {}
        for cell in (f"{inputs.symbol}.{slot(inputs.bar_close)}", f"{inputs.symbol}.{ALL_DAY}"):
            if f"{cell}.n" in params:
                up, timeout, down = (params[f"{cell}.p_{c}"] for c in CLASSES)
                return KindForecast((up, timeout, down))
        return KindForecast((1 / 3, 1 / 3, 1 / 3))


def barrier_width_rule(minutes: int) -> WidthRule:
    """The width rule of a barrier ``minutes`` long (#263): the snapshot's own four-minute
    width (``Snapshot.barrier``, the feature's), or σ̂₁ × √``minutes`` from the same bars."""
    if minutes == BARRIER_MINUTES:
        return barrier_width_at

    def width(s: Snapshot) -> Width | None:
        return barrier_width(s.bars, s.previous_bars, s.session_open, s.price, s.flat_ticks, minutes)

    return width


def barrier_kind(minutes: int = BARRIER_MINUTES) -> Kind:
    """The barrier kind of a time barrier ``minutes`` away (#183, #263): its answer, bar rule,
    statistics and baselines, its width σ̂₁ × √``minutes`` (never below k), its windows that
    long. Four minutes is the barrier as it always ran (``barrier``, its baselines' names
    unchanged); a longer one is ``barrier_<minutes>m``, its baselines ``barrier-climate-<m>m``
    and ``barrier-persist-<m>m``."""
    four = minutes == BARRIER_MINUTES
    return Kind(
        name=NAME if four else f"{NAME}_{minutes}m",
        title="장벽" if four else f"장벽 {minutes}분",
        classes=CLASSES,
        class_titles=("위 먼저", "시간 만료", "아래 먼저"),
        action=(UP_FIRST, DOWN_FIRST),
        answer_version=ANSWER_VERSION,
        answer=barrier_answer,
        bar_answer=bar_barrier,
        width=barrier_width_rule(minutes),
        crossed=True,  # a touch (#187)
        baselines=(BarrierClimate(minutes=minutes), BarrierPersist(minutes)),
        stats=barrier_stats,
        horizon=timedelta(minutes=minutes),
        sides=(UP_FIRST, DOWN_FIRST),  # a long's barrier first, a short's (#307)
    )


# The barrier, as registered (``kind_registry.KINDS``), and the horizon ladder's (#263).
BARRIER = barrier_kind()
LADDER_MINUTES = (15, 30, 60)
BARRIER_LADDER: tuple[Kind, ...] = tuple(barrier_kind(m) for m in LADDER_MINUTES)
