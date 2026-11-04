"""Python experiment tool backed exclusively by a configured sandbox executor.

This module never starts a host interpreter. The trusted executor must enforce
its isolation policy and bound captured output before returning a result.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from typing import Any

from dvp.runtime.python_execution import PythonExecutionRequest, PythonExecutionResult


_MAX_CODE_BYTES = 65_536
# The adapter enforces the experiment's deadline. Leave time for it to kill and
# reap the process and return its bounded console before the fallback watchdog
# cancels it. This is cleanup time, not an extension of the requested timeout.
_EXECUTOR_CLEANUP_GRACE_SECONDS = 2.0


def _error(message: str, kind: str, *, infrastructure: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {"success": False, "error": message, "error_kind": kind}
    if infrastructure:
        result["_harness_infrastructure_error"] = True
    return result


def _positive_number(value: Any) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value) and value > 0
    except OverflowError:
        return False


def _bounded_output(stdout: str, stderr: str, limit: int) -> tuple[str, str, bool]:
    """Share a byte budget, retaining both streams when either is verbose."""
    out, err = stdout.encode("utf-8", errors="replace"), stderr.encode("utf-8", errors="replace")
    if len(out) + len(err) <= limit:
        return stdout, stderr, False
    out_budget = min(len(out), limit // 2)
    err_budget = min(len(err), limit - out_budget)
    out_budget = min(len(out), limit - err_budget)
    return out[:out_budget].decode("utf-8", errors="ignore"), err[:err_budget].decode("utf-8", errors="ignore"), True


async def preflight_executor(executor: Any) -> dict[str, Any]:
    """Check trusted configuration and invoke the real adapter's deployment checks."""
    if not _positive_number(executor.timeout) or type(executor.max_output_bytes) is not int or executor.max_output_bytes < 1:
        raise ValueError("Invalid Python sandbox resource configuration")
    identity = json.loads(json.dumps(executor.identity, allow_nan=False))
    if not isinstance(identity, dict) or not identity:
        raise ValueError("Missing Python executor identity")
    async with asyncio.timeout(min(10.0, float(executor.timeout))):
        await executor.preflight()
    return identity


async def python_execute(registry: Any, args: dict[str, Any]) -> dict[str, Any]:
    executor = getattr(registry, "python_executor", None)
    if executor is None:
        return _error("Python execution requires a configured sandbox executor.", "capability_disabled")
    if not isinstance(args, dict) or set(args) - {"code", "timeout"}:
        return _error("Provide only code and an optional timeout.", "invalid_arguments")
    code = args.get("code")
    if not isinstance(code, str) or not code.strip():
        return _error("code must be a nonempty string.", "invalid_arguments")
    try:
        encoded = code.encode("utf-8")
    except UnicodeEncodeError:
        return _error("code must be valid UTF-8 text.", "invalid_arguments")
    if len(encoded) > _MAX_CODE_BYTES:
        return _error("code exceeds the 65,536-byte limit.", "invalid_arguments")

    maximum = executor.timeout
    output_limit = executor.max_output_bytes
    if not _positive_number(maximum) or type(output_limit) is not int or output_limit < 1:
        return _error("Invalid Python sandbox resource configuration.", "infrastructure", infrastructure=True)
    timeout = args.get("timeout", maximum)
    if not _positive_number(timeout) or timeout > maximum:
        return _error(f"timeout must be positive and at most {maximum} seconds.", "invalid_arguments")

    try:
        # Identity is operator metadata, never taken from the program's stdout.
        identity = await preflight_executor(executor)
    except Exception as error:
        return _error(f"Python sandbox preflight failed ({type(error).__name__}).", "infrastructure", infrastructure=True)

    try:
        workspace = registry.workspace.submission_dir / "experiments"
        if workspace.is_symlink():
            raise ValueError("The Python experiment directory must not be a symlink.")
        workspace.mkdir(parents=True, exist_ok=True)
        workspace = workspace.resolve(strict=True)
        if not workspace.is_relative_to(registry.workspace.submission_dir.resolve(strict=True)):
            raise ValueError("Python experiment directory escapes the submission workspace.")
        request = PythonExecutionRequest(code=code, workspace=workspace,
                                         timeout=float(timeout), max_output_bytes=output_limit)
    except (OSError, ValueError) as error:
        return _error(str(error), "tool_error")

    try:
        # Racing an identical outer deadline against the adapter's own deadline
        # discards captured output when cancellation wins. The adapter still
        # receives the unchanged execution limit; this later watchdog bounds a
        # hung adapter and relies on its mandatory cleanup on cancellation.
        async with asyncio.timeout(float(timeout) + _EXECUTOR_CLEANUP_GRACE_SECONDS):
            result = await executor.execute(request)
        if not isinstance(result, PythonExecutionResult):
            raise TypeError("Python executor returned an invalid result")
    except TimeoutError:
        result = PythonExecutionResult(status="timed_out", error="Python execution timed out.")
    except Exception as error:
        return _error(f"Python sandbox execution failed ({type(error).__name__}).", "infrastructure", infrastructure=True)

    stdout, stderr, truncated = _bounded_output(result.stdout, result.stderr, output_limit)
    success = result.status == "completed" and result.returncode == 0
    response: dict[str, Any] = {
        "success": success,
        "returncode": result.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "timed_out": result.status == "timed_out",
        "output_truncated": truncated or result.status == "output_limit",
        "_harness_python_execution": {
            "code_sha256": hashlib.sha256(encoded).hexdigest(),
            "runtime": identity,
            "timeout": timeout,
            "max_output_bytes": output_limit,
        },
    }
    if not success:
        response["error_kind"] = {
            "completed": "tool_error",
            "timed_out": "tool_timeout",
            # Model output truncation has a separate history/retry policy.
            # A bounded Python console is an ordinary tool execution result.
            "output_limit": "tool_output_limit",
            "infrastructure_error": "infrastructure",
        }[result.status]
        if result.error:
            response["error"] = result.error
        elif result.status == "timed_out":
            response["error"] = "Python execution timed out."
        elif result.status == "output_limit":
            response["error"] = (
                "Python execution exceeded its stdout/stderr output limit. "
                "Print a summary or save larger results to a file."
            )
        elif result.status == "completed":
            response["error"] = f"Python exited with status {result.returncode}."
    if result.status == "infrastructure_error":
        response["_harness_infrastructure_error"] = True
    return response
