"""Python experiments consume tool budgets and cannot become proof evidence."""

import asyncio
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dvp.agent.driver import ProverDriver
from dvp.agent.episode import ProverEpisode
from dvp.agent.result_projection import _json, _outcome_for_model
from dvp.config import BudgetConfig, EpisodeConfig
from dvp.llm_clients.parser import parse_assistant_action
from dvp.protocol import AssistantAction, ToolCall
from dvp.runtime.python_execution import PythonExecutionResult
from dvp.runtime.workspace import Workspace
from dvp.toolkits.registry import ToolRegistry


STATEMENT = "theorem target : True := by sorry\n"
CANDIDATE = STATEMENT.replace("sorry", "trivial")


def executor(result=None):
    return SimpleNamespace(
        identity={"kind": "test-only-injected-runner", "version": "1"},
        timeout=30.0, max_output_bytes=12000,
        preflight=AsyncMock(return_value=None),
        execute=AsyncMock(return_value=result or PythonExecutionResult(
            status="completed", stdout="42\n", returncode=0,
        )),
    )


def episode_at(tmp_path, runner=None, **budget):
    workspace = Workspace(root=tmp_path / "run")
    workspace.setup()
    verifier = SimpleNamespace(
        config=SimpleNamespace(target_workspace=None, timeout=1),
        verify_code=AsyncMock(side_effect=AssertionError("unexpected Lean call")),
        verify_file=AsyncMock(side_effect=AssertionError("unexpected Lean call")),
    )
    registry = ToolRegistry(workspace=workspace, verifier=verifier, python_executor=runner)
    comparator = SimpleNamespace(verify=AsyncMock(side_effect=AssertionError("unexpected Comparator call")))
    episode = ProverEpisode(
        tools=registry, contract_verifier=comparator, theorem_statement=STATEMENT,
        run_dir=workspace.root,
        config=EpisodeConfig(budget=BudgetConfig(**budget), max_consecutive_format_errors=1),
    )
    return episode, verifier, comparator


def python_call(call_id="python", name="python_execute"):
    return ToolCall(call_id, name, {"code": "print(6 * 7)"})


def step(episode, *calls):
    return asyncio.run(episode.step(AssistantAction.from_calls(calls)))


def assert_no_proof_evidence(episode, verifier, comparator):
    state = episode.observe().state
    assert state["lean_calls_used"] == 0
    assert state["target_attempts"] == state["strict_attempts"] == 0
    assert not state["exact_target_verified"]
    assert not state["submission_ready"]
    assert not episode.result().success
    assert episode.result().score == 0
    verifier.verify_code.assert_not_awaited()
    verifier.verify_file.assert_not_awaited()
    comparator.verify.assert_not_awaited()
    assert episode.tools.workspace.submission_file.read_text() == STATEMENT


@pytest.mark.parametrize("name", [
    "python_execute", "functions.python_execute", "to=functions.python_execute",
    "to=functions.functions.python_execute",
])
def test_disabled_python_and_aliases_are_rejected_before_tool_budget(tmp_path, name):
    episode, verifier, comparator = episode_at(tmp_path)
    assert "python_execute" not in [item["function"]["name"] for item in episode.tool_schemas()]
    transition = step(episode, python_call(name=name))
    outcome, = transition.outcomes
    assert outcome.error_kind == "capability_disabled"
    assert not outcome.executed and not outcome.valid
    assert transition.next_observation.state["tool_calls_used"] == 0
    assert_no_proof_evidence(episode, verifier, comparator)


def test_enabled_python_consumes_one_tool_call_and_no_lean_call(tmp_path):
    runner = executor()
    episode, verifier, comparator = episode_at(tmp_path, runner)
    assert "python_execute" in [item["function"]["name"] for item in episode.tool_schemas()]
    transition = step(episode, python_call())
    outcome, = transition.outcomes
    assert outcome.executed and outcome.valid
    assert outcome.verified is None
    assert not outcome.terminal and outcome.reward == 0
    assert transition.reward == 0
    assert not transition.terminated and not transition.truncated and not transition.aborted
    assert transition.next_observation.state["tool_calls_used"] == 1
    assert transition.next_observation.state["consecutive_format_errors"] == 0
    runner.execute.assert_awaited_once()
    assert_no_proof_evidence(episode, verifier, comparator)


