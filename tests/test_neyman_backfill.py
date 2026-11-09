"""Neyman's variants judged on backfilled days (#532): #455's three comparisons as registered, on the
backfilled days before the discovery's training, with the live window's plan moved into the saved
report first.

Input: 26 made-up weekdays of one symbol's backfilled bars (one of them cut short), the night's
sample store holding four of them, the discovery as the server saved it (its period moved after the
made-up days), hand-made calls, and made-up days and counts. Output: the days listed and fixed, the
verdict and its power, the report and lines, the store (as it was), the job, and neyman's code digest
(unchanged).
"""

from __future__ import annotations

import json
import math
import random
import re
from collections.abc import Iterator
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from krx_quant import neyman_backfill as module
from krx_quant import neyman_variants
from krx_quant.algorithms.neyman import TRAINING_DAYS, Neyman
from krx_quant.backfill import BAR_SCHEMA, MINUTE_BARS_TR, bars_key
from krx_quant.cli import main
from krx_quant.config import Settings
from krx_quant.features import Leaders
from krx_quant.feeds import KST
from krx_quant.market_calendar import MarketCalendar
from krx_quant.neyman_backfill import (
    CANDIDATE,
    CHECKPOINT,
    GOES_ON_LINE,
    NO_CONCLUSION,
    NOT_A_CANDIDATE,
    PREPARED,
    WARMUP,
    Backfill,
    Checkpoint,
    Hit,
    Listed,
    Tally,
    Until,
    candidates,
    first_training_day,
    fix_list,
    judge,
    labelled_count,
    neyman_backfill,
    power,
    progress,
)
from krx_quant.neyman_variants import DELTA, LEVEL, LIVE_DELTA, A, B, C, D, Fold, Verdict, required_days
from krx_quant.prevalidation import code_digest
from krx_quant.pricing import Direction
from krx_quant.process_log import Ran
from krx_quant.sample_store import STORE_DIR, SampleStore, night_reads, read_day
from krx_quant.scheduled import FAILED, INTERRUPTED, JOBS, NEYMAN_BACKFILL, OK, Facts, JobRecord, Night
from krx_quant.scheduled.research import SMT_DISCOVERY
from krx_quant.training import is_recorded
from tests.synthetic_bars import day_of_bars
from tests.test_neyman_variants import SERVER_DISCOVERY

UP, FLAT, DOWN = Direction.UP, Direction.FLAT, Direction.DOWN
NEYMAN = Neyman()
SYMBOLS = ("005930",)
DAYS = [d for d in (date(2026, 6, 1) + timedelta(days=i) for i in range(40)) if d.weekday() < 5][:26]
SHORT = DAYS[4]  # 09:00-10:00 only: fewer than 200 bar closes
# The discovery's period moved after the made-up days: it trained on their last 20.
PERIOD = ("2026-07-13", "2026-07-31")
WHEN = datetime(2026, 11, 6, 23, tzinfo=KST)
NIGHT = night_reads([NEYMAN], [], [])
RULES = {"algorithms": [NEYMAN], "kind_algorithms": [], "kinds": [], "warmup": 2, "now": WHEN}


def test_neyman_s_package_is_as_its_pre_validation_passed_it() -> None:
    assert code_digest(NEYMAN) == "650316eaa37bfbe7"
    assert Path(module.__file__).parent.name == "krx_quant", "outside neyman's package"


def test_the_days_come_before_the_discovery_s_training_past_the_warmup() -> None:
    # 70 days with data, the discovery's first day after them: it trained on the last 20.
    days = [date(2025, 9, 1) + timedelta(days=i) for i in range(70)]
    start = date(2025, 11, 20)
    first = first_training_day(days, start)
    assert first == days[50] == [d for d in days if d < start][-TRAINING_DAYS]
    assert WARMUP == TRAINING_DAYS + PREPARED == 40, "20 to train on, 20 for the first's slot returns"
    recorded = {days[45]}
    found = candidates(days, recorded, first)
    assert found == [d for d in days[40:50] if d != days[45]], "backfilled, past the warm-up, before it"
    assert all(d < first for d in found)
    # Fewer than 20 days before the discovery: it trained on all of them, none can judge.
    assert first_training_day(days[:10], start) == days[0]
    assert candidates(days[:10], set(), days[0]) == []
    assert first_training_day([], start) is None

    # Each counted once, by its data alone; in with 200 scored bar closes.
    counted: list[date] = []

    def count(day: date) -> int:
        counted.append(day)
        return 150 if day == days[42] else 1000

    listed = fix_list(days, recorded, start, count, WHEN, min_scored=200)
    assert counted == found
    assert listed.days == [d for d in found if d != days[42]] and listed.short == [days[42]]
    assert (listed.oldest, listed.first, listed.start, listed.warmup) == (days[0], first, start, 40)
    assert Listed.read(json.loads(json.dumps(listed.as_dict()))) == listed
    assert fix_list([], set(), start, count, WHEN, min_scored=200).days == []


