"""Local verifier diagnostics survive the bounded model-feedback boundary."""

import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dvp.agent.result_projection import _compact
from dvp.verification.diagnostics import primary_verifier_diagnostic
from dvp.verification.lean import LeanVerificationConfig, LeanVerifier


# Captured from the local Lean compiler with four independent failing examples;
# only the stdin filename is replaced with the normal candidate filename.
FOUR_ERRORS = [
    "candidate.lean:1:29: error: unsolved goals\nn : Nat\n⊢ n = 0",
    "candidate.lean:4:2: error: Type mismatch\n  h\nhas type\n  a = b\n"
    "but is expected to have type\n  b = a",
    "candidate.lean:6:8: error(lean.unknownIdentifier): Unknown identifier `missingLemma`",
    "candidate.lean:7:36: error: unsolved goals\nP Q : Prop\nh : P\n⊢ Q",
]


def serialized(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def test_known_diagnostic_roots_shorten_without_rewriting_source_or_unknown_paths():
    workspace = "/machine/runs/task/attempt/workspaces"
    target = "/opt/lean/mathlib"
    quoted = f'"{workspace}/Submission/A.lean:9:3: error: literal source text"'
    errors = [
        f"{workspace}/Submission/A.lean:9:3: error: unsolved goals\n⊢ {quoted} = {quoted}",
        f"{workspace}/Submission/B.lean:9:3: error: independent error",
        f"{target}/Mathlib/Test.lean:4:2: warning: imported source",
        f"{workspace}-other/Submission/A.lean:9:3: error: unrelated path",
    ]
    payload = {
        "success": False, "errors": errors, "output": "\n".join(errors) + "\n" + quoted,
        "source_code": quoted,
        "holes": {"total": 1, "items": [{"file": workspace + "/Submission/A.lean",
                                          "line": 10, "goal": quoted}]},
    }
    before = deepcopy(payload)
    result = _compact(payload, tool_name="lean_verify", path_aliases={workspace: "", target: "target/"})

    assert result["errors"] == [
        errors[0].replace(workspace + "/", "", 1),
        errors[1].replace(workspace + "/", "", 1),
        errors[2].replace(target + "/", "target/", 1),
        errors[3],
    ]
    assert result["source_code"] == quoted
    assert result["output"] == quoted
    assert result["holes"]["items"] == [{"file": "Submission/A.lean", "line": 10, "goal": quoted}]
    assert payload == before


def test_structured_diagnostics_keep_data_and_primary_goal_verbatim():
    root = "/shared/long/run/workspaces"
    body = f'⊢ "{root}/Submission/A.lean" = "literal"'
    payload = {
        "success": False,
        "errors": [{"file": root + "/Submission/A.lean", "severity": "error",
                    "pos": {"line": 8, "column": 9}, "data": body}],
        "primary_diagnostic": {"file": root + "/Submission/A.lean", "line": 8,
                               "column": 9, "primary_goal": body},
        "verification": {"success": False, "file_path": root + "/Submission/B.lean",
                         "errors": [root + "/Submission/B.lean:7:1: error: auxiliary"]},
    }
    result = _compact(payload, tool_name="lean_verify", path_aliases={root: ""})
    assert result["errors"][0] == {**payload["errors"][0], "file": "Submission/A.lean"}
    assert result["primary_diagnostic"] == {**payload["primary_diagnostic"], "file": "Submission/A.lean"}
    assert result["verification"]["file_path"] == "Submission/B.lean"
    assert result["verification"]["errors"] == ["Submission/B.lean:7:1: error: auxiliary"]


@pytest.mark.parametrize("tool_name,field", [("file_read", "content"), ("lean_retrieve", "signature")])
def test_diagnostic_path_aliases_do_not_change_read_or_retrieved_source(tool_name, field):
    source = '/machine/workspaces/Submission/A.lean:1:1: error: this is source text'
    item = {field: source}
    payload = {"success": True, **item} if tool_name == "file_read" else {"success": True, "results": [item]}
    result = _compact(payload, tool_name=tool_name, path_aliases={"/machine/workspaces": ""})
    actual = result if tool_name == "file_read" else result["results"][0]
    assert actual[field] == source


def local_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stdout: str,
    *,
    draft: bool = False,
) -> dict:
    process = SimpleNamespace(
        returncode=0 if draft else 1,
        communicate=AsyncMock(return_value=(stdout.encode(), b"")),
    )
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", AsyncMock(return_value=process),
    )
    monkeypatch.setattr(
        LeanVerifier, "_lean_command", lambda self, path: ["lean", str(path)],
    )
    # Replay captured compiler output without consulting a local runtime. The
    # subprocess is mocked above; real sandbox behavior has separate coverage.
    monkeypatch.setattr(
        LeanVerifier, "_sandbox_command", lambda self, command, **kwargs: command,
    )
    checker = LeanVerifier(LeanVerificationConfig(
        target_workspace=None, scratch_workspace=tmp_path, allow_sorry=draft,
    ))
    proof = "sorry" if draft else "trivial"
    return asyncio.run(checker.verify_code(
        f"theorem target : True := by {proof}\n", max_output_chars=200000,
    ))


