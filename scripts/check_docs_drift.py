"""Fail when the settings, commands or scheduled jobs in the code and in the docs disagree (#495).

Other agents learn the system from docs/, README.md and .env.example (AGENTS.md "Documentation"),
and reviews kept finding a name one side had and the other did not (#311, #412, #418). This
compares three lists, both ways:

- the settings `src/krx_quant/config.py` and `src/krx_quant/trader/settings.py` read (each string
  argument of a call there that looks like an environment variable) with `.env.example` (its
  `NAME=` and `# NAME=` lines) and with the tables of docs/operations.md "설정 값 전체";
- the commands `src/krx_quant/cli.py` adds to the `krx-quant` parser with README.md's command table
  ("명령": the first word of each backticked name in a row's first column);
- `scheduled.JOBS` with docs/operations.md's job table ("예약 작업 (#215)": the first backticked
  name in a row's first column, where `<이름>` stands for any name, as for the pre-validation jobs
  built from the algorithm registries).

A name only the docs have passes when a 삭제됨 or 바뀜 note names it at its place: after the note's
marker (`**삭제됨**`, `**바뀜**`) and in the same block (a table row, a quote, a paragraph) of the
same section of the doc (its heading to the next heading of the same level or higher), or, in
.env.example, inside the parentheses of a `(삭제됨 …)` or `(바뀜 …)` note in its block (lines with
no blank line between). A row whose cell after the first begins with a marker is the note of its
own names (`| `probe-kis-orders-403` | **삭제됨** (2026-11-04, #485): … |`), and documents none.

Variables the engine does not read but compose and the deploy scripts do are in "설정 값 전체"'s
table with a `읽는 곳` column: each file a row names there must name the variable. Settings the
engine reads that the docs leave out by design are in UNDOCUMENTED, with why.

Only the section headings, the tables' first columns and the notes are read; the wording around
them is free, and table rows separated by blank lines or notes still belong to their table. Fenced
code is skipped. A section, table or list that is not found fails the check rather than passing.

Usage: uv run python scripts/check_docs_drift.py
"""

from __future__ import annotations

import ast
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from krx_quant.scheduled import JOBS

ROOT = Path(__file__).resolve().parents[1]
SELF = "scripts/check_docs_drift.py"

SETTINGS_CODE = ("src/krx_quant/config.py", "src/krx_quant/trader/settings.py")
CLI = "src/krx_quant/cli.py"
ENV_EXAMPLE = ".env.example"
OPERATIONS = "docs/operations.md"
README = "README.md"
JOB_LIST = "scheduled.JOBS"

SETTINGS_SECTION = "설정 값 전체"
COMMANDS_SECTION = "명령"
JOBS_SECTION = "예약 작업 (#215)"
# The first column of each list's table, and the settings table's column of the files that read
# a variable outside the engine.
VARIABLE_COLUMN = "변수"
COMMAND_COLUMN = "명령"
JOB_COLUMN = "이름"
READERS_COLUMN = "읽는 곳"

# Settings the engine reads that no doc lists, and why.
UNDOCUMENTED: dict[str, str] = {
    "KRX_COMMIT": "the commit the image is built from: deploy/auto-update.sh hands it to compose.yaml's"
    " build argument, and nobody sets it in .env (docs/operations.md 4장)",
    "AWS_DEFAULT_REGION": "AWS's own variable, read only when AWS_REGION is not set: AWS_REGION's line in"
    " .env.example and its row in the settings table say so",
}

