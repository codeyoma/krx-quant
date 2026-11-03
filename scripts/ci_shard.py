"""Split the tests into CI shards, and check that each test ran in exactly one of them (#486).

CI ran the whole suite on one runner; by 2026-11-03 a run took 27 minutes, 26 of them in pytest.
It now runs pytest on four runners at once (`.github/workflows/ci.yml`), each going through its
files one test at a time as before. Not pytest-xdist on one runner: tests there would compete for
its CPU, and some measure wall time (the prediction budget in `tests/test_algorithm_contract.py`).

A shard is a set of test files. The files are dealt by the seconds CI last recorded for them
(`scripts/ci_shard_seconds.json`): the longest first, each to the shard with the fewest seconds
so far, the lower shard on a tie. A file the record does not have counts as the median recorded
file. The split depends only on the files and the record, so every run of a commit splits alike.

Each shard writes the tests it collected and the seconds each file took. The `test` job reads
them all with the suite's own collection (`collect`) and fails when a test ran in no shard or in
two. It also writes the merged seconds: copy them over the record when the shards drift apart.

Usage, from anywhere (`--root` defaults to this repository):
  python3 scripts/ci_shard.py files <shard> <count>       a shard's files, one per line
  uv run python scripts/ci_shard.py run <shard> <count> <out.json> [-- <pytest args>]
  uv run python scripts/ci_shard.py collect <out.json>    the tests a plain `pytest` collects
  python3 scripts/ci_shard.py verify <suite.json> <shard.json>... [--seconds <out.json>]

`files` and `verify` need only the standard library, so the `test` job runs them without
installing the project.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pytest

ROOT = Path(__file__).resolve().parents[1]
RECORD = Path("scripts") / "ci_shard_seconds.json"  # under the root
SHOWN = 20  # tests named in one problem; the rest are counted


def test_files(root: Path) -> list[str]:
    """The files pytest collects tests from (its default `python_files`), relative to the root."""
    tests = root / "tests"
    found = {*tests.rglob("test_*.py"), *tests.rglob("*_test.py")}
    return sorted(path.relative_to(root).as_posix() for path in found)


def recorded_seconds(root: Path) -> dict[str, float]:
    record = root / RECORD
    if not record.exists():
        return {}
    return {file: float(seconds) for file, seconds in json.loads(record.read_text()).items()}


def shards(files: list[str], seconds: dict[str, float], count: int) -> list[list[str]]:
    """Deal the files to `count` shards: the longest first, each to the shard with the fewest seconds."""
    known = [seconds[file] for file in files if file in seconds]
    usual = statistics.median(known) if known else 1.0
    cost = {file: seconds.get(file, usual) for file in files}
    dealt: list[list[str]] = [[] for _ in range(count)]
    totals = [0.0] * count
    for file in sorted(cost, key=lambda file: (-cost[file], file)):
        lightest = min(range(count), key=lambda shard: (totals[shard], shard))
        dealt[lightest].append(file)
        totals[lightest] += cost[file]
    return [sorted(shard) for shard in dealt]


class Recorder:
    """A pytest plugin: the tests a run collected, and the seconds each file's tests took."""

    def __init__(self) -> None:
        self.tests: list[str] = []
        self.seconds: dict[str, float] = {}

    def pytest_collection_finish(self, session: pytest.Session) -> None:
        self.tests = [item.nodeid for item in session.items]

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        # Setup, call and teardown each report their own duration.
        file = report.nodeid.split("::", 1)[0]
        self.seconds[file] = self.seconds.get(file, 0.0) + report.duration


def _write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")


def _rounded(seconds: dict[str, float]) -> dict[str, float]:
    return {file: round(value, 2) for file, value in seconds.items()}


def run(root: Path, shard: int, count: int, out: Path, pytest_args: list[str]) -> int:
    import pytest

    files = shards(test_files(root), recorded_seconds(root), count)[shard - 1]
    recorder = Recorder()
    print(f"Shard {shard} of {count}: {len(files)} test files", flush=True)
    try:
        # pytest with no files would run the whole suite.
        code = int(pytest.main([*pytest_args, *files], plugins=[recorder])) if files else 0
    finally:
        _write(
            out,
            {"shard": shard, "count": count, "tests": recorder.tests, "seconds": _rounded(recorder.seconds)},
        )
    return code


