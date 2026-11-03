"""The docs drift check (``scripts/check_docs_drift.py``, #495): settings, commands and scheduled
jobs agree with the docs, both ways.

On 2026-11-04 `KRX_CALENDAR_EVERY` and `KRX_CALENDAR_COUNTRIES` were missing from the settings
table, `KRX_CALENDAR_URL` from every doc, and `check-mock-trades` and `probe-live-account` from
README's command table; reviews had caught such drift by hand (#311, #412, #418).
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_docs_drift.py"


def load_check() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_docs_drift", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look the module up by name
    spec.loader.exec_module(module)
    return module


check = load_check()

CONFIG = """\
def from_env(values):
    def number(name, default, kind):
        return kind(values.get(name, default))

    return Settings(alpha=values.get("KRX_ALPHA", "1"), beta=number("KRX_BETA", "2", int))
"""
TRADER = """\
GONE = ("KRX_TRADER_OLD",)


def from_env(values):
    return _number(values, "KRX_TRADER_GAMMA", "3", float)
"""
CLI = """\
import argparse


def main(argv=None):
    parser = argparse.ArgumentParser(prog="krx-quant")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("collect", help="record")
    trade = sub.add_parser("trade", help="trade")
    controls = trade.add_subparsers()
    controls.add_parser("inner")
"""
ENV_EXAMPLE = """\
# The alpha.
KRX_ALPHA=
# The beta, and the gamma the trader reads.
# KRX_BETA=2
# KRX_TRADER_GAMMA=3

# Compose publishes the engine on this port.
# KRX_PORT=8080
"""
OPERATIONS = """\
# 운영 문서

#### 설정 값 전체

엔진은 환경 변수와 `.env`에서 설정을 읽는다.

| 변수 | 기본값 | 뜻과 범위 |
|---|---|---|
| `KRX_ALPHA`, `KRX_BETA` | 1, 2 | 알파와 베타 |

