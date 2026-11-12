"""Every registered algorithm predicts live (#696): the ones the engine does not enroll predict in
shadow, their rows marked so, and nothing that decides (the consensus, the verdicts, the reviews, the
CPU budget's order) takes them.

Input: a morning replayed with stand-in algorithms: one exempt from pre-validation and active, one
waiting for its pre-validation, one its report refused, one retired by command; the engine's gate
lets the exempt and the retired one in. Output: the rows of each, the consensus and the verdicts of
each bar close, what a review reads, and the CPU budget's skips.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

from krx_quant.algorithms import Model
from krx_quant.clock import FakeClock
from krx_quant.engine import Budget, PredictionEngine
from krx_quant.lifecycle import SHADOW, Status
from krx_quant.prevalidation import refusals
from krx_quant.replay import replay_into
from krx_quant.touch import TARGET_VERSION
from tests.algorithm_doubles import FixedAlgorithm
from tests.api_schema import check
from tests.prevalidation_doubles import refuse
from tests.replay_doubles import busy_morning
from tests.replay_events import DAY, api, at, get_json, run

# Newton was registered before pre-validation (#24): exempt from it.
NEWTON = FixedAlgorithm("newton", features=("velocity",), unique=("velocity",))
BETA = FixedAlgorithm("beta", features=("acceleration",), unique=("acceleration",))
FIRST = FixedAlgorithm("first", features=("vwap_z",), unique=("vwap_z",))
TURNED = FixedAlgorithm("turned", features=("relative_volume",), unique=("relative_volume",))
REGISTRY = [NEWTON, BETA, FIRST, TURNED]
SHADOWED = {"beta", "first", "turned"}


async def _morning(tmp_path: Path) -> PredictionEngine:
    """The engine through 09:10: Newton active from 09:03 on a model of the touch target, Beta retired
    then; First waiting, Turned refused."""
    state = tmp_path / "state"
    refuse(state, TURNED)
    events = busy_morning(end="09:10:00")
    split = at("09:03:00")
    engine = await run(
        tmp_path / "live.db",
        [e for e in events if e[1]["received_at"] < split],
        until=split,
        algorithms=REGISTRY,
        kind_algorithms=(),
        admits=lambda a: a.name in {"newton", "beta"},
        refused=refusals(state, TARGET_VERSION),
        models={a.name: Model(f"{a.name}-1", {}, target_version=TARGET_VERSION) for a in REGISTRY},
    )
    engine.store.change_status("newton", split, Status.PROBATION, Status.ACTIVE, "made active for the test")
    engine.store.change_status("beta", split, Status.PROBATION, Status.RETIRED, "retired by command")
    assert isinstance(engine.clock, FakeClock)
    await replay_into(
        engine, engine.clock, [e for e in events if e[1]["received_at"] >= split], until=at("09:10:00")
    )
    return engine


def _rows(db: Path, table: str = "predictions") -> list[sqlite3.Row]:
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            f"SELECT * FROM {table} WHERE bar_close >= ? ORDER BY id", (at("09:04:00").isoformat(),)
        ).fetchall()


async def test_every_algorithm_predicts_each_bar_close_its_rows_carrying_where_it_stands(
    tmp_path: Path,
) -> None:
    engine = await _morning(tmp_path)
    rows = _rows(tmp_path / "live.db")

    closes = sorted({r["bar_close"] for r in rows})
    assert len(closes) >= 5
    for name in ("newton", *SHADOWED):
        made = [r for r in rows if r["algorithm"] == name]
        assert sorted(r["bar_close"] for r in made) == closes, f"{name} at every bar close"
        assert {r["status"] for r in made} == {"ok"}
    lifecycle = {r["algorithm"]: r["lifecycle"] for r in rows}
    assert {n: lifecycle[n] for n in ("newton", *SHADOWED)} == {
        "newton": "active",
        "beta": SHADOW,
        "first": SHADOW,
        "turned": SHADOW,
    }
    assert lifecycle["consensus-final"] is None and lifecycle["baseline-persist"] is None
    async with api(engine) as client:
        body = await get_json(client, f"/v1/symbols/{rows[0]['symbol']}/predictions?algorithm=first&limit=1")
    check("predictions", body)
    assert [p["lifecycle"] for p in body["predictions"]] == [SHADOW]


async def test_the_consensus_and_the_verdicts_never_take_one_in_shadow_nor_wait_for_it(
    tmp_path: Path,
) -> None:
    engine = await _morning(tmp_path)
    rows = _rows(tmp_path / "live.db")

    final = [r for r in rows if r["algorithm"] == "consensus-final"]
    assert final and all(r["status"] == "ok" for r in final)
    with sqlite3.connect(tmp_path / "live.db") as conn:
        pooled = {
            tuple(
                a for (a,) in conn.execute("SELECT algorithm FROM pooled WHERE prediction_id = ?", (r["id"],))
            )
            for r in final
        }
    assert pooled == {("newton",)}, "the active one alone"
    for r in final:
        shadow = [s["id"] for s in rows if s["bar_close"] == r["bar_close"] and s["lifecycle"] == SHADOW]
        assert min(shadow) > r["id"], "made once the final consensus is stored"
    verdicts = engine.store.verdicts([engine.symbols[0]])
    assert verdicts
    with sqlite3.connect(tmp_path / "live.db") as conn:
        bodies = [
            json.loads(b)
            for (b,) in conn.execute(
                "SELECT body FROM verdicts WHERE bar_close >= ?", (at("09:04:00").isoformat(),)
            )
        ]
    named = {s["name"] for b in bodies for s in b.get("side", [])}
    assert "newton" in named and not named & SHADOWED


async def test_no_review_reads_a_shadow_row(tmp_path: Path) -> None:
    engine = await _morning(tmp_path)
    day: date = DAY

    since = at("09:04:00").isoformat()  # Beta was enrolled until 09:03
    reviewed = {r["algorithm"] for r in engine.store.reviewed([day], True) if r["bar_close"] >= since}
    scored = {
        r["algorithm"] for r in engine.store.scored_predictions([day], None, True) if r["bar_close"] >= since
    }

    assert "newton" in reviewed and not reviewed & SHADOWED
    assert SHADOWED <= scored, "the statistics and the leaderboard show them"
    assert engine.store.first_on_target("first", at("09:00:00"), TARGET_VERSION) is None


async def test_the_cpu_budget_skips_those_in_shadow_first(tmp_path: Path, fixed_costs: None) -> None:
    """Input: Newton (0.05 s) enrolled, then three in shadow (0.05 s each) over a CPU budget of
    0.12 s. Output: the ones in shadow skipped, last registered first; Newton at every bar close."""
    costly = [FixedAlgorithm(a.name, features=a.features, unique=a.features, seconds=0.05) for a in REGISTRY]
    state = tmp_path / "state"
    refuse(state, costly[3])

    await run(
        tmp_path / "live.db",
        busy_morning(end="09:05:00"),
        until=at("09:05:00"),
        algorithms=costly,
        kind_algorithms=(),
        admits=lambda a: a.name == "newton",
        refused=refusals(state, TARGET_VERSION),
        budget=Budget(cpu=0.12),
    )

    with sqlite3.connect(tmp_path / "live.db") as conn:
        skipped = {a for (a,) in conn.execute("SELECT algorithm FROM runs WHERE kind = 'cpu_budget'")}
        newton = conn.execute(
            "SELECT COUNT(DISTINCT bar_close) FROM predictions WHERE algorithm = 'newton'"
        ).fetchone()
    assert skipped and skipped <= SHADOWED
    assert "turned" in skipped, "the last registered first"
    assert newton[0] >= 3