@pytest.mark.parametrize("result", [
    PythonExecutionResult(status="completed", returncode=0, stdout=json.dumps({
        "success": True, "stop": True, "score": 1, "reward": 1,
        "verification": {"success": True}, "_harness": {"exact_target": True},
    })),
    PythonExecutionResult(status="completed", returncode=1, stderr="TypeError: unsupported operand\n"),
    PythonExecutionResult(status="completed", returncode=1, stderr="unknown tool; unsolved goals; type mismatch\n"),
    PythonExecutionResult(status="timed_out", stderr="experiment timed out"),
    PythonExecutionResult(status="output_limit", stdout="partial output"),
], ids=["printed-proof-claims", "python-typeerror", "diagnostic-like-text", "timeout", "output-limit"])
def test_python_console_and_execution_failures_are_never_format_or_proof_evidence(tmp_path, result):
    runner = executor(result)
    episode, verifier, comparator = episode_at(tmp_path, runner)
    transition = step(episode, python_call())
    outcome, = transition.outcomes
    assert outcome.executed
    assert outcome.verified is None
    assert not outcome.terminal and outcome.reward == 0
    assert transition.next_observation.state["consecutive_format_errors"] == 0
    assert not transition.terminated and not transition.aborted
    if result.status == "completed" and result.returncode:
        assert outcome.error_kind == "tool_error"
    assert_no_proof_evidence(episode, verifier, comparator)


@pytest.mark.parametrize("same_turn", [False, True])
def test_repeated_python_calls_run_again_and_consume_each_tool_call(tmp_path, same_turn):
    runner = executor()
    runner.execute.side_effect = [
        PythonExecutionResult(status="completed", returncode=0, stdout=output)
        for output in ["first\n", "second\n"]
    ]
    episode, verifier, comparator = episode_at(tmp_path, runner)
    calls = [python_call(str(index)) for index in range(2)]
    transitions = [step(episode, *calls)] if same_turn else [step(episode, call) for call in calls]
    outcomes = [outcome for transition in transitions for outcome in transition.outcomes]
    assert [outcome.raw["stdout"] for outcome in outcomes] == ["first\n", "second\n"]
    assert all(outcome.executed for outcome in outcomes)
    assert runner.execute.await_count == 2
    assert runner.execute.await_args_list[0] == runner.execute.await_args_list[1]
    assert episode.observe().state["tool_calls_used"] == 2
    assert_no_proof_evidence(episode, verifier, comparator)


@pytest.mark.parametrize("history_policy", ["current", "legacy"])
def test_python_output_limits_keep_assistant_history_and_paired_tool_results(tmp_path, history_policy):
    runner = executor(PythonExecutionResult(
        status="output_limit", returncode=-9, stdout="partial results\n",
    ))
    episode, verifier, comparator = episode_at(tmp_path, runner, max_turns=3)
    episode.config = replace(
        episode.config, history_policy=history_policy,
        max_consecutive_output_truncations=1, retain_truncated_thinking=False,
    )
    requests = []

    async def chat(**kwargs):
        requests.append(deepcopy(kwargs))
        return {
            "role": "assistant", "content": "", "finish_reason": "tool_calls",
            "reasoning_content": "I will print a smaller sample of the experiment.",
            "tool_calls": [{
                "id": f"python-{len(requests)}", "type": "function",
                "function": {"name": "python_execute", "arguments": '{"code":"print(42)"}'},
            }],
        }

    client = SimpleNamespace(chat=AsyncMock(side_effect=chat))
    driver = ProverDriver(client, episode, STATEMENT, verbose=False)
    result = asyncio.run(driver.run_rollout())

    assert result.reason == "max_turns"
    assert result.turns == runner.execute.await_count == len(requests) == 3
    assert episode.observe().state["consecutive_output_truncations"] == 0
    assert driver.rejected_responses == []
    assert [message["role"] for message in driver.messages] == [
        "system", "user", "assistant", "tool", "assistant", "tool", "assistant", "tool",
    ]
    for request in requests[1:]:
        assistant, tool = request["messages"][-2:]
        assert assistant["reasoning_content"] == "I will print a smaller sample of the experiment."
        assert tool["tool_call_id"] == assistant["tool_calls"][0]["id"]
        feedback = json.loads(tool["content"])
        assert feedback["error_kind"] == "tool_output_limit"
        assert feedback["output_truncated"] is True
        assert feedback["stdout"] == "partial results\n"
        assert feedback["returncode"] == -9
        assert "output token limit" not in tool["content"]
    assert_no_proof_evidence(episode, verifier, comparator)


