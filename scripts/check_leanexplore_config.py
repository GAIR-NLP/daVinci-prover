"""Check the pinned retrieval snapshot and proof workspace without running Lean."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess


LEAN_VERSION = "4.35.0"
MATHLIB_COMMIT = "56a005855ea17e0b0958f73ec801ae140b0a1679"


def validate_configuration(snapshot: Path, workspace: Path) -> None:
    snapshot = snapshot.expanduser().resolve(strict=True)
    workspace = workspace.expanduser().resolve(strict=True)
    build = json.loads((snapshot / "build-manifest.json").read_text())
    indices = json.loads((snapshot / "index-build-report.json").read_text())
    if (
        build.get("full_semantic_ready") is not True
        or build.get("lean_version") != LEAN_VERSION
        or build.get("mathlib_commit") != MATHLIB_COMMIT
        or indices.get("status") != "complete_semantic_index"
    ):
        raise ValueError("Snapshot is incomplete or does not match the pinned Lean / Mathlib versions")
    if (workspace / "lean-toolchain").read_text().strip() != f"leanprover/lean4:v{LEAN_VERSION}":
        raise ValueError(f"Workspace must use Lean {LEAN_VERSION}")
    mathlib = workspace if (workspace / "Mathlib").is_dir() else workspace / ".lake/packages/mathlib"
    actual = subprocess.check_output(
        ["git", "-C", str(mathlib), "rev-parse", "HEAD"], text=True,
    ).strip()
    if actual != MATHLIB_COMMIT:
        raise ValueError(f"Workspace Mathlib commit is {actual}, expected {MATHLIB_COMMIT}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    args = parser.parse_args()
    try:
        validate_configuration(args.snapshot, args.workspace)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"LeanExplore configuration check failed: {error}") from error
    print("LeanExplore snapshot and workspace versions match. Run check_leanexplore_local.py to test retrieval.")


if __name__ == "__main__":
    main()
