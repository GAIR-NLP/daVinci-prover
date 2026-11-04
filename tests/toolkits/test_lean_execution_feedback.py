"""Automatic Lean checks report execution separately from successful file saves."""

import asyncio
from copy import deepcopy
import json
from unittest.mock import AsyncMock

import pytest

from dvp.runtime.workspace import Workspace
from dvp.toolkits.registry import ToolRegistry
from dvp.verification.lean import LeanVerificationConfig
from dvp.verification.lean import LeanVerifier


@pytest.fixture
def registry(tmp_path, monkeypatch):
    workspace = Workspace(root=tmp_path / "run")
    workspace.setup()
    verifier = LeanVerifier(LeanVerificationConfig(
        target_workspace=None,
        scratch_workspace=workspace.submission_dir,
        module_root=workspace.workspaces_dir,
        timeout=1,
    ))
    tools = ToolRegistry(workspace=workspace, verifier=verifier)
    for checker in (tools.verifier, tools.draft_verifier):
        monkeypatch.setattr(checker, "_lean_command", lambda path: ["lean", str(path)])
        monkeypatch.setattr(checker, "_run_local_command", AsyncMock(return_value=(0, "", "")))
    return tools


def write_or_edit_and_check(registry, tool_name, content, file_name="Main.lean"):
    file_path = "Submission/" + file_name
    if tool_name == "file_edit":
        (registry.workspace.submission_dir / file_name).write_text("old content", encoding="utf-8")
        arguments = {"file_path": file_path, "old_str": "old content", "new_str": content}
    else:
        arguments = {"file_path": file_path, "content": content}
    result = asyncio.run(registry.call(tool_name, arguments))
    assert (registry.workspace.submission_dir / file_name).read_text(encoding="utf-8") == content
    assert result["file_saved"] is True
    if file_name.endswith(".lean"):
        assert result["verification_count"] == 1
        return result["verification"]
    assert result["lean_executed"] is False and "verification" not in result
    return result


@pytest.mark.parametrize("tool_name", ["file_write", "file_edit"])
@pytest.mark.parametrize("draft", [False, True])
def test_custom_commands_reach_lean_in_both_profiles(registry, tool_name, draft):
    content = "import Mathlib\nrun_cmd pure ()\n"
    checker = registry.draft_verifier if draft else registry.verifier
    if draft:
        content += "example : True := by sorry\n"
        checker._run_local_command.return_value = (
            0, 'DVP_LOCAL_DRAFT_JSON:{"messages": [], "sorries": []}\n', "",
        )
    result = write_or_edit_and_check(registry, tool_name, content)

    assert result["lean_executed"] is True
    assert result["success"] is True
    assert result["command"]
    assert result["errors"] == []
    assert result["verification_profile"] == ("draft_allow_sorry" if draft else "strict")
    assert result["source_placeholders"]["total"] == int(draft)
    checker._run_local_command.assert_awaited_once()


@pytest.mark.parametrize("tool_name", ["file_write", "file_edit"])
@pytest.mark.parametrize("draft", [False, True])
@pytest.mark.parametrize("probe", ["#eval (20 + 22 : Nat)", "#print Nat"])
def test_inspection_commands_reach_compiler_and_keep_draft_holes(registry, tool_name, draft, probe):
    checker = registry.draft_verifier if draft else registry.verifier
    hole = {"pos": {"line": 2, "column": 25}, "goal": "⊢ True"}
    payload = {"messages": [], "sorries": [hole]}
    checker._run_local_command.return_value = (
        0, "DVP_LOCAL_DRAFT_JSON:" + json.dumps(payload) + "\n" if draft else "42\n", "",
    )
    proof = "sorry" if draft else "trivial"
    result = write_or_edit_and_check(registry, tool_name, probe + "\nexample : True := by " + proof + "\n")

    assert result["success"], result
    assert result["lean_executed"] is True
    assert result["strict_verified"] is (not draft)
    assert result["draft_typechecked"] is draft
    assert result["holes"]["total"] == int(draft)
    if draft:
        assert result["holes"]["items"][0]["goal"] == "⊢ True"
    checker._run_local_command.assert_awaited_once()
    other = registry.verifier if draft else registry.draft_verifier
    other._run_local_command.assert_not_awaited()


