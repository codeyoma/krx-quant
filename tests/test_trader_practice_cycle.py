"""The mock practice cell frozen for each review cycle (#474, spec #468 6 "연습"): the nightly job
chooses it once a cycle, the cell and θ whose net per signal replayed alone (#470) has the greatest
day-level lower bound; the mock book trades it for the cycle whatever the cost map says, after a
restart too; a predictor gone rests it for the rest of the cycle. That the practice counts for the KIS
checks and for no record, promotion or live gate is ``tests/test_trader_practice.py``'s
(``test_the_kis_checks_count_the_practice_and_the_records_and_the_gate_do_not``), on a frozen cell.

The first review cycle runs 10-14 to 11-10 (the calendar guessing the weekdays trade), the second
from 11-11. Every figure is made up.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from pathlib import Path

import pytest

from krx_quant import kis
from krx_quant.config import Settings
from krx_quant.consensus import FINAL
from krx_quant.fake_kis import FakeAccount, FakeKis
from krx_quant.kis_orders import Servers, trader_broker
from krx_quant.predictions import DB_NAME as ENGINE_DB
from krx_quant.predictions import PredictionStore
from krx_quant.stats import day_mean
from krx_quant.touch import TARGET_VERSION
from krx_quant.trader import MOCK, practice, report
from krx_quant.trader.book import Rules
from krx_quant.trader.cells import Cell
from krx_quant.trader.desk import Desk
from krx_quant.trader.practice import Measured, freeze_now, frozen, measure, ranked
from krx_quant.trader.quotes import BestQuote
from krx_quant.trader.selection import GONE, Pick, Selection, choose
from krx_quant.trader.settings import TraderSettings
from krx_quant.trader.store import DB_NAME, TraderStore, read_trader
from tests.api_schema import check
from tests.kis_doubles import kis_mock, settings_of
from tests.test_trader_replay import ONE_CELL, TODAY, models, replayed
from tests.trader_doubles import (
    BARRIER,
    CALENDAR,
    ENOUGH,
    FEE,
    Barrier,
    Day,
    EngineApi,
    alone,
    at,
    cost_map,
    cost_row,
    keep,
    pick,
    policy_cell,
    practise,
    predictor,
    record_quotes,
    whole_rules,
)

__all__ = ["kis_mock"]  # the fixture, used here

# No cell passes. By the hit rate's lower bound less p* (#427's rule) the barrier's breiman ranks
# first, then the barrier's ar; the direction's consensus is not on this map.
FAILING = cost_map(
    [
        cost_row("barrier", top=predictor("breiman", 0.60, "fail"), baseline=predictor("ar", 0.55, "fail")),
    ]
)
# Another day's map: the 15-minute barrier's cox ranks first by #427's rule, breiman is not named.
OTHER_DAY = cost_map(
    [
        cost_row("barrier", top=predictor("turing", 0.50, "fail"), baseline=predictor("ar", 0.55, "fail")),
        cost_row("barrier_15m", top=predictor("cox", 0.62, "fail")),
    ]
)
# A map that names neither breiman nor ar.
NEITHER = cost_map([cost_row("barrier_15m", top=predictor("cox", 0.62, "fail"))])
COX = Cell("barrier_15m", "cox")
D1, D2, D3 = date(2026, 10, 12), date(2026, 10, 13), date(2026, 10, 14)


def the(selection: Selection, cell: Cell) -> Pick:
    [found] = [p for p in selection.practisable if p.cell == cell]
    return found


# The rule.


def test_the_cell_and_theta_chosen_have_the_greatest_lower_bound_of_the_net_a_signal() -> None:
    """Each a cell × θ replayed alone (#470). Breiman at 0.6 nets the most a signal (+0.5 ticks a day
    on average) but its days swing (+3, −2): its lower bound is far below ar's at 0.5 (−0.2, −0.1:
    −0.15 ± 12.706 × 0.05), below 0 itself: ar is chosen. Breiman at 0.8 is tighter still but bought 19
    times; the consensus bought on one day alone; turing's signals were all given up (net 0, no buy):
    none of these may be chosen. Of two alike, the lower θ, then the kinds' order."""
    map_ = choose(FAILING)
    breiman, ar = the(map_, BARRIER), the(map_, Cell("barrier", "ar"))
    consensus = pick(Cell("direction", FINAL))
    turing = pick(Cell("barrier_15m", "turing"))
    tried = [
        Measured(breiman, 0.6, alone({D1: [3.0] * 10, D2: [-2.0] * 10})),
        Measured(ar, 0.5, alone({D1: [-0.2] * 10, D2: [-0.1] * 10})),
        Measured(breiman, 0.8, alone({D1: [0.1] * 10, D2: [0.1] * 9})),
        Measured(consensus, 0.4, alone({D1: [1.0] * 30})),
        Measured(turing, 0.4, alone({D1: [], D2: []}, given_up=25)),
    ]

    best = ranked(tried)

    assert [(m.pick.predictor, m.threshold) for m in best] == [("ar", 0.5), ("breiman", 0.6)]
    chosen = best[0].interval
    expected = day_mean({D1: [-0.2], D2: [-0.1]})
    assert chosen is not None and expected is not None and chosen.low is not None
    assert chosen.low == pytest.approx(expected.low)
    assert chosen.low == pytest.approx(-0.15 - 12.706 * 0.05, abs=1e-3)
    assert chosen.low < 0, "below 0 too: the practice is no evidence"
    assert [m.eligible() for m in tried] == [True, True, False, False, False]

    same = alone(ENOUGH)
    ties = [
        Measured(ar, 0.6, same),
        Measured(ar, 0.4, same),
        Measured(pick(Cell("barrier_15m", "cox"), role="top"), 0.4, same),
        Measured(breiman, 0.5, same),
    ]
    assert [(m.pick.kind, m.pick.predictor, m.threshold) for m in ranked(ties)] == [
        ("barrier", "breiman", 0.5),  # the barrier's top before its baseline
        ("barrier", "ar", 0.4),
        ("barrier", "ar", 0.6),
        ("barrier_15m", "cox", 0.4),
    ]


