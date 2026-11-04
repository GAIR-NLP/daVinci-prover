"""Malformed tool markup is recoverable; an ordinary visible reply ends generation."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from dvp.config import BudgetConfig, EpisodeConfig
from test_format_error_limit import (
    CANDIDATE, action, assert_format_failure, make_episode, native_call,
)
from test_model_completion import driver_for, response


# The tool name is incorrectly placed in a parameter opener, and whitespace
# separates the DSML marker from the tag name. There is no invoke opener.
MALFORMED_DSML = (
    '\n\n<｜DSML｜ parameter name="lean_verify">\n'
    '<｜DSML｜ parameter name="code" string="true">'
    'import Mathlib\n#check Nat.add_comm\n'
    '</｜DSML｜ parameter>\n'
    '</｜DSML｜ invoke>\n'
    '</｜DSML｜ calls>'
)


def assert_no_verification(verifier, comparator):
    verifier.verify_code.assert_not_awaited()
    verifier.verify_file.assert_not_awaited()
    comparator.verify.assert_not_awaited()


@pytest.mark.parametrize("saved_complete_candidate", [False, True])
def test_malformed_visible_tool_text_is_rejected_without_final_check(
    tmp_path, saved_complete_candidate,
):
    episode, verifier, comparator, _ = make_episode(tmp_path)
    episode.tools.call = AsyncMock(wraps=episode.tools.call)
    episode.reset()
    if saved_complete_candidate:
        episode.tools.workspace.submission_file.write_text(CANDIDATE)
    original_source = episode.tools.workspace.submission_file.read_bytes()

    transition = asyncio.run(episode.step(action(
        content=MALFORMED_DSML, finish_reason="stop",
        reasoning_content="Inspect the declaration before continuing the proof.",
    )))

    assert [outcome.error_kind for outcome in transition.outcomes] == ["tool_parse_error"]
    assert not any(outcome.executed for outcome in transition.outcomes)
    assert not transition.terminated and not transition.truncated and not transition.aborted
    state = transition.next_observation.state
    assert state["consecutive_format_errors"] == 1
    assert state["tool_calls_used"] == state["lean_calls_used"] == 0
    assert not any(event["kind"] in {"completion_recovery", "final_candidate_check", "model_finished"}
                   for event in transition.events)
    feedback = transition.outcomes[0].feedback
    assert "tool" in feedback.lower()
    assert "malformed" in feedback.lower() or "parse" in feedback.lower()
    assert "JSON" in feedback
    assert "sorry/admit placeholders" not in feedback
    episode.tools.call.assert_not_awaited()
    assert_no_verification(verifier, comparator)
    assert episode.tools.workspace.submission_file.read_bytes() == original_source


def test_three_consecutive_malformed_visible_calls_reach_format_limit(tmp_path):
    episode, verifier, comparator, _ = make_episode(tmp_path)

    async def run():
        for expected in (1, 2, 3):
            transition = await episode.step(action(content=MALFORMED_DSML, finish_reason="stop"))
            assert [outcome.error_kind for outcome in transition.outcomes] == ["tool_parse_error"]
            assert transition.next_observation.state["consecutive_format_errors"] == expected
            assert transition.terminated is (expected == 3)
        return episode.result()

    result = asyncio.run(run())

    assert_format_failure(result)
    assert result.turns == 3
    assert result.tool_calls == result.lean_calls == 0
    assert_no_verification(verifier, comparator)


def test_executed_read_resets_malformed_streak_without_spending_completion_recovery(tmp_path):
    episode, verifier, comparator, _ = make_episode(tmp_path)

    async def run():
        first = await episode.step(action(content=MALFORMED_DSML, finish_reason="stop"))
        assert first.next_observation.state["consecutive_format_errors"] == 1
        read = await episode.step(action(
            native_call("file_read", {"file_path": "Submission.lean"}),
            finish_reason="tool_calls",
        ))
        assert read.outcomes[0].executed and read.outcomes[0].valid
        assert read.next_observation.state["consecutive_format_errors"] == 0
        second = await episode.step(action(content=MALFORMED_DSML, finish_reason="stop"))
        assert second.outcomes[0].error_kind == "tool_parse_error"
        assert second.next_observation.state["consecutive_format_errors"] == 1
        assert not second.terminated
        return await episode.step(action(content="I cannot finish this proof.", finish_reason="stop"))

    completion = asyncio.run(run())

    assert completion.terminated and completion.reason == "model_finished"
    assert not completion.outcomes
    assert episode.result().tool_calls == 1
    assert_no_verification(verifier, comparator)


def test_a_later_malformed_action_cannot_reopen_a_finished_rollout(tmp_path):
    episode, verifier, comparator, _ = make_episode(tmp_path)
    final = asyncio.run(episode.step(action(content="I cannot finish this proof.", finish_reason="stop")))
    assert final.terminated and final.reason == "model_finished"
    with pytest.raises(RuntimeError, match="episode already finished"):
        asyncio.run(episode.step(action(content=MALFORMED_DSML, finish_reason="stop")))
    assert_no_verification(verifier, comparator)


@pytest.mark.parametrize("finish_reason", ["stop", "tool_calls"])
def test_native_call_takes_priority_over_malformed_visible_fragment(tmp_path, finish_reason):
    episode, verifier, comparator, _ = make_episode(tmp_path)

    transition = asyncio.run(episode.step(action(
        native_call("file_read", {"file_path": "Submission.lean"}),
        content=MALFORMED_DSML, finish_reason=finish_reason,
    )))

    assert len(transition.outcomes) == 1
    assert transition.outcomes[0].name == "file_read"
    assert transition.outcomes[0].executed and transition.outcomes[0].valid
    assert transition.outcomes[0].error_kind is None
    assert transition.next_observation.state["consecutive_format_errors"] == 0
    assert episode.result().tool_calls == 1
    assert episode.result().lean_calls == 0
    assert_no_verification(verifier, comparator)


def test_length_cutoff_without_native_calls_retains_truncation_precedence(tmp_path):
    episode, verifier, comparator, _ = make_episode(tmp_path, config=EpisodeConfig(
        max_consecutive_format_errors=1,
    ))

    transition = asyncio.run(episode.step(action(content=MALFORMED_DSML, finish_reason="length")))

    assert [outcome.error_kind for outcome in transition.outcomes] == ["output_truncated"]
    assert not any(outcome.executed for outcome in transition.outcomes)
    assert not transition.terminated
    assert transition.next_observation.state["consecutive_format_errors"] == 0
    assert transition.next_observation.state["consecutive_output_truncations"] == 1
    assert episode.result().tool_calls == episode.result().lean_calls == 0
    assert_no_verification(verifier, comparator)


@pytest.mark.parametrize("content", [
    "I cannot finish this proof.",
    'I cannot finish. The earlier reply contained `<｜DSML｜ parameter name="lean_verify">`.',
    f"Here is the malformed example I could not use:\n```text\n{MALFORMED_DSML}\n```",
])
def test_ordinary_visible_completion_and_quoted_examples_keep_completion_semantics(tmp_path, content):
    episode, verifier, comparator, _ = make_episode(tmp_path)

    final = asyncio.run(episode.step(action(content=content, finish_reason="stop")))
    assert not final.outcomes
    assert final.next_observation.state["consecutive_format_errors"] == 0

    assert final.terminated and final.reason == "model_finished"
    assert episode.result().tool_calls == episode.result().lean_calls == 0
    assert_no_verification(verifier, comparator)


def test_current_history_excludes_malformed_tool_text_and_keeps_rejection_evidence(tmp_path):
    episode, verifier, comparator, _ = make_episode(tmp_path, config=EpisodeConfig(
        history_policy="current", budget=BudgetConfig(max_turns=2),
    ))
    malformed = response(MALFORMED_DSML, reasoning_content="This lookup will help continue the proof.")
    driver, requests = driver_for(episode, [
        (malformed, "stop"),
        (response(tool_calls=[native_call("file_read", {"file_path": "Submission.lean"})]), "tool_calls"),
    ])

    result = asyncio.run(driver.run_rollout())

    assert result.reason == "max_turns"
    assert result.tool_calls == 1 and result.lean_calls == 0
    assert len(requests) == 2
    recovery_request = requests[1]["messages"]
    assert not any(message.get("content") == MALFORMED_DSML for message in recovery_request)
    assert not any(message["role"] == "assistant" for message in recovery_request)
    feedback = recovery_request[-1]
    assert feedback["role"] == "user"
    assert "tool" in feedback["content"].lower()
    assert "malformed" in feedback["content"].lower() or "parse" in feedback["content"].lower()
    assert "JSON" in feedback["content"]
    assert len(driver.rejected_responses) == 1
    rejected = driver.rejected_responses[0]
    assert rejected["response"]["content"] == MALFORMED_DSML
    assert rejected["response"]["reasoning_content"] == malformed["reasoning_content"]
    assert [error["error_kind"] for error in rejected["errors"]] == ["tool_parse_error"]
    assert episode.observe().state["consecutive_format_errors"] == 0
    assert_no_verification(verifier, comparator)
