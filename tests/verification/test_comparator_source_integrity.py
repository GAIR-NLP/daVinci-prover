"""Native Comparator receives original challenge and submission sources intact."""

import asyncio
import hashlib
import json
from pathlib import Path
import shutil

import pytest

from dvp.verification import comparator as comparator_module
from dvp.verification.comparator import ComparatorVerifier, runtime_for
from dvp.verification.lean import LeanVerificationConfig, LeanVerifier


WORKSPACE = Path(__file__).resolve().parents[2] / "runtime/mathlib4-4.35"
STATEMENT = "import Lean\ntheorem target : (1 : Nat) = 1 := by sorry\n"
INDEXED_HEADER = (
    "(P : List ℕ) (i : Fin (P.length - 1)) : P[i] = P[i]"
)
# The official Comparator rejects this helper-induced generated-proof mismatch.
# DVP must preserve that verdict instead of transforming either source.
INDEXED_STATEMENT = f"import Mathlib\n\ntheorem target {INDEXED_HEADER} := by sorry\n"


def digest(source):
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


@pytest.fixture
def local_verifier(tmp_path):
    if shutil.which("lake") is None or not (WORKSPACE / "lean-toolchain").is_file():
        pytest.skip("local Lean 4.35 workspace/lake unavailable")
    try:
        # Respect the caller's selected runtime and execution mode.
        runtime_for(WORKSPACE)
    except (OSError, RuntimeError, ValueError, KeyError) as error:
        pytest.skip(f"selected Lean 4.35 Comparator runtime unavailable: {error}")
    return LeanVerifier(LeanVerificationConfig(
        target_workspace=WORKSPACE, scratch_workspace=tmp_path,
        timeout=60, allow_sorry=False,
    ))


@pytest.fixture
def project_sources(monkeypatch):
    captured = []
    write_project = comparator_module.write_project

    def record_project(folder, workspace, challenge, solution, spec):
        write_project(folder, workspace, challenge, solution, spec)
        captured.append((
            (folder / "Challenge.lean").read_bytes(),
            (folder / "Solution.lean").read_bytes(),
        ))

    monkeypatch.setattr(comparator_module, "write_project", record_project)
    return captured


@pytest.mark.parametrize("statement,candidate,accepted", [
    pytest.param(
        INDEXED_STATEMENT,
        "import Mathlib\n\n"
        f"lemma helper {INDEXED_HEADER} := by rfl\n\n"
        f"theorem target {INDEXED_HEADER} := by rfl\n",
        False, id="shared-index-proof-helper",
    ),
    pytest.param(
        STATEMENT,
        "import Lean\ntheorem target : ((1 : Nat) = 1) := by rfl\n",
        True, id="equivalent-parenthesized-header",
    ),
    pytest.param(
        STATEMENT,
        "import Lean\n#eval (20 + 22 : Nat)\n"
        "theorem target : (1 : Nat) = 1 := by rfl\n#print axioms target\n",
        True, id="inspection-commands-preserve-proof-acceptance",
    ),
    pytest.param(
        STATEMENT,
        "import Lean\ntheorem target : True := by trivial\n",
        False, id="wrong-same-name-statement",
    ),
    pytest.param(
        "import Lean\ndef predicate : Prop := False\ntheorem target : predicate := by sorry\n",
        "import Lean\ndef predicate : Prop := True\ntheorem target : predicate := by trivial\n",
        False, id="changed-dependency-definition",
    ),
    pytest.param(STATEMENT, STATEMENT, False, id="sorry-proof"),
    pytest.param(
        STATEMENT,
        "import Lean\naxiom invented : (1 : Nat) = 1\n"
        "theorem target : (1 : Nat) = 1 := invented\n",
        False, id="new-axiom-proof",
    ),
    pytest.param(
        STATEMENT,
        "import Lean\nnamespace Shadow\n"
        "theorem target : (1 : Nat) = 1 := by rfl\nend Shadow\n",
        False, id="namespace-decoy",
    ),
])
def test_native_acceptance_preserves_original_sources_and_trust_boundary(
    local_verifier, tmp_path, project_sources, statement, candidate, accepted,
):
    original_path = tmp_path / "original.lean"
    candidate_path = tmp_path / "submitted.lean"
    original_path.write_bytes(statement.encode("utf-8"))
    candidate_path.write_bytes(candidate.encode("utf-8"))
    comparator = ComparatorVerifier(
        local_verifier, statement, artifact_dir=tmp_path / "artifacts",
    )

    result = asyncio.run(comparator.verify(candidate))

    assert result["success"] is accepted, result
    assert not result.get("_harness_infrastructure_error"), result
    assert result["backend"] == "leanprover/comparator"
    assert result["original_source_sha256"] == digest(statement)
    assert result["candidate_sha256"] == digest(candidate)
    assert result["solution_sha256"] == digest(candidate)
    assert project_sources == [(statement.encode("utf-8"), candidate.encode("utf-8"))]
    assert comparator.statement == statement
    assert original_path.read_bytes() == statement.encode("utf-8")
    assert candidate_path.read_bytes() == candidate.encode("utf-8")
    [artifact] = list(comparator.artifact_dir.iterdir())
    assert (artifact / "Challenge.lean").read_bytes() == statement.encode("utf-8")
    assert (artifact / "candidate.lean").read_bytes() == candidate.encode("utf-8")
    assert (artifact / "Solution.lean").read_bytes() == candidate.encode("utf-8")
    assert json.loads((artifact / "result.json").read_text()) == result

    assert "source_preparation" not in result
    assert not (artifact / "Challenge.build.lean").exists()
    assert not (artifact / "Solution.build.lean").exists()
    assert result["command"] is not None
    assert result["returncode"] == (0 if accepted else 1), result
    assert "Lean compilation failed" not in result["output"], result
    assert (artifact / "comparator.log").is_file()
    if accepted:
        assert "Your solution is okay!" in result["output"]


