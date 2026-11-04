"""Comparator preserves original definitions, namespaces, and assumptions."""

import asyncio
import hashlib
import shutil
from pathlib import Path

import pytest

from dvp.verification.comparator import ComparatorVerifier, runtime_for
from dvp.verification.lean import LeanVerificationConfig, LeanVerifier


REPO_ROOT = Path(__file__).resolve().parents[2]
MATHLIB = REPO_ROOT / "runtime/mathlib4-4.35"
HAS_LOCAL_LEAN = shutil.which("lake") is not None and any(
    (MATHLIB / name).exists()
    for name in ("lakefile.toml", "lakefile.lean")
)


@pytest.fixture
def local_verifier(tmp_path: Path) -> LeanVerifier:
    if not HAS_LOCAL_LEAN:
        pytest.skip("local mathlib/lake unavailable")
    try:
        runtime_for(MATHLIB)
    except (OSError, RuntimeError) as error:
        pytest.skip(f"Lean 4.35 comparator runtime unavailable: {error}")
    return LeanVerifier(LeanVerificationConfig(
        target_workspace=MATHLIB,
        scratch_workspace=tmp_path,
        timeout=120,
        allow_sorry=False,
    ))


def _check_contract(
    verifier: LeanVerifier,
    statement: str,
    candidate: str,
    *,
    accepted: bool,
) -> dict[str, object]:
    # Rejected comparisons must exercise a candidate that independently typechecks;
    # rejecting malformed Lean would not test the completion trust boundary.
    ordinary = asyncio.run(verifier.verify_code(candidate))
    assert ordinary["success"], ordinary
    checked = asyncio.run(ComparatorVerifier(verifier, statement).verify(candidate))
    assert checked["success"] is accepted, checked
    return checked


def _odd_function_case() -> tuple[str, str]:
    statement = (Path(__file__).resolve().parents[1] / "fixtures/odd_function_preamble.lean").read_text(encoding="utf-8")
    candidate = statement.replace(
        "  sorry",
        "  intro x\n"
        "  change φ (φ (-x)) = -φ (φ x)\n"
        "  rw [h x]\n"
        "  rw [h (φ x)]",
    )
    assert candidate != statement
    return statement, candidate


def test_real_proverbench_odd_function_proof(
    local_verifier: LeanVerifier,
) -> None:
    statement, candidate = _odd_function_case()
    _check_contract(local_verifier, statement, candidate, accepted=True)


def test_shared_preamble_preserves_structure_identity(
    local_verifier: LeanVerifier,
) -> None:
    statement = """import Mathlib.Util.AssertNoSorry
structure PreambleBox where
  value : Nat
def boxValue (b : PreambleBox) : Nat := b.value
theorem target (b : PreambleBox) : boxValue b = b.value := by sorry
"""
    candidate = statement.replace("by sorry", "by rfl")
    _check_contract(local_verifier, statement, candidate, accepted=True)


def test_shared_namespace_section_accepts_added_helper(
    local_verifier: LeanVerifier,
) -> None:
    statement = """import Mathlib.Util.AssertNoSorry
namespace PreambleScope
section
variable (n : Nat)
def twice (m : Nat) := m + m
theorem target : twice n = n + n := by sorry
end
end PreambleScope
"""
    candidate = statement.replace(
        "theorem target",
        "theorem helper (m : Nat) : twice m = m + m := rfl\n"
        "theorem target",
    ).replace("by sorry", "by exact helper n")
    _check_contract(local_verifier, statement, candidate, accepted=True)


def test_comments_in_shared_preamble_do_not_duplicate_definitions(
    local_verifier: LeanVerifier,
) -> None:
    statement = """import Mathlib.Util.AssertNoSorry
/- Original task description. -/
def preservedValue : Nat := 7
theorem target : preservedValue = 7 := by sorry
"""
    candidate = statement.replace(
        "/- Original task description. -/",
        "/- Reworded /- nested -/ explanation. -/",
    ).replace("by sorry", "by rfl")
    _check_contract(local_verifier, statement, candidate, accepted=True)


def test_changed_preamble_predicate_is_rejected(
    local_verifier: LeanVerifier,
) -> None:
    statement = """import Mathlib.Util.AssertNoSorry
def PreamblePredicate (n : Nat) : Prop := False
theorem target (n : Nat) : PreamblePredicate n := by sorry
"""
    candidate = statement.replace(":= False", ":= True").replace(
        "by sorry", "by trivial"
    )
    _check_contract(local_verifier, statement, candidate, accepted=False)


def test_added_ambient_false_assumption_is_rejected(
    local_verifier: LeanVerifier,
) -> None:
    statement = """import Mathlib.Util.AssertNoSorry
def preservedValue : Nat := 0
theorem target : False := by sorry
"""
    candidate = statement.replace(
        "theorem target",
        "variable (extra : False)\ninclude extra\ntheorem target",
    ).replace("by sorry", "by exact extra")
    _check_contract(local_verifier, statement, candidate, accepted=False)


@pytest.mark.parametrize("original_value", ["different", "a b"])
def test_changed_string_literal_is_rejected(
    local_verifier: LeanVerifier,
    original_value: str,
) -> None:
    statement = (
        "import Mathlib.Util.AssertNoSorry\n"
        f'def preambleLabel : String := "{original_value}"\n'
        'theorem target : preambleLabel = "ab" := by sorry\n'
    )
    candidate = statement.replace(
        f':= "{original_value}"', ':= "ab"'
    ).replace("by sorry", "by rfl")
    _check_contract(local_verifier, statement, candidate, accepted=False)


def test_comparator_uses_exact_root_target_name(
    local_verifier: LeanVerifier,
) -> None:
    statement = """import Mathlib.LinearAlgebra.Matrix.Determinant.Basic
open Matrix
theorem det_mul : (1 : Nat) = 1 := by sorry
"""
    candidate = statement.replace("by sorry", "by rfl")
    _check_contract(local_verifier, statement, candidate, accepted=True)


def test_changed_do_block_indentation_is_rejected(
    local_verifier: LeanVerifier,
) -> None:
    statement = """import Mathlib.Util.AssertNoSorry
def indentationPredicate : Prop := Id.run do
  if false then
    if true then
      return False
    else
      return True
  return False
theorem target : indentationPredicate := by sorry
"""
    candidate = statement.replace(
        "    else\n      return True", "  else\n    return True"
    ).replace("by sorry", "by exact True.intro")
    # Moving `else` changes the predicate from False to True with the same
    # non-whitespace tokens. A compiling proof of the changed definition
    # must still fail comparison against the original module.
    _check_contract(local_verifier, statement, candidate, accepted=False)


def test_comparator_does_not_inject_an_internal_expected_axiom(
    local_verifier: LeanVerifier,
) -> None:
    statement = """import Mathlib.Util.AssertNoSorry
theorem target : True := by sorry
"""
    digest = hashlib.sha256(statement.encode("utf-8")).hexdigest()[:16]
    candidate = statement.replace(
        "by sorry",
        "by\n"
        "  first\n"
        f"  | exact _expected_target_{digest}\n"
        "  | exact True.intro",
    )
    # In separate Challenge/Solution modules this internal name does not exist;
    # the proof reaches its valid True.intro branch instead.
    checked = _check_contract(local_verifier, statement, candidate, accepted=True)
    artifact = Path(checked["comparator"]["artifact_dir"])
    assert (artifact / "Challenge.lean").read_text() == statement
    assert (artifact / "Solution.lean").read_text() == candidate
