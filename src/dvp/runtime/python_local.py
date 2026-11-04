"""Local, single-process Python experiments with Landlock and seccomp.

The trusted bootstrap installs the kernel policy before running model code.
The parent handles resource supervision and never interprets program stdout as
sandbox status. The standard library and registered NumPy, SymPy and mpmath
packages are available; other packages, subprocesses, threads and network
access remain unavailable.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import tempfile
from typing import Any

from .python_execution import (
    DEFAULT_PYTHON_MAX_OUTPUT_BYTES,
    DEFAULT_PYTHON_TIMEOUT,
    PythonExecutionRequest,
    PythonExecutionResult,
)
from .python_packages import discover_packages, validate_packages


_POLICY = "landlock-seccomp-python-v2"
_WORKER = Path(__file__).with_name("python_worker.py")


def _sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _positive_timeout(value: float) -> float:
    if type(value) not in (int, float):
        raise ValueError("Python timeout must be positive and finite")
    try:
        valid = math.isfinite(value) and value > 0
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError("Python timeout must be positive and finite")
    return float(value)


async def _finish(task: asyncio.Task[Any]) -> Any:
    """Finish mandatory cleanup even if a caller repeats cancellation."""
    current = asyncio.current_task()
    already_cancelling = bool(current and current.cancelling())
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled and not already_cancelling:
        raise asyncio.CancelledError
    return result


class LocalPythonExecutor:
    """Run experiments on this machine; fail closed if isolation is unavailable."""

    stdlib_only = False
    allowed_packages = ("numpy", "sympy", "mpmath")

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_PYTHON_TIMEOUT,
        max_output_bytes: int = DEFAULT_PYTHON_MAX_OUTPUT_BYTES,
        memory_bytes: int = 1024 * 1024 * 1024,
    ) -> None:
        self.timeout = _positive_timeout(timeout)
        if type(max_output_bytes) is not int or max_output_bytes <= 0:
            raise ValueError("Python output limit must be a positive integer")
        if type(memory_bytes) is not int or memory_bytes < 64 * 1024 * 1024:
            raise ValueError("Python memory limit must be at least 64 MiB")
        self.max_output_bytes = max_output_bytes
        self.memory_bytes = memory_bytes
        # Keep the venv executable spelling: resolving the symlink changes Python's
        # prefix detection. Record the resolved binary separately for identity.
        self._executable = Path(os.path.abspath(sys.executable))
        self._worker = _WORKER.resolve(strict=True)
        self._packages = discover_packages()
        self._files = {
            "worker": self._worker,
            "supervisor": Path(__file__).resolve(strict=True),
            "interpreter": self._executable.resolve(strict=True),
            "package_policy": Path(__file__).with_name("python_packages.py").resolve(strict=True),
        }
        self._hashes = {name: _sha256(path) for name, path in self._files.items()}
        self._limits = (self.timeout, self.max_output_bytes, self.memory_bytes)
        self._identity = {
            "backend": "local_landlock_seccomp",
            "policy": _POLICY,
            "stdlib_only": False,
            "packages": copy.deepcopy(self._packages),
            "executable": str(self._executable),
            "python_version": sys.version,
            "python_base_prefix": sys.base_prefix,
            "platform": sys.platform,
            "kernel_release": os.uname().release if hasattr(os, "uname") else "",
            "files": {
                name: {"path": str(path), "sha256": self._hashes[name]}
                for name, path in self._files.items()
            },
            "timeout": self.timeout,
            "max_output_bytes": self.max_output_bytes,
            "memory_bytes": self.memory_bytes,
        }
        self._preflight_lock = asyncio.Lock()
        self._preflight_ok = False

    @property
    def identity(self) -> dict[str, Any]:
        return copy.deepcopy(self._identity)

    def _validate_runtime(self) -> None:
        if sys.platform != "linux":
            raise RuntimeError("Local Python isolation requires Linux")
        if (self.timeout, self.max_output_bytes, self.memory_bytes) != self._limits:
            raise RuntimeError("Python execution policy changed; create a new executor")
        if self._executable.resolve(strict=True) != self._files["interpreter"]:
            raise RuntimeError("Python interpreter changed since executor registration")
        for name, path in self._files.items():
            if _sha256(path) != self._hashes[name]:
                raise RuntimeError(f"Python {name} changed since executor registration")
        validate_packages(self._packages)

    async def preflight(self) -> None:
        """Exercise the deployed policy once, then revalidate immutable code identity."""
        self._validate_runtime()
        async with self._preflight_lock:
            if self._preflight_ok:
                return
            with tempfile.TemporaryDirectory(prefix="dvp-python-preflight-") as root:
                base = Path(root)
                workspace = base / "task"
                workspace.mkdir()
                sentinel = base / "outside.txt"
                sentinel.write_text("private-preflight-sentinel", encoding="utf-8")
                (workspace / "outside-link").symlink_to(sentinel)
                # This is trusted, fixed test code, not model output. It tests
                # ordinary capabilities without touching real datasets/secrets.
                code = """
