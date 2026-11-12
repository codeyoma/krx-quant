"""Every registered algorithm in ``/v1/algorithms`` (#561), run or not: its state, its pre-validation,
and each kind's algorithms by state.

Input: a morning replayed with a registry of stand-in algorithms, the engine's pre-validation gate
letting two run, a report that refused one (as a pre-validation leaves it), two waiting, and one of the
two run retired by command. Output: the list, each one's detail and statistics.
"""

from __future__ import annotations

from pathlib import Path

from krx_quant.lifecycle import Status
from krx_quant.prevalidation import code_digest, refusals
from krx_quant.touch import TARGET_VERSION
from tests.algorithm_doubles import FixedAlgorithm
from tests.api_schema import check
from tests.prevalidation_doubles import refuse
from tests.replay_doubles import busy_morning
from tests.replay_events import api, at, get_json, run

# Newton was registered before pre-validation (#24): exempt from it.
NEWTON = FixedAlgorithm("newton", features=("velocity",), unique=("velocity",))
BETA = FixedAlgorithm("beta", features=("acceleration",), unique=("acceleration",))
FIRST = FixedAlgorithm("first", features=("vwap_z",), unique=("vwap_z",))
SECOND = FixedAlgorithm("second", features=("vwap_gap",), unique=("vwap_gap",))
TURNED = FixedAlgorithm("turned", features=("relative_volume",), unique=("relative_volume",))


async def test_the_list_holds_every_algorithm_waiting_refused_and_retired_among_them(tmp_path: Path) -> None:
    state = tmp_path / "state"
    refuse(state, TURNED)
    engine = await run(
        tmp_path / "live.db",
        busy_morning(end="09:05:00"),
        until=at("09:05:00"),
        algorithms=[NEWTON, BETA, FIRST, SECOND, TURNED],
        kind_algorithms=(),
        admits=lambda a: a.name in {"newton", "beta"},
        refused=refusals(state, TARGET_VERSION),
        target_version=TARGET_VERSION,
    )
    engine.store.change_status("beta", at("09:06:00"), Status.PROBATION, Status.RETIRED, "retired by command")

    async with api(engine) as client:
        body = await get_json(client, "/v1/algorithms")
        first = await get_json(client, "/v1/algorithms/first")
        stats = await get_json(client, "/v1/algorithms/first/stats")

    check("algorithms", body)
    check("algorithm_detail", first)
    listed = {a["name"]: a for a in body["algorithms"]}
    assert list(listed) == ["newton", "beta", "first", "second", "turned"], "registration order"
    assert {name: a["state"] for name, a in listed.items()} == {
        "newton": "probation",
        "beta": "retired",
        "first": "waiting",
        "second": "waiting",
        "turned": "refused",
    }
    assert [listed[n]["prevalidation"]["queue"] for n in ("first", "second")] == [1, 2], "the night's order"
    assert listed["newton"]["prevalidation"] == {"exempt": True, "queue": None, "refused": None}
    assert listed["first"]["status"] == {"status": None, "since": None, "history": []}, "never run"
    assert listed["turned"]["prevalidation"]["refused"] == {
        "at": "2026-09-25T23:10:00+09:00",
        "code": code_digest(TURNED),
        "target_version": TARGET_VERSION,
        "inputs": {},
        "checks": ["stats"],
        "why": "stats: weakest lift -0.0100",
    }
    direction, *others = body["kinds"]
    assert all(k["predicting"] == k["shadow"] == [] and k["states"] == {} for k in others), (
        "no algorithm of the other kinds"
    )
    assert direction == {
        "kind": "direction",
        "target_version": TARGET_VERSION,
        "predicting": ["newton"],
        "shadow": ["beta", "first", "second", "turned"],  # #696
        "states": {
            "probation": ["newton"],
            "retired": ["beta"],
            "waiting": ["first", "second"],
            "refused": ["turned"],
        },
    }
    assert (first["state"], first["model_history"]) == ("waiting", [])
    assert stats["algorithm"] == "first" and stats["overall"]["n"] == 0, "known, with nothing scored"
