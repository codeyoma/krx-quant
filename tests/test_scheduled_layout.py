"""The scheduled jobs' package (#500): each family of jobs in its own module, and ``JOBS``
gathering them in the order they run.

The names are pinned as they ran before the split: a PR that adds, removes or moves a job changes
this list with ``JOBS``. The jobs built from the algorithm registries are pinned by where they
come, not by name: a new algorithm needs no entry (AGENTS.md "Timed server work").
"""

from __future__ import annotations

from types import ModuleType

from krx_quant.algorithms import ALGORITHMS, DAILY_ALGORITHMS, KIND_ALGORITHMS
from krx_quant.scheduled import JOBS, Job, daily_prevalidation_jobs, prevalidation_jobs, revision_jobs

# The jobs written by hand, in the list's order, before the daily kinds' pre-validations.
FIRST = (
    "diagnose-203-187",
    "signal-review-206",
    "probe-supply-226",
    "structure-check-270",
    "entry-delay-262",
    "probe-night-264",
    "probe-expected-open-314",
    "probe-expected-open-krx-314",
    "daily-check-264",
    "structure-check-supply-264",
    "probe-net-buys-j-264",
    "probe-close-264",
    "probe-close-refresh-264",
    "probe-nav-305",
    "probe-dividends-310",
    "probe-krx-etf-305",
    "probe-gaps-344",
    "check-closing-flow-305",
    "check-krx-net-assets-305",
    "probe-member-frames-381",
    "trader-report",
    "check-mock-trades-405",
    "fill-notice-layout-403",
    "signal-review-206-1102",
    "signal-review-206-1201",
    "probe-live-account-408",
    "error-breakdown-431",
    "error-holdout-447",
    "climate-gate-449",
    "passive-entry-432",
    "cost-map-audit-438",
    "nightly-hit-rates-437",
)
# Between the daily kinds' pre-validations and the bar-close ones.
SIMULATIONS = ("simulate-daily-298", "simulate-bar-close-298")
# After the bar-close pre-validations: the experiments.
LAST = ("climate-prior-429", "poisson-link-439", "turing-prior-448", "neyman-variants-283")


def _families() -> tuple[ModuleType, ...]:
    from krx_quant.scheduled import prevalidations, probes, research, trader

    return probes, research, trader, prevalidations


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
