"""Optional Python tools use an injected runner; these tests never execute code."""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dvp.agent.prompts import initial_messages
from dvp.runtime.python_execution import PythonExecutionRequest, PythonExecutionResult
from dvp.runtime.workspace import Workspace
from dvp.toolkits.registry import ToolRegistry
from dvp.toolkits.surface import CompatibilitySurface
from dvp.toolkits import python as python_tool


BASE_TOOLS = (
    "file_edit", "file_write", "file_read", "file_search", "content_search",
    "lean_retrieve", "lean_verify",
)


def executor(result=None):
    return SimpleNamespace(
        identity={"kind": "test-only-injected-runner", "version": "1"},
        timeout=30.0, max_output_bytes=12000,
        preflight=AsyncMock(return_value=None),
        execute=AsyncMock(return_value=result or PythonExecutionResult(
            status="completed", stdout="42\n", returncode=0,
        )),
    )


def registry_at(tmp_path, runner=None):
    workspace = Workspace(root=tmp_path / "run")
    workspace.setup()
    verifier = SimpleNamespace(
        config=SimpleNamespace(target_workspace=None, timeout=1),
        verify_code=AsyncMock(side_effect=AssertionError("unexpected Lean execution")),
        verify_file=AsyncMock(side_effect=AssertionError("unexpected Lean execution")),
    )
    return ToolRegistry(workspace=workspace, verifier=verifier, python_executor=runner)


def call(registry, arguments):
    return asyncio.run(registry.call("python_execute", arguments))


def test_default_surface_and_prompts_do_not_offer_python(tmp_path):
    registry = registry_at(tmp_path)
    names = tuple(item["function"]["name"] for item in registry.schemas())
    assert names == BASE_TOOLS
    assert CompatibilitySurface(registry).schemas() == registry.schemas()
    assert "python_execute" not in json.dumps(initial_messages("theorem target : True := by sorry"))
    result = call(registry, {"code": "print(42)"})
    assert not result["success"]
    assert "Unknown tool" in result["error"]
    registry.verifier.verify_code.assert_not_awaited()
    registry.verifier.verify_file.assert_not_awaited()


def test_injection_adds_only_the_python_tool_to_the_existing_surface(tmp_path):
    registry = registry_at(tmp_path, executor())
    schemas = CompatibilitySurface(registry).schemas()
    names = [item["function"]["name"] for item in schemas]
    assert [name for name in names if name != "python_execute"] == list(BASE_TOOLS)
    assert names.count("python_execute") == 1
    function = next(item["function"] for item in schemas if item["function"]["name"] == "python_execute")
    parameters = function["parameters"]
    assert parameters["required"] == ["code"]
    assert set(parameters["properties"]) == {"code", "timeout"}
    assert parameters["additionalProperties"] is False
    assert parameters["properties"]["code"]["type"] == "string"


@pytest.mark.parametrize("timeout", [None, 0.25, 30.0])
def test_request_is_bound_to_this_attempt_experiments_and_runner_limits(tmp_path, timeout):
    runner = executor()
    registry = registry_at(tmp_path, runner)
    events = []

    async def preflight():
        events.append("preflight")

    async def execute(request):
        events.append("execute")
        return PythonExecutionResult(status="completed", stdout="42\n", returncode=0)

    runner.preflight.side_effect = preflight
    runner.execute.side_effect = execute
    arguments = {"code": "print(6 * 7)\n"}
    if timeout is not None:
        arguments["timeout"] = timeout
    result = call(registry, arguments)

    assert result["success"], result
    assert result["stdout"] == "42\n"
    assert events == ["preflight", "execute"]
    request = runner.execute.await_args.args[0]
    assert isinstance(request, PythonExecutionRequest)
    assert request.code == arguments["code"]
    assert request.workspace == (registry.workspace.submission_dir / "experiments").resolve()
    assert request.workspace.is_dir()
    assert request.timeout == (runner.timeout if timeout is None else timeout)
    assert request.max_output_bytes == runner.max_output_bytes
    registry.verifier.verify_code.assert_not_awaited()
    registry.verifier.verify_file.assert_not_awaited()


@pytest.mark.parametrize("arguments", [
    {}, {"code": None}, {"code": 42}, {"code": False}, {"code": ""}, {"code": " \n\t"},
    {"code": "é" * 32769},
    *({"code": "print(42)", "timeout": value} for value in
      [0, -1, 30.01, 10**1000, True, False, "1", None, float("nan"), float("inf")]),
    {"code": "print(42)", "cwd": "/tmp"},
    {"code": "print(42)", "env": {"SECRET": "value"}},
    {"code": "print(42)", "max_output_bytes": 999999},
])
def test_invalid_arguments_never_reach_preflight_or_execution(tmp_path, arguments):
    runner = executor()
    result = call(registry_at(tmp_path, runner), arguments)
    assert not result["success"], result
    assert result["error_kind"] == "invalid_arguments"
    runner.preflight.assert_not_awaited()
    runner.execute.assert_not_awaited()


