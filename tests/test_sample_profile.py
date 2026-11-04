"""Where making a recorded day's samples spends its time (#512, ``sample_profile.py``).

Input: eight trading days of backfilled bars (the symbol's, an ETF's, the two futures'), the last one
also a recorded morning with every feed the engine reads, laid out as a data directory. Output: the day
made as a night makes it (the same samples), the seconds and calls by stage, the profiled functions,
the printed lines (no price in them), and the scheduled job that runs it once on a non-trading night.
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import date, datetime, timedelta
from datetime import time as clock_time
from pathlib import Path

import pytest

from krx_quant import cli, sample_profile
from krx_quant.archive import archive
from krx_quant.clock import FakeClock
from krx_quant.config import Settings
from krx_quant.feeds import FOREIGN_ESTIMATE
from krx_quant.feeds.base import KST
from krx_quant.market_calendar import MarketCalendar
from krx_quant.object_store import LocalObjectStore
from krx_quant.replay import recorded_events
from krx_quant.sample_profile import (
    ANSWERS,
    FEATURES,
    READ,
    REPLAY,
    STAGES,
    STORE,
    Reads,
    algorithm_reads,
    make_day,
    night_reads_of,
)
from krx_quant.sample_store import day_key, fingerprint
from krx_quant.scheduled import FAILED, JOBS, OK, Facts, JobRecord, Night, prevalidation_jobs
from krx_quant.scheduled.research import SAMPLE_PROFILE, SAMPLE_PROFILE_ISSUE
from krx_quant.training import training_day
from tests.nightly_doubles import DAYS, FEE, FINISHED, NIGHT, archive_bars
from tests.replay_doubles import SCHEMAS, record
from tests.scheduled_doubles import FRIDAY, scheduling
from tests.scheduled_doubles import SATURDAY as A_SATURDAY
from tests.scheduled_doubles import at as at_night
from tests.test_sample_store import LEADERS, SYMBOLS, archive_futures, rich_morning
from tests.test_scheduled_give_way import PREVALIDATIONS, Takes, gave_way, waiting
from tests.waiting import until

CALENDAR = MarketCalendar(dict.fromkeys(DAYS, True))
END = "09:30:00"  # the recorded morning's end: half an hour of every feed is enough to time


@pytest.fixture(scope="module")
def market(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The days' files, made once for the module, laid out as a data directory."""
    work = tmp_path_factory.mktemp("market")
    root = work / "data"
    for day in DAYS:
        archive_bars(root, day)
        archive_bars(root, day, symbol="133690")
        archive_futures(root, day)
    recorded = work / "recorded"
    events = sorted(rich_morning(NIGHT, END), key=lambda e: e[1]["received_at"])
    record(recorded, events, {**SCHEMAS, FOREIGN_ESTIMATE.tr_id: FOREIGN_ESTIMATE.schema}, day=NIGHT)
    asyncio.run(archive(recorded, LocalObjectStore(root)))
    return root


def settings(data: Path, state: Path) -> Settings:
    return Settings(
        kis_app_key="",
        kis_app_secret="",
        symbols=SYMBOLS,
        etf_symbols=LEADERS.etfs,
        index_codes=LEADERS.indices,
        us_symbols=LEADERS.us,
        fee_rate=FEE,
        data_dir=data,
        state_dir=state,
    )


def files(root: Path) -> dict[str, tuple[int, int]]:
    return {p.relative_to(root).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns) for p in root.rglob("*")}


def made(market: Path, reads: Reads, deadline: float | None = None) -> sample_profile.Made:
    key = day_key(
        market, NIGHT, CALENDAR, SYMBOLS, reads.names, FEE, LEADERS, target_version=3, kinds=reads.kinds
    )
    return make_day(
        market,
        NIGHT,
        CALENDAR,
        SYMBOLS,
        reads,
        FEE,
        LEADERS,
        target_version=3,
        key=key,
        deadline=time.perf_counter() + 600 if deadline is None else deadline,
    )


