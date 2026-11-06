"""Serializable protocol shared by local inference, replay, and RL."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any, Mapping


def jsonable(value: Any) -> Any:
    """Convert protocol values to stable JSON-compatible data."""

    if is_dataclass(value):
        return jsonable(asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def decode_tool_arguments(raw: Any) -> tuple[dict[str, Any], str | None]:
    """Decode a complete argument object without repairing malformed JSON."""

    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            return {}, f"Invalid tool arguments JSON: {exc}"
    if not isinstance(raw, Mapping):
        return {}, (
            "Tool arguments must be a JSON object; "
            f"received {type(raw).__name__}."
        )
    return dict(raw), None


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    synthetic: bool = False
    source: str = "native"
    argument_parse_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return jsonable(self)


@dataclass(frozen=True)
class AssistantAction:
    content: str = ""
    reasoning_content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_calls(
        cls,
        calls: list[ToolCall] | tuple[ToolCall, ...],
        *,
        content: str = "",
        reasoning_content: str = "",
        raw: dict[str, Any] | None = None,
    ) -> "AssistantAction":
        return cls(
            content=content,
            reasoning_content=reasoning_content,
            tool_calls=tuple(calls),
            raw=dict(raw or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return jsonable(self)


@dataclass(frozen=True)
class ToolOutcome:
    call_id: str
    name: str
    backend_name: str | None
    arguments: dict[str, Any]
    executed: bool
    valid: bool
    verified: bool | None
    terminal: bool
    reward: float
    error_kind: str | None = None
    artifact_id: str | None = None
    feedback: str = ""
    infrastructure_error: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return jsonable(self)


@dataclass(frozen=True)
class Observation:
    schema_version: str
    episode_id: str
    step_id: int
    task: dict[str, Any]
    state: dict[str, Any]
    tools: list[dict[str, Any]]
    last_outcomes: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return jsonable(self)

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]


@dataclass(frozen=True)
class Transition:
    schema_version: str
    episode_id: str
    step_id: int
    observation: Observation
    action: AssistantAction
    outcomes: tuple[ToolOutcome, ...]
    next_observation: Observation
    reward_components: dict[str, float]
    reward: float
    terminated: bool
    truncated: bool
    aborted: bool
    retryable_service_error: bool = False
    reason: str | None = None
    events: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return jsonable(self)


@dataclass(frozen=True)
class EpisodeResult:
    success: bool
    score: float
    message: str
    episode_id: str
    turns: int
    tool_calls: int
    lean_calls: int
    terminated: bool
    truncated: bool
    aborted: bool
    retryable_service_error: bool = False
    reason: str | None = None
    proof_code: str | None = None
    final_tool_result: dict[str, Any] | None = None
    rollout_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return jsonable(self)

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)
