"""Strict local verification preserves every diagnostic's full context."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dvp.verification.lean import LeanVerificationConfig, LeanVerifier
from dvp.verification.diagnostics import primary_verifier_diagnostic


def verify_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stdout: str,
    *,
    stderr: str = "",
    returncode: int = 1,
    max_output_chars: int = 12000,
) -> dict:
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
    # These tests replay compiler output through the real diagnostic parser;
    # neither process creation nor sandbox setup should consult an installation.
    monkeypatch.setattr(
        LeanVerifier, "_sandbox_command", lambda self, command, **kwargs: command,
    )
    checker = LeanVerifier(LeanVerificationConfig(
        target_workspace=None, scratch_workspace=tmp_path,
    ))
    return asyncio.run(checker.verify_code(
        "theorem target : True := by trivial\n",
        max_output_chars=max_output_chars,
    ))


def test_every_error_retains_its_own_context_and_goal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = (
        "candidate.lean:4:2: error: unsolved goals\n"
        "case first\n"
        "n : Nat\n"
        "h : n > 0\n"
        "⊢ n + 0 = n"
    )
    second = (
        "candidate.lean:9:4: error: Tactic `exact` failed\n"
        "case second\n"
        "m : Nat\n"
        "hm : m = 1\n"
        "⊢ m > 0"
    )
    result = verify_output(
        tmp_path, monkeypatch, first + "\n\n" + second + "\n",
        max_output_chars=40,
    )

    assert result["errors"] == [first, second]
    assert result["warnings"] == []
    assert not result["success"]
    assert "truncated" in result["output"]
    assert "⊢ m > 0" not in result["output"]


def test_warning_and_information_messages_do_not_contaminate_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = "candidate.lean:2:0: error: unsolved goals\nα : Type\n⊢ Nonempty α"
    warning = (
        "candidate.lean:3:0: warning: unused variable `x`\n"
        "Note: This linter can be disabled."
    )
    coded_error = (
        "candidate.lean:6:2: error(lean.unknownIdentifier): Unknown identifier `foo`\n"
        "Hint: Check the namespace."
    )
    output = "\n".join([
        error,
        "candidate.lean:2:4: information: first informational message",
        "An informational continuation mentioning error: is still information.",
        warning,
        "candidate.lean:4:0: info: second informational message",
        "Another informational continuation.",
        coded_error,
        "candidate.lean:7:0: Nat : Type",
        "A position-only informational continuation.",
    ])
    result = verify_output(tmp_path, monkeypatch, output)

    assert result["errors"] == [error, coded_error]
    assert result["warnings"] == [warning]
    assert result["output"] == output


def test_unpositioned_stderr_warning_does_not_extend_stdout_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = "candidate.lean:2:0: error: unsolved goals\nn : Nat\n⊢ n = 0"
    warning = "warning: package has local changes\nCheck the working tree."
    result = verify_output(tmp_path, monkeypatch, error, stderr=warning)

    assert result["errors"] == [error]
    assert result["warnings"] == [warning]


def test_nonstructured_process_failure_keeps_the_full_output_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = "failed to load shared library\n  missing libLean.so\n"
    result = verify_output(tmp_path, monkeypatch, "", stderr=output, returncode=2)

    assert not result["success"]
    assert result["returncode"] == 2
    assert result["errors"] == [output.strip()]
    assert result["warnings"] == []


@pytest.mark.parametrize("returncode", [0, 1])
def test_empty_output_keeps_returncode_based_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, returncode: int,
) -> None:
    result = verify_output(tmp_path, monkeypatch, "", returncode=returncode)

    assert result["success"] is (returncode == 0)
    assert result["returncode"] == returncode
    assert result["errors"] == []
    assert result["warnings"] == []
    assert result["output"] == ""


def test_ordinary_warning_keeps_successful_returncode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    warning = "candidate.lean:1:0: warning: unused variable\nDetails of the warning."
    result = verify_output(tmp_path, monkeypatch, warning, returncode=0)

    assert result["success"]
    assert result["errors"] == []
    assert result["warnings"] == [warning]


@pytest.mark.parametrize("local_errors", [False, True])
def test_archived_api_wrapper_cannot_override_local_diagnostics(local_errors):
    result = {
        "errors": ["candidate.lean:2:0: error: unsolved goals\nn : Nat\n⊢ n = 0"]
        if local_errors else [],
        "api_response": {"response": {"messages": [{
            "severity": "error", "data": "Unknown identifier from archived service",
        }]}},
    }

    primary, count, _ = primary_verifier_diagnostic(result, None)

    assert count == int(local_errors)
    if local_errors:
        assert primary["kind"] == "unsolved_goals"
        assert primary["primary_goals"] == ["n = 0"]
    else:
        assert primary is None