def test_each_cell_is_replayed_alone_a_share_at_each_theta(tmp_path: Path) -> None:
    """The night's measurement is #470's replay, one pass for every cell (#480): the barrier's breiman
    on the recorded day, at θ 0.6 and 0.8, as the replay measures it alone. One day: no lower bound,
    none chosen."""
    models(tmp_path)
    Day(tmp_path, picks=[pick(BARRIER)], rules=Rules(fee_rate=FEE)).run(ONE_CELL)
    record_quotes(tmp_path / "data", [e for e in ONE_CELL if isinstance(e, BestQuote)])
    direct, _, _ = replayed(tmp_path, [pick(BARRIER)], thresholds=(0.6, 0.8), alone=(BARRIER,))

    tried, replayer = measure(
        [pick(BARRIER)],
        {"barrier": (TODAY,)},
        tmp_path / ENGINE_DB,
        tmp_path / "data",
        CALENDAR.session,
        rules=Rules(fee_rate=FEE),
        target_version=TARGET_VERSION,
        thresholds=(0.6, 0.8),
    )

    assert [(m.pick.cell, m.threshold) for m in tried] == [(BARRIER, 0.6), (BARRIER, 0.8)]
    assert [m.signals.as_json() for m in tried] == [
        direct.signals[BARRIER][0.6].as_json(),
        direct.signals[BARRIER][0.8].as_json(),
    ]
    assert (tried[0].signals.signals, tried[0].signals.bought) == (8, 7)
    assert replayer is not None and replayer.passes == 1
    assert ranked(tried) == [], "one day has no day-level bound"


# The night.


def settings(state: Path) -> Settings:
    return Settings(kis_app_key="", kis_app_secret="", state_dir=state, data_dir=state / "data", fee_rate=FEE)


def on(url: str) -> TraderSettings:
    return TraderSettings(engine_url=url, mock_practice=True)


