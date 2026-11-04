"""Portable profiles validate versions before changing the caller's settings."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "check_leanexplore_config", ROOT / "scripts/check_leanexplore_config.py",
)
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)


@pytest.fixture
def configuration(tmp_path):
    workspace = tmp_path / "proof workspace"
    (workspace / "Mathlib").mkdir(parents=True)
    (workspace / "lean-toolchain").write_text(f"leanprover/lean4:v{CHECK.LEAN_VERSION}\n")
    snapshot = tmp_path / "retrieval assets" / "snapshot"
    snapshot.mkdir(parents=True)
    (snapshot / "build-manifest.json").write_text(json.dumps({
        "full_semantic_ready": True, "lean_version": CHECK.LEAN_VERSION,
        "mathlib_commit": CHECK.MATHLIB_COMMIT,
    }))
    (snapshot / "index-build-report.json").write_text('{"status":"complete_semantic_index"}')
    return snapshot, workspace


@pytest.mark.parametrize("nested", [False, True])
def test_configuration_checks_the_actual_mathlib_commit(configuration, monkeypatch, nested):
    snapshot, workspace = configuration
    mathlib = workspace
    if nested:
        (workspace / "Mathlib").rmdir()
        mathlib = workspace / ".lake/packages/mathlib"
        mathlib.mkdir(parents=True)

    def git_output(command, **kwargs):
        assert command == ["git", "-C", str(mathlib), "rev-parse", "HEAD"]
        return CHECK.MATHLIB_COMMIT + "\n"

    monkeypatch.setattr(CHECK.subprocess, "check_output", git_output)
    CHECK.validate_configuration(snapshot, workspace)
    monkeypatch.setattr(CHECK.subprocess, "check_output", lambda *a, **k: "wrong-commit\n")
    with pytest.raises(ValueError, match="Workspace Mathlib commit"):
        CHECK.validate_configuration(snapshot, workspace)


@pytest.mark.parametrize("filename,field,value", [
    ("build-manifest.json", "full_semantic_ready", False),
    ("build-manifest.json", "lean_version", "0.0.0"),
    ("build-manifest.json", "mathlib_commit", "wrong-commit"),
    ("index-build-report.json", "status", "incomplete"),
])
def test_incomplete_or_mismatched_snapshot_is_rejected(configuration, filename, field, value):
    snapshot, workspace = configuration
    path = snapshot / filename
    document = json.loads(path.read_text())
    document[field] = value
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="Snapshot is incomplete"):
        CHECK.validate_configuration(snapshot, workspace)


def test_wrong_lean_toolchain_is_rejected(configuration):
    snapshot, workspace = configuration
    (workspace / "lean-toolchain").write_text("leanprover/lean4:v0.0.0\n")
    with pytest.raises(ValueError, match="Workspace must use Lean"):
        CHECK.validate_configuration(snapshot, workspace)


def source_profile(tmp_path, profile, settings):
    tools = tmp_path / "bin"
    tools.mkdir(exist_ok=True)
    (tools / "python").symlink_to(sys.executable)
    git = tools / "git"
    git.write_text("#!/bin/sh\nprintf '%s\\n' '" + CHECK.MATHLIB_COMMIT + "'\n")
    git.chmod(0o755)
    keys = [
        "DVP_COMPARATOR_ROOT", "DAVINCI_TARGET_WORKSPACE", "DAVINCI_MODEL",
        "OPENAI_BASE_URL", "CUDA_VISIBLE_DEVICES", "DAVINCI_LEANEXPLORE_BACKEND",
        "DAVINCI_RETRIEVE_INDEX", "DVP_PROFILE_STATUS",
    ]
    program = "import json,os; print(json.dumps({k:os.environ.get(k) for k in " + repr(keys) + "}))"
    script = 'source "$DVP_TEST_PROFILE"; export DVP_PROFILE_STATUS="$?"; python -c "$DVP_TEST_PROBE"'
    environment = {
        "PATH": str(tools) + os.pathsep + os.defpath,
        "HOME": str(tmp_path), "DVP_ROOT": str(ROOT),
        "DVP_TEST_PROFILE": str(ROOT / "examples" / profile),
        "DVP_TEST_PROBE": program, **settings,
    }
    result = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-c", script],
        env=environment, cwd=ROOT, capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout.splitlines()[-1])


@pytest.mark.parametrize("valid", [True, False])
def test_local_profile_preserves_proof_settings_and_fails_before_export(configuration, tmp_path, valid):
    snapshot, workspace = configuration
    if not valid:
        (snapshot / "index-build-report.json").write_text('{"status":"incomplete"}')
    settings = {
        "DVP_COMPARATOR_ROOT": "/chosen/comparator", "DAVINCI_TARGET_WORKSPACE": str(workspace),
        "DAVINCI_MODEL": "chosen-model", "OPENAI_BASE_URL": "http://model.test/v1",
        "CUDA_VISIBLE_DEVICES": "3", "DAVINCI_LEANEXPLORE_BACKEND": "remote",
        "DAVINCI_RETRIEVE_INDEX": "/chosen/index.jsonl",
        "LEAN_EXPLORE_CACHE_DIR": str(snapshot.parent), "LEAN_EXPLORE_VERSION": snapshot.name,
    }
    actual = source_profile(tmp_path, "leanexplore-local.env.sh", settings)
    assert actual.pop("DVP_PROFILE_STATUS") == ("0" if valid else "1")
    assert actual.pop("DAVINCI_LEANEXPLORE_BACKEND") == ("local" if valid else "remote")
    assert actual.pop("DAVINCI_RETRIEVE_INDEX") == (None if valid else settings["DAVINCI_RETRIEVE_INDEX"])
    assert all(actual[key] == settings[key] for key in actual)


@pytest.mark.parametrize("profile", ["proof.env.sh", "leanexplore-remote.env.sh"])
def test_profiles_keep_caller_runtime_and_model_choices(tmp_path, profile):
    settings = {
        "DVP_COMPARATOR_ROOT": "/chosen/comparator", "DAVINCI_TARGET_WORKSPACE": "/chosen/workspace",
        "DAVINCI_MODEL": "chosen-model", "OPENAI_BASE_URL": "http://model.test/v1",
        "CUDA_VISIBLE_DEVICES": "3", "DAVINCI_LEANEXPLORE_URL": "http://retrieval.test:8765",
    }
    actual = source_profile(tmp_path, profile, settings)
    assert actual["DVP_PROFILE_STATUS"] == "0"
    for key in ["DVP_COMPARATOR_ROOT", "DAVINCI_TARGET_WORKSPACE", "DAVINCI_MODEL", "OPENAI_BASE_URL", "CUDA_VISIBLE_DEVICES"]:
        assert actual[key] == settings[key]


def test_remote_profile_without_url_keeps_existing_retrieval_configuration(tmp_path):
    actual = source_profile(tmp_path, "leanexplore-remote.env.sh", {
        "DAVINCI_LEANEXPLORE_BACKEND": "local", "DAVINCI_RETRIEVE_INDEX": "/chosen/index.jsonl",
    })
    assert actual["DVP_PROFILE_STATUS"] == "1"
    assert actual["DAVINCI_LEANEXPLORE_BACKEND"] == "local"
    assert actual["DAVINCI_RETRIEVE_INDEX"] == "/chosen/index.jsonl"
