"""The cost map's audit (#438, ``krx-quant cost-map-audit``): every ranked algorithm of the
trader's kinds judged as the cost map judges its top.

Input: scored predictions of the direction and the barrier stored through the prediction store
with their answers (``api_doubles``'s), the Engine API's cost map of them as the trader asks
for it (005930, 20d, special situations left out), and the engine's database file it was computed
from. Output: each kind's candidates, the visible one against the cost map's top to the number; a
hidden one that passes where the top fails; the text, exploratory at its head and without prices;
the exit codes; and the command in its own process, as its scheduled job ran it.
"""

from __future__ import annotations

import copy
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from krx_quant.barriers import ANSWER_VERSION as BARRIER_VERSION
from krx_quant.barriers import BARRIER, CLIMATE, DOWN_FIRST, PERSIST, TIMEOUT, UP_FIRST
from krx_quant.consensus import NAMES as CONSENSUS
from krx_quant.cost_map_audit import (
    AUDITED,
    DIFFERS,
    NOT_READ,
    audit,
    report,
    run_audit,
)
from krx_quant.kinds import Answer
from krx_quant.predictions import NewKindPrediction, PredictionStore
from krx_quant.pricing import Direction
from krx_quant.scheduled import run_command
from krx_quant.trader import TRADED_KINDS
from tests.api_doubles import OUTCOMES, barrier_board, baselines, cost_map, get, store_scored
from tests.replay_events import DAY, at
from tests.test_cost_map_api import row
from tests.trader_doubles import EngineApi

UP, FLAT, DOWN = Direction.UP, Direction.FLAT, Direction.DOWN
TRADERS = "?symbol=005930&period=20d&special=exclude"  # the trader's query (``selection.read_cost_map``)


async def traders_cost_map(client: Any) -> Any:
    return await get(client, f"/v1/cost-map{TRADERS}")


def direction_board(store: PredictionStore) -> None:
    """Alpha always right; beta misses the downs; the final consensus calls the rises only."""
    baselines(store)
    store_scored(store, "alpha", OUTCOMES)
    store_scored(store, "beta", [FLAT if o is DOWN else o for o in OUTCOMES])
    store_scored(store, "consensus-final", [FLAT if o is DOWN else o for o in OUTCOMES])


def store_barrier(
    store: PredictionStore, algorithm: str, calls: Sequence[str], outcomes: Sequence[str], width: int = 10
) -> None:
    """``calls`` (one sure call per bar close from 09:01) of the barrier on 005930, answered by
    ``outcomes``, ``width`` ticks from 81,250 won (``api_doubles.store_barrier``'s, any answers)."""
    start = datetime.combine(DAY, at("09:01:00").timetz())
    made = []
    for i, call in enumerate(calls):
        t = start + timedelta(minutes=i)
        output = dict(zip(BARRIER.p_keys(), (float(c == call) for c in BARRIER.classes), strict=True))
        made.append(NewKindPrediction("005930", algorithm, "v1", t, t, output, width_ticks=width))
    store.add_kind_predictions(BARRIER.name, made)
    store.record_models([(algorithm, "v1", BARRIER_VERSION)])
    path = {"w": width, "anchor": 81_250, "tick": 100, "cost": 1.86875, "upper": 82_250, "lower": 80_250}
    path |= {"bid_start": 81_200, "ask_start": 81_300, "bid_end": 81_200, "ask_end": 81_300}
    store.add_kind_answers(
        BARRIER.name,
        BARRIER_VERSION,
        [
            ("005930", start + timedelta(minutes=i), Answer(label, None, path), False)
            for i, label in enumerate(outcomes[: len(calls)])
        ],
    )


# 360 bar closes: a rise first, two timeouts, a fall first, two timeouts, over again.
HIDDEN_OUTCOMES = [UP_FIRST, TIMEOUT, TIMEOUT, DOWN_FIRST, TIMEOUT, TIMEOUT] * 60


