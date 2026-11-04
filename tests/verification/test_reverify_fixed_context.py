"""Offline supplementary-verification selection, lineage and scoring tests."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path

import pytest

import dvp.cli.reverify as replay
from test_fixed_task_contract import fixture_metadata


@pytest.fixture
def staged(tmp_path):
    statement = "import Mathlib\nvariable {R : Type*} [CommRing R]\ntheorem target : True := by sorry"
    metadata = fixture_metadata(statement)
    metadata["task_id"] = "task_a"
    dataset = tmp_path / "dataset.jsonl"
    dataset.write_text(json.dumps({"task_id": "task_a", "theorem_statement": statement}) + "\n")
    sidecar = dataset.with_suffix(".contract.json")
    replay.write_json(sidecar, {"schema_version": "davinci.benchmark_contract.v1",
        "dataset_sha256": replay.digest(dataset.read_bytes()), "offset_unit": "python_unicode_codepoints",
        "tasks": [metadata]})
    candidates = []
    contract = replay.FixedTaskContract.from_metadata(statement, metadata)
    for run in ("run_1", "run_2"):
        run_root = tmp_path / "history" / run
        task_root = run_root / "tasks/task_a"
        artifacts = task_root / "attempt_1_retry_0/contract_artifacts"
        artifacts.mkdir(parents=True)
        replay.write_json(run_root / "run_manifest.json", {"dataset": str(dataset),
            "dataset_sha256": replay.digest(dataset.read_bytes()),
            "policy": {"fixed_context": {"sha256": replay.digest(sidecar.read_bytes())}}})
        replay.write_json(task_root / "result.json", {"task_id": "task_a", "status": "failed", "success": False,
                                                       "result": {"reason": "max_turns"}})
        candidate = artifacts / "candidate.lean"
        candidate.write_bytes(b"theorem target : True := by trivial\n")
        old = artifacts / "old.json"
        replay.write_json(old, {"candidate_path": str(candidate), "canonical_path": str(artifacts / "MUST_NOT_READ.lean"),
            "canonical_sha256": "historical-canonical", "original_sha256": replay.digest(candidate.read_bytes()),
            "source_sha256": replay.digest(statement), "contract_hash": contract.contract_hash})
        candidates.append({"run": run, "task_id": "task_a", "final_status": "failed",
            "candidate_sha256": replay.digest(candidate.read_bytes()), "source_sha256": replay.digest(statement),
            "candidate_path": str(candidate), "metadata_path": str(old),
            "source_section_instance_commands": ["variable {R : Type*} [CommRing R]"]})
    audit = {"strict_guard_only_scope": {"candidates": candidates}}
    audit_path = tmp_path / "audit.json"
    replay.write_json(audit_path, audit)
    workspace = tmp_path / "mathlib"
    workspace.mkdir()
    return audit, audit_path, workspace


def success(job):
    return {"success": True, "errors": [], "backend": "leanprover/comparator",
            "candidate_sha256": job.candidate_sha256,
            "original_source_sha256": job.source_identity["source_sha256"]}


def test_selection_excludes_correct_rejection_and_non_instance_sources(staged):
    audit, _, _ = staged
    excluded = [dict(audit["strict_guard_only_scope"]["candidates"][0], task_id="deepseek_proverbench_calculus__p47"),
                dict(audit["strict_guard_only_scope"]["candidates"][0], source_section_instance_commands=[])]
    audit["strict_guard_only_scope"]["candidates"].extend(excluded)
    selected, rejected = replay.select_candidates(audit)
    assert len(selected) == 2
    assert [item["selection_reason"] for item in rejected] == ["known_correct_rejection_excluded", "no_source_section_instances"]


def test_cross_run_identical_candidate_is_one_job_with_both_origins(staged):
    audit, _, workspace = staged
    jobs, excluded = replay.load_jobs(audit, workspace)
    assert not excluded and len(jobs) == 1
    assert [ref["run"] for ref in jobs[0].references] == ["run_1", "run_2"]
    assert jobs[0].code.encode() == Path(jobs[0].references[0]["candidate_path"]).read_bytes()
    assert not Path(jobs[0].references[0]["old_canonical_path"]).exists()


def test_workspace_identity_is_part_of_deduplication(staged, tmp_path):
    audit, _, workspace = staged
    jobs, _ = replay.load_jobs(audit, workspace)
    elsewhere, _ = replay.load_jobs(audit, tmp_path / "another_workspace")
    assert jobs[0].key != elsewhere[0].key
    alias = tmp_path / "alias"
    alias.symlink_to(workspace, target_is_directory=True)
    same, _ = replay.load_jobs(audit, alias)
    assert jobs[0].key == same[0].key


@pytest.mark.parametrize("damage", ["candidate", "source", "sidecar", "metadata", "old_result"])
def test_changed_original_lineage_fails_before_verification(damage, staged):
    audit, _, workspace = staged
    candidate = audit["strict_guard_only_scope"]["candidates"][0]
    metadata = Path(candidate["metadata_path"])
    run = metadata.parents[4]
    manifest = replay.read_json(run / "run_manifest.json")
    dataset = Path(manifest["dataset"])
    if damage == "candidate":
        Path(candidate["candidate_path"]).write_text("theorem target : False := by contradiction")
    elif damage == "source":
        dataset.write_text(dataset.read_text() + "\n")
    elif damage == "sidecar":
        sidecar = dataset.with_suffix(".contract.json")
        sidecar.write_text(sidecar.read_text() + "\n")
    elif damage == "metadata":
        data = replay.read_json(metadata)
        data["contract_hash"] = "different-contract"
        replay.write_json(metadata, data)
    else:
        path = metadata.parents[2] / "result.json"
        data = replay.read_json(path)
        data["status"] = "passed"
        replay.write_json(path, data)
    with pytest.raises(ValueError, match="hash|metadata|status"):
        replay.load_jobs(audit, workspace)


@pytest.mark.parametrize("field", ["success", "errors", "backend", "candidate_sha256", "original_source_sha256"])
def test_acceptance_requires_all_guard_and_identity_evidence(field, staged):
    audit, _, workspace = staged
    job = replay.load_jobs(audit, workspace)[0][0]
    result = success(job)
    assert replay.accepted(result, job)
    if field == "success":
        result[field] = False
    elif field == "errors":
        result[field] = ["guard failed"]
    else:
        result[field] = "different"
    assert not replay.accepted(result, job)


def test_summary_counts_task_recovery_once_and_preserves_each_run(staged):
    audit, _, workspace = staged
    job = replay.load_jobs(audit, workspace)[0][0]
    duplicate = deepcopy(job)
    duplicate.key = "second-candidate"
    duplicate.references = [duplicate.references[0]]
    summary = replay.summarize([job, duplicate], {job.key: {"accepted": True}, duplicate.key: {"accepted": True}})
    assert summary["selected_candidate_references"] == 3
    assert len(summary["by_run_task"]) == 2
    assert all(count["offline_recovered_tasks"] == 1 for count in summary["by_run"].values())
    assert summary["historical_results_modified"] is False


@pytest.mark.parametrize("arguments", [["--concurrency", "0"], ["--lean-timeout", "0"], ["--lean-timeout", "nan"]])
def test_cli_rejects_invalid_resource_limits(arguments):
    with pytest.raises(SystemExit):
        replay.build_parser().parse_args(["--audit-candidates", "a", "--output-dir", "b", "--target-workspace", "c", *arguments])


def test_plan_only_checks_selection_without_writing_or_starting_lean(staged, tmp_path, capsys, monkeypatch):
    _, audit_path, workspace = staged

    async def forbidden(*args, **kwargs):
        pytest.fail("plan-only started verification")

    monkeypatch.setattr(replay, "execute_jobs", forbidden)
    output = tmp_path / "supplementary"
    assert replay.main(["--audit-candidates", str(audit_path), "--output-dir", str(output),
                        "--target-workspace", str(workspace), "--plan-only"]) == 0
    assert not output.exists()
    assert json.loads(capsys.readouterr().out)["selection"]["unique_verification_jobs"] == 1


def test_output_cannot_overwrite_original_run(staged):
    audit, audit_path, workspace = staged
    historical = Path(audit["strict_guard_only_scope"]["candidates"][0]["metadata_path"]).parents[4]
    with pytest.raises(ValueError, match="outside every historical run"):
        replay.main(["--audit-candidates", str(audit_path), "--output-dir", str(historical / "supplementary"),
                     "--target-workspace", str(workspace), "--plan-only"])


def test_executor_calls_current_verifier_with_original_code_and_separate_evidence(staged, tmp_path, monkeypatch):
    import dvp.cli.prepare_contexts as prepare_contexts
    import dvp.verification.context_cache as fixed_context_cache
    audit, _, workspace = staged
    jobs, _ = replay.load_jobs(audit, workspace)
    job = jobs[0]
    output = tmp_path / "reverification"
    output.mkdir()
    snapshots = {path: path.read_bytes() for path in (tmp_path / "history").rglob("*") if path.is_file()}
    called, warmed = [], []

    async def prepare(rows, target_workspace, *, timeout):
        assert all(row["target_workspace"] == str(workspace) for row in rows)
        warmed.append((rows, target_workspace, timeout))
        return {"prepared": len(rows)}

    class Verifier:
        def __init__(self, verifier, statement, *, task_contract, artifact_dir):
            config = verifier.config
            assert config.allow_sorry is False
            assert config.target_workspace == workspace
            assert artifact_dir.is_relative_to(output)
            assert task_contract.contract_hash == job.source_identity["contract_hash"]

        async def verify(self, code, **kwargs):
            called.append(code)
            return success(job)

    monkeypatch.setattr(prepare_contexts, "prepare_rows", prepare)
    monkeypatch.setattr(fixed_context_cache, "environment_fingerprint", lambda _: "test-environment")
    monkeypatch.setattr(replay, "implementation_identity", lambda: {"guard": "frozen"})
    monkeypatch.setattr(replay, "ComparatorVerifier", Verifier)
    summary = asyncio.run(replay.execute_jobs(jobs, output, timeout=120, concurrency=3))
    assert called == [job.code] and len(warmed) == 1
    assert summary["accepted_jobs"] == 1 and summary["valid_for_recovery"]
    saved = replay.read_json(output / "candidates" / job.key / "result.json")
    assert len(saved["references"]) == 2 and saved["accepted"]
    assert snapshots == {path: path.read_bytes() for path in snapshots}


def test_executor_bounds_concurrency_and_records_failures_without_false_recovery(staged, tmp_path, monkeypatch):
    import dvp.cli.prepare_contexts as prepare_contexts
    import dvp.verification.context_cache as fixed_context_cache
    audit, _, workspace = staged
    original = replay.load_jobs(audit, workspace)[0][0]
    jobs = []
    for index in range(3):
        job = deepcopy(original)
        job.key = f"job-{index}"
        job.code += f"-- variant {index}\n"
        job.candidate_sha256 = replay.digest(job.code)
        job.references = [{**job.references[0], "task_id": f"task-{index}"}]
        jobs.append(job)
    by_code = {job.code: job for job in jobs}
    state = {"active": 0, "peak": 0, "calls": 0}

    async def prepare(*args, **kwargs):
        return {"prepared": 1}

    class Verifier:
        def __init__(self, verifier, statement, **kwargs):
            config = verifier.config
            assert config.target_workspace == workspace
            assert config.timeout == 120 and config.allow_sorry is False

        async def verify(self, code, **kwargs):
            state["active"] += 1
            state["calls"] += 1
            state["peak"] = max(state["peak"], state["active"])
            try:
                await asyncio.sleep(0.01)
                job = by_code[code]
                if job.key == "job-2":
                    raise RuntimeError("local Lean executable unavailable")
                result = success(job)
                if job.key == "job-1":
                    result["success"] = False
                return result
            finally:
                state["active"] -= 1

    monkeypatch.setattr(prepare_contexts, "prepare_rows", prepare)
    monkeypatch.setattr(fixed_context_cache, "environment_fingerprint", lambda _: "test-environment")
    monkeypatch.setattr(replay, "implementation_identity", lambda: {"guard": "frozen"})
    monkeypatch.setattr(replay, "ComparatorVerifier", Verifier)
    output = tmp_path / "reverification"
    output.mkdir()
    result = asyncio.run(replay.execute_jobs(jobs, output, timeout=120, concurrency=2))
    assert state == {"active": 0, "peak": 2, "calls": 3}
    assert result["accepted_jobs"] == 1 and result["failed_jobs"] == 2
    assert result["by_run"]["run_1"]["offline_recovered_tasks"] == 1
    exception = replay.read_json(output / "candidates/job-2/result.json")
    assert not exception["accepted"]
    assert exception["verification"]["error_kind"] == "reverification_infrastructure_exception"
