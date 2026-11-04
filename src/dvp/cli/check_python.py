"""Check local Python isolation without a model endpoint or Lean installation."""
from __future__ import annotations

import argparse
import asyncio
import json

from dvp.cli.options import add_python_options
from dvp.factory import preflight_python_executor


async def async_main(args: argparse.Namespace) -> int:
    args.python_execute = True
    executor = await preflight_python_executor(args)
    print(json.dumps({"success": True, "python_execute": executor.identity}, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_python_options(parser)
    return asyncio.run(async_main(parser.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
