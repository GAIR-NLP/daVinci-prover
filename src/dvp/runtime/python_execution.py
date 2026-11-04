"""Backend-independent contract for isolated Python experiments.

This module launches nothing and supplies no sandbox implementation. A trusted,
configured adapter must enforce the requested workspace boundary,
resource limits, and cleanup using its actual runner. Satisfying this
Protocol or returning an identity dictionary does not establish isolation.

The handler chooses the current attempt's Submission/experiments directory.
Adapters may expose their pinned Python runtime read-only, but must not grant
access to other task files, credentials, host processes, or the network. These
requirements must be verified for the deployed runner before enabling the tool;
there is no fallback to an ordinary host Python process.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
from typing import Any, Literal, Protocol


DEFAULT_PYTHON_TIMEOUT = 30.0
DEFAULT_PYTHON_MAX_OUTPUT_BYTES = 12_000
PythonExecutionStatus = Literal[
    "completed", "timed_out", "output_limit", "infrastructure_error",
]
_STATUSES = frozenset({"completed", "timed_out", "output_limit", "infrastructure_error"})


@dataclass(frozen=True)
class PythonExecutionRequest:
    """Trusted execution parameters, independent of any runner's command syntax.

    ``workspace`` is selected by the handler, never by model arguments. Path
    containment, symlink handling, and isolation belong to that handler and the
    adapter; constructing this value neither reads nor authorizes a directory.
    ``timeout`` bounds wall time and ``max_output_bytes`` bounds combined
    stdout/stderr capture. The adapter must also prevent unbounded buffering.
    """

    code: str
    workspace: Path
    timeout: float
    max_output_bytes: int

    def __post_init__(self) -> None:
        if not isinstance(self.code, str):
            raise TypeError("Python execution code must be a string")
        if not isinstance(self.workspace, Path):
            raise TypeError("Python execution workspace must be a pathlib.Path")
        if (
            isinstance(self.timeout, bool)
            or not isinstance(self.timeout, (int, float))
            or not math.isfinite(self.timeout)
            or self.timeout <= 0
        ):
            raise ValueError("Python execution timeout must be positive and finite")
        if type(self.max_output_bytes) is not int or self.max_output_bytes <= 0:
            raise ValueError("Python execution output limit must be a positive integer")


@dataclass(frozen=True, kw_only=True)
class PythonExecutionResult:
    """Runner status supplied by the trusted adapter, never parsed from program output.

    ``completed`` means the process exited, including an ordinary nonzero exit;
    it says nothing about mathematical correctness. Other statuses may retain
    an observed exit code after cleanup, or None when no exit code is known.
    The handler separately bounds model-visible text; this does not replace
    the adapter's obligation to bound capture while the experiment runs.
    """

    status: PythonExecutionStatus
    stdout: str = ""
    stderr: str = ""
    returncode: int | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, str) or self.status not in _STATUSES:
            raise ValueError("Unsupported Python execution status")
        if not isinstance(self.stdout, str) or not isinstance(self.stderr, str):
            raise TypeError("Python execution stdout and stderr must be strings")
        if self.returncode is not None and type(self.returncode) is not int:
            raise TypeError("Python execution returncode must be an integer or None")
        if self.error is not None and not isinstance(self.error, str):
            raise TypeError("Python execution error must be a string or None")
        if self.status == "completed" and self.returncode is None:
            raise ValueError("Completed Python execution requires a returncode")
        if self.status == "infrastructure_error" and not (self.error and self.error.strip()):
            raise ValueError("Python infrastructure failure requires an error message")


class PythonExecutor(Protocol):
    """Interface implemented by a trusted local or deployment-specific runner.

    ``identity`` must be a stable JSON-safe dictionary identifying the trusted
    runner and its effective policy, suitable for the run manifest. It is
    deployment evidence, not a sandbox capability granted by this module.
    ``timeout`` and ``max_output_bytes`` are adapter upper bounds: the handler
    must not request more, and the adapter must reject requests exceeding them.
    """

    identity: dict[str, Any]
    timeout: float = DEFAULT_PYTHON_TIMEOUT
    max_output_bytes: int = DEFAULT_PYTHON_MAX_OUTPUT_BYTES

    async def preflight(self) -> None:
        """Validate the configured runner; raise on unavailable or invalid isolation.

        Successful validation must apply to the same runtime and policy used
        by execute. Never silently replace a failed runner with host execution.
        """
        ...

    async def execute(self, request: PythonExecutionRequest) -> PythonExecutionResult:
        """Execute within the requested limits and isolate all program output.

        The adapter owns clean environment/FD handling and the execution
        boundary. It must clean up every descendant before returning, including
        on timeout or output limit, and perform cleanup before propagating
        cancellation. Enforce the requested deadline and return bounded partial
        stdout/stderr after timeout cleanup. The handler's later watchdog allows
        a short cleanup margin; it does not extend the experiment's time limit.
        Setup failures must remain distinct from program errors.
        """
        ...
