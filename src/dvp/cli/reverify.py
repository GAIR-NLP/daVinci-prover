"""Reverify unchanged audited candidates with the current fixed-source verifier.

This creates a separate evidence report. It never edits historical run results,
uses an old canonical file as input, or requests a model generation.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import time
from typing import Any

from dvp.verification.fixed_context import FixedTaskContract, preparation_fingerprint, load_contract_sidecar
from dvp.verification.comparator import BACKEND, ComparatorVerifier
from dvp.verification.lean import LeanVerificationConfig, LeanVerifier
from dvp.verification.command_parser import parser_fingerprint


EXCLUDED_TASKS = {"deepseek_proverbench_calculus__p47"}


def digest(value: bytes | str) -> str:
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


def json_digest(value: Any) -> str:
    return digest(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")))


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_bytes())


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@dataclass
class ReplayJob:
    key: str
    row: dict[str, Any]
    source_identity: dict[str, Any]
    candidate_sha256: str
    code: str
    workspace: Path
    references: list[dict[str, Any]] = field(default_factory=list)

    def contract(self) -> FixedTaskContract:
        return FixedTaskContract.from_metadata(self.row["theorem_statement"], self.row["_fixed_task_contract"],
                                               source_identity=self.row["_fixed_contract_identity"])


def select_candidates(audit: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    candidates = audit["strict_guard_only_scope"]["candidates"]
    if not isinstance(candidates, list):
        raise ValueError("strict_guard_only_scope.candidates must be a list")
    selected, excluded = [], []
    for candidate in candidates:
        if candidate["task_id"] in EXCLUDED_TASKS:
            excluded.append({**candidate, "selection_reason": "known_correct_rejection_excluded"})
        elif not candidate.get("source_section_instance_commands"):
            excluded.append({**candidate, "selection_reason": "no_source_section_instances"})
        else:
            selected.append(candidate)
    if not selected:
        raise ValueError("No audited source-section-instance candidates selected")
    return selected, excluded


def load_jobs(audit: dict[str, Any], workspace: Path) -> tuple[list[ReplayJob], list[dict[str, Any]]]:
    """Resolve original source lineage and deduplicate without starting Lean."""
    selected, excluded = select_candidates(audit)
    workspace = workspace.expanduser().resolve()
    datasets, manifests, jobs = {}, {}, {}
    for candidate in selected:
        candidate_path = Path(candidate["candidate_path"]).expanduser().resolve()
        metadata_path = Path(candidate["metadata_path"]).expanduser().resolve()
        run_root = next((parent for parent in metadata_path.parents if (parent / "run_manifest.json").is_file()), None)
        if run_root is None:
            raise ValueError(f"Cannot find the original run manifest for {metadata_path}")
        if candidate["run"] != run_root.name:
            raise ValueError(f"Run identity mismatch: {metadata_path}")
        if not candidate_path.is_relative_to(run_root) or not metadata_path.is_relative_to(run_root):
            raise ValueError("Candidate and old metadata must belong to the original run")
        if run_root not in manifests:
            manifest_path = run_root / "run_manifest.json"
            manifests[run_root] = (read_json(manifest_path), digest(manifest_path.read_bytes()))
        manifest, manifest_sha = manifests[run_root]
        dataset = Path(manifest["dataset"]).expanduser().resolve()
        if dataset not in datasets:
            data = dataset.read_bytes()
            rows = [json.loads(line) for line in data.splitlines() if line.strip()]
            identity = load_contract_sidecar(dataset, rows, require_active=False)
            if identity is None:
                raise ValueError(f"Original dataset has no fixed-context sidecar: {dataset}")
            datasets[dataset] = (digest(data), identity, {row["task_id"]: row for row in rows})
        dataset_sha, sidecar, rows = datasets[dataset]
        if manifest.get("dataset_sha256") != dataset_sha:
            raise ValueError(f"Original dataset hash changed: {dataset}")
        old_sidecar_sha = manifest.get("policy", {}).get("fixed_context", {}).get("sha256")
        if old_sidecar_sha != sidecar["sha256"]:
            raise ValueError(f"Original sidecar hash changed: {sidecar['path']}")
        row = rows[candidate["task_id"]]
        contract = FixedTaskContract.from_metadata(row["theorem_statement"], row["_fixed_task_contract"], source_identity=sidecar)
        source_sha = digest(contract.statement)
        old_metadata_bytes = metadata_path.read_bytes()
        old_metadata = json.loads(old_metadata_bytes)
        code_bytes = candidate_path.read_bytes()
        candidate_sha = digest(code_bytes)
        if candidate_sha != candidate["candidate_sha256"] or candidate_sha != old_metadata.get("original_sha256"):
            raise ValueError(f"Original candidate hash changed: {candidate_path}")
        if source_sha != candidate["source_sha256"] or source_sha != old_metadata.get("source_sha256"):
            raise ValueError(f"Original source hash mismatch: {candidate['task_id']}")
        if old_metadata.get("contract_hash") != contract.contract_hash:
            raise ValueError(f"Original contract metadata mismatch: {metadata_path}")
        if Path(old_metadata["candidate_path"]).resolve() != candidate_path:
            raise ValueError(f"Old metadata points to a different candidate: {metadata_path}")
        task_root = metadata_path.parents[2]
        result_path = task_root / "result.json"
        old_result_bytes = result_path.read_bytes()
        old_result = json.loads(old_result_bytes)
        if old_result.get("task_id") != candidate["task_id"]:
            raise ValueError(f"Original task result identity mismatch: {result_path}")
        if old_result.get("status") != candidate["final_status"]:
            raise ValueError(f"Original task status changed since the audit: {result_path}")
        source_identity = {"dataset": str(dataset), "dataset_sha256": dataset_sha,
                           "sidecar": sidecar, "source_sha256": source_sha,
                           "contract_hash": contract.contract_hash}
        key = json_digest({"source_identity": source_identity, "candidate_sha256": candidate_sha,
                           "workspace": str(workspace)})
        reference = {"run": candidate["run"], "run_root": str(run_root), "task_id": candidate["task_id"],
                     "candidate_path": str(candidate_path), "old_metadata_path": str(metadata_path),
                     "old_metadata_sha256": digest(old_metadata_bytes),
                     "old_canonical_path": old_metadata.get("canonical_path"),
                     "old_canonical_sha256": old_metadata.get("canonical_sha256"),
                     "old_result_path": str(result_path), "old_result_sha256": digest(old_result_bytes),
                     "old_status": old_result["status"], "old_success": old_result.get("success") is True,
                     "old_reason": old_result.get("result", {}).get("reason"),
                     "old_manifest_sha256": manifest_sha,
                     "source_section_instance_commands": candidate["source_section_instance_commands"]}
        if key not in jobs:
            jobs[key] = ReplayJob(key, row, source_identity, candidate_sha, code_bytes.decode("utf-8"), workspace)
        jobs[key].references.append(reference)
    return list(jobs.values()), excluded


def accepted(result: dict[str, Any], job: ReplayJob) -> bool:
    return bool(result.get("success") is True and not result.get("errors")
                and result.get("backend") == BACKEND
                and result.get("candidate_sha256") == job.candidate_sha256
                and result.get("original_source_sha256") == job.source_identity["source_sha256"])


def summarize(jobs: list[ReplayJob], results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    tasks = {}
    for job in jobs:
        replay = results.get(job.key)
        for reference in job.references:
            key = (reference["run_root"], reference["task_id"])
            row = tasks.setdefault(key, {key: reference[key] for key in (
                "run", "run_root", "task_id", "old_result_path", "old_status", "old_success", "old_reason")})
            row.setdefault("candidate_keys", []).append(job.key)
            row.setdefault("accepted_candidate_keys", [])
            if replay and replay["accepted"]:
                row["accepted_candidate_keys"].append(job.key)
    by_run = {}
    for row in tasks.values():
        row["candidate_keys"] = sorted(set(row["candidate_keys"]))
        row["accepted_candidate_keys"] = sorted(set(row["accepted_candidate_keys"]))
        row["supplementary_verified"] = bool(row["accepted_candidate_keys"])
        row["offline_recovered"] = not row["old_success"] and row["supplementary_verified"]
        row["would_be_success"] = row["old_success"] or row["supplementary_verified"]
        count = by_run.setdefault(row["run"], Counter())
        count["audited_tasks"] += 1
        count["old_passed"] += row["old_success"]
        count["supplementary_verified_tasks"] += row["supplementary_verified"]
        count["offline_recovered_tasks"] += row["offline_recovered"]
        count["still_unverified_tasks"] += not row["would_be_success"]
    return {"selected_candidate_references": sum(len(job.references) for job in jobs),
            "unique_verification_jobs": len(jobs), "completed_jobs": len(results),
            "accepted_jobs": sum(result["accepted"] for result in results.values()),
            "failed_jobs": sum(not result["accepted"] for result in results.values()),
            "by_run": {key: dict(value) for key, value in sorted(by_run.items())},
            "by_run_task": sorted(tasks.values(), key=lambda row: (row["run"], row["task_id"])),
            "model_calls": 0,
            "historical_results_modified": False,
            "interpretation": "Supplementary verification of existing candidates, not a rerun or an overwrite of historical benchmark scores."}


def implementation_identity() -> dict[str, str]:
    from dvp.verification import comparator, fixed_context, lean
    sources = {
        "fixed_task_contract.py": Path(fixed_context.__file__),
        "lean.py": Path(lean.__file__),
        "comparator.py": Path(comparator.__file__),
        "reverify_fixed_context.py": Path(__file__),
    }
    return {"parser_sha256": parser_fingerprint(), "preparation_sha256": preparation_fingerprint(),
            **{name: digest(path.read_bytes()) for name, path in sources.items()}}


async def execute_jobs(jobs: list[ReplayJob], output: Path, *, timeout: float, concurrency: int) -> dict[str, Any]:
    from dvp.verification.context_cache import environment_fingerprint
    from dvp.cli.prepare_contexts import prepare_rows

    before = implementation_identity()
    prepared_rows = {json_digest(job.source_identity): {**job.row, "target_workspace": str(job.workspace)} for job in jobs}
    preparation = await prepare_rows(list(prepared_rows.values()), jobs[0].workspace, timeout=timeout)
    environment_before = environment_fingerprint(jobs[0].workspace)
    write_json(output / "preparation.json", preparation)
    results = {}
    semaphore = asyncio.Semaphore(concurrency)

    async def one(job: ReplayJob) -> None:
        async with semaphore:
            folder = output / "candidates" / job.key
            folder.mkdir(parents=True)
            scratch = folder / "scratch"
            scratch.mkdir()
            contract = job.contract()
            config = LeanVerificationConfig(target_workspace=job.workspace, scratch_workspace=scratch,
                                            timeout=timeout, allow_sorry=False)
            verifier = LeanVerifier(config)
            started = time.monotonic()
            try:
                verification = await ComparatorVerifier(
                    verifier, contract.statement, task_contract=contract,
                    artifact_dir=folder / "comparator_artifacts",
                ).verify(job.code, max_output_chars=200_000)
            except Exception as exc:
                verification = {"success": False, "errors": [f"{type(exc).__name__}: {exc}"],
                                "error_kind": "reverification_infrastructure_exception"}
            record = {"key": job.key, "candidate_sha256": job.candidate_sha256,
                      "source_identity": job.source_identity, "target_workspace": str(job.workspace),
                      "references": job.references, "accepted": accepted(verification, job),
                      "elapsed_seconds": round(time.monotonic() - started, 3), "verification": verification}
            write_json(folder / "result.json", record)
            results[job.key] = record
            write_json(output / "summary.json", summarize(jobs, results))
            print(json.dumps({"completed": len(results), "total": len(jobs), "key": job.key,
                              "accepted": record["accepted"], "elapsed_seconds": record["elapsed_seconds"]}), flush=True)

    await asyncio.gather(*(one(job) for job in jobs))
    after = implementation_identity()
    summary = summarize(jobs, results)
    summary.update(implementation_before=before, implementation_after=after,
                   implementation_stable=before == after, environment_sha256=environment_before)
    if before != after:
        summary["valid_for_recovery"] = False
        summary["error"] = "Verifier implementation changed during supplementary verification"
    else:
        summary["valid_for_recovery"] = True
    write_json(output / "summary.json", summary)
    return summary


def positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def positive_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-candidates", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--target-workspace", required=True, type=Path)
    parser.add_argument("--lean-timeout", type=positive_float, default=3600)
    parser.add_argument("--concurrency", type=positive_int, default=3)
    parser.add_argument("--plan-only", action="store_true", help="Validate hashes and report selection without starting Lean or writing output.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    audit_path = args.audit_candidates.expanduser().resolve()
    audit_bytes = audit_path.read_bytes()
    jobs, excluded = load_jobs(json.loads(audit_bytes), args.target_workspace)
    output = args.output_dir.expanduser().resolve()
    historical_roots = {Path(reference["run_root"]) for job in jobs for reference in job.references}
    if any(output == root or output.is_relative_to(root) for root in historical_roots):
        raise ValueError("Supplementary output must be outside every historical run")
    plan = {"created_utc": datetime.now(timezone.utc).isoformat(), "audit_path": str(audit_path),
            "audit_sha256": digest(audit_bytes), "target_workspace": str(jobs[0].workspace),
            "lean_timeout": args.lean_timeout, "concurrency": args.concurrency,
            "excluded_candidates": excluded,
            "selection": summarize(jobs, {}),
            "jobs": [{"key": job.key, "candidate_sha256": job.candidate_sha256,
                      "source_identity": job.source_identity, "references": job.references} for job in jobs]}
    if args.plan_only:
        print(json.dumps({"selection": plan["selection"], "excluded_count": len(excluded)}, ensure_ascii=False, indent=2))
        return 0
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"Use an empty supplementary output directory: {output}")
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "manifest.json", plan)
    summary = asyncio.run(execute_jobs(jobs, output, timeout=args.lean_timeout,
                                       concurrency=args.concurrency))
    return 0 if summary["valid_for_recovery"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
