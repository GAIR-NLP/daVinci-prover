"""Assistant history normalization and paired tool-result messages."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Mapping

from dvp.llm_clients.parser import assistant_action_to_history
from dvp.protocol import AssistantAction, Transition, decode_tool_arguments

from .guidance import no_tool_feedback
from .policy import _FORMAT_ERROR_KINDS
from .result_projection import _outcome_for_model


def _assistant_history_message(action: AssistantAction) -> dict[str, Any]:
    """Preserve sampled fields while keeping invalid arguments out of replay."""
    raw = action.raw
    if isinstance(raw, Mapping) and raw:
        native = raw.get("tool_calls")
        if native is not None and not isinstance(native, list):
            return assistant_action_to_history(action)
        if (
            isinstance(native, list)
            and native
            and not _native_history_matches(native, action)
        ):
            return assistant_action_to_history(action)
        if raw.get("function_call") and not action.tool_calls:
            return assistant_action_to_history(action)
        message: dict[str, Any] = {"role": "assistant"}
        for key in ("content", "reasoning_content", "thinking_blocks", "provider_specific_fields", "tool_calls", "function_call"):
            if key in raw:
                message[key] = deepcopy(raw[key])
        if isinstance(native, list) and native:
            for raw_call in message["tool_calls"]:
                function = raw_call["function"]
                _, parse_error = decode_tool_arguments(function.get("arguments"))
                if parse_error:
                    # The matching tool result explains this rejected call.
                    # Keep the malformed original only in action.raw, since
                    # some providers parse arguments again on the next turn.
                    function["arguments"] = "{}"
        if len(message) > 1:
            return message
    return assistant_action_to_history(action)


def _native_history_matches(
    native: list[Any],
    action: AssistantAction,
) -> bool:
    """Require a legal one-to-one tool/result pairing before raw replay."""

    if len(native) != len(action.tool_calls):
        return False
    for raw_call, parsed in zip(native, action.tool_calls):
        if parsed.synthetic or not isinstance(raw_call, Mapping):
            return False
        function = raw_call.get("function")
        if (
            not isinstance(function, Mapping)
            or not isinstance(function.get("name"), str)
            or not function["name"].strip()
            or ("type" in raw_call and raw_call["type"] != "function")
        ):
            return False
        raw_id = raw_call.get("id")
        if not raw_id or str(raw_id) != parsed.id:
            return False
    return True


def _has_degenerate_character_run(text: str) -> bool:
    """Recognize extreme character loops without classifying mathematical prose.

    Only thousands of identical non-whitespace characters count. Ordinary long
    derivations, indentation, and repeated proof attempts are not filtered.
    """
    previous = ""
    run = 0
    for character in text:
        run = run + 1 if character == previous else 1
        if run >= 4096 and not character.isspace():
            return True
        previous = character
    return False


def truncated_assistant_history_message(
    action: AssistantAction, *, retain_thinking: bool,
) -> dict[str, Any] | None:
    """Keep visible output and optional thinking, never incomplete tool calls."""
    retain_thinking = retain_thinking and not _has_degenerate_character_run(
        action.reasoning_content,
    )
    if not (action.content.strip() or (retain_thinking and action.reasoning_content.strip())):
        return None
    message: dict[str, Any] = {"role": "assistant", "content": action.content}
    if retain_thinking and action.reasoning_content:
        message["reasoning_content"] = action.reasoning_content
    return message


def rejected_response_feedback(
    action: AssistantAction,
    transition: Transition,
    *,
    max_chars: int,
    path_aliases: Mapping[str, str] | None = None,
) -> list[str]:
    """Describe rejected calls without replaying invalid tool-call messages.

    DVP executes calls sequentially, so a mixed batch can have real effects
    before or after a rejected call. Report all outcomes as user feedback;
    include the arguments of executed, well-formed calls to preserve that
    context without orphan tool messages or fabricated assistant messages.
    """
    if not action.tool_calls:
        return [
            outcome.feedback[:max_chars]
            for outcome in transition.outcomes
            if outcome.error_kind in {"empty_response", "output_truncated", "tool_parse_error", "response_format_error"} and outcome.feedback
        ]
    results = []
    for outcome in transition.outcomes:
        result = {
            "tool_call_id": outcome.call_id,
            "name": outcome.name,
            "executed": outcome.executed,
            "error_kind": outcome.error_kind,
            "result": _outcome_for_model(
                outcome, max_chars=max_chars, path_aliases=path_aliases,
            ),
        }
        if outcome.executed and outcome.error_kind not in _FORMAT_ERROR_KINDS:
            result["arguments"] = deepcopy(outcome.arguments)
        results.append(result)
    if any(outcome.error_kind in _FORMAT_ERROR_KINDS for outcome in transition.outcomes):
        prefix = (
            "Tool format error. Correct the rejected calls below. "
            "Each result shows whether its call ran.\nTOOL_RESULTS "
        )
    else:
        prefix = "Tool call results:\nTOOL_RESULTS "
    feedback = [
        prefix
        + json.dumps(results, ensure_ascii=False, default=str, sort_keys=True,
                     separators=(",", ":"))
    ]
    if action.raw.get("finish_reason") == "length":
        feedback.append(no_tool_feedback("length"))
    return feedback


def append_transition_messages(
    messages: list[dict[str, Any]],
    action: AssistantAction,
    transition: Transition,
    *,
    max_chars: int,
    verbose: bool = False,
    path_aliases: Mapping[str, str] | None = None,
) -> list[str]:
    """Append paired tool results and return feedback for synthetic calls."""
    outcomes = transition.outcomes
    if not action.tool_calls:
        # No tool-result message can be paired with this response. Return the
        # correction/continuation as user feedback, independently of soft hints.
        feedback = [
            outcome.feedback[:max_chars]
            for outcome in outcomes
            if outcome.error_kind in {"empty_response", "completion_incomplete", "output_truncated", "tool_parse_error", "response_format_error"} and outcome.feedback
        ]
        if any(outcome.error_kind == "completion_incomplete" for outcome in outcomes):
            # A final check is a harness action, not a model tool call. Report
            # its real result without fabricating an assistant call or an
            # orphan role=tool message. Preserve structured compiler diagnostics.
            checked = [
                {
                    "tool_call_id": outcome.call_id,
                    "name": outcome.name,
                    "arguments": outcome.arguments,
                    "executed": outcome.executed,
                    "result": _outcome_for_model(
                        outcome, max_chars=max_chars, path_aliases=path_aliases,
                    ),
                }
                for outcome in outcomes if outcome.executed
            ]
            if checked:
                feedback.append(
                    "Automatic check results:\nTOOL_RESULTS "
                    + json.dumps(checked, ensure_ascii=False, default=str,
                                 sort_keys=True, separators=(",", ":"))
                )
        return feedback
    synthetic_results: list[dict[str, Any]] = []
    for index, call in enumerate(action.tool_calls):
        outcome = outcomes[index] if index < len(outcomes) else None
        payload = _outcome_for_model(
            outcome,
            max_chars=max_chars,
            tool_name=call.name,
            path_aliases=path_aliases,
        )
        if call.synthetic:
            synthetic_results.append(
                {
                    "tool_call_id": call.id,
                    "name": call.name,
                    "result": payload,
                }
            )
        else:
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "name": call.name,
                    "content": json.dumps(
                        payload,
                        ensure_ascii=False,
                        default=str,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                }
            )
        if verbose:
            success = payload.get("success", payload.get("verified"))
            terminal = payload.get("stop", payload.get("terminal", False))
            print(f"[tool:{call.name}] success={success} terminal={terminal}")
    feedback: list[str] = []
    if synthetic_results:
        feedback.append(
            "TOOL_RESULTS "
            + json.dumps(
                synthetic_results,
                ensure_ascii=False,
                default=str,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    if action.raw.get("finish_reason") == "length" and any(
        outcome.error_kind in _FORMAT_ERROR_KINDS
        for outcome in outcomes
    ):
        feedback.append(no_tool_feedback("length"))
    return feedback
