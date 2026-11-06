"""Bound malformed action retries without treating proof failures as formatting."""

import asyncio
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dvp.agent.driver import ProverDriver
from dvp.agent.episode import ProverEpisode
from dvp.artifacts.conversation import build_conversation_artifact
from dvp.artifacts.trajectory import MemoryTrajectorySink, NullTrajectorySink
from dvp.config import BudgetConfig, EpisodeConfig
from dvp.llm_clients.parser import parse_assistant_action
from dvp.orchestration.attempts import _result_is_infrastructure
from dvp.protocol import AssistantAction
from dvp.runtime.workspace import Workspace
from dvp.toolkits.registry import ToolRegistry


STATEMENT = "theorem target : True := by sorry\n"
CANDIDATE = STATEMENT.replace("sorry", "trivial")


def native_call(name, arguments, call_id="call"):
    return {
        "id": call_id, "type": "function",
        "function": {
            "name": name,
            "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments),
        },
    }


def action(*calls, **raw):
    return parse_assistant_action(
        {"role": "assistant", "content": "", "tool_calls": list(calls), **raw}, turn=1,
    )


def malformed_action():
    return action(native_call("file_read", '{"file_path":'))


def accepted():
    return {
        "success": True, "errors": [], "backend": "leanprover/comparator",
        "candidate_sha256": hashlib.sha256(CANDIDATE.encode()).hexdigest(),
        "original_source_sha256": hashlib.sha256(STATEMENT.encode()).hexdigest(),
    }


def make_episode(tmp_path, *, config=None):
    workspace = Workspace(root=tmp_path / "run")
    workspace.setup()
    verifier = SimpleNamespace(
        config=SimpleNamespace(target_workspace=None, timeout=1),
        verify_code=AsyncMock(return_value={"success": False, "errors": ["unsolved goals"]}),
        verify_file=AsyncMock(return_value={"success": False, "errors": ["unsolved goals"]}),
    )
    tools = ToolRegistry(workspace=workspace, verifier=verifier)
    comparator = SimpleNamespace(verify=AsyncMock(return_value=accepted()))

    async def bound_verdict(code, *, bundle, **kwargs):
        result = comparator.verify.return_value
        if isinstance(result, dict) and result.get("success") is True:
            return {**result, "source_bundle_sha256": bundle.sha256,
                    "source_bundle_manifest": bundle.manifest()}
        return result

    comparator.verify.side_effect = bound_verdict

    sink = MemoryTrajectorySink()
    episode = ProverEpisode(
        tools=tools, contract_verifier=comparator, theorem_statement=STATEMENT,
        run_dir=workspace.root, config=config or EpisodeConfig(), sink=sink,
    )
    return episode, verifier, comparator, sink


def assert_format_failure(result):
    assert result.reason == "repeated_format_error"
    assert result.terminated and not result.truncated and not result.aborted
    assert not result.success and result.score == 0.0
    assert not result.retryable_service_error
    assert not _result_is_infrastructure(result.to_dict())


def test_driver_stops_after_three_malformed_tool_responses(tmp_path):
    episode, verifier, comparator, _ = make_episode(tmp_path)
    response = {
        "role": "assistant", "content": "I will inspect the proof.",
        "tool_calls": [native_call("file_read", '{"file_path":')],
    }
    client = SimpleNamespace(chat=AsyncMock(side_effect=[
        deepcopy(response), deepcopy(response), deepcopy(response),
        AssertionError("must not request a fourth malformed response"),
    ]))
    result = asyncio.run(ProverDriver(client, episode, STATEMENT, verbose=False).run_rollout())
    assert_format_failure(result)
    assert result.turns == client.chat.await_count == 3
    assert result.tool_calls == result.lean_calls == 0
    assert episode.observe().state["consecutive_format_errors"] == 3
    verifier.verify_code.assert_not_awaited()
    verifier.verify_file.assert_not_awaited()
    comparator.verify.assert_not_awaited()
    with pytest.raises(RuntimeError, match="episode already finished"):
        asyncio.run(episode.step(AssistantAction()))


