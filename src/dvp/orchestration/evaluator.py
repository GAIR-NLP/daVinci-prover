"""Schedule dataset evaluation and coordinate checkpoints and progress."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from dvp.artifacts.persistence import append_line, atomic_write_json
from dvp.tasks.dataset import (
    load_dataset, select_rows, safe_task_id,
)
from .attempts import run_task
from .checkpoints import (
    _validated_task_result, acquire_run_lock, release_run_lock,
)
from .manifest import (
    build_run_manifest, validate_resume_manifest, ensure_execution_baseline,
)
from .results import (
    ProgressCounts, write_progress_snapshot, append_progress_record,
)


async def async_main(args: argparse.Namespace) -> int:
    if not args.model:
        raise SystemExit("Provide --model or set DAVINCI_MODEL/OPENAI_MODEL")
    if getattr(args, "allow_endpoint_change", False) and not args.resume:
        raise SystemExit("--allow-endpoint-change requires --resume")
    dataset = args.dataset.expanduser().resolve()
    if not dataset.exists():
        raise SystemExit(f"Dataset not found: {dataset}")
    try:
        rows = select_rows(load_dataset(dataset, split=args.split), args)
    except (ValueError, re.error) as exc:
        raise SystemExit(f"Invalid task selection: {exc}") from exc
    from dvp.factory import preflight_python_executor
    await preflight_python_executor(args)
    from dvp.tasks.dataset import row_target_workspace
    from dvp.verification.comparator import preflight_runtime
    workspaces = {row_target_workspace(row, args.target_workspace) for row in rows}
    try:
        for workspace in workspaces:
            if workspace is None:
                raise ValueError("Comparator requires --target-workspace or a task workspace")
            await preflight_runtime(workspace, timeout=args.lean_timeout)
    except (OSError, ValueError, RuntimeError) as exc:
        raise SystemExit(f"Comparator preflight failed: {exc}") from exc
    if any(row.get("_fixed_task_contract") for row in rows):
        from dvp.cli.prepare_contexts import prepare_rows
        try:
            await prepare_rows(rows, args.target_workspace, timeout=args.lean_timeout)
        except (ValueError, RuntimeError) as exc:
            raise SystemExit(f"Fixed-context preflight failed: {exc}") from exc
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_root = (
        args.run_root
        or args.run_dir
        or Path.cwd() / "runs" / f"dvp_eval_{dataset.stem}_{timestamp}"
    ).expanduser().resolve()
    if (
        not args.resume
        and run_root.exists()
        and any(run_root.iterdir())
    ):
        raise SystemExit(
            f"Run root is not empty: {run_root}. "
            "Choose a new directory or pass --resume."
        )
    run_root.mkdir(parents=True, exist_ok=True)
    try:
        run_lock = acquire_run_lock(run_root)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    try:
        return await _async_main_locked(
            args,
            dataset=dataset,
            rows=rows,
            run_root=run_root,
        )
    finally:
        try:
            from dvp.toolkits.retrieval.backend import close_cached_retrievers
            await close_cached_retrievers()
        finally:
            release_run_lock(run_lock)


async def _async_main_locked(
    args: argparse.Namespace,
    *,
    dataset: Path,
    rows: Sequence[dict[str, Any]],
    run_root: Path,
) -> int:
    allow_endpoint_change = bool(
        getattr(args, "allow_endpoint_change", False)
    )
    manifest = build_run_manifest(
        args,
        dataset=dataset,
        run_root=run_root,
        rows=rows,
    )
    manifest_path = run_root / "run_manifest.json"
    keep_existing_manifest = False
    original_manifest: Mapping[str, Any] = manifest
    manifest_base_url = manifest.get("base_url")
    endpoint_changed = False
    if args.resume:
        if manifest_path.exists():
            try:
                existing_manifest = json.loads(
                    manifest_path.read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError) as exc:
                raise SystemExit(
                    f"Cannot resume: invalid run manifest: {exc}"
                ) from exc
            if not isinstance(existing_manifest, Mapping):
                raise SystemExit("Cannot resume: run manifest is not an object")
            try:
                validate_resume_manifest(
                    existing_manifest,
                    manifest,
                    allow_endpoint_change=allow_endpoint_change,
                )
            except ValueError as exc:
                raise SystemExit(str(exc)) from exc
            keep_existing_manifest = True
            original_manifest = existing_manifest
            manifest_base_url = existing_manifest.get("base_url")
            endpoint_changed = manifest_base_url != manifest.get("base_url")
        elif (run_root / "tasks").exists():
            raise SystemExit(
                "Cannot resume existing task results without run_manifest.json"
            )
    if not keep_existing_manifest:
        atomic_write_json(manifest_path, manifest)
    try:
        execution_baseline = ensure_execution_baseline(
            run_root,
            current_manifest=manifest,
            original_manifest=original_manifest,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    append_line(
        run_root / "run_invocations.jsonl",
        json.dumps(
            {
                "started_at": datetime.now(timezone.utc).isoformat(),
                "resume": bool(args.resume),
                "source": manifest["source"],
                "config_id": manifest["config_id"],
                "concurrency": max(1, int(args.concurrency)),
                "trace_level": args.trace_level,
                "base_url": manifest.get("base_url"),
                "manifest_base_url": manifest_base_url,
                "endpoint_changed": endpoint_changed,
                "allow_endpoint_change": allow_endpoint_change,
                "execution_identity": execution_baseline["identity"],
            },
            ensure_ascii=False,
            default=str,
        )
        + "\n",
    )

    progress: dict[int, dict[str, Any]] = {}

    if args.resume:
        for row in rows:
            result_path = (
                run_root / "tasks" / safe_task_id(row) / "result.json"
            )
            if not result_path.exists():
                continue
            record = _validated_task_result(result_path, row=row)
            record["resumed"] = True
            progress[int(row.get("_dataset_index") or 0)] = record

    write_progress_snapshot(
        args=args,
        manifest=manifest,
        dataset=dataset,
        run_root=run_root,
        selected=len(rows),
        records=list(progress.values()),
    )
    counts = ProgressCounts.from_records(list(progress.values()))
    pending_rows = [
        row
        for row in rows
        if int(row.get("_dataset_index") or 0) not in progress
    ]

    cursor = 0

    async def worker() -> None:
        nonlocal cursor
        while cursor < len(pending_rows):
            row = pending_rows[cursor]
            cursor += 1
            record = await run_task(row, args, run_root)
            dataset_index = int(row.get("_dataset_index") or 0)
            if dataset_index in progress:
                raise RuntimeError(
                    f"task result published twice for dataset index {dataset_index}"
                )
            progress[dataset_index] = record
            append_progress_record(
                args=args,
                manifest=manifest,
                dataset=dataset,
                run_root=run_root,
                selected=len(rows),
                record=record,
                counts=counts,
            )
            print(
                f"[{record['status']}] {record['task_id']} "
                f"score={record['score']:.1f} runs={record['runs_used']}"
            )

    worker_count = min(
        max(1, int(args.concurrency)),
        len(pending_rows),
    )
    workers = [asyncio.create_task(worker()) for _ in range(worker_count)]
    try:
        if workers:
            await asyncio.gather(*workers)
    except BaseException:
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        raise
    summary = write_progress_snapshot(
        args=args,
        manifest=manifest,
        dataset=dataset,
        run_root=run_root,
        selected=len(rows),
        records=list(progress.values()),
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0
