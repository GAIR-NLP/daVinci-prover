"""Coalesce final Lean checks across consecutive file-tool calls in one action."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from dvp.protocol import ToolCall, ToolOutcome

from . import facts, outcomes

if TYPE_CHECKING:
    from .episode import ProverEpisode


def enabled(episode: ProverEpisode) -> bool:
    return all(
        callable(getattr(episode.tools, name, None))
        for name in (
            "begin_automatic_check_batch", "pending_automatic_check_paths",
            "finish_automatic_check_batch", "discard_automatic_check_batch",
        )
    )


def is_file_call(episode: ProverEpisode, call: ToolCall) -> bool:
    return episode.surface.resolve(call.name, call.arguments).backend_name in {
        "file_edit", "file_write",
    }


async def execute_group(
    episode: ProverEpisode, calls: list[ToolCall], *, output_truncated: bool,
) -> tuple[list[ToolOutcome], list[dict[str, Any]]]:
    """Save in order; attach the one grouped check to the last successful saver.

    Saves invalidate existing facts immediately. New facts are recorded only
    after the final bytes have been checked, avoiding intermediate proof claims
    or duplicate progress/error counters. A failed save retains its own result.
    """
    registry = episode.tools
    registry.begin_automatic_check_batch()
    records: list[tuple[ToolCall, ToolOutcome, list[dict[str, Any]]]] = []
    owner: int | None = None
    saved_files: dict[str, dict[str, Any]] = {}
    saving_calls: list[str] = []
    try:
        for call in calls:
            outcome, events = await episode._execute_call(
                call, output_truncated=output_truncated, defer_facts=True,
            )
            records.append((call, outcome, events))
            if outcome.executed and outcome.raw.get("file_saved") and outcome.raw.get("success"):
                owner = len(records) - 1
                saving_calls.append(call.id)
                for item in outcome.raw.get("files", []):
                    if isinstance(item, dict) and isinstance(item.get("file_path"), str):
                        saved_files[item["file_path"]] = item

        if episode._state.aborted:
            # A fatal save failure ends all execution. Earlier saves remain,
            # but must not start new verifier work after the abort.
            for index, (call, saved, events) in enumerate(records):
                if saved.raw.get("automatic_check_deferred"):
                    raw = {
                        **saved.raw, "automatic_check_deferred": False,
                        "automatic_check_skipped": "infrastructure_error",
                    }
                    records[index] = (
                        call, outcomes.normalize_outcome(
                            episode, call, episode.surface.resolve(call.name, call.arguments), raw,
                        ), events,
                    )
        elif owner is not None and registry.pending_automatic_check_paths():
            call, saved, events = records[owner]
            raw = dict(saved.raw)
            raw.pop("automatic_check_deferred", None)
            raw.pop("bytes", None)  # The primary checked file can differ from this call's file.
            raw.update(files=list(saved_files.values()), automatic_check_call_ids=saving_calls)

            def on_check() -> None:
                # Charge each attempt before its await, also on cancellation.
                episode._state.lean_calls += 1

            raw = await registry.finish_automatic_check_batch(raw, on_check=on_check)
            checked = raw.get("verification_count", 0)
            if isinstance(checked, int) and not isinstance(checked, bool) and checked > 0:
                for event in events:
                    if event["kind"] == "tool_execution":
                        event["lean_call"] = True
            dispatch = episode.surface.resolve(call.name, call.arguments)
            records[owner] = (call, outcomes.normalize_outcome(episode, call, dispatch, raw), events)
    finally:
        # CancelledError propagates. No detached task or deferred check survives.
        registry.discard_automatic_check_batch()

    final_outcomes: list[ToolOutcome] = []
    final_events: list[dict[str, Any]] = []
    for call, outcome, events in records:
        if outcome.executed:
            outcome = episode._record_executed_outcome(
                outcome, episode.surface.resolve(call.name, call.arguments),
            )
        else:
            facts.update_facts(episode, outcome)
        final_outcomes.append(outcome)
        final_events.extend(events)
    return final_outcomes, final_events
