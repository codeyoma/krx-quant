"""``krx-quant``'s commands each carry the handler that runs them (#499).

``main`` used to pick among the commands in one ``elif``. A command whose branch was left out did
nothing and exited 0, and a scheduled job that called it was reported as done.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from pathlib import Path

import pytest

from krx_quant import cli
from krx_quant.cli import trader
from krx_quant.process_log import KRX_QUANT
from krx_quant.scheduled import JOBS
from krx_quant.swap import TRAIN_COMMAND
from tests.test_docs_drift import ROOT, check


def commands(
    parser: argparse.ArgumentParser, words: tuple[str, ...] = ()
) -> Iterator[tuple[tuple[str, ...], argparse.ArgumentParser]]:
    """Each command under ``parser`` and each of its own subcommands, by its words."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, child in action.choices.items():
                yield (*words, name), child
                yield from commands(child, (*words, name))


def test_every_command_and_subcommand_carries_its_handler() -> None:
    found = dict(commands(cli._parser()))

    assert [
        " ".join(words) for words, parser in found.items() if not callable(parser.get_default("run"))
    ] == []
    # The docs drift check (#495) reads the same commands from the families' source (#578).
    assert {words[0] for words in found} == set(check.cli_commands(ROOT))


@pytest.mark.parametrize(
    ("argv", "ran"),
    [
        (["trade"], ("trade",)),
        (["trade", "resume"], ("control", "resume")),
        (["trade", "rearm"], ("control", "rearm")),
    ],
)
def test_trade_and_its_controls_reach_their_handlers(
    argv: list[str], ran: tuple[str, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, ...]] = []

    async def trading() -> int:
        seen.append(("trade",))
        return 0

    def control(action: str) -> int:
        seen.append(("control", action))
        return 3

    monkeypatch.setattr(trader, "_trade", trading)
    monkeypatch.setattr(trader, "_trade_control", control)

    code = cli._command(cli._parser().parse_args(argv))

    assert seen == [ran]
    assert code == (0 if ran == ("trade",) else 3)


def test_a_command_without_a_handler_does_not_exit_0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    built = cli._parser

    def with_a_bare_command() -> argparse.ArgumentParser:
        parser = built()
        [sub] = [action for action in parser._actions if isinstance(action, argparse._SubParsersAction)]
        sub.add_parser("bare", help="a command nobody gave a handler")
        return parser

    monkeypatch.setattr(cli, "_parser", with_a_bare_command)
    monkeypatch.chdir(tmp_path)  # no .env

    assert cli.main(["bare"]) == 1
    assert "bare has no handler" in capsys.readouterr().err


def test_every_scheduled_job_and_the_nightly_training_parse_to_a_handler() -> None:
    parser = cli._parser()

    for argv in [*(job.command for job in JOBS), TRAIN_COMMAND[len(KRX_QUANT) :]]:
        assert callable(parser.parse_args(list(argv)).run), argv


def test_arguments_added_as_the_command_parses_parse_and_fail_as_the_others(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # #578: their defaults come from the module the command runs, read once it is parsed.
    from krx_quant.prevalidation import PREVALIDATION_DAYS

    args = cli._parser().parse_args(["prevalidate", "kalman"])

    assert (args.algorithm, args.days, args.revision) == ("kalman", PREVALIDATION_DAYS, False)
    for wrong in (["prevalidate"], ["simulate", "nope"], ["nightly-hit-rates", "--nights", "0"]):
        with pytest.raises(SystemExit) as exited:
            cli.main(wrong)
        assert exited.value.code == 2, wrong
        assert capsys.readouterr().err.startswith(f"usage: krx-quant {wrong[0]} "), wrong


def test_each_command_answers_help_and_a_wrong_one_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    names = [words[0] for words, _ in commands(cli._parser()) if len(words) == 1]

    for name in names:
        with pytest.raises(SystemExit) as exited:
            cli.main([name, "--help"])
        assert exited.value.code == 0, name
        assert capsys.readouterr().out.startswith(f"usage: krx-quant {name} "), name
    for wrong in (["nope"], ["fit"], ["trade", "pause"], ["fit", "newton", "--days", "0"]):
        with pytest.raises(SystemExit) as exited:
            cli.main(wrong)
        assert exited.value.code == 2, wrong
