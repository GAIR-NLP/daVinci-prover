"""Explicit episode semantics and runtime resource limits for DVP."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from dvp.runtime.python_execution import PythonExecutor


GuidanceMode = Literal["off", "soft"]
# Changing final acceptance must invalidate manifests from the previous verifier.
DVP_POLICY_VERSION = "dvp.policy.comparator.v4"


@dataclass(frozen=True)
class BudgetConfig:
    """Resource limits that are part of the environment contract."""

    max_turns: int = 50
    max_tool_calls: int = 200
    max_lean_calls: int | None = None

    def __post_init__(self) -> None:
        if self.max_turns < 1:
            raise ValueError("max_turns must be positive")
        if self.max_tool_calls < 1:
            raise ValueError("max_tool_calls must be positive")
        if self.max_lean_calls is not None and self.max_lean_calls < 1:
            raise ValueError("max_lean_calls must be positive when set")


@dataclass(frozen=True)
class EpisodeConfig:
    """Versioned episode semantics shared by inference and RL."""

    budget: BudgetConfig = field(default_factory=BudgetConfig)
    guidance: GuidanceMode = "off"
    abort_on_infrastructure_error: bool = True
    max_tool_result_chars: int = 12_000
    schema_version: str = "dvp.episode.v12"
    policy_version: str = DVP_POLICY_VERSION
    enable_lean_retrieve: bool = True
    # Match mini-SWE-agent: stop after three consecutive malformed responses;
    # zero disables this limit without disabling the ordinary episode budgets.
    max_consecutive_format_errors: int = 3
    # Output truncation has its own recovery budget, separate from format errors.
    # Zero disables only this limit; the ordinary episode budgets still apply.
    max_consecutive_output_truncations: int = 5
    # Partial reasoning retention is opt-in. This is context retention, not
    # native token-level or signed-block continuation.
    retain_truncated_thinking: bool = False
    # Legacy keeps normalized assistant/tool exchanges even after rejected
    # actions. Resource limits remain independent of the history policy.
    history_policy: Literal["current", "legacy"] = "current"

    def __post_init__(self) -> None:
        if self.guidance not in {"off", "soft"}:
            raise ValueError(f"unsupported guidance mode: {self.guidance}")
        if self.max_tool_result_chars < 512:
            raise ValueError("max_tool_result_chars must be at least 512")
        if not self.policy_version.strip():
            raise ValueError("policy_version must not be empty")
        if not isinstance(self.enable_lean_retrieve, bool):
            raise ValueError("enable_lean_retrieve must be a boolean")
        if (
            type(self.max_consecutive_format_errors) is not int
            or self.max_consecutive_format_errors < 0
        ):
            raise ValueError("max_consecutive_format_errors must be a non-negative integer")
        if (
            type(self.max_consecutive_output_truncations) is not int
            or self.max_consecutive_output_truncations < 0
        ):
            raise ValueError("max_consecutive_output_truncations must be a non-negative integer")
        if not isinstance(self.retain_truncated_thinking, bool):
            raise ValueError("retain_truncated_thinking must be a boolean")
        if self.history_policy not in ("current", "legacy"):
            raise ValueError("history_policy must be current or legacy")

    @property
    def config_id(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class RuntimeConfig:
    """Filesystem paths and operator-supplied services for one episode.

    A configured Python executor is a trusted in-process adapter responsible
    for isolation preflight and execution. It is never selected by model input;
    leaving it unset installs no Python execution capability.
    """

    target_workspace: Path | None = None
    retrieve_index: Path | None = None
    lean_timeout: float = 60.0
    python_executor: PythonExecutor | None = None
