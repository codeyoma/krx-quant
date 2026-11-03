"""CI's test shards (``scripts/ci_shard.py``, #486): every test runs in exactly one shard.

CI ran the whole suite on one runner and took 27 minutes by 2026-11-03, 26 of them in pytest.
"""

from __future__ import annotations

import importlib.util
import json
import random
import subprocess
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ci_shard.py"


def load_shards() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ci_shard", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ci_shard = load_shards()


def test_each_test_file_is_in_exactly_one_of_the_four_shards_and_every_split_is_the_same() -> None:
    files = ci_shard.test_files(ROOT)
    seconds = ci_shard.recorded_seconds(ROOT)
    assert "tests/test_ci_shard.py" in files and "tests/conftest.py" not in files
    split = ci_shard.shards(files, seconds, 4)
    dealt = [file for shard in split for file in shard]
    assert sorted(dealt) == sorted(files) and len(dealt) == len(set(dealt))
    shuffled = list(files)
    random.Random(486).shuffle(shuffled)
    assert ci_shard.shards(shuffled, seconds, 4) == split


def test_the_longest_file_goes_first_and_each_file_to_the_shard_with_the_fewest_seconds() -> None:
    seconds = {"tests/test_a.py": 10.0, "tests/test_b.py": 6.0, "tests/test_c.py": 5.0}
    seconds |= {"tests/test_d.py": 4.0, "tests/test_e.py": 1.0}
    # a → 1 (10, 0), b → 2 (10, 6), c → 2 (10, 11), d → 1 (14, 11), e → 2 (14, 12).
    assert ci_shard.shards(sorted(seconds), seconds, 2) == [
        ["tests/test_a.py", "tests/test_d.py"],
        ["tests/test_b.py", "tests/test_c.py", "tests/test_e.py"],
    ]
    # A tie goes to the lower shard, and equal files are dealt in name order.
    even = {f"tests/test_{name}.py": 1.0 for name in "abc"}
    assert ci_shard.shards(sorted(even), even, 2) == [
        ["tests/test_a.py", "tests/test_c.py"],
        ["tests/test_b.py"],
    ]


def test_a_file_without_a_record_counts_as_the_median_recorded_file() -> None:
    seconds = {"tests/test_a.py": 9.0, "tests/test_b.py": 4.0, "tests/test_c.py": 1.0}
    seconds["tests/test_gone.py"] = 100.0  # deleted since the record: not counted
    files = ["tests/test_a.py", "tests/test_b.py", "tests/test_c.py", "tests/test_new.py"]
    # The new file counts 4 s, after b by name: a → 1 (9, 0), b → 2 (9, 4), new → 2 (9, 8), c → 2.
    assert ci_shard.shards(files, seconds, 2) == [
        ["tests/test_a.py"],
        ["tests/test_b.py", "tests/test_c.py", "tests/test_new.py"],
    ]
    assert ci_shard.shards(files, {}, 2) == [
        ["tests/test_a.py", "tests/test_c.py"],
        ["tests/test_b.py", "tests/test_new.py"],
    ], "without a record every file counts the same"


def result(shard: int, tests: list[str], count: int = 3) -> dict[str, object]:
    return {"shard": shard, "count": count, "tests": tests, "seconds": {}}


def test_a_test_that_ran_in_no_shard_or_in_two_fails_the_check() -> None:
    suite = ["tests/test_a.py::test_x", "tests/test_a.py::test_y", "tests/test_b.py::test_z"]
    assert ci_shard.problems(suite, [result(1, suite[:2]), result(2, suite[2:]), result(3, [])]) == []

    found = ci_shard.problems(suite, [result(1, suite[:1]), result(2, suite[:1]), result(3, [])])
    assert any("ran in no shard" in p and "tests/test_a.py::test_y" in p for p in found)
    assert any("ran in no shard" in p and "tests/test_b.py::test_z" in p for p in found)
    assert any("ran in more than one shard" in p and "tests/test_a.py::test_x" in p for p in found)

    found = ci_shard.problems(suite[:2], [result(1, suite[:2]), result(2, suite[2:]), result(3, [])])
    assert found == ["1 test ran but is not in the suite: tests/test_b.py::test_z"]


