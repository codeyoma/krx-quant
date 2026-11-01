"""Hit rates beside the nightly swap (#437): whether the model the log loss promotes calls up, flat
and down right less often.

The nightly training promotes a challenger whose mean log loss on the validation days is lower than
the champion's (``nightly``). The user judges a model by its hit rate: the class it calls (its most
likely; a tie goes to the middle class, then the first, ``pricing.most_likely``) against the answer.
A lower log loss can come with a lower hit rate. So each report that compared the two models
(``nightly._Trainer._train``) also has ``hit_rate`` (``compare``). It changes nothing of the verdict:
the rule stays the mean log loss.

- **The pairs.** The bar closes both models predicted: the same samples with the same answers (the
  night's target, or the kind's answer version), the ones the log losses' paired difference has.
- **Each model** (``challenger``, ``champion``): its hit rate on the pairs, its coverage (the share of
  its calls of the kind's action classes: up and down for the direction) and its confusion matrix
  (``confusion``: the class called, then the class that happened, counts).
- **The difference** (``difference``): the hit rate gained, the challenger's less the champion's, on
  the pairs; and taken a trading day at a time (``by_trading_day``: each day's mean gain one value,
  the days' mean with its 95% Student t interval, as ``stats.day_lift`` takes a lift; None from fewer
  than two days).
- **By reference price** (``by_reference``): all of the above on the recorded days (``mid``) and on
  the backfilled ones (``close``) apart.
- **The two rules** (``log_loss_rule``, ``hit_rate_rule``: ``promote`` or ``keep``): the log loss's
  comparison (lower: promote; the report's ``promoted`` also promotes a champion of another target,
  ``forced``), and what a hit-rate-first rule would have chosen (the challenger hit more of the
  pairs: promote; a tie keeps). The second is only compared, never applied.
- **The mismatch** (``mismatch``): the log loss promotes while the hit rate fell.

The Telegram summary's line of such an algorithm ends with the two hit rates, and says so when the
rules part (``summary_text``).

**Over nights** (``krx-quant nightly-hit-rates``, the scheduled job ``nightly-hit-rates-437``): the
latest ``HIT_RATE_NIGHTS`` nights whose promotion lists carry ``hit_rate``, by algorithm (``Tally``):
the nights compared, those the log loss promoted and of them those whose hit rate fell, those the
hit-rate rule would have promoted, those the rules parted on, the forced ones; the nights' mean hit
rates and coverages, and the mean difference over all the nights, over those the log loss promoted,
and by reference price; then each night of a mismatch. Two nights in a row share two of their three
validation days, so the nights are not independent: there is no interval over them. Counts, rates
and dates only, never a price.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from krx_quant.algorithms.contract import KindSample, Sample
from krx_quant.config import Settings
from krx_quant.feeds.base import kst_day
from krx_quant.kinds import DIRECTION, Kind
from krx_quant.object_store import ObjectStore, get_json, open_store
from krx_quant.stats import day_lift

log = logging.getLogger(__name__)

HIT_RATE = "hit_rate"  # the report's key
PROMOTE, KEEP = "promote", "keep"
# The nights the summary waits for (the scheduled job's condition) and reads: two weeks of trading
# nights, each validated on its latest three days.
HIT_RATE_NIGHTS = 10
# The latest promotion lists the scheduled job's condition reads, every night: it counts those that
# carry ``hit_rate`` among them.
SCANNED = 2 * HIT_RATE_NIGHTS
PROMOTIONS = "promotions/"  # ``nightly.promotions_key``'s

# A validated sample (``nightly._losses``): the model's log loss on it, and the class it called.
Validated = tuple[Sample | KindSample, float, str]


@dataclass(frozen=True)
class _Call:
    """A model's call at a bar close, as a lift pairs it (``stats.Paired``)."""

    symbol: str
    bar_close: datetime
    correct: bool


@dataclass(frozen=True)
class _Pair:
    sample: Sample | KindSample
    challenger: str  # the class each model called
    champion: str
    happened: str  # the class of the sample's answer


def happened(sample: Sample | KindSample) -> str | None:
    """The class of a sample's answer: the direction's outcome, a kind's label."""
    if isinstance(sample, KindSample):
        return sample.label
    return None if sample.outcome is None else sample.outcome.value


def compare(
    challenger: Sequence[Validated],
    champion: Sequence[Validated],
    references: Mapping[date, str],
    kind: Kind,
    log_loss_promotes: bool,
) -> dict[str, Any] | None:
    """A report's ``hit_rate``: the two models' calls on the bar closes both predicted, of ``kind``'s
    classes, by reference price (``references``: each day's), with what each rule chooses (the log
    loss's: ``log_loss_promotes``). None without a pair."""
    theirs = {(s.symbol, s.bar_close): called for s, _, called in champion}
    pairs = [
        _Pair(s, called, theirs[(s.symbol, s.bar_close)], answer)
        for s, _, called in challenger
        if (s.symbol, s.bar_close) in theirs and (answer := happened(s)) is not None
    ]
    if not pairs:
        return None
    grouped: dict[str, list[_Pair]] = {}
    for p in pairs:
        grouped.setdefault(str(references.get(kst_day(p.sample.bar_close))), []).append(p)
    found = _compared(pairs, kind)
    difference = found["difference"]
    return {
        **found,
        "by_reference": {ref: _compared(rows, kind) for ref, rows in sorted(grouped.items())},
        "log_loss_rule": PROMOTE if log_loss_promotes else KEEP,
        "hit_rate_rule": PROMOTE if difference > 0 else KEEP,
        "mismatch": log_loss_promotes and difference < 0,
    }