def feedback_payload(verification: dict, *, nested: bool) -> dict:
    payload = (
        {"success": True, "file_path": "Submission.lean", "verification": verification}
        if nested else dict(verification)
    )
    primary, count, _ = primary_verifier_diagnostic(payload, verification)
    payload.update(primary_diagnostic=primary, diagnostic_count=count)
    return payload


def project(payload: dict, *, budget: int, nested: bool) -> dict:
    return _compact(
        payload, max_chars=budget, tool_name="file_write" if nested else "lean_verify",
    )


@pytest.mark.parametrize("nested", [False, True], ids=["lean_verify", "file_write"])
def test_four_local_errors_and_the_primary_recommendation_all_remain_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nested: bool,
) -> None:
    verification = local_result(tmp_path, monkeypatch, "\n".join(FOUR_ERRORS) + "\n")
    payload = feedback_payload(verification, nested=nested)
    result = project(payload, budget=12000, nested=nested)
    evidence = result["verification"] if nested else result

    assert evidence["errors"] == FOUR_ERRORS
    assert evidence["success"] is False
    assert result["diagnostic_count"] == 4
    assert result["primary_diagnostic"] == {
        key: value for key, value in payload["primary_diagnostic"].items()
        if key != "message"
    }
    assert result["primary_diagnostic"]["primary_goal"] == "n = 0"
    assert "P Q : Prop\nh : P\n⊢ Q" in evidence["errors"][3]
    assert "command" not in evidence
    assert len(serialized(result)) <= 12000


@pytest.mark.parametrize("nested", [False, True], ids=["lean_verify", "file_write"])
def test_more_than_six_errors_are_preserved_when_the_budget_allows_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nested: bool,
) -> None:
    errors = [
        f"candidate.lean:{line}:2: error: unsolved goals\nn{line} : Nat\n⊢ n{line} = 0"
        for line in range(1, 10)
    ]
    verification = local_result(tmp_path, monkeypatch, "\n".join(errors))
    result = project(feedback_payload(verification, nested=nested), budget=12000, nested=nested)
    evidence = result["verification"] if nested else result

    assert evidence["errors"] == errors
    assert result["diagnostic_count"] == 9
    assert len(serialized(result)) <= 12000


@pytest.mark.parametrize("nested", [False, True], ids=["lean_verify", "file_write"])
def test_independent_diagnostics_take_priority_over_large_console_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nested: bool,
) -> None:
    stdout = "compiler preamble\n" * 5000 + "\n".join(FOUR_ERRORS)
    verification = local_result(tmp_path, monkeypatch, stdout)
    payload = feedback_payload(verification, nested=nested)
    assert len(serialized(payload)) > 4000
    result = project(payload, budget=4000, nested=nested)
    evidence = result["verification"] if nested else result

    assert evidence["errors"] == FOUR_ERRORS
    assert result["primary_diagnostic"] == {
        key: value for key, value in payload["primary_diagnostic"].items()
        if key != "message"
    }
    assert evidence["success"] is False
    assert result["result_truncated"] is True
    assert len(serialized(result)) <= 4000


