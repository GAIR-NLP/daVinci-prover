"""Tool behavior frozen during the dvp package migration, without model calls."""

import asyncio
import hashlib
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

from dvp.runtime.workspace import Workspace
from dvp.toolkits.registry import ToolRegistry
from dvp.toolkits.schemas import TOOL_NAMES
from dvp.toolkits.retrieval import execution
from dvp.toolkits.retrieval.local import LeanRetriever
from dvp.toolkits.surface import CompatibilitySurface


class RecordingVerifier:
    def __init__(self, *, draft=False):
        self.config = SimpleNamespace(
            target_workspace=None, timeout=1,
        )
        self.draft = draft
        self.calls = []

    async def verify_code(self, code, **kwargs):
        self.calls.append((code, kwargs))
        sorries = [{"pos": {"line": 1, "column": 25}, "goal": "⊢ True"}] if self.draft else []
        return {"success": True, "lean_executed": True, "response": {"sorries": sorries}}

    async def verify_file(self, path, **kwargs):
        return await self.verify_code(Path(path).read_text(), **kwargs)


@pytest.fixture
def registry(tmp_path):
    workspace = Workspace(root=tmp_path / "run")
    workspace.setup()
    tools = ToolRegistry(
        workspace=workspace, verifier=RecordingVerifier(),
    )
    tools.draft_verifier = RecordingVerifier(draft=True)
    return tools


def test_schema_shape_matches_migration_baseline(registry):
    # Includes batch edits and the five-result retrieval default. Descriptions
    # remain free to change; parameter shapes, order, and filtering are checked.
    def without_descriptions(value):
        if isinstance(value, dict):
            return {key: without_descriptions(item) for key, item in value.items()
                    if key != "description"}
        if isinstance(value, list):
            return [without_descriptions(item) for item in value]
        return value

    for schemas, expected in [
        (registry.schemas(), "582fec02daa7a752105ffa9a46ca7c1860112369c4d663c521f4354f7858b66c"),
        (CompatibilitySurface(registry).schemas(), "582fec02daa7a752105ffa9a46ca7c1860112369c4d663c521f4354f7858b66c"),
        (CompatibilitySurface(registry, enable_lean_retrieve=False).schemas(), "738fa2aa8add54f723faa998435c135b2dede5408e91b8dfd4b26d1c757b26b1"),
    ]:
        serialized = json.dumps(without_descriptions(schemas), ensure_ascii=False, separators=(",", ":"))
        assert hashlib.sha256(serialized.encode()).hexdigest() == expected
    assert tuple(item["function"]["name"] for item in registry.schemas()) == TOOL_NAMES
    assert tuple(registry._tools) == TOOL_NAMES


def test_descriptions_explain_checks_without_prescribing_exploration_order(registry):
    descriptions = {item["function"]["name"]: item["function"]["description"]
                    for item in registry.schemas()}
    for name in ("file_edit", "file_write"):
        assert "before another tool runs or the response ends" in descriptions[name]
        assert "Non-Lean files are not checked" in descriptions[name]
    verify = descriptions["lean_verify"]
    for fact in ("Check one", "inline code", "local module dependencies", "Submission.Count", "nested modules",
                 "compiler diagnostics", "local hole goals",
                 "up to three", "Earlier compiler errors"):
        assert fact in verify
    assert "1-indexed line range" in descriptions["file_read"]
    combined = " ".join(descriptions.values()).lower()
    for removed in ("before the first", "start with", "primary exact-target proof loop",
                    "concrete current diagnostic", "before an unrelated probe",
                    "reward", "comparator", "strict_verified", "ends the episode"):
        assert removed not in combined


@pytest.mark.parametrize("name", ["bash_execute", "submit_result"])
def test_removed_tools_cannot_dispatch_or_verify(registry, name):
    result = asyncio.run(registry.call(name, {}))
    assert result == {"success": False, "error": f"Unknown tool: {name}", "stop": False}
    assert registry.verifier.calls == registry.draft_verifier.calls == []


