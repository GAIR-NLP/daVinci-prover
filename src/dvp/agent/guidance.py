"""Soft policy guidance rendered from episode evidence.

Guidance never authorizes an action or changes termination, reward, or budgets."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from dvp.verification.contracts import mask_lean_comments_and_strings
from dvp.verification.lean import contains_placeholder

from . import candidate
from .policy import _NAMED_AUXILIARY_DECLARATION_RE
from .verifier_evidence import _raw_hole_count

if TYPE_CHECKING:
    from .episode import ProverEpisode


def no_tool_feedback(finish_reason: Any) -> str:
    """Protocol feedback remains available even when soft guidance is off."""
    if finish_reason == "length":
        return (
            "The response reached the output token limit "
            "(finish_reason=length). Continue from saved files and tool results. "
            "Use smaller steps. Incomplete calls were not executed; resend them "
            "with complete JSON arguments."
        )
    if finish_reason == "tool_calls":
        return (
            "No parseable tool call was received; no tool was executed. "
            "Resend with an available tool name and a complete JSON argument object."
        )
    return (
        "No visible response or tool call was received. "
        "Continue with text or a tool call."
    )


def guidance(episode: ProverEpisode) -> list[str]:
    if episode.config.guidance == "off":
        return []
    guidance = base_guidance(episode)
    return [f"<harness_hint>\n{item}\n</harness_hint>" for item in guidance]


def base_guidance(episode: ProverEpisode) -> list[str]:
    state = episode._state
    if not state.last_outcomes:
        return []
    last = state.last_outcomes[-1]
    if state.submission_ready:
        return ["The exact target is verified and the episode is complete."]
    if last.error_kind in {"empty_response", "completion_incomplete"}:
        # The driver always emits the protocol feedback from this outcome.
        return []
    harness = (
        last.raw.get("_harness") if isinstance(last.raw.get("_harness"), dict) else {}
    )
    guidance = []
    if last.error_kind in {
        "infrastructure",
        "verifier_timeout",
    }:
        guidance.append(
            "This verifier call produced no proof evidence because the "
            "service failed, rate-limited, or timed out. The candidate was "
            "not logically rejected; retry after useful work."
        )
    latest_target = next(
        (
            outcome
            for outcome in reversed(state.last_outcomes)
            if isinstance(outcome.raw.get("_harness"), dict)
            and outcome.raw["_harness"].get("attempt_kind") == "exact_target"
        ),
        None,
    )
    if latest_target is not None:
        target_harness = latest_target.raw["_harness"]
        placeholder_count = target_harness.get("target_source_placeholders")
        elaborated_holes = _raw_hole_count(latest_target.raw)
        if (
            isinstance(placeholder_count, int)
            and not isinstance(placeholder_count, bool)
            and placeholder_count > 0
            and (elaborated_holes is None or elaborated_holes == 0)
        ):
            guidance.append(
                "The exact target still contains "
                f"{placeholder_count} explicit source placeholder(s), "
                "independently of `holes.total`. If an earlier primary "
                "compiler diagnostic blocks elaboration, repair it first; "
                "otherwise patch "
                "`source_placeholders.items[0].range` in the same target "
                "candidate."
            )
    latest_target_index = -1
    for index, outcome in enumerate(state.last_outcomes):
        outcome_harness = (
            outcome.raw.get("_harness")
            if isinstance(outcome.raw.get("_harness"), dict)
            else {}
        )
        if outcome_harness.get("attempt_kind") == "exact_target":
            latest_target_index = index
    boundary_lookup_count = sum(
        1
        for outcome in state.last_outcomes[latest_target_index + 1 :]
        if (
            isinstance(outcome.raw.get("_harness"), dict)
            and outcome.raw["_harness"].get("attempt_kind") == "lookup"
        )
    )
    if boundary_lookup_count > 0 and state.lookup_since_target == boundary_lookup_count:
        if state.last_target_turn is None:
            guidance.append(
                "No exact-target candidate has been checked yet. Use this "
                "evidence in `Submission.lean` on the next edit so "
                "the next lookup, lemma, or strategy change is grounded "
                "in an actual target diagnostic."
            )
        else:
            guidance.append(
                "This is the first lookup/read since the checked exact "
                "target. Prefer to try any relevant evidence in that "
                "target before broadening the lookup chain; if it is not "
                "relevant, refine the query or switch proof strategy."
            )
    if last.error_kind == "redundant_tactic":
        repair_hint = last.raw.get("repair_hint")
        source_range = (
            repair_hint.get("source_range") if isinstance(repair_hint, dict) else None
        )
        location = ""
        source_line_hint = ""
        if isinstance(source_range, dict):
            start = source_range.get("start")
            end = source_range.get("end")
            if isinstance(start, dict) and isinstance(start.get("line"), int):
                line_number = start["line"]
                location = f" at line {line_number}"
                if isinstance(start.get("column"), int):
                    location += f", column {start['column']}"
                if isinstance(end, dict):
                    location += " through " f"line {end.get('line', start['line'])}"
                    if isinstance(end.get("column"), int):
                        location += f", column {end['column']}"
                code = candidate.outcome_code(episode, last)
                if isinstance(code, str):
                    lines = code.splitlines()
                    if 1 <= line_number <= len(lines):
                        source_line = lines[line_number - 1].strip()
                        if source_line and len(source_line) <= 240:
                            source_line_hint = (
                                " The current source line is "
                                f"{json.dumps(source_line, ensure_ascii=False)}; "
                                "use this exact text in `file_edit` if "
                                "deletion is correct."
                            )
        guidance.append(
            "Lean's verifier errors are only redundant tactics "
            "(`no goals to be solved`). Delete the reported tactic"
            f"{location}, then verify the revised candidate."
            f"{source_line_hint} No edit or "
            "verification retry was applied automatically."
        )

    if (
        harness.get("attempt_kind") == "auxiliary"
        and last.valid
        and last.verified
        and harness.get("new_evidence")
    ):
        code = candidate.outcome_code(episode, last)
        handoff_note = auxiliary_handoff_note()
        if code and contains_placeholder(mask_lean_comments_and_strings(code)):
            guidance.append(
                "Lean elaborated this auxiliary candidate, but it contains "
                "a placeholder and is not reusable "
                "proof evidence."
            )
        elif code and re.search(
            r"(?m)^\s*example(?:\s|:)",
            mask_lean_comments_and_strings(code),
        ):
            guidance.append(
                "This anonymous probe verified. Prefer to apply its exact "
                "tactic or fact in Submission.lean before another "
                f"auxiliary probe. {handoff_note}"
            )
        elif code and _NAMED_AUXILIARY_DECLARATION_RE.search(
            mask_lean_comments_and_strings(code)
        ):
            guidance.append(
                "This auxiliary declaration verified. Prefer to apply it "
                "directly inside Submission.lean before another "
                f"auxiliary probe. {handoff_note}"
            )
        else:
            guidance.append(
                "This auxiliary check verified. Prefer to apply the useful "
                "fact inside Submission.lean before another "
                f"auxiliary probe. {handoff_note}"
            )

    if last.error_kind == "unknown_identifier":
        guidance.append(
            "Retrieve or inspect the exact missing declaration if useful, "
            "then revise the proof with the verified API."
        )
    if harness.get("attempt_kind") == "auxiliary" and candidate.is_permissive_draft(last):
        if last.valid:
            guidance.append(
                "This auxiliary draft only exposed assumed holes. Use it "
                "only if it resolves the current target diagnostic, then "
                "apply the result back to the exact target."
            )
        elif not last.valid:
            guidance.append(
                "No proof evidence was produced. Return to the target, "
                "simplify this focused probe, or switch strategy."
            )
    return guidance


def auxiliary_handoff_note() -> str:
    return (
        "Nothing is merged automatically. If it does not address the "
        "blocker, continue the direct proof, isolate a different lemma, "
        "or switch strategy."
    )
