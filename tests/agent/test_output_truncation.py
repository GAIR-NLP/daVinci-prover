"""Output limits permit bounded continuation without hiding tool effects."""

import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dvp.agent.driver import ProverDriver
from dvp.artifacts.conversation import build_conversation_artifact
from dvp.artifacts.trajectory import NullTrajectorySink
from dvp.config import BudgetConfig, EpisodeConfig
from dvp.orchestration.attempts import _result_is_infrastructure
from test_format_error_limit import (
    CANDIDATE, STATEMENT, action, make_episode, malformed_action, native_call,
)


def scripted_client(responses):
    requests = []
    client = SimpleNamespace(generation_records=[])

    async def chat(**kwargs):
        requests.append(deepcopy(kwargs))
        response, finish_reason = responses[len(requests) - 1]
        client.generation_records.append({"finish_reason": finish_reason})
        return deepcopy(response)

    client.chat = AsyncMock(side_effect=chat)
    return client, requests


def assert_truncation_failure(result):
    assert result.reason == "repeated_output_truncation"
    assert result.truncated and not result.terminated and not result.aborted
    assert not result.success and result.score == 0.0
    assert not result.retryable_service_error
    assert not _result_is_infrastructure(result.to_dict())


@pytest.mark.parametrize("fields", [
    {"reasoning_content": "unfinished derivation"},
    {"content": "unfinished explanation"},
    {},
])
def test_length_without_tools_uses_independent_bounded_counter(tmp_path, fields):
    episode, verifier, comparator, _ = make_episode(tmp_path, config=EpisodeConfig(
        max_consecutive_format_errors=1, max_consecutive_output_truncations=2,
    ))

    async def run():
        for expected in (1, 2):
            transition = await episode.step(action(finish_reason="length", **fields))
            assert transition.outcomes[0].error_kind == "output_truncated"
            assert not transition.outcomes[0].executed
            assert transition.next_observation.state["consecutive_format_errors"] == 0
            assert transition.next_observation.state["consecutive_output_truncations"] == expected
            assert transition.truncated is (expected == 2)
        return episode.result()

    result = asyncio.run(run())
    assert_truncation_failure(result)
    assert result.turns == 2 and result.tool_calls == result.lean_calls == 0
    verifier.verify_code.assert_not_awaited()
    verifier.verify_file.assert_not_awaited()
    comparator.verify.assert_not_awaited()


@pytest.mark.parametrize("limit,max_turns", [(0, 7), (5, 2)])
def test_disabled_or_larger_truncation_limit_still_obeys_global_turn_budget(tmp_path, limit, max_turns):
    episode, _, _, _ = make_episode(tmp_path, config=EpisodeConfig(
        max_consecutive_format_errors=1, max_consecutive_output_truncations=limit,
        budget=BudgetConfig(max_turns=max_turns),
    ))

    async def run():
        for _ in range(max_turns):
            await episode.step(action(finish_reason="length", reasoning_content="partial"))
        return episode.result()

    result = asyncio.run(run())
    assert result.reason == "max_turns" and result.truncated
    assert not result.terminated and not result.aborted
    assert episode.observe().state["consecutive_output_truncations"] == max_turns
    assert episode.observe().state["consecutive_format_errors"] == 0


def test_truncation_does_not_clear_format_errors_and_nontruncation_clears_truncations(tmp_path):
    episode, _, _, _ = make_episode(tmp_path, config=EpisodeConfig(
        max_consecutive_format_errors=2,
    ))

    async def run():
        first = await episode.step(malformed_action())
        assert first.next_observation.state["consecutive_format_errors"] == 1
        partial = await episode.step(action(finish_reason="length", reasoning_content="partial"))
        assert partial.next_observation.state["consecutive_format_errors"] == 1
        assert partial.next_observation.state["consecutive_output_truncations"] == 1
        last = await episode.step(malformed_action())
        assert last.next_observation.state["consecutive_output_truncations"] == 0
        assert last.next_observation.state["consecutive_format_errors"] == 2
        return episode.result()

    result = asyncio.run(run())
    assert result.reason == "repeated_format_error" and result.terminated
    assert not result.truncated


def test_length_with_valid_calls_executes_and_resets_both_counters(tmp_path):
    episode, _, _, _ = make_episode(tmp_path)

    async def run():
        await episode.step(malformed_action())
        await episode.step(action(finish_reason="length"))
        return await episode.step(action(
            native_call("file_read", {"file_path": "Submission.lean"}),
            finish_reason="length",
        ))

    transition = asyncio.run(run())
    assert transition.outcomes[0].executed and transition.outcomes[0].valid
    assert transition.next_observation.state["consecutive_format_errors"] == 0
    assert transition.next_observation.state["consecutive_output_truncations"] == 0
    assert not transition.terminated and not transition.truncated


