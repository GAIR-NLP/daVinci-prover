"""Source snapshots, isolated module builds, and native modular acceptance."""

import asyncio
import hashlib
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dvp.verification import comparator
from dvp.verification.lean import LeanVerificationConfig, LeanVerifier
from dvp.verification.modules import CandidateBundle, LocalModule, collect_bundle


WORKSPACE = Path(__file__).resolve().parents[2] / "runtime/mathlib4-4.35"
STATEMENT = "import Mathlib\ntheorem target : (1 : Nat) = 1 := by sorry\n"
CANDIDATE = (
    "import Mathlib\nimport Submission.Step\n"
    "theorem target : (1 : Nat) = 1 := step\n"
)
BASE = "import Mathlib\ntheorem base : (1 : Nat) = 1 := by rfl\n"
STEP = "import Submission.Base\ntheorem step : (1 : Nat) = 1 := base\n"
SPEC = {
    "theorem_names": ["target"], "definition_names": [],
    "permitted_axioms": comparator.STANDARD_AXIOMS,
}


def bundle(entry=CANDIDATE, base=BASE):
    return CandidateBundle(entry, (
        LocalModule("Submission.Base", base), LocalModule("Submission.Step", STEP),
    ))


def fake_workspace(path):
    (path / ".lake/build/lib/lean").mkdir(parents=True)
    (path / "lean-toolchain").write_text("leanprover/lean4:v4.35.0\n")
    (path / "lake-manifest.json").write_text(json.dumps({
        "name": "mathlib", "version": "1.3.0", "packages": [],
    }))
    return path


def test_project_keeps_challenge_separate_and_builds_snapshot_in_dependency_order(tmp_path):
    workspace = fake_workspace(tmp_path / "mathlib")
    project = tmp_path / "project"
    project.mkdir()
    sources = bundle()

    comparator.write_project(project, workspace, STATEMENT, CANDIDATE, SPEC, bundle=sources)

    assert (project / "Challenge.lean").read_bytes() == STATEMENT.encode()
    assert (project / "Solution.lean").read_bytes() == CANDIDATE.encode()
    for module in sources.modules:
        path = project / module.relative_path
        assert path.read_bytes() == module.source.encode()
        assert not path.stat().st_mode & 0o222
        assert path.parent.stat().st_mode & 0o111 == 0o111
    assert not list(project.rglob("*.olean"))
    lakefile = (project / "lakefile.lean").read_text()
    challenge = lakefile.split("target Challenge", 1)[1].split("target Solution", 1)[0]
    solution = lakefile.split("target Solution", 1)[1]
    assert json.dumps(comparator.lean_search_path(workspace)) in challenge
    assert "buildDir" not in challenge and "Submission/" not in challenge
    assert '(pkg.buildDir / "lib" / "lean").toString' in solution
    assert '#["Submission/Base", "Submission/Step"]' in solution
    assert solution.index("for name") < solution.index('compileSource pkg "Solution"')


