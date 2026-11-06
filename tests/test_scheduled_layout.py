"""The scheduled jobs' package (#500): each family of jobs in its own module, and ``JOBS``
gathering them in the order they run.

The names are pinned in the order they ran before the split, less the one-off jobs that have done
their work and left (#505): a PR that adds, removes or moves a job changes this list with ``JOBS``.
The jobs built from the algorithm registries are pinned by where they come, not by name: a new
algorithm needs no entry (AGENTS.md "Timed server work").
"""

from __future__ import annotations

import importlib.util
from types import ModuleType

import pytest

from krx_quant import cli
from krx_quant.algorithms import ALGORITHMS, DAILY_ALGORITHMS, KIND_ALGORITHMS
from krx_quant.scheduled import JOBS, Job, daily_prevalidation_jobs, prevalidation_jobs, revision_jobs

# The jobs written by hand, in the list's order, before the daily kinds' pre-validations: the probe of
# the sample store's keys (#565) first, a few minutes after the night's training.
FIRST = (
    "probe-sample-keys-565",
    "probe-sample-keys-565-again",
    "trader-report",
    "recording-quality-570",
    "night-book-bytes-605",
    "night-book-verdict-605",
    "bar-close-times-589",
    "session-memory-623",
    "probe-inverse-book-606",
    "probe-market-halt-625",
    "signal-review-206-1201",
    "probe-live-account-408",
    "error-holdout-447",
    "climate-gate-449",
    "nightly-hit-rates-437",
    "restore-drill-577",
)
# Between the daily kinds' pre-validations and the bar-close ones: the simulations, then the measure of
# where making a day's samples spends its time (#512), which gives way to a pre-validation as they do,
# then the queue screen (#599), which orders the bar-close ones and gives way to them too.
SIMULATIONS = ("simulate-daily-298", "simulate-bar-close-298", "sample-profile-512", "screen-queue-599")
# After the bar-close pre-validations: the experiments.
LAST = (
    "neyman-variants-283",
    "normal-order-529",
    "smt-discovery-519",
    "smt-holdout-519",
    "neyman-backfill-532",
)
# The one-off jobs that ran, posted their results and left the list (#505), each with the command it
# ran, which stays for a run by hand (as ``probe-kis-orders-403``'s did, #484), but for those in
# ``REMOVED``.
RETIRED = {
    "diagnose-203-187": ("diagnose", "--days", "20"),
    "signal-review-206": ("signal-review", "--days", "20"),
    "probe-supply-226": ("probe-supply",),
    "structure-check-270": ("structure-check",),
    "entry-delay-262": ("diagnose", "--days", "20"),
    "probe-night-264": ("probe-night",),
    "probe-expected-open-314": ("probe-expected-open",),
    "probe-expected-open-krx-314": ("probe-expected-open",),
    "daily-check-264": ("daily-check", "--days", "1"),
    "structure-check-supply-264": ("structure-check",),
    "probe-net-buys-j-264": ("probe-net-buys",),
    "probe-close-264": ("probe-close",),
    "probe-close-refresh-264": ("probe-close",),
    "probe-nav-305": ("probe-nav",),
    "probe-dividends-310": ("probe-dividends",),
    "probe-krx-etf-305": ("probe-krx-etf",),
    "probe-gaps-344": ("probe-gaps",),
    "check-closing-flow-305": ("check-closing-flow",),
    "check-krx-net-assets-305": ("check-krx-net-assets",),
    "probe-member-frames-381": ("probe-member-frames",),
    "check-mock-trades-405": ("check-mock-trades",),
    "signal-review-206-1102": ("signal-review", "--days", "20"),
    "error-breakdown-431": ("error-breakdown", "--days", "20", "--algorithm", "turing"),
    "passive-entry-432": ("measure-passive-entry",),
    "cost-map-audit-438": ("cost-map-audit",),
    "climate-prior-429": ("climate-prior",),
    "poisson-link-439": ("poisson-link",),
    "turing-prior-448": ("turing-prior",),
    # #580: it joined a buy, a sell and a cancel on 2026-11-04 and #531 named the 23 columns.
    "fill-notice-layout-403": ("fill-notice-layout",),
}
# The commands removed with their modules once their questions were settled: the probes (#563) and
# the concluded experiments (#559). The user decided both on 2026-11-05, over #505's "the commands
# stay". ``probe-candles`` ran by hand only (#111).
REMOVED = (
    "probe-candles",
    "probe-close",
    "probe-dividends",
    "probe-expected-open",
    "probe-member-frames",
    "probe-net-buys",
    "measure-passive-entry",
    "cost-map-audit",
    "poisson-link",
    "turing-prior",
)
# The concluded experiments' modules, removed with their commands (#559).
REMOVED_MODULES = (
    "krx_quant.trader.passive",
    "krx_quant.cost_map_audit",
    "krx_quant.poisson_link",
    "krx_quant.turing_prior",
)


def _families() -> tuple[ModuleType, ...]:
    from krx_quant.scheduled import prevalidations, probes, research, trader, upkeep

    return probes, research, trader, upkeep, prevalidations


def test_the_jobs_run_in_the_order_they_ran_before_the_split() -> None:
    bar_close = [*(a.name for a in ALGORITHMS), *(a.name for a in KIND_ALGORITHMS)]
    daily = [j.name for j in daily_prevalidation_jobs([a.name for a in DAILY_ALGORITHMS])]
    revisions = [j.name for j in revision_jobs(bar_close)]
    first = [j.name for j in prevalidation_jobs(bar_close)]
    assert daily and revisions and first, "the registries build jobs"
    assert all(n.startswith("prevalidate-") for n in daily + first)
    assert all(n.startswith("prevalidate-revision-") for n in revisions)
    assert [j.name for j in JOBS] == [*FIRST, *daily, *SIMULATIONS, *revisions, *first, *LAST]


def test_every_job_a_family_defines_is_listed_once() -> None:
    """A job left out of ``JOBS`` would never run: each one a family module defines is in it, and
    defined in one family only."""
    defined = [
        (family.__name__, value)
        for family in _families()
        for value in vars(family).values()
        if isinstance(value, Job)
    ]
    assert defined, "the families define jobs"
    assert [job.name for _, job in defined if job not in JOBS] == []
    names = [job.name for _, job in defined]
    assert len(names) == len(set(names)), "each in one family"
    built = {j.name for j in JOBS if j.command[:1] == ("prevalidate",) and j.name not in names}
    assert {j.name for j in JOBS} == set(names) | built, "the rest are built from the registries"


def test_the_retired_jobs_are_gone_and_their_commands_stay() -> None:
    """Left in, a lost state file would run them again on closed issues; by hand, each still runs,
    unless its command was removed since."""
    assert len(RETIRED) == 29
    assert sorted(set(RETIRED) & {j.name for j in JOBS}) == []
    parser = cli._parser()
    for name, argv in RETIRED.items():
        if argv[0] not in REMOVED:
            assert callable(parser.parse_args(list(argv)).run), name


def test_the_removed_probes_are_no_command_and_no_job_runs_one() -> None:
    parser = cli._parser()
    for command in REMOVED:
        with pytest.raises(SystemExit) as exited:
            parser.parse_args([command])
        assert exited.value.code == 2, command
    assert [j.name for j in JOBS if j.command[0] in REMOVED] == []


def test_the_concluded_experiments_modules_are_gone() -> None:
    """#559: removed with their commands (``REMOVED``), nothing is left to import."""
    assert [m for m in REMOVED_MODULES if importlib.util.find_spec(m) is not None] == []