def _compared(pairs: Sequence[_Pair], kind: Kind) -> dict[str, Any]:
    mine = [_Call(p.sample.symbol, p.sample.bar_close, p.challenger == p.happened) for p in pairs]
    theirs = [_Call(p.sample.symbol, p.sample.bar_close, p.champion == p.happened) for p in pairs]
    gained = sum(m.correct for m in mine) - sum(t.correct for t in theirs)
    by_day = day_lift(mine, theirs)
    return {
        "samples": len(pairs),
        "days": len({kst_day(p.sample.bar_close) for p in pairs}),
        "challenger": _model([(p.challenger, p.happened) for p in pairs], kind),
        "champion": _model([(p.champion, p.happened) for p in pairs], kind),
        "difference": gained / len(pairs),
        "by_trading_day": None
        if by_day is None
        else {"days": by_day.n, "mean": by_day.value, "low": by_day.low, "high": by_day.high},
    }


def _model(calls: Sequence[tuple[str, str]], kind: Kind) -> dict[str, Any]:
    """A model's calls (the class called, the class that happened): its hit rate, its coverage and
    its confusion matrix."""
    confusion = {called: dict.fromkeys(kind.classes, 0) for called in kind.classes}
    for called, answer in calls:
        confusion[called][answer] += 1
    return {
        "hit_rate": sum(called == answer for called, answer in calls) / len(calls),
        "coverage": sum(called in kind.action for called, _ in calls) / len(calls),
        "confusion": confusion,
    }


def summary_text(found: Mapping[str, Any]) -> str:
    """The end of an algorithm's line in the night's Telegram summary: `` · 맞힘률 41.0% → 39.8%``
    (the champion's, then the challenger's), then whether the rules parted."""
    text = f" · 맞힘률 {found['champion']['hit_rate']:.1%} → {found['challenger']['hit_rate']:.1%}"
    if found["mismatch"]:
        return text + " (불일치: 로그 손실로는 승격, 맞힘률은 떨어짐)"
    if found["hit_rate_rule"] != found["log_loss_rule"]:
        return text + f" (맞힘률 우선이면 {'승격' if found['hit_rate_rule'] == PROMOTE else '유지'})"
    return text


# Over nights


@dataclass(frozen=True)
class Compared:
    """An algorithm's night whose report compared its two models' hit rates."""

    day: date
    forced: bool  # promoted whatever the scores: its champion learned another target (#170)
    hit_rate: Mapping[str, Any]


@dataclass
class Tally:
    """An algorithm's nights compared (``Compared``), oldest first."""

    algorithm: str
    kind: str
    nights: list[Compared] = field(default_factory=list)

    def count(self, rule: str, picked: str = PROMOTE) -> int:
        return sum(n.hit_rate[rule] == picked for n in self.nights)

    @property
    def mismatches(self) -> list[Compared]:
        return [n for n in self.nights if n.hit_rate["mismatch"]]

    @property
    def parted(self) -> int:
        return sum(n.hit_rate["log_loss_rule"] != n.hit_rate["hit_rate_rule"] for n in self.nights)


def promotion_lists(store: ObjectStore) -> list[tuple[date, str]]:
    """The nights' promotion lists in ``store``, oldest first: each night and its key."""
    found = []
    for key in store.keys(PROMOTIONS):
        try:
            found.append((date.fromisoformat(key.removeprefix(PROMOTIONS).removesuffix(".json")), key))
        except ValueError:
            continue
    return sorted(found)


def compared_nights(
    store: ObjectStore, nights: int = HIT_RATE_NIGHTS, scanned: int | None = None
) -> list[tuple[date, list[Mapping[str, Any]]]]:
    """The latest ``nights`` nights whose promotion lists carry ``hit_rate``, of the latest
    ``scanned`` lists (None: all), oldest first, each with its reports that do."""
    lists = promotion_lists(store)
    found: list[tuple[date, list[Mapping[str, Any]]]] = []
    for day, key in reversed(lists if scanned is None else lists[-scanned:]):
        if len(found) == nights:
            break
        data = get_json(store, key)
        reports = [r for r in (data or {}).get("reports", []) if isinstance(r.get(HIT_RATE), dict)]
        if reports:
            found.append((day, reports))
    return found[::-1]


def nights_with_hit_rates(settings: Settings) -> int:
    """How many of the latest ``SCANNED`` promotion lists carry ``hit_rate``, up to
    ``HIT_RATE_NIGHTS`` (the scheduled job's ``Facts.hit_rate_nights``): 0 without the store, or when
    it cannot be read (logged; the jobs' other conditions are read all the same)."""
    store = open_store(settings)
    if store is None:
        return 0
    try:
        return len(compared_nights(store, HIT_RATE_NIGHTS, SCANNED))
    except (BotoCoreError, ClientError, OSError, ValueError):
        log.exception("could not count the nights with hit rates in the store")
        return 0