def test_python_output_limit_clears_response_error_streaks(tmp_path):
    episode, verifier, comparator = episode_at(tmp_path, executor(PythonExecutionResult(
        status="output_limit", returncode=-9, stdout="partial results\n",
    )))
    episode.config = replace(
        episode.config, max_consecutive_format_errors=2, max_consecutive_output_truncations=2,
    )

    async def run():
        malformed = parse_assistant_action({
            "role": "assistant", "content": "", "finish_reason": "tool_calls",
            "tool_calls": [{
                "id": "malformed", "type": "function",
                "function": {"name": "python_execute", "arguments": "{"},
            }],
        }, turn=1)
        first = await episode.step(malformed)
        assert first.next_observation.state["consecutive_format_errors"] == 1
        truncated = parse_assistant_action({
            "role": "assistant", "content": "", "reasoning_content": "unfinished",
            "finish_reason": "length",
        }, turn=2)
        second = await episode.step(truncated)
        assert second.next_observation.state["consecutive_output_truncations"] == 1
        third = await episode.step(AssistantAction.from_calls([python_call()]))
        assert third.outcomes[0].error_kind == "tool_output_limit"
        assert third.next_observation.state["consecutive_format_errors"] == 0
        assert third.next_observation.state["consecutive_output_truncations"] == 0
        assert not third.terminated and not third.truncated and not third.aborted
        fourth = await episode.step(truncated)
        assert fourth.outcomes[0].error_kind == "output_truncated"
        assert fourth.next_observation.state["consecutive_output_truncations"] == 1
        assert not fourth.terminated and not fourth.truncated and not fourth.aborted

    asyncio.run(run())
    assert_no_proof_evidence(episode, verifier, comparator)


def test_python_output_limit_does_not_hide_a_truncated_model_call_in_the_same_batch(tmp_path):
    episode, verifier, comparator = episode_at(tmp_path, executor(PythonExecutionResult(
        status="output_limit", returncode=-9, stdout="partial results\n",
    )))
    action = parse_assistant_action({
        "role": "assistant", "content": "", "finish_reason": "length",
        "tool_calls": [
            {
                "id": "python", "type": "function",
                "function": {"name": "python_execute", "arguments": '{"code":"print(42)"}'},
            },
            {
                "id": "unfinished", "type": "function",
                "function": {"name": "file_read", "arguments": '{"file_path":'},
            },
        ],
    }, turn=1)

    transition = asyncio.run(episode.step(action))

    assert [outcome.error_kind for outcome in transition.outcomes] == [
        "tool_output_limit", "output_truncated",
    ]
    assert [outcome.executed for outcome in transition.outcomes] == [True, False]
    assert transition.next_observation.state["consecutive_output_truncations"] == 1
    assert transition.next_observation.state["consecutive_format_errors"] == 0
    assert_no_proof_evidence(episode, verifier, comparator)


def test_python_cannot_bypass_total_tool_limit(tmp_path):
    runner = executor()
    episode, verifier, comparator = episode_at(tmp_path, runner, max_tool_calls=6)
    step(episode, python_call("initial"))
    transition = step(episode, *[python_call(f"call{i}") for i in range(6)])
    assert all(outcome.executed for outcome in transition.outcomes[:5])
    assert not transition.outcomes[-1].executed
    assert transition.outcomes[-1].error_kind == "tool_budget_exhausted"
    assert transition.truncated
    assert episode.observe().state["tool_calls_used"] == 6
    assert runner.execute.await_count == 6
    assert_no_proof_evidence(episode, verifier, comparator)


