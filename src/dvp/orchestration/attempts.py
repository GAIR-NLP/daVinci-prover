"""Execute individual proof samples and aggregate infrastructure retries."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Mapping
from dvp.config import RuntimeConfig
from dvp.agent.driver import ProverDriver
from dvp.agent.episode import ProverEpisode
from dvp.artifacts.conversation import build_conversation_artifact
from dvp.artifacts.persistence import atomic_write_json
from dvp.artifacts.trajectory import BufferedJsonlSink, NullTrajectorySink
from dvp.factory import build_client, build_episode, build_services, build_python_executor
from dvp.llm_clients.client import APIError, is_retryable_model_error
from dvp.tasks.dataset import row_task_id, row_target_workspace, row_theorem, safe_task_id
from .config import episode_config_from_args
from .checkpoints import _validated_task_result, _resumed_attempt_record, _fresh_attempt_run_dir


_INFRA_MARKERS = (
    "unicodeencodeerror",
    "localprotocolerror",
    "illegal header value",
    "connecterror",
    "connecttimeout",
    "connection refused",
    "connection reset",
    "network is unreachable",
    "readtimeout",
    "remoteprotocolerror",
    "http 504",
    "status code 504",
    "error code: 504",
    "stream timeout",
    "transporterror",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
)


def _task_routing_key(row: Mapping[str, Any], run_root: Path) -> str:
    """Keep one task's worker affinity across samples, retries, and resumes."""

    identity = json.dumps(
        [str(run_root.expanduser().resolve()), row_task_id(row)],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return "dvp-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


async def run_attempt(
    row: dict[str, Any],
    args: argparse.Namespace,
    run_dir: Path,
    *,
    attempt: int,
    retry: int,
    routing_key: str | None = None,
) -> dict[str, Any]:
    started = time.time()
    theorem = row_theorem(row)
    from dvp.verification.fixed_context import FixedTaskContract
    contract_metadata = row.get("_fixed_task_contract")
    task_contract = FixedTaskContract.from_metadata(theorem, contract_metadata, source_identity=row.get("_fixed_contract_identity")) if contract_metadata else None
    target = row_target_workspace(row, args.target_workspace)
    if target is None:
        raise ValueError(
            f"task {row_task_id(row)!r}: provide --target-workspace or row target_workspace.path"
        )
    runtime = RuntimeConfig(
        target_workspace=target,
        retrieve_index=args.retrieve_index.expanduser().resolve()
        if args.retrieve_index
        else None,
        lean_timeout=args.lean_timeout,
        python_executor=build_python_executor(args),
    )
    services = build_services(
        theorem_statement=theorem,
        runtime=runtime,
        run_dir=run_dir,
        task_contract=task_contract,
    )
    if routing_key is None:
        # run_task supplies the resolved run root's key. Keep direct callers
        # compatible, including the run-dir alias and standard attempt layout.
        routing_root = (
            getattr(args, "run_root", None)
            or getattr(args, "run_dir", None)
            or run_dir.resolve().parents[2]
        )
        routing_key = _task_routing_key(row, Path(routing_root))
    client = build_client(args, routing_key=routing_key)
    error: str | None = None
    sink: BufferedJsonlSink | NullTrajectorySink | None = None
    episode: ProverEpisode | None = None
    driver: ProverDriver | None = None
    try:
        sink = (
            NullTrajectorySink()
            if args.trace_level != "full"
            else BufferedJsonlSink(run_dir / "trajectory.jsonl")
        )
        episode = build_episode(
            runtime=runtime,
            services=services,
            theorem_statement=theorem,
            run_dir=run_dir,
            config=episode_config_from_args(args),
            sink=sink,
        )
        driver = ProverDriver(
            client=client,
            episode=episode,
            theorem_statement=theorem,
            verbose=False,
        )
        # Generation owns the task deadline. Cancellation finishes subprocess
        # cleanup before the final saved source is frozen. Distinguish this
        # deadline from an upstream TimeoutError raised before it expires.
        deadline = asyncio.timeout(args.task_timeout or None)
        try:
            async with deadline:
                await driver.run_rollout()
        except TimeoutError:
            if not deadline.expired():
                raise
            episode.stop("task_timeout", truncated=True)
        # Final acceptance has its own Lean timeout, independent of exhausted
        # generation time, turn and tool budgets.
        result = await episode.finalize()
        payload = result.to_dict()
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        infrastructure_error = (
            is_retryable_model_error(exc)
            if isinstance(exc, APIError) else _is_infrastructure_text(error)
        )
        payload = {
            "success": False,
            "score": 0.0,
            "message": error,
            "aborted": infrastructure_error,
            "reason": (
                "infrastructure_error" if infrastructure_error else "exception"
            ),
        }
    finally:
        if sink is not None:
            sink.close()

    infrastructure = _result_is_infrastructure(payload)
    success = bool(payload.get("success") and float(payload.get("score") or 0.0) >= 1.0)
    retryable_service_error = bool(
        not success
        and not infrastructure
        and payload.get("retryable_service_error")
    )
    endpoint = {
        "base_url": args.base_url,
        "model": args.model,
        "routing_key": client.routing_key,
    }
    record = {
        "task_id": row_task_id(row),
        "dataset_index": row.get("_dataset_index"),
        "attempt": attempt,
        "retry": retry,
        "endpoint": endpoint,
        "status": (
            "infrastructure_error"
            if infrastructure
            else "retryable_service_error"
            if retryable_service_error
            else "passed"
            if success
            else "failed"
        ),
        "valid_for_scoring": not infrastructure and not retryable_service_error,
        "retryable_service_error": retryable_service_error,
        "success": success,
        "score": 1.0 if success else 0.0,
        "elapsed_seconds": time.time() - started,
        "error": error,
        "result": payload,
        "run_dir": str(run_dir),
        **({"fixed_task_contract": {"contract_hash": task_contract.contract_hash, "target_name": task_contract.target_name, "source_sha256": contract_metadata["effective_statement_sha256"]}} if task_contract is not None else {}),
    }
    if (
        args.trace_level in {"sft", "full"}
        and driver is not None
        and episode is not None
    ):
        try:
            atomic_write_json(
                run_dir / "conversation.json",
                build_conversation_artifact(
                    driver=driver,
                    episode=episode,
                    client=client,
                    result=payload,
                    metadata={
                        "mode": "evaluation",
                        "task_id": row_task_id(row),
                        "dataset_index": row.get("_dataset_index"),
                        "task_metadata": row.get("metadata", {}),
                        "attempt": attempt,
                        "retry": retry,
                        "status": record["status"],
                        "endpoint": endpoint,
                    },
                ),
            )
        except Exception as exc:
            # A trace export failure must be visible without changing a valid
            # theorem score into a proof failure.
            record["conversation_error"] = f"{type(exc).__name__}: {exc}"
    atomic_write_json(run_dir / "attempt_result.json", record)
    return record


async def run_task(
    row: dict[str, Any],
    args: argparse.Namespace,
    run_root: Path,
) -> dict[str, Any]:
    task_dir = run_root / "tasks" / safe_task_id(row)
    result_path = task_dir / "result.json"
    if args.resume and result_path.exists():
        record = _validated_task_result(result_path, row=row)
        record["resumed"] = True
        return record

    runs: list[dict[str, Any]] = []
    valid_attempts: list[dict[str, Any]] = []
    for attempt in range(1, max(1, args.attempts_per_task) + 1):
        for retry in range(0, max(0, args.retries) + 1):
            record = (
                _resumed_attempt_record(
                    task_dir,
                    row=row,
                    attempt=attempt,
                    retry=retry,
                )
                if args.resume
                else None
            )
            ran_new_attempt = record is None
            if record is None:
                run_dir = _fresh_attempt_run_dir(
                    task_dir,
                    attempt=attempt,
                    retry=retry,
                    resume=bool(args.resume),
                )
                record = await run_attempt(
                    row,
                    args,
                    run_dir,
                    attempt=attempt,
                    retry=retry,
                    routing_key=_task_routing_key(row, run_root),
                )
            runs.append(record)
            if record["valid_for_scoring"]:
                valid_attempts.append(record)
                break
            if _final_acceptance_aborted(record):
                # Final acceptance must not launch another model generation
                # through ordinary infrastructure retries.
                break
            if (
                ran_new_attempt
                and retry < max(0, args.retries)
                and args.retry_delay > 0
            ):
                await asyncio.sleep(args.retry_delay * (2**retry))
        if _final_acceptance_aborted(runs[-1]):
            break
        if valid_attempts and valid_attempts[-1]["success"]:
            break

    passed = next((item for item in valid_attempts if item["success"]), None)
    best = passed or (valid_attempts[-1] if valid_attempts else runs[-1])
    aggregate = {
        "task_id": row_task_id(row),
        "dataset_index": row.get("_dataset_index"),
        "metadata": row.get("metadata", {}),
        "status": (
            "passed"
            if passed
            else "failed"
            if valid_attempts
            else "retryable_service_error"
            if any(item.get("retryable_service_error") for item in runs)
            else "infrastructure_error"
        ),
        "valid_for_scoring": bool(valid_attempts),
        "success": bool(passed),
        "score": 1.0 if passed else 0.0,
        "first_attempt_success": bool(valid_attempts and valid_attempts[0]["success"]),
        "attempts_used": len(valid_attempts),
        "runs_used": len(runs),
        "infrastructure_retries": sum(
            1
            for item in runs
            if not item["valid_for_scoring"]
            and not item.get("retryable_service_error")
        ),
        "retryable_service_retries": sum(
            1 for item in runs if item.get("retryable_service_error")
        ),
        "result": best.get("result"),
        "runs": runs,
        "run_dir": str(task_dir),
    }
    atomic_write_json(result_path, aggregate)
    return aggregate


def _final_acceptance_aborted(record: Mapping[str, Any]) -> bool:
    result = record.get("result")
    return bool(
        isinstance(result, Mapping) and result.get("aborted")
        and result.get("reason") in {
            "comparator_timeout", "comparator_infrastructure_error",
        }
    )


def _is_infrastructure_text(text: str) -> bool:
    lower = text.lower()
    return any(marker in lower for marker in _INFRA_MARKERS) or "timeouterror" in lower


def _result_is_infrastructure(
    payload: Mapping[str, Any],
) -> bool:
    """Classify typed outcomes without scanning proof/model content."""

    if bool(payload.get("aborted")) or payload.get("reason") == "infrastructure_error":
        return True
    return False
