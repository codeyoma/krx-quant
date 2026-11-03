"""Scheduled jobs (#215): the server runs a ``krx-quant`` command when its time comes and
leaves the result as a comment on GitHub issues, where people and agents read it.

The jobs are listed here (``JOBS``), so what runs on the server, and when, is reviewed in
a PR; the pre-validations (#204) are built from the algorithm registries, one for each
algorithm registered after pre-validation existed, and one for each bar-close algorithm's changed
code (#454). Each has a name (its key in the state file), a condition asked every night
(``Night``: the day, whether it trades, this job's last run, every job's, and what the engine
knows, ``Facts``), the command (``krx-quant``
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
asked with cheap ``Facts`` of its own (``session_facts``), not the night's, which read every
recorded day beside the live engine.

The state (``state/scheduled.json``) keeps each job's last run: its night, outcome (``ok``,
``failed``, ``interrupted``; ``running`` while it runs), exit code, duration, commit and
comments, a pre-validation's verdict apart from its outcome (``passed``, ``refused``: #440, its
``Job.report``), the nights a job gave way since it last ran (#376), and the results not posted yet.
A run the collector's stop cut (a deploy, a restart, a crash) is ``interrupted``: it does not
count as run, and runs again the next night (a session job, the next trading day). A result
is masked (``redaction.py``) and posted on each issue, which then gets ``ready-for-agent`` in
place of ``ready-for-human`` and ``needs-triage``; a job with ``Job.label_if`` (#402: the trader's
nightly report) changes the labels only on the runs it says call an agent. Without
``KRX_GITHUB_TOKEN``, or when
GitHub fails, the result waits in the state and is posted at the next night or start. Telegram
gets a line per run, with the comments' addresses. The line and the comment's first line say what
came of it (``_what``, #440): 성공 (a pre-validation's 통과), 거부, 실패 (a pre-validation's 예외), 시간
초과 or 신호로 죽음. A refusal's exit code is 1, as an error's is: it is told apart by the report the
run wrote.

The package (#500): ``core`` holds what a job is, the facts its condition asks (``Facts``, read by
``read_facts``), the state file and the scheduler. The jobs are in a module by family: ``probes``
(what an outside source's data means), ``research`` (reviews and experiments), ``trader`` (the auto
trader's) and ``prevalidations`` (the pre-validations and simulated probations, built from the
registries). ``JOBS``, here, gathers them in the order they run. A new job goes in its family's
module and on its line in ``JOBS`` (``tests/test_scheduled_layout.py`` pins the order); a condition
that needs more of what the engine knows adds it to ``Facts`` and ``read_facts``, in ``core``. The
names other modules import from ``krx_quant.scheduled`` are exported here (``__all__``).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from functools import partial

import aiohttp

from krx_quant.algorithms import ALGORITHMS, DAILY_ALGORITHMS, KIND_ALGORITHMS
from krx_quant.clock import SessionClock
from krx_quant.config import Settings
from krx_quant.engine import PredictionEngine
from krx_quant.github import GitHub
from krx_quant.market_calendar import MarketCalendar
from krx_quant.notify import Alerts
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
    SIGNAL_REVIEW_FIXED_DAYS,
    SIGNAL_REVIEW_FROM,
    STATE_FILE,
    Attachment,
    Facts,
    Job,
    JobRecord,
    JobState,
    Night,
    Ran,
    Revising,
    Scheduler,
    Target,
    Unposted,
    first_night,
    prevalidation_reports,
    read_facts,
    revising_facts,
    run_command,
    sections,
    session_facts,
    session_window,
    window,
)
from krx_quant.scheduled.prevalidations import (
    AGAIN,
    BAR_CLOSE_SIMULATION,
    DAILY_SIMULATION,
    FRESH,
    PREVALIDATION_ISSUE,
    _daily_simulation_due,
    daily_prevalidation_jobs,
    prevalidation_job_name,
    prevalidation_jobs,
    prevalidation_order,
    prevalidation_tonight,
    prevalidation_turn,
    revision_job_name,
    revision_jobs,
    revision_turn,
)
from krx_quant.scheduled.probes import (
    CLOSE_PROBE,
    CLOSE_REFRESH_PROBE,
    CLOSING_FLOW_CHECK,
    DAILY_CHECK,
    DIVIDEND_PROBE,
    EXPECTED_OPEN_KRX_PROBE,
    EXPECTED_OPEN_PROBE,
    GAPS_PROBE,
    KRX_ETF_PROBE,
    KRX_NET_ASSETS_CHECK,
    MEMBER_FRAMES_PROBE,
    NAV_PROBE,
    NET_BUYS_PROBE,
    NIGHT_PROBE,
    STRUCTURE_SUPPLY,
    SUPPLY_PROBE,
)
from krx_quant.scheduled.research import (
    CLIMATE_GATE,
    CLIMATE_PRIOR,
    DIAGNOSE,
    ENTRY_DELAY,
    ERROR_BREAKDOWN,
    ERROR_HOLDOUT,
    NEYMAN_VARIANTS,
    NIGHTLY_HIT_RATES,
    POISSON_LINK,
    SIGNAL_REVIEW,
    SIGNAL_REVIEW_AGAIN,
    SIGNAL_REVIEW_AGAIN_FROM,
    SIGNAL_REVIEW_FIXED,
    STRUCTURE_CHECK,
    TURING_PRIOR,
)
from krx_quant.scheduled.trader import (
    COST_MAP_AUDIT,
    FILL_NOTICE_LAYOUT,
    LIVE_PROBE,
    MOCK_CHECK,
    PASSIVE_ENTRY,
    TRADER_REPORT,
    fill_notice_layout_calls,
    trader_report_calls,
)
from krx_quant.swap import NightTraining

# Every name other modules import from ``krx_quant.scheduled`` (mypy's strict mode reads only
# these as exported).
__all__ = [
    "AGAIN",
    "BAR_CLOSE_SIMULATION",
    "CLIMATE_GATE",
    "CLIMATE_PRIOR",
    "CLOSE_PROBE",
    "CLOSE_REFRESH_PROBE",
    "CLOSING_FLOW_CHECK",
    "COST_MAP_AUDIT",
    "DAILY_CHECK",
    "DAILY_SIMULATION",
    "DIAGNOSE",
    "DIVIDEND_PROBE",
    "ENTRY_DELAY",
    "ERROR_BREAKDOWN",
    "ERROR_HOLDOUT",
    "EXPECTED_OPEN_KRX_PROBE",
    "EXPECTED_OPEN_PROBE",
    "FAILED",
    "FILL_NOTICE_LAYOUT",
    "FRESH",
    "GAPS_PROBE",
    "GIVE_WAY_NIGHTS",
    "INTERRUPTED",
    "JOBS",
    "KRX_ETF_PROBE",
    "KRX_NET_ASSETS_CHECK",
    "LIVE_PROBE",
    "MEMBER_FRAMES_PROBE",
    "MOCK_CHECK",
    "NAV_PROBE",
    "NET_BUYS_PROBE",
    "NEYMAN_VARIANTS",
    "NIGHTLY_HIT_RATES",
    "NIGHT_PROBE",
    "OK",
    "PASSED",
    "PASSIVE_ENTRY",
    "POISSON_LINK",
    "PREVALIDATION_ISSUE",
    "REFUSED",
    "RUNNING",
    "SESSION_UNTIL",
    "SIGNAL_REVIEW",
    "SIGNAL_REVIEW_AGAIN",
    "SIGNAL_REVIEW_AGAIN_FROM",
    "SIGNAL_REVIEW_FIXED",
    "SIGNAL_REVIEW_FIXED_DAYS",
    "SIGNAL_REVIEW_FROM",
    "STATE_FILE",
    "STRUCTURE_CHECK",
    "STRUCTURE_SUPPLY",
    "SUPPLY_PROBE",
    "TRADER_REPORT",
    "TURING_PRIOR",
    "Attachment",
    "Facts",
    "Job",
    "JobRecord",
    "JobState",
    "Night",
    "Ran",
    "Revising",
    "Scheduler",
    "Target",
    "Unposted",
    "_daily_simulation_due",
    "daily_prevalidation_jobs",
    "fill_notice_layout_calls",
    "first_night",
    "keep_jobs",
    "prevalidation_job_name",
    "prevalidation_jobs",
    "prevalidation_order",
    "prevalidation_reports",
    "prevalidation_tonight",
    "prevalidation_turn",
    "read_facts",
    "revising_facts",
    "revision_job_name",
    "revision_jobs",
    "revision_turn",
    "run_command",
    "sections",
    "session_facts",
    "session_window",
    "trader_report_calls",
    "window",
]

log = logging.getLogger(__name__)


async def keep_jobs(
    settings: Settings,
    session: aiohttp.ClientSession,
    calendar: MarketCalendar,
    alerts: Alerts,
    clock: SessionClock,
    training: NightTraining,
    engine: PredictionEngine,
    jobs: Sequence[Job] | None = None,
) -> None:
    """The collector's scheduled jobs (``KRX_JOBS_AT``), until cancelled."""
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
        alerts.deliver,
        clock,
        commit=settings.commit,
        secrets=secrets_of(settings),
        training=training,
        facts=partial(read_facts, settings, calendar, clock, engine),
        session_facts=partial(session_facts, settings),
    )
    await scheduler.run()