async def test_the_nightly_job_keeps_one_cell_a_cycle_and_the_next_cycle_s_on_the_review_night(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """10-14 night: the cycle in force (the first) has no cell: breiman at 0.6 has the greatest
    bound and is kept. 10-15 night, the cost map changed and cox would be measured best: the first
    cycle's cell stays, nothing is replayed. 11-10, the first cycle's review night: the cycle in force
    is the second, without a cell: cox at 0.5 is kept for it. The practice off, nothing is chosen."""
    PredictionStore(tmp_path / ENGINE_DB).close()
    keep(tmp_path, at("2026-10-13 22:31:00"), policy_cell(Cell("barrier_30m", "cox")))  # the champion
    asked: list[list[str]] = []

    def measured(picks: Sequence[Pick], *args: object, **kwargs: object) -> tuple[list[Measured], None]:
        asked.append([p.predictor for p in picks])
        good, poor = alone(ENOUGH), alone({D1: [-1.0] * 10, D2: [-3.0] * 10})
        return [
            Measured(p, 0.5 if p.cell == COX else 0.6, good if p.predictor in ("breiman", "cox") else poor)
            for p in picks
        ], None

    monkeypatch.setattr(practice, "measure", measured)
    api = EngineApi([FAILING, OTHER_DAY])
    async with api.serving() as url:
        await freeze_now(settings(tmp_path), on(url), at("2026-10-14 22:40:00"))
        first = capsys.readouterr().out
        await freeze_now(settings(tmp_path), on(url), at("2026-10-15 22:40:00"))
        second = capsys.readouterr().out
        await freeze_now(settings(tmp_path), on(url), at("2026-11-10 22:40:00"))
        review_night = capsys.readouterr().out
        await freeze_now(settings(tmp_path), TraderSettings(engine_url=url), at("2026-11-11 22:40:00"))
        off = capsys.readouterr().out

    assert asked == [["breiman", "ar"], ["cox", "ar", "turing"]], "the second night replays nothing"
    assert len(api.asked) == 2
    assert (
        "연습 칸 (#474): 주기 1(2026-10-14–2026-11-10)의 연습 칸을 골랐다(주기 동안 바꾸지 않는다): barrier "
        "breiman(top) · θ 0.6 · 1주씩 따로 잰 신호당 순손익 +0.25틱(날 단위 하한 " in first
    )
    assert "잰 칸 × θ 2개 가운데 체결 20건 이상 2개의 하한 1위" in first
    assert second.strip() == (
        "연습 칸 (#474): 주기 1(2026-10-14–2026-11-10)의 연습 칸은 barrier breiman(top) · θ 0.6 그대로다"
        "(2026-10-14 22:40에 고름)"
    )
    assert (
        "주기 2(2026-11-11–2026-12-08)의 연습 칸을 골랐다(주기 동안 바꾸지 않는다): barrier_15m cox(top)"
        " · θ 0.5" in review_night
    )
    assert off.strip() == "연습 칸 (#474): 연습이 꺼져 있어(KRX_TRADER_MOCK_PRACTICE=off) 고르지 않는다"
    store = TraderStore(tmp_path / DB_NAME)
    try:
        cycle_1, cycle_2 = frozen(store.db, 2, 1), frozen(store.db, 2, 2)
    finally:
        store.close()
    assert cycle_1 is not None and (cycle_1.cell, cycle_1.threshold) == (BARRIER, 0.6)
    assert [(m["predictor"], m["eligible"]) for m in cycle_1.measured] == [("breiman", True), ("ar", True)]
    assert cycle_1.pick.quantity == 1 and cycle_1.pick.practice
    assert cycle_2 is not None and (cycle_2.cell, cycle_2.threshold, cycle_2.gone_at) == (COX, 0.5, None)
    assert "70,050" not in first + review_night and "70050" not in first + review_night, "no price"


async def test_a_night_that_cannot_choose_keeps_nothing_and_the_next_one_chooses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The cost map cannot be read on the 10-14 night, and on the 10-15 night no cell bought 20 times:
    nothing is kept (the trader practises nothing meanwhile). The 10-16 night keeps one."""
    PredictionStore(tmp_path / ENGINE_DB).close()
    keep(tmp_path, at("2026-10-13 22:31:00"), policy_cell(Cell("barrier_30m", "cox")))
    few = [True]

    def measured(picks: Sequence[Pick], *args: object, **kwargs: object) -> tuple[list[Measured], None]:
        signals = alone({D1: [1.0] * 5, D2: [1.0] * 5}) if few[0] else alone(ENOUGH)
        few[0] = False
        return [Measured(p, 0.6, signals) for p in picks], None

    monkeypatch.setattr(practice, "measure", measured)
    async with EngineApi([503, FAILING]).serving() as url:
        await freeze_now(settings(tmp_path), on(url), at("2026-10-14 22:40:00"))
        unread = capsys.readouterr().out
        await freeze_now(settings(tmp_path), on(url), at("2026-10-15 22:40:00"))
        too_few = capsys.readouterr().out
        store = TraderStore(tmp_path / DB_NAME)
        try:
            assert frozen(store.db, 2, 1) is None
        finally:
            store.close()
        await freeze_now(settings(tmp_path), on(url), at("2026-10-16 22:40:00"))
        chosen = capsys.readouterr().out

    assert "비용 지도를 읽지 못해 주기 1의 칸을 고르지 않았다(다음 밤에 다시)" in unread
    assert "2칸 × θ 가운데 이틀 이상에서 체결 20건 이상인 것이 없다(가장 많을 때 10건)" in too_few
    assert "주기 1(2026-10-14–2026-11-10)의 연습 칸을 골랐다" in chosen


# The trader.


async def test_the_book_trades_the_cycle_s_cell_whatever_the_cost_map_and_after_a_restart(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path
) -> None:
    """The first cycle's cell is the barrier's ar at θ 0.7, though breiman ranks first on the 10-14
    map by #427's rule. Each pick of the cycle practises ar at 0.7: on a map where cox ranks first, and
    after a restart on one that does not name ar. The second cycle's first pick has no cell kept for
    it: no practice (``not_frozen``) until the nightly job keeps one."""
    fake, _ = kis_mock
    ar = the(choose(FAILING), Cell("barrier", "ar"))
    practise(tmp_path, ar, threshold=0.7)
    store = TraderStore(tmp_path / DB_NAME)
    seen: list[tuple[str, list[Cell], float | None, str | None]] = []
    try:
        async with kis.rest_session() as session:
            broker = trader_broker(
                session,
                settings_of(fake, tmp_path),
                lambda: at("09:00:00"),
                servers=Servers(fake.rest_url, fake.ws_url),
            )

            def started(when: str) -> Desk:
                return Desk(
                    store,
                    whole_rules(),
                    CALENDAR.session,
                    at(when),
                    MOCK,
                    broker=broker,
                    clock=lambda: at(when),
                    practice=True,
                )

            def picked(desk: Desk, selection: Selection, when: str) -> None:
                desk.select(selection, at(when))
                state = desk.champion.practice_state
                assert state is not None
                cell = state["cell"]
                seen.append(
                    (when, desk.champion.cells, None if cell is None else cell["threshold"], state["why"])
                )

            first = started("09:00:00")
            picked(first, choose(FAILING), "09:00:00")
            picked(first, choose(OTHER_DAY), "2026-10-15 08:30:00")
            await first.close()
            again = started("2026-10-16 08:00:00")  # a restart
            picked(again, choose(NEITHER), "2026-10-16 08:30:00")
            picked(again, choose(FAILING), "2026-11-11 08:30:00")  # the second cycle
            await again.close()
    finally:
        store.close()

    ar_cell = Cell("barrier", "ar")
    assert seen == [
        ("09:00:00", [ar_cell], 0.7, None),
        ("2026-10-15 08:30:00", [ar_cell], 0.7, None),
        ("2026-10-16 08:30:00", [ar_cell], 0.7, None),
        ("2026-11-11 08:30:00", [], None, practice.NOT_FROZEN),
    ]


async def test_a_practice_cell_whose_predictor_is_gone_rests_until_the_cycle_ends(
    kis_mock: tuple[FakeKis, FakeAccount], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The first cycle's cell is the barrier's breiman. On 10-13 the engine made barrier predictions
    of barrier-persist alone: the 10-14 pick finds breiman gone, keeps when, and practises nothing
    (``gone``), nor another cell. Breiman predicts again on 10-14, but the 10-15 pick rests still. The
    second cycle's cell, breiman again, is practised from its first pick."""
    fake, _ = kis_mock
    breiman = the(choose(FAILING), BARRIER)
    practise(tmp_path, breiman)
    engine = PredictionStore(tmp_path / ENGINE_DB)
    Barrier("2026-10-13 10:00:00", predictor="barrier-persist").write(engine)
    engine.close()
    store = TraderStore(tmp_path / DB_NAME)
    try:
        async with kis.rest_session() as session:
            broker = trader_broker(
                session,
                settings_of(fake, tmp_path),
                lambda: at("09:00:00"),
                servers=Servers(fake.rest_url, fake.ws_url),
            )
            desk = Desk(
                store,
                whole_rules(),
                CALENDAR.session,
                at("09:00:00"),
                MOCK,
                broker=broker,
                clock=lambda: at("09:00:00"),
                practice=True,
                engine=tmp_path / ENGINE_DB,
            )
            desk.select(choose(FAILING), at("09:00:00"))
            rested = desk.champion.practice_state
            assert rested is not None
            assert (desk.champion.cells, rested["active"], rested["why"]) == ([], False, GONE)
            assert rested["frozen"]["gone_at"] == "2026-10-14T09:00:00+09:00"
            desk.champion.write(at("09:00:00"))
            api = read_trader(tmp_path / DB_NAME, at("09:00:01"))
            check("trader", api)
            assert api["practice"]["why"] == GONE

            engine = PredictionStore(tmp_path / ENGINE_DB)
            Barrier("2026-10-14 10:00:00").write(engine)  # breiman back
            engine.close()
            desk.select(choose(FAILING), at("2026-10-15 08:30:00"))
            still = desk.champion.practice_state
            assert still is not None and desk.champion.cells == [] and still["why"] == GONE

            practise(tmp_path, breiman, when="2026-11-10 22:40:00")  # the second cycle's, by the job
            desk.select(choose(FAILING), at("2026-11-11 08:30:00"))
            back = desk.champion.practice_state
            assert back is not None and desk.champion.cells == [BARRIER] and back["active"]
            assert back["frozen"]["cycle"] == 2
            await desk.close()
    finally:
        store.close()

    assert "the practice cell barrier breiman is gone: its predictor no longer predicts" in caplog.text
    text = report.render(report.read(tmp_path / DB_NAME, date(2026, 10, 15), CALENDAR))
    assert "연습 칸 (#474): 주기 1에 고정 · barrier breiman(top) · θ 0.6(2026-10-13 22:40에 고름)" in text
    assert "2026-10-14 09:00에 예측자가 없어져(gone) 주기 끝까지 쉰다" in text
