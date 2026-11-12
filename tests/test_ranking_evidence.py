"""The ranking's walk-forward evidence (#697, ``ranking_evidence.py``): a bar-close algorithm's
pre-validation replay, a daily one's walk-forward days, each read only while of the code and answers it
runs now. Made-up forecasts, no price."""

from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from krx_quant.algorithms import DAILY_ALGORITHMS, KindAlgorithm
from krx_quant.barriers import BARRIER, CLIMATE, DOWN_FIRST, UP_FIRST
from krx_quant.feeds.base import KST
from krx_quant.kind_registry import DAILY_KINDS
from krx_quant.kinds import KindScored
from krx_quant.open_to_close import CLIMATE as OTC_CLIMATE
from krx_quant.predictions import PredictionStore
from krx_quant.prevalidation import code_digest, inputs_of, report_path
from krx_quant.ranking import RECENT_DAYS, WALK_FORWARD
from krx_quant.ranking_evidence import (
    WalkForwardEvidence,
    evidence_path,
    keep_daily,
    replay_path,
    stale_daily,
)
from tests.test_ranking import _stored
from tests.test_verdict_engine import DOWN, Leaning

TODAY = date(2026, 11, 12)
LEAN: KindAlgorithm = Leaning("lean-4", "barrier", "velocity", DOWN)  # type: ignore[assignment]
COX = next(a for a in DAILY_ALGORITHMS if a.name == "cox")
OTC = next(k for k in DAILY_KINDS if k.name == COX.kind)


def report(state: Path, name: str, code: str, version: int) -> None:
    path = report_path(state, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"code": code, "target_version": version, "passed": False}))


def replayed(state: Path, days: list[date]) -> None:
    """``lean-4``'s pre-validation replay of ``days``: 0.6 on the class that happened, the climate a third."""
    store = PredictionStore(replay_path(state, LEAN.name))
    for day in days:
        for minute, label in ((1, UP_FIRST), (2, DOWN_FIRST)):
            mine = (0.6, 0.2, 0.2) if label == UP_FIRST else (0.2, 0.2, 0.6)
            _stored(store, day, minute, CLIMATE, (1 / 3, 1 / 3, 1 / 3), label)
            _stored(store, day, minute, LEAN.name, mine, label)
    store.record_models([(LEAN.name, f"{LEAN.name}-v1", BARRIER.answer_version)])
    store.close()


def test_a_bar_close_algorithm_is_evidenced_by_its_pre_validation_replay(tmp_path: Path) -> None:
    """Input: a 25-day replay of ``lean-4`` beside its report of the code it runs. Output: its latest
    ``RECENT_DAYS`` days before the night asked, each ln 3 − ln(1/0.6) a prediction; none with a report of
    other code, of another answer version, or without the replay."""
    days = [TODAY - timedelta(days=d) for d in range(30, 5, -1)]
    replayed(tmp_path, days)
    gate = WalkForwardEvidence(tmp_path, 3)

    assert gate(LEAN, BARRIER, TODAY) is None, "no report"
    report(tmp_path, LEAN.name, code_digest(LEAN), BARRIER.answer_version)
    found = gate(LEAN, BARRIER, days[-3])
    assert found is not None and found.source == WALK_FORWARD
    assert [d.day for d in found.days] == days[-3 - RECENT_DAYS : -3]
    assert found.days[0].skill / found.days[0].n == pytest.approx(math.log(3) - math.log(1 / 0.6))
    report(tmp_path, LEAN.name, "other", BARRIER.answer_version)
    assert gate(LEAN, BARRIER, TODAY) is None, "other code"
    report(tmp_path, LEAN.name, code_digest(LEAN), BARRIER.answer_version + 1)
    assert gate(LEAN, BARRIER, TODAY) is None, "another answer version"


