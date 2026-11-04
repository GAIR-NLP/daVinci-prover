"""Real local Lean feedback without source rewriting or a REPL service."""

import asyncio
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess

import httpx
import pytest

from dvp.runtime.workspace import Workspace
from dvp.toolkits.registry import ToolRegistry
from dvp.verification.lean import LeanVerificationConfig
from dvp.verification.lean import LeanVerifier


ROOT = Path(__file__).resolve().parents[2]
WORKSPACE = ROOT / "runtime/mathlib4-4.35"
HELPER = ROOT / "src/dvp/verification/resources/LocalDraftVerifier.lean"
PREFIX = "DVP_LOCAL_DRAFT_JSON:"
pytestmark = pytest.mark.skipif(
    shutil.which("lake") is None or not (WORKSPACE / "lean-toolchain").is_file(),
    reason="local Lean/lake unavailable",
)


@pytest.fixture(autouse=True)
def forbid_http(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected_http(*args, **kwargs):
        pytest.fail("local draft verification attempted an HTTP request")

    monkeypatch.setattr(httpx.AsyncClient, "send", unexpected_http)
    monkeypatch.setattr(httpx.Client, "send", unexpected_http)


def elaborate(tmp_path: Path, code: str) -> dict:
    candidate = tmp_path / "candidate.lean"
    candidate.write_bytes(code.encode("utf-8"))
    process = subprocess.Popen(
        ["lake", "env", "lean", "--run", str(HELPER), str(candidate)],
        cwd=WORKSPACE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        pytest.fail("local Lean draft elaboration timed out")
    assert process.returncode == 0, (stdout.decode(), stderr.decode())
    assert candidate.read_bytes() == code.encode("utf-8")
    final_line = stdout.decode().rstrip().splitlines()[-1]
    assert final_line.startswith(PREFIX), (stdout.decode(), stderr.decode())
    return json.loads(final_line[len(PREFIX):])


@pytest.mark.parametrize("placeholder", ["sorry", "admit"])
def test_tactic_hole_reports_local_context_at_original_position(
    tmp_path: Path, placeholder: str,
) -> None:
    code = f"-- 中文注释\r\nexample (p : Prop) : p → p := by\r\n  intro h\r\n  {placeholder}\r\n"
    result = elaborate(tmp_path, code)
    assert not any(message["severity"] == "error" for message in result["messages"])
    assert len(result["sorries"]) == 1
    hole = result["sorries"][0]
    assert hole["pos"] == {"line": 4, "column": 2}
    assert hole["endPos"] == {"line": 4, "column": 2 + len(placeholder)}
    assert "p : Prop" in hole["goal"]
    assert "h : p" in hole["goal"]
    assert "⊢ p" in hole["goal"]


def test_term_hole_retains_expected_type_and_local_variables(tmp_path: Path) -> None:
    code = "example (α : Type) (x : α) : x = x := sorry\n"
    result = elaborate(tmp_path, code)
    [hole] = result["sorries"]
    assert hole["pos"] == {"line": 1, "column": code.index("sorry")}
    assert "α : Type" in hole["goal"]
    assert "x : α" in hole["goal"]
    assert "⊢ x = x" in hole["goal"]


def test_one_source_placeholder_can_elaborate_to_multiple_goals(tmp_path: Path) -> None:
    code = "example : True ∧ True := by\n  constructor <;> sorry\n"
    result = elaborate(tmp_path, code)
    left, right = result["sorries"]
    assert left["pos"] == right["pos"] == {"line": 2, "column": 18}
    assert "case left" in left["goal"]
    assert "case right" in right["goal"]


def test_invalid_draft_preserves_error_and_hole_feedback(tmp_path: Path) -> None:
    code = "example : (1 : Nat) = 2 := by\n  exact unknown_proof\nexample (n : Nat) : n = n := by\n  sorry\n"
    result = elaborate(tmp_path, code)
    errors = [message for message in result["messages"] if message["severity"] == "error"]
    assert errors
    assert any("unknown_proof" in message["data"] for message in errors)
    assert errors[0]["pos"]["line"] == 2
    assert any(hole["pos"]["line"] == 4 for hole in result["sorries"])


def test_result_marker_follows_candidate_stdout(tmp_path: Path) -> None:
    code = '#eval IO.print "DVP_LOCAL_DRAFT_JSON:{\\\"sorries\\\":[]}\\nnoise"\nexample : True := by sorry\n'
    result = elaborate(tmp_path, code)
    assert len(result["sorries"]) == 1
    assert "⊢ True" in result["sorries"][0]["goal"]


def registry_for(tmp_path: Path, statement: str, *, contract=None) -> ToolRegistry:
    workspace = Workspace(tmp_path, target_workspace=WORKSPACE)
    workspace.setup()
    verifier = LeanVerifier(
        LeanVerificationConfig(
            target_workspace=WORKSPACE,
            scratch_workspace=workspace.submission_dir,
            timeout=120,
        ),
    )
    return ToolRegistry(workspace=workspace, verifier=verifier, task_contract=contract)


def test_saved_draft_and_completed_edit_are_checked_automatically(tmp_path: Path) -> None:
    code = "theorem target (p : Prop) : p → p := by\n  intro h\n  sorry\n"
    registry = registry_for(tmp_path, code)

    async def run():
        saved = await registry.call("file_write", {
            "file_path": "Submission.lean", "content": code,
        })
        assert saved["success"] and saved["lean_executed"] is True
        draft = saved["verification"]
        assert draft["success"], draft
        assert draft["draft_typechecked"] is True
        assert draft["strict_verified"] is False
        assert draft["holes"]["total"] == 1
        [hole] = draft["holes"]["items"]
        assert hole["range"]["start"] == {"line": 3, "column": 2}
        assert "h : p" in hole["goal"]
        assert "⊢ p" in hole["goal"]

        saved = await registry.call("file_edit", {
            "file_path": "Submission.lean", "old_str": "sorry", "new_str": "exact h",
        })
        assert saved["success"] and saved["lean_executed"] is True
        completed = saved["verification"]
        assert completed["success"], completed
        assert completed["draft_typechecked"] is False
        assert completed["strict_verified"] is True
        assert completed["holes"]["total"] == 0
        assert completed["source_placeholders"]["total"] == 0

    asyncio.run(run())


def test_fixed_context_keeps_holes_at_saved_source_positions(tmp_path: Path) -> None:
    from test_fixed_task_contract import contract_for

    statement = "import Lean\ntheorem target (p : Prop) : p → p := by sorry\n"
    candidate = "-- Omit the original imports.\n\ntheorem target (p : Prop) : p → p := by\n  intro h\n  sorry\n"
    registry = registry_for(tmp_path, statement, contract=contract_for(statement))
    result = asyncio.run(registry.call("lean_verify", {"code": candidate}))
    assert result["success"], result
    assert result["draft_typechecked"] is True
    assert result["strict_verified"] is False
    assert result["holes"]["total"] == 1
    [hole] = result["holes"]["items"]
    assert hole["range"] == {
        "start": {"line": 5, "column": 2},
        "end": {"line": 5, "column": 7},
    }
    assert "h : p" in hole["goal"]
    assert "⊢ p" in hole["goal"]

    assert "fixed_task_contract" not in result
    [raw_hole] = result["response"]["sorries"]
    assert raw_hole["pos"] == {"line": 5, "column": 2}