@pytest.mark.parametrize("nested", [False, True], ids=["lean_verify", "file_write"])
@pytest.mark.parametrize("success", [False, True], ids=["process_failure", "check_output"])
def test_console_only_evidence_remains_visible_when_it_exceeds_the_budget(
    nested: bool, success: bool,
) -> None:
    verification = {"success": success, "errors": [], "output": "Nat : Type\n" * 4000}
    payload = {"success": True, "verification": verification} if nested else verification
    result = project(payload, budget=512, nested=nested)
    evidence = result["verification"] if nested else result

    assert evidence["success"] is success
    assert "Nat : Type" in evidence["output"]
    assert evidence["output_truncated"] is True
    assert result["result_truncated"] is True
    assert len(serialized(result)) <= 512


@pytest.mark.parametrize("key", ["error", "message", "goal", "goals"])
@pytest.mark.parametrize("budget", [512, 4000])
def test_nested_standalone_diagnostic_is_not_lost_when_oversized(key: str, budget: int) -> None:
    text = "Lean failed: α : Type\n⊢ 目标\\\"\n" * 1000
    value = [text] if key == "goals" else text
    payload = {"success": False, "verification": {"success": False, key: value}}
    result = project(payload, budget=budget, nested=True)
    evidence = result["verification"]

    assert evidence["success"] is False
    assert "Lean failed" in str(evidence[key])
    assert evidence[key + "_truncated"] is True
    assert result["result_truncated"] is True
    assert len(serialized(result)) <= budget


@pytest.mark.parametrize("nested", [False, True], ids=["lean_verify", "file_write"])
@pytest.mark.parametrize("long_path", [False, True], ids=["short_path", "long_path"])
def test_tiny_budget_bounds_unicode_structured_errors_and_marks_missing_evidence(
    nested: bool, long_path: bool,
) -> None:
    verification = {
        "success": False,
        "returncode": 1,
        "errors": [
            {
                "severity": "error",
                "pos": {"line": index + 1, "column": 2},
                "data": (f"case 分支{index}\nα : Type\nh : 证明\n⊢ 目标∀α\n" * 1000),
            }
            for index in range(4)
        ],
        "output": "控制台重复诊断\n" * 10000,
    }
    payload = {"verification": verification} if nested else dict(verification)
    payload.update(
        success=False,
        score=0.0,
        file_path="候选" + ("长" * 400 if long_path else "") + ".lean",
        primary_diagnostic={"message": "证明错误" * 2000},
    )
    result = project(payload, budget=512, nested=nested)
    evidence = result["verification"] if nested else result

    assert len(serialized(result)) <= 512
    assert result["result_truncated"] is True
    assert result["success"] is False
    assert evidence["success"] is False
    assert evidence.get("errors_omitted", 0) > 0 or evidence.get("errors_truncated") is True


@pytest.mark.parametrize("budget", [512, 4000])
def test_draft_compilation_reports_its_result_and_holes_without_scoring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, budget: int,
) -> None:
    goals = [{"pos": {"line": 1, "column": 32}, "goal": "α : Type\n⊢ True" * 500}]
    stdout = "DVP_LOCAL_DRAFT_JSON:" + json.dumps({"messages": [], "sorries": goals}) + "\n"
    verification = local_result(tmp_path, monkeypatch, stdout, draft=True)
    assert verification["success"] is True
    payload = {
        "success": True,
        "score": 0.0,
        "verification_profile": "draft_allow_sorry",
        "draft_typechecked": True,
        "strict_verified": False,
        "proof_complete": False,
        "objective_complete": False,
        "holes": goals,
        "verification": verification,
    }
    result = project(payload, budget=budget, nested=True)

    assert result["success"] is True
    assert result["verification"]["success"] is True
    assert result["check_kind"] == "compilation"
    assert "holes" in result or "holes" in result["verification"]
    for key in ("proof_complete", "objective_complete", "draft_typechecked",
                "strict_verified", "score", "status"):
        assert key not in result
    # The raw verifier result still carries the evidence used for acceptance.
    assert payload["score"] == 0.0 and payload["strict_verified"] is False
    assert len(serialized(result)) <= budget