@pytest.mark.parametrize("kind", ["invalid_arguments", "unknown_tool"])
def test_length_does_not_relabel_complete_invalid_calls_as_truncations(tmp_path, kind):
    episode, _, _, _ = make_episode(tmp_path, config=EpisodeConfig(
        max_consecutive_format_errors=1,
    ))
    if kind == "unknown_tool":
        episode.tools._tools["file_read"].handler = AsyncMock(return_value={
            "success": False, "error": "Unknown tool: file_read",
        })
        call = native_call("file_read", {"file_path": "Submission/unavailable.txt"})
    else:
        call = native_call("file_write", {"file_path": "Submission/note.txt"})
    transition = asyncio.run(episode.step(action(call, finish_reason="length")))
    assert transition.outcomes[0].error_kind == kind
    assert transition.reason == "repeated_format_error"
    assert transition.next_observation.state["consecutive_output_truncations"] == 0


@pytest.mark.parametrize("arguments", ["[]", "null", "1", '"x"'])
def test_length_with_complete_nonobject_json_remains_a_format_error(tmp_path, arguments):
    episode, _, _, _ = make_episode(tmp_path, config=EpisodeConfig(
        max_consecutive_format_errors=1,
    ))
    episode.tools.call = AsyncMock(wraps=episode.tools.call)
    transition = asyncio.run(episode.step(action(
        native_call("file_read", arguments), finish_reason="length",
    )))
    assert transition.outcomes[0].error_kind == "tool_parse_error"
    assert not transition.outcomes[0].executed
    assert transition.next_observation.state["consecutive_format_errors"] == 1
    assert transition.next_observation.state["consecutive_output_truncations"] == 0
    assert transition.reason == "repeated_format_error"
    assert transition.terminated and not transition.truncated
    episode.tools.call.assert_not_awaited()


@pytest.mark.parametrize("name", ["not_available", "bash_execute", "lean_retrieve"])
def test_length_with_broken_json_cannot_exempt_unavailable_tools_from_format_limit(tmp_path, name):
    episode, _, _, _ = make_episode(tmp_path, config=EpisodeConfig(
        max_consecutive_format_errors=1, enable_lean_retrieve=False,
    ))
    episode.tools.call = AsyncMock(wraps=episode.tools.call)
    transition = asyncio.run(episode.step(action(
        native_call(name, "{"), finish_reason="length",
    )))
    assert transition.outcomes[0].error_kind in {
        "tool_parse_error", "capability_disabled", "unknown_tool",
    }
    assert not transition.outcomes[0].executed
    assert transition.next_observation.state["consecutive_format_errors"] == 1
    assert transition.next_observation.state["consecutive_output_truncations"] == 0
    assert transition.reason == "repeated_format_error"
    assert transition.terminated and not transition.truncated
    episode.tools.call.assert_not_awaited()


def test_mixed_truncation_and_real_format_errors_count_each_once(tmp_path):
    episode, _, _, _ = make_episode(tmp_path, config=EpisodeConfig(
        max_consecutive_format_errors=2,
    ))
    transition = asyncio.run(episode.step(action(
        native_call("file_read", "{", "cut-one"),
        native_call("file_read", '{"file_path":', "cut-two"),
        native_call("file_write", {"file_path": "Submission/note.txt"}, "bad-schema"),
        finish_reason="length",
    )))
    assert [item.error_kind for item in transition.outcomes] == [
        "output_truncated", "output_truncated", "invalid_arguments",
    ]
    assert transition.next_observation.state["consecutive_output_truncations"] == 1
    assert transition.next_observation.state["consecutive_format_errors"] == 1
    assert not transition.truncated and not transition.terminated


