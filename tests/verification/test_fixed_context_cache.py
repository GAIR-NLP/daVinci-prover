"""Source-cache identity/transport tests; no Lean daemon or model is launched.

Small parser records isolate disk caching from Lean's separately tested syntax
engine. The API mock checks that cached source validation never replaces final
candidate verification.
"""

import asyncio
from copy import deepcopy
from dataclasses import asdict
import importlib.machinery
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from types import ModuleType

import pytest

import dvp.verification.context_cache as cache
import dvp.verification.fixed_context as contracts
from dvp.verification.lean import LeanVerificationConfig, LeanVerifier
from dvp.verification.command_parser import LeanCommand, LeanCommandParserUnavailable, _ast_fingerprint
from test_fixed_task_contract import fixture_metadata


REPO = Path(__file__).resolve().parents[2]
SOURCE = (
    "import Mathlib.Util.AssertNoSorry\n"
    "axiom sourceFact : True\n"
    "theorem helper : True := by sorry\n"
    "theorem target : True := by sorry\n"
)
CANDIDATE = (
    "theorem helper : True := by exact sourceFact\n"
    "theorem target : True := by exact helper\n"
)
SOURCE_IDENTITY = {"sha256": "fixture-sidecar"}


def source_contract(statement=SOURCE, *, metadata=None, source_identity=None):
    return contracts.FixedTaskContract.from_metadata(
        statement, metadata if metadata is not None else fixture_metadata(statement),
        source_identity=SOURCE_IDENTITY if source_identity is None else source_identity,
    )


def parser_records(code):
    """Valid record shapes for these fixtures' one-line, top-level commands."""
    commands = []
    cursor = 0
    for line in code.splitlines(keepends=True):
        content = line.strip()
        if not content or content.startswith("--"):
            cursor += len(line)
            continue
        start = cursor + len(line) - len(line.lstrip())
        end = start + len(content)
        word = content.split()[0]
        declaration = re.match(r"(axiom|theorem|lemma|def)\s+(\w+)", content)
        attributes = {}
        if declaration:
            decl_kind, name = declaration.groups()
            assignment = content.find(":=")
            signature = content[:assignment] if assignment >= 0 else content
            signature_ast = ["fixture_signature", " ".join(signature.split())]
            attributes = {
                "decl_kind": decl_kind, "decl_name": name,
                "decl_name_parts": (name,), "full_name": name,
                "signature_start": start,
                "signature_end": start + len(signature),
                "signature_ast": signature_ast,
                "signature_fingerprint": _ast_fingerprint(signature_ast),
            }
            if assignment >= 0:
                body = content[assignment + 2:]
                body_start = start + assignment + 2 + len(body) - len(body.lstrip())
                attributes.update(value_start=start + assignment, value_end=end,
                                  body_start=body_start, body_end=end)
            ast = ["fixture_declaration", signature_ast, content[assignment:] if assignment >= 0 else None]
        else:
            ast = ["fixture_command", content]
        commands.append(LeanCommand(
            start=start, end=end, source=code[start:end], kind="Lean.Parser.Command." + word,
            ast=ast, fingerprint=_ast_fingerprint(ast), **attributes,
        ))
        cursor += len(line)
    return commands


def environment_identity(workspace):
    workspace = Path(workspace).resolve()
    return {
        "workspace": str(workspace),
        "lean_toolchain": (workspace / "lean-toolchain").read_text(),
        "lake_manifest": (workspace / "lake-manifest.json").read_text(),
    }


def install_service(monkeypatch=None, *, forbid_parse=False):
    module = ModuleType("dvp.verification.parser_service")
    module.__spec__ = importlib.machinery.ModuleSpec(
        module.__name__, loader=None,
        origin=str(REPO / "src/dvp/verification/parser_service.py"),
    )
    calls = {"parse": [], "warmup": []}

    async def parse(code, workspace, timeout=3600, *, trusted_syntax=(), trusted_source=False):
        if forbid_parse:
            raise AssertionError("Source parser was called on a disk-cache hit")
        calls["parse"].append({"code": code, "trusted_source": trusted_source,
                               "trusted_syntax": tuple(trusted_syntax)})
        return parser_records(code)

    async def warmup(workspace, timeout=3600):
        calls["warmup"].append(str(Path(workspace).resolve()))

    module.parse_commands = parse
    module.warmup_parser = warmup
    module.environment_identity = environment_identity
    if monkeypatch is None:
        sys.modules[module.__name__] = module
    else:
        monkeypatch.setitem(sys.modules, module.__name__, module)
    return module, calls


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("DAVINCI_FIXED_CONTEXT_CACHE_DIR", str(tmp_path / "cache"))
    workspace = tmp_path / "mathlib"
    workspace.mkdir()
    (workspace / "lean-toolchain").write_text("leanprover/lean4:v4.35.0\n")
    (workspace / "lake-manifest.json").write_text('{"version":1}\n')
    module, calls = install_service(monkeypatch)
    return workspace, module, calls


