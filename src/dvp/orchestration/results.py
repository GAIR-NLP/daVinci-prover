"""Maintain scoring denominators and publish ordered evaluation results."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence
from dvp.artifacts.persistence import append_line, atomic_write_json, atomic_write_lines


def _record_sort_key(record: Mapping[str, Any]) -> int:
    value = record.get("dataset_index")
    return int(value) if value is not None else 0


@dataclass
class ProgressCounts:
    """O(1) summary counters for a growing evaluation."""

    completed: int = 0
    scored: int = 0
    passed: int = 0
    first_passed: int = 0
    infrastructure_failed: int = 0
    retryable_service_failed: int = 0

    @classmethod
    def from_records(
        cls,
        records: Sequence[Mapping[str, Any]],
    ) -> "ProgressCounts":
        counts = cls()
        for record in records:
            counts.add(record)
        return counts

    def add(self, record: Mapping[str, Any]) -> None:
        self.completed += 1
        valid = bool(record.get("valid_for_scoring"))
        success = bool(record.get("success"))
        if valid:
            self.scored += 1
            if success:
                self.passed += 1
            if bool(record.get("first_attempt_success")):
                self.first_passed += 1
        self.infrastructure_failed += int(
            record.get("status") == "infrastructure_error"
        )
        self.retryable_service_failed += int(
            record.get("status") == "retryable_service_error"
        )


def build_progress_summary(
    *,
    args: argparse.Namespace,
    manifest: Mapping[str, Any],
    dataset: Path,
    run_root: Path,
    selected: int,
    records: Sequence[Mapping[str, Any]] = (),
    counts: ProgressCounts | None = None,
) -> dict[str, Any]:
    """Build a denominator-explicit summary for complete or partial runs."""

    counts = counts or ProgressCounts.from_records(records)
    return {
        "dataset": str(dataset),
        "split": args.split,
        "run_root": str(run_root),
        "controller": "dvp",
        "model": args.model,
        "config_id": manifest["config_id"],
        "schema_version": manifest["schema_version"],
        "selected": selected,
        "completed": counts.completed,
        "pending": max(0, selected - counts.completed),
        "run_complete": counts.completed == selected,
        "coverage": counts.completed / selected if selected else 1.0,
        "scored": counts.scored,
        "passed": counts.passed,
        "success_rate_selected": (
            counts.passed / selected if selected else None
        ),
        "infrastructure_failed": counts.infrastructure_failed,
        "retryable_service_failed": counts.retryable_service_failed,
        "pass@1": (
            counts.first_passed / counts.scored if counts.scored else None
        ),
        f"pass@{max(1, args.attempts_per_task)}": (
            counts.passed / counts.scored if counts.scored else None
        ),
        "max_turns": args.max_turns,
        "max_tool_calls": args.max_tool_calls,
        "max_lean_calls": args.max_lean_calls,
        "temperature": args.temperature,
        "top_p": args.top_p,
        "presence_penalty": args.presence_penalty,
        "max_tokens": args.max_tokens,
        "trace_level": args.trace_level,
        "concurrency": args.concurrency,
        "retries": args.retries,
    }


def write_progress_snapshot(
    *,
    args: argparse.Namespace,
    manifest: Mapping[str, Any],
    dataset: Path,
    run_root: Path,
    selected: int,
    records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Atomically publish ordered task results and their matching summary."""

    ordered = sorted(records, key=_record_sort_key)
    atomic_write_lines(
        run_root / "results.jsonl",
        (
            json.dumps(item, ensure_ascii=False, default=str) + "\n"
            for item in ordered
        ),
    )
    summary = build_progress_summary(
        args=args,
        manifest=manifest,
        dataset=dataset,
        run_root=run_root,
        selected=selected,
        records=ordered,
    )
    atomic_write_json(run_root / "summary.json", summary)
    return summary


def append_progress_record(
    *,
    args: argparse.Namespace,
    manifest: Mapping[str, Any],
    dataset: Path,
    run_root: Path,
    selected: int,
    record: Mapping[str, Any],
    counts: ProgressCounts,
) -> dict[str, Any]:
    """Publish one completed task without rewriting all earlier results."""

    append_line(
        run_root / "results.jsonl",
        json.dumps(record, ensure_ascii=False, default=str) + "\n",
    )
    counts.add(record)
    summary = build_progress_summary(
        args=args,
        manifest=manifest,
        dataset=dataset,
        run_root=run_root,
        selected=selected,
        counts=counts,
    )
    atomic_write_json(run_root / "summary.json", summary)
    return summary
