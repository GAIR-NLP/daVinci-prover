"""Context activation publishes evidence only after full source validation."""

import argparse
import asyncio
import hashlib
import json

import pytest

from dvp.cli import prepare_contexts
from dvp.verification.fixed_context import load_contract_sidecar
from test_fixed_task_contract import fixture_metadata


def staged_dataset(tmp_path):
    dataset = tmp_path / "fixed_context.jsonl"
    rows = [{"task_id": f"target_{split}", "split": split,
             "theorem_statement": f"import Mathlib\ntheorem target_{split} : True := by sorry"}
            for split in ("train", "test")]
    tasks = []
    for row in rows:
        metadata = fixture_metadata(row["theorem_statement"])
        metadata["task_id"] = row["task_id"]
        tasks.append(metadata)
    dataset.write_text("".join(json.dumps(row) + "\n" for row in rows))
    dataset.with_suffix(".contract.json").write_text(json.dumps({
        "schema_version": "davinci.benchmark_contract.v1",
        "dataset_sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(),
        "offset_unit": "python_unicode_codepoints", "tasks": tasks,
    }))
    dataset.with_suffix(".contract.ready.json").write_text('{"previous": true}\n')
    return dataset


def options(dataset, tmp_path):
    return argparse.Namespace(dataset=[dataset], split="test", target_workspace=tmp_path,
                              lean_timeout=1, activate=True, report=None)


@pytest.mark.parametrize("line_ending", [b"\n", b"\r\n"])
def test_activation_validates_every_row_and_preserves_previous_evidence(tmp_path, monkeypatch, line_ending):
    dataset = staged_dataset(tmp_path)
    ready = dataset.with_suffix(".contract.ready.json")
    ready.write_bytes(ready.read_bytes().replace(b"\n", line_ending))
    previous = ready.read_bytes()
    source_bytes = dataset.read_bytes()
    sidecar_bytes = dataset.with_suffix(".contract.json").read_bytes()
    received = []
    async def prepare(rows, workspace, **kwargs):
        received.extend(rows)
        assert ready.read_bytes() == previous
        return {"tasks": len(rows), "environments": {str(workspace): "test-environment"}}
    monkeypatch.setattr(prepare_contexts, "prepare_rows", prepare)
    asyncio.run(prepare_contexts._main(options(dataset, tmp_path)))
    assert len(received) == 2
    assert {row["split"] for row in received} == {"train", "test"}
    assert all(row.get("_fixed_task_contract") for row in received)
    archive = dataset.with_suffix(f".contract.ready.{hashlib.sha256(previous).hexdigest()}.json")
    assert archive.read_bytes() == previous
    assert dataset.read_bytes() == source_bytes
    assert dataset.with_suffix(".contract.json").read_bytes() == sidecar_bytes
    rows = [json.loads(line) for line in dataset.read_text().splitlines()]
    assert load_contract_sidecar(dataset, rows, require_active=True)
    assert json.loads(ready.read_text())["validation"]["tasks"] == 2


@pytest.mark.parametrize("change_during_validation", [False, True])
def test_failure_or_concurrent_source_change_cannot_publish_activation(tmp_path, monkeypatch, change_during_validation):
    dataset = staged_dataset(tmp_path)
    ready = dataset.with_suffix(".contract.ready.json")
    previous = ready.read_bytes()
    async def prepare(rows, workspace, **kwargs):
        if change_during_validation:
            dataset.write_bytes(dataset.read_bytes() + b"\n")
            return {"tasks": len(rows), "environments": {}}
        raise ValueError("source validation failed")
    monkeypatch.setattr(prepare_contexts, "prepare_rows", prepare)
    with pytest.raises(ValueError, match="changed during activation|source validation failed"):
        asyncio.run(prepare_contexts._main(options(dataset, tmp_path)))
    assert ready.read_bytes() == previous
    assert not list(tmp_path.glob("*.contract.ready.*.json"))


def test_cli_requires_an_explicit_dataset(capsys):
    with pytest.raises(SystemExit) as error:
        prepare_contexts.main([])
    assert error.value.code == 2
    assert "--dataset" in capsys.readouterr().err
