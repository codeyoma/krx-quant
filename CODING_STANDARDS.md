# Coding standards

Rules a reviewer applies to every diff, beyond what CI checks. Each rule cites what it came from here. Mechanical rules live in CI instead (`scripts/check_forbidden.py`, ruff, mypy, pytest), so this file holds only judgement calls.

## External data is probed before it is used

Any field from an outside source (KIS, Toss, Yahoo, ECOS, FRED, KRX files) that becomes a model input, an answer or a label needs evidence of what it means. That evidence comes from the server, in a probe job's comment on the issue (a scheduled job, `docs/operations.md` "예약 작업"). Fixtures written before the probe ran are not evidence.

The evidence covers:

- what `0`, an empty string and `null` mean: a real zero, or no figure;
- which date a row is keyed by: trade date, settlement date or publication date;
- which session a price belongs to: the closing auction, the after-market, the integrated or the KRX-only market;
- whether the values are adjusted (splits, rights), and that a ratio never mixes an adjusted and an unadjusted price.

A PR that adds such a field cites the probe comment. If no probe has run yet, the PR adds the probe job and keeps the field out of training, answers and status reviews until its result is in.

Seen here:

- KIS's integrated (`UN`) investor query returns `0` for days before NXT opened, meaning no figure. Training read it as zero for about 84% of Cox's rows (#295, #297).
- The KRX (`J`) day candle's close stopped being the 15:30 closing auction once the after-market started on 2026-09-14 (#317).
- The credit balance matches Toss by settlement date, not trade date. A review based on pre-probe fixtures asked for the opposite; the probe settled it (#258).

## Tests wait for conditions

A test waits for the state it needs before it checks anything: a request to a path arriving, `reading()` turning false, a row being written. That readiness condition is never the order of requests, a fixed request count or wall-clock time: requests made in parallel arrive in any order, and a read asked while the same read is under way joins it. Once the work is done, the test may assert counts and order where they are the behaviour under test (pagination, retries, caching, fan-out).

A test that measures a time budget uses stand-in algorithms with fixed costs, not real algorithms, whose speed depends on the machine. A sleep alone is not a fixed cost: a loaded machine wakes it late, and the engine counts the wait. A CPU budget test takes the `fixed_costs` fixture (`tests/conftest.py`), which times each stand-in by the seconds it spends (`spend` in `tests/algorithm_doubles.py`). Time limits still count real time, so a stand-in meant to go over one sleeps well past it.

A test's time limit is pytest-timeout's, 30 s by default (`pyproject.toml`). A test that passes it ends the whole run with every thread's stack (`timeout_method = "thread"`). The tests after it do not run, so fix the stuck test first. A test that needs longer by design gets its own `@pytest.mark.timeout(N)` and a comment saying why. The method stays `thread`: the `signal` method raises its exception wherever the main thread is, and asyncio drops one raised in a callback, so the test waits forever (`tests/test_timeouts.py`).

Seen here:

- `tst_krx_cost` waited for requests in a fixed order (fork #99).
- `test_algorithms_replay` ran the real Mandelbrot and Breiman ladder against a 0.6 s budget and failed on slower runners (#327).
- `test_the_replay_shares_the_cpu_budget_with_the_live_algorithms_as_live` slept 3 ms and 4 ms against a 5.5 ms budget. On a loaded machine the late wake-ups alone went over it (#327).
- At load averages of 150–600, runs of the replay tests that went past their limits stopped for good at 0% CPU, in 3 of about 70 runs. pytest-timeout's `signal` method had raised its timeout inside an asyncio callback, such as `gather`'s, which wakes the awaiting task, or the one that hands a worker thread's result to the loop. asyncio logged the exception and went on, and the test never woke. Other runs ended in a pytest `INTERNALERROR` while it formatted such a timeout's traceback (#335).
