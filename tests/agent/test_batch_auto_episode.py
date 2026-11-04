"""Batch checks preserve mutation, budget, and final acceptance boundaries."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dvp.config import BudgetConfig, EpisodeConfig
from dvp.toolkits.surface import CompatibilitySurface
from test_format_error_limit import CANDIDATE, STATEMENT, action, make_episode, native_call


HELPER = "theorem helper : True := by trivial\n"


def batch_episode(tmp_path, *, limit=None):
    return make_episode(tmp_path, config=EpisodeConfig(
        budget=BudgetConfig(max_lean_calls=limit),
    ))


def step(episode, name, args):
    return asyncio.run(episode.step(action(native_call(name, args))))


def seed_helper(episode):
    path = episode.tools.workspace.resolve("Submission/Helper.lean", for_write=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(HELPER)
    return path


def two_files():
    return {"edits": [
        {"old_str": "sorry", "new_str": "trivial"},
        {"file_path": "Submission/Helper.lean", "old_str": "trivial", "new_str": "exact True.intro"},
    ]}


@pytest.mark.parametrize("tool_limit", [200, 5])
def test_six_independent_edits_run_in_order_up_to_total_tool_budget(tmp_path, tool_limit):
    episode, verifier, comparator, _ = make_episode(tmp_path, config=EpisodeConfig(
        budget=BudgetConfig(max_tool_calls=tool_limit),
    ))
    episode.reset()
    path = episode.tools.workspace.submission_file
    path.write_text(CANDIDATE + "-- step0\n")
    checked_sources = []

    async def check(saved_path, **kwargs):
        checked_sources.append(saved_path.read_text())
        return {"success": True, "errors": []}

    verifier.verify_file.side_effect = check
    result = asyncio.run(episode.step(action(*[
        native_call("file_edit", {
            "file_path": "Submission.lean",
            "old_str": f"-- step{i}", "new_str": f"-- step{i + 1}",
        }, f"edit{i}")
        for i in range(6)
    ])))

    executed = min(6, tool_limit)
    assert checked_sources == [CANDIDATE + f"-- step{executed}\n"]
    assert path.read_text() == checked_sources[-1]
    assert [outcome.call_id for outcome in result.outcomes] == [f"edit{i}" for i in range(6)]
    assert all(outcome.executed and outcome.valid for outcome in result.outcomes[:executed])
    assert episode.result().tool_calls == executed
    assert episode.result().lean_calls == 1
    assert result.outcomes[executed - 1].raw["verification_count"] == 1
    assert result.truncated is (tool_limit == 5)
    if tool_limit == 5:
        assert result.outcomes[-1].error_kind == "tool_budget_exhausted"
        assert not result.outcomes[-1].executed
    comparator.verify.assert_not_awaited()


def test_intermediate_invalid_text_is_never_checked_and_comparator_still_required(tmp_path):
    episode, verifier, comparator, _ = batch_episode(tmp_path, limit=2)

    async def check(path, **kwargs):
        assert path.read_text() == CANDIDATE
        return {"success": True, "errors": []}

    verifier.verify_file.side_effect = check
    result = step(episode, "file_edit", {"edits": [
        {"old_str": "sorry", "new_str": "invalid_intermediate_tactic"},
        {"file_path": "Submission.lean/.", "old_str": "invalid_intermediate_tactic", "new_str": "trivial"},
    ]})
    verifier.verify_file.assert_awaited_once()
    comparator.verify.assert_not_awaited()
    assert not result.terminated and not episode.result().success
    episode.stop("model_finished")
    assert asyncio.run(episode.finalize()).success
    comparator.verify.assert_awaited_once()
    assert comparator.verify.await_args.args == (CANDIDATE,)
    assert result.outcomes[0].raw["verification_count"] == 1
    assert episode.result().lean_calls == 2
    assert next(event for event in result.events if event["kind"] == "tool_execution")["lean_call"] is True


def test_all_files_saved_before_any_check_and_target_selected_independent_of_order(tmp_path):
    episode, verifier, comparator, _ = batch_episode(tmp_path, limit=3)
    helper = seed_helper(episode)

    async def check(path, **kwargs):
        assert episode.tools.workspace.submission_file.read_text() == CANDIDATE
        assert "exact True.intro" in helper.read_text()
        return {"success": True, "errors": []}

    verifier.verify_file.side_effect = check
    args = two_files()
    args["edits"].reverse()
    result = step(episode, "file_edit", args)
    assert verifier.verify_file.await_count == 2
    assert result.outcomes[0].raw["file_path"] == "Submission.lean"
    comparator.verify.assert_not_awaited()
    episode.stop("model_finished")
    assert asyncio.run(episode.finalize()).success
    comparator.verify.assert_awaited_once()
    assert episode.result().lean_calls == 3


def test_whole_batch_budget_reserved_before_any_save(tmp_path):
    episode, verifier, comparator, _ = batch_episode(tmp_path, limit=1)
    helper = seed_helper(episode)
    result = step(episode, "file_edit", two_files())
    assert result.outcomes[0].error_kind == "lean_budget_exhausted"
    assert episode.tools.workspace.submission_file.read_text() == STATEMENT
    assert helper.read_text() == HELPER
    assert episode.result().lean_calls == 0
    verifier.verify_file.assert_not_awaited()
    comparator.verify.assert_not_awaited()


def test_invalid_later_edit_saves_nothing_and_consumes_no_lean_budget(tmp_path):
    episode, verifier, comparator, _ = batch_episode(tmp_path, limit=3)
    helper = seed_helper(episode)
    args = two_files()
    args["edits"][1]["old_str"] = "missing_text"
    result = step(episode, "file_edit", args)
    assert not result.outcomes[0].valid
    assert episode.tools.workspace.submission_file.read_text() == STATEMENT
    assert helper.read_text() == HELPER
    assert episode.result().lean_calls == 0
    verifier.verify_file.assert_not_awaited()
    comparator.verify.assert_not_awaited()


def test_failed_helper_blocks_acceptance_even_when_target_compiles(tmp_path):
    episode, verifier, comparator, _ = batch_episode(tmp_path)
    helper = seed_helper(episode)

    async def check(path, **kwargs):
        if path == helper:
            return {"success": False, "errors": ["unsolved goals"]}
        return {"success": True, "errors": []}

    verifier.verify_file.side_effect = check
    result = step(episode, "file_edit", two_files())
    outcome = result.outcomes[0]
    assert not outcome.valid and outcome.verified is False
    assert outcome.raw["file_path"] == "Submission/Helper.lean"
    assert outcome.raw["file_saved"]
    assert episode.tools.workspace.submission_file.read_text() == CANDIDATE
    assert episode.result().lean_calls == 2
    assert not result.terminated
    comparator.verify.assert_not_awaited()


def test_helper_primary_does_not_borrow_untouched_target_placeholders(tmp_path):
    episode, verifier, comparator, _ = batch_episode(tmp_path)
    seed_helper(episode)
    verifier.verify_file.return_value = {"success": False, "errors": ["unknown compiler failure"]}
    result = step(episode, "file_edit", {"file_path": "Submission.lean", "edits": [
        {"file_path": "Submission/Helper.lean", "old_str": "trivial", "new_str": "skip"},
    ]})
    outcome = result.outcomes[0]
    assert outcome.error_kind != "placeholder"
    assert outcome.raw["_harness"]["attempt_kind"] == "auxiliary"
    assert episode.tools.workspace.submission_file.read_text() == STATEMENT
    comparator.verify.assert_not_awaited()


def test_failed_batch_invalidates_every_saved_file_fact(tmp_path):
    episode, verifier, comparator, _ = batch_episode(tmp_path)
    verifier.verify_file.return_value = {"success": True, "errors": []}
    for name in ("A", "B"):
        step(episode, "file_write", {
            "file_path": f"Submission/{name}.lean", "content": HELPER.replace("helper", name),
        })
    assert len(episode.observe().state["verified_artifacts"]) == 2
    verifier.verify_file.return_value = {"success": False, "errors": ["unsolved goals"]}
    result = step(episode, "file_edit", {"edits": [
        {"file_path": f"Submission/{name}.lean", "old_str": "trivial", "new_str": "skip"}
        for name in ("A", "B")
    ]})
    assert result.outcomes[0].raw["file_saved"]
    assert not episode.observe().state["verified_artifacts"]
    comparator.verify.assert_not_awaited()


def test_exception_preserves_all_saved_files_and_remaining_check_count(tmp_path):
    episode, verifier, comparator, _ = batch_episode(tmp_path)
    helper = seed_helper(episode)
    verifier.verify_file.side_effect = [RuntimeError("verifier unavailable"), {"success": True}]
    result = step(episode, "file_edit", two_files())
    outcome = result.outcomes[0]
    assert outcome.infrastructure_error
    assert outcome.raw["file_saved"] and outcome.raw["verification_count"] == 2
    assert episode.result().lean_calls == 2
    assert episode.tools.workspace.submission_file.read_text() == CANDIDATE
    assert "exact True.intro" in helper.read_text()
    comparator.verify.assert_not_awaited()


def test_draft_auto_check_preserves_hole_feedback_without_acceptance(tmp_path):
    episode, verifier, comparator, _ = batch_episode(tmp_path)
    draft = AsyncMock(return_value={"success": True, "errors": []})
    episode.tools.draft_verifier = SimpleNamespace(verify_file=draft)
    result = step(episode, "file_edit", {"edits": [
        {"old_str": "sorry", "new_str": "(by sorry)"},
    ]})
    outcome = result.outcomes[0]
    assert outcome.verified is None and outcome.raw["verification_profile"] == "draft_allow_sorry"
    assert outcome.raw["source_placeholders"]["total"] == 1
    assert episode.result().lean_calls == 1
    draft.assert_awaited_once()
    verifier.verify_file.assert_not_awaited()
    comparator.verify.assert_not_awaited()


@pytest.mark.parametrize("name,args", [
    ("file_write", {"content": CANDIDATE, "target_path": "Submission/notes.txt"}),
    ("file_edit", {"edits": [{"file_path": "Submission/Helper.lean", "target_path": "Submission.lean/.",
                                "old_str": HELPER, "new_str": CANDIDATE}]}),
])
def test_actual_output_destination_budget_includes_required_comparator(tmp_path, name, args):
    episode, verifier, comparator, _ = batch_episode(tmp_path, limit=1)
    seed_helper(episode)
    verifier.verify_file.return_value = {"success": True, "errors": []}
    result = step(episode, name, args)
    assert result.reason == "lean_budget_exhausted"
    assert episode.tools.workspace.submission_file.read_text() == CANDIDATE
    assert episode.result().lean_calls == 1 and not episode.result().success
    comparator.verify.assert_not_awaited()


def test_nonlean_destination_does_not_need_a_lean_budget_slot(tmp_path):
    episode, verifier, comparator, _ = batch_episode(tmp_path, limit=1)
    result = step(episode, "file_write", {"file_path": "Submission/notes.txt", "content": "notes"})
    assert result.outcomes[0].valid and episode.result().lean_calls == 0
    assert result.outcomes[0].raw["verification_count"] == 0
    assert next(event for event in result.events if event["kind"] == "tool_execution")["lean_call"] is False
    verifier.verify_file.assert_not_awaited()
    comparator.verify.assert_not_awaited()


def test_batch_surface_normalization_keeps_items_isolated():
    original = {"edits": [{"old_line_range": "2:4", "new_str": "replacement"}]}
    dispatch = CompatibilitySurface.resolve("file_edit", original)
    assert "file_path" not in dispatch.backend_arguments
    assert dispatch.backend_arguments["edits"][0]["old_line_range"] == [2, 4]
    assert original["edits"][0]["old_line_range"] == "2:4"


@pytest.mark.parametrize("arguments", [
    {"edits": []},
    {"edits": [None]},
    {"edits": [{"old_str": "sorry"}]},
    {"edits": [{"old_str": "sorry", "new_str": "trivial", "unexpected": True}]},
    {"edits": [{"old_str": "sorry", "new_str": "trivial"}], "new_str": "conflicting"},
])
def test_invalid_batch_shape_is_an_argument_error_without_mutation(tmp_path, arguments):
    episode, verifier, comparator, _ = make_episode(tmp_path)
    transition = step(episode, "file_edit", arguments)
    assert transition.outcomes[0].error_kind == "invalid_arguments"
    assert episode.tools.workspace.submission_file.read_text() == STATEMENT
    assert episode.result().lean_calls == 0
    assert not episode.observe().state["target_attempts"]
    verifier.verify_file.assert_not_awaited()
    comparator.verify.assert_not_awaited()