@pytest.mark.parametrize("value", [-1, True, False, 1.5, "3", None])
def test_format_limit_rejects_invalid_configuration(value):
    with pytest.raises((TypeError, ValueError), match="max_consecutive_format_errors"):
        EpisodeConfig(max_consecutive_format_errors=value)


def test_default_and_custom_limits_are_part_of_configuration_identity():
    default = EpisodeConfig()
    assert default.max_consecutive_format_errors == 3
    assert len({
        default.config_id,
        EpisodeConfig(max_consecutive_format_errors=0).config_id,
        EpisodeConfig(max_consecutive_format_errors=1).config_id,
    }) == 3


def test_custom_one_error_limit_wins_over_same_turn_budget(tmp_path):
    episode, _, _, _ = make_episode(tmp_path, config=EpisodeConfig(
        budget=BudgetConfig(max_turns=1), max_consecutive_format_errors=1,
    ))
    transition = asyncio.run(episode.step(malformed_action()))
    assert_format_failure(episode.result())
    assert transition.reason == "repeated_format_error"
    assert transition.reward == 0.0
    assert transition.next_observation.state["consecutive_format_errors"] == 1


def test_zero_disables_format_termination_but_keeps_counter_and_turn_budget(tmp_path):
    episode, _, _, _ = make_episode(tmp_path, config=EpisodeConfig(
        budget=BudgetConfig(max_turns=5), max_consecutive_format_errors=0,
    ))

    async def run():
        for expected in range(1, 6):
            transition = await episode.step(malformed_action())
            assert transition.next_observation.state["consecutive_format_errors"] == expected
            assert not transition.terminated and not transition.aborted
        return episode.result()

    result = asyncio.run(run())
    assert result.reason == "max_turns" and result.truncated
    assert result.turns == 5 and result.score == 0.0


@pytest.mark.parametrize("kind", [
    "response_format_error", "tool_parse_error", "capability_disabled", "unknown_tool", "invalid_arguments",
])
def test_each_format_error_kind_counts_towards_the_limit(tmp_path, kind):
    episode, _, _, _ = make_episode(
        tmp_path, config=EpisodeConfig(max_consecutive_format_errors=1),
    )
    choices = {
        "response_format_error": action(finish_reason="unexpected"),
        "tool_parse_error": action(native_call("file_read", '{"file_path":')),
        "capability_disabled": action(native_call("bash_execute", {})),
        "unknown_tool": action(native_call("file_read", {"file_path": "Submission/unavailable.txt"})),
        "invalid_arguments": action(native_call("file_write", {"file_path": "Submission/note.txt"})),
    }
    if kind == "unknown_tool":
        episode.tools._tools["file_read"].handler = AsyncMock(return_value={
            "success": False, "error": "Unknown tool: file_read",
        })
    transition = asyncio.run(episode.step(choices[kind]))
    assert transition.outcomes[0].error_kind == kind
    assert transition.next_observation.state["consecutive_format_errors"] == 1
    assert_format_failure(episode.result())


@pytest.mark.parametrize("recovery", ["read", "lean_failure", "missing_file"])
def test_normal_step_resets_streak_including_valid_execution_failures(tmp_path, recovery):
    episode, verifier, comparator, _ = make_episode(tmp_path)
    choices = {
        "read": action(native_call("file_read", {"file_path": "Submission.lean"})),
        "lean_failure": action(native_call("lean_verify", {"code": CANDIDATE})),
        "missing_file": action(native_call("file_read", {"file_path": "Submission/missing.txt"})),
    }

    async def run():
        assert episode.reset().state["consecutive_format_errors"] == 0
        await episode.step(malformed_action())
        await episode.step(malformed_action())
        transition = await episode.step(choices[recovery])
        assert transition.outcomes[0].executed
        assert transition.next_observation.state["consecutive_format_errors"] == 0
        if recovery != "read":
            assert not transition.outcomes[0].valid and transition.reward == 0.0
        for expected in range(1, 4):
            transition = await episode.step(malformed_action())
            assert transition.next_observation.state["consecutive_format_errors"] == expected
            assert transition.terminated is (expected == 3)
        return episode.result()

    result = asyncio.run(run())
    assert_format_failure(result)
    assert result.turns == 6 and result.tool_calls == 1
    assert verifier.verify_code.await_count == int(recovery == "lean_failure")
    comparator.verify.assert_not_awaited()