def seed_cache(workspace, calls, statement=SOURCE):
    contract = source_contract(statement)
    asyncio.run(contract.validate(workspace))
    assert contract.source_cache_hit is False
    assert calls["parse"]
    return contract, cache.cache_path(contract, workspace)


def test_fresh_process_uses_disk_context_without_parser(isolated, tmp_path):
    workspace, _, calls = isolated
    contract, _ = seed_cache(workspace, calls)
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"statement": contract.statement, "metadata": contract.metadata,
                                   "source_identity": contract.source_identity, "workspace": str(workspace)}))
    child_env = dict(os.environ)
    child_env["PYTHONPATH"] = os.pathsep.join([str(REPO / "src"), child_env.get("PYTHONPATH", "")])
    process = subprocess.run([sys.executable, str(Path(__file__)), "--cache-hit-child", str(request)],
                             env=child_env, capture_output=True, text=True, timeout=30)
    assert process.returncode == 0, process.stdout + process.stderr
    assert json.loads(process.stdout) == {"cache_hit": True, "parser_calls": 0, "target": "target", "helpers": ["helper"]}


def test_run_workspace_symlink_reuses_same_environment(isolated, tmp_path):
    workspace, _, calls = isolated
    _, original_path = seed_cache(workspace, calls)
    alias = tmp_path / "run-target"
    alias.symlink_to(workspace, target_is_directory=True)
    calls["parse"].clear()
    fresh = source_contract()
    asyncio.run(fresh.validate(alias))
    assert fresh.source_cache_hit and calls["parse"] == []
    assert cache.cache_path(fresh, alias) == original_path


def test_actual_source_axiom_start_only_signature_survives_disk_cache(isolated):
    workspace, module, calls = isolated
    fixture = json.loads((Path(__file__).resolve().parents[1] / "fixtures/fixed_context_cache_source_axiom.json").read_text())
    records = []
    for raw in fixture["commands"]:
        raw = dict(raw)
        raw["namespace"] = tuple(raw["namespace"])
        raw["decl_name_parts"] = tuple(raw["decl_name_parts"])
        records.append(LeanCommand(**raw))
    axiom = next(command for command in records if command.decl_kind == "axiom")
    assert axiom.signature_start == axiom.start
    assert axiom.signature_end is None
    assert axiom.value_start is axiom.value_end is axiom.body_start is axiom.body_end is None
    contract = source_contract(fixture["statement"], metadata=fixture["metadata"])
    cache.save_context(contract, workspace, records, tuple(fixture["trusted_syntax"]))

    async def forbidden(*args, **kwargs):
        raise AssertionError("Actual axiom source record was unnecessarily reparsed")

    module.parse_commands = forbidden
    fresh = source_contract(fixture["statement"], metadata=fixture["metadata"])
    asyncio.run(fresh.validate(workspace))
    assert fresh.source_cache_hit and calls["parse"] == []
    loaded, syntax = cache.load_context(fresh, workspace)
    assert [asdict(command) for command in loaded] == [asdict(command) for command in records]
    assert syntax == ("open Complex",)
    assert fresh.target_name == "i_pow_mod_4"


@pytest.mark.parametrize("span", ["signature", "value", "body"])
def test_proof_bearing_declarations_reject_incomplete_spans(span, isolated):
    workspace, _, calls = isolated
    _, path = seed_cache(workspace, calls)
    payload = json.loads(path.read_text())
    target = payload["context"]["commands"][-1]
    assert target["value_start"] is not None and target["body_start"] is not None
    target[span + "_end"] = None
    payload["payload_sha256"] = cache.digest(payload["context"])
    path.write_text(json.dumps(payload))
    assert cache.load_context(source_contract(), workspace) is None


def test_start_only_axiom_signature_still_must_stay_inside_command(isolated):
    workspace, _, calls = isolated
    _, path = seed_cache(workspace, calls)
    payload = json.loads(path.read_text())
    axiom = next(record for record in payload["context"]["commands"] if record.get("decl_kind") == "axiom")
    axiom["signature_end"] = None
    axiom["signature_start"] = axiom["start"] - 1
    payload["payload_sha256"] = cache.digest(payload["context"])
    path.write_text(json.dumps(payload))
    assert cache.load_context(source_contract(), workspace) is None


