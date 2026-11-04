"""Command-line entry point for DaVinci Prover."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime
from pathlib import Path

from dvp.config import RuntimeConfig
from dvp.artifacts.conversation import build_conversation_artifact
from dvp.agent.driver import ProverDriver
from dvp.factory import build_client, build_episode, build_services, preflight_python_executor
from dvp.orchestration.config import episode_config_from_args
from dvp.cli.options import add_runtime_options
from dvp.artifacts.persistence import atomic_write_json
from dvp.artifacts.trajectory import BufferedJsonlSink, NullTrajectorySink


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DVP: inference-only theorem-proving harness.")
    add_runtime_options(parser)
    parser.add_argument("--theorem", help="Lean theorem statement to prove.")
    parser.add_argument(
        "--theorem-file", type=Path, help="File containing the Lean theorem statement.",
    )
    parser.add_argument("--quiet", action="store_true")
    return parser


def read_theorem(args: argparse.Namespace) -> str:
    if args.theorem:
        return args.theorem.strip()
    if args.theorem_file:
        return args.theorem_file.read_text(encoding="utf-8").strip()
    raise SystemExit("Provide --theorem or --theorem-file")


async def async_main(args: argparse.Namespace) -> int:
    if not args.model:
        raise SystemExit("Provide --model or set DAVINCI_MODEL/OPENAI_MODEL")
    theorem = read_theorem(args)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = (
        args.run_dir or Path.cwd() / "runs" / f"dvp_{timestamp}"
    ).expanduser().resolve()
    if run_dir.exists() and any(run_dir.iterdir()):
        raise SystemExit(
            f"Run directory is not empty: {run_dir}. Choose a new directory."
        )
    runtime = RuntimeConfig(
        target_workspace=(
            args.target_workspace.expanduser().resolve()
            if args.target_workspace
            else None
        ),
        retrieve_index=(
            args.retrieve_index.expanduser().resolve()
            if args.retrieve_index
            else None
        ),
        lean_timeout=args.lean_timeout,
        python_executor=await preflight_python_executor(args),
    )
    episode_config = episode_config_from_args(args)

    services = build_services(
        theorem_statement=theorem,
        runtime=runtime,
        run_dir=run_dir,
    )
    from dvp.verification.comparator import preflight_runtime
    if runtime.target_workspace is None:
        raise SystemExit("Comparator requires --target-workspace")
    try:
        await preflight_runtime(runtime.target_workspace, timeout=runtime.lean_timeout)
    except (OSError, ValueError, RuntimeError) as exc:
        raise SystemExit(f"Comparator preflight failed: {exc}") from exc
    client = build_client(args)

    print(f"Run dir: {run_dir}")
    print(f"Submission: {services.workspace.submission_file}")
    if services.workspace.target:
        print(f"Target:  {services.workspace.target}")

    sink = (
        NullTrajectorySink()
        if args.trace_level != "full"
        else BufferedJsonlSink(run_dir / "trajectory.jsonl")
    )
    episode = build_episode(
        theorem_statement=theorem,
        runtime=runtime,
        services=services,
        run_dir=run_dir,
        config=episode_config,
        sink=sink,
    )
    driver = ProverDriver(
        client=client,
        episode=episode,
        theorem_statement=theorem,
        verbose=not args.quiet,
    )
    try:
        result = await driver.run()
    finally:
        try:
            sink.close()
        finally:
            from dvp.toolkits.retrieval.backend import close_cached_retrievers
            await close_cached_retrievers()

    result_payload = result.to_dict()
    atomic_write_json(run_dir / "result.json", result_payload)
    if args.trace_level in {"sft", "full"}:
        atomic_write_json(
            run_dir / "conversation.json",
            build_conversation_artifact(
                driver=driver,
                episode=episode,
                client=client,
                result=result_payload,
                metadata={"mode": "standalone"},
            ),
        )
    print(json.dumps(result_payload, ensure_ascii=False, indent=2, default=str))
    return 0 if result.success and result.score >= 1.0 else 1


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(async_main(build_parser().parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
