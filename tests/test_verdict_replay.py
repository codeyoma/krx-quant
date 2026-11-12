"""``krx-quant verdict-replay`` (#679): the verdict (#676) made again at each recorded bar close, with the
barrier algorithms the live engine ran and with every one walked forward, and its BUYs replayed a share
each by the paper trader's rules.

Input: a day's bar closes and quotes made by hand (the 4-minute barrier: reference 70,050 won, tick 100,
width 4, the target 70,450, the stop 69,650), and recorded mornings of swings (``prevalidation_doubles``)
with barrier stand-ins and an engine database as the engine writes it. Output: the verdicts counted, each
BUY's trade or why it has none, one at a time with the verdict's exit, the walk-forward's days, the
report's lines and the scheduled job. Every price is made up.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from krx_quant import cli
from krx_quant.algorithms import KindSample, Model
from krx_quant.config import Settings
from krx_quant.entry_delay import Quotes
from krx_quant.features import FeatureVector
from krx_quant.feeds import BOOK
from krx_quant.feeds.base import KST
from krx_quant.kind_registry import kind_named
from krx_quant.kinds import Kind, KindForecast
from krx_quant.lifecycle import SHADOW
from krx_quant.market_calendar import MarketCalendar
from krx_quant.predictions import DB_NAME, NewKindPrediction, PredictionStore
from krx_quant.scheduled import FAILED, JOBS, OK, JobRecord, Night
from krx_quant.scheduled.research import VERDICT_REPLAY, VERDICT_REPLAY_ISSUE
from krx_quant.trader import SYMBOL
from krx_quant.trader.book import STOP_LOSS, TAKE_PROFIT, TIME
from krx_quant.trader.book import Rules as BookRules
from krx_quant.trader.replay import Fills, Recording, micros
from krx_quant.verdict import RULES, RULES_V1
from krx_quant.verdict_replay import (
    BEFORE_ENTRY,
    FILLED,
    HOLDING,
    NO_DAYS,
    VERDICT_EXIT,
    Bought,
    Close,
    Day,
    Replayed,
    drawdown,
    stored_day,
    verdict_replay,
)
from tests.algorithm_doubles import stand_in_card, stand_in_meanings
from tests.prevalidation_doubles import AFTER, DAYS, swings
from tests.prevalidation_doubles import settings as recorded_settings
from tests.replay_doubles import record
from tests.replay_events import book

TODAY = date(2026, 10, 14)  # a Wednesday: 09:00 to 15:30
CALENDAR = MarketCalendar()
FEE = 0.00015


def _kind(name: str) -> Kind:
    found = kind_named(name)
    assert found is not None
    return found


BARRIER = _kind("barrier")
FIFTEEN = _kind("barrier_15m")
UP = (0.9, 0.08, 0.02)
DOWN = (0.02, 0.08, 0.9)


def at(hms: str, day: date = TODAY) -> datetime:
    return datetime.fromisoformat(f"{day.isoformat()}T{hms}+09:00")


def recording(*quotes: tuple[str, float, float]) -> Recording:
    """The trader's stream of the day: (received, bid, ask)."""
    times = np.array([micros(at(t)) for t, _, _ in quotes], dtype=np.int64)
    bids = np.array([b for _, b, _ in quotes], dtype=np.float64)
    asks = np.array([a for _, _, a in quotes], dtype=np.float64)
    return Recording(TODAY, Quotes(times, bids, asks))


def close(p: tuple[float, float, float] | None = UP) -> Close:
    c = Close(70_050.0, 100.0, {BARRIER.name: 4, FIFTEEN.name: 4})
    if p is not None:
        c.forecasts = {BARRIER.name: {"up-4": p}}
    return c


