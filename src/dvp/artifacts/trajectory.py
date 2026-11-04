"""Versioned, replayable trajectory sinks.

The JSONL writer buffers events so an RL worker does not synchronously append on
every tiny state update.  Each episode normally owns its own trajectory file.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Protocol

from dvp.protocol import jsonable


class TrajectorySink(Protocol):
    def record(self, event: dict[str, Any]) -> None: ...

    def close(self) -> None: ...


class NullTrajectorySink:
    def record(self, event: dict[str, Any]) -> None:
        del event

    def close(self) -> None:
        return None


class MemoryTrajectorySink:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def record(self, event: dict[str, Any]) -> None:
        self.events.append(jsonable(event))

    def close(self) -> None:
        return None


class BufferedJsonlSink:
    def __init__(self, path: Path, *, flush_every: int = 32) -> None:
        if flush_every < 1:
            raise ValueError("flush_every must be positive")
        self.path = path
        self.flush_every = flush_every
        self._buffer: list[dict[str, Any]] = []
        self._closed = False

    def record(self, event: dict[str, Any]) -> None:
        if self._closed:
            raise RuntimeError("trajectory sink is closed")
        self._buffer.append(jsonable(event))
        if len(self._buffer) >= self.flush_every:
            self.flush()

    def flush(self) -> None:
        if not self._buffer:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            for event in self._buffer:
                handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
        self._buffer.clear()

    def close(self) -> None:
        if self._closed:
            return
        self.flush()
        self._closed = True

    def __enter__(self) -> "BufferedJsonlSink":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def episode_event(
    *,
    schema_version: str,
    episode_id: str,
    event: str,
    step_id: int,
    payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": schema_version,
        "episode_id": episode_id,
        "event": event,
        "step_id": step_id,
        "time": time.time(),
        "payload": jsonable(payload),
    }