@pytest.mark.parametrize("returncode", [0, 1])
def test_completed_strict_check_executed_even_when_lean_rejects(registry, returncode):
    diagnostic = "Submission/Main.lean:1:0: error: unsolved goals\n⊢ False" if returncode else ""
    registry.verifier._run_local_command.return_value = (returncode, diagnostic, "")
    content = "example : False := by trivial" if returncode else "example : True := by trivial"
    result = write_or_edit_and_check(registry, "file_write", content)

    assert result["lean_executed"] is True
    assert result["success"] is (returncode == 0)
    assert result["strict_verified"] is (returncode == 0)
    assert result["returncode"] == returncode
    assert result["errors"] == ([diagnostic] if diagnostic else [])
    registry.verifier._run_local_command.assert_awaited_once()
    registry.draft_verifier._run_local_command.assert_not_awaited()


def test_completed_draft_check_executed_and_retains_hole_feedback(registry):
    hole = {"pos": {"line": 1, "column": 25}, "goal": "⊢ True"}
    payload = {"messages": [], "sorries": [hole]}
    registry.draft_verifier._run_local_command.return_value = (
        0, "DVP_LOCAL_DRAFT_JSON:" + json.dumps(payload) + "\n", "",
    )
    result = write_or_edit_and_check(registry, "file_write", "example : True := by sorry")

    assert result["lean_executed"] is True
    assert result["draft_typechecked"] is True
    assert result["strict_verified"] is False
    assert result["response"] == payload
    assert result["holes"]["items"] == [{"range": {"start": hole["pos"]}, "goal": hole["goal"]}]
    registry.draft_verifier._run_local_command.assert_awaited_once()
    registry.verifier._run_local_command.assert_not_awaited()


@pytest.mark.parametrize("tool_name", ["file_write", "file_edit"])
def test_non_lean_change_does_not_execute_lean(registry, tool_name):
    result = write_or_edit_and_check(registry, tool_name, "notes", "note.txt")

    assert result["success"] is True
    assert result["lean_executed"] is False
    assert "verification" not in result
    registry.verifier._run_local_command.assert_not_awaited()
    registry.draft_verifier._run_local_command.assert_not_awaited()


@pytest.mark.parametrize("captured", [None, (125, "", "DVP_SANDBOX_ERROR: setup failed")])
def test_timeout_or_infrastructure_result_does_not_prove_lean_started(registry, captured):
    registry.verifier._run_local_command.return_value = captured
    result = write_or_edit_and_check(registry, "file_write", "example : True := by trivial")

    assert result["lean_executed"] is None
    assert result["success"] is False
    assert result["command"]
    registry.verifier._run_local_command.assert_awaited_once()


@pytest.mark.parametrize("verification, expected", [
    pytest.param({"success": True}, None, id="missing-execution-metadata"),
    pytest.param({"success": False, "command": []}, False, id="deadline-before-command"),
    pytest.param({"success": False, "command": ["lean", "entry.lean"]}, None, id="planned-command-only"),
    pytest.param({"success": False, "command": None, "compiled_modules": []}, False, id="modules-not-compiled"),
    pytest.param({"success": False, "command": None, "compiled_modules": ["Submission.Helper"]}, True,
                 id="module-compiled-before-entry-failed"),
    pytest.param({"success": False, "command": ["lean", "entry.lean"], "returncode": 125,
                  "compiled_modules": ["Submission.Helper"], "_harness_infrastructure_error": True}, True,
                 id="module-compiled-before-later-infrastructure-failure"),
    pytest.param({"success": False, "lean_executed": True}, True, id="explicit-execution-evidence"),
    pytest.param({"success": False, "lean_executed": False}, False, id="explicit-no-execution-evidence"),
])
def test_execution_metadata_keeps_unknown_and_prior_module_execution(registry, monkeypatch, verification, expected):
    original = deepcopy(verification)
    monkeypatch.setattr(registry.verifier, "verify_file", AsyncMock(return_value=verification))
    result = write_or_edit_and_check(registry, "file_write", "example : True := by trivial")

    assert result["lean_executed"] is expected
    assert result["success"] is verification["success"]
    assert verification == original
    for key, value in original.items():
        assert result[key] == value