def test_lean_write_and_edit_check_saved_files_and_allow_explicit_recheck(registry):
    async def run():
        draft = await registry.call("file_write", {
            "file_path": "Submission/Main.lean", "content": "example : True := by sorry",
        })
        assert draft["success"] and draft["lean_executed"] is True
        assert draft["draft_typechecked"] and not draft["strict_verified"]
        assert registry.draft_verifier.calls == [("example : True := by sorry", {"max_output_chars": 12000})]
        assert registry.verifier.calls == []
        saved = await registry.call("file_edit", {
            "file_path": "Submission/Main.lean", "old_str": "sorry", "new_str": "trivial",
        })
        assert saved["success"] and saved["lean_executed"] is True
        assert saved["strict_verified"]
        persisted = (registry.workspace.submission_dir / "Main.lean").read_text()
        assert persisted == "example : True := by trivial"
        assert registry.verifier.calls == [(persisted, {"max_output_chars": 12000})]
        strict = await registry.call("lean_verify", {"file_path": "Submission/Main.lean"})
        assert strict["success"] and strict["strict_verified"]
        assert registry.verifier.calls == [(persisted, {"max_output_chars": 12000})] * 2
    asyncio.run(run())


@pytest.mark.parametrize("nested", [False, True])
def test_local_draft_feedback_keeps_all_raw_holes(registry, monkeypatch, nested):
    holes = [{"pos": {"line": i + 1, "column": 4}, "goal": f"h{i} : True\n⊢ True"}
             for i in range(4)]
    payload = {"sorries": holes}
    verification = {"success": True, **({"response": payload} if nested else payload)}

    async def verify(_code, **_kwargs):
        return verification

    monkeypatch.setattr(registry.draft_verifier, "verify_code", verify)
    result = asyncio.run(registry.call("lean_verify", {"code": "example : True := by sorry"}))
    raw = result["response"] if nested else result
    assert raw["sorries"] == holes
    assert result["holes"]["total"] == 4 and result["holes"]["omitted"] == 1
    assert [item["goal"] for item in result["holes"]["items"]] == [hole["goal"] for hole in holes[:3]]
    assert result["draft_typechecked"] and not result["strict_verified"]


@pytest.mark.parametrize("arguments, error", [
    ({"file_path": "Submission/note.txt", "old_str": "absent", "new_str": "β"}, "old_str not found"),
    ({"file_path": "Submission/note.txt", "old_str": "α", "new_str": "β", "replace_all": "true"}, "TypeError: replace_all must be a boolean"),
    ({"file_path": "Submission/note.txt", "old_str": "α", "new_str": "β", "old_line_range": [True, 2]}, "TypeError: old_line_range items must be integers or null"),
])
def test_invalid_edit_preserves_file_and_feedback(registry, arguments, error):
    path = registry.workspace.submission_dir / "note.txt"
    path.write_text("α α\n")
    assert asyncio.run(registry.call("file_edit", arguments)) == {"success": False, "error": error, "stop": False}
    assert path.read_text() == "α α\n"


def test_replacement_to_new_file_and_read_line_ranges(registry):
    source = registry.workspace.submission_dir / "note.txt"
    source.write_text("α α\nα tail\n")
    result = asyncio.run(registry.call("file_edit", {
        "file_path": "Submission/note.txt", "old_str": "α", "new_str": "β", "replace_all": True,
        "target_path": "Submission/copy.txt",
    }))
    assert result["success"] and not result["lean_executed"]
    assert source.read_text() == "α α\nα tail\n"
    assert (source.parent / "copy.txt").read_text() == "β β\nβ tail\n"
    assert asyncio.run(registry.call("file_read", {"file_path": "Submission/copy.txt", "line_range": [2, 3]})) == {
        "success": True, "file_path": "Submission/copy.txt", "total_lines": 2,
        "requested_line_range": [2, 3], "line_range": [2, 3],
        "returned_line_range": [2, 3], "content": "     2|β tail",
        "truncated": False, "remaining_lines": 0, "next_line_range": None,
        "stop": False,
    }


def test_content_search_python_fallback_and_alias(registry, monkeypatch):
    from dvp.toolkits import search
    monkeypatch.setattr(search.shutil, "which", lambda _: None)
    (registry.workspace.submission_dir / "note.txt").write_text("first\nneedle β\nneedle again\n")
    result = asyncio.run(registry.call("to=functions.functions.search", {
        "path": "Submission", "query": "needle", "use_regex": False,
    }))
    assert result == {
        "success": True,
        "results": [
            {
                "file": "Submission/note.txt", "line": line, "text": text,
                "context_start_line": 1, "context_end_line": 3,
                "context": "first\nneedle β\nneedle again",
            }
            for line, text in [(2, "needle β"), (3, "needle again")]
        ],
        "count": 2, "file_count": 1, "truncated": False,
        "file_limit_reached": False, "files_with_more_matches": [],
        "scan_limit_reached": False, "stop": False,
    }