def tallies(nights: Iterable[tuple[date, Iterable[Mapping[str, Any]]]]) -> list[Tally]:
    """Each algorithm's nights compared, in the order the reports first name them."""
    by_name: dict[str, Tally] = {}
    for day, reports in nights:
        for r in reports:
            name = str(r["algorithm"])
            tally = by_name.setdefault(name, Tally(name, str(r.get("kind", DIRECTION))))
            tally.nights.append(Compared(day, bool(r.get("forced")), r[HIT_RATE]))
    return list(by_name.values())


def lines(nights: Sequence[tuple[date, Sequence[Mapping[str, Any]]]], wanted: int) -> list[str]:
    """The command's output: by algorithm, its nights compared; counts, rates and dates, no price."""
    if not nights:
        return ["nightly hit rates (#437): no night's promotion list carries the hit rates yet"]
    found = tallies(nights)
    every = [n for t in found for n in t.nights]
    promoted = sum(t.count("log_loss_rule") for t in found)
    out = [
        f"nightly hit rates (#437): {len(nights)} of the {wanted} nights asked for,"
        f" {nights[0][0]}..{nights[-1][0]}",
        "  each night: the champion and the challenger on the bar closes both predicted of its validation"
        " days; two nights in a row share two of their three, so the nights are not independent",
        "  the swap rule is the mean log loss; the hit-rate rule (the challenger hit more of the pairs)"
        " is compared, never applied",
        "",
        f"all algorithms: {len(every)} comparisons; the log loss promoted {promoted}, of which the hit rate"
        f" fell on {sum(len(t.mismatches) for t in found)} (mismatch); the hit-rate rule would have"
        f" promoted {sum(t.count('hit_rate_rule') for t in found)}; the rules parted on"
        f" {sum(t.parted for t in found)}",
    ]
    for t in found:
        out += ["", *_tally_lines(t)]
    return out


def _tally_lines(t: Tally) -> list[str]:
    promoted = [n for n in t.nights if n.hit_rate["log_loss_rule"] == PROMOTE]
    below = [n for n in t.mismatches if (high := _high(n)) is not None and high < 0]
    out = [
        f"{t.algorithm} ({t.kind}): {len(t.nights)} nights, {sum(n.hit_rate['samples'] for n in t.nights)}"
        f" pairs; the log loss promoted {len(promoted)}, of which the hit rate fell on {len(t.mismatches)}"
        f" (mismatch; {len(below)} with the trading-day interval below 0); the hit-rate rule would have"
        f" promoted {t.count('hit_rate_rule')}; the rules parted on {t.parted}; forced"
        f" {sum(n.forced for n in t.nights)}",
        f"  hit rate (each night's on its pairs, the nights' mean):"
        f" champion {_mean_of(t.nights, 'champion')},"
        f" challenger {_mean_of(t.nights, 'challenger')}; difference {_signed(_differences(t.nights))},"
        f" on the nights the log loss promoted {_signed(_differences(promoted))}",
        f"  coverage: champion {_mean_of(t.nights, 'champion', 'coverage')},"
        f" challenger {_mean_of(t.nights, 'challenger', 'coverage')}",
    ]
    grouped = [n.hit_rate.get("by_reference", {}) for n in t.nights]
    parts = []
    for ref in sorted({ref for by_ref in grouped for ref in by_ref}):
        rows = [by_ref[ref] for by_ref in grouped if ref in by_ref]
        parts.append(
            f"{ref} {_signed(_mean([r['difference'] for r in rows]))} ({len(rows)} nights,"
            f" {sum(r['samples'] for r in rows)} pairs)"
        )
    out.append("  difference by reference price: " + ("; ".join(parts) if parts else "-"))
    for n in t.mismatches:
        out.append(f"  mismatch {n.day}: {_signed(n.hit_rate['difference'])}{_interval(n)}")
    return out


def _differences(nights: Sequence[Compared]) -> float | None:
    return _mean([n.hit_rate["difference"] for n in nights])


def _mean_of(nights: Sequence[Compared], model: str, value: str = HIT_RATE) -> str:
    found = _mean([n.hit_rate[model][value] for n in nights])
    return "-" if found is None else f"{found:.4f}"


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _signed(value: float | None) -> str:
    return "-" if value is None else f"{value:+.4f}"


def _high(n: Compared) -> float | None:
    by_day = n.hit_rate.get("by_trading_day")
    return None if by_day is None else by_day.get("high")


def _interval(n: Compared) -> str:
    by_day = n.hit_rate.get("by_trading_day")
    if by_day is None or by_day.get("low") is None or by_day.get("high") is None:
        return ""
    return (
        f" (by trading day {by_day['mean']:+.4f}, 95% {by_day['low']:+.4f}..{by_day['high']:+.4f},"
        f" {by_day['days']} days)"
    )
