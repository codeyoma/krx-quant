"""The scalp event (#706): whether a long bought at the ask at a bar close reaches the price that nets
+0.2 % after costs before it falls 0.3 %, within 15 minutes. A kind of prediction (``kinds.py``)
beside the barrier: the mock scalp policy's own trade (#705), so that an algorithm forecasts the event
the policy acts on instead of the four-minute barrier's.

**The levels** (answer version 1, constants of this module: changing one is a new answer version,
never a silent edit): the entry e is the best ask received last before the bar close t
(``Followed.ask_start``: what a market buy at t pays; nothing after t is read). The costs are the
trader's round trip (``trader/money.py``): the commission ``FEE_RATE`` (0.015 %) a side, on each
side's notional, and the sell tax (``pricing.SELL_TAX``, 0.20 %) on the sale's.

- **Target** T: the lowest price on the KRX tick grid at or above e × (1 + f + ``TARGET_NET``) ÷
  (1 − f − tax), the price whose sale nets ``TARGET_NET`` (+0.2 %) of e after both commissions and the
  tax (about +0.43 % gross), rounded up to the tick of its band (``pricing.tick_size``);
- **Stop** S: e × (1 − ``STOP_GROSS``), 0.3 % gross below the entry (about −0.53 % net), not rounded;
- **Deadline**: ``MINUTES`` (15) minutes from t (``Kind.horizon``).

These are the scalp policy's settings as the user set them (2026-11-18, #705). The policy reads its
own settings; this kind's answers keep these, and a policy run with other levels reads a forecast of
this event, not of its own.

**Answer** (``scalp_answer``). Of the usable integrated best quotes received in [t, t + 15 minutes)
(the touch label's quotes, crossed ones included as the barrier's: each side a price one could trade
at, #187), T is reached when one's best bid is at or above it (the target's limit sell fills), S when
one's best bid is at or below it (a market sell there leaves at the stop):

- target_first: T was reached first (by received time), or S was not;
- stop_first: S was reached first, or T was not;
- timeout: neither by the deadline;
- both first reached at the same received time (two quotes): no class (``ambiguous``);
- no best ask before t: no class (``no entry``).

A window the shared rules cannot score (no mid-point at t, a REST snapshot, sixty seconds without a
quote, a restart) has no class (#160). The path keeps the entry, T, S, their nets (``target_net``,
``stop_net``, fractions of e), the tick at e, the mid-point at t, when each level was first reached
and the best bid then, the last best quote before the deadline, the quotes received and whether they
went silent.

**No bar rule**: a day with one-minute bars only has no ask at t, so it has no scalp answer and is not
trained on (``Kind.bar_answer`` None). The recording holds the days to learn from.

**Output**: the three probabilities ``p_target_first``, ``p_timeout``, ``p_stop_first``, no other field.

**Baselines.** ``scalp-climate`` (``ScalpClimate``): each symbol and 30-minute slot's class shares
over the latest 20 training days, refit nightly and installed at 08:00, as ``barrier-climate``.
``scalp-persist`` (``ScalpPersist``): the window that ended at t (opened at t − 15 minutes) goes on.

**Statistics** (``scalp_stats``), beside the three-class ones (action classes: target_first and
stop_first): the net of the long a target_first call makes (``scalp_net``): bought at e; sold at T
if T was reached first, at the first bid at or below S if S was, else at the last bid before the
deadline; less both commissions and the tax. Its trades, the mean net per trade (a fraction of e)
with its normal 95% interval, and the share of trades that netted above 0 (the user's first measure).
A stop_first call trades nothing: the scalp is long only.

**Kept out of the verdict** (#676, #697): its classes are not the barrier's, so the verdict's pool
never counts it (``verdict.py`` reads the barrier kinds only). Its algorithms are ranked nightly
against ``scalp-climate`` (``ranking.py``) for the scalp policy to read.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from itertools import compress, islice, repeat
from operator import attrgetter, ge, le
from typing import TYPE_CHECKING, Any

from krx_quant.barriers import TIMEOUT, BarrierClimate, BarrierPersist
from krx_quant.cards import Card
from krx_quant.feeds.base import KST
from krx_quant.kinds import Answer, Followed, Kind, KindScored, Quote, went_silent
from krx_quant.pricing import SELL_TAX, tick_size
from krx_quant.stats import INDEPENDENT, DayOverlap, interval_json, mean_interval

if TYPE_CHECKING:  # the algorithm contract builds on the kinds: its types only
    from krx_quant.algorithms.contract import Model

NAME = "scalp"
CLASSES = ("target_first", TIMEOUT, "stop_first")
TARGET_FIRST, _, STOP_FIRST = CLASSES
ANSWER_VERSION = 1  # the levels below: changing one is a new answer version
TARGET_NET = 0.002  # the target's net after costs, a fraction of the entry (#705: +0.2 %)
STOP_GROSS = 0.003  # the stop below the entry, gross (#705: −0.3 %)
MINUTES = 15  # the deadline (#705)
FEE_RATE = 0.00015  # the commission a side (``KRX_FEE_PERCENT``'s default, the trader's)
# Why a window has no scalp class though the shared rules could score it.
AMBIGUOUS = "ambiguous"  # both levels first reached at the same received time
NO_ENTRY = "no entry"  # no best ask before the bar close

CLIMATE = "scalp-climate"
PERSIST = "scalp-persist"


def target_price(entry: float) -> float:
    """T for an entry at ``entry`` (module doc): the lowest tick at or above the price that nets
    ``TARGET_NET``, the tick of that price's band."""
    raw = entry * (1 + FEE_RATE + TARGET_NET) / (1 - FEE_RATE - SELL_TAX)
    tick = tick_size(raw)
    # Rounded first so a price of exactly whole ticks is not pushed up by float error.
    return float(math.ceil(round(raw / tick, 9)) * tick)


