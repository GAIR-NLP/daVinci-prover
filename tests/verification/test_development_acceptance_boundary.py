"""Ordinary Lean development cannot substitute for source-bound Comparator acceptance."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from dvp.agent.completion import comparator_evidence
from dvp.factory import build_services
from dvp.protocol import ToolOutcome
from dvp.verification.comparator import STANDARD_AXIOMS, digest
from dvp.verification.modules import collect_bundle
from test_fixed_task_contract import contract_for
from test_lean435_local_pipeline import WORKSPACE, local_runtime


def services_for(tmp_path, statement, *, fixed):
    return build_services(
        theorem_statement=statement,
        runtime=SimpleNamespace(target_workspace=WORKSPACE, lean_timeout=120),
        run_dir=tmp_path / "run",
        task_contract=contract_for(statement) if fixed else None,
    )


def develop_and_accept(services, candidate):
    path = services.workspace.submission_file
    path.write_text(candidate)

    async def check():
        development = await services.verifier.verify_file(path)
        assert development["success"], development
        assert not development.get("errors"), development
        assert development.get("command"), development
        acceptance = await services.contract_verifier.verify(candidate)
        assert not acceptance.get("_harness_infrastructure_error"), acceptance
        return development, acceptance

    return asyncio.run(check())


def assert_source_identity(services, statement, candidate, result):
    original = services.contract_verifier.statement
    assert original == statement.strip() or original == statement
    bundle = collect_bundle(candidate, services.workspace.workspaces_dir)
    assert result["original_source_sha256"] == digest(original)
    assert result["candidate_sha256"] == result["solution_sha256"] == digest(candidate)
    assert result["source_bundle_sha256"] == result["solution_bundle_sha256"] == bundle.sha256
    assert result["source_bundle_manifest"] == bundle.manifest()
    artifact = Path(result["comparator"]["artifact_dir"])
    assert (artifact / "Challenge.lean").read_text() == original
    assert (artifact / "candidate.lean").read_text() == candidate
    assert (artifact / "Solution.lean").read_text() == candidate


@pytest.mark.parametrize("fixed", [False, True])
def test_ordinary_development_constructs_and_local_imports_can_pass_comparator(
    tmp_path, local_runtime, fixed,
):
    statement = "import Lean\ntheorem target : True := by sorry\n"
    services = services_for(tmp_path, statement, fixed=fixed)
    (services.workspace.submission_dir / "Helpers.lean").write_text(
        "import Lean\nopaque helperProof : True := True.intro\n"
    )
    candidate = (
        "import Lean\nimport Submission.Helpers\n"
        "opaque entryProof : True := helperProof\n"
        'local notation "Goal" => True\n'
        "theorem target : Goal := entryProof\n"
    )
    development, result = develop_and_accept(services, candidate)
    assert result["success"], result
    assert development["compiled_modules"] == ["Submission.Helpers"]
    assert_source_identity(services, statement, candidate, result)

    # Even genuine accepted evidence is unusable after any identity is changed.
    episode = SimpleNamespace(
        task_contract=services.contract_verifier.task_contract,
        _source_theorem_statement=statement,
    )
    outcome = ToolOutcome(
        call_id="check", name="lean_verify", backend_name="lean_verify",
        arguments={"code": candidate}, executed=True, valid=True,
        verified=True, terminal=False, reward=0.0,
        raw={
            "contract_verification": result,
            "source_bundle_sha256": development["source_bundle_sha256"],
            "source_bundle_manifest": development["source_bundle_manifest"],
        },
    )
    assert comparator_evidence(episode, outcome)
    for key in ("original_source_sha256", "candidate_sha256", "source_bundle_sha256"):
        raw = deepcopy(outcome.raw)
        raw["contract_verification"][key] = "0" * 64
        assert not comparator_evidence(episode, replace(outcome, raw=raw)), key


@pytest.mark.parametrize("fixed", [False, True])
@pytest.mark.parametrize("statement,candidate", [
    pytest.param(
        "import Lean\ntheorem target : False := by sorry\n",
        "import Lean\naxiom invented : False\ntheorem target : False := invented\n",
        id="new-axiom",
    ),
    pytest.param(
        "import Lean\ntheorem target : False := by sorry\n",
        "import Lean\ntheorem target : True := True.intro\n",
        id="changed-statement",
    ),
    pytest.param(
        "import Lean\ndef original : Prop := False\ntheorem target : original := by sorry\n",
        "import Lean\ndef original : Prop := True\ntheorem target : original := True.intro\n",
        id="changed-definition",
    ),
])
def test_compiling_invalid_submission_is_rejected_by_comparator(
    tmp_path, local_runtime, fixed, statement, candidate,
):
    services = services_for(tmp_path, statement, fixed=fixed)
    _, result = develop_and_accept(services, candidate)
    assert not result["success"], result
    assert result["returncode"] != 0, result
    assert result["command"], result
    assert result["comparator"]["specification"]["permitted_axioms"] == STANDARD_AXIOMS
    assert_source_identity(services, statement, candidate, result)


@pytest.mark.parametrize("fixed", [False, True])
def test_only_original_axioms_extend_the_permitted_axiom_set(tmp_path, local_runtime, fixed):
    statement = "import Lean\naxiom sourceFact : False\ntheorem target : False := by sorry\n"
    candidate = statement.replace("by sorry", "sourceFact")
    services = services_for(tmp_path, statement, fixed=fixed)
    _, result = develop_and_accept(services, candidate)
    assert result["success"], result
    assert result["comparator"]["specification"]["permitted_axioms"] == [*STANDARD_AXIOMS, "sourceFact"]
    assert_source_identity(services, statement, candidate, result)


def test_fixed_original_helper_hole_remains_a_final_obligation(tmp_path, local_runtime):
    statement = (
        "import Lean\ntheorem requiredHelper : True := by sorry\n"
        "theorem target : True := by sorry\n"
    )
    # Ordinary Lean accepts this partial file, including its sorry warning.
    candidate = statement.replace("theorem target : True := by sorry", "theorem target : True := True.intro")
    services = services_for(tmp_path, statement, fixed=True)
    development, result = develop_and_accept(services, candidate)
    assert development["warnings"], development
    assert not result["success"], result
    assert result["returncode"] != 0, result
    assert set(result["comparator"]["specification"]["theorem_names"]) == {"target", "requiredHelper"}
    assert_source_identity(services, statement, candidate, result)


def test_fixed_submission_must_now_include_its_compilable_context(tmp_path, local_runtime):
    statement = "import Lean\ndef original : Nat := 1\ntheorem target : original = 1 := by sorry\n"
    candidate = "import Lean\ntheorem target : original = 1 := by rfl\n"
    services = services_for(tmp_path, statement, fixed=True)
    path = services.workspace.submission_file
    path.write_text(candidate)
    result = asyncio.run(services.verifier.verify_file(path))
    assert not result["success"], result
    assert "original" in "\n".join(result["errors"])
    assert path.read_text() == candidate
