"""Model requests contain work evidence; scoring stays in episode artifacts."""

import asyncio
from copy import deepcopy
import json

import pytest

from dvp.agent.driver import ProverDriver
from dvp.agent.result_projection import _compact, _json
from test_format_error_limit import CANDIDATE, STATEMENT, make_episode, native_call
from test_output_truncation import scripted_client


INTERNAL_KEYS = {
    "_harness", "_harness_infrastructure_error", "reward", "score", "stop",
    "terminal", "objective_complete", "proof_complete", "local_proof_verified",
    "lookup_succeeded", "strict_verified", "draft_typechecked", "verified",
    "fixed_task_contract", "source_bundle_sha256", "source_bundle_manifest",
    "verification_profile",
}


def assert_work_result(result):
    assert not INTERNAL_KEYS.intersection(result)
    for key in ("verification", "contract_verification"):
        if isinstance(result.get(key), dict):
            assert_work_result(result[key])


def test_next_request_uses_relative_diagnostics_but_trace_keeps_absolute_evidence(tmp_path):
    episode, verifier, comparator, sink = make_episode(tmp_path)
    root = episode.tools.workspace.workspaces_dir
    error = f"{root}/Submission/Helper.lean:3:7: error: unsolved goals\n⊢ True"
    verifier.verify_code.side_effect = [
        {"success": False, "returncode": 1, "errors": [error], "output": error},
        {"success": True, "returncode": 0, "errors": [], "output": "",
         "verification_profile": "strict", "strict_verified": True,
         "source_placeholders": {"total": 0, "items": []}},
    ]
    client, requests = scripted_client([
        ({"role": "assistant", "content": "", "tool_calls": [
            native_call("lean_verify", {"code": "theorem helper : True := by trivial"}, "helper"),
        ]}, "tool_calls"),
        ({"role": "assistant", "content": "", "tool_calls": [
            native_call("file_write", {"file_path": "Submission.lean", "content": CANDIDATE}, "save"),
            native_call("lean_verify", {"code": CANDIDATE}, "target"),
        ]}, "tool_calls"),
        ({"role": "assistant", "content": "Done."}, "stop"),
    ])
    result = asyncio.run(ProverDriver(client, episode, STATEMENT, verbose=False).run())
    assert result.success
    comparator.verify.assert_awaited_once()
    feedback = next(m for m in requests[1]["messages"] if m["role"] == "tool")
    visible = json.loads(feedback["content"])
    assert visible["errors"] == ["Submission/Helper.lean:3:7: error: unsolved goals\n⊢ True"]
    raw = next(e["payload"] for e in sink.events if e["event"] == "transition")["outcomes"][0]["raw"]
    assert raw["errors"] == [error]
    assert raw["output"] == error


@pytest.mark.parametrize("budget", [512, 12000])
def test_retrieval_scores_and_source_text_survive_removal_of_episode_score(budget):
    source = "-- reward=0; objective_complete=false; _harness\ntheorem score : True := by trivial"
    payload = {
        "success": True, "score": 0, "reward": 0, "stop": False,
        "_harness": {"attempt_kind": "lookup", "target_checked": False},
        "results": [{"fullname": "score", "signature": "score : True",
                     "source_code": source, "score": 0.75, "score_kind": "reciprocal_rank"}],
    }
    before = deepcopy(payload)
    result = _compact(payload, tool_name="lean_retrieve", max_chars=budget)
    assert_work_result(result)
    assert result["success"] is True
    assert result["results"][0]["score"] == 0.75
    assert result["results"][0]["score_kind"] == "reciprocal_rank"
    assert result["results"][0]["source_code"] == source
    assert payload == before
    assert len(_json(result)) <= budget