def test_multiple_errors_in_mixed_batch_count_once_and_preserve_call_order(tmp_path):
    episode, _, _, _ = make_episode(
        tmp_path, config=EpisodeConfig(max_consecutive_format_errors=2),
    )
    episode.tools.call = AsyncMock(wraps=episode.tools.call)
    mixed = action(
        native_call("file_write", {"file_path": "Submission/note.txt", "content": "written"}, "write"),
        native_call("file_read", "{", "bad-json"),
        native_call("bash_execute", {}, "disabled"),
        native_call("file_read", {"file_path": "Submission/note.txt"}, "read"),
    )

    async def run():
        first = await episode.step(mixed)
        assert first.next_observation.state["consecutive_format_errors"] == 1
        assert not first.terminated
        assert [outcome.call_id for outcome in first.outcomes] == ["write", "bad-json", "disabled", "read"]
        assert first.outcomes[0].valid and first.outcomes[3].valid
        assert [call.args[0] for call in episode.tools.call.await_args_list] == ["file_write", "file_read"]
        assert (episode.tools.workspace.submission_dir / "note.txt").read_text() == "written"
        second = await episode.step(mixed)
        assert second.next_observation.state["consecutive_format_errors"] == 2
        return episode.result()

    result = asyncio.run(run())
    assert_format_failure(result)
    assert result.turns == 2 and result.tool_calls == 4


@pytest.mark.parametrize("verdict", ["accepted", "rejected", "comparator_infrastructure", "tool_infrastructure"])
def test_existing_terminal_outcome_wins_over_format_error_in_same_batch(tmp_path, verdict):
    episode, verifier, comparator, _ = make_episode(tmp_path, config=EpisodeConfig(
        budget=BudgetConfig(max_turns=3),
    ))
    verifier.verify_code.return_value = {"success": True, "errors": []}
    if verdict == "rejected":
        comparator.verify.return_value = {"success": False, "errors": ["modified original definition"]}
    elif verdict == "comparator_infrastructure":
        comparator.verify.side_effect = RuntimeError("Comparator service unavailable")
    elif verdict == "tool_infrastructure":
        verifier.verify_code.return_value = {
            "success": False, "_harness_infrastructure_error": True,
            "error": "Verifier service unavailable",
        }

    episode.reset()
    episode.tools.workspace.submission_file.write_text(CANDIDATE)

    async def run():
        await episode.step(malformed_action())
        await episode.step(malformed_action())
        return await episode.step(action(
            native_call("file_read", "{", "bad-json"),
            native_call("lean_verify", {"code": CANDIDATE}, "verify"),
            native_call("file_write", {"file_path": "Submission/after.txt", "content": "must not run"}, "after"),
        ))

    transition = asyncio.run(run())
    comparator.verify.assert_not_awaited()
    result = asyncio.run(episode.finalize())
    expected = {
        "accepted": "verified_exact_target",
        "rejected": "comparator_rejected",
        "comparator_infrastructure": "comparator_infrastructure_error",
        "tool_infrastructure": "infrastructure_error",
    }[verdict]
    assert result.reason == expected
    assert transition.reason == ("infrastructure_error" if verdict == "tool_infrastructure" else "repeated_format_error")
    assert not result.truncated
    assert result.success is (verdict == "accepted")
    assert result.score == float(verdict == "accepted")
    assert result.aborted is verdict.endswith("infrastructure")
    assert (episode.tools.workspace.submission_dir / "after.txt").exists() is (verdict != "tool_infrastructure")
    assert comparator.verify.await_count == int(verdict != "tool_infrastructure")