def day_of(
    closes: Mapping[datetime, Close],
    day: Recording | None,
    decided: Mapping[datetime, datetime] | None = None,
    ranked: Mapping[str, frozenset[str]] | None = None,
) -> Day:
    """The day replayed by version 1's rules (every forecast pooled), or with ``ranked`` version 2's
    (#697: each kind's ranking members alone)."""
    session = CALENDAR.session(TODAY)
    assert session is not None
    book = BookRules(fee_rate=FEE)
    fills = Fills(day, [], book)
    rules = RULES_V1 if ranked is None else RULES
    return Day(TODAY, session, closes, [BARRIER, FIFTEEN], fills, decided or {}, FEE, book, rules, ranked)


def all_members(day: Day) -> Replayed:
    into = Replayed()
    day.replay(lambda b, c: c.forecasts, into)
    return into


def test_version_2_replays_only_the_days_ranking_members() -> None:
    """Input (#697): the same lean up at 10:00, replayed by version 2 with the 4 minutes' ranking making
    ``up-4`` a member, then making none. Output: BUY with it a member; WAIT ``no_members`` without."""
    recorded_quotes = (
        ("09:59:58", 70_000, 70_100),
        ("10:00:01", 70_000, 70_100),
        ("10:02:00", 70_500, 70_600),
    )

    member = all_members(
        day_of(
            {at("10:00:00"): close()}, recording(*recorded_quotes), ranked={"barrier": frozenset({"up-4"})}
        )
    )
    none = all_members(day_of({at("10:00:00"): close()}, recording(*recorded_quotes), ranked={}))

    assert member.verdicts[TODAY] == {"buy": 1}
    assert none.verdicts[TODAY] == {"wait_no_members": 1} and not none.alone


def test_a_buy_is_bought_at_the_first_ask_after_its_decision_and_sold_at_its_target() -> None:
    """Input: the members lean up at 10:00 (4 minutes, width 4), the quote 70,000 / 70,100 standing, a bid
    of 70,500 at 10:02. Output: BUY on the 4 minutes, bought at 70,100 at 10:00:01, sold at the target
    70,450 when the bid reaches it: the trader's round trip, in won and in ticks."""
    day = day_of(
        {at("10:00:00"): close()},
        recording(("09:59:58", 70_000, 70_100), ("10:00:01", 70_000, 70_100), ("10:02:00", 70_500, 70_600)),
    )

    into = all_members(day)

    assert into.verdicts[TODAY] == {"buy": 1}
    assert into.buys[TODAY] == {"barrier": 1}
    [bought] = into.alone
    net = 70_450 - 70_100 - FEE * (70_450 + 70_100) - 0.002 * 70_450
    assert (bought.taken, bought.exit, bought.kind) == (FILLED, TAKE_PROFIT, "barrier")
    assert bought.net == pytest.approx(net) and bought.ticks == pytest.approx(net / 100)
    assert into.held == into.alone


def test_without_members_or_a_quote_the_verdict_waits_and_nothing_is_bought() -> None:
    """Input: a bar close with no member's forecast; another with members but no recording. Output: WAIT
    for want of members; the second has no quote, so no expected net and no BUY."""
    day = day_of({at("10:00:00"): close(None)}, recording(("09:59:58", 70_000, 70_100)))
    assert all_members(day).verdicts[TODAY] == {"wait_no_members": 1}

    blind = all_members(day_of({at("10:00:00"): close()}, None))
    assert blind.verdicts[TODAY] == {"wait_no_edge": 1} and blind.alone == []


def test_a_buy_outside_the_trader_s_entry_window_or_with_no_quote_after_is_counted_not_bought() -> None:
    """Input: BUYs at 09:02 (before 09:05), at 10:00 with no quote within 5 s after the decision, at 15:20
    (its deadline past 15:15). Output: each counted by why, none filled."""
    day = day_of(
        {at("09:02:00"): close(), at("10:00:00"): close(), at("15:20:00"): close()},
        recording(("09:01:58", 70_000, 70_100), ("09:59:58", 70_000, 70_100), ("15:19:58", 70_000, 70_100)),
    )

    into = all_members(day)

    assert [b.taken for b in into.alone] == [BEFORE_ENTRY, "no_quote", "past_last_exit"]
    assert into.verdicts[TODAY] == {"buy": 3}


