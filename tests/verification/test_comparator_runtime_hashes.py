"""Registered executable identities must match the verifier actually launched."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from dvp.verification import comparator
from dvp.verification.lean import LeanVerificationConfig, LeanVerifier


@pytest.fixture
def registration(tmp_path, monkeypatch):
    workspace = tmp_path / "mathlib"
    workspace.mkdir()
    (workspace / "lean-toolchain").write_text("leanprover/lean4:v4.35.0\n")
    prefix = tmp_path / "toolchain"
    (prefix / "bin").mkdir(parents=True)
    for name in ("lean", "lake", "leanexport", "leanchecker", "adapter"):
        path = prefix / "bin" / name
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
    runtime = {
        "kind": "lake", "lean_version": "4.35.0", "execution_mode": "sandboxed",
        "toolchain": str(prefix), "comparator": str(prefix / "bin/lake"),
        "lean4export": str(prefix / "bin/leanexport"),
        "leanchecker": str(prefix / "bin/leanchecker"), "bwrap": str(prefix / "bin/adapter"),
    }
    runtime["sha256"] = {
        key: hashlib.sha256(Path(runtime[key]).read_bytes()).hexdigest()
        for key in ("comparator", "lean4export", "leanchecker", "bwrap")
    }
    root = tmp_path / "runtime"
    manifest = root / "4.35.0/runtime.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps(runtime))
    monkeypatch.setenv("DVP_COMPARATOR_ROOT", str(root))
    return workspace, runtime, manifest


@pytest.mark.parametrize("mode", ["sandboxed", "unsandboxed"])
def test_matching_registration_loads_in_both_modes(registration, mode):
    workspace, runtime, manifest = registration
    runtime["execution_mode"] = mode
    if mode == "unsandboxed":
        del runtime["bwrap"], runtime["sha256"]["bwrap"]
    manifest.write_text(json.dumps(runtime))
    assert comparator.runtime_for(workspace)["sha256"] == runtime["sha256"]


@pytest.mark.parametrize("key", ["comparator", "lean4export", "leanchecker", "bwrap"])
def test_changed_executable_is_rejected_after_a_successful_load(registration, key):
    workspace, runtime, _ = registration
    comparator.runtime_for(workspace)
    path = Path(runtime[key])
    before = path.stat()
    path.write_text("#!/bin/sh\nexit 1\n")  # Same size; do not rely on mtime/size caches.
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(RuntimeError, match=f"SHA256 mismatch for {key}"):
        comparator.runtime_for(workspace)


@pytest.mark.parametrize("hashes", [None, {}, [], {"comparator": "invalid"}, {"comparator": 42}])
def test_missing_or_invalid_hashes_require_registration(registration, hashes):
    workspace, runtime, manifest = registration
    runtime["sha256"] = hashes
    manifest.write_text(json.dumps(runtime))
    with pytest.raises(RuntimeError, match="registered SHA256.*comparator"):
        comparator.runtime_for(workspace)


def test_hashes_cannot_describe_a_different_toolchain_executable(registration, tmp_path):
    workspace, runtime, manifest = registration
    other = tmp_path / "another-lake"
    other.write_bytes(Path(runtime["comparator"]).read_bytes())
    other.chmod(0o755)
    runtime["comparator"] = str(other)  # Even identical bytes must identify the launched path.
    manifest.write_text(json.dumps(runtime))
    with pytest.raises(RuntimeError, match="outside the selected toolchain"):
        comparator.runtime_for(workspace)


def test_preflight_rejects_changed_runtime_before_starting_any_process(registration, monkeypatch):
    workspace, runtime, _ = registration
    Path(runtime["leanchecker"]).write_text("modified")
    process = AsyncMock()
    monkeypatch.setattr(comparator, "run_process", process)
    with pytest.raises(RuntimeError, match="SHA256 mismatch for leanchecker"):
        asyncio.run(comparator.preflight_runtime(workspace))
    process.assert_not_awaited()


def test_changed_checker_is_an_infrastructure_failure_not_a_proof_verdict(registration, tmp_path):
    workspace, runtime, _ = registration
    Path(runtime["leanchecker"]).write_text("modified")
    verifier = LeanVerifier(LeanVerificationConfig(target_workspace=workspace, scratch_workspace=tmp_path))
    checker = comparator.ComparatorVerifier(
        verifier, "theorem target : True := by sorry", artifact_dir=tmp_path / "artifacts",
    )
    result = asyncio.run(checker.verify("theorem target : True := by trivial"))
    assert not result["success"] and result["_harness_infrastructure_error"]
    assert result["command"] is None
    assert "SHA256 mismatch for leanchecker" in result["errors"][0]
