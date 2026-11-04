"""Rollout termination and final saved-source acceptance are separate steps."""
import asyncio
import hashlib
from unittest.mock import AsyncMock

import pytest

from dvp.agent.driver import ProverDriver
from dvp.artifacts.conversation import build_conversation_artifact
from dvp.artifacts.trajectory import NullTrajectorySink
from dvp.config import BudgetConfig, EpisodeConfig
from dvp.protocol import AssistantAction
from test_format_error_limit import (
    CANDIDATE, STATEMENT, accepted, action, make_episode as base_episode, native_call,
)
from test_output_truncation import scripted_client


def response(content="", **fields):
    return {"role": "assistant", "content": content, **fields}


def make_episode(tmp_path, *, config=None):
    episode, verifier, comparator, sink = base_episode(tmp_path, config=config)

    async def compare(code, *, bundle=None, **options):
        result = {
            "success": code == CANDIDATE,
            "errors": [] if code == CANDIDATE else ["The saved source does not prove the original task."],
            "backend": "leanprover/comparator",
            "candidate_sha256": hashlib.sha256(code.encode()).hexdigest(),
            "original_source_sha256": hashlib.sha256(STATEMENT.encode()).hexdigest(),
        }
        if bundle is not None:
            result.update(source_bundle_sha256=bundle.sha256, source_bundle_manifest=bundle.manifest())
        return result

    comparator.verify = AsyncMock(side_effect=compare)
    return episode, verifier, comparator, sink


def driver_for(episode, responses):
    client, requests = scripted_client(responses)
    return ProverDriver(client, episode, STATEMENT, verbose=False), requests


@pytest.mark.parametrize("history_policy", ["current", "legacy"])
@pytest.mark.parametrize("content", ["I could not finish this proof.", "The proof is complete."])
def test_first_visible_final_response_ends_rollout_then_grades_saved_file(tmp_path, history_policy, content):
    episode, verifier, comparator, sink = make_episode(tmp_path, config=EpisodeConfig(history_policy=history_policy))
    final = response(content, reasoning_content="My final assessment.")
    driver, requests = driver_for(episode, [(final, "stop")])
    result = asyncio.run(driver.run())
    assert result.terminated and not result.success and not result.aborted
    assert result.reason == "comparator_rejected" and result.rollout_reason == "model_finished"
    assert result.turns == len(requests) == 1
    assert result.tool_calls == 0 and result.lean_calls == 1
    assert driver.messages[-1] == final
    assert not any(item["role"] == "tool" for item in driver.messages)
    assert len([item for item in driver.messages if item["role"] == "user"]) == 1
    verifier.verify_file.assert_not_awaited()
    verifier.verify_code.assert_not_awaited()
    comparator.verify.assert_awaited_once()
    assert comparator.verify.await_args.args == (STATEMENT,)
    assert sum(item["event"] == "end" for item in sink.events) == 1


@pytest.mark.parametrize("history_policy", ["current", "legacy"])
@pytest.mark.parametrize("fields", [{}, {"reasoning_content": "I am still thinking."}])
def test_empty_response_gets_one_reminder_then_final_grade(tmp_path, history_policy, fields):
    episode, verifier, comparator, _ = make_episode(tmp_path, config=EpisodeConfig(
        history_policy=history_policy, retain_truncated_thinking=True,
    ))
    episode.sink = NullTrajectorySink()
    first, second = response(" \n", **fields), response(**fields)
    driver, requests = driver_for(episode, [(first, "stop"), (second, "stop")])
    result = asyncio.run(driver.run())
    assert not result.success and result.reason == "comparator_rejected"
    assert result.rollout_reason == "empty_response" and result.turns == len(requests) == 2
    reminders = [item for item in driver.messages if item["role"] == "user"
                 and "no visible text or tool call" in item["content"]]
    assert len(reminders) == 1
    assert "resend a complete tool call" in reminders[0]["content"]
    assert requests[1]["messages"][-1] == reminders[0]
    if fields and history_policy == "legacy":
        assert first in requests[1]["messages"]
        assert driver.rejected_responses == []
    else:
        assert not any(item["role"] == "assistant" for item in requests[1]["messages"])
        saved = build_conversation_artifact(driver=driver, episode=episode, client=driver.client, result=result.to_dict())
        assert [item["response"] for item in saved["rejected_responses"]] == [
            {**first, "finish_reason": "stop"}, {**second, "finish_reason": "stop"},
        ]
    verifier.verify_file.assert_not_awaited()
    comparator.verify.assert_awaited_once()


