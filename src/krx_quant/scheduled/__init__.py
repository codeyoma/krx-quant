"""Scheduled jobs (#215): the server runs a ``krx-quant`` command when its time comes and
leaves the result as a comment on GitHub issues, where people and agents read it.

The jobs are listed here (``JOBS``), so what runs on the server, and when, is reviewed in
a PR; the pre-validations (#204) are built from the algorithm registries, one for each
algorithm registered after pre-validation existed, and one for each bar-close algorithm's changed
code (#454). Each has a name (its key in the state file), a condition asked every night
(``Night``: the day, whether it trades, this job's last run, every job's, and what its own reader of
what the engine and the server know read, ``Job.facts``, #651), the command (``krx-quant``
arguments), a time limit, the issues its result goes to (each with the part of the output
it gets), the files it writes that go with the result (``Attachment``) and the labels it
sets.

Every night (trading or not) from ``KRX_JOBS_AT`` (22:30) to 05:30, once the nightly
training is over (``swap.NightTraining``), the jobs due run one at a time, in this list's
order, each in its own process, as the nightly training does; its whole output also goes to
Loki as it comes when it is set (``process_log.py``, ``loki.py``, #358). A job's condition is asked
when its turn comes, so it sees the runs of the jobs before it that night. One whose time
limit would pass 05:30 is not started that night: the 06:00 deploy and the 07:20 restart
stop the collector. A collector started inside the window (a restart at night) runs that
night's jobs at once. A job may give way (``Job.gives_way_to``, #376): the simulated
probations run after a pre-validation that would otherwise miss its last start that night.

A session job (``Job.session``, #403) runs instead on a trading day, from its time in the
continuous session, and ends by ``SESSION_UNTIL``: work only the market's hours can do (an
order on KIS's mock server fills only then). It waits for no training, and its condition is
asked with its own facts read then, which must be cheap beside the live engine (as
``trader.read_mock_session``'s), not the pre-validations', which read every recorded day.

The state (``state/scheduled.json``) keeps each job's last run: its night, outcome (``ok``,
``failed``, ``interrupted``; ``running`` while it runs), exit code, duration, commit and
comments, a pre-validation's verdict apart from its outcome (``passed``, ``refused``: #440, its
``Job.report``), the nights a job gave way since it last ran (#376), and the results not posted yet.
A run the collector's stop cut (a deploy, a restart, a crash) is ``interrupted``: it does not
count as run, and runs again the next night (a session job, the next trading day). A result
is masked (``redaction.py``) and posted on each issue, which then gets ``ready-for-agent`` in
place of ``ready-for-human`` and ``needs-triage``; a job with ``Job.label_if`` (#402: the trader's
nightly report; #570: the recording's quality) changes the labels only on the runs it says call
an agent. Without ``KRX_GITHUB_TOKEN``, or when
GitHub fails, the result waits in the state and is posted at the next night or start. The log
gets a line per run, with the comments' addresses; the trading morning's readiness line counts the
runs no line has told yet, each told it as it ends (``Scheduler``'s ``ran``), and names the failed
ones (#614, ``readiness.py``). The line and the
comment's first line say what came of it (``_what``, #440): 성공 (a pre-validation's 통과), 거부, 실패
(a pre-validation's 예외), 시간 초과 or 신호로 죽음. A refusal's exit code is 1, as an error's is: it is
told apart by the report the run wrote.

> **바뀜** (2026-11-06, #616): each run's line, a deferral's, a run cut by a restart's and a late
> post's went to Telegram → they are logged (``told``); the readiness line counts them. 이유: a line a
> job was most of a night's Telegram, and each result is on its issue (#614).

The package (#500): ``core`` holds what a job is, how its facts are read (``Reads``, from
``Sources``), the state file and the scheduler, and knows no job (#651). The jobs are in a module by
family: ``probes``
(what an outside source's data means), ``research`` (reviews and experiments), ``trader`` (the auto
trader's), ``upkeep`` (the server's: the state backup's restore drill, #576) and ``prevalidations``
(the pre-validations and simulated probations, built from the registries). ``JOBS``, here, gathers
them in the order they run. A new job goes in its family's module and on its line in ``JOBS``
(``tests/test_scheduled_layout.py`` pins the order). A condition that needs what the engine or the
server knows gets it from the job's own reader (``Job.facts``), in the job's module beside it: a
reader the scheduler reads once a night, when the first job that asks it is considered, however many
jobs ask it (the pre-validations share ``read_prevalidation_facts``). Removing a job removes its
reader with it. The names other modules import from ``krx_quant.scheduled`` are exported here
(``__all__``).

> **바뀜** (2026-11-10, #668): every job's condition was asked with one ``Facts``, a field per
> experiment, which ``core.read_facts`` read whole each night → each job reads its own facts
> (``Job.facts``), read only when it is considered. 이유: the core knew every experiment, and adding
> or removing one changed the core, its family's module and the tests together.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any

import aiohttp

from krx_quant.algorithms import ALGORITHMS, DAILY_ALGORITHMS, KIND_ALGORITHMS
from krx_quant.clock import SessionClock
from krx_quant.config import Settings
from krx_quant.engine import PredictionEngine
from krx_quant.github import GitHub
from krx_quant.market_calendar import MarketCalendar
from krx_quant.redaction import secrets_of
from krx_quant.scheduled.core import (
    FAILED,
    GIVE_WAY_NIGHTS,
    INTERRUPTED,
    OK,
    PASSED,
    REFUSED,
    RUNNING,
    SESSION_UNTIL,
    STATE_FILE,
    Attachment,
    Job,
    JobRecord,
    JobState,
    Night,
    Ran,
    Reader,
    Reads,
    Scheduler,
    Sources,
    Target,
    Unposted,
    again_on_new_code,
    first_night,
    in_queue,
    run_command,
    sections,
    session_window,
    window,
)
from krx_quant.scheduled.prevalidations import (
    AGAIN,
    BAR_CLOSE_SIMULATION,
    DAILY_SIMULATION,
    FRESH,
    PREVALIDATION_ISSUE,
    QUEUE_SCREEN,
    PrevalidationFacts,
    Revising,
    _daily_simulation_due,
    daily_prevalidation_jobs,
    prevalidation_job_name,
    prevalidation_jobs,
    prevalidation_order,
    prevalidation_reports,
    prevalidation_tonight,
    prevalidation_turn,
    read_prevalidation_facts,
    read_queue,
    revising_facts,
    revision_job_name,
    revision_jobs,
    revision_turn,
    screened_order,
)
from krx_quant.scheduled.probes import (
    BAR_CLOSE_TIMES,
    CLOSE_RESTART_MEMORY,
    CLOSE_RESTART_MEMORY_AGAIN,
    NIGHT_BOOK_BYTES,
    NIGHT_BOOK_VERDICT,
    RECORDING_QUALITY,
    SAMPLE_KEYS,
    SAMPLE_KEYS_AGAIN,
    SESSION_MEMORY,
    recording_quality_calls,
)
from krx_quant.scheduled.research import (
    CLIMATE_GATE,
    ERROR_HOLDOUT,
    INVERSE_REPLAY,
    NEYMAN_BACKFILL,
    NEYMAN_VARIANTS,
    NIGHTLY_HIT_RATES,
    SIGNAL_REVIEW_FIXED,
    SIGNAL_REVIEW_FIXED_DAYS,
    SIGNAL_REVIEW_FROM,
    SMT_DISCOVERY,
    SMT_HOLDOUT,
    VERDICT_REPLAY,
    BackfillFacts,
    SignalReviewFacts,
)
from krx_quant.scheduled.trader import (
    FILL_NOTICE_COLUMNS,
    LIVE_PROBE,
    TRADER_REPORT,
    MockSessionFacts,
    read_mock_session,
    trader_report_calls,
)
from krx_quant.scheduled.upkeep import RESTORE_DRILL
from krx_quant.swap import NightTraining

# Every name other modules import from ``krx_quant.scheduled`` (mypy's strict mode reads only
# these as exported).
__all__ = [
    "AGAIN",
    "BAR_CLOSE_SIMULATION",
    "BAR_CLOSE_TIMES",
    "CLOSE_RESTART_MEMORY",
    "CLOSE_RESTART_MEMORY_AGAIN",
    "CLIMATE_GATE",
    "DAILY_SIMULATION",
    "ERROR_HOLDOUT",
    "FAILED",
    "FILL_NOTICE_COLUMNS",
    "FRESH",
    "GIVE_WAY_NIGHTS",
    "INTERRUPTED",
    "INVERSE_REPLAY",
    "JOBS",
    "LIVE_PROBE",
    "NEYMAN_BACKFILL",
    "NEYMAN_VARIANTS",
    "NIGHTLY_HIT_RATES",
    "NIGHT_BOOK_BYTES",
    "NIGHT_BOOK_VERDICT",
    "OK",
    "PASSED",
    "PREVALIDATION_ISSUE",
    "QUEUE_SCREEN",
    "RECORDING_QUALITY",
    "REFUSED",
    "RESTORE_DRILL",
    "RUNNING",
    "SESSION_MEMORY",
    "SESSION_UNTIL",
    "SIGNAL_REVIEW_FIXED",
    "SIGNAL_REVIEW_FIXED_DAYS",
    "SIGNAL_REVIEW_FROM",
    "SMT_DISCOVERY",
    "SMT_HOLDOUT",
    "STATE_FILE",
    "TRADER_REPORT",
    "VERDICT_REPLAY",
    "Attachment",
    "BackfillFacts",
    "Job",
    "JobRecord",
    "JobState",
    "MockSessionFacts",
    "Night",
    "PrevalidationFacts",
    "Ran",
    "Reader",
    "Reads",
    "Revising",
    "Scheduler",
    "SignalReviewFacts",
    "Sources",
    "Target",
    "Unposted",
    "_daily_simulation_due",
    "again_on_new_code",
    "daily_prevalidation_jobs",
    "first_night",
    "in_queue",
    "keep_jobs",
    "prevalidation_job_name",
    "prevalidation_jobs",
    "prevalidation_order",
    "prevalidation_reports",
    "prevalidation_tonight",
    "prevalidation_turn",
    "read_mock_session",
    "read_prevalidation_facts",
    "read_queue",
    "recording_quality_calls",
    "revising_facts",
    "revision_job_name",
    "revision_jobs",
    "revision_turn",
    "run_command",
    "screened_order",
    "sections",
    "session_window",
    "trader_report_calls",
    "window",
]

log = logging.getLogger(__name__)


async def told(line: str) -> bool:
    """A scheduled job's line (a run's outcome, a deferral, a run cut by a restart, a result posted
    late): logged, not sent (#614). The trading morning's readiness line counts the runs and names the
    failed ones, and each result is on its issue."""
    log.info("scheduled: %s", line)
    return True


async def keep_jobs(
    settings: Settings,
    session: aiohttp.ClientSession,
    calendar: MarketCalendar,
    clock: SessionClock,
    training: NightTraining,
    engine: PredictionEngine,
    jobs: Sequence[Job[Any]] | None = None,
    ran: Callable[[str, JobRecord], None] | None = None,
) -> None:
    """The collector's scheduled jobs (``KRX_JOBS_AT``), until cancelled; each run's end told to
    ``ran`` (the readiness line, #614)."""
    assert settings.jobs_at is not None
    github = GitHub(session, settings.github_token, settings.github_repo, settings.github_api_url)
    if not github.configured:
        log.warning("KRX_GITHUB_TOKEN is not set: the scheduled jobs keep their results on the server")

    scheduler = Scheduler(
        JOBS if jobs is None else jobs,
        settings.jobs_at,
        calendar,
        JobState.load(settings.state_dir / STATE_FILE),
        github,
        told,
        clock,
        commit=settings.commit,
        secrets=secrets_of(settings),
        training=training,
        sources=Sources(settings, calendar, clock, engine),
        ran=ran,
    )
    await scheduler.run()


# The jobs, run in this order. A new timed piece of server work is a new job in its family's
# module and an entry here (a PR), not an issue asking someone to remember a date (AGENTS.md). A
# one-off job that has run and posted its result leaves the list and its module in a PR too (#505):
# left in, a lost state file would run it again. Its command stays for a run by hand, and a result
# it could not post yet still goes up (the scheduler posts what the state holds, listed or not).


JOBS: tuple[Job[Any], ...] = (
    SAMPLE_KEYS,
    SAMPLE_KEYS_AGAIN,
    TRADER_REPORT,
    RECORDING_QUALITY,
    NIGHT_BOOK_BYTES,
    NIGHT_BOOK_VERDICT,
    BAR_CLOSE_TIMES,
    SESSION_MEMORY,
    CLOSE_RESTART_MEMORY,
    CLOSE_RESTART_MEMORY_AGAIN,
    INVERSE_REPLAY,
    VERDICT_REPLAY,
    SIGNAL_REVIEW_FIXED,
    LIVE_PROBE,
    FILL_NOTICE_COLUMNS,
    ERROR_HOLDOUT,
    CLIMATE_GATE,
    NIGHTLY_HIT_RATES,
    RESTORE_DRILL,
    *daily_prevalidation_jobs(
        [a.name for a in DAILY_ALGORITHMS],
        {a.name: a.features for a in DAILY_ALGORITHMS},
        {a.name: a.kind for a in DAILY_ALGORITHMS},
    ),
    DAILY_SIMULATION,
    BAR_CLOSE_SIMULATION,
    QUEUE_SCREEN,
    *revision_jobs([*(a.name for a in ALGORITHMS), *(a.name for a in KIND_ALGORITHMS)]),
    *prevalidation_jobs([*(a.name for a in ALGORITHMS), *(a.name for a in KIND_ALGORITHMS)]),
    NEYMAN_VARIANTS,
    SMT_DISCOVERY,
    SMT_HOLDOUT,
    NEYMAN_BACKFILL,
)
