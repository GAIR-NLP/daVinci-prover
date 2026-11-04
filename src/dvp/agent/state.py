"""Episode state, observation/result projections, and post-step resource limits."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from dvp.config import BudgetConfig, EpisodeConfig
from dvp.protocol import EpisodeResult, Observation, ToolOutcome

from .policy import _FORMAT_ERROR_KINDS


@dataclass
class EpisodeState:
    turns: int = 0
    tool_calls: int = 0
    lean_calls: int = 0
    contract_lean_calls: int = 0
    consecutive_format_errors: int = 0
    consecutive_output_truncations: int = 0
    empty_response_prompted: bool = False
    rollout_reason: str | None = None
    finalized: bool = False
    verified_artifacts: dict[str, dict[str, Any]] = field(default_factory=dict)
    exact_target_verified: bool = False
    submission_ready: bool = False
    error_counts: dict[str, int] = field(default_factory=dict)
    last_error_kind: str | None = None
    last_attempt_kind: str | None = None
    last_target_path: str | None = None
    last_target_turn: int | None = None
    last_target_error_kind: str | None = None
    last_target_error: str | None = None
    last_target_candidate_hash: str | None = None
    last_target_source_placeholders: int | None = None
    best_target_source_placeholders: int | None = None
    target_attempts: int = 0
    strict_attempts: int = 0
    last_strict_turn: int | None = None
    helper_since_target: int = 0
    lookup_since_target: int = 0
    lookup_since_strict: int = 0
    last_progress_turn: int = 0
    last_outcomes: list[ToolOutcome] = field(default_factory=list)
    terminated: bool = False
    truncated: bool = False
    aborted: bool = False
    reason: str | None = None
    final_tool_result: dict[str, Any] | None = None

    @property
    def done(self) -> bool:
        return self.terminated or self.truncated or self.aborted

    @property
    def retryable_service_error(self) -> bool:
        return self.aborted and self.reason not in {
            "comparator_timeout", "comparator_infrastructure_error",
        }


def build_observation(
    state: EpisodeState,
    config: EpisodeConfig,
    *,
    episode_id: str,
    theorem_statement: str,
    tools: list[dict[str, Any]],
    guidance: list[str],
) -> Observation:
    budget = config.budget
    lean_remaining = (
        None
        if budget.max_lean_calls is None
        else max(0, budget.max_lean_calls - state.lean_calls)
    )
    return Observation(
        schema_version=config.schema_version,
        episode_id=episode_id,
        step_id=state.turns,
        task={
            "theorem_statement": theorem_statement,
            "theorem_hash": hashlib.sha256(
                theorem_statement.encode("utf-8")
            ).hexdigest(),
        },
        state={
            "turns_used": state.turns,
            "tool_calls_used": state.tool_calls,
            "lean_calls_used": state.lean_calls,
            "contract_lean_calls_used": state.contract_lean_calls,
            "consecutive_format_errors": state.consecutive_format_errors,
            "max_consecutive_format_errors": config.max_consecutive_format_errors,
            "consecutive_output_truncations": state.consecutive_output_truncations,
            "empty_response_prompted": state.empty_response_prompted,
            "rollout_reason": state.rollout_reason,
            "finalized": state.finalized,
            "max_consecutive_output_truncations": config.max_consecutive_output_truncations,
            "retain_truncated_thinking": config.retain_truncated_thinking,
            "history_policy": config.history_policy,
            "remaining": {
                "turns": max(0, budget.max_turns - state.turns),
                "tool_calls": max(0, budget.max_tool_calls - state.tool_calls),
                "lean_calls": lean_remaining,
            },
            "verified_artifacts": list(state.verified_artifacts.values()),
            "exact_target_verified": state.exact_target_verified,
            "submission_ready": state.submission_ready,
            "last_error_kind": state.last_error_kind,
            "error_counts": dict(state.error_counts),
            "last_attempt_kind": state.last_attempt_kind,
            "last_target_path": state.last_target_path,
            "last_target_turn": state.last_target_turn,
            "last_target_error_kind": state.last_target_error_kind,
            "last_target_error": state.last_target_error,
            "last_target_candidate_hash": state.last_target_candidate_hash,
            "last_target_source_placeholders": (state.last_target_source_placeholders),
            "best_target_source_placeholders": (state.best_target_source_placeholders),
            "target_attempts": state.target_attempts,
            "strict_attempts": state.strict_attempts,
            "last_strict_turn": state.last_strict_turn,
            "helper_since_target": state.helper_since_target,
            "lookup_since_target": state.lookup_since_target,
            "lookup_since_strict": state.lookup_since_strict,
            "turns_since_progress": max(
                0,
                state.turns - state.last_progress_turn,
            ),
            "terminated": state.terminated,
            "truncated": state.truncated,
            "aborted": state.aborted,
            "reason": state.reason,
            "config_id": config.config_id,
        },
        tools=tools,
        guidance=guidance,
        last_outcomes=[item.to_dict() for item in state.last_outcomes],
    )


def build_result(state: EpisodeState, episode_id: str) -> EpisodeResult:
    final = state.final_tool_result
    success = bool(
        state.terminated and final and float(final.get("score") or 0.0) == 1.0
    )
    message = (
        str(final.get("message"))
        if final and final.get("message")
        else state.reason or "episode in progress"
    )
    return EpisodeResult(
        success=success,
        score=1.0 if success else 0.0,
        message=message,
        episode_id=episode_id,
        turns=state.turns,
        tool_calls=state.tool_calls,
        lean_calls=state.lean_calls,
        terminated=state.terminated,
        truncated=state.truncated,
        aborted=state.aborted,
        retryable_service_error=not success and state.retryable_service_error,
        reason=state.reason,
        rollout_reason=state.rollout_reason,
        proof_code=(
            str(final.get("proof_code")) if final and final.get("proof_code") else None
        ),
        final_tool_result=dict(final) if final else None,
    )


def apply_format_error_limit(state: EpisodeState, config: EpisodeConfig) -> None:
    """Count malformed responses, resetting on any step with valid format.

    A batch counts once even if several calls are malformed. Output truncation
    alone neither increments nor clears an unresolved format-error streak.
    Tool execution errors (including Lean rejection) are clean steps for this
    counter.
    Existing final acceptance or infrastructure outcomes take precedence.
    """
    if state.done:
        return
    if any(item.error_kind in _FORMAT_ERROR_KINDS for item in state.last_outcomes):
        state.consecutive_format_errors += 1
    elif not any(item.error_kind == "output_truncated" for item in state.last_outcomes):
        state.consecutive_format_errors = 0
    limit = config.max_consecutive_format_errors
    if limit > 0 and state.consecutive_format_errors >= limit:
        state.terminated = True
        state.reason = "repeated_format_error"


def apply_output_truncation_limit(state: EpisodeState, config: EpisodeConfig) -> None:
    """Bound unfinished output independently of tool-format and proof failures."""
    if any(item.error_kind == "output_truncated" for item in state.last_outcomes):
        state.consecutive_output_truncations += 1
    else:
        state.consecutive_output_truncations = 0
    # Record the last response even when acceptance or another failure ended
    # the episode, but never override that outcome with a resource limit.
    if state.done:
        return
    limit = config.max_consecutive_output_truncations
    if limit > 0 and state.consecutive_output_truncations >= limit:
        state.truncated = True
        state.reason = "repeated_output_truncation"


def apply_post_step_limits(state: EpisodeState, budget: BudgetConfig) -> None:
    # Terminal or infrastructure outcome wins over horizon truncation.  This
    # is deliberately post-step so verification on the final allowed turn runs.
    if state.done:
        return
    if state.tool_calls >= budget.max_tool_calls:
        state.truncated = True
        state.reason = "tool_budget_exhausted"
    elif (
        budget.max_lean_calls is not None and state.lean_calls >= budget.max_lean_calls
    ):
        state.truncated = True
        state.reason = "lean_budget_exhausted"
    elif state.turns >= budget.max_turns:
        state.truncated = True
        state.reason = "max_turns"