def hidden_board(store: PredictionStore) -> None:
    """``loud`` is right on every timeout and calls a rise at every barrier reached: accuracy
    300/360, lift over the climate 60/360, but half its 120 calls wrong (precision 0.5, below p*).
    ``quiet`` is right on every timeout and on the first 25 rises and 25 falls, and calls a timeout
    at the others: accuracy 290/360, lift 50/360 (ranked second), its 50 calls all right."""
    store_barrier(store, CLIMATE, [TIMEOUT] * 360, HIDDEN_OUTCOMES)
    store_barrier(store, PERSIST, [UP_FIRST] * 360, HIDDEN_OUTCOMES)
    loud = [TIMEOUT if o == TIMEOUT else UP_FIRST for o in HIDDEN_OUTCOMES]
    store_barrier(store, "loud", loud, HIDDEN_OUTCOMES)
    quiet, seen = [], {UP_FIRST: 0, DOWN_FIRST: 0}
    for o in HIDDEN_OUTCOMES:
        if o != TIMEOUT:
            seen[o] += 1
        quiet.append(o if o == TIMEOUT or seen[o] <= 25 else TIMEOUT)
    store_barrier(store, "quiet", quiet, HIDDEN_OUTCOMES)


async def test_the_visible_candidate_has_the_cost_map_s_numbers_to_the_last_digit(tmp_path: Path) -> None:
    async with cost_map(tmp_path) as (engine, client):
        direction_board(engine.store)
        barrier_board(engine.store)
        store_barrier(engine.store, "beta", [UP_FIRST] * 40, [UP_FIRST, TIMEOUT, DOWN_FIRST, TIMEOUT] * 10)
        body = await traders_cost_map(client)
        boards = {k: await get(client, f"/v1/leaderboard{TRADERS}&kind={k}") for k in TRADED_KINDS}
        audits = audit(body, tmp_path / "engine.db", target_version=1)

    assert [a.kind for a in audits] == list(TRADED_KINDS)
    for a in audits:
        theirs = row(body, a.kind)
        ranked = [e["algorithm"] for e in boards[a.kind]["ranked"] if e["algorithm"] not in CONSENSUS]
        assert [c.algorithm for c in a.candidates] == ranked, a.kind
        assert a.differs == (), a.kind
        if not ranked:
            assert theirs["top"] is None
            continue
        visible, *hidden = a.candidates
        assert visible.visible and not any(c.visible for c in hidden)
        assert visible.judged == theirs["top"], "the cost map's own row, to the last digit"
        for c in hidden:  # judged by the same rule: the leaderboard's hit rate against the row's p*
            board_entry = next(e for e in boards[a.kind]["ranked"] if e["algorithm"] == c.algorithm)
            assert c.judged["precision"] == board_entry["precision"]
            assert c.judged["rank"] == board_entry["rank"]
    direction, barrier = audits[0], audits[1]
    assert [c.algorithm for c in direction.candidates] == ["alpha", "beta"]
    alpha = direction.candidates[0]
    assert alpha.rise is not None and alpha.fall is not None
    assert (alpha.rise.n, alpha.rise.value, alpha.fall.n, alpha.fall.value) == (10, 1.0, 10, 1.0)
    beta = direction.candidates[1]
    assert beta.fall is None and beta.rise is not None and beta.rise.n == 10, "beta calls no fall"
    assert [c.algorithm for c in barrier.candidates] == ["alpha", "beta"]
    assert barrier.candidates[0].judged["verdict"] == {"limit": "pass", "market": "pass"}
    # Beta's 40 rise calls, 10 right: the rise's hit rate is its precision, as it calls no fall.
    persisting = barrier.candidates[1]
    assert persisting.rise is not None and (persisting.rise.n, persisting.rise.value) == (40, 0.25)
    assert persisting.judged["verdict"] == persisting.rise_verdict == {"limit": "fail", "market": "fail"}


