"""Malformed endpoint arguments must not poison the next chat request."""

import asyncio
from copy import deepcopy
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from dvp.config import BudgetConfig, EpisodeConfig
from dvp.agent.driver import ProverDriver
from dvp.agent.messages import _assistant_history_message
from dvp.agent.episode import ProverEpisode
import dvp.llm_clients.client as llm
from dvp.llm_clients.parser import parse_assistant_action


def native_call(arguments, *, name="file_write", call_id="bad"):
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": arguments}}


class Registry:
    def __init__(self):
        self.call = AsyncMock(return_value={"success": True, "content": "read successfully"})

    def schemas(self):
        return [
            {"type": "function", "function": {
                "name": name, "description": "test tool",
                "parameters": {"type": "object", "properties": {}}}}
            for name in ("file_write", "file_read")
        ]


def episode_at(tmp_path, *, turns=2):
    return ProverEpisode(
        tools=Registry(), contract_verifier=object(),
        theorem_statement="example : True := by sorry", run_dir=tmp_path,
        config=EpisodeConfig(budget=BudgetConfig(max_turns=turns)),
    )


@pytest.mark.parametrize("name", [
    "bash_execute", "submit_result", "functions.submit_result", "to=functions.bash_execute",
])
def test_removed_tools_rejected_before_execution_or_budget(tmp_path, name):
    episode = episode_at(tmp_path)
    action = parse_assistant_action({
        "role": "assistant", "content": "",
        "tool_calls": [native_call({}, name=name)],
    }, turn=1)
    transition = asyncio.run(episode.step(action))
    outcome = transition.outcomes[0]
    assert outcome.error_kind == "capability_disabled"
    assert not outcome.executed and not outcome.valid and outcome.reward == 0
    assert not transition.terminated and not transition.aborted
    assert transition.next_observation.state["lean_calls_used"] == 0
    assert transition.next_observation.state["tool_calls_used"] == 0
    episode.tools.call.assert_not_awaited()


@pytest.mark.parametrize("arguments", [
    {"final_code_content": "theorem target : True := by trivial"},
    {"final_code_file_path": "Submission.lean"},
])
def test_legacy_submission_json_is_not_inferred_as_a_tool_call(arguments):
    action = parse_assistant_action({
        "role": "assistant", "content": json.dumps(arguments),
    }, turn=1)
    assert not action.tool_calls


@pytest.mark.parametrize("arguments", [
    '{"content": "example : True := by trivial"{}',
    '{"content_pattern": "cbrt_cube"{}',
    '{"file_path": "Submission.lean", "old_str": "old proof"{}',
    '{"file_path": "Submission.lean", "content": "by sorry"',
    '{"file_path": "Submission.lean"',
    '[]', 'null', '"not an object"', '123', [], 42,
])
def test_invalid_arguments_rejected_before_any_backend_or_lean_budget(tmp_path, arguments):
    message = {"role": "assistant", "content": "", "tool_calls": [native_call(arguments)]}
    action = parse_assistant_action(message, turn=1)
    call = action.tool_calls[0]
    assert call.argument_parse_error
    assert call.arguments == {}

    episode = episode_at(tmp_path)
    transition = asyncio.run(episode.step(action))
    outcome = transition.outcomes[0]
    assert outcome.error_kind == "tool_parse_error"
    assert not outcome.executed and not outcome.valid and outcome.reward == 0
    assert "JSON" in outcome.feedback
    assert "Missing required" not in outcome.feedback
    assert transition.next_observation.state["lean_calls_used"] == 0
    assert transition.next_observation.state["tool_calls_used"] == 0
    assert transition.next_observation.state["turns_used"] == 1
    episode.tools.call.assert_not_awaited()
    assert action.raw == message
    history = _assistant_history_message(action)
    assert json.loads(history["tool_calls"][0]["function"]["arguments"]) == {}


@pytest.mark.parametrize("arguments", ['{ }', ' { "file_path" : "Submission/α.txt" }\n'])
def test_valid_native_json_bytes_preserved(arguments):
    message = {"role": "assistant", "content": "sampled text", "tool_calls": [native_call(arguments)]}
    action = parse_assistant_action(message, turn=1)
    assert action.tool_calls[0].argument_parse_error is None
    assert _assistant_history_message(action) == message


def test_malformed_history_can_receive_feedback_and_make_next_http_request(monkeypatch, tmp_path):
    bad_args = '{"file_path": "Submission.lean", "content": "by sorry"{}'
    valid_args = ' { "file_path" : "Submission/first.txt" }\n'
    response_message = {
        "role": "assistant", "content": "sampled content", "reasoning_content": "sampled reasoning",
        "tool_calls": [native_call(valid_args, name="file_read", call_id="good"),
                       native_call(bad_args)],
    }
    original_response = deepcopy(response_message)
    requests = []

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return httpx.Response(200, json={"choices": [{"message": response_message}]})
        # Reproduce the serving template's JSON validation of chat history.
        for message in payload["messages"]:
            for call in message.get("tool_calls", []):
                assert isinstance(json.loads(call["function"]["arguments"]), dict)
        assert not any(m["role"] in {"assistant", "tool"} for m in payload["messages"])
        history = json.dumps(payload["messages"])
        assert "sampled content" not in history and "sampled reasoning" not in history
        correction = payload["messages"][-1]
        assert correction["role"] == "user"
        # The valid call already ran: retain its arguments and outcome without
        # manufacturing an assistant/tool pair for the rejected response.
        results = json.JSONDecoder().raw_decode(correction["content"].split("TOOL_RESULTS ", 1)[1])[0]
        assert [r["tool_call_id"] for r in results] == ["good", "bad"]
        assert results[0]["executed"] and results[0]["arguments"] == json.loads(valid_args)
        assert results[0]["result"]["content"] == "read successfully"
        assert not results[1]["executed"] and "arguments" not in results[1]
        assert "Invalid tool arguments JSON" in correction["content"]
        assert "This tool call was not executed" in correction["content"]
        return httpx.Response(200, json={"choices": [{"message": {
            "role": "assistant", "content": "corrected call",
            "tool_calls": [native_call('{"file_path":"Submission/second.txt"}',
                                       name="file_read", call_id="recovered")],
        }}]})

    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(llm, "AsyncClient",
                        lambda **kw: original_client(transport=transport, **kw))
    episode = episode_at(tmp_path)
    client = llm.LiteLLMClient(
        model="test", api_key=None, base_url="https://mock.invalid/v1", max_retries=0,
    )
    driver = ProverDriver(client, episode, "example : True := by sorry", verbose=False)
    result = asyncio.run(driver.run_rollout())
    assert len(requests) == 2
    assert result.reason == "max_turns"
    assert episode.tools.call.await_count == 2
    assert [call.args[0] for call in episode.tools.call.await_args_list] == ["file_read", "file_read"]
    assert driver.rejected_responses[0]["response"] == original_response
    assert len(driver.rejected_responses) == 1
    assert response_message == original_response
