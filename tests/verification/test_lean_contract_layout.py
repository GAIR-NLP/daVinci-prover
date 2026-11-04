"""Comparator checks original Lean type expressions with their exact layout."""

import asyncio
import shutil
from pathlib import Path

import pytest

from dvp.verification.comparator import ComparatorVerifier, runtime_for
from dvp.verification.lean import LeanVerificationConfig, LeanVerifier


MATHLIB = Path(__file__).resolve().parents[2] / "runtime/mathlib4-4.35"


@pytest.fixture
def local_verifier(tmp_path: Path) -> LeanVerifier:
    if shutil.which("lake") is None or not any((MATHLIB / name).exists() for name in ("lakefile.lean", "lakefile.toml")):
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


@pytest.mark.parametrize(
    "declaration",
    [
        # Same destructuring-let layout as Putnam 1995 B4, with a small proof.
        """theorem target :
    let ⟨a, b, c, d⟩ := ((⟨3, 1, 5, 2⟩) : Nat × Nat × Nat × Nat)
    a + b + c + d = 11 := by decide
""",
        """theorem target (n : Nat) :
    let pair := (n, n)
    pair.1 = pair.2 := by rfl
""",
        """theorem target :
    let a := 3
    let b :=
      match a with
      | 0 => 1
      | _ => 2
    a + b = 5 := by decide
""",
        """theorem target : let n := 1; n = 1 := by rfl
""",
        """theorem target : let n := 1
                 n = 1 := by rfl
""",
        """theorem target :
    (Id.run do
      let n := 1
      return n + 1 : Nat) = 2 := by rfl
""",
    ],
    ids=["putnam_destructuring", "bound_variable", "nested_match", "inline_let", "inline_layout", "do_block"],
)
def test_comparator_preserves_type_layout(
    local_verifier: LeanVerifier,
    declaration: str,
) -> None:
    candidate = "import Mathlib.Util.AssertNoSorry\n" + declaration
    header, _ = candidate.rsplit(" := by", 1)
    statement = header + " := by sorry\n"
    standalone = asyncio.run(local_verifier.verify_code(candidate))
    assert standalone["success"], standalone["output"]

    checked = asyncio.run(ComparatorVerifier(local_verifier, statement).verify(candidate))
    assert checked["success"], checked["output"]


def test_changed_type_expression_indentation_is_rejected(
    local_verifier: LeanVerifier,
) -> None:
    statement = """import Mathlib.Util.AssertNoSorry
theorem target :
    (Id.run do
      if false then
        if true then
          return False
        else
          return True
      return False : Prop) := by sorry
"""
    candidate = statement.replace(
        "        else\n          return True",
        "      else\n        return True",
    ).replace("by sorry", "by change True; trivial")
    standalone = asyncio.run(local_verifier.verify_code(candidate))
    assert standalone["success"], standalone["output"]

    checked = asyncio.run(ComparatorVerifier(local_verifier, statement).verify(candidate))
    assert not checked["success"], checked