import math, os, socket, sys
from pathlib import Path

def denied(action):
    try:
        action()
    except PermissionError:
        return
    raise RuntimeError("isolation probe unexpectedly succeeded")

assert math.factorial(7) == 5040
import numpy as np
import sympy as sp
import mpmath as mp
assert np.allclose(np.linalg.solve([[2., 0.], [0., 4.]], [6., 8.]), [3., 2.])
x = sp.Symbol("x")
assert sp.integrate(x ** 2, (x, 0, 1)) == sp.Rational(1, 3)
assert mp.almosteq(mp.sqrt(2) ** 2, 2)
Path("allowed.txt").write_text("ok")
assert Path("allowed.txt").read_text() == "ok"
denied(lambda: Path(OUTSIDE).read_text())
denied(lambda: Path(OUTSIDE).write_text("changed"))
denied(lambda: os.open(OUTSIDE, os.O_RDONLY | os.O_TRUNC))
denied(lambda: os.truncate(OUTSIDE, 0))
denied(lambda: Path("outside-link").read_text())
denied(lambda: Path("/proc/self/environ").read_bytes())
denied(lambda: socket.socket())
if hasattr(os, "fork"):
    denied(os.fork)
denied(lambda: os.execv(sys.executable, [sys.executable, "-c", "raise SystemExit(71)"]))
assert set(os.environ) <= {"HOME", "TMPDIR", "LANG", "LC_ALL", "PATH", "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "BLIS_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS", "SYMPY_GROUND_TYPES", "MPMATH_NOGMPY"}
os.write(1, b"1")
""".replace("OUTSIDE", repr(str(sentinel)))
                result = await self.execute(PythonExecutionRequest(
                    code=code,
                    workspace=workspace,
                    timeout=min(self.timeout, 5.0),
                    max_output_bytes=self.max_output_bytes,
                ))
                if (
                    result.status != "completed"
                    or result.returncode != 0
                    or result.stdout != "1"
                    or sentinel.read_text(encoding="utf-8") != "private-preflight-sentinel"
                ):
                    detail = "; ".join(
                        value.strip() for value in (result.error, result.stderr) if value and value.strip()
                    ) or result.status
                    raise RuntimeError(f"Local Python isolation preflight failed: {detail[:1200]}")
            self._preflight_ok = True

    async def execute(self, request: PythonExecutionRequest) -> PythonExecutionResult:
        if not isinstance(request, PythonExecutionRequest):
            raise TypeError("Expected PythonExecutionRequest")
        deadline = asyncio.get_running_loop().time() + request.timeout
        self._validate_runtime()
        if request.timeout > self.timeout or request.max_output_bytes > self.max_output_bytes:
            raise ValueError("Python execution request exceeds configured limits")
        data = request.code.encode("utf-8")
        if not data or len(data) > 65_536:
            raise ValueError("Python code must contain 1 to 65536 UTF-8 bytes")
        if request.workspace.is_symlink():
            raise ValueError("Python workspace must not be a symlink")
        workspace = request.workspace.resolve(strict=True)
        if not workspace.is_dir() or workspace == Path("/"):
            raise ValueError("Python workspace must be an existing task directory")
        if any(path.is_relative_to(workspace) for path in self._files.values()):
            raise ValueError("Python workspace must not contain its trusted runtime")
        return await self._run(request, data, workspace, deadline=deadline)

    async def _run(
        self, request: PythonExecutionRequest, code: bytes, workspace: Path,
        *, deadline: float,
    ) -> PythonExecutionResult:
        read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
        status_file = os.fdopen(read_fd, "rb", buffering=0)
        status_transport = None
        proc: asyncio.subprocess.Process | None = None
        tasks: list[asyncio.Task[Any]] = []
        stdout = bytearray()
        stderr = bytearray()
        output_limit = False
        timed_out = False
        acknowledgement = b""
        setup_error: str | None = None
        environment = {
            "HOME": str(workspace), "TMPDIR": str(workspace), "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8", "PATH": "", "OMP_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
        }

        async def cleanup() -> None:
            if proc is not None and proc.returncode is None:
                with suppress(ProcessLookupError):
                    proc.kill()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if proc is not None:
                async def discard(reader: asyncio.StreamReader | None) -> None:
                    if reader is not None:
                        while await reader.read(8192):
                            pass
                # Draining after cancellation matters: a paused pipe transport
                # can otherwise prevent Process.wait() from completing even
                # after the process was killed.
                await asyncio.gather(discard(proc.stdout), discard(proc.stderr), proc.wait())

        try:
            status_reader = asyncio.StreamReader(limit=128)
            status_transport, _ = await asyncio.get_running_loop().connect_read_pipe(
                lambda: asyncio.StreamReaderProtocol(status_reader), status_file,
            )
            command = [
                str(self._executable), "-I", "-S", "-B", "-u", str(self._worker),
                "--workspace", str(workspace), "--timeout", str(request.timeout),
                "--memory-bytes", str(self.memory_bytes), "--status-fd", str(write_fd),
                "--packages-json", json.dumps(self._packages, separators=(",", ":")),
            ]
            # Shield spawn so cancellation cannot lose a successfully created
            # process before we have its handle and can reap it.
            spawn = asyncio.create_task(asyncio.create_subprocess_exec(
                *command, cwd=workspace, env=environment,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, close_fds=True,
                pass_fds=(write_fd,), start_new_session=True, limit=8192,
            ))
            try:
                proc = await asyncio.shield(spawn)
            except asyncio.CancelledError:
                try:
                    proc = await _finish(spawn)
                except Exception:
                    # A failed spawn has no child to reap; preserve the
                    # original cancellation instead of reporting startup error.
                    pass
                raise
            finally:
                os.close(write_fd)
                write_fd = -1

            async def send_code() -> None:
                assert proc is not None and proc.stdin is not None
                try:
                    proc.stdin.write(code)
                    await proc.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    proc.stdin.close()
                    with suppress(BrokenPipeError, ConnectionResetError):
                        await proc.stdin.wait_closed()

            async def read_status() -> None:
                nonlocal acknowledgement
                while True:
                    chunk = await status_reader.read(64)
                    if not chunk:
                        break
                    acknowledgement += chunk
                    if len(acknowledgement) > 64:
                        raise RuntimeError("Invalid Python sandbox setup response")

            async def capture(reader: asyncio.StreamReader, buffer: bytearray) -> None:
                nonlocal output_limit
                while True:
                    chunk = await reader.read(4096)
                    if not chunk:
                        return
                    available = request.max_output_bytes - len(stdout) - len(stderr)
                    buffer.extend(chunk[:available])
                    if len(chunk) > available:
                        output_limit = True
                        if proc is not None and proc.returncode is None:
                            with suppress(ProcessLookupError):
                                proc.kill()

            assert proc.stdout is not None and proc.stderr is not None
            tasks = [
                asyncio.create_task(send_code()), asyncio.create_task(read_status()),
                asyncio.create_task(capture(proc.stdout, stdout)),
                asyncio.create_task(capture(proc.stderr, stderr)),
                asyncio.create_task(proc.wait()),
            ]
            try:
                async with asyncio.timeout_at(deadline):
                    await asyncio.gather(*tasks)
            except TimeoutError:
                timed_out = True
            except Exception as exc:
                setup_error = f"Python supervisor failed ({type(exc).__name__})"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            setup_error = f"Could not start isolated Python ({type(exc).__name__})"
        finally:
            try:
                await _finish(asyncio.create_task(cleanup()))
            finally:
                if write_fd >= 0:
                    os.close(write_fd)
                if status_transport is not None:
                    status_transport.close()
                else:
                    status_file.close()

        common = {
            "stdout": bytes(stdout).decode("utf-8", errors="ignore"),
            "stderr": bytes(stderr).decode("utf-8", errors="ignore"),
            "returncode": proc.returncode if proc is not None else None,
        }
        if setup_error or acknowledgement != b"OK\n":
            return PythonExecutionResult(
                status="infrastructure_error",
                error=setup_error or "Landlock/seccomp Python sandbox setup failed",
                **common,
            )
        if output_limit:
            return PythonExecutionResult(status="output_limit", **common)
        if timed_out:
            return PythonExecutionResult(status="timed_out", **common)
        return PythonExecutionResult(status="completed", **common)
