"""A running bar-close algorithm whose code changes is judged again (#454, ``revisions.py``).

Input: an engine database with statuses (and revisions) as of earlier days, and the code each
algorithm runs now (a stand-in gate: ``Gate``, its digests and verdicts set by the test; the real
gate reads the reports, at the end). Output: the revisions the engine keeps, each algorithm's
status and history, the days its probation counts, its simulation, and the alerts sent.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest

from krx_quant.algorithms import ALGORITHMS, Algorithm, KindAlgorithm, Model
from krx_quant.cli import main
from krx_quant.feeds import KST
from krx_quant.lifecycle import CODE_CHANGED, CODE_REFUSED, REGISTERED, Status, target_changed
from krx_quant.market_calendar import MarketCalendar, first_whole_day
from krx_quant.predictions import (
    DB_NAME,
    REVISION_CHANGED,
    REVISION_FIRST,
    REVISION_PASSED,
    REVISION_REFUSED,
    PredictionStore,
)
from krx_quant.prevalidation import MIN_DAYS, code_digest, report_path, revision_report
from krx_quant.revisions import Code, Refactor, RevisionGate, Verdict
from krx_quant.scheduled import Revising, revising_facts
from krx_quant.touch import TARGET_VERSION
from tests.replay_events import DAY, at, run
from tests.test_lifecycle import (
    ALPHA,
    BETA,
    DELTA,
    GAMMA,
    RIGHT,
    listed,
    put_days,
    stored,
    weekdays_before,
)
from tests.test_prevalidation import DAYS as PV_DAYS
from tests.test_prevalidation import settings as pv_settings

ON_TARGET = {name: Model(f"{name}-v", {}, target_version=TARGET_VERSION) for name in ("alpha", "beta")}


class Gate(RevisionGate):
    """The live gate with the digests and verdicts the test sets: ``digests`` by algorithm (``a1``
    unless set), ``verdicts`` by algorithm and revision boundary."""

    def __init__(
        self,
        digests: Mapping[str, str] | None = None,
        verdicts: Mapping[tuple[str, datetime], Verdict] | None = None,
        refactors: Sequence[Refactor] = (),
    ) -> None:
        super().__init__(Path("unused"), TARGET_VERSION, refactors=refactors)
        self.digests = dict(digests or {})
        self.verdicts = dict(verdicts or {})

    def code(self, algorithm: Algorithm | KindAlgorithm) -> Code:
        return Code(self.digests.get(algorithm.name, "a1"), TARGET_VERSION, {})

    def verdict(self, algorithm: Algorithm | KindAlgorithm, boundary: datetime) -> Verdict | None:
        return self.verdicts.get((algorithm.name, boundary))


def put_statuses(db: Path, statuses: Mapping[str, tuple[str, str]], since: datetime) -> None:
    """``statuses`` (name: status, reason) since ``since``, on the touch target already."""
    with stored(db) as store:
        for name, (status, reason) in statuses.items():
            store.add_status(name, since, None, Status(status), reason)
        store.mark_target(TARGET_VERSION, since)


def history(db: Path, name: str) -> list[dict[str, Any]]:
    with stored(db) as store:
        return store.status_history(name)


async def start(db: Path, when: datetime, gate: Gate, algorithms: Sequence[Any], **options: Any) -> Any:
    """An engine started at ``when`` with ``gate``, run a minute (no bar close before 09:01)."""
    calendar = MarketCalendar({when.date(): True, DAY: True})
    return await run(
        db,
        [],
        start=when,
        until=when + timedelta(minutes=1),
        calendar=calendar,
        algorithms=algorithms,
        kind_algorithms=(),
        revisions=gate,
        **options,
    )


EARLIER = datetime.combine(weekdays_before(DAY, 30)[0], time(8), KST)


async def test_the_first_start_records_each_code_as_it_runs_and_changes_no_status(tmp_path: Path) -> None:
    """Input: alpha active, beta on the probation its pre-validation began (waiting for its
    simulation, as neyman on 2026-11-02), gamma retired, delta a clone; no revision kept yet.
    Output: the first start with revisions records each one's code as its first revision, from
    then, and changes no status: beta still waits for its simulation. A start again records
    nothing more."""
    db = tmp_path / "engine.db"
    put_statuses(
        db,
        {
            "alpha": ("active", "set by the test"),
            "beta": ("probation", REGISTERED),
            "gamma": ("retired", "retired by command"),
            "delta": ("clone", "set by the test"),
        },
        EARLIER,
    )
    names = ("alpha", "beta", "gamma", "delta")
    before = {name: history(db, name) for name in names}

    first = await start(db, at("06:00:00"), Gate(), [ALPHA, BETA, GAMMA, DELTA])
    await start(db, at("06:30:00"), Gate(), [ALPHA, BETA, GAMMA, DELTA])

    assert {name: history(db, name) for name in names} == before, "no status changed"
    with stored(db) as store:
        for name in names:
            [revision] = store.revision_history(name)
            assert (revision.code, revision.state, revision.verdict) == ("a1", REVISION_FIRST, None)
            assert revision.seen_at == revision.boundary == at("06:00:00")
        assert store.to_simulate(["beta"]) == ["beta"], "its simulation still waits"
    assert first.revising() == []


async def test_new_code_restarts_probation_once_and_leaves_a_retired_one_retired(tmp_path: Path) -> None:
    """Input: alpha active, beta benched, gamma retired, delta a clone, each first recorded on
    code a1; then all four deployed on code a2. Output: alpha, beta and delta go back on probation
    (``code changed``), once: a start again on a2 leaves them be; none is simulated; gamma stays
    retired, its revision a1."""
    db = tmp_path / "engine.db"
    put_statuses(
        db,
        {
            "alpha": ("active", "set by the test"),
            "beta": ("bench", "set by the test"),
            "gamma": ("retired", "retired by command"),
            "delta": ("clone", "set by the test"),
        },
        EARLIER,
    )
    algorithms = [ALPHA, BETA, GAMMA, DELTA]
    await start(db, at("06:00:00", weekdays_before(DAY, 1)[0]), Gate(), algorithms)
    changed = Gate(dict.fromkeys(("alpha", "beta", "gamma", "delta"), "a2"))

    deployed = await start(db, at("06:00:00"), changed, algorithms)
    await start(db, at("06:30:00"), changed, algorithms)

    for name, was in (("alpha", "active"), ("beta", "bench"), ("delta", "clone")):
        assert [(h["previous"], h["status"], h["reason"]) for h in history(db, name)[1:]] == [
            (was, "probation", CODE_CHANGED)
        ], "once, at the deploy"
        assert history(db, name)[-1]["at"] == at("06:00:00").isoformat()
    assert [h["status"] for h in history(db, "gamma")] == ["retired"]
    with stored(db) as store:
        assert [(r.code, r.state) for r in store.revision_history("alpha")] == [
            ("a1", REVISION_FIRST),
            ("a2", REVISION_CHANGED),
        ]
        assert store.revisions()["alpha"].boundary == at("06:00:00")
        assert [r.code for r in store.revision_history("gamma")] == ["a1"], "never revived"
        assert store.to_simulate(["alpha", "beta", "delta"]) == [], "never simulated (#298)"
    assert [a.name for a, _ in deployed.revising()] == ["alpha", "beta", "delta"]


async def test_a_reviewed_refactor_is_the_same_revision(tmp_path: Path) -> None:
    """Input: alpha active on a1; a2 is in the reviewed map as a1's refactor, a3 is not. Output: a2
    keeps alpha active, recorded as the same revision (a1's boundary, a1 kept in the history);
    a3 after it puts alpha back on probation."""
    db = tmp_path / "engine.db"
    put_statuses(db, {"alpha": ("active", "set by the test")}, EARLIER)
    first = at("06:00:00", weekdays_before(DAY, 2)[0])
    await start(db, first, Gate(), [ALPHA])
    refactor = Refactor("alpha", "a1", "a2", "PR 0: the same samples, model and forecasts")

    await start(
        db, at("06:00:00", weekdays_before(DAY, 1)[0]), Gate({"alpha": "a2"}, refactors=[refactor]), [ALPHA]
    )
    assert [h["status"] for h in history(db, "alpha")] == ["active"]
    with stored(db) as store:
        mapped = store.revisions()["alpha"]
        assert (mapped.code, mapped.mapped_from, mapped.boundary, mapped.state) == (
            "a2",
            "a1",
            first,
            REVISION_FIRST,
        )
        assert mapped.detail == refactor.evidence

    await start(db, at("06:00:00"), Gate({"alpha": "a3"}, refactors=[refactor]), [ALPHA])
    assert last_change_of(db, "alpha") == ("active", "probation", CODE_CHANGED)


def last_change_of(db: Path, name: str) -> tuple[str | None, str, str]:
    h = history(db, name)[-1]
    return h["previous"], h["status"], h["reason"]


def test_the_first_whole_day_is_that_of_a_start_before_its_open() -> None:
    calendar = MarketCalendar({date(2026, 11, 2): True, date(2026, 11, 3): True})
    assert first_whole_day(calendar, at("06:00:00", date(2026, 11, 2))) == date(2026, 11, 2)
    assert first_whole_day(calendar, at("09:00:00", date(2026, 11, 2))) == date(2026, 11, 2)
    assert first_whole_day(calendar, at("09:00:01", date(2026, 11, 2))) == date(2026, 11, 3)
    assert first_whole_day(calendar, at("20:00:00", date(2026, 10, 30))) == date(2026, 11, 2), "a Friday"


async def test_the_probation_counts_the_revision_s_whole_days_only(tmp_path: Path) -> None:
    """Input: alpha active on a1; a2 deployed at 10:30 on D0, in the session. alpha predicts the
    rest of D0 with a model of the touch target. Output: its stage counts from D0's next trading
    day, not D0 (a status begun in the session counts its day otherwise); deployed before D0's
    open it would count D0."""
    db = tmp_path / "engine.db"
    put_statuses(db, {"alpha": ("active", "set by the test")}, EARLIER)
    d0 = weekdays_before(DAY, 1)[0]
    await start(db, at("06:00:00", d0), Gate(), [ALPHA])

    engine = await run(
        db,
        [],
        start=at("10:30:00", d0),
        until=at("15:45:00", d0),
        calendar=MarketCalendar({d0: True, DAY: True}),
        algorithms=[ALPHA],
        kind_algorithms=(),
        models=ON_TARGET,
        revisions=Gate({"alpha": "a2"}),
    )

    alpha = (await listed(engine))["alpha"]
    assert alpha["status"]["history"][-1]["reason"] == CODE_CHANGED
    assert alpha["stage"]["counted_from"] == str(DAY), "the first whole trading day after 10:30 on D0"
    with stored(db) as store:
        assert any(
            p["bar_close"] > at("10:30:00", d0).isoformat() for p in store.recent("005930", 10, "alpha")
        )


async def ten_days_right(db: Path, deploy: datetime) -> None:
    """alpha active on a1, deployed on a2 at ``deploy`` (06:00 of the first of the ten trading days to
    DAY), right on the nine before DAY."""
    put_statuses(db, {"alpha": ("active", "set by the test")}, EARLIER)
    await start(db, deploy - timedelta(days=1), Gate(), [ALPHA])
    await start(db, deploy, Gate({"alpha": "a2"}), [ALPHA])
    put_days(db, weekdays_before(DAY, 9), {"alpha": RIGHT})


async def test_the_probation_makes_it_active_only_once_its_pre_validation_passed(tmp_path: Path) -> None:
    """Input: alpha back on probation for a2 ten trading days before DAY, right throughout. Output:
    DAY's review, its tenth day, leaves it on probation while a2's pre-validation has not passed;
    with it passed, the same review makes it active."""
    deploy = at("06:00:00", weekdays_before(DAY, 9)[0])
    waiting, passed = tmp_path / "waiting.db", tmp_path / "passed.db"
    for db in (waiting, passed):
        await ten_days_right(db, deploy)

    await run(
        db=waiting,
        events=[],
        until=at("15:45:00"),
        algorithms=[ALPHA],
        kind_algorithms=(),
        min_sample=20,
        revisions=Gate({"alpha": "a2"}),
    )
    await run(
        db=passed,
        events=[],
        until=at("15:45:00"),
        algorithms=[ALPHA],
        kind_algorithms=(),
        min_sample=20,
        revisions=Gate({"alpha": "a2"}, {("alpha", deploy): Verdict(True, "")}),
    )

    assert last_change_of(waiting, "alpha") == ("active", "probation", CODE_CHANGED)
    assert last_change_of(passed, "alpha")[:2] == ("probation", "active")
    assert last_change_of(passed, "alpha")[2].startswith("10 days on probation;")
    with stored(passed) as store:
        assert store.revisions()["alpha"].verdict == REVISION_PASSED


async def test_a_pass_alone_makes_nothing_active(tmp_path: Path) -> None:
    """Input: alpha back on probation for a2 that morning; its pre-validation passes. Output: the
    verdict is kept, alpha stays on probation (its review days are ahead)."""
    db = tmp_path / "engine.db"
    put_statuses(db, {"alpha": ("active", "set by the test")}, EARLIER)
    await start(db, at("06:00:00", weekdays_before(DAY, 1)[0]), Gate(), [ALPHA])
    deploy = at("06:00:00")
    await start(db, deploy, Gate({"alpha": "a2"}), [ALPHA])

    engine = await run(
        db,
        [],
        start=at("08:00:00"),
        until=at("15:45:00"),
        algorithms=[ALPHA],
        kind_algorithms=(),
        revisions=Gate({"alpha": "a2"}, {("alpha", deploy): Verdict(True, "")}),
    )

    assert last_change_of(db, "alpha") == ("active", "probation", CODE_CHANGED)
    assert engine.revising() == []
    with stored(db) as store:
        assert store.revisions()["alpha"].verdict == REVISION_PASSED


async def test_a_refusal_retires_it_as_soon_as_the_engine_reads_it_told_once(tmp_path: Path) -> None:
    """Input: alpha back on probation for a2; its pre-validation refuses a2 that night. Output: the
    engine retires it as it reads the verdict, here as it starts the next morning (else at a bar
    close): nothing predicted that day; the review tells it once, the next day's, after a restart,
    not again."""
    db = tmp_path / "engine.db"
    put_statuses(db, {"alpha": ("active", "set by the test")}, EARLIER)
    d0 = weekdays_before(DAY, 1)[0]
    await start(db, at("06:00:00", weekdays_before(d0, 1)[0]), Gate(), [ALPHA])
    deploy = at("06:00:00", d0)
    await start(db, deploy, Gate({"alpha": "a2"}), [ALPHA])
    refused = Gate({"alpha": "a2"}, {("alpha", deploy): Verdict(False, "stats: weakest lift -0.0100")})
    told: list[str] = []

    async def notify(text: str) -> bool:
        told.append(text)
        return True

    engine = await run(
        db,
        [],
        start=at("08:00:00"),
        until=at("15:45:00"),
        algorithms=[ALPHA],
        kind_algorithms=(),
        revisions=refused,
        notify=notify,
    )
    next_day = DAY + timedelta(days=1)
    await run(
        db,
        [],
        start=at("08:00:00", next_day),
        until=at("15:45:00", next_day),
        calendar=MarketCalendar({DAY: True, next_day: True}),
        algorithms=[ALPHA],
        kind_algorithms=(),
        revisions=refused,
        notify=notify,
    )

    previous, status, reason = last_change_of(db, "alpha")
    assert (previous, status) == ("probation", "retired")
    assert reason == f"{CODE_REFUSED}: stats: weakest lift -0.0100"
    assert history(db, "alpha")[-1]["at"] == at("08:00:00").isoformat(), "as it starts, before any bar close"
    with stored(db) as store:
        assert store.revisions()["alpha"].verdict == REVISION_REFUSED
        assert all(p["bar_close"] < str(DAY) for p in store.recent("005930", 1000, "alpha")), "nothing on DAY"
    refusals = [t for t in told if t.startswith("코드 변경 사전 검증 거부: alpha")]
    assert len(refusals) == 1, told
    assert "stats: weakest lift -0.0100" in refusals[0]
    assert engine.revising() == []


async def test_a_new_algorithm_s_first_revision_is_the_code_it_passed_with(tmp_path: Path) -> None:
    """Input: no status for alpha; the engine enrolls it (its pre-validation passed). Output: its
    code is recorded as its first revision when it is enrolled; new code at the next start puts it
    back on probation, as any running algorithm."""
    db = tmp_path / "engine.db"
    await run(
        db,
        [],
        start=at("08:00:00"),
        until=at("09:02:00"),
        algorithms=[ALPHA],
        kind_algorithms=(),
        revisions=Gate(),
    )
    with stored(db) as store:
        [first] = store.revision_history("alpha")
        assert (first.code, first.state) == ("a1", REVISION_FIRST)
    next_day = DAY + timedelta(days=1)
    await start(db, at("06:00:00", next_day), Gate({"alpha": "a2"}), [ALPHA])
    assert last_change_of(db, "alpha") == ("probation", "probation", CODE_CHANGED)


async def test_a_new_target_s_restart_waits_for_a_changed_revision_s_verdict(tmp_path: Path) -> None:
    """Input: alpha on probation for new code a2, not judged yet; a new target restarts it (a
    reason that is simulated). Output: no simulation while a2 waits for its pre-validation (the days
    replayed may be those a2 was chosen on); once it passed, it is simulated as before."""
    db = tmp_path / "engine.db"
    put_statuses(db, {"alpha": ("active", "set by the test")}, EARLIER)
    await start(db, at("06:00:00", weekdays_before(DAY, 1)[0]), Gate(), [ALPHA])
    deploy = at("06:00:00")
    await start(db, deploy, Gate({"alpha": "a2"}), [ALPHA])
    with stored(db) as store:
        store.change_status(
            "alpha", at("07:00:00"), Status.PROBATION, Status.PROBATION, target_changed("direction", 9)
        )
        assert store.to_simulate(["alpha"]) == []
        store.judge_revision(store.revisions()["alpha"], REVISION_PASSED, at("07:30:00"), None)
        assert store.to_simulate(["alpha"]) == ["alpha"]


# Turing, registered before pre-validation, and the real gate


async def test_turing_follows_the_rule_too_its_exemption_ends_with_its_first_revision(tmp_path: Path) -> None:
    """Input: turing active, its revision kept on code it no longer runs; newton active without a
    revision. Output: the real gate, on the code the registry runs: turing goes back on probation,
    newton's code is recorded as its first revision and it stays active."""
    db = tmp_path / "state" / DB_NAME
    put_statuses(
        db, {"turing": ("active", "set by the test"), "newton": ("active", "set by the test")}, EARLIER
    )
    with stored(db) as store:
        store.add_revision(
            "turing", "0123456789abcdef", TARGET_VERSION, {}, EARLIER, boundary=EARLIER, state=REVISION_FIRST
        )
    gate = RevisionGate(tmp_path / "state", TARGET_VERSION)
    algorithms = [a for a in ALGORITHMS if a.name in ("newton", "turing")]

    engine = await run(
        db,
        [],
        start=at("06:00:00"),
        until=at("06:01:00"),
        algorithms=algorithms,
        kind_algorithms=(),
        revisions=gate,
        target_version=TARGET_VERSION,
    )

    assert last_change_of(db, "turing") == ("active", "probation", CODE_CHANGED)
    assert [h["status"] for h in history(db, "newton")] == ["active"]
    turing = next(a for a in algorithms if a.name == "turing")
    with stored(db) as store:
        assert store.revisions()["turing"].code == code_digest(turing)
        assert store.revisions()["newton"].state == REVISION_FIRST
    assert [a.name for a, _ in engine.revising()] == ["turing"]


def report_of(
    algorithm: Algorithm, boundary: datetime | None, *, passed: bool, days: int = MIN_DAYS
) -> dict[str, Any]:
    """A report as ``prevalidate`` writes it, on the code and target ``algorithm`` runs now; for
    the revision begun at ``boundary`` (``--revision``) unless None."""
    out: dict[str, Any] = {
        "algorithm": algorithm.name,
        "code": code_digest(algorithm),
        "prevalidated_at": at("23:00:00").isoformat(),
        "passed": passed,
        "days": [f"2026-10-{d:02d}" for d in range(1, days + 1)],
        "checks": [{"check": "stats", "passed": passed, "detail": "weakest lift -0.0100"}],
        "target_version": TARGET_VERSION,
    }
    if boundary is not None:
        out["revision"] = {"boundary": boundary.isoformat(), "since": boundary.date().isoformat()}
    return out


@pytest.mark.parametrize(
    ("bound", "passed", "days", "verdict"),
    [
        ("this", True, MIN_DAYS, Verdict(True, "")),
        ("this", False, MIN_DAYS, Verdict(False, "stats: weakest lift -0.0100")),
        ("this", False, MIN_DAYS - 1, None),  # a look on fewer days is no verdict
        ("another", True, MIN_DAYS, None),  # an earlier revision's
        (None, True, MIN_DAYS, None),  # a first pre-validation's
    ],
)
def test_the_gate_reads_the_verdict_of_the_report_made_for_the_revision(
    tmp_path: Path, bound: str | None, passed: bool, days: int, verdict: Verdict | None
) -> None:
    [turing] = [a for a in ALGORITHMS if a.name == "turing"]
    boundary = at("06:00:00")
    made_for = {"this": boundary, "another": boundary - timedelta(days=7), None: None}[bound]
    path = report_path(tmp_path, "turing")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(report_of(turing, made_for, passed=passed, days=days)))

    assert RevisionGate(tmp_path, TARGET_VERSION).verdict(turing, boundary) == verdict


