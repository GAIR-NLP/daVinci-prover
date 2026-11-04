"""Python activation, startup failures, and resume identity are deployment policy."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from unittest.mock import AsyncMock

import pytest

from dvp.cli.evaluate import build_parser
from dvp.cli.prove import build_parser as prove_parser
from dvp.factory import build_python_executor
from dvp.orchestration import evaluator, manifest


class FakeExecutor:
    def __init__(self, **settings):
        self.timeout = settings["timeout"]
        self.max_output_bytes = settings["max_output_bytes"]
        self.identity = {"backend": "test-sandbox", **settings}
        self.preflight = AsyncMock()


@pytest.fixture
def fake_executor(monkeypatch):
    monkeypatch.setattr("dvp.runtime.python_local.LocalPythonExecutor", FakeExecutor)


def options(tmp_path, *extra):
    dataset = tmp_path / "tasks.jsonl"
    dataset.write_text(json.dumps({"task_id": "sample", "statement": "theorem t : True := by trivial"}) + "\n")
    return build_parser().parse_args([
        "--dataset", str(dataset), "--model", "mock", "--lean-retrieve", "false",
        "--run-root", str(tmp_path / "run"), *extra,
    ])


@pytest.mark.parametrize("parser", [build_parser, prove_parser])
def test_cli_explicit_enable_and_disabled_default(parser, fake_executor):
    args = parser().parse_args([])
    assert build_python_executor(args) is None
    args = parser().parse_args(["--python-execute", "true", "--python-timeout", "7.5",
                               "--python-memory-mb", "256", "--python-max-output-bytes", "2048"])
    executor = build_python_executor(args)
    assert executor is build_python_executor(args)
    assert executor.identity == {"backend": "test-sandbox", "timeout": 7.5,
                                 "memory_bytes": 256 * 1024 * 1024, "max_output_bytes": 2048}


@pytest.mark.parametrize("flag,value", [
    ("--python-timeout", "nan"), ("--python-timeout", "inf"), ("--python-timeout", "0"),
    ("--python-memory-mb", "0"), ("--python-max-output-bytes", "-1"),
])
def test_cli_rejects_invalid_limits(flag, value):
    with pytest.raises(SystemExit):
        build_parser().parse_args([flag, value])


def test_resume_rejects_python_enable_or_limit_changes(tmp_path, fake_executor):
    args = options(tmp_path)
    disabled = manifest.build_run_manifest(args, dataset=args.dataset, run_root=args.run_root)
    assert "python_execute" not in disabled["services"]
    args.python_execute = True
    enabled = manifest.build_run_manifest(args, dataset=args.dataset, run_root=args.run_root)
    with pytest.raises(ValueError, match="services"):
        manifest.validate_resume_manifest(disabled, enabled)
    manifest.validate_resume_manifest(deepcopy(enabled), enabled)
    args.python_timeout = 9
    changed = manifest.build_run_manifest(args, dataset=args.dataset, run_root=args.run_root)
    with pytest.raises(ValueError, match="services"):
        manifest.validate_resume_manifest(enabled, changed)


def test_failed_python_preflight_stops_before_verification_or_tasks(tmp_path, monkeypatch, fake_executor):
    args = options(tmp_path, "--python-execute", "true")
    executor = build_python_executor(args)
    executor.preflight.side_effect = RuntimeError("Landlock unavailable")
    comparator = AsyncMock()
    task = AsyncMock()
    monkeypatch.setattr("dvp.verification.comparator.preflight_runtime", comparator)
    monkeypatch.setattr(evaluator, "run_task", task)
    with pytest.raises(SystemExit, match="Python sandbox preflight failed: Landlock unavailable"):
        asyncio.run(evaluator.async_main(args))
    comparator.assert_not_awaited()
    task.assert_not_awaited()
    assert not args.run_root.exists()
