"""Public command line: ``dvp prove``, ``dvp eval`` and maintenance commands."""

from __future__ import annotations

import argparse
import importlib
import sys
from collections.abc import Sequence


COMMANDS = {
    "prove": ("dvp.cli.prove", "Prove one Lean theorem"),
    "eval": ("dvp.cli.evaluate", "Evaluate a dataset with retries and resume"),
    "prepare-contexts": ("dvp.cli.prepare_contexts", "Prepare fixed benchmark contexts"),
    "reverify": ("dvp.cli.reverify", "Reverify saved fixed-context candidates"),
    "parser": ("dvp.verification.parser_service", "Manage the shared Lean parser"),
    "check-python": ("dvp.cli.check_python", "Check local Python sandbox isolation"),
}


def main(argv: Sequence[str] | None = None) -> int | None:
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        prog="dvp", description="DaVinci Prover: tool-driven Lean theorem proving.",
        epilog="Use dvp COMMAND --help for command-specific options.",
    )
    parser.add_argument("command", choices=COMMANDS, help="; ".join(
        f"{name}: {description}" for name, (_, description) in COMMANDS.items()
    ))
    if not arguments:
        parser.print_help()
        return
    command = parser.parse_args(arguments[:1]).command
    module = importlib.import_module(COMMANDS[command][0])
    return module.main(arguments[1:])
