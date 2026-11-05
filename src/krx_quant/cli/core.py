"""What the command families share (#578): a command's parser (``Command``), the subparsers each
family adds its commands to (``Commands``), a handler (``Run``) and the argument types more than
one family takes."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from typing import Any, TypeAlias

# A command's handler (#499): the parsed arguments in, the exit code out.
Run = Callable[[argparse.Namespace], int]
# What adds a command's arguments to its parser (``Command``).
Arguments = Callable[[argparse.ArgumentParser], None]


class Command(argparse.ArgumentParser):
    """A command's parser. With ``arguments``, its arguments are added when it parses or prints its
    help or usage, not when ``krx-quant``'s parser is built (#578): a command whose arguments' defaults
    come from a module it runs (``prevalidate --days``, ``prevalidation.PREVALIDATION_DAYS``) loads
    that module only when it runs, and the other commands never do."""

    def __init__(self, *args: Any, arguments: Arguments | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._arguments = arguments

    def _add_arguments(self) -> None:
        arguments, self._arguments = self._arguments, None
        if arguments is not None:
            arguments(self)

    def parse_known_args(  # type: ignore[override]  # the stub's three overloads, as argparse calls it
        self, args: Sequence[str] | None = None, namespace: argparse.Namespace | None = None
    ) -> tuple[argparse.Namespace, list[str]]:
        self._add_arguments()
        return super().parse_known_args(args, namespace)

    def format_usage(self) -> str:
        self._add_arguments()
        return super().format_usage()

    def format_help(self) -> str:
        self._add_arguments()
        return super().format_help()


# ``krx-quant``'s subparsers, which make each command's parser a ``Command``: a family's ``add`` adds
# its commands there.
Commands: TypeAlias = "argparse._SubParsersAction[Command]"


def positive(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {value}")
    return value