def hit(day: date, minute: int, called: Direction, outcome: Direction) -> Hit:
    return Hit("005930", datetime.combine(day, time(9, minute), KST), called, outcome)


def tally(days: list[date], calls: dict[str, list[list[Direction]]], outcomes: list[Direction]) -> Tally:
    """A tally of ``days``, each arm calling ``calls[arm][i]`` on day i's bar closes."""
    found = Tally()
    for arm, by_day in calls.items():
        for day, called in zip(days, by_day, strict=True):
            found.hits[arm] += [
                hit(day, m + 1, c, o) for m, (c, o) in enumerate(zip(called, outcomes, strict=True))
            ]
    found.folds = [Fold(d, 4000, "0+20", len(outcomes)) for d in days]
    return found


def test_the_verdict_is_as_registered_with_fewer_days_than_206() -> None:
    # Five days of four bar closes: A right on the two flat ones, B always right, C right on one more on
    # three days of five, D wrong more often.
    days = [date(2026, 3, 2) + timedelta(days=i) for i in range(5)]
    outcomes = [UP, FLAT, FLAT, DOWN]
    a = [[FLAT] * 4] * 5
    b = [outcomes] * 5
    c = [[UP, FLAT, FLAT, FLAT] if i < 3 else [FLAT] * 4 for i in range(5)]
    d = [[DOWN, UP, FLAT, FLAT]] * 5
    found = tally(days, {A: a, B: b, C: c, D: d}, outcomes)
    spreads = SERVER_DISCOVERY["spreads"]
    judged = judge(found, spreads, WHEN)  # type: ignore[arg-type]

    # The three comparisons, each by #455's rule at 98.333%, on five days (N_avail, not 206).
    assert LEVEL == pytest.approx(1 - 0.05 / 3)
    assert set(judged.verdicts) == {B, C, D} and judged.days == 5
    for arm in (B, C, D):
        assert judged.verdicts[arm] == Verdict.of(found.hits[arm], found.hits[A], LEVEL)
    assert judged.verdicts[B].candidate and judged.candidates == [B]
    c_v = judged.verdicts[C]
    assert c_v.pairs.value is not None and c_v.pairs.value > 0, "above 0 paired"
    assert c_v.by_day is not None and c_v.by_day.low is not None and c_v.by_day.low < 0, "not by day"
    # Power with five days at the discovery's s_d is low: what did not pass has no conclusion.
    assert judged.powers[C][f"{DELTA:g}"] == pytest.approx(power(5, spreads["C"]["sd"], DELTA))  # type: ignore[index]
    assert judged.conclusions == {B: CANDIDATE, C: NO_CONCLUSION, D: NO_CONCLUSION}
    # With the power (a discovery's s_d far smaller), what did not pass is not a candidate.
    tight = {arm: {"sd": 0.0001} for arm in (B, C, D)}
    assert judge(found, tight, WHEN).conclusions == {B: CANDIDATE, C: NOT_A_CANDIDATE, D: NOT_A_CANDIDATE}
    # These days' own s_d, described.
    assert judged.spreads[B].sd == 0.0 and judged.own_powers[B] == {"0.003": None, "0.005": None}
    assert judged.spreads[C].days == 5 and judged.spreads[C].sd is not None
    saved = judged.as_dict()
    assert saved["n_avail"] == 5 and saved["level"] == LEVEL
    assert {arm: v["conclusion"] for arm, v in saved["verdict"].items()} == judged.conclusions
    assert set(saved["described"]["by_month"]) == {"2026-03"}
    assert set(saved["described"]["by_slot"]) == {"09:00"}
    assert saved["described"]["caveats"] and saved["arms"][B]["accuracy"]["value"] == 1.0
    # No day in: nothing passes, nothing concludes, and the lines still say so.
    empty = judge(Tally(), spreads, WHEN)  # type: ignore[arg-type]
    assert empty.days == 0 and set(empty.conclusions.values()) == {NO_CONCLUSION}
    listed = Listed(WHEN, None, None, date(2026, 10, 12), WARMUP, 200, [], [])
    assert "0 backfilled days can judge (-), 0 with at least 200" in "\n".join(
        module.backfill_lines(listed, empty, 206)
    )