@pytest.mark.parametrize("nested", [False, True], ids=["lean_verify", "file_write"])
@pytest.mark.parametrize("budget", [512, 12000])
def test_duplicate_console_blocks_are_removed_without_truncating_diagnostics(
    nested: bool, budget: int,
) -> None:
    error = "Submission.lean:3:2: error: unsolved goals\nx : Nat\n⊢ x = 0"
    warning = "Submission.lean:1:1: warning: unused variable"
    verification = {
        "success": False, "errors": [error], "warnings": [warning],
        "output": error + "\n" + warning + "\n",
        "stdout": error + "\n", "stderr": warning,
    }
    payload = {"success": True, "verification": verification} if nested else verification
    before = deepcopy(payload)
    result = project(payload, budget=budget, nested=nested)
    evidence = result["verification"] if nested else result

    assert evidence["errors"] == [error]
    assert evidence["warnings"] == [warning]
    assert all(not evidence.get(key) for key in ("output", "stdout", "stderr"))
    assert not result.get("result_truncated")
    assert payload == before
    assert project(result, budget=budget, nested=nested) == result
    assert len(serialized(result)) <= budget


@pytest.mark.parametrize("nested", [False, True], ids=["lean_verify", "file_write"])
def test_mixed_console_keeps_check_results_and_embedded_diagnostic_text(nested: bool) -> None:
    error = "Submission.lean:3:2: error: unknown identifier `missing`"
    warning = "Submission.lean:4:2: warning: unused variable"
    # A diagnostic quoted inside unrelated output is not a diagnostic block.
    quoted = f"printed diagnostic: {error} (quoted)"
    verification = {
        "success": False, "errors": [error], "warnings": [warning],
        "output": "\n".join(["Nat : Type", error, quoted, warning, "Bool : Type"]),
    }
    payload = {"success": True, "verification": verification} if nested else verification
    result = project(payload, budget=12000, nested=nested)
    evidence = result["verification"] if nested else result

    assert evidence["errors"] == [error]
    assert evidence["warnings"] == [warning]
    assert [line for line in evidence["output"].splitlines() if line.strip()] == [
        "Nat : Type", quoted, "Bool : Type",
    ]
    assert not result.get("result_truncated")


@pytest.mark.parametrize("nested", [False, True], ids=["lean_verify", "file_write"])
@pytest.mark.parametrize("budget", [512, 4000])
def test_unique_console_output_survives_budgeting_alongside_errors(
    nested: bool, budget: int,
) -> None:
    error = "Submission.lean:3:2: error: missing lemma"
    verification = {
        "success": False, "errors": [error],
        "output": "Nat : Type\n" + "compiler context\n" * 2000 + error,
    }
    payload = {"success": True, "verification": verification} if nested else verification
    result = project(payload, budget=budget, nested=nested)
    evidence = result["verification"] if nested else result

    assert evidence["errors"] == [error]
    assert "Nat : Type" in evidence["output"]
    assert evidence["output_truncated"] is True
    assert result["result_truncated"] is True
    assert len(serialized(result)) <= budget


@pytest.mark.parametrize("nested", [False, True], ids=["lean_verify", "file_write"])
@pytest.mark.parametrize("long_message", [False, True], ids=["complete_message", "capped_prefix"])
def test_regenerated_primary_message_is_redundant_but_its_goals_and_location_are_not(
    nested: bool, long_message: bool,
) -> None:
    context = "h : True\n" * (300 if long_message else 1)
    error = {
        "file": "Submission.lean", "pos": {"line": 3, "column": 2},
        "endPos": {"line": 4, "column": 8},
        "data": "unsolved goals\n" + context + "⊢ n = 0",
    }
    payload = feedback_payload({"success": False, "errors": [error]}, nested=nested)
    before = deepcopy(payload)
    result = project(payload, budget=12000, nested=nested)
    evidence = result["verification"] if nested else result

    assert evidence["errors"] == [error]
    assert result["primary_diagnostic"] == {
        key: value for key, value in before["primary_diagnostic"].items()
        if key != "message"
    }
    assert result["primary_diagnostic"]["primary_goal"] == "n = 0"
    assert result["primary_diagnostic"]["primary_goals"] == ["n = 0"]
    assert not result.get("result_truncated")
    assert payload == before
    assert project(result, budget=12000, nested=nested) == result


