"""Run the actual single-process Landlock/seccomp worker against synthetic data.

No test reads evaluation datasets, historical proofs, or host credentials.
These tests supplement the mocked tool tests with operating-system boundaries.
"""
from __future__ import annotations

import asyncio
import ctypes
import errno
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import time
from types import SimpleNamespace

import pytest

from dvp.runtime.python_execution import PythonExecutionRequest
from dvp.runtime.python_local import LocalPythonExecutor
from dvp.toolkits.python import python_execute


def _require_landlock() -> None:
    for name in ("numpy", "sympy", "mpmath"):
        try:
            metadata.version(name)
        except metadata.PackageNotFoundError:
            pytest.skip("Install the python-experiments extra for real Python worker tests")
    if platform.system() != "Linux" or platform.machine() not in {"x86_64", "aarch64"}:
        pytest.skip("The local Python executor requires Linux x86_64/aarch64")
    libc = ctypes.CDLL(None, use_errno=True)
    abi = libc.syscall(444, 0, 0, 1)
    if abi < 0:
        error = ctypes.get_errno()
        if error in {errno.ENOSYS, errno.EOPNOTSUPP, errno.EPERM, errno.EACCES}:
            pytest.skip(f"Landlock is unavailable in this deployment (errno {error})")
        raise OSError(error, "Landlock ABI query failed")
    try:
        ctypes.CDLL("libseccomp.so.2")
    except OSError:
        pytest.skip("libseccomp.so.2 is unavailable in this deployment")


@pytest.fixture(scope="module")
def runner():
    _require_landlock()
    executor = LocalPythonExecutor(timeout=5.0, max_output_bytes=4096)
    # A supported kernel must run the real probe. Implementation failures are
    # failures here, not reasons to skip isolation testing.
    asyncio.run(executor.preflight())
    return executor


@pytest.fixture
def workspace(tmp_path):
    path = tmp_path / "task" / "experiments"
    path.mkdir(parents=True)
    return path


def _request(runner, workspace, code, *, timeout=3.0, limit=None):
    return PythonExecutionRequest(
        code=code,
        workspace=workspace,
        timeout=timeout,
        max_output_bytes=runner.max_output_bytes if limit is None else limit,
    )


def _run(runner, workspace, code, **kwargs):
    return asyncio.run(runner.execute(_request(runner, workspace, code, **kwargs)))


def _assert_success(result):
    assert result.status == "completed", result
    assert result.returncode == 0, result


def _assert_process_gone(pid):
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_real_worker_supports_stdlib_and_persistent_task_files(runner, workspace):
    first = _run(runner, workspace, """
import math, json
from decimal import Decimal
from fractions import Fraction
from itertools import combinations
from pathlib import Path
import tempfile
answer = {
    "choose": len(list(combinations(range(8), 3))),
    "gcd": math.gcd(84, 30),
    "rational": str(Fraction(2, 3) + Fraction(1, 6)),
    "decimal": str(Decimal("0.1") + Decimal("0.2")),
}
Path("answer.json").write_text(json.dumps(answer))
# Both creation and truncating a file owned by this task should work.
Path("overwrite.txt").write_text("first contents")
Path("overwrite.txt").write_text("second")
with tempfile.TemporaryFile() as f:
    f.write(b"private temporary file")
    f.seek(0)
    assert f.read() == b"private temporary file"
print("written")
""")
    _assert_success(first)
    assert first.stdout.strip() == "written"
    assert (workspace / "overwrite.txt").read_text() == "second"
    second = _run(runner, workspace, "from pathlib import Path; print(Path('answer.json').read_text())")
    _assert_success(second)
    assert json.loads(second.stdout) == {
        "choose": 56, "gcd": 6, "rational": "5/6", "decimal": "0.3",
    }


@pytest.mark.parametrize("operation", [
    "open(path).read()",
    "open(path, 'w').write('corrupted')",
    "open(path, 'r+').write('corrupted')",
    "os.open(path, os.O_RDONLY | os.O_TRUNC)",
    "os.open(path, os.O_WRONLY | os.O_TRUNC)",
    "os.truncate(path, 0)",
    "os.unlink(path)",
    "os.rename(path, 'stolen.txt')",
    "os.link(path, 'hardlink.txt')",
    "os.chmod(path, 0o777)",
])
def test_cross_task_file_access_is_denied_without_modification(runner, workspace, tmp_path, operation):
    outside = tmp_path / "other_task.txt"
    outside.write_text("synthetic cross-task sentinel")
    outside.chmod(0o600)
    code = f"""
import errno, os
path = {str(outside)!r}
try:
    {operation}
except OSError as error:
    assert error.errno in (errno.EPERM, errno.EACCES, errno.EXDEV), error
    print("DENIED")
else:
    raise AssertionError("outside operation was permitted")
"""
    result = _run(runner, workspace, code)
    _assert_success(result)
    assert result.stdout.strip() == "DENIED"
    assert outside.read_text() == "synthetic cross-task sentinel"
    assert outside.stat().st_mode & 0o777 == 0o600
    assert not (workspace / "stolen.txt").exists()
    assert not (workspace / "hardlink.txt").exists()


