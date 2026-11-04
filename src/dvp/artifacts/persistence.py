"""Small crash-safe persistence helpers used by runners.

The proof environment does not depend on these helpers.  They only make
standalone artifacts and evaluator checkpoints replace atomically, so a
process interruption cannot leave a partially-written JSON document that a
later ``--resume`` mistakes for a completed result.
"""

from __future__ import annotations

from collections.abc import Iterable
import json
import os
from pathlib import Path
import tempfile
from typing import Any


def atomic_write_text(
    path: str | Path,
    text: str,
    *,
    encoding: str = "utf-8",
) -> None:
    """Write ``text`` beside ``path`` and atomically replace the destination."""

    atomic_write_lines(path, (text,), encoding=encoding)


def atomic_write_json(
    path: str | Path,
    payload: Any,
    *,
    indent: int | None = 2,
) -> None:
    """Serialize a JSON value and atomically replace ``path``."""

    atomic_write_text(
        path,
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=indent,
            default=str,
        ),
    )


def atomic_write_lines(
    path: str | Path,
    lines: Iterable[str],
    *,
    encoding: str = "utf-8",
) -> None:
    """Stream ``lines`` into a temporary file and atomically replace ``path``.

    Unlike joining a large JSONL snapshot into one string, this keeps peak
    memory bounded by the largest individual line.  The caller remains
    responsible for including line terminators.
    """

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding=encoding) as handle:
            descriptor = -1
            for line in lines:
                handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def append_line(
    path: str | Path,
    line: str,
    *,
    encoding: str = "utf-8",
    sync: bool = True,
) -> None:
    """Append one complete line to a journal.

    Per-task result files remain the source of truth for resume.  Syncing this
    derived root journal makes freshly completed tasks visible promptly while
    avoiding a rewrite of every earlier result.
    """

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("a", encoding=encoding) as handle:
        handle.write(line)
        handle.flush()
        if sync:
            os.fsync(handle.fileno())
