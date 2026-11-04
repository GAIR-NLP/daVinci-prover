"""Final acceptance requires both native success signals and exact build inputs."""

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dvp.verification import comparator
from dvp.verification.lean import LeanVerificationConfig, LeanVerifier


STATEMENT = "theorem target : True := by sorry\n"
CANDIDATE = "theorem target : True := by trivial\n"
SPEC = {"theorem_names": ["target"], "definition_names": [], "permitted_axioms": []}


def digest(source):
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("returncode,stdout,stderr,accepted,infrastructure", [
    (0, "Your solution is okay!\n", "", True, False),
    (0, "", "Your solution is okay!\n", True, False),
    (0, "build finished\n", "", False, True),
    (1, "Your solution is okay!\n", "kernel rejected", False, False),
    (1, "", "statement mismatch", False, False),
    (0, "long diagnostic\n" * 100 + "Your solution is okay!\n", "", True, False),
])
def test_verdict_requires_zero_exit_and_marker_from_full_output(
    tmp_path, monkeypatch, returncode, stdout, stderr, accepted, infrastructure,
):
    workspace = tmp_path / "mathlib"
    (workspace / ".lake/build/lib/lean").mkdir(parents=True)
    (workspace / "lean-toolchain").write_text("leanprover/lean4:v4.35.0\n")
    (workspace / "lake-manifest.json").write_text(json.dumps({
        "name": "mathlib", "version": "1.3.0", "packages": [],
    }))
    monkeypatch.setattr(comparator, "runtime_for", lambda _: {
        "toolchain": "/unused/unit-test-toolchain", "execution_mode": "unsandboxed",
    })
    process = AsyncMock(return_value=(returncode, stdout, stderr))
    monkeypatch.setattr(comparator, "run_process", process)
    contract = SimpleNamespace(
        statement=STATEMENT,
        comparator_spec=AsyncMock(return_value=SPEC),
    )
    verifier = LeanVerifier(LeanVerificationConfig(
        target_workspace=workspace, scratch_workspace=tmp_path, timeout=1,
    ))
    checker = comparator.ComparatorVerifier(
        verifier, STATEMENT, task_contract=contract, artifact_dir=tmp_path / "artifacts",
    )
    captured = []
    write_project = comparator.write_project

    def record_project(folder, project_workspace, challenge, solution, spec):
        write_project(folder, project_workspace, challenge, solution, spec)
        captured.append((
            (folder / "Challenge.lean").read_bytes(),
            (folder / "Solution.lean").read_bytes(),
        ))

    monkeypatch.setattr(comparator, "write_project", record_project)

    result = asyncio.run(checker.verify(CANDIDATE, max_output_chars=32))

    process.assert_awaited_once()
    contract.comparator_spec.assert_awaited_once_with(workspace, timeout=1)
    assert result["success"] is accepted
    assert bool(result.get("_harness_infrastructure_error")) is infrastructure
    assert result["returncode"] == returncode
    assert result["original_source_sha256"] == digest(STATEMENT)
    assert result["candidate_sha256"] == digest(CANDIDATE)
    assert result["solution_sha256"] == digest(CANDIDATE)
    assert captured == [(STATEMENT.encode(), CANDIDATE.encode())]
    assert "source_preparation" not in result
    artifact = Path(result["comparator"]["artifact_dir"])
    assert (artifact / "Challenge.lean").read_bytes() == STATEMENT.encode()
    assert (artifact / "candidate.lean").read_bytes() == CANDIDATE.encode()
    assert (artifact / "Solution.lean").read_bytes() == CANDIDATE.encode()
    assert not (artifact / "Challenge.build.lean").exists()
    assert not (artifact / "Solution.build.lean").exists()
    if infrastructure:
        assert result["errors"] == ["Comparator exited successfully without its acceptance marker."]
    elif accepted:
        assert result["errors"] == []
