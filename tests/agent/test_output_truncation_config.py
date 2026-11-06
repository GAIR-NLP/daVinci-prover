"""Output-truncation recovery has a recorded, independently configurable limit."""

from types import SimpleNamespace

import pytest

from dvp.artifacts.conversation import build_conversation_artifact
from dvp.cli.evaluate import build_parser as eval_parser
from dvp.cli.prove import build_parser as prove_parser
from dvp.config import EpisodeConfig
from dvp.orchestration.config import episode_config_from_args
from dvp.orchestration.manifest import build_run_manifest, validate_resume_manifest


@pytest.mark.parametrize("value", [-1, True, False, 1.0, 1.5, "5", None])
def test_output_truncation_limit_requires_a_nonnegative_integer(value):
    with pytest.raises(ValueError, match="max_consecutive_output_truncations"):
        EpisodeConfig(max_consecutive_output_truncations=value)


def test_default_and_disabled_limit_have_independent_configuration_identities():
    default = EpisodeConfig()
    disabled = EpisodeConfig(max_consecutive_output_truncations=0)
    custom = EpisodeConfig(max_consecutive_output_truncations=2)

    assert default.max_consecutive_output_truncations == 5
    assert len({config.config_id for config in (default, disabled, custom)}) == 3
    assert default.budget == disabled.budget == custom.budget
    assert default.max_consecutive_format_errors == disabled.max_consecutive_format_errors == 3
    assert disabled.retain_truncated_thinking is False


@pytest.mark.parametrize("parser_factory", [eval_parser, prove_parser])
def test_cli_translates_default_disabled_and_custom_limits(parser_factory):
    parser = parser_factory()
    default = episode_config_from_args(parser.parse_args([]))
    disabled = episode_config_from_args(parser.parse_args([
        "--max-consecutive-output-truncations", "0",
    ]))
    custom = episode_config_from_args(parser.parse_args([
        "--max-consecutive-output-truncations", "2",
        "--max-consecutive-format-errors", "7",
        "--retain-truncated-thinking", "false",
    ]))

    assert default.max_consecutive_output_truncations == 5
    assert disabled.max_consecutive_output_truncations == 0
    assert disabled.budget == default.budget
    assert custom.max_consecutive_output_truncations == 2
    assert custom.max_consecutive_format_errors == 7
    assert custom.retain_truncated_thinking is False


@pytest.mark.parametrize("parser_factory", [eval_parser, prove_parser])
@pytest.mark.parametrize("value", ["-1", "1.5", "true"])
def test_cli_rejects_invalid_truncation_limits(parser_factory, value):
    with pytest.raises(SystemExit):
        parser_factory().parse_args(["--max-consecutive-output-truncations", value])


def test_existing_argument_namespaces_receive_the_default_limit():
    args = eval_parser().parse_args([])
    del args.max_consecutive_output_truncations
    assert episode_config_from_args(args).max_consecutive_output_truncations == 5


@pytest.mark.parametrize("limit", [0, 2])
def test_manifest_and_conversation_record_limit_and_resume_rejects_change(tmp_path, limit):
    dataset = tmp_path / "tasks.jsonl"
    dataset.write_text('{"task_id":"t","statement":"example : True := by sorry"}\n')
    args = eval_parser().parse_args([
        "--dataset", str(dataset), "--model", "test", "--python-execute", "false",
    ])
    args.target_workspace = None
    default = build_run_manifest(args, dataset=dataset, run_root=tmp_path / "run")
    args.max_consecutive_output_truncations = limit
    changed = build_run_manifest(args, dataset=dataset, run_root=tmp_path / "run")

    assert default["episode"]["max_consecutive_output_truncations"] == 5
    assert changed["episode"]["max_consecutive_output_truncations"] == limit
    assert changed["schema_version"] == default["schema_version"] == "dvp.episode.v13"
    assert changed["episode"]["budget"] == default["episode"]["budget"]
    with pytest.raises(ValueError, match="config_id.*episode"):
        validate_resume_manifest(default, changed)

    saved = build_conversation_artifact(
        driver=SimpleNamespace(messages=[]),
        episode=SimpleNamespace(config=episode_config_from_args(args), tool_schemas=lambda: []),
        client=SimpleNamespace(), result={},
    )
    assert saved["episode"]["max_consecutive_output_truncations"] == limit
    assert saved["episode"]["config_id"] == changed["config_id"]
    assert saved["schema_version"] == "dvp.conversation.v4"


def test_conversation_without_episode_config_records_the_default_limit():
    saved = build_conversation_artifact(
        driver=SimpleNamespace(messages=[]),
        episode=SimpleNamespace(tool_schemas=lambda: []),
        client=SimpleNamespace(), result={},
    )
    assert saved["episode"]["max_consecutive_output_truncations"] == 5
