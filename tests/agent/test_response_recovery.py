"""Portable corrective feedback for empty and malformed native responses."""

import asyncio
from copy import deepcopy
import json

import httpx
import pytest

from dvp.agent.driver import ProverDriver
from dvp.agent.messages import _assistant_history_message
from dvp.config import EpisodeConfig
import dvp.llm_clients.client as llm
from dvp.llm_clients.parser import parse_assistant_action
from test_format_error_limit import action, make_episode, native_call
from test_tool_argument_errors import episode_at


BAD_ITEMS = [
    None,
    "not an object",
    42,
    {},
    {"id": "bad", "function": "not an object"},
    {"id": "bad", "function": None},
    {"id": "bad", "function": {"arguments": "{}"}},
    {"id": "bad", "function": {"name": 123, "arguments": "{}"}},
    {"id": "bad", "function": {"name": " ", "arguments": "{}"}},
    {"id": "bad", "type": "custom", "function": {"name": "file_read", "arguments": "{}"}},
    # A malformed nested function must not be rescued by unrelated top-level keys.
    {"id": "bad", "function": {}, "name": "file_read", "arguments": "{}"},
]


@pytest.mark.parametrize("bad", BAD_ITEMS)
def test_mixed_native_calls_keep_rejected_items_and_execute_only_valid_call(tmp_path, bad):
    episode = episode_at(tmp_path)
    good = native_call("file_read", {"file_path": "Submission.lean"}, "good")
    message = {"role": "assistant", "content": "", "tool_calls": [good, deepcopy(bad)]}
    parsed = parse_assistant_action(message, turn=1)
    assert len(parsed.tool_calls) == 2
    assert parsed.tool_calls[1].argument_parse_error
    assert parsed.tool_calls[1].arguments == {}

    transition = asyncio.run(episode.step(parsed))

    assert len(transition.outcomes) == 2
    assert transition.outcomes[0].executed
    assert not transition.outcomes[1].executed
    assert transition.outcomes[1].error_kind == "tool_parse_error"
    assert transition.next_observation.state["consecutive_format_errors"] == 1
    episode.tools.call.assert_awaited_once_with("file_read", {"file_path": "Submission.lean"})
    history = _assistant_history_message(parsed)
    assert len(history["tool_calls"]) == 2
    for call in history["tool_calls"]:
        assert call["type"] == "function" and isinstance(call["function"]["name"], str)
        assert isinstance(json.loads(call["function"]["arguments"]), dict)
    assert parsed.raw == message


@pytest.mark.parametrize("bad_container", [{}, {"name": "file_write"}, "invalid", False, 0])
def test_nonarray_native_calls_cannot_fall_through_to_content_or_reasoning(bad_container):
    message = {
        "role": "assistant",
        "tool_calls": bad_container,
        "content": '{"file_path":"Submission.lean","content":"not authorized by this call"}',
        "reasoning_content": '{"code":"example : True := by trivial"}',
    }
    parsed = parse_assistant_action(message, turn=1)
    assert len(parsed.tool_calls) == 1
    assert parsed.tool_calls[0].argument_parse_error.startswith("Response tool_calls must be an array")
    assert parsed.tool_calls[0].arguments == {}
    history = _assistant_history_message(parsed)
    assert isinstance(history["tool_calls"], list)
    assert len(history["tool_calls"]) == 1


def test_existing_flat_native_compatibility_and_valid_wire_bytes_are_preserved():
    flat = {"id": "flat", "name": "functions.file_read", "arguments": '{"file_path":"Submission.lean"}'}
    parsed = parse_assistant_action({"tool_calls": [flat]}, turn=1)
    assert parsed.tool_calls[0].name == "file_read"
    assert parsed.tool_calls[0].argument_parse_error is None
    wire = {"role": "assistant", "content": "", "tool_calls": [
        native_call("file_read", ' { "file_path" : "Submission.lean" }\n', "native"),
    ]}
    assert _assistant_history_message(parse_assistant_action(wire, turn=1)) == wire


def test_missing_nested_arguments_are_not_repaired_from_outer_fields(tmp_path):
    episode = episode_at(tmp_path)
    wire = {"role": "assistant", "content": "", "tool_calls": [{
        "id": "missing-arguments", "type": "function",
        "function": {"name": "file_write"},
        "arguments": '{"file_path":"Submission.lean","content":"unexpected write"}',
    }]}
    parsed = parse_assistant_action(wire, turn=1)
    call = parsed.tool_calls[0]
    assert call.argument_parse_error == "Tool arguments must be a JSON object; received NoneType."
    assert call.arguments == {}

    transition = asyncio.run(episode.step(parsed))

    assert transition.outcomes[0].error_kind == "tool_parse_error"
    assert not transition.outcomes[0].executed
    episode.tools.call.assert_not_awaited()
    history = _assistant_history_message(parsed)
    assert json.loads(history["tool_calls"][0]["function"]["arguments"]) == call.arguments
    assert parsed.raw == wire