@pytest.mark.parametrize("retain", [True, False])
def test_bad_json_is_not_executed_and_only_safe_text_can_reenter_context(tmp_path, retain):
    episode, _, _, _ = make_episode(tmp_path, config=EpisodeConfig(
        retain_truncated_thinking=retain, max_consecutive_output_truncations=1,
        max_consecutive_format_errors=1,
    ))
    episode.sink = NullTrajectorySink()
    episode.tools.call = AsyncMock(wraps=episode.tools.call)
    response = {
        "role": "assistant", "content": "partial explanation",
        "reasoning_content": "partial reasoning",
        "tool_calls": [native_call("file_write", '{"file_path":', "cut")],
    }
    client, _ = scripted_client([(response, "length")])
    driver = ProverDriver(client, episode, STATEMENT, verbose=False)
    result = asyncio.run(driver.run_rollout())
    assert_truncation_failure(result)
    episode.tools.call.assert_not_awaited()
    assert not any(message.get("tool_calls") for message in driver.messages)
    assert not any(message["role"] == "tool" for message in driver.messages)
    assistant = [message for message in driver.messages if message["role"] == "assistant"]
    assert len(assistant) == 1
    assert assistant[0]["content"] == response["content"]
    if retain:
        assert assistant[0]["reasoning_content"] == response["reasoning_content"]
    else:
        assert "reasoning_content" not in assistant[0]
        assert "partial reasoning" not in json.dumps(driver.messages)
    assert driver.messages[-1]["role"] == "user"
    assert "truncat" in driver.messages[-1]["content"].lower() or "token limit" in driver.messages[-1]["content"].lower()
    saved = build_conversation_artifact(
        driver=driver, episode=episode, client=client, result=result.to_dict(),
    )
    assert any(item["response"] == {**response, "finish_reason": "length"}
               for item in saved["rejected_responses"])


def test_empty_truncated_response_is_never_sent_as_an_empty_assistant_message(tmp_path):
    episode, _, _, _ = make_episode(tmp_path, config=EpisodeConfig(
        max_consecutive_format_errors=1, budget=BudgetConfig(max_turns=2),
    ))
    client, requests = scripted_client([
        ({"role": "assistant", "content": "", "reasoning_content": ""}, "length"),
        ({"role": "assistant", "tool_calls": [
            native_call("file_read", {"file_path": "Submission.lean"}),
        ]}, "tool_calls"),
    ])
    result = asyncio.run(ProverDriver(client, episode, STATEMENT, verbose=False).run_rollout())
    assert result.reason == "max_turns" and result.tool_calls == 1
    assert not any(message["role"] == "assistant" for message in requests[1]["messages"])
    assert len(requests[1]["messages"]) > len(requests[0]["messages"])


@pytest.mark.parametrize("retain", [None, True, False])
def test_next_request_drops_truncated_thinking_by_default_and_keeps_tool_thinking(tmp_path, retain):
    retention = {} if retain is None else {"retain_truncated_thinking": retain}
    episode, _, _, _ = make_episode(tmp_path, config=EpisodeConfig(
        budget=BudgetConfig(max_turns=3), **retention,
    ))
    client, requests = scripted_client([
        ({"role": "assistant", "content": "", "reasoning_content": "partial derivation"}, "length"),
        ({"role": "assistant", "content": "", "reasoning_content": "inspect the saved candidate",
          "tool_calls": [native_call("file_read", {"file_path": "Submission.lean"}, "first-read")]},
         "tool_calls"),
        ({"role": "assistant", "content": "", "tool_calls": [
            native_call("file_read", {"file_path": "Submission.lean"}, "second-read"),
        ]}, "tool_calls"),
    ])
    driver = ProverDriver(client, episode, STATEMENT, verbose=False)
    result = asyncio.run(driver.run_rollout())

    assert result.reason == "max_turns" and result.tool_calls == 2
    partial = [message for message in requests[1]["messages"]
               if message.get("reasoning_content") == "partial derivation"]
    assert bool(partial) is (retain is True)
    assert all(message["role"] == "assistant" and not message.get("tool_calls") for message in partial)
    assert any(message.get("reasoning_content") == "inspect the saved candidate"
               and message.get("tool_calls") for message in requests[2]["messages"])
    if retain is not True:
        assert driver.rejected_responses[0]["response"]["reasoning_content"] == "partial derivation"
    else:
        assert driver.rejected_responses == []