async def test_a_hidden_candidate_that_passes_where_the_top_fails_is_caught(tmp_path: Path) -> None:
    async with cost_map(tmp_path) as (engine, client):
        hidden_board(engine.store)
        body = await traders_cost_map(client)
        audits = audit(body, tmp_path / "engine.db", target_version=1)

    barrier = next(a for a in audits if a.kind == "barrier")
    top = row(body, "barrier")["top"]
    assert (top["algorithm"], top["verdict"]) == ("loud", {"limit": "fail", "market": "fail"})
    visible, hidden = barrier.candidates
    assert (visible.algorithm, visible.visible, visible.judged) == ("loud", True, top)
    assert (hidden.algorithm, hidden.visible, hidden.judged["rank"]) == ("quiet", False, 2)
    assert hidden.judged["calls"] == 50
    assert hidden.judged["verdict"] == {"limit": "pass", "market": "pass"}
    assert hidden.rise is not None and hidden.fall is not None
    assert (hidden.rise.n, hidden.rise.value, hidden.fall.n, hidden.fall.value) == (25, 1.0, 25, 1.0)
    assert hidden.rise_verdict == {"limit": "pass", "market": "pass"}
    # The top's rises are right on the rises (60), wrong on the falls (60); it calls no fall.
    assert visible.rise is not None and (visible.rise.n, visible.rise.value) == (120, 0.5)
    assert visible.fall is None

    text = report(body, audits)
    assert text.splitlines()[0].startswith("비용 지도 감사 (#438)")
    assert "탐색용이다" in text and "매매 허가가 아니다" in text
    assert "보임 1위 loud" in text and "숨음 2위 quiet" in text
    summary = text.split("\n\n")[-1]
    assert summary.startswith("요약")
    assert "barrier 2위 quiet" in summary and "loud" not in summary
    for price in ("81,250", "81250", "82,250", "80,250", "81,300", "81,200"):
        assert price not in text, "no price"


async def test_the_command_reads_the_trader_s_cost_map_and_the_engine_s_database(tmp_path: Path) -> None:
    async with cost_map(tmp_path) as (engine, client):
        hidden_board(engine.store)
        url = str(client.make_url("")).rstrip("/")
        code = await run_audit(tmp_path / "engine.db", url, target_version=1)

    assert code == AUDITED


async def test_a_cost_map_that_cannot_be_read_is_not_audited(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    async with EngineApi([503]).serving() as url:
        code = await run_audit(tmp_path / "engine.db", url, target_version=1)
    assert code == NOT_READ
    assert "비용 지도를 읽지 못해 감사하지 않았다" in capsys.readouterr().out


async def test_without_the_engine_s_database_nothing_is_audited(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    async with cost_map(tmp_path) as (_, client):
        body = await traders_cost_map(client)
    async with EngineApi([body]).serving() as url:
        code = await run_audit(tmp_path / "missing.db", url, target_version=1)
    assert code == NOT_READ
    assert "엔진 DB가 없어" in capsys.readouterr().out


async def test_a_visible_candidate_unlike_the_cost_map_s_top_is_told(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    async with cost_map(tmp_path) as (engine, client):
        hidden_board(engine.store)
        body = await traders_cost_map(client)
    moved = copy.deepcopy(body)
    row(moved, "barrier")["top"]["calls"] += 1  # as if the database moved since the cost map

    audits = audit(moved, tmp_path / "engine.db", target_version=1)
    barrier = next(a for a in audits if a.kind == "barrier")
    assert barrier.differs == ("top",)
    assert "비용 지도와 다르다: top" in report(moved, audits)
    async with EngineApi([moved]).serving() as url:
        code = await run_audit(tmp_path / "engine.db", url, target_version=1)
    assert code == DIFFERS
    assert "비용 지도와 다르다: top" in capsys.readouterr().out


async def test_another_cost_map_than_the_trader_s_is_refused(tmp_path: Path) -> None:
    async with cost_map(tmp_path) as (_, client):
        everyone = await get(client, "/v1/cost-map?period=20d")
    async with EngineApi([everyone]).serving() as url:
        code = await run_audit(tmp_path / "engine.db", url, target_version=1)
    assert code == NOT_READ


# The command in its own process, as its scheduled job ran it (``cost-map-audit-438``, which ran on
# 2026-11-02 and left ``JOBS``, #505).


async def test_the_command_runs_in_its_own_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the engine's database: the command runs, reads the cost map, and says why it stopped."""
    monkeypatch.chdir(tmp_path)  # no .env
    monkeypatch.setenv("KRX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KRX_STATE_DIR", str(tmp_path / "state"))
    async with cost_map(tmp_path) as (_, client):
        body = await traders_cost_map(client)
    async with EngineApi([body]).serving() as url:
        monkeypatch.setenv("KRX_TRADER_ENGINE_URL", url)
        ran = await run_command(["cost-map-audit"], timedelta(seconds=25))
    assert "엔진 DB가 없어" in ran.stdout
    assert ran.exit_code == NOT_READ
