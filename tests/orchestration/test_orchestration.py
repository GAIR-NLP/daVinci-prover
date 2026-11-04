"""Behavior contracts for evaluator extraction and checkpoint recovery."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from dvp.artifacts.persistence import atomic_write_json, atomic_write_lines
from dvp.cli.evaluate import build_parser
from dvp.orchestration import attempts, checkpoints, evaluator, manifest, provenance, results
from dvp.tasks.dataset import safe_task_id


def options(tmp_path: Path, *extra: str):
    dataset = tmp_path / "tasks.jsonl"
    if not dataset.exists():
        dataset.write_text(json.dumps({"task_id": "sample", "statement": "theorem t : True := by trivial"}) + "\n")
    return build_parser().parse_args([
        "--dataset", str(dataset), "--model", "mock", "--run-root", str(tmp_path / "run"),
        "--lean-retrieve", "false", "--python-execute", "false",
        "--trace-level", "off", "--retry-delay", "0", *extra,
    ])


def attempt_record(row, attempt, retry, status, run_dir):
    success = status == "passed"
    return {
        "task_id": row["task_id"], "dataset_index": row["_dataset_index"],
        "attempt": attempt, "retry": retry, "status": status,
        "valid_for_scoring": status in {"passed", "failed"},
        "retryable_service_error": status == "retryable_service_error",
        "success": success, "score": float(success),
        "result": {"success": success, "score": float(success)}, "run_dir": str(run_dir),
    }


def stub_attempts(monkeypatch, statuses):
    pending = iter(statuses)
    calls = []

    async def run(row, args, run_dir, *, attempt, retry, routing_key=None):
        calls.append((attempt, retry, run_dir))
        record = attempt_record(row, attempt, retry, next(pending), run_dir)
        atomic_write_json(run_dir / "attempt_result.json", record)
        return record

    monkeypatch.setattr(attempts, "run_attempt", run)
    return calls


def test_infrastructure_retries_do_not_consume_samples_and_success_stops(tmp_path, monkeypatch):
    args = options(tmp_path, "--attempts-per-task", "3", "--retries", "2")
    row = {"task_id": "sample", "_dataset_index": 0}
    calls = stub_attempts(monkeypatch, ["infrastructure_error", "retryable_service_error", "failed", "passed"])
    record = asyncio.run(attempts.run_task(row, args, args.run_root))
    assert [(a, r) for a, r, _ in calls] == [(1, 0), (1, 1), (1, 2), (2, 0)]
    assert record["attempts_used"] == 2
    assert record["runs_used"] == 4
    assert record["infrastructure_retries"] == record["retryable_service_retries"] == 1
    assert record["success"] and not record["first_attempt_success"]
    assert record["status"] == "passed"
    assert json.loads((args.run_root / "tasks" / safe_task_id(row) / "result.json").read_text()) == record


@pytest.mark.parametrize("reason", ["comparator_timeout", "comparator_infrastructure_error"])
def test_final_acceptance_error_does_not_restart_model_generation(tmp_path, monkeypatch, reason):
    args = options(tmp_path, "--attempts-per-task", "3", "--retries", "2")
    row = {"task_id": "sample", "_dataset_index": 0}
    async def run(row, args, run_dir, *, attempt, retry, routing_key=None):
        record = attempt_record(row, attempt, retry, "infrastructure_error", run_dir)
        record["result"].update(aborted=True, reason=reason, retryable_service_error=False)
        return record
    run_attempt = AsyncMock(side_effect=run)
    monkeypatch.setattr(attempts, "run_attempt", run_attempt)
    result = asyncio.run(attempts.run_task(row, args, args.run_root))
    assert run_attempt.await_count == result["runs_used"] == 1
    assert not result["valid_for_scoring"] and not result["success"]
    assert result["status"] == "infrastructure_error" and result["result"]["reason"] == reason


def test_resume_recovers_attempt_and_isolates_interrupted_directory(tmp_path, monkeypatch):
    args = options(tmp_path, "--resume", "--attempts-per-task", "2", "--retries", "0")
    row = {"task_id": "sample", "_dataset_index": 0}
    task_dir = args.run_root / "tasks" / safe_task_id(row)
    complete = task_dir / "attempt_1_retry_0"
    atomic_write_json(complete / "attempt_result.json", attempt_record(row, 1, 0, "failed", complete))
    interrupted = task_dir / "attempt_2_retry_0"
    interrupted.mkdir()
    (interrupted / "trajectory.jsonl").write_text("partial old trajectory\n")
    calls = stub_attempts(monkeypatch, ["passed"])
    record = asyncio.run(attempts.run_task(row, args, args.run_root))
    assert record["runs"][0]["resumed"]
    assert record["runs_used"] == record["attempts_used"] == 2
    assert calls == [(2, 0, task_dir / "attempt_2_retry_0_resume_1")]
    assert (interrupted / "trajectory.jsonl").read_text() == "partial old trajectory\n"
    again = asyncio.run(attempts.run_task(row, args, args.run_root))
    assert again["resumed"] and again["success"]
    assert len(calls) == 1


@pytest.mark.parametrize("content", ['{"task_id":', "[]"])
def test_corrupt_checkpoint_fails_closed(tmp_path, monkeypatch, content):
    args = options(tmp_path, "--resume")
    row = {"task_id": "sample", "_dataset_index": 0}
    task_dir = args.run_root / "tasks" / safe_task_id(row)
    task_dir.mkdir(parents=True)
    (task_dir / "result.json").write_text(content)
    calls = stub_attempts(monkeypatch, [])
    with pytest.raises(ValueError, match="Invalid task result"):
        asyncio.run(attempts.run_task(row, args, args.run_root))
    assert not calls


def test_scoring_denominators_distinguish_coverage_and_service_failures(tmp_path):
    args = options(tmp_path, "--attempts-per-task", "3")
    records = [
        {"status": "passed", "success": True, "valid_for_scoring": True, "first_attempt_success": False},
        {"status": "failed", "success": False, "valid_for_scoring": True},
        {"status": "infrastructure_error", "success": False, "valid_for_scoring": False},
        {"status": "retryable_service_error", "success": False, "valid_for_scoring": False},
    ]
    summary = results.build_progress_summary(
        args=args, manifest={"config_id": "test", "schema_version": "test"},
        dataset=args.dataset, run_root=args.run_root, selected=5, records=records,
    )
    assert (summary["selected"], summary["completed"], summary["pending"], summary["scored"]) == (5, 4, 1, 2)
    assert summary["pass@1"] == 0 and summary["pass@3"] == 0.5
    assert summary["success_rate_selected"] == 0.2 and summary["coverage"] == 0.8
    assert summary["infrastructure_failed"] == summary["retryable_service_failed"] == 1
    assert not summary["run_complete"]


def test_atomic_snapshot_keeps_previous_file_on_interrupted_serialization(tmp_path):
    target = tmp_path / "result.json"
    atomic_write_json(target, {"success": True})
    original = target.read_bytes()

    def interrupted():
        yield '{"success":'
        raise RuntimeError("simulated writer interruption")

    with pytest.raises(RuntimeError, match="interruption"):
        atomic_write_lines(target, interrupted())
    assert target.read_bytes() == original
    assert not list(tmp_path.glob(".*.tmp"))


def test_progress_snapshot_orders_results_then_append_updates_counts(tmp_path):
    args = options(tmp_path)
    kwargs = dict(args=args, manifest={"config_id": "test", "schema_version": "test"},
                  dataset=args.dataset, run_root=args.run_root, selected=3)
    records = [
        {"dataset_index": 2, "status": "failed", "valid_for_scoring": True, "success": False},
        {"dataset_index": 0, "status": "passed", "valid_for_scoring": True, "success": True, "first_attempt_success": True},
    ]
    summary = results.write_progress_snapshot(**kwargs, records=records)
    saved = [json.loads(line) for line in (args.run_root / "results.jsonl").read_text().splitlines()]
    assert [item["dataset_index"] for item in saved] == [0, 2]
    assert json.loads((args.run_root / "summary.json").read_text()) == summary
    counts = results.ProgressCounts.from_records(records)
    record = {"dataset_index": 1, "status": "infrastructure_error", "valid_for_scoring": False}
    appended = results.append_progress_record(**kwargs, counts=counts, record=record)
    assert appended["run_complete"] and appended["scored"] == 2
    assert appended["pass@1"] == 0.5
    assert json.loads((args.run_root / "summary.json").read_text()) == appended
    assert len((args.run_root / "results.jsonl").read_text().splitlines()) == 3


def test_run_lock_is_exclusive_and_released(tmp_path):
    handle = checkpoints.acquire_run_lock(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="already active"):
            checkpoints.acquire_run_lock(tmp_path)
    finally:
        checkpoints.release_run_lock(handle)
    checkpoints.release_run_lock(checkpoints.acquire_run_lock(tmp_path))


def test_complete_run_resume_preserves_manifest_and_does_not_rescore(tmp_path, monkeypatch):
    # This is a scheduler/checkpoint replay; real runtime acceptance is tested
    # separately. Its startup check must not invoke Lean on the test machine.
    preflight = AsyncMock(return_value={})
    monkeypatch.setattr("dvp.verification.comparator.preflight_runtime", preflight)
    args = options(tmp_path)
    calls = stub_attempts(monkeypatch, ["passed"])
    assert asyncio.run(evaluator.async_main(args)) == 0
    original_manifest = (args.run_root / "run_manifest.json").read_bytes()
    original_baseline = (args.run_root / "execution_baseline.json").read_bytes()
    args.resume = True
    args.concurrency = 3
    args.base_url = "http://replacement.invalid/v1"
    args.allow_endpoint_change = True
    assert asyncio.run(evaluator.async_main(args)) == 0
    assert len(calls) == 1
    assert preflight.await_count == 2
    assert (args.run_root / "run_manifest.json").read_bytes() == original_manifest
    assert (args.run_root / "execution_baseline.json").read_bytes() == original_baseline
    summary = json.loads((args.run_root / "summary.json").read_text())
    assert summary["completed"] == summary["scored"] == summary["passed"] == 1
    invocations = [json.loads(line) for line in (args.run_root / "run_invocations.jsonl").read_text().splitlines()]
    assert len(invocations) == 2 and invocations[-1]["endpoint_changed"]


@pytest.mark.parametrize("relative", ["toolkits/nested/execute.py", "verification/guard.lean"])
def test_source_identity_tracks_deep_implementation_and_resources(tmp_path, monkeypatch, relative):
    package = tmp_path / "dvp"
    implementation = package / relative
    implementation.parent.mkdir(parents=True)
    implementation.write_text("before\n")
    monkeypatch.setattr(provenance, "_SOURCE_PACKAGE_ROOT", package)
    old = provenance._agent_source_identity()
    old_trusted = provenance._trusted_source_identity()
    implementation.write_text("after\n")
    new = provenance._agent_source_identity()
    new_trusted = provenance._trusted_source_identity()
    assert old["sha256"] != new["sha256"]
    assert old_trusted["sha256"] != new_trusted["sha256"]
    assert {Path(item["path"]) for item in new["files"]} == {implementation}
    assert not manifest._runner_only_source_change(old, new)


def test_manifest_rejects_execution_change_and_legacy_sources(tmp_path):
    args = options(tmp_path)
    current = manifest.build_run_manifest(args, dataset=args.dataset, run_root=args.run_root)
    manifest.validate_resume_manifest(deepcopy(current), current)
    for component in ("agent", "trusted"):
        before = deepcopy(current)
        before["source"][component] = {
            "sha256": "old-source-sha",
            "files": [{"module": "legacy.tools", "sha256": "old-tool-sha"}],
        }
        with pytest.raises(ValueError, match="source"):
            manifest.validate_resume_manifest(before, current, allow_endpoint_change=True)
    endpoint = deepcopy(current)
    endpoint["base_url"] = "http://changed.invalid/v1"
    with pytest.raises(ValueError, match="base_url"):
        manifest.validate_resume_manifest(current, endpoint)
    manifest.validate_resume_manifest(current, endpoint, allow_endpoint_change=True)
    changed = deepcopy(current)
    changed["evaluation"]["concurrency"] += 1
    manifest.validate_resume_manifest(current, changed)
    changed["evaluation"]["attempts_per_task"] += 1
    with pytest.raises(ValueError, match="evaluation"):
        manifest.validate_resume_manifest(current, changed)


@pytest.mark.parametrize("missing", ["source", "agent", "trusted", "artifacts"])
def test_resume_rejects_missing_execution_metadata_without_adopting_run(tmp_path, missing):
    args = options(tmp_path)
    current = manifest.build_run_manifest(args, dataset=args.dataset, run_root=args.run_root)
    incomplete = deepcopy(current)
    if missing in {"agent", "trusted"}:
        incomplete["source"].pop(missing)
    else:
        incomplete.pop(missing)

    with pytest.raises(ValueError, match="source and artifact metadata"):
        manifest.validate_resume_manifest(incomplete, current)
    with pytest.raises(ValueError, match="source and artifact metadata"):
        manifest.ensure_execution_baseline(
            args.run_root, current_manifest=current, original_manifest=incomplete,
        )
    assert not (args.run_root / "execution_baseline.json").exists()


def test_resume_rejects_incompatible_execution_baseline_without_rewriting(tmp_path):
    args = options(tmp_path)
    current = manifest.build_run_manifest(args, dataset=args.dataset, run_root=args.run_root)
    baseline = manifest.ensure_execution_baseline(
        args.run_root, current_manifest=current, original_manifest=current,
    )
    baseline["schema_version"] = "legacy.execution-baseline.v1"
    path = args.run_root / "execution_baseline.json"
    atomic_write_json(path, baseline)
    original = path.read_bytes()

    with pytest.raises(ValueError, match="execution_baseline.json does not match"):
        manifest.ensure_execution_baseline(
            args.run_root, current_manifest=current, original_manifest=current,
        )
    assert path.read_bytes() == original