def test_local_retrieval_keeps_source_enrichment_and_retry_markers(registry, tmp_path):
    registry.retrieve_index = tmp_path / "unused.jsonl"
    target = registry.workspace.workspaces_dir / "target"
    target.mkdir()
    (target / "Nat.lean").write_text("-- header\ntheorem sample : True := by trivial\n")

    async def retrieve(_index, **_kwargs):
        return [{"kind": "theorem", "filename": "Nat.lean", "line_start": 2, "line_end": 2}]

    registry._retrieval_search = retrieve
    result = asyncio.run(registry.call("lean_retrieve", {"lean_name": "sample", "include_theorem_proof": True}))
    assert result == {
        "success": True, "results": [{"kind": "theorem", "filename": "target/Nat.lean", "line_start": 2, "line_end": 2, "source_code": "theorem sample : True := by trivial"}],
        "count": 1, "retrieval_backend": "local", "stop": False,
    }

    async def unavailable(_index, **_kwargs):
        raise execution.RetrievalInfrastructureError("busy", "temporarily unavailable")

    registry._retrieval_search = unavailable
    result = asyncio.run(registry.call("lean_retrieve", {"lean_name": "sample"}))
    assert result == {
        "success": False, "stop": False, "error": "temporarily unavailable", "results": [],
        "_harness_infrastructure_error": True, "_harness_retryable_service_error": True, "_harness_reason": "busy",
    }


@pytest.mark.parametrize("asynchronous", [False, True])
def test_configured_retrieval_dispatch_keeps_queries_and_embedded_source(
    registry, monkeypatch, asynchronous,
):
    calls = []
    source = "theorem sample : True := by trivial"

    def search(**kwargs):
        calls.append(kwargs)
        return [{"kind": "theorem", "filename": "Sample.lean", "_source_text": source}]

    async def async_search(**kwargs):
        return search(**kwargs)

    retriever = SimpleNamespace(search=async_search if asynchronous else search)
    monkeypatch.setattr(execution, "get_cached_retriever", lambda: retriever)
    monkeypatch.setenv("DAVINCI_RETRIEVE_BACKEND", "lean_explore")
    result = asyncio.run(registry.call("lean_retrieve", {
        "lean_name": "sample", "keywords": ["True"], "limit": 7,
        "include_theorem_proof": True,
    }))
    assert calls == [{"natural_language_query": None, "lean_name": "sample",
                      "keywords": ["True"], "limit": 7}]
    assert result == {
        "success": True, "count": 1, "retrieval_backend": "lean_explore", "stop": False,
        "results": [{"kind": "theorem", "filename": "target/Sample.lean", "source_code": source}],
    }


def test_jsonl_retriever_retains_exact_name_priority_and_deduplication(tmp_path):
    index = tmp_path / "index.jsonl"
    records = [
        {"name": "add_zero", "fullname": "Nat.add_zero", "signature": "theorem add_zero (n : Nat) : n + 0 = n", "keywords": ["nat", "addition", "zero"], "file_path": "Nat.lean"},
        {"name": "add_zero_more", "fullname": "Nat.add_zero_more", "signature": "theorem add_zero_more : True", "keywords": ["addition"], "file_path": "Other.lean"},
    ]
    index.write_text("".join(json.dumps(row) + "\n" for row in records))
    result = LeanRetriever(index).search(lean_name="Nat.add_zero", keywords="addition", limit=10)
    assert [item["fullname"] for item in result] == ["Nat.add_zero", "Nat.add_zero_more"]
    assert [item["score"] for item in result] == [100.0, 80.0]
    assert [item["reason"] for item in result] == ["name match", "name match"]


def test_local_search_admission_caps_concurrency_and_loads_index_once(tmp_path, monkeypatch):
    index = tmp_path / "unique-index.jsonl"
    counters = {"constructs": 0, "active": 0, "peak": 0}
    lock = threading.Lock()

    class Retriever:
        def __init__(self, _path):
            with lock:
                counters["constructs"] += 1

        def search(self, **_kwargs):
            with lock:
                counters["active"] += 1
                counters["peak"] = max(counters["peak"], counters["active"])
            time.sleep(0.02)
            with lock:
                counters["active"] -= 1
            return [{"fullname": "Nat.sample"}]

    monkeypatch.setattr(execution, "LeanRetriever", Retriever)
    monkeypatch.setenv("DVP_RETRIEVAL_CONCURRENCY", "2")

    async def run():
        return await asyncio.gather(*[
            execution._search_local_retriever(index, natural_language_query=None, lean_name="sample", keywords=None, limit=1)
            for _ in range(4)
        ])

    assert asyncio.run(run()) == [[{"fullname": "Nat.sample"}]] * 4
    assert counters == {"constructs": 1, "active": 0, "peak": 2}