@pytest.mark.parametrize("retain", [False, True])
@pytest.mark.parametrize("degenerate", [False, True])
def test_truncated_mixed_batch_reports_executed_effects_and_arguments_without_bad_calls(tmp_path, retain, degenerate):
    episode, _, _, _ = make_episode(tmp_path, config=EpisodeConfig(
        max_consecutive_format_errors=1, budget=BudgetConfig(max_turns=2),
        retain_truncated_thinking=retain,
    ))
    episode.tools.call = AsyncMock(wraps=episode.tools.call)
    reasoning = "A putative prime 1" + "0" * 82_775 if degenerate else "safe partial reasoning"
    response = {
        "role": "assistant", "content": "", "reasoning_content": reasoning,
        "tool_calls": [
            native_call("file_write", {"file_path": "Submission/note.txt", "content": "written once"}, "written"),
            native_call("file_read", '{"file_path":', "cut"),
        ],
    }
    client, requests = scripted_client([
        (response, "length"),
        ({"role": "assistant", "tool_calls": [
            native_call("file_read", {"file_path": "Submission/note.txt"}, "read"),
        ]}, "tool_calls"),
    ])
    driver = ProverDriver(client, episode, STATEMENT, verbose=False)
    result = asyncio.run(driver.run_rollout())
    assert result.reason == "max_turns" and result.tool_calls == 2
    assert [call.args[0] for call in episode.tools.call.await_args_list] == ["file_write", "file_read"]
    assert (episode.tools.workspace.submission_dir / "note.txt").read_text() == "written once"
    messages = requests[1]["messages"]
    assert not any(message.get("tool_calls") or message["role"] == "tool" for message in messages)
    assert any(message.get("reasoning_content") == reasoning for message in messages) is (retain and not degenerate)
    assert driver.rejected_responses[0]["response"] == {**response, "finish_reason": "length"}
    feedback = next(message["content"] for message in messages if "TOOL_RESULTS " in message.get("content", ""))
    results, _ = json.JSONDecoder().raw_decode(feedback.split("TOOL_RESULTS ", 1)[1])
    assert results[0]["tool_call_id"] == "written" and results[0]["executed"]
    assert results[0]["arguments"] == {"file_path": "Submission/note.txt", "content": "written once"}
    assert results[1]["tool_call_id"] == "cut" and not results[1]["executed"]
    assert results[1]["error_kind"] == "output_truncated"


@pytest.mark.parametrize("verdict", ["accepted", "rejected", "infrastructure"])
def test_truncation_budget_exit_grades_saved_source_after_whole_batch(tmp_path, verdict):
    episode, verifier, comparator, _ = make_episode(tmp_path, config=EpisodeConfig(
        max_consecutive_format_errors=1, max_consecutive_output_truncations=1,
        budget=BudgetConfig(max_turns=1),
    ))
    verifier.verify_code.return_value = {"success": True, "errors": []}
    if verdict == "rejected":
        comparator.verify.return_value = {"success": False, "errors": ["modified target"]}
    elif verdict == "infrastructure":
        comparator.verify.side_effect = RuntimeError("Comparator unavailable")
    episode.reset()
    episode.tools.workspace.submission_file.write_text(CANDIDATE)
    transition = asyncio.run(episode.step(action(
        native_call("file_read", "{", "cut"),
        native_call("lean_verify", {"code": CANDIDATE}, "verify"),
        native_call("file_write", {"file_path": "Submission/after.txt", "content": "must not run"}, "after"),
        finish_reason="length",
    )))
    comparator.verify.assert_not_awaited()
    assert transition.reason == "repeated_output_truncation"
    result = asyncio.run(episode.finalize())
    assert result.reason == {
        "accepted": "verified_exact_target", "rejected": "comparator_rejected",
        "infrastructure": "comparator_infrastructure_error",
    }[verdict]
    assert result.success is (verdict == "accepted")
    assert not result.truncated
    assert transition.outcomes[0].error_kind == "output_truncated"
    assert transition.outcomes[-1].executed
    assert (episode.tools.workspace.submission_dir / "after.txt").exists()
    comparator.verify.assert_awaited_once()


@pytest.mark.parametrize("finish", ["tool_calls", "length"])
def test_history_reaches_next_request_after_four_recoverable_replies(tmp_path, finish):
    episode, verifier, comparator, _ = make_episode(tmp_path, config=EpisodeConfig(
        retain_truncated_thinking=False,
        max_consecutive_format_errors=0, max_consecutive_output_truncations=0,
        budget=BudgetConfig(max_turns=5),
    ))
    replies = [({"role": "assistant", "content": f"partial {i}",
                 "reasoning_content": f"derivation {i}"}, finish) for i in range(4)]
    replies.append(({"role": "assistant", "content": "", "tool_calls": [
        native_call("file_read", {"file_path": "Submission.lean"}),
    ]}, "tool_calls"))
    client, requests = scripted_client(replies)
    driver = ProverDriver(client, episode, STATEMENT, verbose=False)
    result = asyncio.run(driver.run_rollout())
    assert result.reason == "max_turns" and result.turns == 5 and result.tool_calls == 1
    history = [m for m in requests[4]["messages"] if m["role"] == "assistant"]
    if finish == "length":
        assert history == [{"role": "assistant", "content": f"partial {i}"} for i in range(4)]
        assert len(driver.rejected_responses) == 4
        assert [item["response"]["reasoning_content"] for item in driver.rejected_responses] == [
            f"derivation {i}" for i in range(4)
        ]
    else:
        assert history == [] and len(driver.rejected_responses) == 4
    verifier.verify_file.assert_not_awaited()
    comparator.verify.assert_not_awaited()
