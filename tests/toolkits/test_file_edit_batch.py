"""Batch edits preserve transaction boundaries before automatic compilation."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from dvp.runtime.workspace import Workspace
from dvp.toolkits import files, lean
from dvp.toolkits.registry import ToolRegistry
from dvp.toolkits.schemas import build_tools
from dvp.toolkits.surface import CompatibilitySurface


@pytest.fixture
def registry(tmp_path, monkeypatch):
    workspace = Workspace(root=tmp_path / "run")
    workspace.setup()
    registry = object.__new__(ToolRegistry)
    registry.workspace = workspace
    monkeypatch.setattr(lean, "lean_verify", AsyncMock(return_value={
        "success": True, "lean_executed": True,
    }))
    return registry


def call_edit(registry, edits):
    return asyncio.run(registry.file_edit({"edits": edits}))


def seed(registry, name, text):
    path = registry.workspace.resolve(name, for_write=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_sequential_edits_across_files_preserve_all_changes(registry):
    a = seed(registry, "Submission/A.lean", "def old := 1\n")
    b = seed(registry, "Submission/B.lean", "import Submission.A\n#check old\n")
    result = call_edit(registry, [
        {"file_path": "Submission/A.lean", "old_str": "old", "new_str": "renamed"},
        {"file_path": "Submission/B.lean", "old_str": "old", "new_str": "renamed"},
        {"file_path": "Submission/A.lean", "old_str": "renamed := 1", "new_str": "renamed := 2"},
    ])
    assert {key: result[key] for key in ("success", "files", "edits_applied", "lean_executed")} == {
        "success": True,
        "files": [{"file_path": "Submission/A.lean", "bytes": 17},
                  {"file_path": "Submission/B.lean", "bytes": 35}],
        "edits_applied": 3, "lean_executed": True,
    }
    assert a.read_text() == "def renamed := 2\n"
    assert b.read_text() == "import Submission.A\n#check renamed\n"
    assert not list(registry.workspace.workspaces_dir.rglob(".dvp-write-*"))


def test_shared_file_path_accepts_sequential_edits_from_observed_calls(registry):
    path = seed(registry, "Submission/Helper.lean", "def old := 1\n")
    arguments = {
        "file_path": "Submission/Helper.lean",
        "edits": [
            {"old_str": "old", "new_str": "renamed"},
            {"old_str": "renamed := 1", "new_str": "renamed := 2"},
        ],
    }
    dispatch = CompatibilitySurface.resolve("file_edit", arguments)
    result = asyncio.run(registry.file_edit(dispatch.backend_arguments))
    assert {key: result[key] for key in ("success", "files", "edits_applied", "lean_executed")} == {
        "success": True,
        "files": [{"file_path": "Submission/Helper.lean", "bytes": 17}],
        "edits_applied": 2,
        "lean_executed": True,
    }
    assert path.read_text() == "def renamed := 2\n"
    assert all("file_path" not in edit for edit in arguments["edits"])


def test_item_path_overrides_shared_default_only_for_that_item(registry):
    shared = seed(registry, "Submission/A.lean", "alpha\n")
    override = seed(registry, "Submission/B.lean", "beta\n")
    result = asyncio.run(registry.file_edit({
        "file_path": "Submission/A.lean",
        "edits": [
            {"old_str": "alpha", "new_str": "intermediate"},
            {"file_path": "Submission/B.lean", "old_str": "beta", "new_str": "other"},
            {"old_str": "intermediate", "new_str": "final"},
        ],
    }))
    assert result["success"] and result["edits_applied"] == 3
    assert shared.read_text() == "final\n"
    assert override.read_text() == "other\n"
    assert result["files"] == [
        {"file_path": "Submission/A.lean", "bytes": 6},
        {"file_path": "Submission/B.lean", "bytes": 6},
    ]


def test_late_shared_path_edit_failure_preserves_original_file_bytes(registry):
    shared = seed(registry, "Submission/A.lean", "unused")
    shared.write_bytes(b"alpha\r\n")
    override = seed(registry, "Submission/B.lean", "beta\n")
    result = asyncio.run(registry.file_edit({
        "file_path": "Submission/A.lean",
        "edits": [
            {"old_str": "alpha", "new_str": "changed"},
            {"file_path": "Submission/B.lean", "old_str": "beta", "new_str": "other"},
            {"old_str": "missing", "new_str": "final"},
        ],
    }))
    assert not result["success"]
    assert result["error"].startswith("Edit 3:")
    assert "old_str not found" in result["error"]
    assert shared.read_bytes() == b"alpha\r\n"
    assert override.read_bytes() == b"beta\n"
    assert not list(registry.workspace.workspaces_dir.rglob(".dvp-write-*"))


@pytest.mark.parametrize("shared_path", [
    "target/Test.lean", "Submission/../../escape.lean", "/tmp/escape.lean",
])
def test_restricted_inherited_path_cannot_apply_an_earlier_override(registry, shared_path):
    writable = seed(registry, "Submission.lean", "alpha")
    result = asyncio.run(registry.file_edit({
        "file_path": shared_path,
        "edits": [
            {"file_path": "Submission.lean", "old_str": "alpha", "new_str": "beta"},
            {"old_str": "private", "new_str": "changed"},
        ],
    }))
    assert not result["success"]
    assert writable.read_text() == "alpha"


@pytest.mark.parametrize("shared_path", [None, "", 0, True, [], {}])
def test_invalid_shared_path_is_rejected_even_when_an_item_overrides_it(registry, shared_path):
    path = seed(registry, "Submission.lean", "alpha")
    result = asyncio.run(registry.file_edit({
        "file_path": shared_path,
        "edits": [{"file_path": "Submission.lean", "old_str": "alpha", "new_str": "beta"}],
    }))
    assert not result["success"]
    assert "file_path" in result["error"]
    assert path.read_text() == "alpha"


@pytest.mark.parametrize("field, value", [
    ("new_str", "beta"), ("old_str", "alpha"), ("old_line_range", [1, 2]),
    ("replace_all", True), ("target_path", "Submission/copy.txt"),
])
def test_shared_path_does_not_allow_mixed_single_edit_operations(registry, field, value):
    path = seed(registry, "Submission.lean", "alpha")
    result = asyncio.run(registry.file_edit({
        "file_path": "Submission.lean", field: value,
        "edits": [{"old_str": "alpha", "new_str": "beta"}],
    }))
    assert not result["success"]
    assert "not both" in result["error"]
    assert path.read_text() == "alpha"


def test_schema_accepts_shared_and_overridden_paths_but_rejects_mixed_operations(registry):
    jsonschema = pytest.importorskip("jsonschema")
    schema = build_tools(registry)["file_edit"].parameters
    validator = jsonschema.Draft202012Validator(schema)
    assert "default" not in schema["properties"]["edits"]["items"]["properties"]["file_path"]
    shared = {"file_path": "Submission/Helper.lean", "edits": [
        {"old_str": "alpha", "new_str": "beta"},
        {"file_path": "Submission.lean", "old_str": "sorry", "new_str": "trivial"},
    ]}
    validator.validate(shared)
    validator.validate({"edits": shared["edits"]})
    for field, value in [
        ("new_str", "beta"), ("old_str", "alpha"), ("old_line_range", [1, 2]),
        ("replace_all", True), ("target_path", "Submission/copy.txt"),
    ]:
        assert not validator.is_valid({**shared, field: value})
    for path in [None, "", 0, True, [], {}]:
        assert not validator.is_valid({**shared, "file_path": path})


def test_later_line_ranges_apply_to_staged_line_numbers(registry):
    p = seed(registry, "Submission.lean", "first\nsecond\n")
    result = call_edit(registry, [
        {"old_str": "first\n", "new_str": "first\ninserted\n"},
        {"old_line_range": [3, 4], "new_str": "last"},
    ])
    assert result["success"]
    assert p.read_text() == "first\ninserted\nlast\n"


def test_later_edit_can_read_a_new_copy_without_touching_source(registry):
    p = seed(registry, "Submission/source.txt", "alpha alpha")
    result = call_edit(registry, [
        {"file_path": "Submission/source.txt", "target_path": "Submission/nested/copy.txt",
         "old_str": "alpha", "new_str": "beta", "replace_all": True},
        {"file_path": "Submission/nested/copy.txt", "old_str": "beta", "new_str": "gamma"},
    ])
    assert result["success"]
    assert p.read_text() == "alpha alpha"
    assert (p.parent / "nested/copy.txt").read_text() == "gamma beta"
    assert len(result["files"]) == 1


@pytest.mark.parametrize("bad_edit, message", [
    ({"file_path": "Submission/B.lean", "old_str": "missing", "new_str": "changed"}, "old_str not found"),
    ({"file_path": "Submission/B.lean", "old_str": "beta"}, "Missing required"),
    ({"file_path": "Submission/B.lean", "old_str": "beta", "new_str": "changed", "replace_all": 1}, "replace_all must"),
    ({"file_path": "Submission/B.lean", "old_str": "beta", "new_str": "changed", "typo": 1}, "Unknown edit"),
    (None, "Each edit must be an object"),
])
def test_invalid_later_edit_leaves_every_file_unchanged(registry, bad_edit, message):
    a = seed(registry, "Submission/A.lean", "alpha\n")
    b = seed(registry, "Submission/B.lean", "beta\n")
    result = call_edit(registry, [
        {"file_path": "Submission/A.lean", "old_str": "alpha", "new_str": "changed"}, bad_edit,
    ])
    assert not result["success"]
    assert result["error"].startswith("Edit 2:") and message in result["error"]
    assert a.read_text() == "alpha\n" and b.read_text() == "beta\n"
    assert not list(registry.workspace.workspaces_dir.rglob(".dvp-write-*"))


@pytest.mark.parametrize("destination", ["target/Test.lean", "Submission/../../escape.lean", "/tmp/escape.lean"])
def test_invalid_destination_does_not_apply_earlier_edit(registry, destination):
    a = seed(registry, "Submission.lean", "alpha")
    result = call_edit(registry, [
        {"old_str": "alpha", "new_str": "beta"},
        {"file_path": "Submission.lean", "old_str": "beta", "new_str": "gamma", "target_path": destination},
    ])
    assert not result["success"]
    assert a.read_text() == "alpha"


def test_symlink_escape_rejected_before_other_edits_are_applied(registry, tmp_path):
    original = seed(registry, "Submission.lean", "alpha")
    outside = tmp_path / "private.lean"
    outside.write_text("private")
    (registry.workspace.submission_dir / "linked.lean").symlink_to(outside)
    result = call_edit(registry, [
        {"old_str": "alpha", "new_str": "beta"},
        {"file_path": "Submission/linked.lean", "old_str": "private", "new_str": "changed"},
    ])
    assert not result["success"]
    assert original.read_text() == "alpha" and outside.read_text() == "private"


@pytest.mark.parametrize("args", [
    {"edits": []}, {"edits": None}, {"edits": {}},
    {"edits": [{"old_str": "alpha", "new_str": "beta"}], "new_str": "conflicting"},
    {"edits": [{"old_str": "alpha", "new_str": "beta"}], "unknown": True},
])
def test_invalid_batch_shape_preserves_files(registry, args):
    p = seed(registry, "Submission.lean", "alpha")
    result = asyncio.run(registry.file_edit(args))
    assert not result["success"] and p.read_text() == "alpha"


def test_failed_commit_restores_exact_bytes_and_removes_new_files(registry, monkeypatch):
    a = seed(registry, "Submission/A.lean", "alpha")
    a.write_bytes(b"alpha\r\n")
    a.chmod(0o640)
    b = seed(registry, "Submission/B.lean", "beta")
    replace = files.os.replace
    count = 0

    def fail_third(src, dst):
        nonlocal count
        count += 1
        if count == 3:
            raise OSError("simulated storage failure")
        return replace(src, dst)

    monkeypatch.setattr(files.os, "replace", fail_third)
    result = call_edit(registry, [
        {"file_path": "Submission/A.lean", "old_str": "alpha", "new_str": "changed"},
        {"file_path": "Submission/B.lean", "old_str": "beta", "new_str": "new", "target_path": "Submission/new/module.lean"},
        {"file_path": "Submission/B.lean", "old_str": "beta", "new_str": "changed"},
    ])
    assert not result["success"] and "No file contents were changed" in result["error"]
    assert a.read_bytes() == b"alpha\r\n" and b.read_text() == "beta"
    assert a.stat().st_mode & 0o777 == 0o640
    assert not (b.parent / "new").exists()
    assert not list(registry.workspace.workspaces_dir.rglob(".dvp-write-*"))


def test_staging_failure_never_starts_commit(registry, monkeypatch):
    a = seed(registry, "Submission/A.lean", "alpha")
    b = seed(registry, "Submission/B.lean", "beta")
    stage = files._stage_file

    def fail_second(path, content):
        if path == b:
            raise OSError("simulated full disk")
        return stage(path, content)

    monkeypatch.setattr(files, "_stage_file", fail_second)
    result = call_edit(registry, [
        {"file_path": "Submission/A.lean", "old_str": "alpha", "new_str": "changed"},
        {"file_path": "Submission/B.lean", "old_str": "beta", "new_str": "changed"},
    ])
    assert not result["success"]
    assert a.read_text() == "alpha" and b.read_text() == "beta"
    assert not list(registry.workspace.workspaces_dir.rglob(".dvp-write-*"))


def test_rollback_failure_is_reported_without_claiming_no_changes(registry, monkeypatch):
    a = seed(registry, "Submission/A.lean", "alpha")
    seed(registry, "Submission/B.lean", "beta")
    replace = files.os.replace
    count = 0

    def fail_after_first(src, dst):
        nonlocal count
        count += 1
        if count >= 2:
            raise OSError("storage unavailable")
        return replace(src, dst)

    monkeypatch.setattr(files.os, "replace", fail_after_first)
    result = call_edit(registry, [
        {"file_path": "Submission/A.lean", "old_str": "alpha", "new_str": "changed"},
        {"file_path": "Submission/B.lean", "old_str": "beta", "new_str": "changed"},
    ])
    assert not result["success"] and result["rollback_failed"] is True
    assert "some files may have changed" in result["error"]
    assert "No file contents were changed" not in result["error"]
    assert a.read_text() == "changed"
    assert result["files"] == [{"file_path": "Submission/A.lean"}, {"file_path": "Submission/B.lean"}]
    assert not list(registry.workspace.workspaces_dir.rglob(".dvp-write-*"))


@pytest.mark.parametrize("newline", [b"\r\n", b"\r"])
def test_file_read_text_can_be_used_in_sequential_batch_edits(registry, newline):
    p = seed(registry, "Submission.lean", "unused")
    p.write_bytes(newline.join([b"alpha", b"beta", b""]))
    result = asyncio.run(registry.file_read({"file_path": "Submission.lean"}))
    text = "\n".join(line.split("|", 1)[1] for line in result["content"].splitlines()) + "\n"
    result = call_edit(registry, [
        {"old_str": text, "new_str": "one\r\ntwo\r\n"},
        {"old_str": "one\ntwo\n", "new_str": "done\n"},
    ])
    assert result["success"] and p.read_bytes() == b"done\n"


def test_file_write_can_clear_a_lean_file_and_check_saved_contents(registry):
    p = seed(registry, "Submission.lean", "example : True := by sorry")
    result = asyncio.run(registry.file_write({"content": ""}))
    assert result["success"] and result["file_saved"]
    assert result["file_path"] == "Submission.lean" and result["bytes"] == 0
    assert result["lean_executed"] is True and result["verification_count"] == 1
    assert p.read_bytes() == b""


def test_surface_preserves_batch_mode_and_normalizes_nested_ranges(registry):
    p = seed(registry, "Submission.lean", "first\nsecond\n")
    dispatch = CompatibilitySurface.resolve("file_edit", {"edits": [{"old_line_range": "2:3", "new_str": "last"}]})
    assert "file_path" not in dispatch.backend_arguments
    result = asyncio.run(registry.file_edit(dispatch.backend_arguments))
    assert result["success"] and p.read_text() == "first\nlast\n"


@pytest.mark.parametrize("mode", ["write", "single_edit", "batch_edit"])
def test_cleanup_failure_after_commit_keeps_success_and_saved_paths(registry, monkeypatch, mode):
    path = seed(registry, "Submission.lean", "before")
    unlink = Path.unlink

    def fail_temporary_cleanup(self, *args, **kwargs):
        if self.name.startswith(".dvp-write-"):
            raise OSError("cleanup unavailable")
        return unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_temporary_cleanup)
    if mode == "write":
        result = asyncio.run(registry.file_write({"content": "after"}))
    elif mode == "single_edit":
        result = asyncio.run(registry.file_edit({"old_str": "before", "new_str": "after"}))
    else:
        result = call_edit(registry, [{"old_str": "before", "new_str": "after"}])
    assert result["success"] is True
    assert result["lean_executed"] is True
    assert "error" not in result and "rollback_failed" not in result
    assert path.read_text() == "after"
    assert all("cleanup unavailable" in warning for warning in result["warnings"])
    if mode == "batch_edit":
        assert result["files"] == [{"file_path": "Submission.lean", "bytes": 5}]
        assert result["edits_applied"] == 1
    else:
        assert result["file_path"] == "Submission.lean" and result["bytes"] == 5


@pytest.mark.parametrize("rollback_fails", [False, True])
def test_cleanup_failure_preserves_original_commit_and_rollback_outcome(registry, monkeypatch, rollback_fails):
    a = seed(registry, "Submission/A.lean", "alpha")
    b = seed(registry, "Submission/B.lean", "beta")
    replace, unlink = files.os.replace, Path.unlink
    calls = 0

    def fail_commit_and_optionally_rollback(src, dst):
        nonlocal calls
        calls += 1
        if calls == 2 or rollback_fails and calls == 3:
            raise OSError("replacement failed")
        return replace(src, dst)

    def fail_temporary_cleanup(self, *args, **kwargs):
        if self.name.startswith(".dvp-write-"):
            raise OSError("cleanup failed too")
        return unlink(self, *args, **kwargs)

    monkeypatch.setattr(files.os, "replace", fail_commit_and_optionally_rollback)
    monkeypatch.setattr(Path, "unlink", fail_temporary_cleanup)
    result = call_edit(registry, [
        {"file_path": "Submission/A.lean", "old_str": "alpha", "new_str": "changed"},
        {"file_path": "Submission/B.lean", "old_str": "beta", "new_str": "changed"},
    ])
    assert result["success"] is False
    assert "replacement failed" in result["error"]
    assert bool(result.get("rollback_failed")) is rollback_fails
    assert a.read_text() == ("changed" if rollback_fails else "alpha")
    assert b.read_text() == "beta"
    assert result["files"] == [{"file_path": "Submission/A.lean"}, {"file_path": "Submission/B.lean"}]
    assert any("cleanup failed too" in warning for warning in result["warnings"])
    for temporary in registry.workspace.workspaces_dir.rglob(".dvp-write-*"):
        unlink(temporary)


def test_stage_cleanup_failure_preserves_staging_error(registry, monkeypatch):
    path = seed(registry, "Submission.lean", "before")
    chmod, unlink = Path.chmod, Path.unlink

    def fail_stage_permissions(self, *args, **kwargs):
        if self.name.startswith(".dvp-write-"):
            raise OSError("original staging error")
        return chmod(self, *args, **kwargs)

    def fail_temporary_cleanup(self, *args, **kwargs):
        if self.name.startswith(".dvp-write-"):
            raise OSError("secondary cleanup error")
        return unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "chmod", fail_stage_permissions)
    monkeypatch.setattr(Path, "unlink", fail_temporary_cleanup)
    result = asyncio.run(registry.file_write({"content": "after"}))
    assert result["success"] is False
    assert result["error"].startswith("Could not save files: original staging error.")
    assert result["file_path"] == "Submission.lean"
    assert "rollback_failed" not in result
    assert path.read_text() == "before"
    assert any("secondary cleanup error" in warning for warning in result["warnings"])
    for temporary in registry.workspace.workspaces_dir.rglob(".dvp-write-*"):
        unlink(temporary)
