"""Fixed-source metadata identifies original obligations; Comparator judges raw candidates.

The fixture statements retain archived source shapes. Development compilation
uses ordinary Lean, with no reconstruction of omitted benchmark context.
"""

import asyncio
import json
from pathlib import Path
import re

import pytest

from dvp.verification.fixed_context import FixedTaskContract, sha256
from dvp.verification.lean import LeanVerificationConfig, LeanVerifier, mask_lean_comments_and_strings
from dvp.verification.comparator import ComparatorVerifier, BACKEND
from test_lean435_local_pipeline import WORKSPACE, local_runtime


MATHLIB = WORKSPACE
CASES = json.loads((Path(__file__).resolve().parents[1] / "fixtures/fixed_task_contract.json").read_text())
BY_NAME = {case["name"]: case for case in CASES}
IMPORT = "import Mathlib.Util.AssertNoSorry\n"


def fixture_metadata(statement: str) -> dict:
    """Describe fixtures whose complete named proof bodies are sorry/by sorry."""
    statement = statement.strip()
    masked = mask_lean_comments_and_strings(statement)
    declarations = list(re.finditer(
        r"(?m)^[ \t]*(?P<start>(?:(?:noncomputable|private|protected)\s+)*)"
        r"(?P<kind>theorem|lemma|def)\s+(?P<name>[^\s:({]+)", masked,
    ))
    slots = []
    for assignment in re.finditer(r":=\s*(?:by\s+)?(?P<hole>sorry)\b", masked):
        declaration = [d for d in declarations if d.start("start") < assignment.start()][-1]
        start = declaration.start("start")
        scopes = []
        for block in re.finditer(r"(?m)^\s*(namespace|section|end)\b([^\r\n]*)", masked[:start]):
            word, tail = block.groups()
            if word == "end":
                scopes.pop()
            else:
                scopes.append((word, tail.strip()))
        full_name = ".".join([name for kind, name in scopes if kind == "namespace"] + [declaration["name"]])
        slots.append({
            "name": full_name,
            "kind": declaration["kind"],
            "declaration_span": [start, assignment.end()],
            "signature_span": [start, assignment.start()],
            "assignment_span": [assignment.start(), assignment.start() + 2],
            "proof_span": [assignment.start() + 2, assignment.end()],
            "placeholder_span": [assignment.start("hole"), assignment.end()],
        })
    assert slots, "Fixture must contain at least one original proof obligation"
    for index, slot in enumerate(slots):
        slot["role"] = "target" if index == len(slots) - 1 else "helper"
    target = slots[-1]
    return {
        "task_id": "regression_fixture",
        "effective_statement_sha256": sha256(statement),
        "statement_sha256": sha256(statement),
        "target_name": target["name"],
        "target_kind": target["kind"],
        "trusted_header_span": [0, target["declaration_span"][0]],
        "formal_statement_span": target["declaration_span"],
        "footer_span": [target["declaration_span"][1], len(statement)],
        "target_signature_span": target["signature_span"],
        "target_proof_span": target["proof_span"],
        "proof_slots": slots,
    }


def contract_for(statement: str) -> FixedTaskContract:
    return FixedTaskContract.from_metadata(statement, fixture_metadata(statement))


async def verify_final(statement, candidate, tmp_path):
    verifier = LeanVerifier(LeanVerificationConfig(
        target_workspace=MATHLIB, scratch_workspace=tmp_path, timeout=120,
    ))
    contract = contract_for(statement)
    return await ComparatorVerifier(
        verifier, statement, task_contract=contract,
        artifact_dir=tmp_path / "comparator_artifacts",
    ).verify(candidate, max_output_chars=40000)


def assert_raw_sources(statement, candidate, result):
    original = contract_for(statement).statement
    assert result["candidate_sha256"] == sha256(candidate)
    assert result["original_source_sha256"] == sha256(original)
    assert result["solution_sha256"] == sha256(candidate)
    artifact = Path(result["comparator"]["artifact_dir"])
    assert (artifact / "Challenge.lean").read_text() == original
    assert (artifact / "candidate.lean").read_text() == candidate
    assert (artifact / "Solution.lean").read_text() == candidate


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_original_objective_acceptance(case, tmp_path, local_runtime):
    result = asyncio.run(verify_final(case["statement"], case["candidate"], tmp_path))
    # These historical fixtures depended on automatic context reconstruction.
    # Missing scopes/definitions cannot compile; original permitted axioms
    # must also remain available for Comparator's export on both sides.
    expected = case["expected_accept"] and case["name"] not in {
        "original_namespace_section_omitted", "original_source_axiom_omitted",
    }
    assert result["success"] is expected, result
    if case.get("expected_error_kind"):
        assert case["expected_error_kind"] in "\n".join(result["errors"]), result
    else:
        assert_raw_sources(case["statement"], case["candidate"], result)
        assert result["command"], result
    if expected:
        assert result["backend"] == BACKEND
        assert contract_for(case["statement"]).target_name in result["comparator"]["specification"]["theorem_names"]