def scored(name: str, day: date, p: tuple[float, float, float], outcome: int) -> KindScored:
    at = datetime.combine(day, datetime.min.time(), KST) + timedelta(hours=8, minutes=50)
    return KindScored(name, "v", "005930", at, p, outcome, {}, {})


def test_a_daily_algorithm_is_evidenced_by_its_walk_forward_kept_beside_its_code(tmp_path: Path) -> None:
    """Input: a walk-forward of ``cox`` over three days (0.5 on what happened, the climate a third), kept;
    then the night it is asked. Output: its days before the night, scored against the climate; stale once
    its code or inputs are others; ``stale_daily`` lists the daily algorithms without current evidence."""
    days = [date(2019, 1, 2), date(2019, 1, 3), date(2019, 1, 4)]
    records = {
        COX.name: [scored(COX.name, d, (0.5, 0.25, 0.25), 0) for d in days],
        OTC_CLIMATE: [scored(OTC_CLIMATE, d, (1 / 3, 1 / 3, 1 / 3), 0) for d in days],
    }
    assert COX in stale_daily(tmp_path, 3)
    path = keep_daily(tmp_path, COX, OTC, records, inputs_of(COX), datetime(2026, 11, 12, 23, tzinfo=KST))
    gate = WalkForwardEvidence(tmp_path, 3)

    assert path == evidence_path(tmp_path, COX.name)
    found = gate(COX, OTC, days[-1])
    assert found is not None and [d.day for d in found.days] == days[:2]
    assert found.days[0].skill == pytest.approx(math.log(3) - math.log(2))
    assert (found.days[0].calls, found.days[0].hits) == (1, 1)
    assert COX not in stale_daily(tmp_path, 3)
    kept = json.loads(path.read_text())
    path.write_text(json.dumps(kept | {"code": "other"}))
    assert gate(COX, OTC, TODAY) is None and COX in stale_daily(tmp_path, 3)


def test_the_job_runs_while_a_daily_algorithm_lacks_current_evidence() -> None:
    """``ranking-evidence-697``: due on a night some daily algorithm's evidence is stale, not on one
    none is; after a failure, again only on other code; a failure alone calls an agent."""
    from krx_quant.scheduled import JOBS
    from krx_quant.scheduled.core import FAILED, OK, JobRecord, Night
    from krx_quant.scheduled.prevalidations import PREVALIDATION_ISSUE
    from krx_quant.scheduled.research import RANKING_EVIDENCE

    def night(stale: int, record: JobRecord | None = None, commit: str = "c1") -> Night[int]:
        return Night(TODAY, True, commit, record, {}, stale)

    at = datetime(2026, 11, 12, 23, tzinfo=KST)
    assert RANKING_EVIDENCE in JOBS and RANKING_EVIDENCE.command == ("ranking-evidence",)
    assert [t.issue for t in RANKING_EVIDENCE.targets] == [PREVALIDATION_ISSUE]
    assert RANKING_EVIDENCE.due(night(8)) and not RANKING_EVIDENCE.due(night(0))
    assert RANKING_EVIDENCE.due(night(1, JobRecord(TODAY, OK, at, "c1", 0)))
    failed = JobRecord(TODAY, FAILED, at, "c1", 1)
    assert not RANKING_EVIDENCE.due(night(1, failed)) and RANKING_EVIDENCE.due(night(1, failed, "c2"))


def test_the_command_keeps_nothing_without_krx_days(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``krx-quant ranking-evidence`` on a server with no KRX day candle: each daily algorithm is stale,
    walked over no day, and nothing is kept (so it is walked again once they are in); exit 0."""
    from krx_quant import cli

    monkeypatch.setenv("KRX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KRX_STATE_DIR", str(tmp_path / "state"))

    assert cli.main(["ranking-evidence"]) == 0
    out = capsys.readouterr().out
    assert f"{len(DAILY_ALGORITHMS)} daily algorithm(s) without current walk-forward evidence" in out
    assert "nothing kept" in out and not (tmp_path / "state" / "rankings").exists()
