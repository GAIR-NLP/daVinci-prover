"""Real Lean isolation checks for the runtime selected by DVP_COMPARATOR_ROOT."""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import pytest

from dvp.verification import comparator, sandbox
from dvp.verification.lean import LeanVerificationConfig, LeanVerifier


REPO = Path(__file__).resolve().parents[2]
DENY = """def requireDenied (name : String) (action : IO Unit) : IO Unit := do
  let succeeded ← try
    action
    pure true
  catch _ => pure false
  if succeeded then
    throw (IO.userError s!"isolation failure: {name}")
  IO.println s!"blocked: {name}"
"""


@pytest.fixture
def installed_runtime():
    workspace = Path(os.environ.get("DAVINCI_TARGET_WORKSPACE", REPO / "runtime/mathlib4-4.35"))
    if not workspace.is_dir():
        pytest.skip("installed Lean 4.35 Mathlib workspace unavailable")
    try:
        runtime = comparator.runtime_for(workspace)
    except (OSError, RuntimeError, ValueError, KeyError) as error:
        pytest.skip(f"registered Lean 4.35 runtime unavailable: {error}")
    return workspace.resolve(), runtime


@pytest.fixture
def isolated_paths():
    # Test ancestors must permit the sandbox UID; pytest's private tmp_path
    # would otherwise hide a failed filesystem policy behind Unix permissions.
    with tempfile.TemporaryDirectory(prefix="dvp-lean-isolation-test-") as temporary:
        root = Path(temporary)
        root.chmod(0o755)
        task, other = root / "task", root / "other-task"
        task.mkdir(mode=0o755)
        other.mkdir(mode=0o755)
        secret = other / "prior-solution.txt"
        secret.write_text("synthetic-other-task-marker")
        secret.chmod(0o666)
        source = task / "input.txt"
        source.write_text("current-task-input")
        source.chmod(0o666)
        (task / "escape.txt").symlink_to(secret)
        build = task / ".lake"
        build.mkdir(mode=0o755)
        if os.geteuid() == 0:
            os.chown(build, sandbox.SANDBOX_UID, sandbox.SANDBOX_UID)
        yield task, other, secret, source, build


def capture(command, cwd, timeout=90):
    result = asyncio.run(comparator.run_process(command, cwd=cwd, timeout=timeout))
    assert result is not None, "isolated Lean process timed out"
    return result


def test_lean_preserves_allowed_io_and_denies_cross_task_io(
    installed_runtime, isolated_paths, monkeypatch,
):
    _, runtime = installed_runtime
    task, _, secret, source, build = isolated_paths
    monkeypatch.setenv("DVP_ISOLATION_TEST_SECRET", "synthetic-inherited-secret")
    probe = task / "Probe.lean"
    probe.write_text("import Lean\n" + DENY + f"""
#eval do
  unless (← IO.FS.readFile {json.dumps(str(source))}) == "current-task-input" do
    throw (IO.userError "allowed source read failed")
  unless (← IO.getEnv "DVP_ISOLATION_TEST_SECRET").isNone do
    throw (IO.userError "inherited environment was not cleared")
  requireDenied "other task read" do
    let _ ← IO.FS.readFile {json.dumps(str(secret))}
    pure ()
  requireDenied "symlink read" do
    let _ ← IO.FS.readFile {json.dumps(str(task / 'escape.txt'))}
    pure ()
  requireDenied "host process contents" do
    let _ ← IO.FS.readFile "/proc/self/environ"
    pure ()
  requireDenied "source write" do
    IO.FS.writeFile {json.dumps(str(source))} "changed"
  requireDenied "other task write" do
    IO.FS.writeFile {json.dumps(str(secret))} "changed"
  IO.FS.writeFile {json.dumps(str(build / 'allowed-output'))} "build artifact"
  let some temporary ← IO.getEnv "TMPDIR"
    | throw (IO.userError "missing private temporary directory")
  IO.FS.writeFile (temporary ++ "/allowed-temporary-output") "temporary artifact"
  IO.println "allowed I/O completed"
""")
    command = comparator.sandbox_command(
        [str(Path(runtime["toolchain"]) / "bin/lean"), str(probe)],
        workspace=task, toolchain=Path(runtime["toolchain"]),
        writable=(build,), sandboxed=runtime["execution_mode"] == "sandboxed",
    )
    status, stdout, stderr = capture(command, task)
    assert status == 0, stdout + stderr
    assert "allowed I/O completed" in stdout
    assert stdout.count("blocked:") == 5
    assert source.read_text() == "current-task-input"
    assert secret.read_text() == "synthetic-other-task-marker"
    assert (build / "allowed-output").read_text() == "build artifact"
    assert (build / "allowed-output").stat().st_uid == (
        sandbox.SANDBOX_UID if os.geteuid() == 0 else os.geteuid()
    )