def stop_price(entry: float) -> float:
    """S for an entry at ``entry``: ``STOP_GROSS`` below it."""
    return entry * (1 - STOP_GROSS)


def net_return(entry: float, exit_price: float) -> float:
    """A share bought at ``entry`` and sold at ``exit_price``: its net after both commissions and the
    sell tax (``trader/money.net_of``'s round trip), a fraction of ``entry``."""
    return (exit_price * (1 - FEE_RATE - SELL_TAX) - entry * (1 + FEE_RATE)) / entry


def _at(t: datetime | None) -> str | None:
    return t.astimezone(KST).isoformat() if t is not None else None


def _first(quotes: Sequence[Quote], reached: Iterable[bool], ordered: bool) -> Quote | None:
    """The first received of ``quotes`` that reached a level (``barriers._first``'s rule)."""
    hits = compress(quotes, reached)
    if ordered:
        return next(hits, None)
    return min(hits, key=attrgetter("received_at"), default=None)


def scalp_answer(window: Followed) -> Answer:
    """The scalp answer of a window (answer version 1): its class and path (module doc)."""
    entry = window.ask_start
    target = stop = None
    if entry is not None and entry > 0:
        target, stop = target_price(entry), stop_price(entry)
    quotes = window.quotes
    received = list(map(attrgetter("received_at"), quotes))
    ordered = all(map(le, received, islice(received, 1, None)))
    bids = list(map(attrgetter("bid"), quotes))
    up = _first(quotes, map(ge, bids, repeat(target)), ordered) if target is not None else None
    down = _first(quotes, map(le, bids, repeat(stop)), ordered) if stop is not None else None
    last = quotes[-1] if quotes else None
    path: dict[str, Any] = {
        "entry": entry,
        "target": target,
        "stop": stop,
        "target_net": net_return(entry, target) if entry and target else None,
        "stop_net": net_return(entry, stop) if entry and stop else None,
        "tick": tick_size(entry) if entry else None,
        "anchor": window.anchor,
        "bid_start": window.bid_start,
        "target_at": _at(up.received_at) if up else None,
        "target_bid": up.bid if up else None,
        "stop_at": _at(down.received_at) if down else None,
        "stop_bid": down.bid if down else None,
        "bid_end": last.bid if last else None,
        "ask_end": last.ask if last else None,
        "quotes": len(quotes),
        "silent": went_silent(window.bar_close, window.end, received),
    }
    if target is None:
        return Answer(None, NO_ENTRY, path)
    if up is not None and down is not None and up.received_at == down.received_at:
        return Answer(None, AMBIGUOUS, path)
    if up is not None and (down is None or up.received_at < down.received_at):
        return Answer(TARGET_FIRST, None, path)
    return Answer(STOP_FIRST if down is not None else TIMEOUT, None, path)


def scalp_net(outcome: str, path: Mapping[str, Any]) -> float | None:
    """The net of the long a target_first call makes (module doc), a fraction of the entry, the
    answer being ``outcome``: sold at T, at the first bid at or below S, or at the last bid before
    the deadline. None without the prices it needs."""
    entry = path.get("entry")
    sold = {TARGET_FIRST: path.get("target"), STOP_FIRST: path.get("stop_bid")}.get(
        outcome, path.get("bid_end")
    )
    if not entry or sold is None:
        return None
    return net_return(float(entry), float(sold))


def scalp_ticks(outcome: str, path: Mapping[str, Any]) -> float | None:
    """``scalp_net`` in ticks of the entry's tick size (the ranking's ``net_ticks``, #697)."""
    net, entry, tick = scalp_net(outcome, path), path.get("entry"), path.get("tick")
    if net is None or not entry or not tick:
        return None
    return net * float(entry) / float(tick)