@pytest.mark.parametrize("has_incomplete_call", [False, True])
@pytest.mark.parametrize("retain", [True, False])
def test_length_keeps_unfinished_thinking_but_discards_malformed_calls(tmp_path, has_incomplete_call, retain):
    episode, _, _, sink = make_episode(tmp_path, config=EpisodeConfig(
        retain_truncated_thinking=retain, max_consecutive_output_truncations=3,
        max_consecutive_format_errors=1,
    ))
    discarded = not retain
    response = {"role": "assistant", "content": "", "reasoning_content": "unfinished reasoning"}
    if has_incomplete_call:
        response["tool_calls"] = [native_call("file_read", '{"file_path":', "incomplete")]
    original = deepcopy(response)
    requests = []
    client = SimpleNamespace(generation_records=[])

    async def chat(**kwargs):
        requests.append(deepcopy(kwargs))
        assert len(requests) <= 3, "must stop at the independent truncation budget"
        client.generation_records.append({"finish_reason": "length"})
        return deepcopy(response)

    client.chat = AsyncMock(side_effect=chat)
    driver = ProverDriver(client, episode, STATEMENT, verbose=False)
    result = asyncio.run(driver.run_rollout())
    assert result.reason == "repeated_output_truncation"
    assert result.truncated and not result.terminated and not result.aborted
    assert not result.success and not result.retryable_service_error
    assert not _result_is_infrastructure(result.to_dict())
    assert episode.observe().state["consecutive_format_errors"] == 0
    feedback = "\n".join(
        message.get("content") or ""
        for message in requests[1]["messages"] if message["role"] == "user"
    ).lower()
    assert "tool" in feedback
    assert "truncat" in feedback or "token limit" in feedback
    assert "smaller steps" in feedback
    for request in requests:
        for message in request["messages"]:
            assert "finish_reason" not in message and "generation_records" not in message
            assert message["role"] != "tool"
            assert not message.get("tool_calls")
            if discarded:
                assert message["role"] != "assistant"
        if discarded:
            assert "unfinished reasoning" not in json.dumps(request)
    initial_length = len(requests[0]["messages"])
    increment = 1 if discarded else 2
    assert [len(r["messages"]) for r in requests] == [initial_length + increment * i for i in range(3)]
    assert len(driver.messages) == initial_length + increment * 3
    if discarded:
        assert [r["turn"] for r in driver.rejected_responses] == [1, 2, 3]
        assert all(r["response"] == {**original, "finish_reason": "length"}
                   for r in driver.rejected_responses)
    else:
        safe_response = {key: value for key, value in original.items() if key != "tool_calls"}
        assert requests[1]["messages"][-2] == safe_response
        assert [m for m in requests[2]["messages"] if m["role"] == "assistant"] == [safe_response] * 2
        saved = build_conversation_artifact(
            driver=driver, episode=episode, client=client, result=result.to_dict(),
        )
        assert [m for m in saved["messages"] if m["role"] == "assistant"] == [safe_response] * 3
    transitions = [entry["payload"] for entry in sink.events if entry["event"] == "transition"]
    assert transitions[0]["action"]["raw"]["finish_reason"] == "length"
    assert response == original


