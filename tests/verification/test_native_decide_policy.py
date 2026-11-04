"""Native decision compiles normally; final axiom policy belongs to Comparator."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from dvp.verification.comparator import ComparatorVerifier
from dvp.verification.lean import (
    LeanVerificationConfig,
    LeanVerifier,
)
from test_lean435_local_pipeline import WORKSPACE, local_runtime


@pytest.mark.parametrize(
    "code",
    [
        "example : True := by native_decide",
        "example : True := by decide +native",
        "example : True := by decide /- mode -/ + native",
        "example : True := by decide (config := { native := true })",
        "example : True := by decide (config := { native :=\n true })",
        "#check Lean.ofReduceBool",
        "#check Lean.trustCompiler",
    ],
)
def test_native_source_reaches_ordinary_compiler_without_lexical_rejection(
    code: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    verifier = LeanVerifier(LeanVerificationConfig(
        target_workspace=None,
        scratch_workspace=tmp_path,
        timeout=1,
        allow_sorry=True,
    ))
    compiled = []

    async def compile_source(path, **kwargs):
        compiled.append(path.read_text(encoding="utf-8"))
        return {"success": True, "errors": [], "warnings": []}

    backend = AsyncMock(side_effect=compile_source)
    monkeypatch.setattr(verifier, "_verify_local_file", backend)
    draft = "import Mathlib\n" + code + "\nexample : True := by sorry\n"

    result = asyncio.run(verifier.verify_code(draft))

    assert result["success"]
    assert compiled == [draft]
    backend.assert_awaited_once()


def test_comparator_rejects_transitive_native_dependency(
    tmp_path: Path, local_runtime,
) -> None:
    statement = """import Mathlib.Util.AssertNoSorry
import Std.Tactic.BVDecide
namespace NativePolicyProbe
theorem target : (41 : Nat) = 41 := by sorry
end NativePolicyProbe
"""
    ordinary = """import Mathlib.Util.AssertNoSorry
import Std.Tactic.BVDecide
namespace NativePolicyProbe
theorem target : (41 : Nat) = 41 := by
  bv_decide
end NativePolicyProbe
"""
    via_helper = """import Mathlib.Util.AssertNoSorry
import Std.Tactic.BVDecide
namespace NativePolicyProbe
theorem helper : (41 : Nat) = 41 := by native_decide
theorem target : (41 : Nat) = 41 := helper
end NativePolicyProbe
"""
    verifier = ComparatorVerifier(LeanVerifier(
        LeanVerificationConfig(
            target_workspace=WORKSPACE,
            scratch_workspace=tmp_path,
            timeout=120,
            allow_sorry=False,
        )
    ), statement)

    good = asyncio.run(verifier.verify(ordinary))
    assert good["success"], good["output"]

    bad = asyncio.run(verifier.verify(via_helper))
    assert not bad["success"], bad["output"]
    diagnostics = "\n".join([
        str(bad.get("output") or ""),
        *(str(item) for item in bad.get("errors") or []),
    ])
    assert "axiom" in diagnostics.lower()