def test_the_decision_waits_for_the_live_engine_s_last_display_of_the_bar_close() -> None:
    """Input: the engine displayed the bar close's last prediction at 10:00:06; a quote at 10:00:01 and
    one at 10:00:07, the same at 10:03:58. Output: the BUY is bought on the 10:00:07 quote (the first after
    the decision), not on the 10:00:01 one, and sold at the 10:03:58 bid at its deadline."""
    quotes = recording(
        ("09:59:58", 70_000, 70_100),
        ("10:00:01", 70_000, 70_100),
        ("10:00:07", 70_100, 70_200),
        ("10:03:58", 70_100, 70_200),
    )
    day = day_of({at("10:00:00"): close()}, quotes, {at("10:00:00"): at("10:00:06")})

    [bought] = all_members(day).alone

    assert bought.taken == FILLED and bought.exit == TIME, "no barrier reached; sold at its deadline"
    assert bought.net == pytest.approx(70_100 - 70_200 - FEE * (70_200 + 70_100) - 0.002 * 70_100)


def test_one_at_a_time_a_buy_waits_for_the_share_held_and_a_sell_exit_sells_it() -> None:
    """Input: BUY at 10:00 (bought 70,100); at 10:01 the members lean down: with the share held the
    verdict is SELL (exit), sold at the next quote's bid 69,900; at 10:02 BUY again while nothing is held;
    at 10:03 BUY while that one is held. Output: each alone, three shares (the first sold at its stop
    later); one at a time, the first sold on the verdict, the third not taken."""
    day = day_of(
        {
            at("10:00:00"): close(),
            at("10:01:00"): close(DOWN),
            at("10:02:00"): close(),
            at("10:03:00"): close(),
        },
        recording(
            ("09:59:58", 70_000, 70_100),
            ("10:00:01", 70_000, 70_100),
            ("10:01:01", 69_900, 70_000),
            ("10:02:01", 70_000, 70_100),
            ("10:03:01", 70_000, 70_100),
            ("10:03:30", 69_500, 69_600),  # every share's stop
        ),
    )

    into = all_members(day)

    assert into.verdicts[TODAY] == {"buy": 3, "sell_avoid": 1}, "counted with nothing held"
    assert [(b.taken, b.exit) for b in into.alone] == [(FILLED, STOP_LOSS)] * 3
    first, second, third = into.held
    assert (first.taken, first.exit) == (FILLED, VERDICT_EXIT)
    assert first.net == pytest.approx(69_900 - 70_100 - FEE * (69_900 + 70_100) - 0.002 * 69_900)
    assert (second.taken, second.exit, third.taken) == (FILLED, STOP_LOSS, HOLDING)


def test_the_drawdown_is_the_largest_fall_of_the_net_cumulated_in_the_order_sold() -> None:
    def sold(minute: int, net: float) -> Bought:
        return Bought(TODAY, "barrier", at("10:00:00"), FILLED, TIME, net, 0.0, at(f"10:{minute:02d}:00"))

    assert drawdown([sold(3, -50), sold(1, 100), sold(2, -30), sold(4, 80)]) == pytest.approx(80)
    assert drawdown([sold(1, 10)]) == 0


# The walk-forward on recorded days.


class Leaning:
    """A barrier kind's stand-in: predicts ``forecast`` (up first, timeout, down first) every time, once
    trained; the days each fit trained on."""

    training_days = 20
    features: tuple[str, ...] = ("velocity",)

    def __init__(self, name: str, kind: Kind, forecast: tuple[float, float, float]) -> None:
        self.name = name
        self.kind = kind.name
        self.card = stand_in_card(name, "a fixed barrier forecast", ("velocity",), "nothing")
        self.param_meanings = stand_in_meanings()
        self._forecast = KindForecast(forecast)
        self.fitted: list[list[date]] = []

    def default_model(self) -> Model:
        return Model(f"{self.name}-default", {})

    def fit(self, samples: Sequence[KindSample]) -> Model:
        self.fitted.append(sorted({s.bar_close.astimezone(KST).date() for s in samples}))
        labelled = [s for s in samples if s.label is not None]
        return Model.fitted(self.name, {}, labelled, 0.0)

    def predict(self, features: FeatureVector, model: Model) -> KindForecast:
        return self._forecast