def test_the_power_is_the_inverse_of_455_s_n() -> None:
    sd = SERVER_DISCOVERY["spreads"]["D"]["sd"]  # type: ignore[index]
    n = required_days(sd)
    assert n == 206
    assert power(n, sd, DELTA) == pytest.approx(0.8, abs=0.005) and power(n - 1, sd, DELTA) < 0.8  # type: ignore[operator]
    assert power(n, sd, LIVE_DELTA) > 0.99  # type: ignore[operator]
    assert power(0, sd, DELTA) is None and power(5, None, DELTA) is None and power(5, 0.0, DELTA) is None
    # With 0.3 points at 98.333%: Φ(0.003 √n ÷ s_d − 2.394).
    assert power(100, 0.01, DELTA) == pytest.approx(0.5 + math.erf((0.003 * 10 / 0.01 - 2.394) / 2**0.5) / 2)


def write_bars(data: Path, day: date, minutes: int = 390) -> None:
    """A random walk of one-minute bars from 09:00 (steps of two ticks, about: up, flat and down all
    happen), where the backfill puts them."""
    rng = random.Random(day.toordinal())
    steps = [rng.gauss(0, 2) for _ in range(390)]
    rows = [
        {
            "symbol": SYMBOLS[0],
            "bar_time": b.start,
            "venue": "UN",
            "open": b.open,
            "high": b.high,
            "low": b.low,
            "close": b.close,
            "volume": b.volume,
            "cum_value": 0,
        }
        for b in day_of_bars(day, steps, seed=day.toordinal())[:minutes]
    ]
    path = data / bars_key(day, MINUTE_BARS_TR, SYMBOLS[0])
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=BAR_SCHEMA), path)


