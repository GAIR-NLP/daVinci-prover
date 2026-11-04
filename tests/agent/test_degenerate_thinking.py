"""Malformed thinking stays auditable without contaminating the next request."""

import asyncio
import json

import pytest

from dvp.agent.driver import ProverDriver
from dvp.agent.messages import _has_degenerate_character_run
from dvp.artifacts.conversation import build_conversation_artifact
from dvp.artifacts.trajectory import NullTrajectorySink
from dvp.config import BudgetConfig, EpisodeConfig
from test_format_error_limit import STATEMENT, make_episode, native_call
from test_output_truncation import assert_truncation_failure, scripted_client


# The first observed 2021 A6 failure ends with 82,775 consecutive zeros. Recreate
# its shape without making this regression depend on a private run directory.
DEGENERATE_REASONING = "Consider a counterexample with prime 1" + "0" * 82_775


@pytest.mark.parametrize("reasoning,expected", [
    ("0" * 4_095, False),
    ("prefix 1" + "0" * 4_096 + " suffix", True),
    ("\n" * 8_000 + " " * 8_000, False),
    ("0" * 4_095 + "1" + "0" * 4_095, False),
    ("α" * 4_096, True),
])
def test_character_run_guard_uses_a_conservative_nonwhitespace_boundary(reasoning, expected):
    assert _has_degenerate_character_run(reasoning) is expected


@pytest.mark.parametrize("retain", [True, False])
@pytest.mark.parametrize("visible", ["", "The helper lemma is saved in Submission/Helper.lean."])
def test_truncated_zero_run_is_archived_but_absent_from_next_request(tmp_path, retain, visible):
    episode, _, _, _ = make_episode(tmp_path, config=EpisodeConfig(
        budget=BudgetConfig(max_turns=2), retain_truncated_thinking=retain,
    ))
    # Verify that SFT-only artifacts preserve discarded output without relying
    # on a full trajectory sink.
    episode.sink = NullTrajectorySink()
    response = {
        "role": "assistant", "content": visible,
        "reasoning_content": DEGENERATE_REASONING,
    }
    client, requests = scripted_client([
        (response, "length"),
        ({"role": "assistant", "reasoning_content": "Inspect the saved candidate.",
          "tool_calls": [native_call("file_read", {"file_path": "Submission.lean"})]}, "tool_calls"),
    ])
    driver = ProverDriver(client, episode, STATEMENT, verbose=False)
    result = asyncio.run(driver.run_rollout())

    assert result.reason == "max_turns" and result.tool_calls == 1
    assert episode.observe().state["consecutive_output_truncations"] == 0
    next_messages = requests[1]["messages"]
    assert "0" * 500 not in json.dumps(next_messages)
    assistant = [message for message in next_messages if message["role"] == "assistant"]
    assert assistant == ([{"role": "assistant", "content": visible}] if visible else [])
    assert not any(message["role"] == "tool" for message in next_messages)
    assert any(message.get("reasoning_content") == "Inspect the saved candidate."
               and message.get("tool_calls") for message in driver.messages)

    saved = build_conversation_artifact(
        driver=driver, episode=episode, client=client, result=result.to_dict(),
    )
    assert "0" * 500 not in json.dumps(saved["messages"])
    assert any(item["response"] == {**response, "finish_reason": "length"}
               for item in saved["rejected_responses"])


def test_discarding_degenerate_reasoning_still_consumes_truncation_budget(tmp_path):
    episode, _, _, _ = make_episode(tmp_path, config=EpisodeConfig(
        budget=BudgetConfig(max_turns=10), retain_truncated_thinking=True,
        max_consecutive_output_truncations=2,
    ))
    response = {"role": "assistant", "reasoning_content": DEGENERATE_REASONING}
    client, requests = scripted_client([(response, "length"), (response, "length")])
    driver = ProverDriver(client, episode, STATEMENT, verbose=False)
    result = asyncio.run(driver.run_rollout())

    assert_truncation_failure(result)
    assert result.turns == len(requests) == 2
    assert result.tool_calls == 0
    assert "0" * 500 not in json.dumps(driver.messages)
    assert len(driver.rejected_responses) == 2
    assert all(item["response"]["reasoning_content"] == DEGENERATE_REASONING
               for item in driver.rejected_responses)


def test_opt_in_retains_long_useful_reasoning_and_whitespace(tmp_path):
    episode, _, _, _ = make_episode(tmp_path, config=EpisodeConfig(
        budget=BudgetConfig(max_turns=2), retain_truncated_thinking=True,
    ))
    reasoning = "\n" * 2_000 + "\n".join(
        f"Case {n}: evaluating f({n}) gives {n * n + 1}; reduce this identity modulo {n + 2}."
        for n in range(400)
    )
    client, requests = scripted_client([
        ({"role": "assistant", "reasoning_content": reasoning}, "length"),
        ({"role": "assistant", "tool_calls": [
            native_call("file_read", {"file_path": "Submission.lean"}),
        ]}, "tool_calls"),
    ])
    driver = ProverDriver(client, episode, STATEMENT, verbose=False)
    asyncio.run(driver.run_rollout())

    assert any(message.get("reasoning_content") == reasoning
               for message in requests[1]["messages"])
    assert driver.rejected_responses == []