def stored_live(state: Path, day: date, name: str, p: tuple[float, float, float]) -> None:
    """``name``'s 4-minute barrier forecast of every bar close from 09:10 to 10:20 of ``day``, as the live
    engine stores it, displayed 2 s after its bar close."""
    store = PredictionStore(state / DB_NAME)
    try:
        output = dict(zip(("p_up_first", "p_timeout", "p_down_first"), p, strict=True))
        rows = []
        for minute in range(10, 81):
            bar_close = at("09:00:00", day) + timedelta(minutes=minute)
            rows.append(
                NewKindPrediction(
                    symbol=SYMBOL,
                    algorithm=name,
                    model_version="v1",
                    bar_close=bar_close,
                    displayed_at=bar_close + timedelta(seconds=2),
                    output=output,
                    status="ok",
                    flat_ticks=2,
                    width_ticks=4,
                    anchor=81_250.0,
                    tick=100,
                )
            )
        store.add_kind_predictions("barrier", rows)
    finally:
        store.close()


def recorded(tmp_path: Path, days: Sequence[date]) -> Settings:
    """``days`` recorded as the pre-validation's swings, with the book quoted again 3 s after each of its
    quotes: a quote received within 5 s after each bar close, as the trader needs one."""
    s = recorded_settings(tmp_path, [])
    for day in days:
        events = swings(day)
        again = [
            book(row["received_at"].astimezone(KST) + timedelta(seconds=3), row["bid_px_1"], row["ask_px_1"])
            for feed, row in events
            if feed == BOOK.tr_id
        ]
        record(s.data_dir, sorted([*events, *again], key=lambda e: e[1]["received_at"]), day=day)
    return s


# Four recorded mornings made into samples, twice: a few seconds on a laptop, more on a loaded CI runner.
@pytest.mark.timeout(240)
def test_the_recorded_days_are_replayed_both_ways_each_model_trained_before_its_day(tmp_path: Path) -> None:
    """Input: four recorded mornings of swings; stand-ins of the 4- and 15-minute barriers leaning up
    hard; the live engine stored the 4-minute one's forecasts on the third day only. Output: each day's
    model fitted on the days before it only (the first predicted by none); every way of the members
    replays the four days; live, the verdict waits for members but on the third day; walked forward, it
    buys and fills from the second day; the report holds no price."""
    days = DAYS[:4]
    s = recorded(tmp_path, days)
    four = Leaning("up-4", BARRIER, UP)
    fifteen = Leaning("up-15", FIFTEEN, UP)
    stored_live(s.state_dir, days[2], "up-4", UP)

    report, code = verdict_replay(
        s, AFTER, algorithms=[], kind_algorithms=[four, fifteen], kinds=[BARRIER, FIFTEEN], version=1
    )

    assert code == 0
    for stand_in in (four, fifteen):
        assert stand_in.fitted == [days[:i] for i in range(1, len(days))], (
            "the days before each, never its own"
        )
    assert "up-4 학습 3/4일" in report and "up-15 학습 3/4일" in report
    assert f"재생한 날: {days[0]} – {days[-1]}, 4거래일" in report
    live = report.split("1. 라이브 그대로")[1].split("2. 장벽 알고리즘 모두")[0]
    walked_forward = report.split("2. 장벽 알고리즘 모두")[1]
    assert f"{days[0]}(" in live and f"{days[3]}(" in live
    live_days = [line for line in live.splitlines() if line.strip().startswith("2026-")]
    assert [("매수 0" not in line) for line in live_days] == [False, False, True, False], "live on the third"
    first, *later = [line for line in walked_forward.splitlines() if line.strip().startswith("2026-")]
    assert "매수 0" in first, "nothing trained before the first day: no member"
    assert all("매수 0" not in line for line in later)
    assert "체결" in walked_forward and "청산:" in walked_forward
    assert "81,2" not in report and "81_2" not in report and "8125" not in report, "no price"