@pytest.mark.parametrize("retain", [False, True])
def test_driver_recovers_after_three_truncated_thinking_turns_with_selected_history(tmp_path, retain):
    episode, _, _, sink = make_episode(tmp_path, config=EpisodeConfig(
        budget=BudgetConfig(max_turns=4),
        max_consecutive_format_errors=1, retain_truncated_thinking=retain,
    ))
    responses = [
        {"role": "assistant", "content": "", "reasoning_content": "first partial derivation"},
        {"role": "assistant", "content": "", "reasoning_content": "second partial derivation"},
        {"role": "assistant", "content": "", "reasoning_content": "third partial derivation"},
        {"role": "assistant", "content": "", "tool_calls": [
            native_call("file_read", {"file_path": "Submission.lean"}, "recovered"),
        ]},
    ]
    client = SimpleNamespace(generation_records=[])
    requests = []

    async def chat(**kwargs):
        requests.append(deepcopy(kwargs))
        index = len(requests) - 1
        if index:
            assert [m for m in kwargs["messages"] if m["role"] == "assistant"] == (
                responses[:index] if retain else []
            )
        client.generation_records.append({"finish_reason": "length" if index < 3 else "tool_calls"})
        return deepcopy(responses[index])

    client.chat = AsyncMock(side_effect=chat)
    driver = ProverDriver(client, episode, STATEMENT, verbose=False)
    result = asyncio.run(driver.run_rollout())
    assert result.reason == "max_turns" and result.turns == 4
    assert result.tool_calls == 1 and result.lean_calls == 0
    assert episode.observe().state["consecutive_format_errors"] == 0
    assert episode.observe().state["consecutive_output_truncations"] == 0
    if retain:
        assert driver.rejected_responses == []
    else:
        assert [item["response"] for item in driver.rejected_responses] == [
            {**item, "finish_reason": "length"} for item in responses[:3]
        ]
    transitions = [item["payload"] for item in sink.events if item["event"] == "transition"]
    assert [item["action"]["raw"] for item in transitions[:3]] == [
        {**item, "finish_reason": "length"} for item in responses[:3]
    ]
    assert [m for m in driver.messages if m["role"] == "tool"][0]["tool_call_id"] == "recovered"


@pytest.mark.parametrize("kind", [
    "response_format_error", "tool_parse_error", "capability_disabled", "invalid_arguments", "synthetic",
])
def test_format_recovery_drops_rejected_thinking_but_keeps_lean_failure_and_sft_log(tmp_path, kind):
    episode, _, comparator, _ = make_episode(tmp_path, config=EpisodeConfig(
        budget=BudgetConfig(max_turns=4),
    ))
    # SFT collection has no full trajectory; raw rejected replies must still survive.
    episode.sink = NullTrajectorySink()
    rejected = {"role": "assistant", "content": "discarded answer", "reasoning_content": "discarded thinking"}
    calls = {
        "tool_parse_error": native_call("file_read", "{"),
        "capability_disabled": native_call("bash_execute", {}),
        "invalid_arguments": native_call("file_write", {"file_path": "Submission/note.txt"}),
    }
    if kind in calls:
        rejected["tool_calls"] = [calls[kind]]
    elif kind == "synthetic":
        rejected["content"] = json.dumps({"name": "bash_execute", "arguments": {}})
    elif kind == "response_format_error":
        rejected["finish_reason"] = "unexpected"
    normal = {
        "role": "assistant", "content": "valid attempt", "reasoning_content": "valid thinking",
        "tool_calls": [native_call("lean_verify", {"code": CANDIDATE}, "verify")],
    }
    final = {"role": "assistant", "content": "", "tool_calls": [
        native_call("file_read", {"file_path": "Submission.lean"}, "read"),
    ]}
    responses = [rejected, normal, rejected, final]
    requests = []

    async def chat(**kwargs):
        requests.append(deepcopy(kwargs))
        return deepcopy(responses[len(requests) - 1])

    client = SimpleNamespace(chat=AsyncMock(side_effect=chat))
    driver = ProverDriver(client, episode, STATEMENT, verbose=False)
    result = asyncio.run(driver.run_rollout())
    assert result.reason == "max_turns" and result.turns == 4
    assert episode.observe().state["consecutive_format_errors"] == 0
    assert comparator.verify.await_count == 0
    assert not any(m["role"] in {"assistant", "tool"} for m in requests[1]["messages"])
    for request in requests:
        assert "discarded thinking" not in json.dumps(request["messages"])
        assert "discarded answer" not in json.dumps(request["messages"])
    assert normal in requests[2]["messages"]
    lean_feedback = next(m for m in requests[2]["messages"] if m["role"] == "tool")
    assert lean_feedback["tool_call_id"] == "verify"
    assert "unsolved goals" in lean_feedback["content"]
    saved = build_conversation_artifact(
        driver=driver, episode=episode, client=client, result=result.to_dict(),
    )
    assert [r["turn"] for r in saved["rejected_responses"]] == [1, 3]
    assert all(r["response"] == rejected for r in saved["rejected_responses"])
    assert [r["errors"][0]["error_kind"] for r in saved["rejected_responses"]] == [
        "capability_disabled" if kind == "synthetic" else kind
    ] * 2
    assert "discarded thinking" not in json.dumps(saved["messages"])