@pytest.mark.parametrize("work", ["read", "lean_failure", "missing_file"])
def test_tool_work_replenishes_empty_response_reminder_and_can_recover(tmp_path, work):
    episode, verifier, comparator, _ = make_episode(tmp_path)
    tools = {
        "read": native_call("file_read", {"file_path": "Submission.lean"}, "work"),
        "lean_failure": native_call("lean_verify", {"code": CANDIDATE}, "work"),
        "missing_file": native_call("file_read", {"file_path": "Submission/missing.txt"}, "work"),
    }
    driver, requests = driver_for(episode, [
        (response(reasoning_content="Let's edit."), "stop"),
        (response(tool_calls=[tools[work]]), "tool_calls"),
        (response(reasoning_content="Let's replace the proof."), "stop"),
        (response(tool_calls=[native_call("file_write", {"content": CANDIDATE}, "write")]), "tool_calls"),
        (response("The saved proof is complete."), "stop"),
    ])
    result = asyncio.run(driver.run())
    assert result.success and result.reason == "verified_exact_target"
    assert result.turns == len(requests) == 5 and result.tool_calls == 2
    assert sum("no visible text or tool call" in item.get("content", "")
               for item in driver.messages if item["role"] == "user") == 2
    assert verifier.verify_code.await_count == int(work == "lean_failure")
    verifier.verify_file.assert_awaited_once()
    comparator.verify.assert_awaited_once()
    assert comparator.verify.await_args.args == (CANDIDATE,)


@pytest.mark.parametrize("rejected_call,executed,error_kind", [
    (native_call("file_read", '{"file_path":'), False, "tool_parse_error"),
    (native_call("file_write", {"file_path": "Submission.lean"}), True, "invalid_arguments"),
    (native_call("file_write", {"file_path": "Submission.lean", "content": 42}), True, "invalid_arguments"),
    (native_call("bash_execute", {}), False, "capability_disabled"),
])
def test_invalid_tools_do_not_replenish_empty_response_reminder(tmp_path, rejected_call, executed, error_kind):
    episode, _, comparator, _ = make_episode(tmp_path)

    async def run():
        first = await episode.step(action(reasoning_content="A private plan.", finish_reason="stop"))
        assert first.outcomes[0].error_kind == "empty_response"
        rejected = await episode.step(action(rejected_call, finish_reason="tool_calls"))
        assert rejected.outcomes[0].executed is executed
        assert rejected.outcomes[0].error_kind == error_kind
        assert rejected.next_observation.state["empty_response_prompted"]
        final = await episode.step(action(reasoning_content="Another plan.", finish_reason="stop"))
        assert final.terminated and final.reason == "empty_response"
        comparator.verify.assert_not_awaited()
        return await episode.finalize()

    result = asyncio.run(run())
    assert not result.success and result.rollout_reason == "empty_response"
    assert result.tool_calls == int(executed)
    comparator.verify.assert_awaited_once()


@pytest.mark.parametrize("retain", [False, True])
def test_length_continuation_is_unchanged_before_visible_finish(tmp_path, retain):
    episode, _, comparator, _ = make_episode(tmp_path, config=EpisodeConfig(retain_truncated_thinking=retain))
    partial = response("An unfinished conclusion", reasoning_content="Useful partial reasoning")
    driver, requests = driver_for(episode, [(partial, "length"), (response("I cannot complete the proof."), "stop")])
    result = asyncio.run(driver.run())
    assert not result.success and result.rollout_reason == "model_finished"
    assert result.turns == len(requests) == 2
    assert (partial if retain else response(partial["content"])) in requests[1]["messages"]
    assert "output token limit" in requests[1]["messages"][-1]["content"]
    if not retain:
        assert not any("reasoning_content" in item for item in requests[1]["messages"])
        assert driver.rejected_responses[0]["response"] == {**partial, "finish_reason": "length"}
    comparator.verify.assert_awaited_once()


@pytest.mark.parametrize("raw", [
    {"finish_reason": "tool_calls"},
    {"finish_reason": "stop", "tool_calls": ["malformed native call"]},
    {"finish_reason": "stop", "tool_calls": [{"id": "missing-function"}]},
])
def test_unparseable_native_call_is_not_mistaken_for_final_prose(tmp_path, raw):
    episode, _, comparator, _ = make_episode(tmp_path)
    transition = asyncio.run(episode.step(action(content="I will use a tool.", **raw)))
    assert not transition.terminated and not transition.truncated
    assert transition.outcomes[0].error_kind == "tool_parse_error"
    comparator.verify.assert_not_awaited()


