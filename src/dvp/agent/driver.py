"""LLM generation loop around the shared prover episode state machine."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dvp.agent.episode import ProverEpisode
from dvp.llm_clients.parser import parse_assistant_action
from dvp.protocol import AssistantAction, EpisodeResult, Transition

from .messages import (
    _assistant_history_message,
    append_transition_messages,
    rejected_response_feedback,
    truncated_assistant_history_message,
)
from .policy import _FORMAT_ERROR_KINDS
from .prompts import initial_messages

if TYPE_CHECKING:
    from dvp.llm_clients.client import LiteLLMClient


@dataclass
class ProverDriver:
    """Drive ``ProverEpisode`` with a LiteLLM model client.

    Generation advances through ``episode.step``; ``episode.finalize`` grades
    saved sources after it stops. Replay and RL use the same two phases.
    """

    client: LiteLLMClient
    episode: ProverEpisode
    theorem_statement: str
    verbose: bool = True
    system_prompt: str | None = None
    user_prompt: str | None = None
    messages: list[dict[str, Any]] = field(default_factory=list, init=False)
    rejected_responses: list[dict[str, Any]] = field(default_factory=list, init=False)

    async def run(self) -> EpisodeResult:
        await self.run_rollout()
        return await self.episode.finalize()

    async def run_rollout(self) -> EpisodeResult:
        """Generate and execute tools without entering final acceptance."""
        preflight = getattr(self.episode.tools, "preflight", None)
        if callable(preflight):
            await preflight()
        observation = self.episode.reset()
        self.messages = self._initial_messages()
        self.rejected_responses = []

        while True:
            turn = observation.step_id + 1
            message = await self.client.chat(
                messages=self.messages,
                tools=self.episode.tool_schemas(),
                tool_choice="auto",
            )
            # Keep provider termination metadata in the replayable action, not
            # in chat history. It distinguishes truncated calls from prose.
            records = getattr(self.client, "generation_records", None)
            if isinstance(records, list) and records and isinstance(records[-1], dict):
                finish_reason = records[-1].get("finish_reason")
                if isinstance(finish_reason, str):
                    message = {**message, "finish_reason": finish_reason}
            action = parse_assistant_action(
                message,
                turn=turn,
            )
            self.messages.append(_assistant_history_message(action))

            if self.verbose and action.content:
                print(action.content)

            transition = await self.episode.step(action)
            format_errors = [
                outcome for outcome in transition.outcomes
                if outcome.error_kind in _FORMAT_ERROR_KINDS
            ]
            truncations = [
                outcome for outcome in transition.outcomes
                if outcome.error_kind == "output_truncated"
            ]
            selective_recovery = (
                self.episode.config.history_policy == "current"
                and bool(format_errors or truncations)
            )
            if selective_recovery:
                self.messages.pop()
                retained = None
                if truncations and not format_errors:
                    # Resume visible text and optionally thinking, never incomplete calls. A
                    # mixed batch's executed operations are reported below so
                    # the model sees their effects without orphan tool results.
                    retained = truncated_assistant_history_message(
                        action,
                        retain_thinking=self.episode.config.retain_truncated_thinking,
                    )
                    if retained is not None:
                        self.messages.append(retained)
                if (
                    retained is None or action.tool_calls
                    or (
                        action.reasoning_content
                        and retained.get("reasoning_content") != action.reasoning_content
                    )
                ):
                    self.rejected_responses.append({
                        "turn": turn,
                        "response": deepcopy(action.raw),
                        "errors": [
                            {"tool_call_id": outcome.call_id, "name": outcome.name,
                             "error_kind": outcome.error_kind, "feedback": outcome.feedback}
                            for outcome in (*format_errors, *truncations)
                        ],
                    })
                feedback = rejected_response_feedback(
                    action, transition,
                    max_chars=self.episode.config.max_tool_result_chars,
                    path_aliases=self._feedback_path_aliases(),
                )
            else:
                # Legacy retains the assistant message already appended above,
                # with normal paired results even for rejected tool calls.
                if (
                    not action.tool_calls
                    and not action.content.strip()
                    and (
                        self.episode.config.history_policy == "current"
                        or not action.reasoning_content.strip()
                    )
                ):
                    # A thinking-only stop gets one reminder without feeding its
                    # private text back into the next request. Legacy still keeps
                    # thinking, but no policy sends a completely empty entry.
                    self.messages.pop()
                    self.rejected_responses.append({
                        "turn": turn, "response": deepcopy(action.raw),
                        "errors": [
                            {"tool_call_id": outcome.call_id, "name": outcome.name,
                             "error_kind": outcome.error_kind, "feedback": outcome.feedback}
                            for outcome in transition.outcomes if outcome.error_kind
                        ] or [{"error_kind": "empty_response"}],
                    })
                feedback = self._append_transition(action, transition)
            observation = transition.next_observation

            if transition.terminated or transition.truncated or transition.aborted:
                if selective_recovery and feedback:
                    self.messages.append({"role": "user", "content": "\n".join(feedback)})
                return self.episode.result()

            state_feedback = "\n".join(
                text for item in observation.guidance if (text := item.strip())
            )
            if state_feedback:
                feedback.append(state_feedback)
            if feedback:
                self.messages.append({"role": "user", "content": "\n".join(feedback)})

    def _initial_messages(self) -> list[dict[str, Any]]:
        return initial_messages(
            self.theorem_statement,
            enable_lean_retrieve=self.episode.config.enable_lean_retrieve,
            enable_python_execute=getattr(self.episode.tools, "python_executor", None) is not None,
            system_prompt=self.system_prompt,
            user_prompt=self.user_prompt,
            task_contract=self.episode.task_contract,
        )

    def _append_transition(
        self,
        action: AssistantAction,
        transition: Transition,
    ) -> list[str]:
        return append_transition_messages(
            self.messages,
            action,
            transition,
            max_chars=self.episode.config.max_tool_result_chars,
            verbose=self.verbose,
            path_aliases=self._feedback_path_aliases(),
        )

    def _feedback_path_aliases(self) -> dict[str, str]:
        """Known diagnostic roots; source text and recorded evidence stay intact."""
        workspace = getattr(self.episode.tools, "workspace", None)
        root = getattr(workspace, "workspaces_dir", None)
        if root is None:
            root = getattr(workspace, "root", None)
        target = getattr(workspace, "target_workspace", None)
        aliases = {}
        for path, display in ((root, ""), (target, "target/")):
            if isinstance(path, (str, Path)) and str(path):
                aliases[str(path)] = display
        return aliases