# An environment variable as the code names it: capitals and digits, with an underscore.
SETTING = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+")
# A variable in a settings table: the scripts' have no underscore at times (ZONE, KEEP).
VARIABLE = re.compile(r"[A-Z][A-Z0-9_]*")
# A line of .env.example that sets a variable, commented out or not.
ASSIGNMENT = re.compile(r"#?\s*([A-Z][A-Z0-9_]*)=")
# A .env.example note: "(바뀜 2026-10-31, #425: … 이유: ….)".
ENV_NOTE = re.compile(r"\((?:삭제됨|바뀜) ")
MARKERS = ("**삭제됨**", "**바뀜**")
HEADING = re.compile(r"(#{1,6})\s+(.+?)\s*")
FENCE = re.compile(r"\s*(`{3,}|~{3,})")
SEPARATOR = re.compile(r"\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)*\|?\s*")
CODE_SPAN = re.compile(r"`([^`]+)`")
# `<이름>` in a job's name: any algorithm's.
PLACEHOLDER = re.compile(r"<[^>]+>")
ANY_NAME = "[a-z0-9][a-z0-9-]*"
# A file a row names as reading a variable: `compose.yaml`, `deploy/setup-server.sh`.
PATH = re.compile(r"[\w.-]+(?:/[\w.-]+)*\.\w+")

GONE_HINT = "remove it, or leave a 삭제됨 or 바뀜 note in its section that names it"


@dataclass(frozen=True, order=True)
class Problem:
    path: str
    line: int  # 0: the file or the list as a whole
    text: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.text}" if self.line else f"{self.path}: {self.text}"


@dataclass(frozen=True)
class Line:
    number: int
    text: str


@dataclass(frozen=True)
class Row:
    line: int
    cells: list[str]
    header: list[str]

    def first_names(self) -> list[str]:
        return CODE_SPAN.findall(self.cells[0]) if self.cells else []

    def column(self, name: str) -> str:
        index = self.header.index(name)
        return self.cells[index] if index < len(self.cells) else ""

    @property
    def is_note(self) -> bool:
        """Whether the row is a 삭제됨 or 바뀜 note itself: a cell after its first begins with a
        marker."""
        return any(cell.startswith(MARKERS) for cell in self.cells[1:])


@dataclass(frozen=True)
class Section:
    lines: list[Line]
    notes: list[str]  # its 삭제됨 and 바뀜 notes (notes())

    def rows(self, first_column: str) -> list[Row]:
        """The rows of the tables whose first column is ``first_column``. A row belongs to the table
        whose header is the last above it: blank lines and notes between rows do not end a table."""
        found: list[Row] = []
        header: list[str] | None = None
        for i, line in enumerate(self.lines):
            if not line.text.lstrip().startswith("|") or SEPARATOR.fullmatch(line.text):
                continue
            after = self.lines[i + 1] if i + 1 < len(self.lines) else None
            if after is not None and after.number == line.number + 1 and SEPARATOR.fullmatch(after.text):
                header = cells(line.text)
            elif header is not None and header[0] == first_column:
                found.append(Row(line.number, cells(line.text), header))
        return found

    def noted(self, name: str) -> bool:
        return any(mentions(note, name) for note in self.notes)


def notes(lines: list[Line]) -> list[str]:
    """The text after each 삭제됨 or 바뀜 marker to the end of its block: a table row, a run of
    quoted lines, or a run of other lines with no blank line between."""
    blocks: list[list[str]] = []
    kind: str | None = None
    last = 0
    for line in lines:
        text = line.text.strip()
        this = None if not text else "row" if text.startswith("|") else "quote" if text[0] == ">" else "text"
        if this is not None:
            if this != kind or this == "row" or line.number != last + 1:
                blocks.append([])
            blocks[-1].append(text)
        kind, last = this, line.number
    found = []
    for block in blocks:
        text = " ".join(block)
        starts = [text.index(marker) for marker in MARKERS if marker in text]
        if starts:
            found.append(text[min(starts) :])
    return found


def mentions(text: str, name: str) -> bool:
    """Whether ``text`` has ``name`` as a whole name (Korean may follow it: `KRX_X`는)."""
    return re.search(rf"(?<![A-Za-z0-9_-]){re.escape(name)}(?![A-Za-z0-9_-])", text) is not None


