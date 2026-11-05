"""Command-line entry point: ``krx-quant <command>``.

The commands are in a module by family (#578): ``engine`` (the collector, and the algorithms'
models and standing), ``trader`` (the auto trader's), ``data`` (KIS's limits, the backfill, the
upload, a day's recording quality, the macro indicators), ``probes`` (what an outside source's data
means), ``research`` (reviews and experiments) and ``tools`` (the server's state, the notices, the
fake KIS and the demo engine). Each family's ``add`` adds its commands to ``krx-quant``'s parser
(``_parser``), in the order ``FAMILIES`` gives, each with the handler that runs it as its ``run``
default (#499): it takes the parsed arguments and returns the exit code. A new command goes in its
family's ``add``, with a row in README's command table, and its help in ``tests/cli_help/``
(``uv run python -m tests.test_cli_help``).

A command loads only what it runs (#578): a family imports at its top only what every command
loads anyway (the settings, ``KST``), and each handler imports the rest when it runs. A command
whose arguments' defaults come from such a module adds them when it is parsed
(``core.Command``'s ``arguments``). Before, this module imported every command's modules for
each one: ``krx-quant trade`` loaded LightGBM and SciPy, which the trader never uses
(``tests/test_cli_imports.py``).
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from krx_quant import loki
from krx_quant.cli import data, engine, probes, research, tools, trader
from krx_quant.cli.core import Command, Run
from krx_quant.config import SettingsError, lenient_settings, read_values
from krx_quant.kis import KisError
from krx_quant.process_log import FORMAT as LOG_FORMAT
from krx_quant.process_log import open_process_log
from krx_quant.redaction import secrets_of

# The families, in the order ``krx-quant --help`` lists their commands.
FAMILIES = (engine, trader, data, probes, research, tools)


def _parser() -> argparse.ArgumentParser:
    """``krx-quant``'s parser: each family's commands (``FAMILIES``), each command's parser with its
    handler as its ``run`` default (#499)."""
    parser = argparse.ArgumentParser(prog="krx-quant")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=Command)
    for family in FAMILIES:
        family.add(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    # This process's lines also go to Loki when it is set (#358), masked, between a start line and
    # one with its exit code.
    values = read_values(None, Path(".env"))
    files = open_process_log(
        args.command, sys.argv[1:] if argv is None else argv, values, _log_secrets(values)
    )
    code, why = 1, None
    try:
        code, why = _command(args), None
    except SettingsError as e:
        print(f"configuration error: {e}", file=sys.stderr)
        code, why = 2, f"configuration error: {e}"
    except KisError as e:
        # Non-zero so a supervisor (Docker restart policy) restarts the collector.
        print(f"KIS error: {e}", file=sys.stderr)
        code, why = 1, f"KIS error: {e}"
    except KeyboardInterrupt:
        code, why = 0, "interrupted"
    except BaseException as e:  # its traceback goes to the file too, then on to stderr
        if files is not None:
            files.close(None, e)
            files = None
        raise
    finally:
        if files is not None:
            files.close(code, why)
    return code


def _log_secrets(values: dict[str, str]) -> tuple[str, ...]:
    """What the lines sent to Loki mask (#358): the settings' secrets, read leniently as
    ``doctor`` reads them (a malformed setting is the command's to report). None to mask
    unless Loki is set."""
    if not loki.configured(values):
        return ()
    settings, _ = lenient_settings(values, None)
    return secrets_of(settings)


def _command(args: argparse.Namespace) -> int:
    """Run the command ``args`` name with the handler its parser carries (``_parser``, #499); its
    exit code. A command whose parser carries none fails rather than doing nothing and exiting 0,
    which a scheduled job calling it would report as done."""
    run: Run | None = getattr(args, "run", None)
    if run is None:
        print(f"krx-quant: command {args.command} has no handler (set_defaults(run=...))", file=sys.stderr)
        return 1
    return run(args)
