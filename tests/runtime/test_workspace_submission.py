"""Submission layout, default seeding, and workspace path boundaries."""
from pathlib import Path

import pytest

from dvp.runtime.submission import SUBMISSION_PATH, seed_submission
from dvp.runtime.workspace import Workspace


def test_submission_layout_and_verbatim_seed(tmp_path):
    workspace = Workspace(tmp_path / "run")
    workspace.setup()
    assert workspace.submission_dir.is_dir()
    assert not (workspace.workspaces_dir / "scratch").exists()
    assert not (workspace.workspaces_dir / "sketch").exists()
    assert not hasattr(workspace, "scratch") and not hasattr(workspace, "sketch")
    source = "import Mathlib\r\n-- α\r\ntheorem target : True := by sorry\r\n"
    assert seed_submission(workspace, source) == workspace.submission_file
    assert SUBMISSION_PATH == "Submission.lean"
    assert workspace.submission_file.read_bytes() == source.encode()
    assert workspace.resolve(".") == workspace.workspaces_dir.resolve()
    assert workspace.resolve("Submission/Count.lean", for_write=True) == workspace.submission_dir / "Count.lean"


@pytest.mark.parametrize("path", [
    "scratch/solution.lean", "sketch/Lemmas.lean", "solution.lean",
    "Submission.lean/extra", "Submission/../../outside.lean",
    "Submission/../target/Main.lean", "/tmp/Submission.lean", "../Submission.lean", ".", "",
])
def test_invalid_or_retired_write_paths_are_rejected(tmp_path, path):
    workspace = Workspace(tmp_path / "run")
    workspace.setup()
    with pytest.raises(ValueError):
        workspace.resolve(path, for_write=True)


@pytest.mark.parametrize("location", ["Submission.lean", "Submission/Escape.lean", "Submission"])
def test_submission_symlinks_cannot_escape_workspace(tmp_path, location):
    workspace = Workspace(tmp_path / "run")
    workspace.setup()
    outside = tmp_path / "outside"
    outside.mkdir()
    destination = outside / "proof.lean"
    destination.write_text("original")
    link = workspace.workspaces_dir / location
    if location == "Submission":
        link.rmdir()
        link.symlink_to(outside, target_is_directory=True)
        path = "Submission/proof.lean"
    else:
        link.symlink_to(destination)
        path = location
    with pytest.raises(ValueError, match="escapes"):
        workspace.resolve(path, for_write=True)
    assert destination.read_text() == "original"


def test_target_remains_read_only(tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    (library / "Main.lean").write_text("import Mathlib")
    workspace = Workspace(tmp_path / "run", target_workspace=library)
    workspace.setup()
    assert workspace.resolve("target/Main.lean") == library / "Main.lean"
    with pytest.raises(ValueError, match="read-only"):
        workspace.resolve("target/Main.lean", for_write=True)