def originals() -> list[object]:
    return [vars(owner)[name] for owner, name, _ in sample_profile._TIMED]


def prices(market: Path) -> set[str]:
    """The symbol's prices in the recorded morning, as the output could print them."""
    found: set[float] = set()
    for _, row in recorded_events(market, NIGHT, SYMBOLS, LEADERS):
        if row.get("symbol") == SYMBOLS[0]:
            for field in ("price", "bid1", "ask1", "bid_px_1", "ask_px_1"):
                value = row.get(field)
                if isinstance(value, (int, float)) and value > 1000:
                    found.add(float(value))
    assert found, "the morning has prices"
    return {text for v in found for text in (f"{v:.0f}", f"{v:,.0f}", f"{v:g}", str(v))}


def test_a_recorded_day_is_made_as_a_night_makes_it_and_timed_by_stage(market: Path) -> None:
    before = originals()
    night = night_reads_of()

    run = made(market, night)

    assert not run.cut
    assert run.found is not None
    expected = training_day(
        market, NIGHT, CALENDAR, SYMBOLS, night.names, FEE, LEADERS, target_version=3, kinds=night.kinds
    )
    assert expected is not None
    assert fingerprint(run.found) == fingerprint(expected)
    assert run.found.samples and run.found.answers
    # Every stage took time, and the stages make up the run.
    assert all(run.seconds[stage] > 0 for stage in STAGES)
    assert run.total == pytest.approx(sum(run.seconds.values()))
    # Each event was read once, through the timed reader; the other stages were entered by their
    # methods (the answers' windows on every quote and trade of the symbol).
    events = list(recorded_events(market, NIGHT, SYMBOLS, LEADERS))
    assert sum(run.feeds.values()) == len(events)
    assert run.calls[READ] >= len(events)
    assert run.calls[REPLAY] >= len(events)
    assert all(run.calls[stage] > 0 for stage in (FEATURES, ANSWERS, STORE))
    assert run.stored_bytes is not None and run.stored_bytes > 0
    assert originals() == before, "the methods are put back"


def test_an_algorithm_alone_reads_its_own_features_and_its_kind(market: Path) -> None:
    newton = algorithm_reads("newton")
    breiman = algorithm_reads("breiman")
    night = night_reads_of()
    assert newton is not None and breiman is not None
    assert newton.kinds == () and set(newton.names) < set(night.names)
    assert [k.name for k in breiman.kinds] == ["barrier"]
    assert algorithm_reads("nobody") is None

    run = made(market, newton)

    assert run.found is not None
    expected = training_day(market, NIGHT, CALENDAR, SYMBOLS, newton.names, FEE, LEADERS, target_version=3)
    assert expected is not None
    assert fingerprint(run.found) == fingerprint(expected)


def test_the_output_has_each_run_by_stage_and_the_functions_and_no_price(
    market: Path, tmp_path: Path
) -> None:
    lines: list[str] = []
    data_before = files(market)

    code = sample_profile.sample_profile(settings(market, tmp_path / "state"), FINISHED, lines.append, top=10)

    assert code == 0
    text = "\n".join(lines)
    assert lines[0].startswith(f"sample profile (#512): {NIGHT}, the latest finished recorded day")
    # Which runs it made, each with its stages, then the profiled one with its functions.
    titles = ("the night's reads (", "newton alone (", "turing alone (", "the night's reads under cProfile")
    assert all(title in text for title in titles)
    assert text.count("  total ") == 3
    for stage in STAGES:
        assert text.count(f"  {stage} ") == 3, stage
    assert "krx_quant/market_view.py:" in text
    assert "events by feed: " in text
    # No price: the symbol's bids, asks and trades never show.
    shown = set(re.findall(r"[\d][\d,.]*", text))
    assert not shown & prices(market)
    # Read only: the data directory as it was, nothing in the state's sample store.
    assert files(market) == data_before
    assert not (tmp_path / "state" / "training-samples").exists()


