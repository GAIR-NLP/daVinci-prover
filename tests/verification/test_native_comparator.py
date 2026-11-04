"""Runtime selection and isolation invariants for Lean's bundled Comparator."""
import importlib.util
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

import pytest

from dvp.verification import comparator, sandbox


def test_native_project_resolves_only_pinned_local_dependencies(tmp_path):
    workspace = tmp_path / "mathlib"
    workspace.mkdir()
    (workspace / "lean-toolchain").write_text("leanprover/lean4:v4.35.0")
    (workspace / ".lake/build/lib/lean").mkdir(parents=True)
    (workspace / ".lake/packages/batteries").mkdir(parents=True)
    (workspace / "lake-manifest.json").write_text(json.dumps({
        "name": "mathlib", "version": "1.3.0", "packages": [
            {"name": "batteries", "configFile": "lakefile.toml", "manifestFile": "lake-manifest.json"},
        ],
    }))
    project = tmp_path / "attempt"
    project.mkdir()
    comparator.write_project(project, workspace, "theorem t : True := by sorry",
                             "theorem t : True := by trivial", {})
    manifest = json.loads((project / "lake-manifest.json").read_text())
    assert [p["name"] for p in manifest["packages"]] == ["mathlib", "batteries"]
    assert all(p["type"] == "path" and Path(p["dir"]).is_absolute() for p in manifest["packages"])
    assert 'require mathlib from "' in (project / "lakefile.lean").read_text()
    assert not (project / "Challenge.lean").stat().st_mode & 0o222


def test_native_dispatch_keeps_official_sandbox_enabled(tmp_path):
    command = comparator.comparison_command({
        "kind": "lake", "toolchain": "/opt/lean435", "bwrap": "/trusted/adapter",
    }, tmp_path)
    assert "native_comparator.py" in command[1]
    assert "--inadvisably-no-sandbox" not in command


def test_native_local_dispatch_does_not_require_sandbox_adapter(tmp_path):
    command = comparator.comparison_command({
        "kind": "lake", "toolchain": "/opt/lean435", "execution_mode": "unsandboxed",
    }, tmp_path)
    assert "--inadvisably-no-sandbox" in command
    assert "--bwrap" not in command


@pytest.mark.parametrize("mode,kind,has_adapter,error", [
    ("sandboxed", "lake", True, None),
    ("sandboxed", "lake", False, "Missing comparator executable bwrap"),
    ("unsandboxed", "lake", False, None),
    ("misspelled", "lake", False, "Unknown Comparator execution mode"),
    ("sandboxed", None, True, "must use Lean's bundled lake comparator"),
    ("unsandboxed", None, False, "must use Lean's bundled lake comparator"),
])
def test_runtime_mode_and_kind_are_validated(tmp_path, monkeypatch, mode, kind, has_adapter, error):
    workspace = tmp_path / "mathlib"
    workspace.mkdir()
    (workspace / "lean-toolchain").write_text("leanprover/lean4:v4.35.0")
    prefix = tmp_path / "toolchain"
    (prefix / "bin").mkdir(parents=True)
    for name in ("lean", "lake", "leanexport", "leanchecker"):
        binary = prefix / "bin" / name
        binary.touch(mode=0o755)
    root = tmp_path / "runtime"
    manifest = root / "4.35.0/runtime.json"
    manifest.parent.mkdir(parents=True)
    adapter = tmp_path / "sandbox-adapter"
    if has_adapter:
        adapter.touch(mode=0o755)
    manifest.write_text(json.dumps({
        "kind": kind, "lean_version": "4.35.0", "execution_mode": mode,
        "toolchain": str(prefix), "comparator": str(prefix / "bin/lake"),
        "lean4export": str(prefix / "bin/leanexport"),
        "leanchecker": str(prefix / "bin/leanchecker"),
        "bwrap": str(adapter),
        "sha256": {
            key: hashlib.sha256(b"").hexdigest()
            for key in ("comparator", "lean4export", "leanchecker", "bwrap")
        },
    }))
    monkeypatch.setenv("DVP_COMPARATOR_ROOT", str(root))
    if error:
        with pytest.raises(RuntimeError, match=error):
            comparator.runtime_for(workspace)
    else:
        assert comparator.runtime_for(workspace)["execution_mode"] == mode