def test_a_shard_that_did_not_report_fails_the_check() -> None:
    suite = ["tests/test_a.py::test_x"]
    assert ci_shard.problems(suite, [result(1, suite), result(3, [])]) == [
        "Shards reported: 1, 3 of 3 (expected 1 to 3)"
    ]
    assert ci_shard.problems(suite, [result(1, suite, count=2), result(2, [], count=3)]) != []


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def shard_script(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_the_shards_run_collect_and_check_a_small_suite_end_to_end(tmp_path: Path) -> None:
    write(tmp_path / "pyproject.toml", '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
    write(tmp_path / "tests" / "test_a.py", "def test_x():\n    pass\n\n\ndef test_y():\n    pass\n")
    write(
        tmp_path / "tests" / "test_b.py",
        "import pytest\n\n\n@pytest.mark.parametrize('n', [1, 2])\ndef test_z(n):\n    pass\n",
    )
    write(tmp_path / "tests" / "deep" / "test_c.py", "def test_w():\n    pass\n")
    out = tmp_path / "out"
    pytest_args = ["--", "-q", "-p", "no:cacheprovider"]

    collected = shard_script(tmp_path, "collect", str(out / "suite.json"))
    assert collected.returncode == 0, collected.stdout + collected.stderr
    suite = json.loads((out / "suite.json").read_text())["tests"]
    assert len(suite) == 5 and "tests/test_b.py::test_z[2]" in suite

    for shard in ("1", "2"):
        ran = shard_script(tmp_path, "run", shard, "2", str(out / f"shard-{shard}.json"), *pytest_args)
        assert ran.returncode == 0, ran.stdout + ran.stderr
    files = shard_script(tmp_path, "files", "1", "2")
    shard_1 = json.loads((out / "shard-1.json").read_text())
    assert {test.split("::")[0] for test in shard_1["tests"]} == set(files.stdout.split())
    assert set(shard_1["seconds"]) == set(files.stdout.split())

    seconds = out / "seconds.json"
    checked = shard_script(
        tmp_path, "verify", str(out / "suite.json"), *(str(out / f"shard-{n}.json") for n in "12"),
        "--seconds", str(seconds),
    )  # fmt: skip
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert "5 tests, each in exactly one of 2 shards" in checked.stdout
    assert set(json.loads(seconds.read_text())) == {
        "tests/test_a.py", "tests/test_b.py", "tests/deep/test_c.py",
    }  # fmt: skip

    alone = shard_script(tmp_path, "verify", str(out / "suite.json"), str(out / "shard-1.json"))
    assert alone.returncode == 1 and "Shards reported: 1 of 2" in alone.stdout


def test_a_failing_test_fails_its_shard_but_the_shard_still_reports(tmp_path: Path) -> None:
    write(tmp_path / "pyproject.toml", '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
    write(tmp_path / "tests" / "test_a.py", "def test_x():\n    assert False\n")
    out = tmp_path / "shard-1.json"
    ran = shard_script(tmp_path, "run", "1", "1", str(out), "--", "-q", "-p", "no:cacheprovider")
    assert ran.returncode == 1
    assert json.loads(out.read_text())["tests"] == ["tests/test_a.py::test_x"]


def test_ci_runs_four_shards_behind_one_check_and_cancels_only_superseded_pr_runs() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "shard: [1, 2, 3, 4]" in workflow
    assert "scripts/ci_shard.py run ${{ matrix.shard }} ${{ strategy.job-total }}" in workflow
    assert "scripts/ci_shard.py collect" in workflow and "scripts/ci_shard.py verify" in workflow
    # The check a merge waits for keeps its name, whatever the number of shards.
    test_job = workflow.split("\n  test:\n", 1)[1]
    assert "needs: [lint, pytest]" in test_job and "if: ${{ !cancelled() }}" in test_job
    # A push to main is never cancelled: its group is the run's own.
    assert "group: ci-${{ github.event.pull_request.number || github.run_id }}" in workflow
    assert "cancel-in-progress: ${{ github.event_name == 'pull_request' }}" in workflow
