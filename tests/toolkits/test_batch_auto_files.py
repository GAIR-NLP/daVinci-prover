"""Batch saves validate first and check each final Lean destination once."""

import asyncio
from unittest.mock import AsyncMock, call

import pytest

from dvp.runtime.workspace import Workspace
from dvp.toolkits import files, lean
from dvp.toolkits.registry import ToolRegistry
from dvp.verification import modular_feedback
from dvp.verification.lean import LeanVerificationConfig, LeanVerifier


@pytest.fixture
def registry(tmp_path):
    workspace = Workspace(root=tmp_path / "run")
    workspace.setup()
    return ToolRegistry(
        workspace=workspace,
        verifier=LeanVerifier(LeanVerificationConfig(
            target_workspace=None, scratch_workspace=workspace.submission_dir,
            module_root=workspace.workspaces_dir, timeout=1,
        )),
    )


@pytest.fixture
def check(monkeypatch):
    mock = AsyncMock(return_value={
        "success": True, "lean_executed": True, "verification_profile": "strict",
        "strict_verified": True, "draft_typechecked": False,
    })
    monkeypatch.setattr(lean, "lean_verify", mock)
    return mock


def seed(registry, path, content="old"):
    destination = registry.workspace.resolve(path, for_write=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8")
    return destination


def test_batch_compiles_final_version_once_after_all_files_are_saved(registry, check):
    submission = seed(registry, "Submission.lean", "import Submission.Helper\nold")
    helper = seed(registry, "Submission/Helper.lean", "old helper")
    arguments = {"edits": [
        {"old_str": "old", "new_str": "invalid intermediate"},
        {"file_path": "Submission/Helper.lean", "old_str": "old", "new_str": "new"},
        {"old_str": "invalid intermediate", "new_str": "valid final"},
        {"file_path": "Submission/./Helper.lean", "old_str": "helper", "new_str": "module"},
    ]}
    planned = files.automatic_check_paths(registry, "file_edit", arguments)
    assert planned == (submission, helper)
    assert submission.read_text().endswith("old")

    async def inspect_final(registry, args):
        assert submission.read_text() == "import Submission.Helper\nvalid final"
        assert helper.read_text() == "new module"
        return {"success": True, "lean_executed": True}

    check.side_effect = inspect_final
    result = asyncio.run(registry.call("file_edit", arguments))

    assert result["success"] is True
    assert result["file_saved"] is True
    assert result["edits_applied"] == 4
    assert result["verification_count"] == 2
    assert [item["file_path"] for item in result["files"]] == [
        "Submission.lean", "Submission/Helper.lean",
    ]
    assert [item["file_path"] for item in result["verifications"]] == [
        "Submission.lean", "Submission/Helper.lean",
    ]
    assert check.await_args_list == [
        call(registry, {"file_path": "Submission.lean"}),
        call(registry, {"file_path": "Submission/Helper.lean"}),
    ]


@pytest.mark.parametrize("bad_edit", [
    {"old_str": "missing", "new_str": "bad"},
    {"old_str": "old", "new_str": None},
    {"file_path": "target/Mathlib.lean", "old_str": "old", "new_str": "bad"},
    {"file_path": "Submission/../../outside.lean", "old_str": "old", "new_str": "bad"},
    "not an edit",
])
def test_invalid_edit_rolls_back_entire_staged_batch_without_check(registry, check, bad_edit):
    original = seed(registry, "Submission.lean", "old\n")
    result = asyncio.run(registry.call("file_edit", {"edits": [
        {"old_str": "old", "new_str": "new", "target_path": "Submission/New.lean"},
        bad_edit,
    ]}))

    assert result["success"] is False
    assert result["error"].startswith("Edit 2:")
    assert "file_saved" not in result
    assert original.read_text() == "old\n"
    assert not (registry.workspace.submission_dir / "New.lean").exists()
    check.assert_not_awaited()


def test_target_path_and_nonlean_outputs_are_saved_but_only_lean_destinations_checked(registry, check):
    source = seed(registry, "Submission.lean")
    arguments = {"edits": [
        {"old_str": "old", "new_str": "notes", "target_path": "Submission/notes.txt"},
        {"file_path": "Submission/notes.txt", "old_str": "notes", "new_str": "helper",
         "target_path": "Submission/Helper.lean"},
        {"file_path": "Submission/./Helper.lean", "old_str": "helper", "new_str": "final helper"},
    ]}
    assert files.automatic_check_paths(registry, "file_edit", arguments) == (
        registry.workspace.resolve("Submission/Helper.lean"),
    )
    result = asyncio.run(registry.call("file_edit", arguments))

    assert source.read_text() == "old"
    assert (registry.workspace.submission_dir / "notes.txt").read_text() == "notes"
    assert (registry.workspace.submission_dir / "Helper.lean").read_text() == "final helper"
    assert result["file_path"] == "Submission/Helper.lean"
    assert len(result["files"]) == 2
    assert result["verification_count"] == 1
    check.assert_awaited_once_with(registry, {"file_path": "Submission/Helper.lean"})


@pytest.mark.parametrize("tool", ["file_write", "file_edit"])
@pytest.mark.parametrize("path", ["Submission.lean", "Submission/notes.txt"])
def test_single_save_in_batch_auto_checks_only_lean_and_retains_save_metadata(registry, check, tool, path):
    if tool == "file_edit":
        seed(registry, path)
        arguments = {"file_path": path, "old_str": "old", "new_str": "new"}
    else:
        arguments = {"file_path": path, "content": "new", "target_path": "ignored.lean"}
    result = asyncio.run(registry.call(tool, arguments))

    assert result["success"] is True and result["file_saved"] is True
    assert result["file_path"] == path
    assert result["files"] == [{"file_path": path, "bytes": 3}]
    assert registry.workspace.resolve(path).read_text() == "new"
    if path.endswith(".lean"):
        assert result["verification_count"] == 1
        check.assert_awaited_once_with(registry, {"file_path": path})
    else:
        assert result["verification_count"] == 0 and result["verifications"] == []
        assert result["lean_executed"] is False
        assert "verification" not in result
        check.assert_not_awaited()


def test_compile_failure_keeps_all_changes_and_feedback(registry, check):
    seed(registry, "Submission.lean")
    seed(registry, "Submission/Helper.lean")
    failing = {"success": False, "lean_executed": True, "errors": ["unsolved goals"]}
    passing = {"success": True, "lean_executed": True, "strict_verified": True}
    check.side_effect = [failing, passing]
    result = asyncio.run(registry.call("file_edit", {"edits": [
        {"old_str": "old", "new_str": "new"},
        {"file_path": "Submission/Helper.lean", "old_str": "old", "new_str": "helper"},
    ]}))

    assert result["success"] is False and result["file_saved"] is True
    assert registry.workspace.submission_file.read_text() == "new"
    assert (registry.workspace.submission_dir / "Helper.lean").read_text() == "helper"
    assert result["verification_count"] == 2
    assert [item["verification"] for item in result["verifications"]] == [failing, passing]
    assert result["verification"] is failing


@pytest.mark.parametrize("failure_kind, expected_primary", [
    ("none", "Submission.lean"),
    ("compile", "Submission/First.lean"),
    ("infrastructure", "Submission/Last.lean"),
])
def test_primary_evidence_stays_bound_to_one_checked_file(registry, check, failure_kind, expected_primary):
    names = ["Submission/First.lean", "Submission.lean", "Submission/Last.lean"]
    results = []
    for index, name in enumerate(names):
        seed(registry, name)
        results.append({
            "success": True, "lean_executed": True,
            "verification_profile": "draft_allow_sorry" if index == 0 else "strict",
            "strict_verified": index != 0, "draft_typechecked": index == 0,
            "holes": {"items": [name]}, "source_placeholders": {"total": index},
            "source_bundle_sha256": f"bundle-{index}",
            "source_bundle_manifest": {"entry": name},
        })
    if failure_kind in {"compile", "infrastructure"}:
        results[0]["success"] = False
    if failure_kind == "infrastructure":
        results[2].update(success=False, _harness_infrastructure_error=True)
    check.side_effect = results

    result = asyncio.run(registry.call("file_edit", {"edits": [
        {"file_path": name, "old_str": "old", "new_str": "new"} for name in names
    ]}))

    expected = results[names.index(expected_primary)]
    assert result["file_path"] == expected_primary
    assert result["verification"] is expected
    for key in ("verification_profile", "strict_verified", "draft_typechecked", "holes", "source_placeholders"):
        assert result[key] == expected[key]
    assert "source_bundle_sha256" not in result  # Metadata remains in its own check.
    assert result["success"] is (failure_kind == "none")


@pytest.mark.parametrize("previous_execution, aggregate", [(True, True), (False, None), (None, None)])
def test_verifier_exception_preserves_all_saved_files_and_attempted_check_count(
    registry, check, previous_execution, aggregate,
):
    names = ["Submission.lean", "Submission/Helper.lean", "Submission/Unreached.lean"]
    for name in names:
        seed(registry, name)
    previous = {"success": True, "lean_executed": previous_execution}
    check.side_effect = [previous, RuntimeError("verifier unavailable"),
                         {"success": True, "lean_executed": False}]
    result = asyncio.run(registry.call("file_edit", {"edits": [
        {"file_path": name, "old_str": "old", "new_str": "saved"} for name in names
    ]}))

    assert result["success"] is False and result["file_saved"] is True
    assert result["_harness_infrastructure_error"] is True
    assert result["error"] == "RuntimeError: verifier unavailable"
    assert result["file_path"] == "Submission/Helper.lean"
    assert result["verification"]["_harness_infrastructure_error"] is True
    assert result["lean_executed"] is aggregate
    assert result["verification_count"] == 3
    assert len(result["verifications"]) == 3
    assert result["verifications"][0]["verification"] is previous
    assert len(result["files"]) == 3
    for name in names:
        assert registry.workspace.resolve(name).read_text() == "saved"


def test_direct_handler_keeps_verifier_exception_cause_and_metadata(registry, check):
    original = RuntimeError("broken compiler")
    check.side_effect = original
    with pytest.raises(files.SavedFileVerificationError) as caught:
        asyncio.run(files.file_write(registry, {"content": "saved"}))
    assert caught.value.__cause__ is original
    assert caught.value.result["file_saved"] is True
    assert caught.value.result["verification_count"] == 1
    assert registry.workspace.submission_file.read_text() == "saved"


@pytest.mark.parametrize("malformed", [None, "invalid verifier result", []])
def test_malformed_verifier_result_is_infrastructure_failure_and_other_checks_continue(registry, check, malformed):
    for name in ("Submission.lean", "Submission/Helper.lean"):
        seed(registry, name)
    check.side_effect = [malformed, {"success": True, "lean_executed": True}]
    result = asyncio.run(registry.call("file_edit", {"edits": [
        {"old_str": "old", "new_str": "new"},
        {"file_path": "Submission/Helper.lean", "old_str": "old", "new_str": "new"},
    ]}))
    assert result["success"] is False and result["file_saved"] is True
    assert result["_harness_infrastructure_error"] is True
    assert result["verification_count"] == 2
    assert result["file_path"] == "Submission.lean"
    assert "non-object" in result["error"]
    assert result["lean_executed"] is True
    assert result["verifications"][1]["verification"]["success"] is True


def test_batch_of_nonlean_edits_saves_without_checks(registry, check):
    seed(registry, "Submission/notes.txt")
    arguments = {"file_path": "Submission/notes.txt", "edits": [
        {"old_str": "old", "new_str": "new"},
        {"old_str": "new", "new_str": "copy", "target_path": "Submission/copy.txt"},
    ]}
    assert files.automatic_check_paths(registry, "file_edit", arguments) == ()
    result = asyncio.run(registry.call("file_edit", arguments))
    assert result["success"] is True and result["file_saved"] is True
    assert result["verification_count"] == 0 and result["verifications"] == []
    assert result["edits_applied"] == 2 and len(result["files"]) == 2
    assert result["lean_executed"] is False
    assert "verification" not in result
    check.assert_not_awaited()


@pytest.mark.parametrize("tool, arguments", [
    ("file_write", {"content": None}),
    ("file_write", {"content": "x", "file_path": "target/Mathlib.lean"}),
    ("file_edit", {"edits": []}),
    ("file_edit", {"edits": ["not an object"]}),
    ("file_edit", {"edits": [{"old_str": "old", "new_str": "new", "extra": 1}]}),
    ("file_edit", {"old_str": "old", "new_str": "new", "target_path": "../outside.lean"}),
    ("file_edit", {"edits": [{"old_str": "old", "new_str": "new"}], "new_str": "mixed"}),
    ("file_edit", {"edits": [{"old_str": "old", "new_str": "new"}], "file_path": None}),
    ("file_edit", {"old_str": "old"}),
    ("lean_verify", {"file_path": "Submission.lean"}),
])
def test_invalid_arguments_or_other_tools_plan_no_automatic_checks(registry, tool, arguments):
    assert files.automatic_check_paths(registry, tool, arguments) == ()
    assert not registry.workspace.submission_file.exists()


def test_planner_write_ignores_target_path_and_accepts_both_edit_forms(registry):
    assert files.automatic_check_paths(registry, "file_write", {
        "content": "new", "target_path": "target/ignored.lean",
    }) == (registry.workspace.submission_file.resolve(),)
    arguments = {"edits": [{"old_str": "old", "new_str": "new"}]}
    assert files.automatic_check_paths(registry, "file_edit", arguments) == (
        registry.workspace.submission_file.resolve(),
    )
    assert files.automatic_check_paths(registry, "file_edit", arguments["edits"][0]) == (
        registry.workspace.submission_file.resolve(),
    )


def test_batch_auto_preserves_draft_and_strict_profile_selection(registry, monkeypatch):
    seed(registry, "Submission.lean")
    seed(registry, "Submission/Helper.lean")
    for verifier in (registry.verifier, registry.draft_verifier):
        monkeypatch.setattr(verifier, "_lean_command", lambda path: ["lean", str(path)])
        monkeypatch.setattr(verifier, "_run_local_command", AsyncMock(return_value=(0, "", "")))
    registry.draft_verifier._run_local_command.return_value = (
        0, 'DVP_LOCAL_DRAFT_JSON:{"messages": [], "sorries": []}\n', "",
    )
    result = asyncio.run(registry.call("file_edit", {"edits": [
        {"old_str": "old", "new_str": "example : True := by trivial\n"},
        {"file_path": "Submission/Helper.lean", "old_str": "old", "new_str": "example : True := by sorry\n"},
    ]}))
    assert result["success"] is True
    assert result["file_path"] == "Submission.lean"
    assert result["verification_profile"] == "strict"
    assert result["strict_verified"] is True
    assert result["verifications"][1]["verification"]["verification_profile"] == "draft_allow_sorry"
    assert result["verifications"][1]["verification"]["source_placeholders"]["total"] == 1
    registry.verifier._run_local_command.assert_awaited_once()
    registry.draft_verifier._run_local_command.assert_awaited_once()


def test_batch_auto_keeps_modular_compilation_and_entry_bundle_metadata(registry, monkeypatch):
    seed(registry, "Submission.lean")
    seed(registry, "Submission/Helper.lean")
    monkeypatch.setattr(modular_feedback, "lean_search_path", lambda _: "/pinned/lean")
    monkeypatch.setattr(registry.verifier, "_sandbox_command", lambda command, **options: command)
    monkeypatch.setattr(registry.verifier, "_lean_command", lambda path: ["lean", str(path)])
    monkeypatch.setattr(registry.verifier, "_run_local_command", AsyncMock(return_value=(0, "", "")))
    process = AsyncMock(return_value=(0, "", ""))
    monkeypatch.setattr(modular_feedback, "run_process", process)

    result = asyncio.run(registry.call("file_edit", {"edits": [
        {"file_path": "Submission/Helper.lean", "old_str": "old",
         "new_str": "theorem helper : True := by trivial\n"},
        {"old_str": "old", "new_str": "import Submission.Helper\nexample : True := helper\n"},
    ]}))
    assert result["success"] is True and result["lean_executed"] is True
    assert result["verification_count"] == 2
    assert result["file_path"] == "Submission.lean"
    assert result["verification"]["compiled_modules"] == ["Submission.Helper"]
    assert result["verification"]["source_bundle_sha256"]
    assert result["verification"]["source_bundle_manifest"]["modules"]
    assert process.await_count == 2  # The imported helper is rebuilt inside the entry check.
    registry.verifier._run_local_command.assert_awaited_once()  # Standalone helper check.
