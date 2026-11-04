"""Load and select evaluation tasks without starting an agent."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence


def load_dataset(path: Path, *, split: str = "test") -> list[dict[str, Any]]:
    """Load JSONL, a JSON list, or a split-keyed JSON object."""

    path = path.expanduser().resolve()
    if path.suffix.lower() == ".jsonl":
        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    else:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, list):
            rows = payload
        elif isinstance(payload, dict):
            selected: Any
            if split.lower() == "all":
                split_lists = [
                    value for value in payload.values() if isinstance(value, list)
                ]
                selected = [
                    item
                    for values in split_lists
                    for item in values
                ] if split_lists else None
            else:
                selected = payload.get(split)
            if not isinstance(selected, list):
                selected = (
                    payload.get("data")
                    or payload.get("rows")
                    or payload.get("examples")
                    or payload.get("tasks")
                )
            if not isinstance(selected, list) and any(
                key in payload
                for key in ("theorem_statement", "formal_statement", "statement")
            ):
                selected = [payload]
            if not isinstance(selected, list):
                raise ValueError(
                    f"JSON must be a list or contain list key {split!r}/data/rows/examples"
                )
            rows = selected
        else:
            raise ValueError("dataset JSON must contain an object or list")

    from dvp.verification.fixed_context import load_contract_sidecar
    load_contract_sidecar(path, rows, require_active=True)
    normalized: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"dataset row {index} is not an object")
        row_split = row.get("split")
        if (
            split.lower() != "all"
            and row_split is not None
            and str(row_split) != split
        ):
            continue
        item = dict(row)
        item["_dataset_index"] = index
        normalized.append(item)
    return normalized


def select_rows(rows: Sequence[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    exact = {str(item) for item in (args.task_id or [])}
    if exact:
        available = {row_task_id(row) for row in rows}
        missing = sorted(exact - available)
        if missing:
            raise ValueError(
                "Unknown --task-id value(s): " + ", ".join(missing)
            )
    pattern = re.compile(args.task_filter) if args.task_filter else None
    selected = []
    for row in rows:
        task_id = row_task_id(row)
        if exact and task_id not in exact:
            continue
        if pattern and not pattern.search(task_id):
            continue
        selected.append(row)
    start = max(0, int(args.start))
    stop = None if args.limit is None else start + max(0, int(args.limit))
    return selected[start:stop]


def row_task_id(row: Mapping[str, Any]) -> str:
    return str(
        row.get("task_id")
        or row.get("name")
        or row.get("id")
        or f"row_{row.get('_dataset_index', 0)}"
    )


def safe_task_id(row: Mapping[str, Any]) -> str:
    raw = row_task_id(row)
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in raw)[:128]
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    return f"{safe or 'task'}_{digest}"


def row_theorem(row: Mapping[str, Any]) -> str:
    value = (
        row.get("theorem_statement")
        or row.get("formal_statement")
        or row.get("statement")
    )
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"task {row_task_id(row)!r} has no theorem statement")
    return value.strip()


def row_target_workspace(
    row: Mapping[str, Any],
    fallback: Path | None,
) -> Path | None:
    value = row.get("target_workspace")
    if isinstance(value, Mapping):
        value = value.get("path")
    if value:
        return Path(str(value)).expanduser().resolve()
    return fallback.expanduser().resolve() if fallback else None
