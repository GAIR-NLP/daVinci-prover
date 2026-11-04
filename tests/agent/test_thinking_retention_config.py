"""Retention experiments must be selectable, recorded, and resume-isolated."""

from types import SimpleNamespace

import pytest

from dvp.artifacts.conversation import build_conversation_artifact
from dvp.cli.evaluate import build_parser as eval_parser
from dvp.cli.prove import build_parser as prove_parser
from dvp.config import EpisodeConfig
from dvp.orchestration.config import episode_config_from_args
from dvp.orchestration.manifest import build_run_manifest, validate_resume_manifest


@pytest.mark.parametrize("value", [None, 0, 1, "false", "true"])
def test_retention_configuration_requires_a_boolean(value):
    with pytest.raises(ValueError, match="retain_truncated_thinking"):
        EpisodeConfig(retain_truncated_thinking=value)


@pytest.mark.parametrize("parser_factory", [eval_parser, prove_parser])
def test_cli_selects_both_policies_and_preserves_other_budgets(parser_factory):
    parser = parser_factory()
    default = episode_config_from_args(parser.parse_args([]))
    discarded = episode_config_from_args(parser.parse_args(["--retain-truncated-thinking", "false"]))
    retained = episode_config_from_args(parser.parse_args(["--retain-truncated-thinking", "true"]))
    assert default == discarded == EpisodeConfig()
    assert retained.retain_truncated_thinking is True
    assert discarded.retain_truncated_thinking is False
    assert retained.config_id != discarded.config_id
    assert retained.budget == discarded.budget
    assert retained.max_consecutive_format_errors == discarded.max_consecutive_format_errors == 3
    with pytest.raises(SystemExit):
        parser.parse_args(["--retain-truncated-thinking", "auto"])


def test_manifest_and_conversation_record_policy_and_resume_rejects_switch(tmp_path):
    dataset = tmp_path / "tasks.jsonl"
    dataset.write_text('{"task_id":"t","statement":"example : True := by sorry"}\n')
    args = eval_parser().parse_args(["--dataset", str(dataset), "--model", "test"])
    args.target_workspace = None
    args.retain_truncated_thinking = True
    retained = build_run_manifest(args, dataset=dataset, run_root=tmp_path / "run")
    args.retain_truncated_thinking = False
    discarded = build_run_manifest(args, dataset=dataset, run_root=tmp_path / "run")
    assert retained["episode"]["retain_truncated_thinking"] is True
    assert discarded["episode"]["retain_truncated_thinking"] is False
    with pytest.raises(ValueError, match="config_id.*episode"):
        validate_resume_manifest(retained, discarded)
    args.retain_truncated_thinking = False
    saved = build_conversation_artifact(
        driver=SimpleNamespace(messages=[]),
        episode=SimpleNamespace(config=episode_config_from_args(args), tool_schemas=lambda: []),
        client=SimpleNamespace(), result={},
    )
    assert saved["episode"]["retain_truncated_thinking"] is False


def test_older_argument_namespaces_and_artifacts_use_the_retention_default():
    args = eval_parser().parse_args([])
    del args.retain_truncated_thinking
    assert episode_config_from_args(args).retain_truncated_thinking is False
    saved = build_conversation_artifact(
        driver=SimpleNamespace(messages=[]), episode=SimpleNamespace(tool_schemas=lambda: []),
        client=SimpleNamespace(), result={},
    )
    assert saved["episode"]["retain_truncated_thinking"] is False
