"""Automatic file checks preserve final acceptance and resource budgets."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dvp.config import BudgetConfig, EpisodeConfig
from test_format_error_limit import CANDIDATE, STATEMENT, action, make_episode, native_call


def automatic_episode(tmp_path, *, budget=None):
    return make_episode(tmp_path, config=EpisodeConfig(
        budget=budget or BudgetConfig(),
    ))


def step(episode, name, args):
    return asyncio.run(episode.step(action(native_call(name, args))))






@pytest.mark.parametrize("name,args", [
    ("file_write", {"content": CANDIDATE}),
    ("file_edit", {"old_str": "sorry", "new_str": "trivial"}),
])
def test_saved_target_automatically_checks_then_requires_comparator(tmp_path, name, args):
    episode, verifier, comparator, _ = automatic_episode(tmp_path)
    verifier.verify_file.return_value = {"success": True, "errors": []}
    transition = step(episode, name, args)
    verifier.verify_file.assert_awaited_once()
    comparator.verify.assert_not_awaited()
    assert transition.outcomes[0].verified and not transition.terminated
    episode.stop("model_finished")
    assert asyncio.run(episode.finalize()).success
    comparator.verify.assert_awaited_once()
    assert episode.result().turns == 1
    assert episode.result().lean_calls == 2  # compiler plus final Comparator


def test_compiler_success_does_not_override_comparator_rejection(tmp_path):
    episode, verifier, comparator, _ = automatic_episode(tmp_path)
    verifier.verify_file.return_value = {"success": True, "errors": []}
    comparator.verify.return_value = {"success": False, "errors": ["statement mismatch"]}
    result = step(episode, "file_edit", {"old_str": "sorry", "new_str": "trivial"})
    assert result.outcomes[0].verified and not result.terminated
    comparator.verify.assert_not_awaited()
    episode.stop("model_finished")
    final = asyncio.run(episode.finalize())
    assert final.reason == "comparator_rejected" and not final.success


def test_draft_save_checks_holes_without_comparator(tmp_path):
    episode, verifier, comparator, _ = automatic_episode(tmp_path)
    draft = AsyncMock(return_value={"success": True, "errors": []})
    episode.tools.draft_verifier = SimpleNamespace(verify_file=draft)
    result = step(episode, "file_write", {"content": STATEMENT})
    draft.assert_awaited_once()
    verifier.verify_file.assert_not_awaited()
    comparator.verify.assert_not_awaited()
    assert result.outcomes[0].verified is None and not result.terminated
    assert result.outcomes[0].raw["verification_profile"] == "draft_allow_sorry"
    assert episode.result().lean_calls == 1


def test_automatic_check_budget_is_enforced_before_mutating_file(tmp_path):
    episode, verifier, comparator, _ = automatic_episode(tmp_path, budget=BudgetConfig(max_lean_calls=1))
    result = asyncio.run(episode.step(action(
        native_call("lean_verify", {"code": "example : False := by trivial"}, "check"),
        native_call("file_edit", {"old_str": "sorry", "new_str": "trivial"}, "edit"),
    )))
    assert result.outcomes[1].error_kind == "lean_budget_exhausted"
    assert episode.tools.workspace.submission_file.read_text() == STATEMENT
    verifier.verify_file.assert_not_awaited()
    comparator.verify.assert_not_awaited()


@pytest.mark.parametrize("name,args", [
    ("file_edit", {"old_str": "sorry", "new_str": "trivial"}),
    ("file_write", {"file_path": "Submission.lean", "target_path": "Submission/notes.txt", "content": CANDIDATE}),
    ("file_write", {"file_path": "Submission.lean/.", "content": CANDIDATE}),
])
def test_automatic_check_consumes_budget_before_comparator(tmp_path, name, args):
    episode, verifier, comparator, _ = automatic_episode(tmp_path, budget=BudgetConfig(max_lean_calls=1))
    verifier.verify_file.return_value = {"success": True, "errors": []}
    result = step(episode, name, args)
    assert result.reason == "lean_budget_exhausted" and not episode.result().success
    assert episode.tools.workspace.submission_file.read_text() == CANDIDATE
    comparator.verify.assert_not_awaited()


def test_nonlean_save_does_not_take_lean_budget(tmp_path):
    episode, verifier, comparator, _ = automatic_episode(tmp_path)
    result = step(episode, "file_write", {"file_path": "Submission/notes.txt", "content": "notes"})
    assert result.outcomes[0].valid
    assert episode.result().lean_calls == 0
    verifier.verify_file.assert_not_awaited()
    comparator.verify.assert_not_awaited()


@pytest.mark.parametrize("path", ["Submission/Helper.lean", "Submission/Helper.lean/."])
def test_failed_recheck_invalidates_old_helper_evidence(tmp_path, path):
    episode, verifier, comparator, _ = automatic_episode(tmp_path)
    verifier.verify_file.return_value = {"success": True, "errors": []}
    step(episode, "file_write", {
        "file_path": "Submission/Helper.lean", "content": "theorem helper : True := by trivial\n",
    })
    assert episode.observe().state["verified_artifacts"]
    verifier.verify_file.return_value = {"success": False, "errors": ["unsolved goals"]}
    checked = step(episode, "file_edit", {
        "file_path": path, "old_str": "trivial", "new_str": "skip",
    })
    assert checked.outcomes[0].verified is False
    assert "skip" in episode.tools.workspace.resolve("Submission/Helper.lean").read_text()
    assert not episode.observe().state["verified_artifacts"]
    comparator.verify.assert_not_awaited()
