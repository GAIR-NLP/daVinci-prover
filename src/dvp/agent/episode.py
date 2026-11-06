"""One small environment shared by inference, replay, and RL.

There are intentionally no proof-failure phase switches or forced strategy
gates in this module. Consecutive malformed responses have a bounded retry
allowance. The episode enforces safety/resource invariants, executes
tools, turns their heterogeneous results into one protocol, and exposes
verified facts.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import uuid
from pathlib import Path
from typing import Any

from dvp.artifacts.trajectory import NullTrajectorySink, TrajectorySink, episode_event
from dvp.config import EpisodeConfig
from dvp.llm_clients.parser import looks_like_tool_call_text
from dvp.protocol import (
    AssistantAction,
    EpisodeResult,
    Observation,
    ToolCall,
    ToolOutcome,
    Transition,
)
from dvp.runtime.submission import SUBMISSION_PATH, seed_submission
from dvp.toolkits.files import automatic_check_paths
from dvp.toolkits.surface import CompatibilitySurface, Dispatch
from dvp.verification.comparator import ComparatorVerifier
from dvp.verification.lean import LeanVerifier

from . import automatic_checks, candidate, completion, facts, outcomes, state
from .messages import no_tool_feedback
from .policy import _FORMAT_ERROR_KINDS, _LEAN_BACKENDS, _TOOL_NAME_SET
from .state import EpisodeState


class ProverEpisode:
    """Deterministic transition layer around the existing trusted tools."""

    def __init__(
        self,
        *,
        tools: Any,
        contract_verifier: Any | None = None,
        theorem_statement: str,
        run_dir: Path,
        config: EpisodeConfig | None = None,
        sink: TrajectorySink | None = None,
        episode_id: str | None = None,
    ) -> None:
        self.tools = tools
        if contract_verifier is None:
            verifier = getattr(tools, "verifier", None)
            if not isinstance(verifier, LeanVerifier):
                raise ValueError(
                    "ProverEpisode requires a comparator verifier; "
                    "use build_episode/build_services or pass one explicitly"
                )
            contract_verifier = ComparatorVerifier(
                verifier, theorem_statement,
                task_contract=getattr(tools, "task_contract", None),
                artifact_dir=Path(run_dir) / "comparator_artifacts",
            )
        self.contract_verifier = contract_verifier
        self.task_contract = getattr(tools, "task_contract", None)
        self._source_theorem_statement = theorem_statement
        self.theorem_statement = theorem_statement.strip()
        self.run_dir = Path(run_dir)
        self.config = config or EpisodeConfig()
        self.sink: TrajectorySink = sink or NullTrajectorySink()
        self.episode_id = episode_id or uuid.uuid4().hex
        self.surface = CompatibilitySurface(
            tools,
            enable_lean_retrieve=self.config.enable_lean_retrieve,
        )
        self._state = EpisodeState()
        self._started = False
        self._sink_closed = False
        self._rollout_end_recorded = False
        self._finalization_task: asyncio.Task[EpisodeResult] | None = None
        self._initial_submission_path = seed_submission(
            getattr(self.tools, "workspace", None),
            self._source_theorem_statement,
        )

    def reset(self) -> Observation:
        """Start a fresh logical episode.

        A ``ProverEpisode`` is intentionally single-use after it reaches a
        terminal state.  Constructing a new object also guarantees a fresh
        workspace, which prevents cross-rollout leakage.
        """

        if self._started and (self._state.turns > 0 or self._state.done):
            raise RuntimeError(
                "ProverEpisode is single-use; build a new episode instead of resetting it"
            )
        # Reset establishes deterministic task-derived workspace state before
        # the first observation.  It performs no tool or verifier call.
        self._initial_submission_path = seed_submission(
            getattr(self.tools, "workspace", None),
            self._source_theorem_statement,
        )
        self._started = True
        self._state = EpisodeState()
        observation = self.observe()
        self._record(
            "start",
            {
                "config_id": self.config.config_id,
                "config": {
                    "policy_version": self.config.policy_version,
                    "budget": {
                        "max_turns": self.config.budget.max_turns,
                        "max_tool_calls": self.config.budget.max_tool_calls,
                        "max_lean_calls": self.config.budget.max_lean_calls,
                    },
                    "enable_lean_retrieve": self.config.enable_lean_retrieve,
                    "abort_on_infrastructure_error": self.config.abort_on_infrastructure_error,
                    "max_tool_result_chars": self.config.max_tool_result_chars,
                    "max_consecutive_format_errors": self.config.max_consecutive_format_errors,
                    "max_consecutive_output_truncations": self.config.max_consecutive_output_truncations,
                    "retain_truncated_thinking": self.config.retain_truncated_thinking,
                },
                # Retain the source bytes represented by the task string for
                # deterministic workspace replay. The observation keeps the
                # historical stripped contract value.
                "theorem_statement": self._source_theorem_statement,
                "initial_workspace": {
                    "solution_seed": (
                        {
                            "file_path": SUBMISSION_PATH,
                            "source": "theorem_statement",
                            "bytes": len(
                                self._source_theorem_statement.encode("utf-8")
                            ),
                            "sha256": hashlib.sha256(
                                self._source_theorem_statement.encode("utf-8")
                            ).hexdigest(),
                        }
                        if self._initial_submission_path is not None
                        else None
                    ),
                },
                "observation": observation.to_dict(),
            },
        )
        return observation

    def tool_schemas(self) -> list[dict[str, Any]]:
        return self.surface.schemas()

    def observe(self) -> Observation:
        return state.build_observation(
            self._state,
            self.config,
            episode_id=self.episode_id,
            theorem_statement=self.theorem_statement,
            tools=self.tool_schemas(),
        )

    async def step(self, action: AssistantAction) -> Transition:
        if not self._started:
            self.reset()
        if self._state.done:
            raise RuntimeError(f"episode already finished: {self._state.reason}")
        if not isinstance(action, AssistantAction):
            raise TypeError("action must be an AssistantAction")

        before = self.observe()
        self._state.turns += 1
        outcomes: list[ToolOutcome] = []
        events: list[dict[str, Any]] = []
        calls = list(action.tool_calls)
        output_truncated = action.raw.get("finish_reason") == "length"

        if not calls:
            outcomes, events = await self._handle_no_calls(action)
        else:
            index = 0
            while index < len(calls):
                if automatic_checks.enabled(self) and automatic_checks.is_file_call(self, calls[index]):
                    end = index + 1
                    while end < len(calls) and automatic_checks.is_file_call(self, calls[end]):
                        end += 1
                    group_outcomes, group_events = await automatic_checks.execute_group(
                        self, calls[index:end], output_truncated=output_truncated,
                    )
                    outcomes.extend(group_outcomes)
                    events.extend(group_events)
                    index = end
                else:
                    outcome, call_events = await self._execute_call(
                        calls[index], output_truncated=output_truncated,
                    )
                    outcomes.append(outcome)
                    events.extend(call_events)
                    index += 1

        self._state.last_outcomes = outcomes
        state.apply_format_error_limit(self._state, self.config)
        if self._state.reason == "repeated_format_error":
            events.append({
                "kind": "format_error_limit",
                "consecutive_format_errors": self._state.consecutive_format_errors,
                "limit": self.config.max_consecutive_format_errors,
            })
        state.apply_output_truncation_limit(self._state, self.config)
        if self._state.reason == "repeated_output_truncation":
            events.append({
                "kind": "output_truncation_limit",
                "consecutive_output_truncations": self._state.consecutive_output_truncations,
                "limit": self.config.max_consecutive_output_truncations,
            })
        state.apply_post_step_limits(self._state, self.config.budget)
        reward_components = {
            "verified_target": max(
                (item.reward for item in outcomes if item.terminal and item.verified),
                default=0.0,
            ),
        }
        reward = float(sum(reward_components.values()))
        after = self.observe()
        transition = Transition(
            schema_version=self.config.schema_version,
            episode_id=self.episode_id,
            step_id=self._state.turns,
            observation=before,
            action=action,
            outcomes=tuple(outcomes),
            next_observation=after,
            reward_components=reward_components,
            reward=reward,
            terminated=self._state.terminated,
            truncated=self._state.truncated,
            aborted=self._state.aborted,
            retryable_service_error=self._state.retryable_service_error,
            reason=self._state.reason,
            events=tuple(events),
        )
        self._record("transition", self._transition_trace_payload(transition))
        if self._state.done:
            self._record_rollout_end()
        return transition

    async def _handle_no_calls(
        self, action: AssistantAction,
    ) -> tuple[list[ToolOutcome], list[dict[str, Any]]]:
        """Separate ordinary completion from unfinished or malformed output."""
        finish_reason = action.raw.get("finish_reason")
        if finish_reason == "length":
            error_kind = "output_truncated"
            feedback = no_tool_feedback("length")
        elif finish_reason == "tool_calls" or action.raw.get("tool_calls"):
            error_kind = "tool_parse_error"
            feedback = no_tool_feedback("tool_calls")
        elif looks_like_tool_call_text(
            action.content if action.content.strip() else action.reasoning_content,
        ):
            error_kind = "tool_parse_error"
            feedback = (
                "Malformed tool-call text; no tool was executed. Resend through "
                "the tool interface with a valid JSON argument object. "
                f"{self._available_tools_feedback()}"
            )
        elif finish_reason not in (None, "stop"):
            error_kind = "response_format_error"
            feedback = (
                f"Unexpected response finish reason: {finish_reason!r}. "
                "Provide a visible response or a complete tool call."
            )
        elif action.content.strip():
            return await self._finish_model_response("model_finished")
        elif self._state.empty_response_prompted:
            return await self._finish_model_response("empty_response")
        else:
            self._state.empty_response_prompted = True
            error_kind = "empty_response"
            feedback = (
                "The response contained no visible text or tool call. "
                "Continue with a visible response or resend a complete tool call "
                "with a valid JSON argument object. "
                f"{self._available_tools_feedback()}"
            )

        outcome = self._rejected_outcome(
            call_id=f"no_tool_{self._state.turns}", name="<no_tool>",
            arguments={}, error_kind=error_kind, feedback=feedback,
        )
        facts.update_facts(self, outcome)
        return [outcome], [{
            "kind": (
                "output_truncated" if error_kind == "output_truncated" else
                "empty_response_prompt" if error_kind == "empty_response" else
                "invalid_action"
            ),
            "reason": error_kind,
        }]

    async def _finish_model_response(
        self, reason: str,
    ) -> tuple[list[ToolOutcome], list[dict[str, Any]]]:
        """A visible final reply ends generation; grading is a separate phase."""
        self.stop(reason)
        return [], [{"kind": "model_finished", "reason": reason}]

    def stop(self, reason: str, *, truncated: bool = False, aborted: bool = False) -> None:
        """Stop generation without spending a tool call or grading the submission."""
        if self._state.done:
            return
        self._state.terminated = not (truncated or aborted)
        self._state.truncated = truncated
        self._state.aborted = aborted
        self._state.reason = reason

    async def finalize(self) -> EpisodeResult:
        """Grade the final saved sources once, outside all generation budgets."""
        if not self._state.done:
            raise RuntimeError("Stop the rollout before final acceptance")
        if self._finalization_task is None:
            self._finalization_task = asyncio.create_task(self._finalize_once())
        # Cancellation propagates to the owned verifier task; never leave a
        # background grader writing into a closed trajectory or workspace.
        return await self._finalization_task

    async def _finalize_once(self) -> EpisodeResult:
        try:
            # An external deadline can stop generation between steps. Record
            # that boundary too, before the final verdict changes the reason.
            self._record_rollout_end()
            self._state.rollout_reason = self._state.reason
            if self._state.aborted:
                self._record("final_acceptance_skipped", {
                    "reason": "rollout_infrastructure_error",
                    "rollout_reason": self._state.rollout_reason,
                })
            else:
                final = await completion.finalize_saved_submission(self)
                self._state.final_tool_result = final
                self._state.reason = final["reason"]
                self._state.aborted = bool(final.get("_harness_infrastructure_error"))
                self._state.terminated = not self._state.aborted
                self._state.truncated = False
                self._state.exact_target_verified = bool(final["success"])
                self._state.submission_ready = bool(final["success"])
                self._record("final_acceptance", final)
            self._state.finalized = True
            result = self.result()
            self._record("end", result.to_dict())
            return result
        except asyncio.CancelledError:
            self._state.aborted = True
            self._state.terminated = self._state.truncated = False
            self._state.reason = "cancelled"
            self._record("final_acceptance_cancelled", {"reason": "cancelled"})
            raise
        finally:
            self._close_sink()

    def _record_rollout_end(self) -> None:
        if not self._rollout_end_recorded:
            self._record("rollout_end", self.result().to_dict())
            self._rollout_end_recorded = True

    async def _execute_call(
        self, call: ToolCall, *, output_truncated: bool = False,
        defer_facts: bool = False,
    ) -> tuple[ToolOutcome, list[dict[str, Any]]]:
        if self._state.done:
            outcome = self._rejected_outcome(
                call_id=call.id, name=call.name, arguments=call.arguments,
                error_kind="episode_finished",
                feedback="A previous call in this action finished the episode.",
            )
            events = []
        elif self._state.tool_calls >= self.config.budget.max_tool_calls:
            outcome = self._rejected_outcome(
                call_id=call.id, name=call.name, arguments=call.arguments,
                error_kind="tool_budget_exhausted", feedback="Tool-call budget exhausted.",
            )
            events = []
        else:
            outcome, events = await self._execute(
                call, output_truncated=output_truncated, defer_facts=defer_facts,
            )
        if not outcome.executed and not defer_facts:
            facts.update_facts(self, outcome)
        return outcome, events

    async def _execute(
        self, call: ToolCall, *, output_truncated: bool = False,
        defer_facts: bool = False,
    ) -> tuple[ToolOutcome, list[dict[str, Any]]]:
        if call.argument_parse_error:
            # Only a JSON decoding failure from our parser can be caused by a
            # cut-off argument string. Complete non-object JSON and unavailable
            # tools are still format errors, even if the response hit its cap.
            truncated_arguments = (
                output_truncated
                and call.argument_parse_error.startswith("Invalid tool arguments JSON:")
                and self._capability_error(self.surface.resolve(call.name, call.arguments)) is None
            )
            error_kind = "output_truncated" if truncated_arguments else "tool_parse_error"
            return self._rejected_outcome(
                call_id=call.id,
                name=call.name,
                arguments=call.arguments,
                error_kind=error_kind,
                feedback=(
                    f"{call.argument_parse_error} This tool call was not executed. "
                    "Resend a complete JSON object with all required arguments and full code or file content."
                ),
            ), [
                {
                    "kind": "rejected_tool_call",
                    "call_id": call.id,
                    "reason": error_kind,
                }
            ]
        dispatch = self.surface.resolve(call.name, call.arguments)
        policy_error = self._capability_error(dispatch)
        if policy_error is not None:
            return self._rejected_outcome(
                call_id=call.id,
                name=call.name,
                backend_name=dispatch.backend_name,
                arguments=call.arguments,
                error_kind="capability_disabled",
                feedback=policy_error,
            ), [
                {
                    "kind": "rejected_tool_call",
                    "call_id": call.id,
                    "reason": policy_error,
                }
            ]
        lean_call = dispatch.backend_name in _LEAN_BACKENDS
        automatic_file_check = (
            automatic_checks.enabled(self)
            and dispatch.backend_name in {"file_write", "file_edit"}
        )
        if automatic_file_check:
            planned_paths = automatic_check_paths(
                self.tools, dispatch.backend_name, dispatch.backend_arguments,
            )
            pending = self.tools.pending_automatic_check_paths() if defer_facts else ()
            planned_lean_calls = len(set(pending).union(planned_paths))
        else:
            planned_lean_calls = int(lean_call)
        lean_limit = self.config.budget.max_lean_calls
        if (
            planned_lean_calls
            and lean_limit is not None
            and self._state.lean_calls + planned_lean_calls > lean_limit
        ):
            return self._rejected_outcome(
                call_id=call.id,
                name=call.name,
                backend_name=dispatch.backend_name,
                arguments=call.arguments,
                error_kind="lean_budget_exhausted",
                feedback="Lean verification budget exhausted.",
            ), [
                {
                    "kind": "rejected_tool_call",
                    "call_id": call.id,
                    "reason": "lean_budget_exhausted",
                }
            ]

        self._state.tool_calls += 1
        if lean_call:
            self._state.lean_calls += 1
        try:
            raw = await self.tools.call(
                dispatch.backend_name, dispatch.backend_arguments
            )
        except (ValueError, TypeError, FileNotFoundError, re.error) as exc:
            raw = {
                "success": False,
                "stop": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
        except (
            Exception
        ) as exc:  # A fake/custom registry may not wrap transport errors.
            raw = {
                "success": False,
                "stop": False,
                "_harness_infrastructure_error": True,
                "error": (
                    "Tool infrastructure exception: " f"{type(exc).__name__}: {exc}"
                ),
            }
        if not isinstance(raw, dict):
            raw = {
                "success": False,
                "stop": False,
                "_harness_infrastructure_error": True,
                "error": (
                    "Tool infrastructure protocol error: returned non-object "
                    f"result: {type(raw).__name__}"
                ),
            }

        if automatic_file_check:
            # Reserve room for every final destination before saving, but only
            # charge checks actually attempted (an invalid batch checks none).
            checked = raw.get("verification_count", 0)
            if isinstance(checked, int) and not isinstance(checked, bool) and checked > 0:
                self._state.lean_calls += checked
                lean_call = True

        outcome = outcomes.normalize_outcome(self, call, dispatch, raw)
        facts.invalidate_saved_sources(self, outcome)
        if not defer_facts:
            outcome = self._record_executed_outcome(outcome, dispatch)
        elif outcome.infrastructure_error and self.config.abort_on_infrastructure_error:
            self._state.aborted = True
            self._state.reason = str(outcome.raw.get("_harness_reason") or "infrastructure_error")
        events = [
            {
                "kind": "tool_execution",
                "call_id": call.id,
                "name": call.name,
                "backend_name": dispatch.backend_name,
                "lean_call": lean_call,
            }
        ]
        return outcome, events

    def _record_executed_outcome(self, outcome: ToolOutcome, dispatch: Dispatch) -> ToolOutcome:
        """Apply final evidence once, after a deferred group check if necessary."""
        outcome = facts.annotate_outcome(self, outcome, dispatch)
        facts.update_facts(self, outcome)

        # An executed tool, including a final automatic check with ordinary
        # Lean errors, gives the model new feedback between empty replies.
        # Malformed or rejected calls do not replenish the reminder allowance.
        if outcome.executed and outcome.error_kind not in _FORMAT_ERROR_KINDS:
            self._state.empty_response_prompted = False

        if outcome.infrastructure_error and self.config.abort_on_infrastructure_error:
            self._state.aborted = True
            self._state.reason = str(
                outcome.raw.get("_harness_reason") or "infrastructure_error"
            )
        return outcome

    def _available_tools_feedback(self) -> str:
        names = ", ".join(schema["function"]["name"] for schema in self.tool_schemas())
        return f"Available tools: {names or '(none)'}."

    def _capability_error(self, dispatch: Dispatch) -> str | None:
        name = dispatch.backend_name
        if name == "python_execute" and getattr(self.tools, "python_executor", None) is None:
            return "python_execute requires a configured sandbox executor. Use an available tool."
        if name == "lean_retrieve" and not self.config.enable_lean_retrieve:
            return "lean_retrieve is disabled for this episode. Use an available tool."
        if name not in _TOOL_NAME_SET:
            return (
                f"Unknown tool {name!r}. This tool call was not executed. "
                f"{self._available_tools_feedback()} "
                "Use an available name and its argument schema."
            )
        return None

    @staticmethod
    def _rejected_outcome(
        *,
        call_id: str,
        name: str,
        arguments: dict[str, Any],
        error_kind: str,
        feedback: str,
        backend_name: str | None = None,
    ) -> ToolOutcome:
        return ToolOutcome(
            call_id=call_id,
            name=name,
            backend_name=backend_name,
            arguments=dict(arguments),
            executed=False,
            valid=False,
            verified=None,
            terminal=False,
            reward=0.0,
            error_kind=error_kind,
            feedback=feedback,
            raw={
                "success": False,
                "stop": False,
                "error": feedback,
                "_harness": facts.harness_metadata(new_evidence=False),
            },
        )

    def result(self) -> EpisodeResult:
        return state.build_result(self._state, self.episode_id)

    @staticmethod
    def _transition_trace_payload(transition: Transition) -> dict[str, Any]:
        """Deduplicate constant schemas while keeping the trace replayable.

        Tool schemas are recorded in the start observation. Repeating the same
        (often large) schema twice per step is a material I/O cost in rollout
        workers, so transition observations refer back to that immutable copy.
        """

        payload = transition.to_dict()
        for key in ("observation", "next_observation"):
            observation = payload.get(key)
            if isinstance(observation, dict):
                observation["tools"] = []
                observation["tools_ref"] = "start.payload.observation.tools"
        return payload

    def _record(self, event: str, payload: dict[str, Any]) -> None:
        self.sink.record(
            episode_event(
                schema_version=self.config.schema_version,
                episode_id=self.episode_id,
                event=event,
                step_id=self._state.turns,
                payload=payload,
            )
        )

    def _close_sink(self) -> None:
        if not self._sink_closed:
            close = getattr(self.sink, "close", None)
            if callable(close):
                close()
            self._sink_closed = True