def test_the_time_limit_stops_a_run_where_it_is_and_the_rest_do_not_start(
    market: Path, tmp_path: Path
) -> None:
    before = originals()

    run = made(market, night_reads_of(), deadline=time.perf_counter())

    assert run.cut and run.found is None and run.stored_bytes is None
    assert sum(run.feeds.values()) == 0
    assert run.total == pytest.approx(sum(run.seconds.values()))
    assert originals() == before, "the methods are put back"
    assert "(cut: the time limit passed; the figures are up to there)" in sample_profile._run_lines(run)[0]

    lines: list[str] = []
    code = sample_profile.sample_profile(
        settings(market, tmp_path / "state"), FINISHED, lines.append, limit=timedelta(0)
    )

    assert code == 1
    assert [line for line in lines if "not run" in line] == [
        f"{title}: not run, the time limit (0 min) has passed"
        for title in ("the night's reads", "newton alone", "turing alone", "the night's reads under cProfile")
    ]


def test_without_a_recorded_day_or_with_an_unknown_algorithm_nothing_is_made(tmp_path: Path) -> None:
    lines: list[str] = []
    s = settings(tmp_path / "data", tmp_path / "state")

    assert sample_profile.sample_profile(s, FINISHED, lines.append) == 1
    assert sample_profile.sample_profile(s, FINISHED, lines.append, day=NIGHT) == 1
    assert sample_profile.sample_profile(s, FINISHED, lines.append, algorithms=("nobody",)) == 2

    assert lines[0] == (
        "sample profile (#512): no finished day has every symbol's trades recorded; nothing to make"
    )
    assert lines[1] == (
        f"sample profile (#512): {NIGHT} does not have every symbol's trades recorded; nothing to make"
    )
    assert lines[2].startswith("sample profile (#512): unknown algorithm nobody; registered: newton,")