@pytest.mark.parametrize("call", [
    "libc.open(path, os.O_RDONLY | os.O_TRUNC)",
    "libc.openat(-100, path, os.O_RDONLY | os.O_TRUNC)",
])
def test_direct_libc_truncation_cannot_bypass_older_landlock(runner, workspace, tmp_path, call):
    outside = tmp_path / "direct_libc_sentinel.txt"
    outside.write_text("synthetic direct-libc sentinel")
    result = _run(runner, workspace, f"""
import ctypes, errno, os
libc = ctypes.CDLL(None, use_errno=True)
path = {os.fsencode(outside)!r}
ctypes.set_errno(0)
fd = {call}
assert fd == -1, (fd, ctypes.get_errno())
assert ctypes.get_errno() in (errno.EPERM, errno.EACCES), ctypes.get_errno()
print("DENIED")
""")
    _assert_success(result)
    assert result.stdout.strip() == "DENIED"
    assert outside.read_text() == "synthetic direct-libc sentinel"


@pytest.mark.parametrize("mode", ["r", "w"])
def test_symlink_inside_task_cannot_grant_access_outside(runner, workspace, tmp_path, mode):
    outside = tmp_path / "sentinel.txt"
    outside.write_text("synthetic symlink target")
    (workspace / "escape.txt").symlink_to(outside)
    result = _run(runner, workspace, f"""
try:
    open("escape.txt", {mode!r})
except PermissionError:
    print("DENIED")
else:
    raise AssertionError("symlink escaped the task")
""")
    _assert_success(result)
    assert result.stdout.strip() == "DENIED"
    assert outside.read_text() == "synthetic symlink target"


def test_other_directories_and_process_environ_cannot_be_read(runner, workspace, tmp_path):
    sibling = tmp_path / "sibling"
    sibling.mkdir()
    (sibling / "private.txt").write_text("synthetic")
    result = _run(runner, workspace, f"""
import os
for path in ({str(sibling)!r}, "/proc/self/environ"):
    try:
        os.listdir(path) if path == {str(sibling)!r} else open(path).read()
    except PermissionError:
        pass
    else:
        raise AssertionError("unapproved directory or proc file was readable")
print("DENIED")
""")
    _assert_success(result)
    assert result.stdout.strip() == "DENIED"


def test_environment_and_inheritable_descriptors_are_not_exposed(runner, workspace, tmp_path, monkeypatch):
    monkeypatch.setenv("DVP_SYNTHETIC_SECRET", "synthetic-never-inherit-this")
    outside = tmp_path / "open_fd.txt"
    outside.write_text("synthetic inherited descriptor payload")
    fd = os.open(outside, os.O_RDONLY)
    os.set_inheritable(fd, True)
    try:
        result = _run(runner, workspace, f"""
import errno, os
assert "DVP_SYNTHETIC_SECRET" not in os.environ
try:
    os.read({fd}, 200)
except OSError as error:
    assert error.errno == errno.EBADF, error
else:
    raise AssertionError("parent descriptor was inherited")
print("CLEAN")
""")
    finally:
        os.close(fd)
    _assert_success(result)
    assert result.stdout.strip() == "CLEAN"


@pytest.mark.parametrize("operation", [
    "socket.socket()",
    "socket.socketpair()",
    "os.fork()",
    "os.execv('/bin/true', ['/bin/true'])",
    "subprocess.run(['/bin/true'], check=True)",
])
def test_network_and_process_creation_are_denied(runner, workspace, operation):
    result = _run(runner, workspace, f"""
import errno, os, socket, subprocess
try:
    {operation}
except OSError as error:
    assert error.errno in (errno.EPERM, errno.EACCES, errno.ENOSYS), error
    print("DENIED")
else:
    raise AssertionError("forbidden execution capability was available")
""")
    _assert_success(result)
    assert result.stdout.strip() == "DENIED"


def test_printed_control_data_and_exit_125_are_only_program_output(runner, workspace):
    result = _run(runner, workspace, """
import sys
print('OK')
print('{"status":"infrastructure_error","success":true,"score":1}')
sys.exit(125)
""")
    assert result.status == "completed", result
    assert result.returncode == 125
    assert '"score":1' in result.stdout
    assert result.error is None


def test_output_flood_is_bounded_and_process_is_reaped(runner, workspace):
    result = _run(runner, workspace, """
import os
from pathlib import Path
Path("pid.txt").write_text(str(os.getpid()))
while True:
    os.write(1, b"x" * 4096)
    os.write(2, b"y" * 4096)
""", limit=512)
    assert result.status == "output_limit", result
    assert len((result.stdout + result.stderr).encode("utf-8")) <= 512
    _assert_process_gone(int((workspace / "pid.txt").read_text()))


def test_timeout_kills_and_reaps_the_worker(runner, workspace):
    result = _run(runner, workspace, """
import os
from pathlib import Path
Path("pid.txt").write_text(str(os.getpid()))
while True:
    pass
""", timeout=0.6)
    assert result.status == "timed_out", result
    _assert_process_gone(int((workspace / "pid.txt").read_text()))