def test_project_rejects_bundle_for_a_different_entry_before_writing(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    with pytest.raises(ValueError, match="bundle entry"):
        comparator.write_project(project, tmp_path, STATEMENT, "different", SPEC, bundle=bundle())
    assert list(project.iterdir()) == []


@pytest.fixture
def mocked_checker(tmp_path, monkeypatch):
    workspace = fake_workspace(tmp_path / "mathlib")
    verifier = LeanVerifier(LeanVerificationConfig(
        target_workspace=workspace, scratch_workspace=tmp_path / "workspaces/Submission",
    ))
    checker = comparator.ComparatorVerifier(
        verifier, STATEMENT, artifact_dir=tmp_path / "artifacts",
    )
    checker._prepare = AsyncMock(return_value=(CANDIDATE, SPEC))
    monkeypatch.setattr(comparator, "runtime_for", lambda _: {
        "toolchain": "/unused/unit-test-toolchain", "execution_mode": "unsandboxed",
    })
    process = AsyncMock(return_value=(0, comparator.SUCCESS_LINE, ""))
    monkeypatch.setattr(comparator, "run_process", process)
    return checker, process


def test_verdict_and_artifacts_bind_entry_and_every_auxiliary_source(mocked_checker):
    checker, process = mocked_checker
    sources = bundle()
    captured = {}

    async def inspect_build(command, *, cwd, timeout):
        captured["challenge"] = (cwd / "Challenge.lean").read_text()
        captured["solution"] = (cwd / "Solution.lean").read_text()
        captured["modules"] = {
            module.name: (cwd / module.relative_path).read_text() for module in sources.modules
        }
        return 0, comparator.SUCCESS_LINE, ""

    process.side_effect = inspect_build
    result = asyncio.run(checker.verify(CANDIDATE, bundle=sources))

    assert result["success"], result
    assert result["candidate_sha256"] == hashlib.sha256(CANDIDATE.encode()).hexdigest()
    assert result["source_bundle_sha256"] == sources.sha256
    assert result["source_bundle_manifest"] == sources.manifest()
    assert result["solution_bundle_sha256"] == sources.sha256
    assert result["solution_bundle_sha256"] == result["source_bundle_sha256"]
    assert captured == {
        "challenge": STATEMENT, "solution": CANDIDATE,
        "modules": {module.name: module.source for module in sources.modules},
    }
    artifact = Path(result["comparator"]["artifact_dir"])
    assert (artifact / "candidate.lean").read_text() == CANDIDATE
    assert json.loads((artifact / "source_bundle.json").read_text()) == sources.manifest()
    for module in sources.modules:
        assert (artifact / "modules" / module.relative_path).read_text() == module.source
    assert not list(artifact.rglob("*.olean"))


def test_supplied_bundle_entry_mismatch_cannot_run_comparator(mocked_checker):
    checker, process = mocked_checker
    result = asyncio.run(checker.verify("different", bundle=bundle()))
    assert not result["success"] and result["command"] is None
    assert "bundle entry" in " ".join(result["errors"])
    assert not result.get("_harness_infrastructure_error")
    checker._prepare.assert_not_awaited()
    process.assert_not_awaited()


def test_fixed_contract_supplies_original_obligations_without_candidate_rewriting(mocked_checker):
    checker, _ = mocked_checker
    contract = SimpleNamespace(
        comparator_spec=AsyncMock(return_value=SPEC),
    )
    checker.task_contract = contract
    checker._prepare = comparator.ComparatorVerifier._prepare.__get__(checker)

    result = asyncio.run(checker.verify(CANDIDATE, bundle=bundle()))

    assert result["success"], result
    contract.comparator_spec.assert_awaited_once_with(
        checker._verifier.config.target_workspace, timeout=checker._verifier.config.timeout,
    )
    assert result["solution_sha256"] == comparator.digest(CANDIDATE)
    assert result["source_bundle_sha256"] == result["solution_bundle_sha256"]


def test_default_collection_freezes_configured_module_root(mocked_checker, tmp_path):
    checker, process = mocked_checker
    root = tmp_path / "workspaces"
    for module in bundle().modules:
        path = root / module.relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(module.source)
    checker._verifier.config.module_root = root
    expected = collect_bundle(CANDIDATE, root)
    result = asyncio.run(checker.verify(CANDIDATE))
    assert result["success"], result
    assert result["source_bundle_sha256"] == expected.sha256
    process.assert_awaited_once()


@pytest.fixture
def native_checker(tmp_path):
    if shutil.which("lake") is None or not (WORKSPACE / "lean-toolchain").is_file():
        pytest.skip("local Lean 4.35 workspace/lake unavailable")
    try:
        comparator.runtime_for(WORKSPACE)
    except (OSError, RuntimeError, ValueError, KeyError) as error:
        pytest.skip(f"selected Lean 4.35 Comparator runtime unavailable: {error}")
    verifier = LeanVerifier(LeanVerificationConfig(
        target_workspace=WORKSPACE, scratch_workspace=tmp_path / "Submission", timeout=90,
    ))
    return comparator.ComparatorVerifier(verifier, STATEMENT, artifact_dir=tmp_path / "artifacts")


@pytest.mark.parametrize("base,entry,accepted", [
    pytest.param(BASE, CANDIDATE, True, id="two-module-proof"),
    pytest.param(
        "import Mathlib\naxiom base : (1 : Nat) = 1\n", CANDIDATE, False,
        id="auxiliary-invented-axiom",
    ),
    pytest.param(BASE.replace("by rfl", "by sorry"), CANDIDATE, False, id="auxiliary-sorry"),
    pytest.param(
        BASE, CANDIDATE.replace("(1 : Nat) = 1 := step", "True := by trivial"), False,
        id="changed-original-theorem",
    ),
])
def test_native_modular_submission_keeps_kernel_axiom_and_original_checks(
    native_checker, base, entry, accepted,
):
    sources = bundle(entry, base)
    result = asyncio.run(native_checker.verify(entry, bundle=sources))
    assert result["success"] is accepted, result
    assert not result.get("_harness_infrastructure_error"), result
    assert result["source_bundle_sha256"] == sources.sha256
    assert result["original_source_sha256"] == comparator.digest(STATEMENT)
    assert result["returncode"] == (0 if accepted else 1), result
    assert "Lean compilation failed" not in result["output"], result


def test_native_uses_frozen_sources_and_never_workspace_olean(native_checker, tmp_path):
    root = tmp_path / "workspace"
    for module in bundle().modules:
        path = root / module.relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(module.source)
    frozen = collect_bundle(CANDIDATE, root)
    source = root / "Submission/Base.lean"
    source.write_text(BASE.replace("by rfl", "by exact nonexistent_proof"))
    source.with_suffix(".olean").write_bytes(b"untrusted stale cache")

    accepted = asyncio.run(native_checker.verify(CANDIDATE, bundle=frozen))
    changed = collect_bundle(CANDIDATE, root)
    rejected = asyncio.run(native_checker.verify(CANDIDATE, bundle=changed))

    assert accepted["success"], accepted
    assert not rejected["success"] and rejected["returncode"] == 1, rejected
    assert "Lean compilation failed" in rejected["output"], rejected
    assert accepted["candidate_sha256"] == rejected["candidate_sha256"]
    assert accepted["source_bundle_sha256"] != rejected["source_bundle_sha256"]
