"""Saved files retain their identity and compiler feedback for automatic checks."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from dvp.runtime.workspace import Workspace
from dvp.toolkits import files, lean
from dvp.toolkits.registry import ToolRegistry
from dvp.verification import modular_feedback
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
    registry = ToolRegistry(
        workspace=workspace, verifier=verifier,
    )
    for checker in (registry.verifier, registry.draft_verifier):
        monkeypatch.setattr(checker, "_lean_command", lambda path: ["lean", str(path)])
        monkeypatch.setattr(checker, "_run_local_command", AsyncMock(return_value=(0, "", "")))
    return registry


def save(registry, tool_name, content, file_path="Submission.lean"):
    if tool_name == "file_edit":
        path = registry.workspace.resolve(file_path, for_write=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("old content", encoding="utf-8")
        arguments = {"old_str": "old content", "new_str": content}
    else:
        arguments = {"content": content}
    # Exercise the default entrypoint as well as explicit helper paths.
    if file_path != "Submission.lean":
        arguments["file_path"] = file_path
    return asyncio.run(registry.call(tool_name, arguments))


@pytest.mark.parametrize("tool_name", ["file_write", "file_edit"])
@pytest.mark.parametrize("returncode", [0, 1])
def test_automatic_strict_success_or_failure_retains_written_bytes(registry, monkeypatch, tool_name, returncode):
    diagnostic = "Submission.lean:1:0: error: unsolved goals\n⊢ False" if returncode else ""
    registry.verifier._run_local_command.return_value = (returncode, diagnostic, "")
    automatic_check = AsyncMock(wraps=lean.lean_verify)
    monkeypatch.setattr(lean, "lean_verify", automatic_check)
    content = "example : False := by trivial\n" if returncode else "example : True := by trivial\n"

    result = save(registry, tool_name, content)

    assert result["success"] is (returncode == 0)
    assert result["file_saved"] is True
    assert result["bytes"] == len(content.encode("utf-8"))
    assert result["file_path"] == "Submission.lean"
    assert registry.workspace.submission_file.read_bytes() == content.encode("utf-8")
    assert result["lean_executed"] is True
    assert result["verification_profile"] == "strict"
    assert result["strict_verified"] is (returncode == 0)
    assert result["draft_typechecked"] is False
    assert result["verification"]["returncode"] == returncode
    assert result["verification"]["errors"] == ([diagnostic] if diagnostic else [])
    assert result["verification"]["file_path"] == "Submission.lean"
    automatic_check.assert_awaited_once_with(registry, {"file_path": "Submission.lean"})
    registry.verifier._run_local_command.assert_awaited_once()
    registry.draft_verifier._run_local_command.assert_not_awaited()


@pytest.mark.parametrize("tool_name", ["file_write", "file_edit"])
def test_automatic_draft_uses_draft_checker_and_preserves_holes(registry, tool_name):
    hole = {"pos": {"line": 1, "column": 25}, "goal": "⊢ True"}
    payload = {"messages": [], "sorries": [hole]}
    registry.draft_verifier._run_local_command.return_value = (
        0, "DVP_LOCAL_DRAFT_JSON:" + json.dumps(payload) + "\n", "",
    )

    result = save(registry, tool_name, "example : True := by sorry\n")

    assert result["success"] is True
    assert result["verification_profile"] == "draft_allow_sorry"
    assert result["draft_typechecked"] is True
    assert result["strict_verified"] is False
    assert result["source_placeholders"]["total"] == 1
    assert result["holes"]["items"] == [{"range": {"start": hole["pos"]}, "goal": hole["goal"]}]
    assert result["verification"]["response"] == payload
    registry.draft_verifier._run_local_command.assert_awaited_once()
    registry.verifier._run_local_command.assert_not_awaited()


@pytest.mark.parametrize("tool_name", ["file_write", "file_edit"])
def test_automatic_nonlean_save_never_calls_verifier(registry, monkeypatch, tool_name):
    automatic_check = AsyncMock(side_effect=AssertionError("Non-Lean files must not compile"))
    monkeypatch.setattr(lean, "lean_verify", automatic_check)

    result = save(registry, tool_name, "new notes\n", "Submission/notes.txt")

    assert result["success"] is True and result["file_saved"] is True
    assert result["lean_executed"] is False
    assert "verification" not in result
    assert (registry.workspace.submission_dir / "notes.txt").read_text() == "new notes\n"
    automatic_check.assert_not_awaited()


@pytest.mark.parametrize("selection, expected", [
    ({"old_str": "old", "new_str": "new"}, "new old\nold\n"),
    ({"old_str": "old", "new_str": "new", "replace_all": True}, "new new\nnew\n"),
    ({"old_str": "old", "new_str": "new", "replace_all": True, "old_line_range": [2, 3]}, "old old\nnew\n"),
    ({"old_str": "old\n", "new_str": "", "replace_all": True}, "old "),
])
def test_automatic_preserves_text_range_and_empty_replacement_semantics(registry, selection, expected):
    path = registry.workspace.submission_dir / "notes.txt"
    path.write_text("old old\nold\n")

    result = asyncio.run(registry.call("file_edit", {"file_path": "Submission/notes.txt", **selection}))

    assert result["success"] is True
    assert path.read_text() == expected
    assert result["bytes"] == len(expected.encode("utf-8"))


@pytest.mark.parametrize("destination", ["Submission/copy.lean", "Submission/copy.txt"])
def test_target_path_checks_only_actual_saved_destination(registry, monkeypatch, destination):
    source = registry.workspace.submission_file
    source.write_text("example : True := by sorry\n")
    automatic_check = AsyncMock(wraps=lean.lean_verify)
    monkeypatch.setattr(lean, "lean_verify", automatic_check)

    result = asyncio.run(registry.call("file_edit", {
        "old_str": "sorry", "new_str": "trivial", "target_path": destination,
    }))

    assert result["success"] is True
    assert result["file_path"] == destination
    assert source.read_text() == "example : True := by sorry\n"
    assert registry.workspace.resolve(destination).read_text() == "example : True := by trivial\n"
    if destination.endswith(".lean"):
        automatic_check.assert_awaited_once_with(registry, {"file_path": destination})
        assert result["verification"]["file_path"] == destination
        assert result["verification"]["command"][-1] == str(registry.workspace.resolve(destination))
        assert result["verification_profile"] == "strict"
    else:
        automatic_check.assert_not_awaited()
        assert result["lean_executed"] is False


@pytest.mark.parametrize("tool_name", ["file_write", "file_edit"])
def test_verifier_exception_preserves_saved_identity_and_infrastructure_status(registry, monkeypatch, tool_name):
    monkeypatch.setattr(registry.verifier, "verify_file", AsyncMock(side_effect=RuntimeError("compiler unavailable")))
    content = "example : True := by trivial\n"

    result = save(registry, tool_name, content)

    assert result["success"] is False
    assert result["file_saved"] is True
    assert result["file_path"] == "Submission.lean"
    assert result["bytes"] == len(content.encode("utf-8"))
    assert result["_harness_infrastructure_error"] is True
    assert result["lean_executed"] is None
    assert result["error"] == "RuntimeError: compiler unavailable"
    assert registry.workspace.submission_file.read_text() == content
    registry.verifier.verify_file.assert_awaited_once()


def test_direct_handler_preserves_verifier_exception_cause(registry, monkeypatch):
    error = RuntimeError("compiler unavailable")
    monkeypatch.setattr(registry.verifier, "verify_file", AsyncMock(side_effect=error))

    with pytest.raises(files.SavedFileVerificationError) as caught:
        asyncio.run(registry.file_write({"content": "example : True := by trivial"}))

    assert caught.value.__cause__ is error
    assert caught.value.result["file_saved"] is True
    assert caught.value.result["file_path"] == "Submission.lean"


def test_custom_command_is_saved_and_compiled(registry):
    content = "run_cmd pure ()\nexample : True := by trivial\n"

    result = save(registry, "file_write", content)

    assert result["success"] is True
    assert result["file_saved"] is True
    assert result["lean_executed"] is True
    assert result["verification"]["command"]
    assert registry.workspace.submission_file.read_text() == content
    registry.verifier._run_local_command.assert_awaited_once()


def test_automatic_keeps_modular_verification_and_source_bundle_metadata(registry, monkeypatch):
    helper = registry.workspace.submission_dir / "Helper.lean"
    helper.write_text("theorem helper : True := by trivial\n")
    monkeypatch.setattr(modular_feedback, "lean_search_path", lambda _: "/pinned/lean")
    monkeypatch.setattr(registry.verifier, "_sandbox_command", lambda command, **options: command)
    process = AsyncMock(return_value=(0, "", ""))
    monkeypatch.setattr(modular_feedback, "run_process", process)

    result = save(registry, "file_write", "import Submission.Helper\nexample : True := helper\n")

    assert result["success"] is True and result["lean_executed"] is True
    assert result["verification"]["compiled_modules"] == ["Submission.Helper"]
    assert result["verification"]["source_bundle_sha256"]
    assert result["verification"]["source_bundle_manifest"]["modules"]
    assert process.await_count == 2  # The helper and entrypoint belong to one check.
    registry.verifier._run_local_command.assert_not_awaited()


@pytest.mark.parametrize("verification, executed", [
    ({"success": False, "command": ["lean", "Submission.lean"], "errors": ["timeout"]}, None),
    ({"success": False, "_harness_infrastructure_error": True, "lean_executed": False}, False),
    ({"success": False, "_harness_infrastructure_error": True,
      "compiled_modules": ["Submission.Helper"]}, True),
])
def test_automatic_preserves_structured_failure_and_execution_metadata(registry, monkeypatch, verification, executed):
    monkeypatch.setattr(registry.verifier, "verify_file", AsyncMock(return_value=verification))

    result = save(registry, "file_write", "example : True := by trivial\n")

    assert result["success"] is False and result["file_saved"] is True
    assert result["lean_executed"] is executed
    for key, value in verification.items():
        assert result["verification"][key] == value
    if verification.get("_harness_infrastructure_error"):
        assert result["_harness_infrastructure_error"] is True


@pytest.mark.parametrize("destination", ["target/Test.lean", "Submission/../../escape.lean", "/tmp/escape.lean"])
def test_automatic_keeps_workspace_write_protections(registry, destination):
    registry.workspace.submission_file.write_text("old\n")

    result = asyncio.run(registry.call("file_edit", {
        "old_str": "old", "new_str": "new", "target_path": destination,
    }))

    assert result["success"] is False
    assert registry.workspace.submission_file.read_text() == "old\n"
    assert "file_saved" not in result
    registry.verifier._run_local_command.assert_not_awaited()
