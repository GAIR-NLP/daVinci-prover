"""Public DVP entrypoints and packaged resources work outside the checkout."""

import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from dvp import cli


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("arguments", [
    ["dvp"], ["dvp", "prove"], ["dvp", "eval"],
    ["dvp", "prepare-contexts"], ["dvp", "reverify"], ["dvp", "parser"],
])
def test_entrypoint_help_works_outside_the_project(arguments, tmp_path):
    environment = {**os.environ, "PYTHONPATH": str(ROOT / "src"),
                   "PYTHONDONTWRITEBYTECODE": "1"}
    completed = subprocess.run([sys.executable, "-m", *arguments, "--help"],
                               cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
    assert "usage:" in completed.stdout


def test_command_exit_code_and_arguments_are_preserved(monkeypatch):
    received = []
    def run(argv):
        received.extend(argv)
        return 7
    monkeypatch.setattr(cli.importlib, "import_module", lambda _: SimpleNamespace(main=run))
    assert cli.main(["reverify", "--plan-only"]) == 7
    assert received == ["--plan-only"]


@pytest.mark.parametrize("arguments", [
    ["--theorem", "example : True := by trivial"],
    ["--theorem-file", "theorem.lean"],
    ["--quiet"],
])
def test_single_problem_arguments_are_not_silently_accepted_by_evaluation(arguments):
    from dvp.cli.evaluate import build_parser as eval_parser
    from dvp.cli.prove import build_parser as prove_parser

    prove_parser().parse_args(arguments)
    with pytest.raises(SystemExit) as error:
        eval_parser().parse_args(arguments)
    assert error.value.code == 2


@pytest.mark.parametrize("limit", [0, 1, 3, 7])
def test_single_and_batch_commands_share_episode_options(limit):
    from dvp.cli.evaluate import build_parser as eval_parser
    from dvp.cli.prove import build_parser as prove_parser
    from dvp.orchestration.config import episode_config_from_args

    arguments = ["--max-consecutive-format-errors", str(limit), "--max-turns", "11",
                 "--lean-retrieve", "false"]
    single = episode_config_from_args(prove_parser().parse_args(arguments))
    batch = episode_config_from_args(eval_parser().parse_args(arguments))
    assert single == batch
    assert batch.max_consecutive_format_errors == limit
    assert batch.budget.max_turns == 11
    assert not batch.enable_lean_retrieve


@pytest.mark.parametrize("command", ["evaluate", "prove"])
@pytest.mark.parametrize("arguments", [
    ["--history-policy", "current"], ["--history-policy", "legacy"],
    ["--guidance", "off"], ["--guidance", "soft"],
])
def test_removed_history_and_guidance_options_are_rejected(command, arguments):
    parser = cli.importlib.import_module(f"dvp.cli.{command}").build_parser()
    with pytest.raises(SystemExit) as error:
        parser.parse_args(arguments)
    assert error.value.code == 2


def test_lean_resources_are_available_at_the_packaged_location():
    from dvp.verification.command_parser import WORKER_SOURCE
    from dvp.verification import lean, sandbox

    assert WORKER_SOURCE.read_text().startswith("import Lean\n")
    draft = Path(lean.__file__).with_name("resources") / "LocalDraftVerifier.lean"
    assert draft.read_text().startswith("import Lean\n")
    assert WORKER_SOURCE.parent == draft.parent
    assert not (draft.parent / "FixedContextGuard.lean").exists()
    assert Path(sandbox.__file__).is_file()