> **바뀜** (2026-11-01, #1): 알파의 기본값 0 → 1. 이유: 시험.
| `KRX_TRADER_GAMMA` | 3 | trader만 읽는 감마 |

compose와 스크립트가 읽는 변수다.

| 변수 | 읽는 곳 | 기본값 | 뜻 |
|---|---|---|---|
| `KRX_PORT` | `compose.yaml`, 엔진(가림만) | 8080 | 엔진 API를 여는 포트 |

### 예약 작업 (#215)

| 이름 | 언제(조건) | 명령 |
|---|---|---|
| `probe-1` | 첫 밤, 한 번 | `probe` |
| `prevalidate-<이름>`: `newton`, 그 뒤 새 알고리즘 | 녹화 10거래일 이상 | `prevalidate <이름>` |

### 다음 절

| 이름 | 뜻 |
|---|---|
| `not-a-job` | 다른 표 |
"""
README = """\
# krx-quant

## 명령

| 명령 | 하는 일 | KIS 키 |
|---|---|---|
| `collect` | 실시간 녹화 | 필요 |
| `trade`, `trade resume` | 자동 매매 | 없어도 된다 |

## 문서
"""
COMPOSE = 'ports:\n  - "${KRX_PORT:-8080}:8080"\n'
JOBS = ["probe-1", "prevalidate-newton"]


def repo(tmp_path: Path, **texts: str) -> Path:
    """A small repository whose code and docs agree, with ``texts`` (by file key) replacing files."""
    files = {
        "config": ("src/krx_quant/config.py", CONFIG),
        "trader": ("src/krx_quant/trader/settings.py", TRADER),
        "cli": ("src/krx_quant/cli.py", CLI),
        "env": (".env.example", ENV_EXAMPLE),
        "operations": ("docs/operations.md", OPERATIONS),
        "readme": ("README.md", README),
        "compose": ("compose.yaml", COMPOSE),
    }
    for key, (path, text) in files.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(texts.get(key, text))
    return tmp_path


def problems(root: Path, jobs: list[str] | None = None) -> list[str]:
    found: list[str] = check.problems(root, JOBS if jobs is None else jobs, undocumented={})
    return found


def test_the_repository_agrees_with_its_docs() -> None:
    assert check.problems(ROOT, check.job_names()) == []


def test_the_script_passes_on_the_repository() -> None:
    done = subprocess.run([sys.executable, str(SCRIPT)], cwd=ROOT, capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr


def test_a_small_repository_whose_code_and_docs_agree_passes(tmp_path: Path) -> None:
    assert problems(repo(tmp_path)) == []


def test_a_setting_read_but_not_documented_is_caught_in_each_doc(tmp_path: Path) -> None:
    config = CONFIG.replace('beta=number("KRX_BETA", "2", int)', 'beta=values.get("KRX_DELTA", "")')
    found = problems(repo(tmp_path, config=config, env=ENV_EXAMPLE.replace("KRX_BETA", "KRX_DELTA")))
    assert found == [
        "docs/operations.md:9: KRX_BETA is in the settings table, but no setting reads it"
        " (src/krx_quant/config.py, src/krx_quant/trader/settings.py): remove it, or leave a"
        " 삭제됨 or 바뀜 note in its section that names it",
        'src/krx_quant/config.py:5: KRX_DELTA is read but not in docs/operations.md "설정 값 전체"',
    ]
    found = problems(repo(tmp_path, config=config))
    assert "src/krx_quant/config.py:5: KRX_DELTA is read but not in .env.example" in found


def test_a_trader_setting_counts_as_read_and_a_gone_one_does_not(tmp_path: Path) -> None:
    # The trader's GONE names are read only to warn that they are set: not settings.
    env = ENV_EXAMPLE + "# KRX_TRADER_OLD=x\n"
    assert problems(repo(tmp_path, env=env)) == [
        ".env.example:9: KRX_TRADER_OLD is set here, but no setting reads it (src/krx_quant/config.py,"
        ' src/krx_quant/trader/settings.py) and docs/operations.md "설정 값 전체" does not list it as'
        " read elsewhere: remove it, or leave a (삭제됨 …) or (바뀜 …) note in its block that names it",
    ]


def test_a_documented_setting_no_code_reads_passes_with_a_note_at_its_place(tmp_path: Path) -> None:
    config = CONFIG.replace('beta=number("KRX_BETA", "2", int)', "beta=2")
    found = problems(repo(tmp_path, config=config))
    assert [p.split(":")[0] for p in found] == [".env.example", "docs/operations.md"]
    assert all("KRX_BETA is" in p for p in found)
    # A note elsewhere does not count: another block of .env.example, another section.
    env = ENV_EXAMPLE + "# (삭제됨 2026-11-04, #2: KRX_BETA. 이유: 시험.)\n"
    operations = OPERATIONS.replace(
        "### 다음 절\n", "### 다음 절\n\n> **삭제됨** (2026-11-04, #2): `KRX_BETA`. 이유: 시험.\n"
    )
    assert len(problems(repo(tmp_path, config=config, env=env, operations=operations))) == 2
    # In its block and its section, over several lines and inside parentheses, it does.
    env = ENV_EXAMPLE.replace(
        "# KRX_BETA=2\n",
        "# (삭제됨 2026-11-04, #2: the beta (and its\n# default, KRX_BETA). 이유: 시험.)\n# KRX_BETA=2\n",
    )
    operations = OPERATIONS.replace(
        "compose와", "> **삭제됨** (2026-11-04, #2): 베타\n> (`KRX_BETA`)를 뺐다. 이유: 시험.\n\ncompose와"
    )
    assert problems(repo(tmp_path, config=config, env=env, operations=operations)) == []


def test_a_note_names_a_setting_only_after_its_marker_and_as_a_whole_name(tmp_path: Path) -> None:
    config = CONFIG.replace('beta=number("KRX_BETA", "2", int)', "beta=2")
    # The name before the marker, or as a part of a longer name, is not the note's; nor is a marker
    # in the middle of a row a note of the row's names.
    env = ENV_EXAMPLE.replace("# KRX_BETA=2\n", "# KRX_BETA=2 (삭제됨 2026-11-04, #2: KRX_BETA_MAX.)\n")
    operations = OPERATIONS.replace("알파와 베타 |", "알파와 베타. **바뀜** (2026-11-04, #2): `KRX_BETA2` |")
    found = problems(repo(tmp_path, config=config, env=env, operations=operations))
    assert [p.split(":")[0] for p in found] == [".env.example", "docs/operations.md"]
    # A note in the row that names it, Korean around it, does; and so does a row that is a note.
    operations = OPERATIONS.replace(
        "알파와 베타 |", "알파와 베타. **삭제됨** (2026-11-04, #2): KRX_BETA는 뺐다 |"
    )
    env = ENV_EXAMPLE.replace("# KRX_BETA=2\n", "# KRX_BETA=2 (바뀜 2026-11-04, #2: KRX_BETA의 뜻.)\n")
    assert problems(repo(tmp_path, config=config, env=env, operations=operations)) == []
    operations = OPERATIONS.replace(
        "| `KRX_ALPHA`, `KRX_BETA` | 1, 2 | 알파와 베타 |",
        "| `KRX_ALPHA` | 1 | 알파 |\n| `KRX_BETA` | **삭제됨** (2026-11-04, #2): 기본값 2. 이유: 시험. | |",
    )
    assert problems(repo(tmp_path, config=config, env=env, operations=operations)) == []
    # Such a row documents none of its names: one the code still reads is not in the table.
    noted_alpha = operations.replace(
        "| `KRX_ALPHA` | 1 | 알파 |", "| `KRX_ALPHA` | **바뀜** (2026-11-04, #2): 1 | |"
    )
    assert problems(repo(tmp_path, config=config, env=env, operations=noted_alpha)) == [
        'src/krx_quant/config.py:5: KRX_ALPHA is read but not in docs/operations.md "설정 값 전체"',
    ]


def test_rows_after_notes_and_blank_lines_still_belong_to_their_table(tmp_path: Path) -> None:
    # KRX_TRADER_GAMMA's row follows a note without a blank line (the table goes on after it).
    trader = TRADER.replace('"KRX_TRADER_GAMMA"', '"KRX_TRADER_EPSILON"')
    found = problems(repo(tmp_path, trader=trader, env=ENV_EXAMPLE.replace("GAMMA", "EPSILON")))
    assert found == [
        "docs/operations.md:12: KRX_TRADER_GAMMA is in the settings table, but no setting reads it"
        " (src/krx_quant/config.py, src/krx_quant/trader/settings.py): remove it, or leave a"
        " 삭제됨 or 바뀜 note in its section that names it",
        "src/krx_quant/trader/settings.py:5: KRX_TRADER_EPSILON is read but not in docs/operations.md"
        ' "설정 값 전체"',
    ]


def test_fenced_code_is_not_read_as_docs(tmp_path: Path) -> None:
    fence = "````text\n#### 설정 값 전체\n\n```\n| `KRX_FAKE` | 1 | 가짜 |\n```\n````\n\n"
    operations = OPERATIONS.replace("compose와", fence + "compose와")
    assert problems(repo(tmp_path, operations=operations)) == []


def test_a_variable_read_elsewhere_is_checked_against_the_files_its_row_names(tmp_path: Path) -> None:
    found = problems(repo(tmp_path, compose="ports: []\n"))
    assert found == [
        "docs/operations.md:18: KRX_PORT: compose.yaml, which its row says reads it, does not name it",
    ]
    operations = OPERATIONS.replace("`compose.yaml`,", "`deploy/gone.sh`, `compose.yaml`,")
    assert problems(repo(tmp_path, operations=operations)) == [
        "docs/operations.md:18: KRX_PORT: deploy/gone.sh, which its row says reads it, is not in the"
        " repository",
    ]
    # Without that row the .env.example line has no reader.
    operations = OPERATIONS.replace(
        "| `KRX_PORT` | `compose.yaml`, 엔진(가림만) | 8080 | 엔진 API를 여는 포트 |\n", ""
    )
    found = problems(repo(tmp_path, operations=operations))
    assert len(found) == 1 and found[0].startswith(".env.example:8: KRX_PORT is set here, but no setting")


def test_allowlisted_settings_pass_and_a_stale_allowlist_entry_fails(tmp_path: Path) -> None:
    config = CONFIG.replace(
        'values.get("KRX_ALPHA", "1")', 'values.get("KRX_ALPHA", "1") + values.get("KRX_COMMIT", "")'
    )
    root = repo(tmp_path, config=config)
    assert check.problems(root, JOBS, undocumented={"KRX_COMMIT": "set by the deploy"}) == []
    assert check.problems(root, JOBS, undocumented={"KRX_COMMIT": "set by the deploy", "KRX_GONE": "x"}) == [
        "scripts/check_docs_drift.py: KRX_GONE is in UNDOCUMENTED, but no setting reads it: remove it",
    ]
    # Every allowlisted name says why.
    assert check.UNDOCUMENTED and all(len(why) > 20 for why in check.UNDOCUMENTED.values())


def test_a_command_missing_from_the_readme_is_caught_and_subcommands_are_not_commands(tmp_path: Path) -> None:
    cli = CLI + '    sub.add_parser(\n        "probe-live-account",\n        help="read",\n    )\n'
    assert problems(repo(tmp_path, cli=cli)) == [
        'src/krx_quant/cli.py:12: command probe-live-account is not in README.md "명령"',
    ]


def test_a_readme_command_the_cli_lacks_passes_only_with_a_note(tmp_path: Path) -> None:
    row = "| `logs-bundle [--days N | --since 때]` | 로그 꾸러미 | 없어도 된다 |\n"
    readme = README.replace("\n## 문서", row + "\n## 문서")
    assert problems(repo(tmp_path, readme=readme)) == [
        "README.md:9: command logs-bundle is in the table, but src/krx_quant/cli.py has no such command:"
        " remove it, or leave a 삭제됨 or 바뀜 note in its section that names it",
    ]
    note = "\n> **삭제됨** (2026-10-23, #360): `logs-bundle`. 이유: 시험.\n"
    assert problems(repo(tmp_path, readme=readme.replace("\n## 문서", note + "\n## 문서"))) == []


def test_a_job_missing_from_the_table_is_caught_and_a_template_row_covers_its_jobs(tmp_path: Path) -> None:
    root = repo(tmp_path)
    assert problems(root, [*JOBS, "prevalidate-gauss", "check-2"]) == [
        'scheduled.JOBS: job check-2 is not in docs/operations.md "예약 작업 (#215)"',
    ]


def test_a_table_job_not_in_jobs_passes_only_with_a_note_in_its_row_or_section(tmp_path: Path) -> None:
    row = "| `old-3` | 첫 밤, 한 번 | `old` |\n"
    operations = OPERATIONS.replace("\n### 다음 절", row + "\n### 다음 절")
    assert problems(repo(tmp_path, operations=operations)) == [
        "docs/operations.md:26: job old-3 is in the table, but not in scheduled.JOBS: remove it, or"
        " leave a 삭제됨 or 바뀜 note in its section that names it",
    ]
    noted = row.replace("첫 밤, 한 번", "**삭제됨** (2026-11-04, #3): `JOBS`에서 뺐다")
    tombstone = OPERATIONS.replace("\n### 다음 절", noted + "\n### 다음 절")
    assert problems(repo(tmp_path, operations=tombstone)) == []
    # A job back in JOBS is not documented by its old row's note.
    assert problems(repo(tmp_path, operations=tombstone), [*JOBS, "old-3"]) == [
        'scheduled.JOBS: job old-3 is not in docs/operations.md "예약 작업 (#215)"',
    ]
    note = "\n> **바뀜** (2026-11-04, #3): old-3은 다른 작업이 됐다.\n"
    assert (
        problems(repo(tmp_path, operations=operations.replace("\n### 다음 절", note + "\n### 다음 절"))) == []
    )


def test_a_template_row_that_matches_no_job_is_caught(tmp_path: Path) -> None:
    assert problems(repo(tmp_path), ["probe-1"]) == [
        "docs/operations.md:25: job prevalidate-<이름> is in the table, but no job in scheduled.JOBS"
        " matches it: remove it, or leave a 삭제됨 or 바뀜 note in its section that names it",
    ]


def test_a_missing_section_or_table_fails_the_check(tmp_path: Path) -> None:
    operations = OPERATIONS.replace("### 예약 작업 (#215)", "### 예약 작업")
    assert problems(repo(tmp_path, operations=operations)) == [
        'docs/operations.md: no section "예약 작업 (#215)" (a heading with that text)',
    ]
    readme = README.replace("| 명령 | 하는 일 | KIS 키 |\n|---|---|---|\n", "")
    assert problems(repo(tmp_path, readme=readme)) == [
        'README.md: section "명령" has no table whose first column is "명령"',
    ]
    cli = CLI.replace('prog="krx-quant"', 'prog="other"')
    assert problems(repo(tmp_path, cli=cli)) == [
        'src/krx_quant/cli.py: no commands found (sub.add_parser on ArgumentParser(prog="krx-quant"))',
    ]


def test_ci_runs_the_check_in_the_lint_job() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    lint = workflow.split("\n  lint:\n", 1)[1].split("\n  pytest:\n", 1)[0]
    assert "run: uv run python scripts/check_docs_drift.py" in lint


def test_no_workflow_runs_an_action_on_node_20() -> None:
    # GitHub warns of Node 20 actions (#486 moved ci.yml and pr-refs.yml off them, #495 the rest).
    node_20 = ("actions/checkout@v4", "aws-actions/configure-aws-credentials@v4", "astral-sh/setup-uv@v6")
    for workflow in (ROOT / ".github" / "workflows").glob("*.yml"):
        text = workflow.read_text()
        assert not [action for action in node_20 if f"uses: {action}" in text], workflow.name