def prose(text: str) -> list[Line]:
    """The lines of a Markdown text outside its fenced code."""
    lines: list[Line] = []
    fence: str | None = None
    for number, line in enumerate(text.splitlines(), 1):
        opening = FENCE.match(line)
        if fence is None:
            if opening:
                fence = opening.group(1)
            else:
                lines.append(Line(number, line))
        elif opening and line.strip() == opening.group(1) and opening.group(1)[0] == fence[0]:
            if len(opening.group(1)) >= len(fence):
                fence = None
    return lines


def section(text: str, title: str) -> Section | None:
    """The lines under the heading ``title``, to the next heading of the same level or higher."""
    lines = prose(text)
    for i, line in enumerate(lines):
        heading = HEADING.fullmatch(line.text)
        if heading and heading.group(2) == title:
            level = len(heading.group(1))
            body = []
            for after in lines[i + 1 :]:
                next_heading = HEADING.fullmatch(after.text)
                if next_heading and len(next_heading.group(1)) <= level:
                    break
                body.append(after)
            return Section(body, notes(body))
    return None


def cells(row: str) -> list[str]:
    """A table row's cells; a `|` in backticks or after a backslash does not split one."""
    text = row.strip()
    text = text[1:] if text.startswith("|") else text
    text = text[:-1] if text.endswith("|") and not text.endswith("\\|") else text
    found: list[str] = []
    cell: list[str] = []
    code = False
    for i, char in enumerate(text):
        if char == "`":
            code = not code
        if char == "|" and not code and (i == 0 or text[i - 1] != "\\"):
            found.append("".join(cell).strip())
            cell = []
        else:
            cell.append(char)
    found.append("".join(cell).strip())
    return found


@dataclass(frozen=True)
class Assignment:
    name: str
    line: int
    notes: list[str]  # the (삭제됨 …) and (바뀜 …) notes of its block


def assignments(text: str) -> list[Assignment]:
    """The variables .env.example sets, each with the notes of its block (lines with no blank line
    between)."""
    blocks: list[list[Line]] = [[]]
    for number, text_line in enumerate(text.splitlines(), 1):
        if text_line.strip():
            blocks[-1].append(Line(number, text_line))
        elif blocks[-1]:
            blocks.append([])
    found = []
    for block in blocks:
        block_notes = env_notes(" ".join(line.text.lstrip("#").strip() for line in block))
        for line in block:
            assignment = ASSIGNMENT.match(line.text)
            if assignment:
                found.append(Assignment(assignment.group(1), line.number, block_notes))
    return found


def env_notes(text: str) -> list[str]:
    """Each `(삭제됨 …)` or `(바뀜 …)` in ``text``, to its closing parenthesis (or the end)."""
    found = []
    for start in ENV_NOTE.finditer(text):
        depth = 0
        end = len(text)
        for i in range(start.start(), len(text)):
            depth += {"(": 1, ")": -1}.get(text[i], 0)
            if depth == 0:
                end = i + 1
                break
        found.append(text[start.start() : end])
    return found


def settings_read(source: str) -> dict[str, int]:
    """The settings ``source`` reads, each at its first line: a string argument of a call that looks
    like an environment variable (``values.get("KRX_SYMBOLS", …)``, ``number("KRX_API_PORT", …)``)."""
    found: dict[str, int] = {}
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            for arg in [*node.args, *(keyword.value for keyword in node.keywords)]:
                if (
                    isinstance(arg, ast.Constant)
                    and isinstance(arg.value, str)
                    and SETTING.fullmatch(arg.value)
                ):
                    found[arg.value] = min(found.get(arg.value, arg.lineno), arg.lineno)
    return found


