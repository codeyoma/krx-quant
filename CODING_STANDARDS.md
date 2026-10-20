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

A test that measures a time budget uses stand-in algorithms with fixed costs, not real algorithms, whose speed depends on the machine.

Seen here:

- `tst_krx_cost` waited for requests in a fixed order (fork #99).
- `test_algorithms_replay` ran the real Mandelbrot and Breiman ladder against a 0.6 s budget and failed on slower runners (#327).