def test_the_command_runs_on_the_settings(
    market: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KRX_DATA_DIR", str(market))
    monkeypatch.setenv("KRX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("KRX_SYMBOLS", ",".join(SYMBOLS))
    monkeypatch.setenv("KRX_ETF_SYMBOLS", ",".join(LEADERS.etfs))
    monkeypatch.setenv("KRX_INDEX_CODES", ",".join(("0001", *LEADERS.indices)))  # KOSPI's is required
    monkeypatch.setenv("KRX_US_SYMBOLS", ",".join(LEADERS.us))

    argv = ["sample-profile", "--day", NIGHT.isoformat(), "--algorithms", "newton", "--top", "5"]
    assert cli.main(argv) == 0

    out = capsys.readouterr().out
    assert out.startswith(f"sample profile (#512): {NIGHT}, the day asked for")
    assert "newton alone (" in out and "turing alone" not in out


# --- the scheduled job --------------------------------------------------------------------------


def night(day: date, trading: bool, record: JobRecord | None = None, recorded: int = 1) -> Night:
    return Night(day, trading, "new", record, {}, Facts(recorded_days=recorded))


def ran(outcome: str, commit: str) -> JobRecord:
    return JobRecord(date(2026, 11, 7), outcome, datetime(2026, 11, 7, 22, 30, tzinfo=KST), commit)


SATURDAY = date(2026, 11, 7)


def test_the_job_runs_once_on_a_night_that_does_not_trade_and_again_after_a_failure_on_other_code() -> None:
    due = SAMPLE_PROFILE.due
    assert due(night(SATURDAY, trading=False))
    assert not due(night(SATURDAY - timedelta(days=1), trading=True)), "not after a trading day"
    assert not due(night(SATURDAY, trading=False, recorded=0)), "not without a recorded day"
    assert not due(night(SATURDAY, trading=False, record=ran(OK, "new")))
    assert not due(night(SATURDAY, trading=False, record=ran(OK, "old")))
    assert not due(night(SATURDAY, trading=False, record=ran(FAILED, "new")))
    assert due(night(SATURDAY, trading=False, record=ran(FAILED, "old")))


def test_the_job_runs_the_command_inside_the_night_and_gives_way_to_a_prevalidation() -> None:
    assert SAMPLE_PROFILE in JOBS
    assert callable(cli._parser().parse_args(list(SAMPLE_PROFILE.command)).run)
    # The command's own limit ends it first, with what it has; the job's ends inside the night.
    assert sample_profile.LIMIT < SAMPLE_PROFILE.timeout <= timedelta(hours=3)
    start = datetime.combine(SATURDAY, clock_time(22, 30))
    assert start + SAMPLE_PROFILE.timeout < datetime.combine(SATURDAY + timedelta(days=1), clock_time(5, 30))
    assert [t.issue for t in SAMPLE_PROFILE.targets] == [SAMPLE_PROFILE_ISSUE] == [512]
    assert "다음 PR" in SAMPLE_PROFILE.targets[0].note
    gives_way = SAMPLE_PROFILE.gives_way_to
    assert gives_way is not None
    assert all(gives_way(job) for job in prevalidation_jobs(["kalman", "neyman"]))
    assert not any(gives_way(job) for job in JOBS if job.command[:1] != ("prevalidate",))


async def test_on_a_night_that_does_not_trade_it_gives_way_to_a_pre_validation_and_runs_after_it(
    tmp_path: Path,
) -> None:
    """A pre-validation (its last start 00:30) and this job (three hours) both due at 22:30 on a
    Saturday: this job, run first to its limit, would carry the pre-validation past its last start, so
    it runs after it (by 02:30). The Friday before trades: it was not due."""
    clock = FakeClock(at_night(FRIDAY, "22:00"))
    runs = Takes(clock, {"prevalidate kalman": timedelta(hours=1, minutes=50)})
    facts = waiting("kalman", simulating=())
    async with scheduling(
        tmp_path, clock.now(), [SAMPLE_PROFILE, *PREVALIDATIONS], runs=runs, facts=facts, clock=clock
    ) as (_, _, told, _):
        clock.set(at_night(FRIDAY, "22:30"))
        await until(lambda: runs.names() == ["prevalidate kalman"])
        clock.set(at_night(A_SATURDAY, "00:20"))
        await until(lambda: runs.ended == ["prevalidate kalman"])
        assert not told.saying("sample-profile-512"), "not due after a trading day"

        clock.set(at_night(A_SATURDAY, "22:30"))  # kalman ran without a verdict: not due again
        await until(lambda: runs.ended == ["prevalidate kalman", "sample-profile"])
        assert runs.started[1] == ("sample-profile", at_night(A_SATURDAY, "22:30"))

    # Another collector, a pre-validation due the same Saturday night: it goes first.
    clock = FakeClock(at_night(A_SATURDAY, "22:00"))
    runs = Takes(clock, {"prevalidate shannon": timedelta(hours=1, minutes=50)})
    async with scheduling(
        tmp_path / "again",
        clock.now(),
        [SAMPLE_PROFILE, *PREVALIDATIONS],
        runs=runs,
        facts=waiting("shannon", simulating=()),
        clock=clock,
    ) as (_, _, told, _):
        clock.set(at_night(A_SATURDAY, "22:30"))
        await until(lambda: runs.names() == ["prevalidate shannon"])
        assert told.saying("sample-profile-512") == [
            gave_way("sample-profile-512", "prevalidate-shannon", A_SATURDAY, "00:30", "3시간", "02:30", 1)
        ]
        clock.set(at_night(A_SATURDAY + timedelta(days=1), "00:20"))
        await until(lambda: runs.ended == ["prevalidate shannon", "sample-profile"])
        assert runs.started[1] == ("sample-profile", at_night(A_SATURDAY + timedelta(days=1), "00:20"))