def collect(out: Path) -> int:
    import pytest

    recorder = Recorder()
    code = int(pytest.main(["--collect-only", "-qq"], plugins=[recorder]))
    _write(out, {"tests": recorder.tests})
    print(f"The suite has {len(recorder.tests)} tests.")
    return code


def _listed(tests: list[str]) -> str:
    named = ", ".join(tests[:SHOWN])
    return named if len(tests) <= SHOWN else f"{named} and {len(tests) - SHOWN} more"


def _tests(n: int) -> str:
    return f"{n} test" if n == 1 else f"{n} tests"


def problems(suite: list[str], results: list[dict[str, Any]]) -> list[str]:
    """What keeps the shards from having run each of the suite's tests exactly once."""
    found = []
    counts = sorted({int(result["count"]) for result in results})
    numbers = sorted(int(result["shard"]) for result in results)
    if not results:
        found.append("No shard reported")
    elif len(counts) != 1:
        found.append(f"Shards disagree on how many there are: {', '.join(map(str, counts))}")
    elif numbers != list(range(1, counts[0] + 1)):
        reported = ", ".join(map(str, numbers))
        found.append(f"Shards reported: {reported} of {counts[0]} (expected 1 to {counts[0]})")
    ran = Counter(test for result in results for test in result["tests"])
    known = set(suite)
    if missing := [test for test in suite if test not in ran]:
        found.append(f"{_tests(len(missing))} ran in no shard: {_listed(missing)}")
    if twice := sorted(test for test, n in ran.items() if n > 1):
        found.append(f"{_tests(len(twice))} ran in more than one shard: {_listed(twice)}")
    if unknown := sorted(test for test in ran if test not in known):
        verb = "is" if len(unknown) == 1 else "are"
        found.append(f"{_tests(len(unknown))} ran but {verb} not in the suite: {_listed(unknown)}")
    return found


def verify(suite_file: Path, result_files: list[Path], seconds_out: Path | None) -> int:
    suite = json.loads(suite_file.read_text())["tests"]
    results = sorted((json.loads(path.read_text()) for path in result_files), key=lambda r: r["shard"])
    rows = [f"| {r['shard']} | {len(r['tests'])} | {sum(r['seconds'].values()):.0f} |" for r in results]
    table = "\n".join(["| Shard | Tests | Seconds in tests |", "|---|---|---|", *rows])
    print(table)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a") as page:
            page.write(table + "\n")
    if seconds_out is not None:
        _write(seconds_out, _rounded({f: s for r in results for f, s in r["seconds"].items()}))
    found = problems(suite, results)
    for problem in found:
        print(f"::error::{problem}")
    if found:
        return 1
    print(f"{_tests(len(suite))}, each in exactly one of {len(results)} shards.")
    return 0


def main(argv: list[str]) -> int:
    pytest_args: list[str] = []
    if "--" in argv:
        split = argv.index("--")
        argv, pytest_args = argv[:split], argv[split + 1 :]
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--root", type=Path, default=ROOT)
    commands = parser.add_subparsers(dest="command", required=True)
    files = commands.add_parser("files")
    files.add_argument("shard", type=int)
    files.add_argument("count", type=int)
    running = commands.add_parser("run")
    running.add_argument("shard", type=int)
    running.add_argument("count", type=int)
    running.add_argument("out", type=Path)
    collecting = commands.add_parser("collect")
    collecting.add_argument("out", type=Path)
    checking = commands.add_parser("verify")
    checking.add_argument("suite", type=Path)
    checking.add_argument("results", type=Path, nargs="+")
    checking.add_argument("--seconds", type=Path)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if args.command in ("files", "run") and not 1 <= args.shard <= args.count:
        parser.error(f"shard {args.shard} is not one of 1 to {args.count}")
    if args.command == "files":
        print("\n".join(shards(test_files(root), recorded_seconds(root), args.count)[args.shard - 1]))
        return 0
    if args.command == "verify":
        return verify(args.suite, args.results, args.seconds)
    # pytest finds the suite's settings and tests from the root, as a plain `pytest` there does.
    out = args.out.resolve()
    os.chdir(root)
    if args.command == "run":
        return run(root, args.shard, args.count, out, pytest_args)
    return collect(out)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
