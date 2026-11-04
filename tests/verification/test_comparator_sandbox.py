"""Exercise the actual process boundary used for feedback and final checking."""

import asyncio
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

import pytest

from dvp.verification.comparator import run_process, runtime_for, sandbox_command
from dvp.verification import sandbox


REPO = Path(__file__).resolve().parents[2]


def test_sandbox_cannot_change_source_read_host_processes_or_inherit_secrets(monkeypatch):
    workspace = REPO / "runtime/mathlib4-4.35"
    runtime_root = Path(os.environ.get("DVP_COMPARATOR_ROOT", REPO / "runtime/comparator"))
    if not (runtime_root / "4.35.0/runtime.json").is_file() or not workspace.is_dir():
        pytest.skip("installed Lean 4.35 comparator runtime is unavailable")
    runtime = runtime_for(workspace)
    if runtime["execution_mode"] != "sandboxed":
        pytest.skip("a sandboxed Comparator runtime is required")
    monkeypatch.setenv("DVP_SANDBOX_TEST_SECRET", "must-not-enter-child")
    with tempfile.TemporaryDirectory(prefix="dvp-sandbox-test-") as temporary:
        folder = Path(temporary)
        folder.chmod(0o755)
        sentinel = folder / "Challenge.lean"
        sentinel.write_text("immutable original source")
        # Ordinary Unix permissions would allow the sandbox UID to write it.
        # The read-only mount must independently block that write.
        sentinel.chmod(0o666)
        build = folder / ".lake"
        build.mkdir(mode=0o755)
        if os.geteuid() == 0:
            os.chown(build, sandbox.SANDBOX_UID, sandbox.SANDBOX_UID)
        program = """
import json, os, pathlib, socket
result = {'uid': os.geteuid(), 'secret_present': 'DVP_SANDBOX_TEST_SECRET' in os.environ}
try:
    pathlib.Path('Challenge.lean').write_text('changed objective')
    result['source_write_blocked'] = False
except OSError:
    result['source_write_blocked'] = True
try:
    socket.socket(socket.AF_UNIX, socket.SOCK_STREAM).close()
    result['socket_blocked'] = False
except OSError:
    result['socket_blocked'] = True
result['host_proc_environ_visible'] = pathlib.Path('/proc/self/environ').exists()
pathlib.Path('.lake/allowed-output').write_text('build artifact')
pathlib.Path('/tmp/private-output').write_text('private temporary output')
print(json.dumps(result))
"""
        command = sandbox_command(
            [shutil.which("python3"), "-c", program], workspace=folder,
            toolchain=Path(runtime["toolchain"]), writable=(build,),
        )
        captured = asyncio.run(run_process(command, cwd=folder, timeout=30))
        assert captured is not None
        returncode, stdout, stderr = captured
        assert returncode == 0, stderr
        result = json.loads(stdout)
        assert result["uid"] != 0
        assert not result["secret_present"]
        assert result["source_write_blocked"]
        assert result["socket_blocked"]
        assert not result["host_proc_environ_visible"]
        assert sentinel.read_text() == "immutable original source"
        assert (build / "allowed-output").read_text() == "build artifact"


def test_missing_sandbox_is_an_error_not_an_unsandboxed_fallback(tmp_path, monkeypatch):
    toolchain = tmp_path / "toolchain"
    (toolchain / "bin").mkdir(parents=True)
    (toolchain / "bin/lean").touch()
    monkeypatch.setattr(sys, "argv", [
        "sandbox.py", "--toolchain", str(toolchain), "--cwd", str(tmp_path), "--", "true",
    ])
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="unsandboxed execution is disabled"):
        sandbox.main()