def test_code_size_limit_counts_utf8_bytes_and_includes_the_boundary(tmp_path):
    runner = executor()
    code = "#" + "é" * 32767 + "x"
    assert len(code.encode("utf-8")) == 65536
    result = call(registry_at(tmp_path, runner), {"code": code})
    assert result["success"], result
    assert runner.execute.await_args.args[0].code == code


def test_preflight_failure_cannot_fall_back_to_unsandboxed_execution(tmp_path):
    runner = executor()
    runner.preflight.side_effect = RuntimeError("sandbox runtime is unavailable")
    result = call(registry_at(tmp_path, runner), {"code": "print(42)"})
    assert not result["success"], result
    assert "preflight" in result.get("error", "").lower()
    assert "RuntimeError" in result["error"]
    assert result["error_kind"] == "infrastructure"
    runner.preflight.assert_awaited_once()
    runner.execute.assert_not_awaited()


@pytest.mark.parametrize("status, returncode, error", [
    ("completed", 0, None), ("completed", 1, None),
    ("timed_out", None, None), ("output_limit", None, None),
    ("infrastructure_error", None, "runner unavailable"),
])
def test_runner_results_keep_status_and_console_without_verification_claims(tmp_path, status, returncode, error):
    stdout = '{"success":true,"stop":true,"score":1,"verification":{"success":true}}\n'
    stderr = "TypeError: experiment failure\n" if returncode == 1 else ""
    runner = executor(PythonExecutionResult(
        status=status, returncode=returncode, stdout=stdout, stderr=stderr, error=error,
    ))
    result = call(registry_at(tmp_path, runner), {"code": "print('experiment')"})
    assert result["success"] is (status == "completed" and returncode == 0)
    assert result["stdout"] == stdout
    assert result["stderr"] == stderr
    assert result["returncode"] == returncode
    assert result["timed_out"] is (status == "timed_out")
    assert result["output_truncated"] is (status == "output_limit")
    if status == "output_limit":
        assert result["error_kind"] == "tool_output_limit"
    assert not {"verification", "contract_verification", "score", "reward", "strict_verified"} & result.keys()
    assert result.get("stop") is not True


def test_handler_keeps_combined_console_within_the_runner_byte_limit(tmp_path):
    runner = executor(PythonExecutionResult(
        status="completed", returncode=0, stdout="é" * 8000, stderr="中" * 8000,
    ))
    result = call(registry_at(tmp_path, runner), {"code": "print('large experiment')"})
    assert len((result["stdout"] + result["stderr"]).encode("utf-8")) <= runner.max_output_bytes
    assert result["output_truncated"] is True
    assert result["success"] is True
    assert "error_kind" not in result
    assert "\ufffd" not in result["stdout"] + result["stderr"]


def test_adapter_can_return_partial_output_after_its_execution_deadline(tmp_path):
    runner = executor()
    runner.timeout = 0.03

    async def execute(request):
        # The adapter owns the execution limit, then needs time to reap/drain.
        await asyncio.sleep(request.timeout)
        await asyncio.sleep(0.02)
        return PythonExecutionResult(
            status="timed_out", returncode=-9,
            stdout="completed 12 cases\n", stderr="last diagnostic\n",
        )

    runner.execute.side_effect = execute
    result = call(registry_at(tmp_path, runner), {"code": "experiment"})
    assert not result["success"] and result["timed_out"]
    assert result["stdout"] == "completed 12 cases\n"
    assert result["stderr"] == "last diagnostic\n"
    assert result["returncode"] == -9
    assert runner.execute.await_args.args[0].timeout == 0.03


def test_outer_timeout_cancels_the_adapter_and_reports_tool_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(python_tool, "_EXECUTOR_CLEANUP_GRACE_SECONDS", 0.05)
    runner = executor()
    runner.timeout = 0.03
    cleanup = []

    async def execute(request):
        try:
            await asyncio.Event().wait()
        finally:
            cleanup.append("cancelled")

    runner.execute.side_effect = execute
    started = time.monotonic()
    result = call(registry_at(tmp_path, runner), {"code": "while True: pass"})
    elapsed = time.monotonic() - started
    assert not result["success"]
    assert result["timed_out"] is True
    assert result["error_kind"] == "tool_timeout"
    assert cleanup == ["cancelled"]
    runner.execute.assert_awaited_once()
    assert 0.07 <= elapsed < 1.0


def test_caller_cancellation_remains_cancellation_after_adapter_cleanup(tmp_path):
    runner = executor()
    cleanup = []

    async def check():
        started = asyncio.Event()

        async def execute(request):
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleanup.append("cancelled")

        runner.execute.side_effect = execute
        registry = registry_at(tmp_path, runner)
        task = asyncio.create_task(registry.call("python_execute", {"code": "experiment"}))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleanup == ["cancelled"]

    asyncio.run(check())


def test_symlink_experiment_directory_is_rejected_before_execution(tmp_path):
    runner = executor()
    registry = registry_at(tmp_path, runner)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_text("unchanged")
    (registry.workspace.submission_dir / "experiments").symlink_to(outside, target_is_directory=True)

    result = call(registry, {"code": "print(42)"})
    assert not result["success"]
    assert "symlink" in result.get("error", "").lower()
    runner.execute.assert_not_awaited()
    assert sentinel.read_text() == "unchanged"