def test_lean_spawned_child_inherits_cross_task_read_restriction(installed_runtime, isolated_paths):
    _, runtime = installed_runtime
    task, _, secret, _, _ = isolated_paths
    lean = Path(runtime["toolchain"]) / "bin/lean"
    child = task / "Child.lean"
    child.write_text("import Lean\n" + DENY + f"""
#eval requireDenied "child other task read" do
  let _ ← IO.FS.readFile {json.dumps(str(secret))}
  pure ()
""")
    parent = task / "Parent.lean"
    parent.write_text(f"""import Lean
#eval do
  let result ← IO.Process.output {{
    cmd := {json.dumps(str(lean))}
    args := #[{json.dumps(str(child))}]
  }}
  IO.print result.stdout
  IO.eprint result.stderr
  unless result.exitCode == 0 do
    throw (IO.userError "isolated child failed")
""")
    command = comparator.sandbox_command(
        [str(lean), str(parent)], workspace=task,
        toolchain=Path(runtime["toolchain"]),
        sandboxed=runtime["execution_mode"] == "sandboxed",
    )
    status, stdout, stderr = capture(command, task)
    assert status == 0, stdout + stderr
    assert "blocked: child other task read" in stdout


def test_kernel_policy_blocks_readonly_truncate_and_closes_inherited_fds(
    installed_runtime, isolated_paths,
):
    _, runtime = installed_runtime
    task, _, secret, source, build = isolated_paths
    # This trusted test supervisor loads Python before restricting itself. It
    # does not add the Python interpreter or its packages to Lean's allowlist.
    program = f"""import errno, os, socket, sys
from pathlib import Path
sys.path.insert(0, {str(REPO / 'src/dvp/verification')!r})
from sandbox import _drop_privileges
from lean_isolation import isolate
descriptor = os.open({str(secret)!r}, os.O_RDONLY)
_drop_privileges()
isolate(Path({runtime['toolchain']!r}), (Path({str(task)!r}),), (Path({str(build)!r}),))
try:
    os.read(descriptor, 1)
except OSError as error:
    assert error.errno == errno.EBADF
    print('inherited descriptor closed')
else:
    raise SystemExit('inherited descriptor bypassed isolation')
try:
    descriptor = os.open({str(source)!r}, os.O_RDONLY | os.O_TRUNC)
except PermissionError:
    print('readonly truncation blocked')
else:
    os.close(descriptor)
    raise SystemExit('readonly truncation bypassed isolation')
for family in (socket.AF_INET, socket.AF_UNIX):
    try:
        connection = socket.socket(family, socket.SOCK_STREAM)
    except PermissionError:
        continue
    connection.close()
    raise SystemExit('socket creation bypassed isolation')
print('network and unix sockets blocked')
"""
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", program],
        text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "inherited descriptor closed" in result.stdout
    assert "readonly truncation blocked" in result.stdout
    assert "network and unix sockets blocked" in result.stdout
    assert source.read_text() == "current-task-input"


