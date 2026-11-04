"""Legacy comparisons retain rejected exchanges without changing execution."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from dvp.agent.driver import ProverDriver
from dvp.artifacts.conversation import build_conversation_artifact
from dvp.cli.evaluate import build_parser as eval_parser
from dvp.cli.prove import build_parser as prove_parser
from dvp.config import BudgetConfig, EpisodeConfig
from dvp.orchestration.config import episode_config_from_args
from dvp.orchestration.manifest import build_run_manifest, validate_resume_manifest

from test_format_error_limit import CANDIDATE, STATEMENT, make_episode, native_call
from test_output_truncation import scripted_client


@pytest.mark.parametrize("value", [None, True, 0, "retain", "LEGACY", []])
def test_history_policy_rejects_unknown_values(value):
    with pytest.raises(ValueError, match="history_policy"):
        EpisodeConfig(history_policy=value)


@pytest.mark.parametrize("parser_factory", [eval_parser, prove_parser])
def test_legacy_cli_sets_history_and_keeps_limits_independent(parser_factory):
    parser = parser_factory()
    default = episode_config_from_args(parser.parse_args([]))
    legacy = episode_config_from_args(parser.parse_args([
        "--history-policy", "legacy", "--max-consecutive-format-errors", "0",
        "--max-consecutive-output-truncations", "0", "--max-turns", "200",
        "--max-tool-calls", "600",
    ]))
    assert default.history_policy == "current"
    assert legacy.history_policy == "legacy"
    assert default.config_id != legacy.config_id
    assert legacy.max_consecutive_format_errors == legacy.max_consecutive_output_truncations == 0
    assert legacy.budget.max_turns == 200 and legacy.budget.max_tool_calls == 600
    assert EpisodeConfig(history_policy="legacy").max_consecutive_format_errors == 3
    with pytest.raises(SystemExit):
        parser.parse_args(["--history-policy", "unknown"])


@pytest.mark.parametrize("finish", ["tool_calls", "length"])
@pytest.mark.parametrize("policy", ["current", "legacy"])
def test_history_reaches_next_request_after_four_recoverable_replies(tmp_path, finish, policy):
    episode, verifier, comparator, _ = make_episode(tmp_path, config=EpisodeConfig(
        history_policy=policy, retain_truncated_thinking=False,
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
    if policy == "legacy":
        assert [m["reasoning_content"] for m in history] == [f"derivation {i}" for i in range(4)]
        assert [m["content"] for m in history] == [f"partial {i}" for i in range(4)]
        assert driver.rejected_responses == []
    elif finish == "length":
        assert history == [{"role": "assistant", "content": f"partial {i}"} for i in range(4)]
        assert len(driver.rejected_responses) == 4
        assert [item["response"]["reasoning_content"] for item in driver.rejected_responses] == [
            f"derivation {i}" for i in range(4)
        ]
    else:
        assert history == [] and len(driver.rejected_responses) == 4
    verifier.verify_file.assert_not_awaited()
    comparator.verify.assert_not_awaited()


def test_legacy_mixed_batch_retains_paired_results_and_real_workspace_effects(tmp_path):
    episode, _, _, sink = make_episode(tmp_path, config=EpisodeConfig(
        history_policy="legacy", budget=BudgetConfig(max_turns=2),
    ))
    broken = '{"file_path":'
    client, requests = scripted_client([
        ({"role": "assistant", "content": "", "reasoning_content": "keep this reasoning",
          "tool_calls": [
              native_call("file_write", {"file_path": "Submission/note.txt", "content": "written once"}, "write"),
              native_call("file_read", broken, "broken"),
          ]}, "tool_calls"),
        ({"role": "assistant", "content": "", "tool_calls": [
            native_call("file_read", {"file_path": "Submission/note.txt"}, "read"),
        ]}, "tool_calls"),
    ])
    driver = ProverDriver(client, episode, STATEMENT, verbose=False)
    asyncio.run(driver.run_rollout())
    history = requests[1]["messages"]
    assistant = next(m for m in history if m["role"] == "assistant")
    assert assistant["reasoning_content"] == "keep this reasoning"
    assert assistant["tool_calls"][1]["function"]["arguments"] == "{}"
    paired = [m for m in history if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in paired] == ["write", "broken"]
    rejected = json.loads(paired[1]["content"])
    assert rejected["success"] is False
    assert "Invalid tool arguments" in rejected["error"]
    assert (episode.tools.workspace.submission_dir / "note.txt").read_text() == "written once"
    transition = next(e["payload"] for e in sink.events if e["event"] == "transition")
    assert transition["action"]["raw"]["tool_calls"][1]["function"]["arguments"] == broken
    assert transition["outcomes"][1]["error_kind"] == "tool_parse_error"
    assert not transition["outcomes"][1]["executed"]
    assert driver.rejected_responses == []


@pytest.mark.parametrize("finish,expected,turns", [
    ("tool_calls", "repeated_format_error", 3), ("length", "repeated_output_truncation", 5),
])
def test_legacy_history_does_not_implicitly_remove_limits(tmp_path, finish, expected, turns):
    episode, _, _, _ = make_episode(tmp_path, config=EpisodeConfig(history_policy="legacy"))
    client, _ = scripted_client([
        ({"role": "assistant", "content": "partial", "reasoning_content": "reasoning"}, finish)
    ] * turns)
    result = asyncio.run(ProverDriver(client, episode, STATEMENT, verbose=False).run_rollout())
    assert result.reason == expected and result.turns == turns


@pytest.mark.parametrize("accepted", [True, False])
def test_legacy_grades_only_after_all_development_calls_and_visible_stop(tmp_path, accepted):
    episode, verifier, comparator, _ = make_episode(tmp_path, config=EpisodeConfig(
        history_policy="legacy", max_consecutive_format_errors=0,
        max_consecutive_output_truncations=0,
    ))
    verifier.verify_code.return_value = {"success": True, "errors": []}
    if not accepted:
        comparator.verify.return_value = {"success": False, "errors": ["modified target"]}
    client, requests = scripted_client([
        ({"role": "assistant", "content": "", "tool_calls": [
            native_call("file_write", {"file_path": "Submission.lean", "content": CANDIDATE}, "save"),
            native_call("lean_verify", {"code": CANDIDATE}, "verify"),
            native_call("file_write", {"file_path": "Submission/after.txt", "content": "must not run"}, "after"),
        ]}, "tool_calls"),
        ({"role": "assistant", "content": "Done."}, "stop"),
    ])
    result = asyncio.run(ProverDriver(client, episode, STATEMENT, verbose=False).run())
    assert result.success is accepted
    assert result.reason == ("verified_exact_target" if accepted else "comparator_rejected")
    assert len(requests) == 2
    assert (episode.tools.workspace.submission_dir / "after.txt").exists()
    comparator.verify.assert_awaited_once()


def test_policy_is_recorded_and_cannot_change_on_resume(tmp_path):
    dataset = tmp_path / "tasks.jsonl"
    dataset.write_text(json.dumps({"task_id": "t", "statement": STATEMENT}) + "\n")
    args = eval_parser().parse_args([
        "--dataset", str(dataset), "--model", "test", "--python-execute", "false",
    ])
    args.target_workspace = None
    current = build_run_manifest(args, dataset=dataset, run_root=tmp_path / "run")
    args.history_policy = "legacy"
    legacy = build_run_manifest(args, dataset=dataset, run_root=tmp_path / "run")
    assert legacy["episode"]["history_policy"] == "legacy"
    with pytest.raises(ValueError, match="config_id.*episode"):
        validate_resume_manifest(current, legacy)
    episode, _, _, sink = make_episode(tmp_path, config=episode_config_from_args(args))
    observation = episode.reset()
    assert observation.state["history_policy"] == "legacy"
    assert sink.events[0]["payload"]["config"]["history_policy"] == "legacy"
    artifact = build_conversation_artifact(
        driver=SimpleNamespace(messages=[]), episode=episode,
        client=SimpleNamespace(), result={},
    )
    assert artifact["episode"]["history_policy"] == "legacy"
    assert artifact["episode"]["config_id"] == legacy["config_id"]