@pytest.mark.parametrize("changed", ["source", "metadata", "sidecar", "parser", "preparation", "adapter", "toolchain", "dependencies"])
def test_identity_changes_force_source_revalidation(changed, isolated, monkeypatch):
    workspace, _, calls = isolated
    _, original_path = seed_cache(workspace, calls)
    fresh = source_contract()
    if changed == "source":
        fresh = source_contract("-- different source bytes\n" + SOURCE)
    elif changed == "metadata":
        metadata = fixture_metadata(SOURCE)
        metadata["cache_test_revision"] = 2
        fresh = source_contract(metadata=metadata)
    elif changed == "sidecar":
        fresh = source_contract(source_identity={"sha256": "different-sidecar"})
    elif changed == "parser":
        monkeypatch.setattr(cache, "parser_fingerprint", lambda: "different-parser")
    elif changed == "preparation":
        monkeypatch.setattr(contracts, "preparation_fingerprint", lambda: "different-preparation")
    elif changed == "adapter":
        monkeypatch.setattr(cache, "implementation_fingerprint", lambda: "different-adapter")
    elif changed == "toolchain":
        (workspace / "lean-toolchain").write_text("different Lean version")
    elif changed == "dependencies":
        (workspace / "lake-manifest.json").write_text('{"version":2}')
    calls["parse"].clear()
    assert cache.cache_path(fresh, workspace) != original_path
    assert cache.load_context(fresh, workspace) is None
    asyncio.run(fresh.validate(workspace))
    assert fresh.source_cache_hit is False
    assert len(calls["parse"]) == 1


def test_in_memory_context_rechecks_environment_identity(isolated):
    workspace, _, calls = isolated
    contract, _ = seed_cache(workspace, calls)
    calls["parse"].clear()
    (workspace / "lean-toolchain").write_text("updated Lean version")
    asyncio.run(contract.validate(workspace))
    assert len(calls["parse"]) == 1
    assert contract.source_cache_hit is False


@pytest.mark.parametrize("damage", ["truncated", "checksum", "schema", "source_text", "command_span", "ast", "signature", "body_span", "syntax", "target", "missing_helper"])
def test_corrupt_cache_is_rebuilt_without_dropping_source_obligations(damage, isolated):
    workspace, _, calls = isolated
    _, path = seed_cache(workspace, calls)
    payload = json.loads(path.read_text())
    context = payload["context"]
    target = context["commands"][-1]
    if damage == "schema":
        payload["identity"]["schema"] = "unknown-schema"
    elif damage == "source_text":
        target["source"] = target["source"].replace("True", "False")
    elif damage == "command_span":
        target["end"] += 50
    elif damage == "ast":
        target["ast"] = ["tampered"]
    elif damage == "signature":
        target["signature_fingerprint"] = "tampered"
    elif damage == "body_span":
        target["body_start"] = target["start"] - 1
    elif damage == "syntax":
        context["trusted_syntax"] = ["open Fake"]
    elif damage == "target":
        target["decl_name"] = target["full_name"] = "anotherTarget"
    elif damage == "missing_helper":
        context["commands"] = [record for record in context["commands"] if record.get("decl_name") != "helper"]
    payload["payload_sha256"] = cache.digest(context)
    if damage == "checksum":
        payload["payload_sha256"] = "broken-checksum"
    path.write_text("{broken" if damage == "truncated" else json.dumps(payload))
    assert cache.load_context(source_contract(), workspace) is None
    calls["parse"].clear()
    fresh = source_contract()
    asyncio.run(fresh.validate(workspace))
    assert fresh.source_cache_hit is False and len(calls["parse"]) == 1
    loaded, syntax = cache.load_context(source_contract(), workspace)
    assert syntax == ()
    assert [command.decl_name for command in loaded if command.decl_kind == "theorem"] == ["helper", "target"]


def test_corrupt_cache_with_unavailable_parser_fails_explicitly(isolated):
    workspace, module, calls = isolated
    _, path = seed_cache(workspace, calls)
    path.write_text("{broken")

    async def unavailable(*args, **kwargs):
        raise LeanCommandParserUnavailable("test parser unavailable")

    module.parse_commands = unavailable
    with pytest.raises(LeanCommandParserUnavailable, match="unavailable"):
        asyncio.run(source_contract().validate(workspace))


def test_unvalidated_missing_helper_is_never_published(isolated):
    workspace, _, _ = isolated
    contract = source_contract()
    records = [command for command in parser_records(contract.statement) if command.decl_name != "helper"]
    with pytest.raises(contracts.FixedContextError, match="original helper"):
        cache.save_context(contract, workspace, records, ())
    assert not cache.cache_path(contract, workspace).exists()


def test_cached_trusted_open_is_restored_for_original_task_specification(isolated):
    workspace, _, calls = isolated
    statement = SOURCE.replace("axiom sourceFact", "open Nat\naxiom sourceFact")
    seed_cache(workspace, calls, statement)
    calls["parse"].clear()
    fresh = source_contract(statement)
    spec = asyncio.run(fresh.comparator_spec(workspace))
    assert fresh.source_cache_hit and fresh._trusted_syntax == ("open Nat",)
    assert spec["theorem_names"] == ["target", "helper"]
    assert spec["permitted_axioms"][-1] == "sourceFact"
    assert fresh.statement == statement.strip()
    assert calls["parse"] == []


