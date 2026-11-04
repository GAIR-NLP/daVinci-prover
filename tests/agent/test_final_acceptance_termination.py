"""Development continues after compilation; saved files are graded once at exit."""
import asyncio
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dvp.agent.driver import ProverDriver
from dvp.agent.episode import ProverEpisode
from dvp.artifacts.trajectory import MemoryTrajectorySink
from dvp.config import BudgetConfig, EpisodeConfig
from dvp.orchestration.attempts import _result_is_infrastructure
from dvp.protocol import AssistantAction
from dvp.runtime.workspace import Workspace
from dvp.toolkits.registry import ToolRegistry


STATEMENT = "theorem target : True := by sorry\n"
CANDIDATE = STATEMENT.replace("sorry", "trivial")


def accepted():
    return {
        "success": True, "errors": [], "backend": "leanprover/comparator",
        "candidate_sha256": hashlib.sha256(CANDIDATE.encode()).hexdigest(),
        "original_source_sha256": hashlib.sha256(STATEMENT.encode()).hexdigest(),
    }


def tool_call(name, arguments, call_id="candidate"):
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}


def message(*calls):
    return {"role": "assistant", "content": "", "tool_calls": list(calls)}


def make_driver(tmp_path, response, messages, *, local_results=None):
    workspace = Workspace(root=tmp_path / "run")
    workspace.setup()
    verifier = SimpleNamespace(
        config=SimpleNamespace(target_workspace=None, timeout=1),
        verify_code=AsyncMock(return_value={"success": True, "errors": []}, side_effect=local_results),
        verify_file=AsyncMock(return_value={"success": True, "errors": []}, side_effect=local_results),
    )
    tools = ToolRegistry(workspace=workspace, verifier=verifier)
    comparator = SimpleNamespace(verify=AsyncMock(return_value=response))
    async def bound_verdict(code, *, bundle, **kwargs):
        if isinstance(response, Exception):
            raise response
        if isinstance(response, dict) and response.get("success") is True:
            return {**response, "source_bundle_sha256": bundle.sha256,
                    "source_bundle_manifest": bundle.manifest()}
        return response

    comparator.verify.side_effect = bound_verdict
    client = SimpleNamespace(chat=AsyncMock(side_effect=[
        *messages, AssertionError("unexpected model generation after final acceptance"),
    ]))
    sink = MemoryTrajectorySink()
    episode = ProverEpisode(
        tools=tools, contract_verifier=comparator, theorem_statement=STATEMENT,
        run_dir=workspace.root, sink=sink,
        config=EpisodeConfig(
            budget=BudgetConfig(max_turns=10),
            # Final acceptance still ends on error with this opt-out.
            abort_on_infrastructure_error=False,
        ),
    )
    return ProverDriver(client, episode, STATEMENT, verbose=False), verifier, comparator, sink


@pytest.mark.parametrize("tool", ["lean_verify", "file_write", "file_edit"])
@pytest.mark.parametrize("verdict", ["accepted", "rejected", "timeout", "exception", "invalid", "stale"])
def test_final_acceptance_follows_visible_stop_after_all_development_calls(tmp_path, tool, verdict):
    responses = {
        "accepted": accepted(),
        "rejected": {"success": False, "errors": ["modified original definition"]},
        "timeout": {"success": False, "command": ["comparator"],
                    "errors": ["Lean verification timed out after 1s"]},
        "exception": RuntimeError("comparator unavailable"),
        "invalid": "invalid response",
        "stale": {**accepted(), "candidate_sha256": "wrong candidate"},
    }
    arguments = {
        "lean_verify": {"code": CANDIDATE},
        "file_write": {"file_path": "Submission.lean", "content": CANDIDATE},
        "file_edit": {"file_path": "Submission.lean", "old_str": "sorry", "new_str": "trivial"},
    }[tool]
    calls = [tool_call(tool, arguments)]
    if tool != "lean_verify":
        calls.append(tool_call("lean_verify", {"file_path": "Submission.lean"}, "check"))
    if tool == "lean_verify":
        calls.insert(0, tool_call("file_write", {"file_path": "Submission.lean", "content": CANDIDATE}, "save"))
    extra = tool_call("file_write", {"file_path": "Submission/after.txt", "content": "executed"}, "after")
    driver, verifier, comparator, sink = make_driver(
        tmp_path, responses[verdict], [message(*calls, extra), {"role": "assistant", "content": "Done."}],
    )
    result = asyncio.run(driver.run())
    assert driver.client.chat.await_count == 2
    assert comparator.verify.await_count == 1
    assert comparator.verify.await_args.args == (CANDIDATE,)
    assert verifier.verify_code.await_count + verifier.verify_file.await_count == 2
    assert (driver.episode.tools.workspace.submission_dir / "after.txt").read_text() == "executed"
    assert result.turns == 2 and result.tool_calls == len(calls) + 1 and result.lean_calls == 3
    assert result.rollout_reason == "model_finished"
    assert not result.truncated and not result.retryable_service_error
    exceptional = verdict in {"timeout", "exception", "invalid", "stale"}
    assert result.aborted is exceptional and result.terminated is not exceptional
    assert _result_is_infrastructure(result.to_dict()) is exceptional
    assert result.success is (verdict == "accepted")
    assert result.score == float(verdict == "accepted")
    expected_reason = (
        "verified_exact_target" if verdict == "accepted" else
        "comparator_rejected" if verdict == "rejected" else
        "comparator_timeout" if verdict == "timeout" else "comparator_infrastructure_error"
    )
    assert result.reason == expected_reason
    assert result.proof_code == CANDIDATE
    assert result.final_tool_result["stop"] is True
    assert "contract_verification" in result.final_tool_result
    transition = next(event["payload"] for event in sink.events if event["event"] == "transition")
    assert transition["outcomes"][len(calls)]["executed"]
    assert not transition["terminated"] and transition["reward"] == 0
    assert asyncio.run(driver.episode.finalize()) == result
    comparator.verify.assert_awaited_once()
    with pytest.raises(RuntimeError, match="episode already finished"):
        asyncio.run(driver.episode.step(AssistantAction()))


@pytest.mark.parametrize("failure", [
    {"success": False, "errors": ["unsolved goals"]},
    {"success": False, "command": ["lean"], "errors": ["Lean verification timed out after 1s"]},
])
def test_ordinary_lean_failure_still_allows_repair_before_final_acceptance(tmp_path, failure):
    driver, verifier, comparator, _ = make_driver(tmp_path, accepted(), [
        message(tool_call("file_write", {"file_path": "Submission.lean",
                                        "content": STATEMENT.replace("sorry", "assumption")}),
                tool_call("lean_verify", {"file_path": "Submission.lean"}, "check-broken")),
        message(tool_call("file_write", {"file_path": "Submission.lean", "content": CANDIDATE}),
                tool_call("lean_verify", {"file_path": "Submission.lean"}, "check-fixed")),
        {"role": "assistant", "content": "Done."},
    ], local_results=[failure, failure, {"success": True, "errors": []}, {"success": True, "errors": []}])
    result = asyncio.run(driver.run())
    assert result.success and result.turns == 3
    assert driver.client.chat.await_count == 3 and verifier.verify_file.await_count == 4
    assert comparator.verify.await_count == 1 and result.lean_calls == 5