def test_length_finish_reason_with_complete_valid_call_is_not_a_format_error(tmp_path):
    episode, _, _, _ = make_episode(tmp_path, config=EpisodeConfig(
        budget=BudgetConfig(max_turns=2), max_consecutive_format_errors=1,
    ))
    client = SimpleNamespace(generation_records=[])

    async def chat(**kwargs):
        assert len(client.generation_records) < 2
        client.generation_records.append({"finish_reason": "length"})
        return {
            "role": "assistant", "content": "",
            "tool_calls": [native_call("file_read", {"file_path": "Submission.lean"})],
        }

    client.chat = AsyncMock(side_effect=chat)
    driver = ProverDriver(client, episode, STATEMENT, verbose=False)
    result = asyncio.run(driver.run_rollout())
    assert client.chat.await_count == result.turns == result.tool_calls == 2
    assert episode.observe().state["consecutive_format_errors"] == 0
    assert result.reason == "max_turns" and result.truncated
    assert not result.terminated and not result.aborted
    tool_results = [json.loads(message["content"]) for message in driver.messages if message["role"] == "tool"]
    assert len(tool_results) == 2
    assert all(outcome["valid"] for outcome in episode.observe().last_outcomes)


@pytest.mark.parametrize("name,arguments", [
    ("lean_verify", {"code": 17}),
    ("lean_verify", {"code": "  "}),
    ("lean_verify", {"file_path": "  "}),
    ("file_edit", {"file_path": "Submission.lean", "new_str": "trivial"}),
    ("file_read", {"file_path": "Submission.lean", "start_column": -1}),
    ("lean_retrieve", {}),
    ("content_search", {"path": "Submission", "content_pattern": "target", "B": -1}),
    ("content_search", {"path": "Submission", "content_pattern": "target", "max_matches_per_file": 0}),
])
def test_real_tool_argument_rejections_count_as_format_errors(tmp_path, name, arguments):
    episode, verifier, comparator, _ = make_episode(
        tmp_path, config=EpisodeConfig(max_consecutive_format_errors=1),
    )
    transition = asyncio.run(episode.step(action(native_call(name, arguments))))
    assert transition.outcomes[0].error_kind == "invalid_arguments"
    assert_format_failure(episode.result())
    assert transition.next_observation.state["consecutive_format_errors"] == 1
    verifier.verify_code.assert_not_awaited()
    verifier.verify_file.assert_not_awaited()
    comparator.verify.assert_not_awaited()


@pytest.mark.parametrize("compiler_text", [
    "code and file_path must be strings or null",
    "code must not be empty",
    "file_path must not be empty",
    "Provide old_str or old_line_range",
    "ValueError: start_column must be a non-negative integer",
    "Provide at least one of natural_language_query, lean_name, or keywords.",
    "ValueError: Content search context counts must be nonnegative",
    "ValueError: max_matches_per_file must be positive",
])
def test_argument_error_words_in_lean_diagnostics_do_not_count_as_format_errors(tmp_path, compiler_text):
    episode, verifier, comparator, _ = make_episode(tmp_path)
    verifier.verify_code.return_value = {"success": False, "errors": [compiler_text]}

    async def run():
        await episode.step(malformed_action())
        await episode.step(malformed_action())
        return await episode.step(action(native_call("lean_verify", {"code": CANDIDATE})))

    transition = asyncio.run(run())
    assert transition.outcomes[0].error_kind != "invalid_arguments"
    assert not transition.outcomes[0].valid
    assert transition.next_observation.state["consecutive_format_errors"] == 0
    assert not transition.terminated and not transition.truncated and not transition.aborted
    assert transition.reward == 0.0
    assert not _result_is_infrastructure(episode.result().to_dict())
    verifier.verify_code.assert_awaited_once()
    comparator.verify.assert_not_awaited()
