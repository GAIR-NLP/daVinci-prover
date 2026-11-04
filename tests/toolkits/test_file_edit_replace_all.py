"""Exercise file-edit replacement semantics through actual temporary files."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from dvp.toolkits import lean
from dvp.toolkits.registry import ToolRegistry
from dvp.runtime.workspace import Workspace


@pytest.fixture
def registry(tmp_path: Path, monkeypatch) -> ToolRegistry:
    workspace = Workspace(root=tmp_path / "run")
    workspace.setup()
    tool_registry = object.__new__(ToolRegistry)
    tool_registry.workspace = workspace
    monkeypatch.setattr(lean, "lean_verify", AsyncMock(return_value={
        "success": True, "lean_executed": True,
    }))
    return tool_registry


@pytest.mark.parametrize(
    ("original", "old_str", "new_str", "options", "expected"),
    [
        ("alpha alpha\nalpha\n", "alpha", "beta", {}, "beta alpha\nalpha\n"),
        ("alpha alpha\nalpha\n", "alpha", "beta", {"replace_all": False}, "beta alpha\nalpha\n"),
        ("alpha alpha\nalpha\n", "alpha", "beta", {"replace_all": True}, "beta beta\nbeta\n"),
        ("  exact α\n  exact α\n", "  exact α\n", "  exact β\n",
         {"replace_all": True}, "  exact β\n  exact β\n"),
        ("x x", "x", "xx", {"replace_all": True}, "xx xx"),
        ("keep\ndelete\ndelete\n", "delete\n", "", {}, "keep\ndelete\n"),
        ("keep\ndelete\ndelete\n", "delete\n", "", {"replace_all": True}, "keep\n"),
        ("delete\n", "delete\n", "", {}, ""),
    ],
)
def test_file_edit_replacement_modes(
    registry: ToolRegistry, original, old_str, new_str, options, expected,
) -> None:
    source = registry.workspace.submission_dir / "note.txt"
    source.write_text(original, encoding="utf-8")

    result = asyncio.run(registry.file_edit({
        "file_path": "Submission/note.txt",
        "old_str": old_str,
        "new_str": new_str,
        **options,
    }))

    assert result["success"], result
    assert source.read_text(encoding="utf-8") == expected
    assert result["bytes"] == len(expected.encode("utf-8"))
    assert "diff" not in result


def test_file_edit_line_range_takes_priority_over_replace_all(registry: ToolRegistry) -> None:
    source = registry.workspace.submission_dir / "note.txt"
    source.write_text("alpha\nalpha\nalpha\n", encoding="utf-8")

    result = asyncio.run(registry.file_edit({
        "file_path": "Submission/note.txt",
        "old_str": "alpha",
        "new_str": "beta",
        "old_line_range": [2, 3],
        "replace_all": True,
    }))

    assert result["success"], result
    assert source.read_text(encoding="utf-8") == "alpha\nbeta\nalpha\n"


@pytest.mark.parametrize("selection, expected", [
    ({"old_str": "-- temporary\n"}, "example : True := by trivial\n"),
    ({"old_line_range": [1, 2]}, "example : True := by trivial\n"),
    ({"old_line_range": [1, 3]}, ""),
])
def test_empty_replacement_deletes_and_checks_saved_lean(
    registry: ToolRegistry, selection, expected,
) -> None:
    source = registry.workspace.submission_file
    source.write_text("-- temporary\nexample : True := by trivial\n", encoding="utf-8")
    arguments = {"file_path": "Submission.lean", "new_str": "", **selection}
    result = asyncio.run(registry.file_edit(arguments))

    assert result["success"], result
    assert source.read_text(encoding="utf-8") == expected
    assert result["lean_executed"] is True


@pytest.mark.parametrize("replacement, error", [
    ({}, "Missing required argument(s): new_str"),
    ({"new_str": None}, "Missing required argument(s): new_str"),
    ({"new_str": False}, "TypeError: new_str must be a string"),
    ({"new_str": 0}, "TypeError: new_str must be a string"),
    ({"new_str": []}, "TypeError: new_str must be a string"),
    ({"file_path": "", "new_str": ""}, "Missing required argument(s): file_path"),
])
def test_empty_replacement_support_preserves_argument_validation(
    registry: ToolRegistry, replacement, error,
) -> None:
    source = registry.workspace.submission_dir / "note.txt"
    source.write_text("keep\ndelete\n", encoding="utf-8")
    arguments = {"file_path": "Submission/note.txt", "old_str": "delete\n", **replacement}

    result = asyncio.run(registry.file_edit(arguments))

    assert result == {"success": False, "error": error}
    assert source.read_text(encoding="utf-8") == "keep\ndelete\n"


def test_file_edit_missing_match_preserves_source(registry: ToolRegistry) -> None:
    source = registry.workspace.submission_dir / "note.txt"
    source.write_text("alpha alpha", encoding="utf-8")

    result = asyncio.run(registry.file_edit({
        "file_path": "Submission/note.txt",
        "old_str": "missing",
        "new_str": "beta",
        "replace_all": True,
    }))

    assert not result["success"]
    assert result["error"] == "old_str not found"
    assert source.read_text(encoding="utf-8") == "alpha alpha"


def test_file_edit_replace_all_to_another_file_preserves_source(registry: ToolRegistry) -> None:
    source = registry.workspace.submission_dir / "note.txt"
    source.write_text("alpha alpha", encoding="utf-8")

    result = asyncio.run(registry.file_edit({
        "file_path": "Submission/note.txt",
        "target_path": "Submission/copy.txt",
        "old_str": "alpha",
        "new_str": "beta",
        "replace_all": True,
    }))

    assert result["success"], result
    assert result["file_path"] == "Submission/copy.txt"
    assert source.read_text(encoding="utf-8") == "alpha alpha"
    assert (registry.workspace.submission_dir / "copy.txt").read_text(encoding="utf-8") == "beta beta"


def test_replace_all_checks_complete_saved_lean_file(
    registry: ToolRegistry,
) -> None:
    source = registry.workspace.submission_file
    source.write_text(
        "example : True := by exact old_lemma\nexample : True := by exact old_lemma\n",
        encoding="utf-8",
    )
    expected = "example : True := by exact True.intro\nexample : True := by exact True.intro\n"
    result = asyncio.run(registry.file_edit({
        "file_path": "Submission.lean",
        "old_str": "old_lemma",
        "new_str": "True.intro",
        "replace_all": True,
    }))

    assert result["success"], result
    assert result["lean_executed"] is True
    assert source.read_text(encoding="utf-8") == expected
