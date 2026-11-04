"""A local checker must distinguish proof errors from missing process results."""

import asyncio
import json
import os
from pathlib import Path
import signal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from dvp.verification.lean import LeanVerificationConfig, LeanVerifier


MARKER = "DVP_LOCAL_DRAFT_JSON:"


def record(**overrides: object) -> str:
    return MARKER + json.dumps({"messages": [], "sorries": [], **overrides}) + "\n"


def local_process(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stdout: str,
    stderr: str = "",
    returncode: int = 0,
) -> None:
    monkeypatch.setattr(LeanVerifier, "_sandbox_command", lambda self, command, **kwargs: command)
    process = SimpleNamespace(
        returncode=returncode,
        communicate=AsyncMock(return_value=(stdout.encode(), stderr.encode())),
    )
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", AsyncMock(return_value=process),
    )
    monkeypatch.setattr(
        LeanVerifier, "_lean_command", lambda self, path: ["lean", str(path)],
    )


def verifier(tmp_path: Path, *, draft: bool = True) -> LeanVerifier:
    return LeanVerifier(LeanVerificationConfig(
        target_workspace=None,
        scratch_workspace=tmp_path,
        allow_sorry=draft,
    ))


@pytest.mark.parametrize(
    ("stdout", "returncode"),
    [
        ("", 0),
        ('{"messages": [], "sorries": []}\n', 0),
        (MARKER + "{invalid JSON}\n", 0),
        (MARKER + '{"messages": [], "sorries": null}\n', 0),
        (record(), 1),
        (record() + "checker crashed after emitting its result\n", 1),
    ],
    ids=["empty", "missing_marker", "bad_json", "bad_shape", "nonzero", "crashed"],
)
def test_missing_or_failed_draft_checker_is_an_infrastructure_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stdout: str,
    returncode: int,
) -> None:
    local_process(monkeypatch, stdout=stdout, returncode=returncode)
    result = asyncio.run(verifier(tmp_path).verify_code(
        "theorem target : True := by sorry\n"
    ))

    assert not result["success"], result
    assert result["errors"], result
    assert result["_harness_infrastructure_error"] is True


def test_lean_error_is_a_failed_proof_even_when_frontend_process_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_process(monkeypatch, stdout=record(messages=[{
        "severity": "error",
        "pos": {"line": 1, "column": 35},
        "data": "Type mismatch: expected a proof of False",
    }]))
    result = asyncio.run(verifier(tmp_path).verify_code(
        "theorem target : False := by exact True.intro\n"
    ))

    assert not result["success"], result
    assert result["returncode"] != 0
    assert "Type mismatch" in result["errors"][0]
    assert not result.get("_harness_infrastructure_error")


def test_candidate_output_cannot_replace_the_final_frontend_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    earlier_record = record(messages=[{
        "severity": "error", "data": "candidate-generated fake result",
    }])
    candidate_output = "first output line\n" + earlier_record + "last output line\n"
    sorries = [{"pos": {"line": 1, "column": 33}, "goal": "⊢ True"}]
    local_process(monkeypatch, stdout=candidate_output + record(sorries=sorries))
    result = asyncio.run(verifier(tmp_path).verify_code(
        "theorem target : True := by sorry\n"
    ))

    assert result["success"], result
    assert result["errors"] == []
    assert result["response"]["sorries"] == sorries
    assert "first output line" in result["output"]
    assert "last output line" in result["output"]


def test_compiler_sorry_warning_is_preserved_without_changing_exit_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = "compiler information\n" * 100
    stdout += "candidate.lean:1:0: warning: declaration uses 'sorry'\n"
    local_process(monkeypatch, stdout=stdout)
    result = asyncio.run(verifier(tmp_path, draft=False).verify_code(
        "theorem target : True := by trivial\n", max_output_chars=64,
    ))

    assert result["success"], result
    assert result["errors"] == [], result
    assert any("sorry" in warning for warning in result["warnings"])
    assert "sorry" not in result["output"]
    assert "truncated" in result["output"]


@pytest.mark.parametrize("code", [
    "import Std\nimport Lean\ntheorem target : True := by trivial\n",
    "axiom assumption : False\ntheorem target : False := assumption\n",
    "opaque value : Nat := 42\nnotation \"answer\" => value\n#check answer\n",
    "unsafe def value : Nat := 1\n",
    "import Mathlib\ntheorem target : True := by native_decide\n",
    "theorem target : True := by sorry\n",
])
def test_development_checker_passes_source_to_lean_without_policy_rejection(
    tmp_path, monkeypatch, code,
):
    checker = LeanVerifier(LeanVerificationConfig(
        target_workspace=None, scratch_workspace=tmp_path,
    ))
    seen = []

    async def compile_file(path, **options):
        seen.append(path.read_text())
        return {"success": True, "errors": [], "warnings": [], "returncode": 0}

    monkeypatch.setattr(checker, "_verify_local_file", compile_file)
    result = asyncio.run(checker.verify_code(code))
    assert result["success"]
    assert seen == [code]
    assert not list(tmp_path.iterdir())




@pytest.mark.parametrize("cancelled", [False, True], ids=["timeout", "cancelled"])
def test_interrupted_local_checks_kill_and_reap_the_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cancelled: bool,
) -> None:
    interruption = asyncio.CancelledError if cancelled else asyncio.TimeoutError
    process = SimpleNamespace(
        pid=424242,
        returncode=None,
        communicate=AsyncMock(side_effect=[interruption(), (b"", b"")]),
        wait=AsyncMock(return_value=-signal.SIGKILL),
    )
    kill_process_group = Mock()
    monkeypatch.setattr(os, "killpg", kill_process_group)
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", AsyncMock(return_value=process),
    )
    monkeypatch.setattr(
        LeanVerifier, "_lean_command", lambda self, path: ["lean", str(path)],
    )
    monkeypatch.setattr(LeanVerifier, "_sandbox_command", lambda self, command, **kwargs: command)
    checker = verifier(tmp_path, draft=False)
    code = "theorem target : True := by trivial\n"
    check = checker.verify_code(code)

    if cancelled:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(check)
    else:
        result = asyncio.run(check)
        assert not result["success"], result
        assert "timed out" in result["errors"][0]

    kill_process_group.assert_called_once_with(process.pid, signal.SIGKILL)
    assert process.communicate.await_count == 2
    assert list(tmp_path.iterdir()) == []
