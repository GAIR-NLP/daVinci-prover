"""Offline identity, activation and resume checks for fixed benchmark contexts."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from dvp.cli.evaluate import build_parser as build_eval_parser
from dvp.orchestration.manifest import build_run_manifest, validate_resume_manifest
from dvp.tasks.dataset import load_dataset
from dvp.verification.fixed_context import (
    FixedContextError, FixedTaskContract, POLICY_VERSION,
    preparation_fingerprint, load_contract_sidecar,
)
from dvp.verification.command_parser import parser_fingerprint
from test_fixed_task_contract import fixture_metadata


@pytest.fixture
def staged_dataset(tmp_path):
    row = {"task_id": "regression_fixture", "split": "test",
           "theorem_statement": "import Mathlib\ntheorem target : True := by sorry"}
    metadata = {"schema_version": "davinci.benchmark_contract.v1", "rows": 1,
                "offset_unit": "python_unicode_codepoints",
                "tasks": [fixture_metadata(row["theorem_statement"])]}
    task = metadata["tasks"][0]
    task["contract_hash"] = hashlib.sha256(json.dumps(
        task, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    dataset = tmp_path / "fixed_context.jsonl"
    dataset.write_text(json.dumps(row, ensure_ascii=False) + "\n")
    metadata["dataset_sha256"] = hashlib.sha256(dataset.read_bytes()).hexdigest()
    sidecar = dataset.with_suffix(".contract.json")
    sidecar.write_text(json.dumps(metadata, ensure_ascii=False))
    return dataset, [row], metadata


def activate(dataset):
    ready = {
        "policy": POLICY_VERSION,
        "dataset_sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(),
        "sidecar_sha256": hashlib.sha256(dataset.with_suffix(".contract.json").read_bytes()).hexdigest(),
        "parser_sha256": parser_fingerprint(),
        "preparation_sha256": preparation_fingerprint(),
    }
    dataset.with_suffix(".contract.ready.json").write_text(json.dumps(ready))
    return ready


def test_changed_task_statement_is_rejected(staged_dataset):
    dataset, rows, metadata = staged_dataset
    rows[0]["theorem_statement"] += "\n-- changed objective"
    with pytest.raises(ValueError, match="objective hash mismatch"):
        load_contract_sidecar(dataset, rows)
    with pytest.raises(FixedContextError, match="contract_hash_mismatch"):
        FixedTaskContract.from_metadata(rows[0]["theorem_statement"], metadata["tasks"][0])


@pytest.mark.parametrize("field", ["trusted_header_span", "target_signature_span", "target_proof_span"])
def test_changed_boundary_metadata_is_rejected(staged_dataset, field):
    _, rows, metadata = staged_dataset
    task = deepcopy(metadata["tasks"][0])
    task[field][1] -= 1
    with pytest.raises(FixedContextError, match="invalid_contract_metadata|contract_hash_mismatch"):
        FixedTaskContract.from_metadata(rows[0]["theorem_statement"], task)


def test_staged_sidecar_does_not_activate_evaluation(staged_dataset):
    dataset, rows, _ = staged_dataset
    assert load_contract_sidecar(dataset, rows, require_active=True) is None
    assert "_fixed_task_contract" not in rows[0]
    assert "_fixed_task_contract" not in load_dataset(dataset)[0]
    # Explicit offline validation remains available before activation.
    assert load_contract_sidecar(dataset, rows)["policy"] == POLICY_VERSION


def test_matching_activation_enables_fixed_context(staged_dataset):
    dataset, rows, _ = staged_dataset
    activate(dataset)
    identity = load_contract_sidecar(dataset, rows, require_active=True)
    assert identity["policy"] == POLICY_VERSION
    assert rows[0]["_fixed_task_contract"]["task_id"] == rows[0]["task_id"]
    assert load_dataset(dataset)[0]["_fixed_contract_identity"] == identity


def test_activated_dataset_cannot_silently_lose_its_sidecar(staged_dataset):
    dataset, rows, _ = staged_dataset
    activate(dataset)
    dataset.with_suffix(".contract.json").unlink()
    with pytest.raises(ValueError, match="Missing fixed-context sidecar"):
        load_contract_sidecar(dataset, rows, require_active=True)


@pytest.mark.parametrize("field", ["policy", "dataset_sha256", "sidecar_sha256", "parser_sha256", "preparation_sha256"])
def test_stale_activation_is_rejected(staged_dataset, field):
    dataset, rows, _ = staged_dataset
    ready = activate(dataset)
    ready[field] = "stale"
    dataset.with_suffix(".contract.ready.json").write_text(json.dumps(ready))
    with pytest.raises(ValueError, match="activation does not match"):
        load_contract_sidecar(dataset, rows, require_active=True)


def test_old_activation_is_source_lineage_not_acceptance_evidence(staged_dataset):
    dataset, rows, _ = staged_dataset
    ready = activate(dataset)
    ready.pop("preparation_sha256")
    ready["guard_sha256"] = "obsolete-guard-that-is-no-longer-executed"
    dataset.with_suffix(".contract.ready.json").write_text(json.dumps(ready))
    identity = load_contract_sidecar(dataset, rows, require_active=True)
    assert identity["policy"] == POLICY_VERSION
    assert "guard_sha256" not in identity
    assert "strict_contract_verified" not in rows[0]["_fixed_task_contract"]


def test_manifest_pins_fixed_policy_parser_and_rejects_legacy_resume(staged_dataset, tmp_path, monkeypatch):
    import dvp.verification.parser_service as lean_parser_service
    from dvp.verification.context_cache import environment_fingerprint

    # Manifest identity checks remain offline; parser service integration tests
    # separately exercise resolution of the real Lake environment.
    monkeypatch.setattr(lean_parser_service, "environment_identity", lambda workspace: {
        "workspace": str(Path(workspace).resolve()), "revision": "test-environment",
    })
    dataset, rows, _ = staged_dataset
    args = build_eval_parser().parse_args([
        "--model", "mock", "--lean-retrieve", "false", "--python-execute", "false",
    ])
    legacy = build_run_manifest(args, dataset=dataset, run_root=tmp_path, rows=rows)
    activate(dataset)
    identity = load_contract_sidecar(dataset, rows, require_active=True)
    current = build_run_manifest(args, dataset=dataset, run_root=tmp_path, rows=rows)
    assert current["policy"]["fixed_context"] == identity
    assert current["policy"]["command_parser_sha256"] == parser_fingerprint()
    assert current["policy"]["fixed_context_preparation_sha256"] == preparation_fingerprint()
    environments = current["policy"]["fixed_context_environments"]
    assert environments
    assert environments == {workspace: environment_fingerprint(Path(workspace)) for workspace in environments}
    validate_resume_manifest(deepcopy(current), current)
    with pytest.raises(ValueError, match="policy"):
        validate_resume_manifest(legacy, current, allow_endpoint_change=True)
    for key in ("command_parser_sha256", "fixed_context_preparation_sha256", "fixed_context_environments"):
        modified = deepcopy(current)
        modified["policy"][key] = "different-verifier"
        with pytest.raises(ValueError, match="policy"):
            validate_resume_manifest(current, modified)
