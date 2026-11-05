"""Exercise the actual process boundary used for feedback and final checking."""

import asyncio
import os
from pathlib import Path
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
        probe = folder / "Probe.lean"
        probe.write_text("""import Lean
def requireDenied (action : IO Unit) : IO Unit := do
  let succeeded ← try action; pure true catch _ => pure false
  if succeeded then throw (IO.userError "unexpected access")
#eval (show IO Unit from do
  unless (← IO.getEnv "DVP_SANDBOX_TEST_SECRET").isNone do
    throw (IO.userError "inherited secret")
  requireDenied (IO.FS.writeFile "Challenge.lean" "changed objective")
  requireDenied do
    let _ ← IO.FS.readFile "/proc/self/environ"
    pure ()
  IO.FS.writeFile ".lake/allowed-output" "build artifact"
  let some temporary ← IO.getEnv "TMPDIR"
    | throw (IO.userError "missing private temporary directory")
  IO.FS.writeFile (temporary ++ "/private-output") "private temporary output"
  IO.println "isolation passed")
""")
        command = sandbox_command(
            [str(Path(runtime["toolchain"]) / "bin/lean"), str(probe)], workspace=folder,
            toolchain=Path(runtime["toolchain"]), writable=(build,),
        )
        captured = asyncio.run(run_process(command, cwd=folder, timeout=30))
        assert captured is not None
        returncode, stdout, stderr = captured
        assert returncode == 0, stdout + stderr
        assert "isolation passed" in stdout
        assert sentinel.read_text() == "immutable original source"
        assert (build / "allowed-output").read_text() == "build artifact"
        assert (build / "allowed-output").stat().st_uid == (
            sandbox.SANDBOX_UID if os.geteuid() == 0 else os.geteuid()
        )


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


def test_bwrap_exposes_only_declared_paths_not_the_host_root(tmp_path, monkeypatch):
    toolchain, task, library, build = [tmp_path / name for name in ("toolchain", "task", "library", "build")]
    for path in (toolchain, task, library, build):
        path.mkdir()
    monkeypatch.setattr(sandbox, "trusted_read_paths", lambda _: (toolchain,))
    monkeypatch.setattr(sandbox, "_seccomp_filter", lambda: 42)
    monkeypatch.setattr(sandbox, "_drop_privileges", lambda: None)
    captured = {}
    def execve(binary, arguments, environment):
        captured.update(binary=binary, arguments=arguments, environment=environment)
    monkeypatch.setattr(sandbox.os, "execve", execve)
    sandbox._exec_bwrap(
        "/usr/bin/bwrap", [str(toolchain / "bin/lean"), "Probe.lean"],
        toolchain, task, (task, library), (build,), {"PATH": str(toolchain / "bin")},
    )
    arguments = captured["arguments"]
    mounts = [arguments[index:index + 3] for index, value in enumerate(arguments)
              if value in {"--ro-bind", "--bind"}]
    assert mounts == [
        ["--ro-bind", str(toolchain), str(toolchain)],
        ["--ro-bind", str(task), str(task)],
        ["--ro-bind", str(library), str(library)],
        ["--bind", str(build), str(build)],
    ]
    assert arguments[:3] == ["/usr/bin/bwrap", "--tmpfs", "/"]
    assert arguments[arguments.index("--seccomp") + 1] == "42"
    assert "--unshare-all" in arguments and "--clearenv" in arguments


@pytest.mark.parametrize("sandboxed", [False, True])
@pytest.mark.parametrize("grant", ["/", "/tmp", "/usr", "/proc", "/dev"])
def test_broad_read_grants_are_rejected_before_starting_child(tmp_path, monkeypatch, sandboxed, grant):
    toolchain = tmp_path / "toolchain"
    (toolchain / "bin").mkdir(parents=True)
    (toolchain / "bin/lean").touch()
    task = tmp_path / "task"
    task.mkdir()
    monkeypatch.setattr(sandbox.shutil, "which", lambda _: "/usr/bin/bwrap")

    def unexpected_fork():
        pytest.fail("unsafe read grant reached the child launcher")

    monkeypatch.setattr(sandbox.os, "fork", unexpected_fork)
    with pytest.raises(RuntimeError):
        sandbox.run_isolated(
            ["true"], toolchain=toolchain, cwd=task,
            read_only=(Path(grant),), sandboxed=sandboxed,
        )


@pytest.mark.parametrize("sandboxed", [False, True])
@pytest.mark.parametrize("grant", ["task", "parent", "toolchain", "toolchain_bin"])
def test_unsafe_write_grants_are_rejected_before_starting_child(tmp_path, monkeypatch, sandboxed, grant):
    toolchain = tmp_path / "toolchain"
    (toolchain / "bin").mkdir(parents=True)
    (toolchain / "bin/lean").touch()
    task = tmp_path / "task"
    task.mkdir()
    paths = {"task": task, "parent": tmp_path,
             "toolchain": toolchain, "toolchain_bin": toolchain / "bin"}
    monkeypatch.setattr(sandbox.shutil, "which", lambda _: "/usr/bin/bwrap")

    def unexpected_fork():
        pytest.fail("unsafe write grant reached the child launcher")

    monkeypatch.setattr(sandbox.os, "fork", unexpected_fork)
    with pytest.raises(RuntimeError):
        sandbox.run_isolated(
            ["true"], toolchain=toolchain, cwd=task,
            writable=(paths[grant],), sandboxed=sandboxed,
        )