@pytest.mark.parametrize("finish_reason", ["stop", "length", "tool_calls"])
def test_complete_tool_round_keeps_reasoning_and_does_not_end_on_prose(tmp_path, finish_reason):
    episode, _, _, _ = make_episode(tmp_path)
    tool_response = response("This is my final response.", reasoning_content="A useful plan.", tool_calls=[
        native_call("file_read", {"file_path": "Submission.lean"}, "read"),
    ])
    driver, requests = driver_for(episode, [(tool_response, finish_reason), (response("I cannot finish."), "stop")])
    result = asyncio.run(driver.run())
    assert not result.success and result.rollout_reason == "model_finished"
    assert tool_response in requests[1]["messages"]
    paired = [item for item in requests[1]["messages"] if item["role"] == "tool"]
    assert len(paired) == 1 and paired[0]["tool_call_id"] == "read"
    assert driver.rejected_responses == []


@pytest.mark.parametrize("verdict", ["accepted", "rejected", "timeout", "exception", "stale"])
def test_finalize_grades_saved_source_once_without_preliminary_lean_check(tmp_path, verdict):
    episode, verifier, comparator, sink = make_episode(tmp_path)
    episode.reset()
    episode.tools.workspace.submission_file.write_text(CANDIDATE)
    if verdict == "exception":
        comparator.verify.side_effect = RuntimeError("comparator unavailable")
    else:
        checked = {
            "accepted": accepted(),
            "rejected": {"success": False, "errors": ["original contract changed"]},
            "timeout": {"success": False, "command": ["comparator"], "errors": ["Lean verification timed out after 1s"]},
            "stale": {**accepted(), "candidate_sha256": "different-candidate"},
        }[verdict]

        async def compare(code, *, bundle, **options):
            return {**checked, "source_bundle_sha256": bundle.sha256,
                    "source_bundle_manifest": bundle.manifest()}

        comparator.verify.side_effect = compare

    async def run():
        transition = await episode.step(action(content="Finished.", finish_reason="stop"))
        assert transition.terminated and transition.reward == 0 and transition.outcomes == ()
        comparator.verify.assert_not_awaited()
        result = await episode.finalize()
        again = await episode.finalize()
        assert again.to_dict() == result.to_dict()
        return result

    result = asyncio.run(run())
    verifier.verify_file.assert_not_awaited()
    verifier.verify_code.assert_not_awaited()
    comparator.verify.assert_awaited_once()
    assert comparator.verify.await_args.args == (CANDIDATE,)
    assert result.turns == result.lean_calls == 1 and result.tool_calls == 0
    assert result.success is (verdict == "accepted") and result.score == float(verdict == "accepted")
    assert result.reason == {
        "accepted": "verified_exact_target", "rejected": "comparator_rejected", "timeout": "comparator_timeout",
        "exception": "comparator_infrastructure_error", "stale": "comparator_infrastructure_error",
    }[verdict]
    assert result.rollout_reason == "model_finished"
    assert result.aborted is (verdict in {"timeout", "exception", "stale"})
    assert result.proof_code == CANDIDATE
    assert sum(item["event"] == "end" for item in sink.events) == 1


@pytest.mark.parametrize("first", [CANDIDATE, "axiom assumption : True\ntheorem target : True := assumption\n"])
@pytest.mark.parametrize("final_code", [CANDIDATE, "theorem target : False := by trivial\n"])
def test_compile_does_not_stop_later_tools_and_only_final_saved_code_is_graded(tmp_path, first, final_code):
    episode, verifier, comparator, _ = make_episode(tmp_path)
    verifier.verify_file.return_value = {"success": True, "errors": []}

    async def run():
        transition = await episode.step(action(
            native_call("file_write", {"content": first}, "initial"),
            native_call("lean_verify", {"file_path": "Submission.lean"}, "check"),
            native_call("file_write", {"content": final_code}, "overwrite"),
            native_call("file_write", {"file_path": "Submission/after.txt", "content": "executed"}, "after"),
            finish_reason="tool_calls",
        ))
        assert not transition.terminated and not transition.truncated
        assert len(transition.outcomes) == 4 and all(item.executed for item in transition.outcomes)
        assert transition.reward == 0
        comparator.verify.assert_not_awaited()
        assert episode.tools.workspace.submission_file.read_text() == final_code
        assert (episode.tools.workspace.submission_dir / "after.txt").read_text() == "executed"
        await episode.step(action(content="Finished.", finish_reason="stop"))
        return await episode.finalize()

    result = asyncio.run(run())
    assert result.success is (final_code == CANDIDATE)
    assert result.tool_calls == 4 and result.lean_calls == 4
    assert verifier.verify_file.await_count == 3
    assert comparator.verify.await_args.args == (final_code,)
    comparator.verify.assert_awaited_once()