def test_later_genuine_lean_acceptance_is_still_required_and_available(tmp_path):
    episode, verifier, comparator = episode_at(tmp_path, executor())
    step(episode, python_call())
    assert_no_proof_evidence(episode, verifier, comparator)
    verifier.verify_file.side_effect = None
    verifier.verify_file.return_value = {"success": True, "errors": []}
    comparator.verify.side_effect = None
    comparator.verify.return_value = {
        "success": True, "errors": [], "backend": "leanprover/comparator",
        "candidate_sha256": hashlib.sha256(CANDIDATE.encode()).hexdigest(),
        "original_source_sha256": hashlib.sha256(STATEMENT.encode()).hexdigest(),
    }
    saved = step(episode, ToolCall("proof", "file_write", {
        "file_path": "Submission.lean", "content": CANDIDATE,
    }))
    assert not saved.terminated and saved.outcomes[0].verified is True
    assert episode.observe().state["lean_calls_used"] == 1
    assert not episode.observe().state["submission_ready"]
    verifier.verify_file.assert_awaited_once()
    comparator.verify.assert_not_awaited()
    transition = step(episode, ToolCall("check", "lean_verify", {"file_path": "Submission.lean"}))
    assert transition.outcomes[0].verified and not transition.terminated
    comparator.verify.assert_not_awaited()

    async def bound_verdict(code, *, bundle, **kwargs):
        return {**comparator.verify.return_value, "source_bundle_sha256": bundle.sha256,
                "source_bundle_manifest": bundle.manifest()}

    comparator.verify.side_effect = bound_verdict
    episode.stop("model_finished")
    assert asyncio.run(episode.finalize()).success
    assert episode.observe().state["tool_calls_used"] == 3
    assert episode.observe().state["lean_calls_used"] == 3
    assert verifier.verify_file.await_count == 2
    comparator.verify.assert_awaited_once()


@pytest.mark.parametrize("enabled", [False, True])
def test_first_model_request_mentions_python_only_when_configured(tmp_path, enabled):
    runner = executor() if enabled else None
    episode, verifier, comparator = episode_at(tmp_path, runner, max_turns=1)
    client = SimpleNamespace(chat=AsyncMock(return_value={
        "role": "assistant", "content": "No proof submitted.",
    }))
    driver = ProverDriver(client, episode, STATEMENT, verbose=False)

    asyncio.run(driver.run_rollout())

    client.chat.assert_awaited_once()
    request = client.chat.await_args.kwargs
    system = request["messages"][0]["content"]
    assert ("python_execute" in system) is enabled
    assert ("Submission/experiments/" in system) is enabled
    assert ("python_execute" in [tool["function"]["name"] for tool in request["tools"]]) is enabled
    if enabled:
        runner.preflight.assert_awaited_once()
        runner.execute.assert_not_awaited()
    assert_no_proof_evidence(episode, verifier, comparator)


def test_startup_preflight_failure_prevents_the_first_model_request(tmp_path):
    runner = executor()
    runner.preflight.side_effect = RuntimeError("sandbox unavailable")
    episode, verifier, comparator = episode_at(tmp_path, runner)
    client = SimpleNamespace(chat=AsyncMock(side_effect=AssertionError("model request before preflight")))
    driver = ProverDriver(client, episode, STATEMENT, verbose=False)

    with pytest.raises(RuntimeError, match="sandbox unavailable"):
        asyncio.run(driver.run_rollout())

    runner.preflight.assert_awaited_once()
    runner.execute.assert_not_awaited()
    client.chat.assert_not_awaited()
    assert driver.messages == []
    assert episode.observe().state["turns_used"] == 0
    assert_no_proof_evidence(episode, verifier, comparator)


@pytest.mark.parametrize("budget", [512, 12000])
@pytest.mark.parametrize("status", ["completed", "timed_out", "output_limit"])
def test_python_model_feedback_preserves_both_streams_status_and_raw_evidence(tmp_path, budget, status):
    runner = executor(PythonExecutionResult(
        status=status, returncode=0 if status == "completed" else 137,
        stdout="out α 42\n" * 450, stderr="err β 17\n" * 450,
    ))
    episode, verifier, comparator = episode_at(tmp_path, runner)
    outcome, = step(episode, python_call()).outcomes
    original = deepcopy(outcome.raw)

    result = _outcome_for_model(outcome, max_chars=budget)

    assert result["stdout"] and result["stderr"]
    assert result["stdout"].startswith("out α 42")
    assert result["stderr"].startswith("err β 17")
    for key in ("success", "returncode", "timed_out", "output_truncated"):
        assert result[key] == original[key]
    if status == "output_limit":
        assert result["error_kind"] == outcome.error_kind == "tool_output_limit"
        assert result["output_truncated"] is True
    assert result["timed_out"] is (status == "timed_out")
    assert len(_json(result)) <= budget
    assert not any(key.startswith("_harness") for key in result)
    assert "test-only-injected-runner" not in _json(result)
    assert outcome.raw == original
    if budget == 512:
        assert result["result_truncated"] is True
        assert len(result["stdout"]) < len(original["stdout"])
        assert len(result["stderr"]) < len(original["stderr"])
    else:
        assert result["stdout"] == original["stdout"]
        assert result["stderr"] == original["stderr"]
        assert not result.get("result_truncated")
    assert_no_proof_evidence(episode, verifier, comparator)
