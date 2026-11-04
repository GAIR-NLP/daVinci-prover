"""Prepare immutable benchmark contexts and a shared parser without model calls."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import time
from typing import Any, Callable, Mapping, Sequence

from dvp.verification.context_cache import cache_root, environment_fingerprint
from dvp.verification.fixed_context import FixedTaskContract
from dvp.verification.parser_service import warmup_parser
from dvp.artifacts.persistence import atomic_write_json, atomic_write_text


from dvp.runtime.paths import DEFAULT_TARGET_WORKSPACE


async def prepare_rows(rows: Sequence[Mapping[str, Any]], target_workspace: Path | None,
                       *, timeout: float = 3600, emit: Callable[[str], None] | None = None) -> dict[str, Any]:
    from dvp.tasks.dataset import row_target_workspace, row_task_id, row_theorem
    emit = emit or (lambda message: print(message, flush=True))
    fixed = [row for row in rows if row.get("_fixed_task_contract")]
    started = time.monotonic()
    targets = {}
    for row in fixed:
        target = row_target_workspace(row, target_workspace)
        if target is None:
            raise ValueError("A target workspace is required for fixed-context verification.")
        targets[target.expanduser().resolve()] = None
    if fixed:
        emit("Connecting to the shared Lean parser (only a new service imports Mathlib; later runs reuse it)...")
    parsers = []
    for target in targets:
        state = await warmup_parser(target, timeout=timeout)
        if isinstance(state, dict):
            parsers.append({"workspace": str(target), **{
                key: state.get(key) for key in (
                    "pid", "started_utc", "socket", "warmed", "worker_reused", "reused", "daemon_started"
                )
            }})
            action = "reused" if state.get("worker_reused") else "loaded"
            emit(f"Shared Lean parser ready: {action} Mathlib environment (PID {state.get('pid')}).")
        targets[target] = environment_fingerprint(target)
    hits = 0
    for index, row in enumerate(fixed, 1):
        target = row_target_workspace(row, target_workspace).expanduser().resolve()
        contract = FixedTaskContract.from_metadata(
            row_theorem(row), row["_fixed_task_contract"],
            source_identity=row.get("_fixed_contract_identity"),
        )
        try:
            await contract.validate(target, timeout=timeout)
        except (ValueError, RuntimeError) as exc:
            raise ValueError(f"Fixed-context preparation failed for {row_task_id(row)}: {exc}") from exc
        hits += int(contract.source_cache_hit)
        if isinstance(row, dict):
            row["_fixed_context_environment_sha256"] = targets[target]
        if index % 100 == 0 and index != len(fixed):
            emit(f"Fixed benchmark contexts: {index}/{len(fixed)} ({hits} cache hits).")
    result = {"tasks": len(fixed), "cache_hits": hits, "prepared": len(fixed) - hits,
              "elapsed_seconds": round(time.monotonic() - started, 3),
              "cache_root": str(cache_root()),
              "parsers": parsers,
              "environments": {str(path): value for path, value in targets.items()}}
    if fixed:
        emit(f"Fixed benchmark contexts ready: {len(fixed)} tasks (cache hits: {hits}, prepared: {len(fixed) - hits}).")
    return result


def _staged_rows(path: Path) -> list[dict[str, Any]]:
    """Load all source rows for explicit activation, including every split."""
    from dvp.verification.fixed_context import load_contract_sidecar

    if path.suffix.lower() != ".jsonl":
        raise ValueError("Fixed-context activation requires a JSONL dataset.")
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    load_contract_sidecar(path, rows, require_active=False)
    if not rows or not all(row.get("_fixed_task_contract") for row in rows):
        raise ValueError(f"Dataset has no fixed-context metadata: {path}")
    return rows


def _activation_identity(path: Path) -> dict[str, str]:
    from dvp.verification.command_parser import parser_fingerprint
    from dvp.verification.fixed_context import POLICY_VERSION, preparation_fingerprint

    return {
        "policy": POLICY_VERSION,
        "dataset_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "sidecar_sha256": hashlib.sha256(path.with_suffix(".contract.json").read_bytes()).hexdigest(),
        "parser_sha256": parser_fingerprint(),
        "preparation_sha256": preparation_fingerprint(),
    }


def _publish_activation(path: Path, identity: dict[str, str], preparation: dict[str, Any]) -> None:
    activation = path.with_suffix(".contract.ready.json")
    if activation.exists():
        previous = activation.read_bytes()
        previous_hash = hashlib.sha256(previous).hexdigest()
        archive = path.with_suffix(f".contract.ready.{previous_hash}.json")
        if not archive.exists():
            atomic_write_text(archive, previous.decode("utf-8"))
    atomic_write_json(activation, {
        **identity,
        "validated_at_utc": datetime.now(timezone.utc).isoformat(),
        "validation": {
            "command": "dvp prepare-contexts --activate",
            "tasks": preparation["tasks"],
            "environments": preparation["environments"],
            "model_calls": 0,
        },
    })


async def _main(args: argparse.Namespace) -> None:
    from dvp.tasks.dataset import load_dataset
    paths = args.dataset
    if not paths:
        raise ValueError("Provide at least one --dataset with fixed-context metadata.")
    rows = []
    identities = {}
    for path in paths:
        if args.activate:
            identities[path] = _activation_identity(path)
            selected = _staged_rows(path)
        else:
            selected = load_dataset(path, split=args.split)
        if not selected or not all(row.get("_fixed_task_contract") for row in selected):
            raise ValueError(f"Dataset has no active fixed-context metadata: {path}")
        rows.extend(selected)
    result = await prepare_rows(rows, args.target_workspace, timeout=args.lean_timeout)
    if args.activate:
        # A long parser warmup must not activate metadata or code that changed
        # while it was being checked. Publish only after every dataset passes.
        for path, identity in identities.items():
            if _activation_identity(path) != identity:
                raise ValueError(f"Dataset or verifier changed during activation: {path}")
        for path, identity in identities.items():
            preparation = {**result, "tasks": len(_staged_rows(path))}
            _publish_activation(path, identity, preparation)
        result["activated"] = [str(path.expanduser().resolve()) for path in paths]
    result["datasets"] = [str(path.expanduser().resolve()) for path in paths]
    result["model_calls"] = 0
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False), flush=True)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, action="append", required=True, help="Dataset with fixed-context metadata; repeat for multiple datasets.")
    parser.add_argument("--split", default="test")
    parser.add_argument("--target-workspace", type=Path, default=DEFAULT_TARGET_WORKSPACE)
    parser.add_argument("--lean-timeout", type=float, default=3600)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--activate", action="store_true", help="Validate all source contexts, then activate the current source preparation and parser; preserve previous ready metadata.")
    args = parser.parse_args(argv)
    try:
        asyncio.run(_main(args))
    except (ValueError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
