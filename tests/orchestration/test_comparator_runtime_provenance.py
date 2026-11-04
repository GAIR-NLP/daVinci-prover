"""Installed comparator identities participate in run resume validation."""

import hashlib
from unittest.mock import Mock

import pytest

from dvp.cli.evaluate import build_parser
from dvp.orchestration.manifest import build_run_manifest, validate_resume_manifest
from dvp.orchestration.provenance import _comparator_runtime_identity
from dvp.verification import comparator


def test_runtime_manifests_are_fingerprinted_once_per_effective_workspace(tmp_path, monkeypatch):
    workspaces = [tmp_path / "a", tmp_path / "b"]
    manifests = {}
    for workspace in workspaces:
        workspace.mkdir()
        path = workspace / "runtime.json"
        path.write_text('{"upstream_commit":"original"}')
        manifests[workspace] = path
    resolver = Mock(side_effect=lambda workspace: {"manifest": str(manifests[workspace])})
    monkeypatch.setattr(comparator, "runtime_for", resolver)
    rows = [
        {"target_workspace": str(workspaces[0])},
        {"target_workspace": {"path": str(workspaces[0])}},
        {},
    ]
    identity = _comparator_runtime_identity(rows, workspaces[1])
    assert resolver.call_count == 2
    assert set(identity) == {str(path) for path in workspaces}
    for workspace in workspaces:
        assert identity[str(workspace)]["sha256"] == hashlib.sha256(manifests[workspace].read_bytes()).hexdigest()


def test_unavailable_runtime_can_be_recorded_without_starting_verification(tmp_path, monkeypatch):
    monkeypatch.setattr(comparator, "runtime_for", Mock(side_effect=RuntimeError("not installed")))
    assert _comparator_runtime_identity([], tmp_path) == {str(tmp_path): None}
    assert _comparator_runtime_identity([], None) == {}


def test_changed_runtime_manifest_prevents_resume_even_with_endpoint_change(tmp_path, monkeypatch):
    dataset = tmp_path / "tasks.jsonl"
    dataset.write_text('{"task_id":"test","statement":"theorem t : True := by sorry"}\n')
    runtime_manifest = tmp_path / "runtime.json"
    runtime_manifest.write_text('{"upstream_commit":"before","sha256":{"comparator":"old"}}')
    monkeypatch.setattr(comparator, "runtime_for", lambda _: {"manifest": str(runtime_manifest)})
    args = build_parser().parse_args([
        "--dataset", str(dataset), "--model", "mock", "--lean-retrieve", "false",
        "--target-workspace", str(tmp_path), "--run-root", str(tmp_path / "run"),
    ])
    previous = build_run_manifest(args, dataset=dataset, run_root=args.run_root)
    runtime_manifest.write_text('{"upstream_commit":"after","sha256":{"comparator":"new"}}')
    current = build_run_manifest(args, dataset=dataset, run_root=args.run_root)
    with pytest.raises(ValueError, match="services"):
        validate_resume_manifest(previous, current, allow_endpoint_change=True)