def test_comparator_spec_keeps_unused_helper_obligations_and_source_axioms(isolated):
    workspace, _, _ = isolated
    spec = asyncio.run(source_contract().comparator_spec(workspace))
    assert spec == {
        "theorem_names": ["target", "helper"],
        "definition_names": [],
        "permitted_axioms": ["propext", "Quot.sound", "Classical.choice", "sourceFact"],
    }


def test_comparator_spec_rejects_definition_holes_without_semantic_policy(isolated):
    workspace, _, _ = isolated
    statement = SOURCE.replace("theorem helper : True", "def helper : Nat")
    with pytest.raises(contracts.FixedContextError, match="unsupported_definition_obligation"):
        asyncio.run(source_contract(statement).comparator_spec(workspace))


def test_cached_original_metadata_does_not_rewrite_or_accept_candidate(isolated, tmp_path, monkeypatch):
    workspace, _, calls = isolated
    seed_cache(workspace, calls)
    calls["parse"].clear()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    verifier = LeanVerifier(LeanVerificationConfig(target_workspace=workspace, scratch_workspace=scratch))
    requests = []

    async def local_check(path, **kwargs):
        code = path.read_text(encoding="utf-8")
        requests.append(code)
        good = "missingTerm" not in code
        return {"success": good, "errors": [] if good else ["unknown identifier"], "warnings": [], "output": ""}

    monkeypatch.setattr(verifier, "_verify_local_file", local_check)
    good = asyncio.run(verifier.verify_code(CANDIDATE))
    bad = asyncio.run(verifier.verify_code(CANDIDATE.replace("exact helper", "exact missingTerm")))
    assert requests == [CANDIDATE, CANDIDATE.replace("exact helper", "exact missingTerm")]
    assert calls["parse"] == []
    assert good["success"] and not bad["success"]
    assert "fixed_task_contract" not in good
    # Ordinary compilation does not enforce original proof obligations. The
    # unchanged original task still supplies every obligation to Comparator.
    incomplete = asyncio.run(verifier.verify_code("theorem target : True := by trivial\n"))
    assert incomplete["success"] and len(requests) == 3
    fresh = source_contract()
    spec = asyncio.run(fresh.comparator_spec(workspace))
    assert fresh.source_cache_hit and calls["parse"] == []
    assert spec["theorem_names"] == ["target", "helper"]


def test_prepare_rows_warms_once_then_reports_persistent_cache_hits(isolated, monkeypatch):
    import dvp.cli.prepare_contexts as prepare_contexts

    workspace, module, calls = isolated
    monkeypatch.setattr(prepare_contexts, "warmup_parser", module.warmup_parser)
    rows = []
    for name in ("first", "second"):
        metadata = fixture_metadata(SOURCE)
        metadata["task_id"] = name
        rows.append({"task_id": name, "theorem_statement": SOURCE, "_fixed_task_contract": metadata,
                     "_fixed_contract_identity": SOURCE_IDENTITY})
    rows.append({"task_id": "legacy", "theorem_statement": SOURCE})
    messages = []
    first = asyncio.run(prepare_contexts.prepare_rows(rows, workspace, emit=messages.append))
    assert (first["tasks"], first["cache_hits"], first["prepared"]) == (2, 0, 2)
    assert len(calls["warmup"]) == 1 and len(calls["parse"]) == 2
    calls["parse"].clear()
    second = asyncio.run(prepare_contexts.prepare_rows(deepcopy(rows), workspace, emit=messages.append))
    assert (second["tasks"], second["cache_hits"], second["prepared"]) == (2, 2, 0)
    assert len(calls["warmup"]) == 2 and calls["parse"] == []
    assert all(row.get("_fixed_context_environment_sha256") == cache.environment_fingerprint(workspace) for row in rows[:2])
    assert any("cache hits: 2" in message for message in messages)


if __name__ == "__main__":
    assert sys.argv[1] == "--cache-hit-child"
    module, calls = install_service(forbid_parse=True)
    request = json.loads(Path(sys.argv[2]).read_text())
    contract = contracts.FixedTaskContract.from_metadata(
        request["statement"], request["metadata"], source_identity=request["source_identity"],
    )
    asyncio.run(contract.validate(Path(request["workspace"])))
    _, helpers, _ = contract._source_parts(contract._commands)
    print(json.dumps({"cache_hit": contract.source_cache_hit, "parser_calls": len(calls["parse"]),
                      "target": contract.target_name, "helpers": list(helpers)}))