def test_the_command_needs_a_changed_revision_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KRX_STATE_DIR", str(state))
    monkeypatch.setenv("KRX_DATA_DIR", str(tmp_path / "data"))

    assert main(["prevalidate", "turing", "--revision"]) == 1
    assert "no revision to judge" in capsys.readouterr().err
    store = PredictionStore(state / DB_NAME)
    store.add_revision("turing", "a1", TARGET_VERSION, {}, EARLIER, boundary=EARLIER, state=REVISION_FIRST)
    store.close()
    assert main(["prevalidate", "turing", "--revision"]) == 1, "its first revision: nothing changed"
    assert main(["prevalidate", "cox", "--revision"]) == 1
    assert "daily kind" in capsys.readouterr().err


def test_the_command_judges_the_changed_code_on_the_days_after_the_change_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Input: three recorded days (14 to 16 September); newton's code changed at 10:00 on the 15th,
    in its session. Output: ``prevalidate newton --revision`` replays the 16th only, its first whole
    day, its report bound to that revision (a look on one day: no verdict); newton is not exempt;
    the live engine's database is not written."""
    s = pv_settings(tmp_path, PV_DAYS[:3])
    s.state_dir.mkdir()
    live = s.state_dir / DB_NAME
    boundary = datetime.combine(PV_DAYS[1], time(10), KST)
    store = PredictionStore(live)
    store.add_status("newton", EARLIER, None, Status.ACTIVE, "set by the test")
    store.add_revision("newton", "a1", TARGET_VERSION, {}, EARLIER, boundary=EARLIER, state=REVISION_FIRST)
    store.add_revision(
        "newton", "a2", TARGET_VERSION, {}, boundary, boundary=boundary, state=REVISION_CHANGED
    )
    store.close()
    before = live.read_bytes()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KRX_DATA_DIR", str(s.data_dir))
    monkeypatch.setenv("KRX_STATE_DIR", str(s.state_dir))
    monkeypatch.setenv("KRX_SYMBOLS", "005930")

    code = main(["prevalidate", "newton", "--revision"])

    out = capsys.readouterr().out
    assert code == 1 and "(1 recorded days, 2026-09-16..2026-09-16)" in out
    assert "registered before pre-validation" not in out
    saved = json.loads(report_path(s.state_dir, "newton").read_text())
    assert saved["days"] == ["2026-09-16"]
    assert saved["revision"] == {"boundary": boundary.isoformat(), "since": "2026-09-16"}
    [newton] = [a for a in ALGORITHMS if a.name == "newton"]
    found = revision_report(s.state_dir, newton, boundary, TARGET_VERSION)
    assert found is not None and found[0].days == 1 and not found[0].judged, "a look: no verdict"
    assert revision_report(s.state_dir, newton, EARLIER, TARGET_VERSION) is None, "not the first revision's"
    assert live.read_bytes() == before, "the live engine's database is never written"


def test_the_jobs_count_the_recorded_days_from_the_revision_s_first_whole_day(tmp_path: Path) -> None:
    """Input: three recorded days; turing's code changed at 10:00 on the second, in its session.
    Output: what the scheduled job asks: its first whole day is the third, one recorded day."""
    s = pv_settings(tmp_path, PV_DAYS[:3])
    boundary = datetime.combine(PV_DAYS[1], time(10), KST)
    store = PredictionStore(tmp_path / "engine.db")
    revision = store.add_revision(
        "turing", "a2", TARGET_VERSION, {}, boundary, boundary=boundary, state=REVISION_CHANGED
    )
    store.close()
    [turing] = [a for a in ALGORITHMS if a.name == "turing"]
    after = datetime.combine(PV_DAYS[2] + timedelta(days=3), time(8), KST)

    facts = revising_facts(s, MarketCalendar(), after, [(turing, revision)])

    assert facts == {"turing": Revising(boundary, PV_DAYS[2], 1, None)}
    assert revising_facts(s, MarketCalendar(), after, []) == {}