@pytest.mark.parametrize("budget", [512, 12000])
def test_nested_compiler_failure_keeps_diagnostics_and_raw_acceptance_evidence(budget):
    error = "Submission/Helper.lean:3:7: error: unknown identifier 'score'"
    payload = {
        "success": True, "score": 0, "stop": False, "file_path": "Submission.lean",
        "source_bundle_sha256": "bound-source",
        "_harness": {"target_checked": True, "contract_verified": False},
        "verification": {
            "success": False, "returncode": 1, "errors": [error],
            "output": "compiler diagnostic\n" * 4000,
            "_harness_infrastructure_error": False, "strict_verified": False,
            "fixed_task_contract": {"contract_hash": "original-task"},
        },
    }
    before = deepcopy(payload)
    result = _compact(payload, tool_name="file_write", max_chars=budget)
    assert_work_result(result)
    assert result["success"] is True  # The file was written.
    assert result["verification"]["success"] is False  # Compilation failed.
    assert result["verification"]["returncode"] == 1
    assert result["verification"]["errors"] == [error]
    assert payload == before
    assert len(_json(result)) <= budget


@pytest.mark.parametrize("budget", [512, 12000])
def test_imported_helper_holes_remain_visible_when_entry_has_no_holes(budget):
    payload = {
        "success": True, "verification_profile": "draft_allow_sorry",
        "strict_verified": False, "source_placeholders": {"total": 0, "items": []},
        "dependency_placeholders": [{"module": "Submission.Helper", "total": 1}],
        "output": "Lean warning\n" * 4000,
    }
    result = _compact(payload, tool_name="lean_verify", max_chars=budget)
    assert_work_result(result)
    assert result["dependency_placeholders"] == payload["dependency_placeholders"]
    assert "source_placeholders" not in result
    assert result["check_kind"] == "compilation"
    assert len(_json(result)) <= budget


def test_actual_next_requests_hide_scoring_without_changing_acceptance(tmp_path):
    episode, verifier, comparator, sink = make_episode(tmp_path)
    verifier.verify_code.return_value = {
        "success": True, "errors": [], "returncode": 0,
        "verification_profile": "strict", "strict_verified": True,
        "source_placeholders": {"total": 0, "items": []},
    }
    verifier.verify_file.return_value = verifier.verify_code.return_value.copy()
    helper = "theorem helper : True := by trivial\n"
    client, requests = scripted_client([
        ({"role": "assistant", "content": "", "tool_calls": [
            native_call("file_read", {"file_path": "Submission.lean"}, "read"),
        ]}, "tool_calls"),
        ({"role": "assistant", "content": "", "tool_calls": [
            native_call("lean_verify", {"code": helper}, "helper"),
        ]}, "tool_calls"),
        ({"role": "assistant", "content": "", "tool_calls": [
            native_call("file_write", {"file_path": "Submission.lean", "content": CANDIDATE}, "save"),
            native_call("lean_verify", {"code": CANDIDATE}, "target"),
        ]}, "tool_calls"),
        ({"role": "assistant", "content": "Done."}, "stop"),
    ])
    driver = ProverDriver(client, episode, STATEMENT, verbose=False)
    result = asyncio.run(driver.run())
    assert result.success and result.score == 1
    assert result.turns == 4 and result.tool_calls == 4
    comparator.verify.assert_awaited_once()
    for request in requests:
        for message in request["messages"]:
            if message["role"] == "tool":
                projected = json.loads(message["content"])
                assert_work_result(projected)
                assert projected["success"] is True
    # Terminal results are archived in chat too, even without a next request.
    for message in driver.messages:
        if message["role"] == "tool":
            assert_work_result(json.loads(message["content"]))
    transitions = [e["payload"] for e in sink.events if e["event"] == "transition"]
    assert transitions[0]["outcomes"][0]["raw"]["_harness"]["attempt_kind"] == "lookup"
    auxiliary = transitions[1]["outcomes"][0]
    assert auxiliary["verified"] is True and auxiliary["reward"] == 0
    assert auxiliary["raw"]["_harness"]["attempt_kind"] == "auxiliary"
    assert not transitions[1]["terminated"]
    checked = transitions[2]["outcomes"][1]
    assert "contract_verification" not in checked["raw"]
    assert checked["verified"] and checked["reward"] == 0 and not transitions[2]["terminated"]
    assert transitions[3]["terminated"] and transitions[3]["reward"] == 0
    assert any(event["event"] == "final_acceptance" for event in sink.events)
    assert result.final_tool_result["contract_verification"]["backend"] == "leanprover/comparator"
