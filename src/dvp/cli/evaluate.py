"""Command-line argument parsing for dataset evaluation."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import Sequence
from dvp.cli.options import add_runtime_options
from dvp.runtime.paths import BENCHMARKS_ROOT


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate DVP on JSON/JSONL tasks.")
    add_runtime_options(parser)
    parser.add_argument(
        "--dataset",
        type=Path,
        default=BENCHMARKS_ROOT / "fate_h_lean435.jsonl",
        help="JSON/JSONL task file (defaults to this repository's Lean 4.35 FATE-H set).",
    )
    parser.add_argument(
        "--split",
        default="test",
        help="JSON dictionary key or row split value; use 'all' to disable filtering.",
    )
    parser.add_argument(
        "--run-root",
        "--output-dir",
        dest="run_root",
        type=Path,
        default=None,
        help="Evaluation output root; --run-dir is also accepted as a fallback.",
    )
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument(
        "--attempts-per-task",
        type=int,
        default=1,
        help="Fresh model samples per task; stop at the first verified proof.",
    )
    parser.add_argument(
        "--retries",
        "--transient-error-retries",
        dest="retries",
        type=int,
        default=1,
        help="Whole-attempt retries for infrastructure failures only.",
    )
    parser.add_argument("--retry-delay", type=float, default=2.0)
    parser.add_argument("--task-timeout", type=float, default=None)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--allow-endpoint-change",
        action="store_true",
        help=(
            "Allow --resume to use a different LLM base URL while keeping "
            "the original manifest immutable. Model, sampling, policy, "
            "tools, data, and all other run identity fields remain locked."
        ),
    )
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--task-id",
        action="append",
        default=[],
        help="Keep an exact task id (repeatable).",
    )
    parser.add_argument(
        "--task-filter",
        default=None,
        help="Regex applied to task id after --task-id filtering.",
    )
    return parser



def main(argv: Sequence[str] | None = None) -> int:
    from dvp.orchestration.evaluator import async_main

    return asyncio.run(async_main(build_parser().parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