@pytest.mark.parametrize("nested", [False, True], ids=["lean_verify", "file_write"])
@pytest.mark.parametrize("field,value", [
    ("message", "unsolved goals"),
    ("message", "Additional diagnostic from another checker"),
    ("file", "Submission/Other.lean"),
    ("line", 99), ("column", 99), ("end_line", 99), ("end_column", 99),
])
def test_unrecognized_primary_message_or_source_is_not_removed(
    nested: bool, field: str, value: object,
) -> None:
    error = {
        "file": "Submission.lean", "pos": {"line": 3, "column": 2},
        "endPos": {"line": 4, "column": 8},
        "data": "unsolved goals\nn : Nat\n⊢ n = 0",
    }
    payload = feedback_payload({"success": False, "errors": [error]}, nested=nested)
    payload["primary_diagnostic"][field] = value
    before = deepcopy(payload)
    result = project(payload, budget=12000, nested=nested)

    assert result["primary_diagnostic"] == before["primary_diagnostic"]
    assert payload == before


def test_diagnostic_deduplication_does_not_cross_verification_sources() -> None:
    outer = "Submission.lean:3:2: error: unsolved goals\n⊢ False"
    inner = "Submission/Helper.lean:3:2: error: unsolved goals\n⊢ False"
    payload = {
        "success": True, "errors": [outer],
        "verification": {
            "success": False, "errors": [inner],
            "output": outer + "\n" + inner,
        },
    }
    before = deepcopy(payload)
    result = project(payload, budget=12000, nested=True)

    assert result["errors"] == [outer]
    assert result["verification"]["errors"] == [inner]
    assert result["verification"]["output"].strip() == outer
    assert payload == before


@pytest.mark.parametrize("nested", [False, True], ids=["lean_verify", "file_write"])
def test_tiny_budget_keeps_primary_goal_outside_the_error_excerpt(nested: bool) -> None:
    error = (
        "Submission.lean:3:2: error: unsolved goals\n"
        + "h : True\n" * 1200 + "⊢ a + b = c\n\n"
        + "compiler diagnostic detail\n" * 1200
    )
    payload = feedback_payload({"success": False, "errors": [error]}, nested=nested)
    result = project(payload, budget=512, nested=nested)
    evidence = result["verification"] if nested else result

    assert result["primary_diagnostic"]["primary_goal"] == "a + b = c"
    assert evidence["success"] is False
    assert evidence.get("errors_omitted", 0) > 0 or evidence.get("errors_truncated") is True
    assert result["result_truncated"] is True
    assert len(serialized(result)) <= 512


@pytest.mark.parametrize("budget", [512, 4000])
@pytest.mark.parametrize("nested_console", ["Bool : Type", "Nat : Type"])
def test_nested_verification_keeps_both_console_sources_when_diff_exceeds_budget(
    budget: int, nested_console: str,
) -> None:
    error = "Submission.lean:3:2: error: missing lemma"
    payload = {
        "success": True, "file_path": "Submission.lean",
        "output": "Nat : Type", "diff": "+ changed source\n" * 2000,
        "verification": {
            "success": False, "errors": [error],
            "output": nested_console + "\n" + error,
        },
    }
    before = deepcopy(payload)
    result = project(payload, budget=budget, nested=True)

    # Matching text from different result envelopes is not a proven duplicate.
    assert result["output"] == "Nat : Type"
    assert result["verification"]["output"] == nested_console
    assert result["verification"]["errors"] == [error]
    assert result["result_truncated"] is True
    assert len(serialized(result)) <= budget
    assert payload == before
    assert project(result, budget=budget, nested=True) == result
