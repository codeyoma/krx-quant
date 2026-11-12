"""Every job's due decision on representative nights (#651, ``tests/scheduled_nights.py``), as kept in
``tests/scheduled_due.json``.

The file was written before the jobs read their own facts (``Job.facts``), each condition asked with the
one ``Facts`` every job had, by its fields' names: the test builds each job's own facts from those names
(``OWN``), so the move is shown to change no job's decision, its quiet night's condition's either. A job
the file does not hold (a new one) is not asked, and one it holds that left ``JOBS`` is passed over. A
PR that changes a condition on purpose writes the file again with ``uv run python -m
tests.test_scheduled_due``, and the review sees which decisions moved.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import fields
from pathlib import Path
from typing import Any

from krx_quant.scheduled import JOBS, Job, Night, PrevalidationFacts
from krx_quant.scheduled.prevalidations import read_prevalidation_facts
from krx_quant.scheduled.probes import read_last_restart
from krx_quant.scheduled.research import (
    read_hit_rate_nights,
    read_smt_holdout_days,
)
from krx_quant.scheduled.trader import read_fill_notice_apart, read_live_kis
from krx_quant.scheduled.upkeep import read_last_backup
from tests.scheduled_nights import NOW, Scene, decisions, marks, runs, scenes

KEPT = Path(__file__).with_name("scheduled_due.json")
WRITE = "uv run python -m tests.test_scheduled_due"


def _prevalidation(facts: Mapping[str, Any]) -> PrevalidationFacts:
    return PrevalidationFacts(
        **{f.name: facts[f.name] for f in fields(PrevalidationFacts) if f.name in facts}
    )


# Each reader's facts from the one ``Facts``'s fields of before #651, by name, with its defaults.
OWN: dict[Any, Callable[[Mapping[str, Any]], Any]] = {
    read_prevalidation_facts: _prevalidation,
    read_hit_rate_nights: lambda f: f.get("hit_rate_nights", 0),
    read_smt_holdout_days: lambda f: f.get("smt_holdout_days", 0),
    read_last_restart: lambda f: f.get("close_restart_day"),
    read_fill_notice_apart: lambda f: f.get("fill_notice_apart", 0),
    read_live_kis: lambda f: f.get("trader_live_kis", False),
    read_last_backup: lambda f: f.get("state_backup"),
}


def asked(job: Job[Any], scene: Scene, quiet: bool) -> bool:
    """``job``'s condition on ``scene``, with its own facts."""
    records = scene.records([j.name for j in JOBS], job.name)
    facts = None if job.facts is None else OWN[job.facts](scene.facts)
    night = Night(scene.day, scene.trading_day, NOW, records.get(job.name), records, facts)
    condition = job.quiet if quiet and job.quiet is not None else job.due
    return condition(night)


def test_every_reader_of_the_jobs_has_its_facts_here() -> None:
    assert {job.facts for job in JOBS} - {None} <= set(OWN), "a new reader: add it to OWN"


def test_every_job_decides_each_night_as_before_its_own_facts() -> None:
    kept: dict[str, str] = json.loads(KEPT.read_text())
    held = [job for job in JOBS if job.name in kept]
    assert held, f"no job in the file: {WRITE}"

    found = decisions(held, asked)

    every = [scene.key for scene in scenes()]
    moved = {
        name: [key for key, was, now in zip(every, marks(kept[name]), now, strict=True) if was != now][:3]
        for name, now in found.items()
        if name in kept and runs(now) != kept[name]
    }
    assert moved == {}, f"a job decides otherwise on these nights (on purpose: {WRITE})"
    assert all("E" not in now for now in found.values()), "a condition raised"


def write() -> None:
    found = decisions(list(JOBS), asked)
    KEPT.write_text(json.dumps({name: runs(now) for name, now in found.items()}, indent=1) + "\n")


if __name__ == "__main__":
    write()