def commands(source: str) -> dict[str, int]:
    """The commands ``source`` adds to ``ArgumentParser(prog="krx-quant")``'s subparsers, each at its
    line. A command's own subparsers' are not commands."""
    tree = ast.parse(source)
    assigned = [
        (node.targets[0].id, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and isinstance(node.value, ast.Call)
    ]
    parsers = {name for name, call in assigned if _is_krx_quant_parser(call)}
    subparsers = {
        name
        for name, call in assigned
        if isinstance(call.func, ast.Attribute)
        and call.func.attr == "add_subparsers"
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id in parsers
    }
    found: dict[str, int] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_parser"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in subparsers
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            found[node.args[0].value] = node.args[0].lineno
    return found


def _is_krx_quant_parser(call: ast.Call) -> bool:
    func = call.func
    name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
    return name == "ArgumentParser" and any(
        k.arg == "prog" and isinstance(k.value, ast.Constant) and k.value.value == "krx-quant"
        for k in call.keywords
    )


def template(name: str) -> re.Pattern[str] | None:
    """A job name with `<이름>` in it as a pattern, or None for a plain name."""
    parts = PLACEHOLDER.split(name)
    return re.compile(ANY_NAME.join(map(re.escape, parts))) if len(parts) > 1 else None


def problems(root: Path, jobs: Iterable[str], undocumented: Mapping[str, str] = UNDOCUMENTED) -> list[str]:
    """Every disagreement between the code under ``root`` (and ``jobs``) and its docs, in file order."""
    found = [
        *_settings(root, undocumented),
        *_commands(root),
        *_jobs(root, list(jobs)),
    ]
    return [str(problem) for problem in sorted(found)]


def _read(root: Path, path: str) -> str:
    return (root / path).read_text(encoding="utf-8")


def _missing_section(path: str, title: str) -> Problem:
    return Problem(path, 0, f'no section "{title}" (a heading with that text)')


def _missing_table(path: str, title: str, column: str) -> Problem:
    return Problem(path, 0, f'section "{title}" has no table whose first column is "{column}"')


def _settings(root: Path, undocumented: Mapping[str, str]) -> list[Problem]:
    read: dict[str, tuple[str, int]] = {}
    for path in SETTINGS_CODE:
        for name, line in settings_read(_read(root, path)).items():
            read.setdefault(name, (path, line))
    found = [
        Problem(SELF, 0, f"{name} is in UNDOCUMENTED, but no setting reads it: remove it")
        for name in undocumented
        if name not in read
    ]
    if not read:
        return [*found, Problem(SETTINGS_CODE[0], 0, "no settings found (string arguments like KRX_NAME)")]
    body = section(_read(root, OPERATIONS), SETTINGS_SECTION)
    if body is None:
        return [*found, _missing_section(OPERATIONS, SETTINGS_SECTION)]
    rows = body.rows(VARIABLE_COLUMN)
    if not rows:
        return [*found, _missing_table(OPERATIONS, SETTINGS_SECTION, VARIABLE_COLUMN)]
    engine = [row for row in rows if READERS_COLUMN not in row.header]
    elsewhere = [row for row in rows if READERS_COLUMN in row.header]
    tabled = {name for row in rows if not row.is_note for name in _variables(row)}
    read_elsewhere = {name for row in elsewhere for name in _variables(row)}
    env = assignments(_read(root, ENV_EXAMPLE))
    in_env = {assignment.name for assignment in env}
    readers = ", ".join(SETTINGS_CODE)

    for name, (path, line) in read.items():
        if name in undocumented:
            continue
        if name not in in_env:
            found.append(Problem(path, line, f"{name} is read but not in {ENV_EXAMPLE}"))
        if name not in tabled:
            found.append(Problem(path, line, f'{name} is read but not in {OPERATIONS} "{SETTINGS_SECTION}"'))
    for assignment in env:
        name = assignment.name
        if name in read or name in read_elsewhere or any(mentions(note, name) for note in assignment.notes):
            continue
        found.append(
            Problem(
                ENV_EXAMPLE,
                assignment.line,
                f"{name} is set here, but no setting reads it ({readers}) and {OPERATIONS}"
                f' "{SETTINGS_SECTION}" does not list it as read elsewhere: remove it, or leave a'
                " (삭제됨 …) or (바뀜 …) note in its block that names it",
            )
        )
    for row in engine:
        for name in _variables(row):
            if name not in read and name not in read_elsewhere and not (row.is_note or body.noted(name)):
                found.append(
                    Problem(
                        OPERATIONS,
                        row.line,
                        f"{name} is in the settings table, but no setting reads it ({readers}): {GONE_HINT}",
                    )
                )
    for row in elsewhere:
        files = [span for span in CODE_SPAN.findall(row.column(READERS_COLUMN)) if PATH.fullmatch(span)]
        for name in _variables(row):
            if row.is_note or body.noted(name):
                continue
            for path in files:
                if not (root / path).is_file():
                    said = "is not in the repository"
                elif not mentions(_read(root, path), name):
                    said = "does not name it"
                else:
                    continue
                found.append(
                    Problem(OPERATIONS, row.line, f"{name}: {path}, which its row says reads it, {said}")
                )
    return found


def _variables(row: Row) -> list[str]:
    return [name for name in row.first_names() if VARIABLE.fullmatch(name)]


def _commands(root: Path) -> list[Problem]:
    code = commands(_read(root, CLI))
    if not code:
        return [Problem(CLI, 0, 'no commands found (sub.add_parser on ArgumentParser(prog="krx-quant"))')]
    body = section(_read(root, README), COMMANDS_SECTION)
    if body is None:
        return [_missing_section(README, COMMANDS_SECTION)]
    rows = body.rows(COMMAND_COLUMN)
    if not rows:
        return [_missing_table(README, COMMANDS_SECTION, COMMAND_COLUMN)]
    # `trade resume`, `retire <알고리즘>`: the command is the first word.
    tabled = [
        (row, list(dict.fromkeys(span.split()[0] for span in row.first_names() if span.split())))
        for row in rows
    ]
    documented = {name for row, names in tabled if not row.is_note for name in names}
    found = [
        Problem(CLI, line, f'command {name} is not in {README} "{COMMANDS_SECTION}"')
        for name, line in code.items()
        if name not in documented
    ]
    for row, names in tabled:
        for name in names:
            if name not in code and not (row.is_note or body.noted(name)):
                found.append(
                    Problem(
                        README,
                        row.line,
                        f"command {name} is in the table, but {CLI} has no such command: {GONE_HINT}",
                    )
                )
    return found


def _jobs(root: Path, jobs: Sequence[str]) -> list[Problem]:
    if not jobs:
        return [Problem(JOB_LIST, 0, "no jobs")]
    body = section(_read(root, OPERATIONS), JOBS_SECTION)
    if body is None:
        return [_missing_section(OPERATIONS, JOBS_SECTION)]
    rows = body.rows(JOB_COLUMN)
    if not rows:
        return [_missing_table(OPERATIONS, JOBS_SECTION, JOB_COLUMN)]
    # A row's name is its first: a template row lists the algorithms after it.
    tabled = [(row.first_names()[0], row) for row in rows if row.first_names()]
    exact = {name for name, row in tabled if template(name) is None and not row.is_note}
    patterns = [
        pattern for name, row in tabled if (pattern := template(name)) is not None and not row.is_note
    ]
    found = [
        Problem(JOB_LIST, 0, f'job {job} is not in {OPERATIONS} "{JOBS_SECTION}"')
        for job in jobs
        if job not in exact and not any(pattern.fullmatch(job) for pattern in patterns)
    ]
    for name, row in tabled:
        if row.is_note or body.noted(name):
            continue
        pattern = template(name)
        if pattern is None and name not in jobs:
            said = f"not in {JOB_LIST}"
        elif pattern is not None and not any(pattern.fullmatch(job) for job in jobs):
            said = f"no job in {JOB_LIST} matches it"
        else:
            continue
        found.append(Problem(OPERATIONS, row.line, f"job {name} is in the table, but {said}: {GONE_HINT}"))
    return found


def job_names() -> list[str]:
    return [job.name for job in JOBS]


def main() -> int:
    found = problems(ROOT, job_names())
    for problem in found:
        print(problem)
    if found:
        print(
            f"{len(found)} place(s) where the code and the docs disagree: name it in the doc, or leave a"
            ' 삭제됨 or 바뀜 note where it was (AGENTS.md "Documentation")',
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