def test_missing_original_target_keeps_inputs_and_never_builds(local_verifier, tmp_path, project_sources):
    statement = "import Lean\n-- The original has no target declaration.\n"
    candidate = "import Lean\ntheorem target : True := by trivial\n"
    comparator = ComparatorVerifier(
        local_verifier, statement, artifact_dir=tmp_path / "artifacts",
    )

    result = asyncio.run(comparator.verify(candidate))

    assert not result["success"] and not result.get("_harness_infrastructure_error"), result
    assert "no named target theorem" in " ".join(result["errors"])
    assert result["command"] is None
    assert project_sources == []
    assert "source_preparation" not in result
    assert result["original_source_sha256"] == digest(statement)
    assert result["candidate_sha256"] == digest(candidate)
    [artifact] = list(comparator.artifact_dir.iterdir())
    assert (artifact / "Challenge.lean").read_bytes() == statement.encode("utf-8")
    assert (artifact / "candidate.lean").read_bytes() == candidate.encode("utf-8")
    assert not (artifact / "Challenge.build.lean").exists()
    assert not (artifact / "Solution.build.lean").exists()


def test_import_sensitive_original_is_compared_without_changes(local_verifier, tmp_path, project_sources):
    # Adding `import Lean` to this immutable source changes `sensitive` from
    # False to True. Verification must not make a changed candidate valid.
    statement = (
        "import Lean.Elab.ElabRules\n"
        'elab "sensitive" : term => do\n'
        "  return Lean.mkConst (if (← Lean.getEnv).contains "
        "`Lean.Server.Logging.LogConfig then ``True else ``False)\n"
        "theorem target : sensitive := by sorry\n"
        "#print target\n"
    )
    candidate = "import Lean\n" + statement.replace("by sorry", "by trivial")
    comparator = ComparatorVerifier(
        local_verifier, statement, artifact_dir=tmp_path / "artifacts",
    )

    result = asyncio.run(comparator.verify(candidate))

    assert not result["success"], result
    assert not result.get("_harness_infrastructure_error"), result
    assert result["command"] is not None and result["returncode"] == 1, result
    assert "Lean compilation failed" not in result["output"], result
    assert result["original_source_sha256"] == digest(statement)
    assert result["candidate_sha256"] == digest(candidate)
    assert result["solution_sha256"] == digest(candidate)
    assert project_sources == [(statement.encode("utf-8"), candidate.encode("utf-8"))]
    assert "source_preparation" not in result
    artifact = Path(result["comparator"]["artifact_dir"])
    assert (artifact / "Challenge.lean").read_bytes() == statement.encode("utf-8")
    assert (artifact / "candidate.lean").read_bytes() == candidate.encode("utf-8")
    assert (artifact / "Solution.lean").read_bytes() == candidate.encode("utf-8")
    assert not (artifact / "Challenge.build.lean").exists()
    assert not (artifact / "Solution.build.lean").exists()
    log = (artifact / "comparator.log").read_text()
    assert "target : False" in log and "target : True" in log, log