def test_successful_inline_proof_is_not_a_saved_submission(tmp_path):
    episode, verifier, comparator, _ = make_episode(tmp_path)
    verifier.verify_code.return_value = {"success": True, "errors": []}
    driver, _ = driver_for(episode, [
        (response(tool_calls=[native_call("lean_verify", {"code": CANDIDATE})]), "tool_calls"),
        (response("Finished."), "stop"),
    ])
    result = asyncio.run(driver.run())
    assert not result.success and result.reason == "comparator_rejected"
    assert episode.tools.workspace.submission_file.read_text() == STATEMENT
    assert comparator.verify.await_args.args == (STATEMENT,)


def test_rollout_only_api_defers_comparator_and_end_event_until_finalize(tmp_path):
    episode, _, comparator, sink = make_episode(tmp_path)
    driver, _ = driver_for(episode, [(response("Finished."), "stop")])

    async def run():
        rollout = await driver.run_rollout()
        assert rollout.reason == "model_finished"
        comparator.verify.assert_not_awaited()
        assert not any(item["event"] == "end" for item in sink.events)
        return await episode.finalize()

    assert not asyncio.run(run()).success
    comparator.verify.assert_awaited_once()
    assert sum(item["event"] == "end" for item in sink.events) == 1


@pytest.mark.parametrize("budget", [BudgetConfig(max_turns=1), BudgetConfig(max_turns=2, max_tool_calls=1)])
def test_generation_budget_cannot_prevent_final_grading(tmp_path, budget):
    episode, verifier, comparator, _ = make_episode(tmp_path, config=EpisodeConfig(budget=budget))
    driver, _ = driver_for(episode, [
        (response(tool_calls=[native_call("file_write", {"content": CANDIDATE})]), "tool_calls"),
    ])
    result = asyncio.run(driver.run())
    assert result.success and result.reason == "verified_exact_target" and result.lean_calls == 2
    verifier.verify_file.assert_awaited_once()
    comparator.verify.assert_awaited_once()


def test_final_grading_does_not_consume_or_obey_generation_tool_and_lean_budget(tmp_path):
    episode, _, comparator, _ = make_episode(tmp_path, config=EpisodeConfig(
        budget=BudgetConfig(max_turns=1, max_tool_calls=1, max_lean_calls=1),
    ))
    episode.reset()
    episode.tools.workspace.submission_file.write_text(CANDIDATE)
    episode._state.tool_calls = episode._state.lean_calls = 1
    episode.stop("lean_budget_exhausted", truncated=True)
    result = asyncio.run(episode.finalize())
    assert result.success and result.tool_calls == 1 and result.lean_calls == 2
    assert result.rollout_reason == "lean_budget_exhausted"
    comparator.verify.assert_awaited_once()


def test_external_timeout_grades_saved_file_but_infrastructure_abort_does_not(tmp_path):
    episode, _, comparator, _ = make_episode(tmp_path)
    episode.reset()
    episode.tools.workspace.submission_file.write_text(CANDIDATE)
    episode.stop("task_timeout", truncated=True)
    result = asyncio.run(episode.finalize())
    assert result.success and result.rollout_reason == "task_timeout"
    comparator.verify.assert_awaited_once()
    failed, _, failed_comparator, _ = make_episode(tmp_path / "infra")
    failed.reset()
    failed.stop("llm_infrastructure_error", aborted=True)
    result = asyncio.run(failed.finalize())
    assert result.aborted and result.reason == "llm_infrastructure_error"
    failed_comparator.verify.assert_not_awaited()


@pytest.mark.parametrize("invalid", ["missing_entry", "missing_helper"])
def test_missing_saved_source_is_invalid_submission_without_comparator(tmp_path, invalid):
    episode, _, comparator, _ = make_episode(tmp_path)
    episode.reset()
    if invalid == "missing_entry":
        episode.tools.workspace.submission_file.unlink()
    else:
        episode.tools.workspace.submission_file.write_text("import Submission.Missing\n" + CANDIDATE)
    episode.stop("model_finished")
    result = asyncio.run(episode.finalize())
    assert not result.success and result.reason == "invalid_submission"
    comparator.verify.assert_not_awaited()


def test_final_acceptance_cancellation_propagates_without_closing_the_trajectory(tmp_path):
    episode, _, comparator, sink = make_episode(tmp_path)
    episode.reset()
    episode.tools.workspace.submission_file.write_text(CANDIDATE)
    comparator.verify.side_effect = asyncio.CancelledError()
    episode.stop("model_finished")
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(episode.finalize())
    comparator.verify.assert_awaited_once()
    assert not any(item["event"] == "end" for item in sink.events)
