"""``krx-quant``'s help as it prints (#578): ``krx-quant --help`` and each command's ``--help`` are
kept in ``tests/cli_help/``, a file each, and must print the same.

The files were written before the commands were split by family (#578), so the split is shown to
change no command's name, arguments or help. A command added or changed writes them again with
``uv run python -m tests.test_cli_help``, and the review sees what its help says.
"""

from __future__ import annotations

import argparse
import io
import os
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from krx_quant import cli

HELP = Path(__file__).with_name("cli_help")
TOP = "krx-quant"
# argparse wraps at the terminal's width (``COLUMNS``): the files are at a fixed one, the width it
# takes when there is no terminal.
WIDTH = "80"
WRITE = "uv run python -m tests.test_cli_help"


def printed(argv: list[str]) -> str:
    """What ``krx-quant <argv> --help`` prints."""
    out = io.StringIO()
    with redirect_stdout(out):
        try:
            cli.main([*argv, "--help"])
        except SystemExit as exited:
            assert exited.code == 0, argv
    return out.getvalue()


def helps() -> dict[str, str]:
    """``krx-quant --help`` and each command's ``--help``, by the name of the file that keeps it."""
    [sub] = [action for action in cli._parser()._actions if isinstance(action, argparse._SubParsersAction)]
    found = {f"{TOP}.txt": printed([])}
    for name in sub.choices:
        found[f"{name}.txt"] = printed([name])
    return found


def test_every_help_prints_as_its_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", WIDTH)
    kept = {path.name: path.read_text(encoding="utf-8") for path in sorted(HELP.glob("*.txt"))}

    found = helps()

    assert sorted(found) == sorted(kept), f"a command without a file, or a file without a command: {WRITE}"
    assert [name for name, text in found.items() if text != kept[name]] == [], f"a help changed: {WRITE}"


def write() -> None:
    """Write every help's file again, and remove those of commands that are gone."""
    os.environ["COLUMNS"] = WIDTH
    found = helps()
    HELP.mkdir(exist_ok=True)
    for path in HELP.glob("*.txt"):
        if path.name not in found:
            path.unlink()
    for name, text in found.items():
        (HELP / name).write_text(text, encoding="utf-8")


if __name__ == "__main__":
    write()
