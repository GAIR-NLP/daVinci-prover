"""Exercise default verification with real local Lean and no HTTP transport."""

import asyncio
from dataclasses import replace
from pathlib import Path
import shutil
from typing import Any

import httpx
import pytest

from dvp.verification.comparator import ComparatorVerifier, runtime_for
from dvp.verification.lean import LeanVerificationConfig, LeanVerifier


MATHLIB = Path(__file__).resolve().parents[2] / "runtime/mathlib4-4.35"
IMPORTS = "import Mathlib.Util.AssertNoSorry\n"


@pytest.fixture
def local_verifier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> LeanVerifier:
    if shutil.which("lake") is None or not any(
        (MATHLIB / name).exists() for name in ("lakefile.lean", "lakefile.toml")
    ):
        pytest.skip("local Lean/Mathlib unavailable")
    try:
        runtime_for(MATHLIB)
    except (OSError, RuntimeError) as error:
        pytest.skip(f"Lean 4.35 comparator runtime unavailable: {error}")

    # Stale service configuration must never switch verification away from Lean.
    monkeypatch.setenv("DAVINCI_LEAN_API_URL", "https://must-not-connect.invalid")
    monkeypatch.setenv("DAVINCI_LEAN_API_CONCURRENCY", "invalid")
    monkeypatch.setenv("DAVINCI_LEAN_API_QUEUE_TIMEOUT", "invalid")

    def unexpected_http(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("local Lean verification attempted an HTTP request")

    monkeypatch.setattr(httpx.AsyncClient, "send", unexpected_http)
    monkeypatch.setattr(httpx.Client, "send", unexpected_http)
    config = LeanVerificationConfig(
        target_workspace=MATHLIB,
        scratch_workspace=tmp_path,
        timeout=120,
    )
    return LeanVerifier(config)


def test_default_verifier_accepts_proof_locally(
    local_verifier: LeanVerifier,
) -> None:
    result = asyncio.run(local_verifier.verify_code(
        IMPORTS + "theorem target (n : Nat) : n = n := by rfl\n"
    ))

    assert result["success"], result
    assert result["errors"] == []
    assert result["command"][:3] == ["lake", "env", "lean"]


@pytest.mark.parametrize("probe, expected", [
    ("#eval (20 + 22 : Nat)", "42"),
    ("#print target", "theorem target"),
])
def test_local_compilation_executes_inspection_commands(local_verifier, probe, expected):
    code = IMPORTS + "theorem target : True := by trivial\n" + probe + "\n"
    result = asyncio.run(local_verifier.verify_code(code))
    assert result["success"], result
    assert result["errors"] == []
    assert expected in result["output"]
    assert result["command"][:3] == ["lake", "env", "lean"]


def test_local_draft_inspection_commands_preserve_placeholder_feedback(local_verifier):
    code = (IMPORTS + "#eval (20 + 22 : Nat)\n#print Nat\n"
            "theorem target : True := by sorry\n")
    checker = LeanVerifier(replace(local_verifier.config, allow_sorry=True))
    result = asyncio.run(checker.verify_code(code))
    assert result["success"], result
    assert result["warnings"], result
    assert "42" in result["output"]
    assert "Nat" in result["output"]
    assert len(result["response"]["sorries"]) == 1
    assert "⊢ True" in result["response"]["sorries"][0]["goal"]

    ordinary = asyncio.run(local_verifier.verify_code(code))
    assert ordinary["success"], ordinary
    assert "sorry" in "\n".join(ordinary["warnings"])


def test_local_verifier_reports_lean_errors(
    local_verifier: LeanVerifier,
) -> None:
    result = asyncio.run(local_verifier.verify_code(
        IMPORTS + "theorem target : (1 : Nat) = 2 := by rfl\n"
    ))

    assert not result["success"], result
    assert result["errors"], result
    assert result["returncode"] != 0
    assert result["command"][:3] == ["lake", "env", "lean"]


@pytest.mark.parametrize("placeholder", ["sorry", "admit"])
def test_local_compilation_preserves_holes_for_final_comparator_rejection(
    local_verifier: LeanVerifier,
    placeholder: str,
) -> None:
    code = IMPORTS + f"theorem target (n : Nat) : n = n := by {placeholder}\n"
    ordinary = asyncio.run(local_verifier.verify_code(code))
    assert ordinary["success"], ordinary
    assert ordinary["warnings"], ordinary

    # ToolRegistry uses the base verifier for non-fixed-context drafts.
    draft_verifier = LeanVerifier(replace(local_verifier.config, allow_sorry=True))
    draft = asyncio.run(draft_verifier.verify_code(code))
    assert draft["success"], draft
    assert draft["warnings"], draft
    assert draft["command"][:3] == ["lake", "env", "lean"]

    checked = asyncio.run(ComparatorVerifier(local_verifier, code).verify(code))
    assert not checked["success"], checked


def test_local_compilation_accepts_library_imports_and_ordinary_declarations(local_verifier):
    code = ("import Std\nimport Lean\n"
            "opaque value : Nat := 42\n"
            'notation "answer" => value\n'
            "unsafe def experiment : Nat := 7\n"
            "theorem target : answer = value := by rfl\n")
    result = asyncio.run(local_verifier.verify_code(code))
    assert result["success"], result
    assert result["errors"] == []


def test_extra_axiom_compiles_but_cannot_prove_the_original_task(local_verifier):
    statement = IMPORTS + "theorem target : False := by sorry\n"
    candidate = IMPORTS + "axiom assumption : False\ntheorem target : False := assumption\n"
    ordinary = asyncio.run(local_verifier.verify_code(candidate))
    assert ordinary["success"], ordinary
    checked = asyncio.run(ComparatorVerifier(local_verifier, statement).verify(candidate))
    assert not checked["success"], checked
    assert checked["backend"] == "leanprover/comparator"


@pytest.mark.parametrize("added_assumption", [False, True])
def test_local_completion_checks_the_original_elaborated_target_type(
    local_verifier: LeanVerifier,
    added_assumption: bool,
) -> None:
    statement = IMPORTS + "theorem target (n : Nat) : n = n := by sorry\n"
    if added_assumption:
        candidate = statement.replace(
            "theorem target",
            "variable (extra : False)\ninclude extra\ntheorem target",
        ).replace("by sorry", "by exact False.elim extra")
    else:
        candidate = statement.replace("by sorry", "by rfl")

    # Both candidates compile; only one proves the original target type.
    ordinary = asyncio.run(local_verifier.verify_code(candidate))
    assert ordinary["success"], ordinary

    checked = asyncio.run(ComparatorVerifier(local_verifier, statement).verify(candidate))
    assert checked["success"] is (not added_assumption), checked
    assert checked["backend"] == "leanprover/comparator"
    if added_assumption:
        assert checked["errors"], checked