def test_without_a_recorded_day_it_says_so_and_exits_3(tmp_path: Path) -> None:
    s = recorded_settings(tmp_path, [])
    report, code = verdict_replay(s, AFTER, kind_algorithms=[], kinds=[BARRIER])
    assert code == NO_DAYS and "기록된 거래일이 없다" in report


def test_the_command_prints_the_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("KRX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KRX_STATE_DIR", str(tmp_path / "state"))

    assert cli.main(["verdict-replay", "--day", "2026-10-14", "--days", "3"]) == NO_DAYS
    assert "판정 재생 (#679)" in capsys.readouterr().out


# The scheduled job.


def night(record: JobRecord | None, commit: str = "c2") -> Night[None]:
    return Night(date(2026, 11, 11), True, commit, record, {}, None)


def test_the_job_runs_once_the_first_night_and_again_after_a_failure_on_other_code() -> None:
    assert VERDICT_REPLAY in JOBS
    assert VERDICT_REPLAY.command == ("verdict-replay",)
    assert [t.issue for t in VERDICT_REPLAY.targets] == [VERDICT_REPLAY_ISSUE] == [679]
    assert VERDICT_REPLAY.timeout <= timedelta(hours=1)
    assert VERDICT_REPLAY.due(night(None))
    ran = JobRecord(date(2026, 11, 10), OK, at("23:00:00"), "c1", 0)
    assert not VERDICT_REPLAY.due(night(ran))
    failed = JobRecord(date(2026, 11, 10), FAILED, at("23:00:00"), "c1", 1)
    assert not VERDICT_REPLAY.due(night(failed, "c1")) and VERDICT_REPLAY.due(night(failed, "c2"))


def test_the_rules_are_the_verdict_s_version() -> None:
    assert RULES.version == 2 and RULES.ranked, "#697: the ranking's members"
    assert RULES_V1.version == 1 and not RULES_V1.ranked


def _stored(lifecycle: str | None, at: datetime, shown: datetime) -> NewKindPrediction:
    output = {"p_up_first": 0.5, "p_timeout": 0.3, "p_down_first": 0.2}
    return NewKindPrediction(SYMBOL, "breiman", "breiman-1", at, shown, output, lifecycle=lifecycle)


def test_the_live_members_leave_out_shadow_rows_and_a_database_from_before_them_is_read(
    tmp_path: Path,
) -> None:
    """#696: a shadow row is neither a live member nor the bar close's last display. A database copied
    before the ``lifecycle`` columns, read as it is (``read_only``), has no shadow row."""
    bar_close = datetime(2026, 10, 14, 9, 30, tzinfo=KST)
    shown, later = bar_close + timedelta(seconds=1), bar_close + timedelta(seconds=3)
    db = tmp_path / "engine.sqlite3"
    store = PredictionStore(db)
    store.add_kind_predictions(
        BARRIER.name,
        [_stored("active", bar_close, shown), replace(_stored(SHADOW, bar_close, later), algorithm="other")],
    )
    store.close()

    with sqlite3.connect(db) as conn:
        found = stored_day(conn, TODAY, [BARRIER], ["breiman", "other"])
    assert list(found.forecasts[bar_close][BARRIER.name]) == ["breiman"]
    assert found.displayed[bar_close] == shown

    with sqlite3.connect(db) as conn:  # as an engine from before #696 left it
        for table in ("predictions", "kind_predictions"):
            conn.execute(f"ALTER TABLE {table} DROP COLUMN lifecycle")
        found = stored_day(conn, TODAY, [BARRIER], ["breiman", "other"])
    assert sorted(found.forecasts[bar_close][BARRIER.name]) == ["breiman", "other"]
    assert found.displayed[bar_close] == later