@pytest.mark.parametrize("arguments", [{}, {"file_path": "Submission/actual.txt"}])
def test_nested_arguments_match_execution_and_history_despite_outer_fields(tmp_path, arguments):
    episode = episode_at(tmp_path)
    wire = {"role": "assistant", "content": "", "tool_calls": [{
        "id": "nested-arguments", "type": "function",
        "function": {"name": "file_read", "arguments": json.dumps(arguments)},
        "arguments": '{"file_path":"Submission/ignored.txt"}',
    }]}
    parsed = parse_assistant_action(wire, turn=1)
    call = parsed.tool_calls[0]
    assert call.argument_parse_error is None and call.arguments == arguments

    transition = asyncio.run(episode.step(parsed))

    assert transition.outcomes[0].executed
    episode.tools.call.assert_awaited_once_with("file_read", arguments)
    history = _assistant_history_message(parsed)
    assert json.loads(history["tool_calls"][0]["function"]["arguments"]) == arguments
    assert history == wire


def test_empty_feedback_uses_enabled_tools_without_guessing_hidden_output(tmp_path):
    episode, _, _, _ = make_episode(tmp_path, config=EpisodeConfig(enable_lean_retrieve=False))

    transition = asyncio.run(episode.step(action(reasoning_content="Some reasoning.", finish_reason="stop")))

    outcome = transition.outcomes[0]
    assert outcome.error_kind == "empty_response" and not transition.terminated
    assert "no visible text or tool call" in outcome.feedback
    assert "resend a complete tool call" in outcome.feedback
    assert "file_edit" in outcome.feedback and "file_read" in outcome.feedback
    assert "lean_retrieve" not in outcome.feedback and "python_execute" not in outcome.feedback
    assert "visible response" in outcome.feedback and "JSON argument object" in outcome.feedback
    assert "Unknown tool" not in outcome.feedback
    assert "budget" not in outcome.feedback.lower()
    assert transition.next_observation.state["consecutive_format_errors"] == 0


def test_mixed_unknown_tool_feedback_does_not_deny_other_executed_calls(tmp_path):
    episode, _, _, _ = make_episode(tmp_path, config=EpisodeConfig(enable_lean_retrieve=False))
    transition = asyncio.run(episode.step(action(
        native_call("file_write", {"file_path": "Submission/note.txt", "content": "saved"}, "good"),
        native_call("edit", {"file_path": "Submission.lean", "old_str": "sorry", "new_str": "trivial"}, "bad"),
        finish_reason="tool_calls",
    )))

    assert transition.outcomes[0].executed
    rejected = transition.outcomes[1]
    assert rejected.error_kind == "capability_disabled" and not rejected.executed
    assert "Unknown tool 'edit'" in rejected.feedback
    assert "This tool call was not executed" in rejected.feedback
    assert "No tool was executed" not in rejected.feedback
    assert "file_edit" in rejected.feedback and "lean_retrieve" not in rejected.feedback
    assert episode.result().tool_calls == 1
    assert "sorry" in episode.tools.workspace.submission_file.read_text()


@pytest.mark.parametrize("first_calls", [
    {"bad": "container"},
    [native_call("file_read", {"file_path": "Submission.lean"}, "good"), {"id": "missing-name", "function": {}}],
])
def test_standard_chat_response_recovers_with_valid_history(monkeypatch, tmp_path, first_calls):
    requests = []

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return httpx.Response(200, json={"choices": [{"message": {
                "role": "assistant", "content": "", "tool_calls": deepcopy(first_calls),
            }, "finish_reason": "stop"}]})
        assistant_ids = []
        result_ids = []
        for message in payload["messages"]:
            if message.get("tool_calls") is not None:
                assert isinstance(message["tool_calls"], list)
                for call in message["tool_calls"]:
                    assert isinstance(call["function"]["name"], str)
                    assert isinstance(json.loads(call["function"]["arguments"]), dict)
                    assistant_ids.append(call["id"])
            if message["role"] == "tool":
                result_ids.append(message["tool_call_id"])
        assert sorted(assistant_ids) == sorted(result_ids)
        text = json.dumps(payload["messages"])
        assert "must be an array" in text or "function.name must be a non-empty string" in text
        return httpx.Response(200, json={"choices": [{"message": {
            "role": "assistant", "content": "",
            "tool_calls": [native_call("file_read", {"file_path": "Submission.lean"}, "recovered")],
        }, "finish_reason": "tool_calls"}]})

    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(llm, "AsyncClient", lambda **kw: original_client(transport=transport, **kw))
    episode = episode_at(tmp_path)
    client = llm.LiteLLMClient(model="test", api_key=None, base_url="https://provider.invalid/v1", max_retries=0)
    result = asyncio.run(ProverDriver(client, episode, "example : True := by sorry", verbose=False).run_rollout())

    assert len(requests) == 2 and result.reason == "max_turns"
    expected_calls = 2 if isinstance(first_calls, list) else 1
    assert episode.tools.call.await_count == expected_calls


def test_normal_history_retains_provider_reasoning_signatures():
    wire = {
        "role": "assistant", "content": "", "reasoning_content": "Inspect the file.",
        "thinking_blocks": [{"type": "thinking", "thinking": "Inspect the file.", "signature": "signed-block"}],
        "provider_specific_fields": {"signature": "provider-signature"},
        "tool_calls": [native_call("file_read", '{"file_path":"Submission.lean"}', "signed-call")],
    }
    history = _assistant_history_message(parse_assistant_action(wire, turn=1))
    assert history == wire
    history["thinking_blocks"][0]["signature"] = "changed"
    assert wire["thinking_blocks"][0]["signature"] == "signed-block"