# The jobs, run in this order. A new timed piece of server work is a new job in its family's
# module and an entry here (a PR), not an issue asking someone to remember a date (AGENTS.md).


JOBS: tuple[Job, ...] = (
    DIAGNOSE,
    SIGNAL_REVIEW,
    SUPPLY_PROBE,
    STRUCTURE_CHECK,
    ENTRY_DELAY,
    NIGHT_PROBE,
    EXPECTED_OPEN_PROBE,
    EXPECTED_OPEN_KRX_PROBE,
    DAILY_CHECK,
    STRUCTURE_SUPPLY,
    NET_BUYS_PROBE,
    CLOSE_PROBE,
    CLOSE_REFRESH_PROBE,
    NAV_PROBE,
    DIVIDEND_PROBE,
    KRX_ETF_PROBE,
    GAPS_PROBE,
    CLOSING_FLOW_CHECK,
    KRX_NET_ASSETS_CHECK,
    MEMBER_FRAMES_PROBE,
    TRADER_REPORT,
    MOCK_CHECK,
    FILL_NOTICE_LAYOUT,
    SIGNAL_REVIEW_AGAIN,
    SIGNAL_REVIEW_FIXED,
    LIVE_PROBE,
    ERROR_BREAKDOWN,
    ERROR_HOLDOUT,
    CLIMATE_GATE,
    PASSIVE_ENTRY,
    COST_MAP_AUDIT,
    NIGHTLY_HIT_RATES,
    *daily_prevalidation_jobs(
        [a.name for a in DAILY_ALGORITHMS],
        {a.name: a.features for a in DAILY_ALGORITHMS},
        {a.name: a.kind for a in DAILY_ALGORITHMS},
    ),
    DAILY_SIMULATION,
    BAR_CLOSE_SIMULATION,
    *revision_jobs([*(a.name for a in ALGORITHMS), *(a.name for a in KIND_ALGORITHMS)]),
    *prevalidation_jobs([*(a.name for a in ALGORITHMS), *(a.name for a in KIND_ALGORITHMS)]),
    CLIMATE_PRIOR,
    POISSON_LINK,
    TURING_PRIOR,
    NEYMAN_VARIANTS,
)
