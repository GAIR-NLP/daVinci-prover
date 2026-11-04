"""Validate task checkpoints and hold the exclusive evaluator lease."""

from __future__ import annotations

import fcntl
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, TextIO
from dvp.tasks.dataset import row_task_id


def _read_json_object(path: Path, *, label: str) -> dict[str, Any]:
    """Read a checkpoint object, failing closed on truncation or wrong shape."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid {label} at {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Invalid {label} at {path}: expected a JSON object")
    return payload


def _validated_attempt_record(
    path: Path,
    *,
    row: Mapping[str, Any],
    attempt: int,
    retry: int,
) -> dict[str, Any]:
    record = _read_json_object(path, label="attempt result")
    expected = {
        "task_id": row_task_id(row),
        "dataset_index": row.get("_dataset_index"),
        "attempt": attempt,
        "retry": retry,
    }
    mismatched = [
        key for key, value in expected.items() if record.get(key) != value
    ]
    if mismatched:
        raise ValueError(
            f"Invalid attempt result at {path}: mismatched "
            + ", ".join(mismatched)
        )
    required = {"status", "valid_for_scoring", "success", "score", "result"}
    missing = sorted(required - record.keys())
    if missing:
        raise ValueError(
            f"Invalid attempt result at {path}: missing " + ", ".join(missing)
        )
    return record


def _validated_task_result(
    path: Path,
    *,
    row: Mapping[str, Any],
) -> dict[str, Any]:
    record = _read_json_object(path, label="task result")
    expected = {
        "task_id": row_task_id(row),
        "dataset_index": row.get("_dataset_index"),
    }
    mismatched = [
        key for key, value in expected.items() if record.get(key) != value
    ]
    if mismatched:
        raise ValueError(
            f"Invalid task result at {path}: mismatched "
            + ", ".join(mismatched)
        )
    required = {
        "status",
        "valid_for_scoring",
        "success",
        "score",
        "first_attempt_success",
        "runs_used",
    }
    missing = sorted(required - record.keys())
    if missing:
        raise ValueError(
            f"Invalid task result at {path}: missing " + ", ".join(missing)
        )
    return record


def _attempt_slot_directories(
    task_dir: Path,
    *,
    attempt: int,
    retry: int,
) -> list[tuple[int, Path]]:
    """Return the base and any isolated resume directories for one slot."""

    if not task_dir.exists():
        return []
    base = f"attempt_{attempt}_retry_{retry}"
    pattern = re.compile(rf"^{re.escape(base)}(?:_resume_(\d+))?$")
    directories: list[tuple[int, Path]] = []
    for path in task_dir.iterdir():
        if not path.is_dir():
            continue
        match = pattern.fullmatch(path.name)
        if match is None:
            continue
        generation = int(match.group(1) or 0)
        directories.append((generation, path))
    return sorted(directories, key=lambda item: item[0])


def _resumed_attempt_record(
    task_dir: Path,
    *,
    row: Mapping[str, Any],
    attempt: int,
    retry: int,
) -> dict[str, Any] | None:
    completed: list[dict[str, Any]] = []
    for _, directory in _attempt_slot_directories(
        task_dir,
        attempt=attempt,
        retry=retry,
    ):
        path = directory / "attempt_result.json"
        if path.exists():
            completed.append(
                _validated_attempt_record(
                    path,
                    row=row,
                    attempt=attempt,
                    retry=retry,
                )
            )
    if len(completed) > 1:
        raise ValueError(
            f"Multiple completed checkpoints for attempt={attempt}, retry={retry} "
            f"under {task_dir}"
        )
    if not completed:
        return None
    record = dict(completed[0])
    record["resumed"] = True
    return record


def _fresh_attempt_run_dir(
    task_dir: Path,
    *,
    attempt: int,
    retry: int,
    resume: bool,
) -> Path:
    base = task_dir / f"attempt_{attempt}_retry_{retry}"
    if not resume:
        return base
    existing = _attempt_slot_directories(
        task_dir,
        attempt=attempt,
        retry=retry,
    )
    if not existing:
        return base
    next_generation = max(generation for generation, _ in existing) + 1
    return task_dir / f"attempt_{attempt}_retry_{retry}_resume_{next_generation}"


def acquire_run_lock(run_root: Path) -> TextIO:
    """Hold an advisory exclusive lease for one evaluator process."""

    path = run_root / ".evaluator.lock"
    handle = path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise RuntimeError(
            f"Run root is already active in another evaluator process: {run_root}"
        ) from None
    try:
        handle.seek(0)
        handle.truncate()
        handle.write(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "acquired_at": datetime.now(timezone.utc).isoformat(),
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        handle.flush()
        os.fsync(handle.fileno())
    except BaseException:
        handle.close()
        raise
    return handle


def release_run_lock(handle: TextIO) -> None:
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()