def test_feedback_compiles_mathlib_but_cannot_read_another_task(installed_runtime, isolated_paths):
    workspace, _ = installed_runtime
    task, _, secret, _, _ = isolated_paths
    verifier = LeanVerifier(LeanVerificationConfig(
        target_workspace=workspace, scratch_workspace=task, timeout=90,
    ))
    code = "import Mathlib\n" + DENY + f"""
#eval requireDenied "feedback other task read" do
  let _ ← IO.FS.readFile {json.dumps(str(secret))}
  pure ()
example : (1 : ℕ) + 1 = 2 := by norm_num
"""
    result = asyncio.run(verifier.verify_code(code))
    assert result["success"], result
    assert "blocked: feedback other task read" in result["output"]


@pytest.mark.parametrize("failed_setup", ["_landlock", "_seccomp"])
@pytest.mark.parametrize("wrapped_stderr", [False, True])
def test_isolation_setup_failure_never_executes_candidate(
    installed_runtime, isolated_paths, failed_setup, wrapped_stderr,
):
    _, runtime = installed_runtime
    task, _, _, _, build = isolated_paths
    sentinel = build / "must-not-execute"

    # A separate supervisor keeps child setup failures and fd closure away from
    # pytest's own capture handles. Wrapped stderr exercises embedded callers.
    program = f"""import os, sys
from pathlib import Path
sys.path.insert(0, {str(REPO / 'src/dvp/verification')!r})
import lean_isolation, sandbox
def unavailable(*args, **kwargs):
    raise RuntimeError('synthetic {failed_setup} unavailable')
setattr(lean_isolation, {failed_setup!r}, unavailable)
if {wrapped_stderr!r}:
    sys.stderr = os.fdopen(os.dup(2), 'w')
status = sandbox.run_isolated(
    ['/bin/sh', '-c', 'printf executed > "$1"', 'sh', {str(sentinel)!r}],
    toolchain=Path({runtime['toolchain']!r}), cwd=Path({str(task)!r}),
    writable=(Path({str(build)!r}),), sandboxed=False,
)
raise SystemExit(status)
"""
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", program],
        text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == 125, result.stdout + result.stderr
    assert f"synthetic {failed_setup} unavailable" in result.stderr
    assert not sentinel.exists()


def test_final_comparator_cannot_read_sibling_but_accepts_valid_mathlib_proof(
    installed_runtime, isolated_paths,
):
    workspace, selected_runtime = installed_runtime
    task, _, secret, _, _ = isolated_paths
    project = task / "comparison"
    project.mkdir()
    statement = "import Mathlib\ntheorem target : (1 : ℕ) + 1 = 2 := by sorry\n"
    solution = "import Mathlib\n" + DENY + f"""
#eval requireDenied "comparator other task read" do
  let _ ← IO.FS.readFile {json.dumps(str(secret))}
  pure ()
#eval (show IO Unit from do
  let entries ← try (System.FilePath.mk "/tmp").readDir catch _ => pure #[]
  if entries.any (fun entry => entry.fileName.startsWith "dvp-comparator-private-") then
    throw (IO.userError "candidate can enumerate Comparator export directories")
  IO.println "private Comparator export directories hidden"
  let some temporary ← IO.getEnv "TMPDIR"
    | throw (IO.userError "missing candidate temporary directory")
  if temporary.startsWith "/tmp/dvp-comparator-private-" then
    throw (IO.userError "candidate inherited Comparator export directory"))
theorem target : (1 : ℕ) + 1 = 2 := by norm_num
"""
    comparator.write_project(project, workspace, statement, solution, {
        "theorem_names": ["target"], "permitted_axioms": comparator.STANDARD_AXIOMS,
    })
    status, stdout, stderr = capture(comparator.comparison_command(selected_runtime, project), project, timeout=180)
    assert status == 0, stdout + stderr
    assert comparator.SUCCESS_LINE in stdout + stderr
    assert "blocked: comparator other task read" in stdout + stderr
    assert "private Comparator export directories hidden" in stdout + stderr