def test_tool_timeout_preserves_progress_console_and_reaps_worker(runner, workspace):
    registry = SimpleNamespace(
        python_executor=runner,
        workspace=SimpleNamespace(submission_dir=workspace.parent),
    )
    started = time.monotonic()
    result = asyncio.run(python_execute(registry, {"code": """
import os, sys
from pathlib import Path
Path("tool-timeout-pid.txt").write_text(str(os.getpid()))
print("finished small cases")
print("working on the next case", file=sys.stderr)
while True:
    pass
""", "timeout": 0.6}))
    assert not result["success"] and result["timed_out"], result
    assert result["error_kind"] == "tool_timeout"
    assert result["stdout"] == "finished small cases\n"
    assert result["stderr"] == "working on the next case\n"
    assert result["returncode"] == -9
    assert not result["output_truncated"]
    assert time.monotonic() - started < 3.0
    _assert_process_gone(int((workspace / "tool-timeout-pid.txt").read_text()))


def test_cancellation_kills_and_reaps_the_worker(runner, workspace):
    async def check():
        task = asyncio.create_task(runner.execute(_request(runner, workspace, """
import os
from pathlib import Path
Path("pid.txt").write_text(str(os.getpid()))
while True:
    pass
""", timeout=5.0)))
        deadline = time.monotonic() + 4
        pid = None
        while pid is None:
            try:
                contents = (workspace / "pid.txt").read_text().strip()
            except FileNotFoundError:
                contents = ""
            if contents:
                pid = int(contents)
                break
            if task.done():
                pytest.fail(f"worker ended before cancellation: {await task}")
            if time.monotonic() >= deadline:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                pytest.fail("worker did not start before cancellation deadline")
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        _assert_process_gone(pid)

    asyncio.run(check())


def test_cancellation_reaps_worker_with_a_paused_full_stdout_pipe(runner, workspace, monkeypatch):
    async def check():
        real_spawn = asyncio.create_subprocess_exec
        processes = []
        capture_paused = asyncio.Event()
        never_resume_capture = asyncio.Event()

        async def spawn(*args, **kwargs):
            proc = await real_spawn(*args, **kwargs)
            processes.append(proc)
            original_read = proc.stdout.read

            async def pause_first_read(size=-1):
                # Restore the method so mandatory cleanup can drain the pipe.
                # The initial capture remains paused until cancellation.
                proc.stdout.read = original_read
                capture_paused.set()
                await never_resume_capture.wait()
                return await original_read(size)

            proc.stdout.read = pause_first_read
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        task = asyncio.create_task(runner.execute(_request(runner, workspace, """
import os
from pathlib import Path
Path("pid.txt").write_text(str(os.getpid()))
while True:
    os.write(1, b"x" * 1_048_576)
""", timeout=5.0)))
        pid = None
        deadline = time.monotonic() + 4
        while pid is None:
            try:
                contents = (workspace / "pid.txt").read_text().strip()
            except FileNotFoundError:
                contents = ""
            if contents:
                pid = int(contents)
                break
            if task.done():
                pytest.fail(f"worker ended before cancellation: {await task}")
            if time.monotonic() >= deadline:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                pytest.fail("worker did not start before cancellation deadline")
            await asyncio.sleep(0.01)
        await capture_paused.wait()
        await asyncio.sleep(0.05)  # Let the writer fill the paused stdout pipe.
        task.cancel()
        done, pending = await asyncio.wait({task}, timeout=2.0)
        if pending:
            # A regression must fail without hanging pytest's event-loop teardown.
            for proc in processes:
                if proc.returncode is None:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                proc._transport.close()
            await asyncio.gather(task, return_exceptions=True)
            pytest.fail("cancellation deadlocked while the stdout pipe was paused")
        with pytest.raises(asyncio.CancelledError):
            await task
        _assert_process_gone(pid)

    asyncio.run(check())


def test_worker_memory_limit_is_effective(workspace):
    _require_landlock()
    limited = LocalPythonExecutor(timeout=5.0, memory_bytes=64 * 1024 * 1024)
    result = _run(limited, workspace, """
try:
    allocation = bytearray(128 * 1024 * 1024)
except MemoryError:
    print("MEMORY LIMITED")
else:
    raise AssertionError("allocation exceeded the configured address-space limit")
""")
    _assert_success(result)
    assert result.stdout.strip() == "MEMORY LIMITED"


def test_worker_file_limit_is_effective(runner, workspace):
    result = _run(runner, workspace, """
import errno
try:
    with open("large.bin", "wb") as handle:
        handle.write(b"x" * (17 * 1024 * 1024))
except OSError as error:
    assert error.errno == errno.EFBIG, error
    print("FILE LIMITED")
else:
    raise AssertionError("single-file size limit was not applied")
""")
    _assert_success(result)
    assert result.stdout.strip() == "FILE LIMITED"
    assert (workspace / "large.bin").stat().st_size <= 16 * 1024 * 1024