def test_doccomment_changes_do_not_rewrite_submitted_source(tmp_path, local_runtime):
    statement = IMPORT + "/-- Original docs. -/\ndef preserved : Nat := 1\ntheorem target : preserved = 1 := by sorry\n"
    candidate = statement.replace("Original docs.", "Candidate docs.").replace("by sorry", "by rfl")
    result = asyncio.run(verify_final(statement, candidate, tmp_path))
    assert result["success"], result
    assert_raw_sources(statement, candidate, result)


@pytest.mark.parametrize("header,signature", [
    (
        "variable (p q r : Nat)\n"
        "variable [Fact (p ≠ 0)] [Fact (q ≠ 0)] [Fact (r ≠ 0)]\n",
        "theorem target : (p + q) + r = (p + q) + r",
    ),
    (
        "namespace OriginalScope\nuniverse u\n"
        "variable (α : Type u) [Inhabited α]\ninclude α\n",
        "theorem target : True",
    ),
])
def test_original_section_parameters_are_preserved(header, signature, tmp_path, local_runtime):
    footer = "end OriginalScope\n" if "namespace" in header else ""
    statement = IMPORT + "import Mathlib.Basic.Logic.Basic\n" + header + signature + " := by sorry\n" + footer
    proof = "trivial" if signature.endswith(": True") else "rfl"
    candidate = statement.replace("by sorry", "by " + proof)
    result = asyncio.run(verify_final(statement, candidate, tmp_path))
    assert result["success"], result
    assert_raw_sources(statement, candidate, result)


def test_model_specializing_original_free_parameter_is_rejected(tmp_path, local_runtime):
    statement = IMPORT + "theorem target : C = C + (0 : Nat) := by sorry\n"
    candidate = IMPORT + "def C : Nat := 0\ntheorem target : C = C + (0 : Nat) := by rfl\n"
    result = asyncio.run(verify_final(statement, candidate, tmp_path))
    assert not result["success"], result
    assert "theorem statement do not match" in result.get("output", ""), result
    assert_raw_sources(statement, candidate, result)


@pytest.mark.parametrize("helper,target_type", [
    ("theorem sourceNative : (1 : Nat) = 1 := by native_decide", "(1 : Nat) = 1"),
    ("theorem sourceOracle : False := @sorryAx False false", "False"),
])
def test_candidate_beq_instance_cannot_disable_comparator_axiom_checks(
    helper, target_type, tmp_path, local_runtime,
):
    imports = IMPORT + "import Lean.Elab.Tactic.BVDecide\n"
    target = f"theorem target : {target_type} := by "
    statement = imports + helper + "\n" + target + "sorry\n"
    helper_name = helper.split()[1]
    candidate = (
        imports + helper + "\n"
        "instance (priority := 999999) : BEq Lean.Name where\n"
        "  beq _ _ := false\n" + target + f"exact {helper_name}\n"
    )
    result = asyncio.run(verify_final(statement, candidate, tmp_path))
    assert not result["success"], result
    assert "Illegal axiom detected" in result.get("output", ""), result
    assert_raw_sources(statement, candidate, result)


@pytest.mark.parametrize("namespaces", [(), ("N",), ("Outer", "Inner")],
                         ids=["top-level", "namespace", "nested-namespaces"])
def test_inspection_commands_keep_the_submitted_order(namespaces, tmp_path, local_runtime):
    prefix = "".join(f"namespace {name}\n" for name in namespaces)
    suffix = "".join(f"end {name}\n" for name in reversed(namespaces))
    target = "theorem target : True := by "
    statement = IMPORT + prefix + target + "sorry\n" + suffix
    candidate = (IMPORT + prefix + "#check Nat\n#print Nat\n#eval (20 + 22 : Nat)\n"
                 + target + "trivial\n#print target\n#eval (40 + 2 : Nat)\n")
    for index in reversed(range(len(namespaces))):
        relative = ".".join((*namespaces[index:], "target"))
        candidate += f"end {namespaces[index]}\n#print {relative}\n"
    result = asyncio.run(verify_final(statement, candidate, tmp_path))
    assert result["success"], result
    assert_raw_sources(statement, candidate, result)