def test_runtime_requires_supported_toolchain(tmp_path):
    (tmp_path / "lean-toolchain").write_text("leanprover/lean4:unsupported")
    with pytest.raises(RuntimeError, match="Comparator requires Lean 4.35.0"):
        comparator.runtime_for(tmp_path)


def test_local_child_clears_credentials_and_drops_root(monkeypatch):
    monkeypatch.setenv("INF_API_KEY", "test-secret-not-a-real-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-secret-not-a-real-key")
    with tempfile.TemporaryDirectory(prefix="dvp-local-test-") as temporary:
        folder = Path(temporary)
        folder.chmod(0o755)
        (folder / "bin").mkdir()
        (folder / "bin/lean").touch()
        command = comparator.sandbox_command([
            sys.executable, "-c",
            "import json,os; print(json.dumps({'uid':os.getuid(),"
            "'secret':any(k in os.environ for k in ['INF_API_KEY','OPENAI_API_KEY']),"
            "'cwd':os.getcwd()}))",
        ], workspace=folder, toolchain=folder, sandboxed=False)
        status, stdout, stderr = asyncio.run(comparator.run_process(command, cwd=folder, timeout=10))
        assert status == 0, stderr
        assert json.loads(stdout) == {
            "uid": 65534 if os.geteuid() == 0 else os.getuid(),
            "secret": False, "cwd": str(folder),
        }


def load_adapter(monkeypatch):
    monkeypatch.setitem(sys.modules, "sandbox", sandbox)
    spec = importlib.util.spec_from_file_location(
        "native_adapter", Path(comparator.__file__).with_name("native_comparator_sandbox.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_native_adapter_preserves_bwrap_and_seccomp_with_clean_environment(tmp_path, monkeypatch):
    adapter = load_adapter(monkeypatch)
    lake = tmp_path / "bin/lake"
    lake.parent.mkdir()
    lake.write_text("executable")
    monkeypatch.setattr(adapter.shutil, "which",
                        lambda name, path=None: "/usr/bin/bwrap" if name == "bwrap" else str(lake))
    monkeypatch.setattr(adapter, "_seccomp_filter", lambda: 42)
    captured = {}
    def execve(binary, arguments, environment):
        captured.update(binary=binary, arguments=arguments, environment=environment)
    monkeypatch.setattr(adapter.os, "execve", execve)
    monkeypatch.setattr(sys, "argv", [
        "adapter", "--ro-bind", "/", "/", "--proc", "/proc", "--clearenv",
        "--setenv", "PATH", str(lake.parent), "--unshare-all", "--share-net",
        "--", str(lake), "env",
    ])
    adapter.main()
    args = captured["arguments"]
    assert captured["binary"] == "/usr/bin/bwrap"
    assert args[args.index("--seccomp") + 1] == "42"
    assert "--share-net" not in args and "--unshare-all" in args
    assert args[args.index("--remount-ro") + 1] == "/proc"
    assert "/proc/self/exe" in args and str(lake) in args
    assert set(captured["environment"]) == {"PATH", "LANG"}


def test_native_adapter_rejects_missing_isolation_arguments(tmp_path, monkeypatch):
    adapter = load_adapter(monkeypatch)
    lake = tmp_path / "lake"
    lake.touch()
    monkeypatch.setattr(adapter.shutil, "which", lambda *a, **kw: str(lake))
    monkeypatch.setattr(sys, "argv", ["adapter", "--setenv", "PATH", str(tmp_path), "--", "true"])
    with pytest.raises(RuntimeError, match="unexpected native Comparator sandbox"):
        adapter.main()
