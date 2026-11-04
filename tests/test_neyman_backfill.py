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
    NO_CONCLUSION,
    NOT_A_CANDIDATE,
    PREPARED,
    WARMUP,
    Hit,
    Listed,
    Tally,
    candidates,
    first_training_day,
    fix_list,
    judge,
    labelled_count,
    neyman_backfill,
    power,
)
from krx_quant.neyman_variants import DELTA, LEVEL, LIVE_DELTA, A, B, C, D, Fold, Verdict, required_days
from krx_quant.prevalidation import code_digest
from krx_quant.pricing import Direction
from krx_quant.sample_store import STORE_DIR, SampleStore, night_reads, read_day
from krx_quant.scheduled import FAILED, JOBS, NEYMAN_BACKFILL, Facts, JobRecord, Night
from krx_quant.scheduled.research import NORMAL_ORDER, SMT_DISCOVERY
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


def settings_of(data: Path, tmp_path: Path) -> Settings:
    state = tmp_path / "state"
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


def test_the_job_judges_once_after_the_discovery_last_in_the_list() -> None:
    job = NEYMAN_BACKFILL
    assert (job.name, job.command, job.timeout) == (
        "neyman-backfill-532",
        ("neyman-variants", "--backfill"),
        timedelta(hours=5),
    )
    assert [t.issue for t in job.targets] == [455] and "시세 값이 없다" in job.targets[0].note
    assert [a.path for a in job.attach] == ["experiments/neyman-backfill-532.json"]
    assert JOBS[-1] is job, "last: after the pre-validations and the experiments of 11-05"
    assert JOBS.index(job) > max(JOBS.index(NORMAL_ORDER), JOBS.index(SMT_DISCOVERY))
    assert all(
        JOBS.index(j) < JOBS.index(job) for j in JOBS if j.name.startswith(("prevalidate-", "simulate-"))
    )
    assert job.gives_way_to is None and job.session is None
    sunday = date(2026, 11, 8)
    started = datetime(2026, 11, 8, 23, 0, tzinfo=KST)
    ran = JobRecord(sunday, "ok", started, "abc", 0, 9000.0)
    failed = JobRecord(sunday, FAILED, started, "abc", None, 18000.0, timed_out=True)

    def night(found: neyman_variants.Progress, record: JobRecord | None = None, commit: str = "abc") -> Night:
        return Night(sunday + timedelta(days=1), False, commit, record, {}, Facts(neyman_variants=found))

    discovered = neyman_variants.Progress(True, 56, False, 3)
    assert job.due(night(discovered)), "once, trading or not"
    assert not job.due(night(neyman_variants.Progress(read=True))), "not before the discovery"
    assert not job.due(night(neyman_variants.Progress())), "not on a night its progress was not read"
    assert not job.due(night(discovered, ran)), "judged once"
    assert not job.due(night(discovered, failed)), "a failure is not run again on the same code"
    assert job.due(night(discovered, failed, "def")), "but again once a deploy brings other code"
