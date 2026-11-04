"""Submission workspace layout and safe path resolution."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from dvp.runtime.submission import SUBMISSION_PATH


@dataclass
class Workspace:
    root: Path
    target_workspace: Path | None = None

    @property
    def workspaces_dir(self) -> Path:
        return self.root / "workspaces"

    @property
    def submission_dir(self) -> Path:
        return self.workspaces_dir / "Submission"

    @property
    def submission_file(self) -> Path:
        return self.workspaces_dir / SUBMISSION_PATH

    @property
    def target(self) -> Path | None:
        path = self.workspaces_dir / "target"
        return path if path.exists() else None

    def setup(self) -> None:
        self.submission_dir.mkdir(parents=True, exist_ok=True)
        if self.target_workspace:
            self._link_or_copy(self.target_workspace.resolve(), self.workspaces_dir / "target")

    def _link_or_copy(self, source: Path, dest: Path) -> None:
        if dest.exists() or dest.is_symlink():
            return
        try:
            dest.symlink_to(source, target_is_directory=True)
        except OSError:
            shutil.copytree(source, dest)

    def resolve(self, workspace_path: str, *, for_write: bool = False) -> Path:
        raw = Path(workspace_path)
        if raw.is_absolute():
            raise ValueError("Use workspace-relative paths such as Submission.lean or Submission/Count.lean")
        if workspace_path == "." and not for_write:
            return self.workspaces_dir.resolve()
        parts = raw.parts
        if not parts or parts[0] in ("", ".", ".."):
            raise ValueError("Use Submission.lean, Submission/, or target/")

        writable = parts[0] in {SUBMISSION_PATH, "Submission"}
        if parts[0] == SUBMISSION_PATH and len(parts) == 1:
            base = self.workspaces_dir
            rel = Path(SUBMISSION_PATH)
        elif parts[0] == "Submission":
            base = self.submission_dir
            rel = Path(*parts[1:])
        elif parts[0] == "target":
            if for_write:
                raise ValueError("target/ is read-only; write Submission.lean or a file under Submission/")
            if self.target is None:
                raise ValueError("target/ workspace is not configured")
            base = self.target
            rel = Path(*parts[1:])
        else:
            raise ValueError("Use Submission.lean, Submission/, or target/")

        resolved = (base / rel).resolve()
        if not resolved.is_relative_to(base.resolve()):
            raise ValueError(f"Path escapes workspace root: {workspace_path}")
        if writable and not resolved.is_relative_to(self.workspaces_dir.resolve()):
            raise ValueError(f"Path escapes submission workspace: {workspace_path}")
        return resolved
