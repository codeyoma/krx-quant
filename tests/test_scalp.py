"""The scalp event (#706): a long bought at the ask at a bar close, +0.2 % net or −0.3 % gross first,
within 15 minutes.

Input: entries by hand; windows of hand-built quotes (``Followed``, a ``QuoteLog``); a morning of
hand-built book updates replayed through the engine. Output: the target, stop and nets by hand; each
window's class; the engine's answers and predictions of the kind; its ranking against its climate;
the trees trained on its answers.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from krx_quant.algorithm_days import climate_of
from krx_quant.algorithms import KIND_ALGORITHMS
from krx_quant.algorithms.breiman_scalp import BreimanScalp
from krx_quant.algorithms.contract import KindSample
from krx_quant.barriers import CLASSES as BARRIER_CLASSES
from krx_quant.features import BARRIER_SIGMA, BARRIER_WIDTH, FeatureVector
from krx_quant.kind_registry import KINDS
from krx_quant.kinds import Answer, Followed, KindScored, Quote, QuoteLog
from krx_quant.predictions import PredictionStore
from krx_quant.ranking import ranked_kind, score_day
from krx_quant.scalp import (
    AMBIGUOUS,
    CLIMATE,
    NO_ENTRY,
    PERSIST,
    SCALP,
    STOP_FIRST,
    TARGET_FIRST,
    ScalpClimate,
    net_return,
    scalp_answer,
    scalp_net,
    scalp_stats,
    scalp_ticks,
    stop_price,
    target_price,
)
from krx_quant.touch import SILENT, TARGET_VERSION
from tests.replay_events import SYMBOL, at, run, steady_book, trade

T0 = at("09:10:00")


def test_the_target_nets_two_tenths_after_costs_rounded_up_to_the_tick() -> None:
    """Input: entries at 70,000 (tick 100), 10,000 (tick 10), 49,950 (tick 50, its target in the
    band above). Output by hand: e × 1.00215 ÷ 0.99785 is 70,301.6 → 70,400 (+0.340 % net; 70,300
    nets +0.198 %, short of it), 10,043.1 → 10,050, 50,165.3 → 50,200 (the 100-won band's tick). The
    stop is 0.3 % below, not rounded: 69,790, −0.529 % net."""
    assert target_price(70_000) == 70_400
    assert net_return(70_000, 70_400) == pytest.approx(0.003402, abs=1e-6)
    assert net_return(70_000, 70_300) == pytest.approx(0.0019765, abs=1e-6)
    assert net_return(70_000, 70_300) < 0.002
    assert target_price(10_000) == 10_050
    assert target_price(49_950) == 50_200
    assert stop_price(70_000) == pytest.approx(69_790)
    assert net_return(70_000, 69_790) == pytest.approx(-0.0052936, abs=1e-6)
    for entry in (1_234, 4_985, 19_990, 81_300, 199_900, 512_000):
        target = target_price(entry)
        assert net_return(entry, target) >= 0.002, entry


def window(quotes: list[tuple[str, float, float]], *, ask_start: float | None = 81_300) -> Followed:
    """A window from 09:10 of ``quotes`` (received, bid, ask), the entry ``ask_start``."""
    made = tuple(Quote(at(t), bid, ask, None, None) for t, bid, ask in quotes)
    return Followed(
        SYMBOL,
        T0,
        81_250.0,
        100.0,
        2,
        81_200.0 if ask_start else None,
        ask_start,
        made,
        (),
        None,
        horizon=SCALP.horizon,
    )


def test_the_answer_is_the_level_a_bid_reaches_first() -> None:
    """Input: the entry at 81,300 (T = 81,700: 81,650.4 rounded up; S = 81,056.1). Output: a bid at
    81,700 first is target first (sold at T), a bid at 81,000 first stop first (sold at that bid),
    neither by the deadline timeout (sold at the last bid); an ask at T is not a sale; both at the
    same received time no class; no ask before the bar close no class."""
    assert target_price(81_300) == 81_700
    assert stop_price(81_300) == pytest.approx(81_056.1)
    up = scalp_answer(
        window([("09:10:05", 81_200, 81_300), ("09:12:00", 81_700, 81_800), ("09:13:00", 81_000, 81_100)])
    )
    assert up.label == TARGET_FIRST and up.path["target_at"] == at("09:12:00").isoformat()
    assert scalp_net(TARGET_FIRST, up.path) == pytest.approx(net_return(81_300, 81_700))
    down = scalp_answer(
        window([("09:10:05", 81_200, 81_300), ("09:11:00", 81_000, 81_100), ("09:12:00", 81_700, 81_800)])
    )
    assert down.label == STOP_FIRST and down.path["stop_bid"] == 81_000
    assert scalp_net(STOP_FIRST, down.path) == pytest.approx(net_return(81_300, 81_000))
    flat = scalp_answer(window([("09:10:05", 81_200, 81_700), ("09:20:00", 81_100, 81_200)]))
    assert flat.label == "timeout" and scalp_net("timeout", flat.path) == pytest.approx(
        net_return(81_300, 81_100)
    )
    same = scalp_answer(window([("09:11:00", 81_700, 81_800), ("09:11:00", 81_000, 81_100)]))
    assert (same.label, same.unscorable) == (None, AMBIGUOUS)
    none = scalp_answer(window([("09:11:00", 81_700, 81_800)], ask_start=None))
    assert (none.label, none.unscorable) == (None, NO_ENTRY)
    assert scalp_ticks(TARGET_FIRST, up.path) == pytest.approx(net_return(81_300, 81_700) * 81_300 / 100)


def test_the_answer_reads_nothing_after_the_deadline_nor_before_the_bar_close() -> None:
    """Input: a log with the bid at 81,700 at 09:09:59 (before t) and at 09:25:00 (t + 15 minutes),
    the book at 81,200 / 81,300 between. Output: timeout: neither quote is in the window; a silent
    stretch makes it unscorable, whatever the quotes said."""
    log = QuoteLog()
    log.quote(at("09:09:59"), 81_700, 81_800, None, None, rest=False)
    for s in range(0, 900, 30):
        log.quote(T0 + timedelta(seconds=s), 81_200, 81_300, None, None, rest=False)
    log.quote(at("09:25:00"), 81_700, 81_800, None, None, rest=False)
    followed = log.window(SYMBOL, T0, 81_250.0, 100.0, 2, 81_200, 81_300, crossed=True, horizon=SCALP.horizon)
    assert SCALP.answer_to(followed).label == "timeout"
    quiet = QuoteLog()
    quiet.quote(T0, 81_200, 81_300, None, None, rest=False)
    quiet.quote(at("09:12:00"), 81_700, 81_800, None, None, rest=False)
    answered = SCALP.answer_to(
        quiet.window(SYMBOL, T0, 81_250.0, 100.0, 2, 81_200, 81_300, crossed=True, horizon=SCALP.horizon)
    )
    assert (answered.label, answered.unscorable) == (None, SILENT)


def test_the_kind_is_registered_after_the_ladder_and_out_of_the_verdict() -> None:
    """The scalp is the last bar-close kind, 15 minutes long, with its own baselines and one Breiman;
    it is ranked against its climate, and its classes are not the barrier's, so the verdict never
    pools it."""
    assert KINDS[-1] is SCALP
    assert (SCALP.name, SCALP.horizon, SCALP.answer_version, SCALP.width, SCALP.bar_answer) == (
        "scalp",
        timedelta(minutes=15),
        1,
        None,
        None,
    )
    assert SCALP.p_keys() == ("p_target_first", "p_timeout", "p_stop_first")
    assert [b.name for b in SCALP.baselines] == [CLIMATE, PERSIST]
    assert climate_of(SCALP, SCALP.baseline_names()) == CLIMATE
    assert ranked_kind(SCALP) and SCALP.classes != BARRIER_CLASSES
    mine = [a for a in KIND_ALGORITHMS if a.kind == SCALP.name]
    assert [a.name for a in mine] == ["breiman-scalp"] and KIND_ALGORITHMS[-1] is mine[0]
    assert mine[0].card.character == "트리 앙상블 (스캘프 15분)"


def scored(
    name: str, minute: int, p: tuple[float, float, float], outcome: int, path: Mapping[str, Any]
) -> KindScored:
    return KindScored(name, "v", SYMBOL, T0 + timedelta(minutes=minute), p, outcome, {}, path)


def test_the_ranking_scores_the_scalp_against_its_climate_and_nets_its_target_calls() -> None:
    """Input: the climate at a third each; an algorithm calling target first at 0.6 twice (one
    reached T, one stopped) and timeout once. Output: its skill is the climate's log loss less its
    own summed; two calls, one hit; two longs, their net the two trades' in ticks of the entry."""
    up = scalp_answer(window([("09:12:00", 81_700, 81_800)])).path
    down = scalp_answer(window([("09:11:00", 81_000, 81_100)])).path
    third = (1 / 3, 1 / 3, 1 / 3)
    records = {
        CLIMATE: [scored(CLIMATE, m, third, o, {}) for m, o in ((0, 0), (1, 2), (2, 1))],
        "breiman-scalp": [
            scored("breiman-scalp", 0, (0.6, 0.3, 0.1), 0, up),
            scored("breiman-scalp", 1, (0.6, 0.3, 0.1), 2, down),
            scored("breiman-scalp", 2, (0.2, 0.7, 0.1), 1, {}),
        ],
    }
    day = score_day(date(2026, 10, 12), records, CLIMATE, SCALP)["breiman-scalp"]

    assert day.skill == pytest.approx(3 * math.log(3) + math.log(0.6) + math.log(0.1) + math.log(0.7))
    assert (day.n, day.calls, day.hits, day.longs) == (3, 2, 1, 2)
    assert day.net == pytest.approx((net_return(81_300, 81_700) + net_return(81_300, 81_000)) * 813)
    stats = scalp_stats("breiman-scalp", records["breiman-scalp"], {})
    assert (stats["trades"], stats["won"]) == (2, 0.5)


def test_breiman_learns_the_scalp_s_classes_in_their_order() -> None:
    """Input: 200 samples, a wide barrier target first, a narrow one stop first. Output: the trees'
    probabilities are the scalp's order, target first likelier on a wide barrier."""
    algorithm = BreimanScalp()
    names = algorithm.features

    def sample(i: int) -> KindSample:
        wide = i % 2 == 0
        known: dict[str, float | None] = dict.fromkeys(names)
        values = FeatureVector(known | {BARRIER_WIDTH: 8.0 if wide else 2.0, BARRIER_SIGMA: 1.0}, names)
        label = TARGET_FIRST if wide else STOP_FIRST
        return KindSample(
            SYMBOL, T0 + timedelta(minutes=i), values, Answer(label), SCALP.answer_version, "mid"
        )

    model = algorithm.fit([sample(i) for i in range(200)])
    assert set(model.params) == {"rounds", "share.target_first", "share.timeout", "share.stop_first"}
    wide = algorithm.predict(sample(0).features, model).probabilities
    narrow = algorithm.predict(sample(1).features, model).probabilities
    assert wide[0] > wide[2] and narrow[2] > narrow[0]


def scalp_morning() -> list[Any]:
    """The book at 81,200 / 81,300 every 10 s from 09:00:30 (entry 81,300: T 81,700, S 81,056.1),
    trades making one-minute bars, the bid at 81,700 from 09:20:00 to 09:20:20 and at 81,000 from
    09:40:00 to 09:40:20."""
    trades = [trade(at(f"09:{m:02d}:30"), 81_200) for m in range(0, 59, 2)]
    return (
        trades
        + steady_book(at("09:00:30"), at("09:19:50"), 81_200, 81_300)
        + steady_book(at("09:20:00"), at("09:20:20"), 81_700, 81_800)
        + steady_book(at("09:20:30"), at("09:39:50"), 81_200, 81_300)
        + steady_book(at("09:40:00"), at("09:40:20"), 81_000, 81_100)
        + steady_book(at("09:40:30"), at("09:58:00"), 81_200, 81_300)
    )


def answers(db: Path) -> dict[str, dict[str, Any]]:
    store = PredictionStore(db)
    try:
        return {a["bar_close"][11:16]: a for a in store.kind_answers(SCALP.name, SYMBOL)}
    finally:
        store.close()


async def test_the_engine_answers_and_predicts_the_scalp_from_the_recording(tmp_path: Path) -> None:
    """Input: the morning above to 09:58. Output by hand: the windows of 09:06 to 09:20 reach T at
    09:20:00 (target first), 09:05's ends before it (timeout); 09:26 to 09:40 reach S at 09:40:00
    (stop first), 09:21 to 09:25 end before it (timeout). Each answer's entry is 81,300, the ask
    before its bar close. Both baselines predict every bar close; the persistence carries the window
    that ended at t (09:21's from 09:06's target first)."""
    db = tmp_path / "engine.db"
    engine = await run(db, scalp_morning(), until=at("09:58:00"), target_version=TARGET_VERSION)
    found = answers(db)
    assert found["09:05"]["class"] == "timeout"
    assert {found[f"09:{m:02d}"]["class"] for m in range(6, 21)} == {TARGET_FIRST}
    assert {found[f"09:{m:02d}"]["class"] for m in range(21, 26)} == {"timeout"}
    assert {found[f"09:{m:02d}"]["class"] for m in range(26, 41)} == {STOP_FIRST}
    assert found["09:30"]["path"]["entry"] == 81_300 and found["09:30"]["path"]["target"] == 81_700
    assert found["09:12"]["path"]["target_at"] == at("09:20:00").isoformat()
    assert max(found) == "09:43"  # 09:43's window ends at 09:58

    store = PredictionStore(db)
    try:
        made = {
            (r["bar_close"][11:16], r["algorithm"]): r
            for r in store.kind_recent(SCALP.name, SCALP.answer_version, SYMBOL, 1000)
        }
    finally:
        store.close()
    assert made[("09:21", PERSIST)]["output"] == {
        "p_target_first": 1.0,
        "p_timeout": 0.0,
        "p_stop_first": 0.0,
    }
    assert made[("09:12", PERSIST)]["status"] == "no_previous"
    assert made[("09:12", CLIMATE)]["model_version"] == ScalpClimate().version
    engine.store.close()