def scalp_stats(
    algorithm: str,
    records: Sequence[KindScored],
    baselines: Mapping[str, Sequence[KindScored]],
    overlap: DayOverlap = INDEPENDENT,
) -> dict[str, Any]:
    """``algorithm``'s scalp trades (module doc), from its scored predictions: those it called
    target_first with a net."""
    nets = [
        net
        for r in records
        if CLASSES[r.predicted] == TARGET_FIRST and (net := scalp_net(CLASSES[r.outcome], r.path)) is not None
    ]
    return {
        "n": len(records),
        "trades": len(nets),
        "net_per_trade": interval_json(mean_interval(nets)),
        "won": sum(n > 0 for n in nets) / len(nets) if nets else None,
    }


class ScalpPersist(BarrierPersist):
    """The last scalp window's answer goes on: the window that ended at t."""

    classes = CLASSES

    def __init__(self) -> None:
        super().__init__(MINUTES)
        self.name = PERSIST
        self.card = Card(
            scientist=None,
            character="기준 예측: 직전 15분 스캘프",
            logic=(
                "직전 15분(t − 15분 → t) 창의 스캘프 정답(그 봉 마감의 매도 호가로 산 롱이 순 +0.2% "
                "목표가에 먼저 닿았는지, −0.3% 손절에 먼저 닿았는지, 둘 다 아닌지)이 다음 15분에도 "
                "그대로라고 본다. 그 창을 채점할 수 없으면(호가 끊김, REST 대체 호가, 재시작, 같은 받은 "
                "시각에 둘 다 닿음, 그날 처음 15분) 예측하지 않는다."
            ),
            unique_features=(),
            training="배우지 않는다.",
            forecasting=(
                "직전 창의 클래스에 확률 1, 나머지 둘에 0을 준다. 예측하지 않은 봉 마감은 상태가 "
                "no_previous다."
            ),
            failure="장세가 바뀔 때(추세가 꺾이거나 변동성이 바뀔 때) 틀린다.",
        )


class ScalpClimate(BarrierClimate):
    """Each symbol and 30-minute slot's scalp class shares over the latest training days: a table as
    ``barrier-climate``'s, of the scalp's answers (``<symbol>.<slot>.p_target_first`` and so on)."""

    classes = CLASSES
    answer_version = ANSWER_VERSION
    card = Card(
        scientist=None,
        character="기준 예측: 스캘프 시간대 기후",
        logic=(
            "종목 × 30분 시간대(봉 마감 09:01–09:30은 09:00 칸)마다, 최근 20거래일 학습 창의 스캘프 정답"
            "(녹화일의 호가로 정한다)에서 목표 먼저·시간 만료·손절 먼저의 비율을 그 칸의 모든 봉 마감에 "
            "낸다. 시간대 말고는 아무 정보도 쓰지 않는다. 스캘프의 목표 적중률이 시간대만으로 얼마인지를 "
            "보여 주는 기준이고, 순위(ranking)의 기술 점수가 이것과 견준다."
        ),
        unique_features=(),
        training=(
            "밤 학습이 매일 최근 20거래일(그날까지)의 스캘프 정답으로 칸마다 클래스 수를 센다. 종목마다 하루 "
            "전체의 비율도 센다. 클래스가 없는 정답(채점할 수 없는 창, 매도 호가가 없던 봉 마감, 같은 받은 "
            "시각에 양쪽이 닿은 창)은 세지 않는다. 늘 새 표로 바꾸고, 다음 거래일 08:00 교체 때부터 쓴다. "
            "학습 전에는 1/3씩이다."
        ),
        forecasting=(
            "그 칸의 클래스 비율을 세 확률로 낸다. 표에 없는 칸은 그 종목 하루 전체의 비율, 그것도 없으면 "
            "1/3씩이다. 예측의 모델 버전은 그 표의 버전이다(`scalp-climate-<날짜>-<해시>`, 학습 전에는 "
            "`scalp-climate-default`)."
        ),
        failure=(
            "그날 장의 성격이 학습 창의 시간대 버릇과 다를 때 틀린다: 변동성이 갑자기 커진 날에는 목표와 "
            "손절이 모두 잦아지고, 잦아든 날에는 시간 만료가 는다."
        ),
    )

    def __init__(self, table: Model | None = None) -> None:
        self.minutes = MINUTES
        self.name = CLIMATE
        self.table = table
        self.version = table.version if table is not None else f"{CLIMATE}-default"

    def using(self, table: Model) -> ScalpClimate:
        return ScalpClimate(table)


# The scalp, as registered (``kind_registry.KINDS``), after the barrier's horizon ladder.
SCALP = Kind(
    name=NAME,
    title="스캘프 15분",
    classes=CLASSES,
    class_titles=("목표 먼저", "시간 만료", "손절 먼저"),
    action=(TARGET_FIRST, STOP_FIRST),
    answer_version=ANSWER_VERSION,
    answer=scalp_answer,
    crossed=True,  # a touch, as the barrier's (#187)
    baselines=(ScalpClimate(), ScalpPersist()),
    stats=scalp_stats,
    horizon=timedelta(minutes=MINUTES),
    sides=(TARGET_FIRST, STOP_FIRST),
)