@pytest.fixture(scope="module")
def data(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The made-up backfilled days, written once for the module (only read)."""
    root = tmp_path_factory.mktemp("backfilled") / "data"
    for day in DAYS:
        write_bars(root, day, 60 if day == SHORT else 390)
    return root


def settings_of(data: Path, tmp_path: Path, state_dir: str = "state") -> Settings:
    state = tmp_path / state_dir
    report = state / neyman_variants.REPORT
    report.parent.mkdir(parents=True)
    discovery = SERVER_DISCOVERY | {"period": list(PERIOD)}
    saved = {"experiment": neyman_variants.EXPERIMENT, "issue": 455, "discovery": discovery}
    report.write_text(json.dumps(saved))
    return Settings(
        kis_app_key="",
        kis_app_secret="",
        symbols=SYMBOLS,
        etf_symbols=(),
        index_codes=(),
        us_symbols=(),
        data_dir=data,
        state_dir=state,
    )


def store_files(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def stored(s: Settings, days: Iterator[date] | list[date]) -> dict[str, bytes]:
    """``days`` stored as the night stores them (its key: neyman's features alone here)."""
    store = SampleStore(s.state_dir / STORE_DIR)
    for day in days:
        read = read_day(
            store,
            s.data_dir,
            day,
            MarketCalendar(),
            s.symbols,
            NIGHT.names,
            s.fee_rate,
            Leaders.of(s),
            target_version=s.target_version,
            kinds=NIGHT.kinds,
        )
        assert read.made_stored
    return store_files(store.root)


# 26 made-up backfilled days, five of them made twice (counted, then walked) and four stored, with ten
# tree models on each of the three days judged: past the 30 s default on a loaded CI runner.
@pytest.mark.timeout(240)
def test_the_backfill_judges_the_listed_days_and_leaves_the_night_s_store_as_it_is(
    data: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    s = settings_of(data, tmp_path)
    assert not any(is_recorded(data, d, SYMBOLS) for d in DAYS), "backfilled only"
    # The night's store holds the first two days (the first day's window) and two of the discovery's.
    before = stored(s, [DAYS[0], DAYS[1], DAYS[10], DAYS[11]])
    caplog.set_level("INFO", logger="krx_quant.neyman_variants")
    fitted: list[int] = []
    fit_arms = neyman_variants.fit_arms

    def fit_after_the_list(*args: object, **kwargs: object) -> object:
        saved = json.loads((s.state_dir / module.REPORT).read_text())
        assert saved["listed"]["days"] and "verdict" not in saved, "the list fixed before any arm is fitted"
        fitted.append(1)
        return fit_arms(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(neyman_variants, "fit_arms", fit_after_the_list)
    run = neyman_backfill(s, **RULES)  # type: ignore[arg-type]

    assert run.stage == module.JUDGED and run.listed is not None and run.judged is not None
    listed, judged = run.listed, run.judged
    # The discovery trained on the 20 days before its first: the days before those, past the warm-up.
    first = DAYS[-TRAINING_DAYS]
    assert (listed.oldest, listed.first, listed.start) == (DAYS[0], first, date(2026, 7, 13))
    assert listed.days == [DAYS[2], DAYS[3], DAYS[5]] and listed.short == [SHORT]
    assert all(d < first for d in listed.days) and fitted
    assert [f.day for f in judged.folds] == listed.days and judged.days == 3
    assert [f.composition for f in judged.folds] == ["0+2", "0+3", "0+5"], "the days before each, backfilled"
    assert {len(judged.verdicts), len(judged.conclusions)} == {3}
    assert set(judged.arms) == {A, B, C, D}
    assert {a["n"] for a in judged.arms.values()} == {sum(f.scored for f in judged.folds)}, (
        "every arm on each"
    )
    assert all(f.scored >= 200 for f in judged.folds)
    # The night's store as it was: read, never written.
    assert store_files(s.state_dir / STORE_DIR) == before
    # The report: the list, the verdict, the lines; the experiment's report has the live plan now.
    saved = json.loads((s.state_dir / module.REPORT).read_text())
    assert saved["listed"] == listed.as_dict() and saved["verdict"]["n_avail"] == 3
    assert set(saved["verdict"]["verdict"]) == {B, C, D}
    main_report = json.loads((s.state_dir / neyman_variants.REPORT).read_text())
    assert main_report["plan"]["n"] == 56 and main_report["discovery"]["period"] == list(PERIOD)
    text = "\n".join(run.lines)
    assert "the live window's plan, changed on 2026-11-04 (#532)" in text and "moved into the report" in text
    assert "days with data from 2026-06-01 (the oldest on the server)" in text
    assert f"the discovery trained from {first} on" in text
    assert "4 backfilled days can judge (2026-06-03..2026-06-08), 3 with at least 200" in text
    assert f"left out: {SHORT}" in text
    assert len(re.findall(r"^  [BCD] .* - A: .* -> (passed|did not pass)$", text, re.M)) == 3
    assert "power with N_avail = 3 days" in text and "conclusions (" in text
    assert len(re.findall(rf"^  [BCD] .*: ({CANDIDATE}|{NOT_A_CANDIDATE}|{NO_CONCLUSION})$", text, re.M)) == 3
    assert "by month" in text and "2026-06:" in text and "caveats" in text and "touch.bar_touch" in text
    assert not re.search(r"\b8[01][0-9],?[0-9]{3}\b", text), "no price"
    logged = [
        r.getMessage() for r in caplog.records if r.getMessage().startswith("neyman backfill: memory on")
    ]
    assert len(logged) == 3

    # Later: the verdict stands; nothing is listed, counted or computed again.
    def never(day: date) -> int:
        raise AssertionError("counted again")

    again = neyman_backfill(s, **RULES | {"count": never})  # type: ignore[arg-type]
    assert again.stage == module.JUDGED_BEFORE and again.judged is None
    assert json.loads((s.state_dir / module.REPORT).read_text()) == saved
    assert "it stands" in "\n".join(again.lines) and "moved into the report" not in "\n".join(again.lines)


@pytest.mark.timeout(120)
def test_a_later_run_reads_the_days_listed_and_counts_none(data: Path, tmp_path: Path) -> None:
    # A run cut after listing (by its time limit, say): the next one reads the same days.
    s = settings_of(data, tmp_path)
    listed = Listed(WHEN, DAYS[0], DAYS[-TRAINING_DAYS], date(2026, 7, 13), 2, 200, [DAYS[3]], [DAYS[2]])
    path = s.state_dir / module.REPORT
    path.write_text(json.dumps({"experiment": module.EXPERIMENT, "listed": listed.as_dict()}))

    def never(day: date) -> int:
        raise AssertionError("counted again")

    run = neyman_backfill(s, **RULES | {"count": never})  # type: ignore[arg-type]
    assert run.listed == listed and run.judged is not None
    assert [f.day for f in run.judged.folds] == [DAYS[3]]
    # A listed day that counts fewer bar closes when walked fails: the list stays as it was.
    cut = Listed(WHEN, DAYS[0], DAYS[-TRAINING_DAYS], date(2026, 7, 13), 2, 200, [SHORT], [])
    path.write_text(json.dumps({"experiment": module.EXPERIMENT, "listed": cut.as_dict()}))
    with pytest.raises(ValueError, match="counted again with fewer bar closes"):
        neyman_backfill(s, **RULES | {"count": never})  # type: ignore[arg-type]
    assert json.loads(path.read_text())["listed"] == cut.as_dict()


def test_a_day_is_counted_from_its_labels_alone(data: Path, tmp_path: Path) -> None:
    s = settings_of(data, tmp_path)
    leaders = Leaders.of(s)
    full, short = (labelled_count(s, MarketCalendar(), leaders, d) for d in (DAYS[3], SHORT))
    assert full >= 200 > short > 0
    # As many as the walk scores: the day's samples with neyman's features and a label.
    from krx_quant.prevalidation import _WalkForward

    walked = _WalkForward(s, NEYMAN, DAYS, MarketCalendar(), leaders).samples_on(DAYS[3])
    assert full == sum(1 for x in walked if x.outcome is not None)


def test_the_command_needs_the_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KRX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KRX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("KRX_SYMBOLS", "005930")
    assert main(["neyman-variants", "--backfill"]) == 1
    assert "no discovery in" in capsys.readouterr().err
    assert not (tmp_path / "state" / module.REPORT).exists()


def test_the_job_goes_on_by_day_until_its_verdict_last_in_the_list() -> None:
    job = NEYMAN_BACKFILL
    assert (job.name, job.command, job.timeout) == (
        "neyman-backfill-532",
        ("neyman-variants", "--backfill"),
        timedelta(hours=5),
    )
    # It starts when a day fits before 05:30 (the first night's listing among it), not five hours.
    assert job.resumes == timedelta(minutes=30) and job.needs == job.resumes
    assert [t.issue for t in job.targets] == [455] and "시세 값이 없다" in job.targets[0].note
    assert "이어 한다" in job.targets[0].note
    assert [a.path for a in job.attach] == ["experiments/neyman-backfill-532.json"]
    assert JOBS[-1] is job, "last: after the pre-validations and the experiments of 11-05"
    assert JOBS.index(job) > JOBS.index(SMT_DISCOVERY)
    assert all(
        JOBS.index(j) < JOBS.index(job) for j in JOBS if j.name.startswith(("prevalidate-", "simulate-"))
    )
    assert job.gives_way_to is None and job.session is None
    sunday = date(2026, 11, 8)
    started = datetime(2026, 11, 8, 23, 0, tzinfo=KST)
    went_on = JobRecord(sunday, OK, started, "abc", 0, 9000.0)
    stopped = JobRecord(sunday, FAILED, started, "abc", None, 18000.0, timed_out=True)
    raised = JobRecord(sunday, FAILED, started, "abc", 1, 60.0)
    cut = JobRecord(sunday + timedelta(days=1), INTERRUPTED, started, "abc")
    open_ = module.Progress(read=True)

    def night(
        found: neyman_variants.Progress,
        record: JobRecord | None = None,
        commit: str = "abc",
        backfill: module.Progress = open_,
    ) -> Night:
        facts = Facts(neyman_variants=found, neyman_backfill=backfill)
        return Night(sunday + timedelta(days=1), False, commit, record, {}, facts)

    discovered = neyman_variants.Progress(True, 56, False, 3)
    assert job.due(night(discovered)), "first, trading or not"
    assert not job.due(night(neyman_variants.Progress(read=True))), "not before the discovery"
    assert not job.due(night(neyman_variants.Progress())), "not on a night its progress was not read"
    assert not job.due(night(discovered, backfill=module.Progress())), "nor the backfill's"
    assert job.due(night(discovered, went_on)), "it goes on the next night"
    assert job.due(night(discovered, stopped)), "and after its limit stopped it: the days walked are kept"
    assert job.due(night(discovered, cut)), "a run a restart cut"
    assert not job.due(night(discovered, went_on, backfill=module.Progress(True, judged=True))), "judged once"
    assert not job.due(night(discovered, raised)), "a failure is not run again on the same code"
    assert job.due(night(discovered, raised, "def")), "but again once a deploy brings other code"
    same_night = JobRecord(sunday + timedelta(days=1), OK, started, "abc", 0, 60.0)
    assert not job.due(night(discovered, same_night)), "once a night"
    # Its issue is called on the run that judges, not on a night it goes on.
    assert job.label_if is not None
    assert not job.label_if(Ran(0, f"...\n  {GOES_ON_LINE} 2026-03-02; the verdict comes once\n", ""))
    assert job.label_if(Ran(0, "verdict (as #455 registered it ...\n", ""))


def test_progress_says_whether_the_verdict_is_in(tmp_path: Path) -> None:
    s = Settings(kis_app_key="", kis_app_secret="", symbols=SYMBOLS, data_dir=tmp_path, state_dir=tmp_path)
    assert progress(s) == module.Progress(True, False)
    path = tmp_path / module.REPORT
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"listed": {}, "progress": {"walked": 3}}))
    assert progress(s) == module.Progress(True, False)
    path.write_text(json.dumps({"listed": {}, "verdict": {"n_avail": 3}}))
    assert progress(s) == module.Progress(True, True)


class Ticks:
    """A clock that moves ``step`` on at each reading."""

    def __init__(self, start: datetime, step: timedelta) -> None:
        self.now, self.step = start, step

    def __call__(self) -> datetime:
        found = self.now
        self.now += self.step
        return found


def test_the_walk_stops_before_a_day_that_would_end_after_its_time() -> None:
    start = datetime(2026, 11, 10, 3, 0, tzinfo=KST)
    # Each day takes an hour: the third would end at 06:00, after 05:30.
    stop = Until(start + timedelta(hours=2, minutes=30), Ticks(start, timedelta(hours=1)))
    assert [stop() for _ in range(3)] == [False, False, True]
    assert stop.slowest == timedelta(hours=1)
    # The first day starts whatever its length, unless the time is past; without one, none stops.
    assert not Until(start + timedelta(minutes=1), Ticks(start, timedelta(hours=1)))()
    assert Until(start, Ticks(start, timedelta(hours=1)))()
    endless = Until(None, Ticks(start, timedelta(hours=9)))
    assert not any(endless() for _ in range(5))


def made_up_tally() -> Tally:
    days = [date(2026, 3, 2), date(2026, 3, 3)]
    calls = {arm: [[UP, FLAT, DOWN], [DOWN, DOWN, UP]] for arm in (A, B, C, D)}
    found = tally(days, calls, [UP, FLAT, UP])
    found.sigmas = {
        ("005930", h.bar_close.isoformat()): 0.0011 * (i + 1) for i, h in enumerate(found.hits[A][:4])
    }
    return found


def kept_in(path: Path, key: dict[str, str], source: Tally) -> None:
    """``source``'s days kept, a line each, as the walk keeps them."""
    checkpoint = Checkpoint(path, key)
    replay = Tally()
    for fold in source.folds:
        n = fold.scored
        at = len(replay.folds) * n
        for arm in (A, B, C, D):
            replay.hits[arm] += source.hits[arm][at : at + n]
        replay.sigmas = dict(source.sigmas)
        replay.folds.append(fold)
        checkpoint.keep(module._day_line(fold.day, fold, replay, n))


def listed_of(days: list[date]) -> Listed:
    return Listed(WHEN, days[0], days[-1], date(2026, 7, 13), 2, 3, days, [])


def test_a_day_kept_gives_back_the_tally_the_walk_gave(tmp_path: Path) -> None:
    source = made_up_tally()
    path = tmp_path / CHECKPOINT
    key = {"code": "c1", "input": "i1"}
    kept_in(path, key, source)
    back = Tally()
    assert Checkpoint(path, key).restore(back, listed_of([f.day for f in source.folds])) == (2, None)
    assert back.hits == source.hits and back.folds == source.folds and back.sigmas == source.sigmas
    # The same hour, minute and month, so the same slot, month and σ̂₄ cell.
    assert [h.bar_close.isoformat() for h in back.hits[A]] == [
        h.bar_close.isoformat() for h in source.hits[A]
    ]
    # Only the listed days' first ones, in order: the rest of the file is dropped.
    first = Tally()
    assert Checkpoint(path, key).restore(first, listed_of([source.folds[0].day])) == (1, None)
    assert first.folds == source.folds[:1] and len(path.read_text().splitlines()) == 2


def test_a_line_cut_short_ends_the_days_kept(tmp_path: Path) -> None:
    source = made_up_tally()
    path = tmp_path / CHECKPOINT
    key = {"code": "c1", "input": "i1"}
    kept_in(path, key, source)
    whole = path.read_text()
    path.write_text(whole[: len(whole) - 20])  # the run was killed as it wrote the second day
    back = Tally()
    assert Checkpoint(path, key).restore(back, listed_of([f.day for f in source.folds])) == (1, None)
    assert back.folds == source.folds[:1] and len(back.hits[A]) == 3
    assert path.read_text() == "".join(whole.splitlines(keepends=True)[:2])


def test_days_kept_with_other_code_or_input_are_set_aside(tmp_path: Path) -> None:
    source = made_up_tally()
    days = [f.day for f in source.folds]
    path = tmp_path / CHECKPOINT
    kept_in(path, {"code": "c1", "input": "i1"}, source)
    back = Tally()
    assert Checkpoint(path, {"code": "c2", "input": "i1"}).restore(back, listed_of(days)) == (
        0,
        "set aside: another code",
    )
    assert not path.exists() and back.folds == [] and back.hits[A] == []
    kept_in(path, {"code": "c1", "input": "i1"}, source)
    assert Checkpoint(path, {"code": "c1", "input": "i2"}).restore(Tally(), listed_of(days)) == (
        0,
        "set aside: another input",
    )
    path.write_text("not json\n")
    assert Checkpoint(path, {"code": "c1", "input": "i1"}).restore(Tally(), listed_of(days)) == (
        0,
        "set aside: another code and input",
    )
    # Another list: the days do not line up from the first.
    kept_in(path, {"code": "c1", "input": "i1"}, source)
    assert Checkpoint(path, {"code": "c1", "input": "i1"}).restore(Tally(), listed_of(days[1:])) == (0, None)


def test_the_keys_follow_the_code_the_settings_the_list_the_calendar_and_the_data(
    data: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The code: the tree models' shared settings neyman's own digest leaves out, and the sample code.
    assert "krx_quant.algorithms.turing" in module._algorithm_code()
    code = module.code_key()
    assert module.code_key() == code and len(code) == 16
    monkeypatch.setattr(module, "samples_code", lambda: "another")
    assert module.code_key() != code
    monkeypatch.undo()
    # The input.
    s = settings_of(data, tmp_path)
    listed = Listed(WHEN, DAYS[0], DAYS[-TRAINING_DAYS], date(2026, 7, 13), 2, 200, [DAYS[3]], [DAYS[2]])
    leaders, calendar = Leaders.of(s), MarketCalendar()

    def key_of(
        s: Settings = s,
        listed: Listed = listed,
        available: list[date] = DAYS,
        calendar: MarketCalendar = calendar,
    ) -> str:
        return module.input_key(s, listed, available, calendar, leaders)

    key = key_of()
    assert key_of() == key
    assert key_of(s=replace(s, fee_rate=s.fee_rate + 0.0001)) != key
    assert key_of(listed=replace(listed, days=[DAYS[3], DAYS[5]])) != key
    assert key_of(available=[d for d in DAYS if d != DAYS[1]]) != key, "the days the windows are taken from"
    # The trading calendar: a day corrected near the listed days.
    assert key_of(calendar=MarketCalendar({date(2026, 5, 29): False})) != key
    assert key_of(calendar=MarketCalendar({date(2026, 1, 2): False})) == key, "one far before is not read"
    # A data file a listed day's samples read, by its content; one after the last listed day is not.
    for day, changes in ((DAYS[1], True), (DAYS[9], False)):
        path = data / bars_key(day, MINUTE_BARS_TR, SYMBOLS[0])
        before = path.read_bytes()
        write_bars(data, day, 200)
        try:
            assert (key_of() != key) is changes, day
        finally:
            path.write_bytes(before)
    assert key_of() == key


# The made-up days walked once whole and once over three runs (each fitting ten tree models on each of
# the three days judged): past the 30 s default on a loaded CI runner.
@pytest.mark.timeout(240)
def test_days_walked_over_several_nights_give_the_verdict_of_one_walk(
    data: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    whole = neyman_backfill(settings_of(data, tmp_path, "whole"), **RULES)  # type: ignore[arg-type]
    assert whole.stage == module.JUDGED and whole.judged is not None and whole.judged.days == 3

    s = settings_of(data, tmp_path, "split")
    report, kept = s.state_dir / module.REPORT, s.state_dir / CHECKPOINT
    caplog.set_level("INFO", logger="krx_quant.neyman_variants")
    night = datetime(2026, 11, 10, 3, 0, tzinfo=KST)
    # The first night: each day an hour, 05:30 its end: two days walked, the third would end at 06:00.
    until, clock = night.replace(hour=5, minute=30), Ticks(night, timedelta(hours=1))
    first = neyman_backfill(s, **RULES, until=until, clock=clock)  # type: ignore[arg-type]
    assert first.stage == module.GOES_ON and first.judged is None
    text = "\n".join(first.lines)
    assert "neyman backfill (#532, by day across nights, #643): 2 of 3 listed days walked" in text
    assert f"1 left ({DAYS[5]}..{DAYS[5]})" in text and "this run: 2 days" in text
    assert "from the first day" in text and f"{GOES_ON_LINE} {DAYS[5]}" in text
    saved = json.loads(report.read_text())
    assert (
        "verdict" not in saved
        and saved["progress"]["walked"] == 2
        and saved["progress"]["next"] == str(DAYS[5])
    )
    assert len(kept.read_text().splitlines()) == 3, "the key and a line a day"
    assert not progress(s).judged
    # The second night has an hour: the day left starts and ends by it.
    caplog.clear()
    until, clock = night + timedelta(hours=1), Ticks(night, timedelta(minutes=50))
    second = neyman_backfill(s, **RULES, until=until, clock=clock)  # type: ignore[arg-type]
    assert second.stage == module.JUDGED and second.judged is not None
    walked = [
        r.getMessage() for r in caplog.records if r.getMessage().startswith("neyman backfill: memory on")
    ]
    assert len(walked) == 1 and str(DAYS[5]) in walked[0], "the days kept are not walked again"
    text = "\n".join(second.lines)
    assert "3 of 3 listed days walked" in text and "0 left (-)" in text
    assert f"after the 2 days kept from earlier runs ({DAYS[2]}..{DAYS[3]})" in text
    assert GOES_ON_LINE not in text
    # The same verdict, the same lines, as one walk gave.
    made = {"made_at"}
    one = {k: v for k, v in whole.judged.as_dict().items() if k not in made}
    split = {k: v for k, v in second.judged.as_dict().items() if k not in made}
    assert split == one
    assert (
        json.loads(report.read_text())["lines"]
        == json.loads((tmp_path / "whole" / module.REPORT).read_text())["lines"]
    )
    assert not kept.exists(), "gone once the verdict is in"
    assert progress(s).judged


@pytest.mark.timeout(240)
@pytest.mark.parametrize("change", ["code", "samples", "calendar"])
def test_days_kept_with_other_code_or_calendar_are_walked_again_from_the_first(
    data: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    s = settings_of(data, tmp_path)
    night = datetime(2026, 11, 10, 3, 0, tzinfo=KST)

    def one_day() -> Backfill:  # each day an hour: the second would end after 04:30
        until, clock = night + timedelta(minutes=90), Ticks(night, timedelta(hours=1))
        return neyman_backfill(s, **RULES, until=until, clock=clock)  # type: ignore[arg-type]

    first = one_day()
    assert first.stage == module.GOES_ON and "1 of 3 listed days walked" in "\n".join(first.lines)
    if change == "code":
        monkeypatch.setattr(module, "code_key", lambda: "another")
    elif change == "samples":  # code shared with the night's samples, outside neyman's package
        monkeypatch.setattr(module, "samples_code", lambda: "another")
    else:  # the stored trading calendar corrected: a day before the made-up days closed
        store = s.state_dir / "market_calendar.json"
        store.write_text(json.dumps({"refreshed_on": None, "open_days": {"2026-05-29": False}}))
    again = one_day()
    text = "\n".join(again.lines)
    assert "1 of 3 listed days walked" in text and "this run: 1 days" in text
    why = "input" if change == "calendar" else "code"
    assert f"from the first day (the days kept were set aside: another {why})" in text
