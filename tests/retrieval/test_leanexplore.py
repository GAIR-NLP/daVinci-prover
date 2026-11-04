"""LeanExplore evidence must retain its own revision and optional context."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from dvp.runtime.workspace import Workspace
from dvp.toolkits.registry import ToolRegistry
from dvp.toolkits.retrieval import execution, explore
from dvp.toolkits.retrieval.explore import LeanExploreRetriever
from dvp.verification.lean import LeanVerificationConfig, LeanVerifier
from test_leanexplore_local import sdk, sources


REVISION = "a" * 40
LINK = f"https://github.com/leanprover-community/mathlib4/blob/{REVISION}/Mathlib/Sample.lean#L4-L4"
SOURCE = "theorem sample (a : G) : a * a⁻¹ = 1 := by simp"
FILE = "namespace Example\nvariable {G : Type*} [Group G]\n\n" + SOURCE + "\nend Example\n"


def search_hit(**overrides):
    values = dict(id=42, name="Example.sample", module="Mathlib.Sample",
                  source_text=SOURCE, source_link=LINK,
                  informalization="An element times its inverse is one.",
                  docstring="Group inverse law.", dependencies='["mul_inv_cancel", "HMul.hMul"]')
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture
def retriever(sdk, sources):
    source_dir, _ = sources

    def create(*hits, include_context=True):
        sdk.hits = list(hits)
        return LeanExploreRetriever(
            packages=["Mathlib"], include_context=include_context, source_dir=source_dir,
        )

    return create


@pytest.fixture
def registry(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    (target / "Mathlib").mkdir()
    (target / "Mathlib/Sample.lean").write_text(
        "-- old version\n\n\ninstance unrelated : Inhabited Nat := ⟨0⟩\n"
    )
    workspace = Workspace(tmp_path / "run", target_workspace=target)
    workspace.setup()
    verifier = LeanVerifier(LeanVerificationConfig(
        target_workspace=target, scratch_workspace=workspace.submission_dir, timeout=1,
    ))
    return ToolRegistry(workspace=workspace, verifier=verifier)


def test_retains_dependency_names_docstring_and_exact_source_revision(retriever):
    result = asyncio.run(retriever(search_hit(), include_context=False).search(lean_name="sample"))[0]
    assert result["id"] == 42
    assert result["module"] == "Mathlib.Sample"
    assert result["dependencies"] == ["mul_inv_cancel", "HMul.hMul"]
    assert result["docstring"] == "Group inverse law."
    assert result["source_repository"] == "https://github.com/leanprover-community/mathlib4"
    assert result["source_revision"] == REVISION
    assert result["source_origin"] == "lean_explore"
    assert result["score_kind"] == "reciprocal_rank"
    assert result["context_status"] == "disabled"


@pytest.mark.parametrize("source, flag", [
    (SOURCE, "include_theorem_proof"),
    ("def sample : Nat := 42", "include_def_proof"),
])
def test_requested_source_never_uses_workspace_file_at_indexed_line_numbers(
    registry, monkeypatch, retriever, source, flag,
):
    client = retriever(search_hit(source_text=source), include_context=False)
    monkeypatch.setattr(execution, "get_cached_retriever", lambda: client)
    result = asyncio.run(registry.call("lean_retrieve", {"lean_name": "sample", flag: True}))
    assert result["success"]
    hit = result["results"][0]
    assert hit["source_code"] == source
    assert "unrelated" not in hit["source_code"]
    assert "_source_text" not in hit
    assert hit["source_revision"] == REVISION


def test_missing_indexed_source_is_reported_instead_of_using_workspace_source(registry, monkeypatch, retriever):
    client = retriever(search_hit(source_text=""), include_context=False)
    monkeypatch.setattr(execution, "get_cached_retriever", lambda: client)
    result = asyncio.run(registry.call("lean_retrieve", {
        "lean_name": "sample", "include_def_proof": True, "include_theorem_proof": True,
    }))
    hit = result["results"][0]
    assert "source_code" not in hit
    assert "different revision" in hit["source_error"]


def test_source_is_optional_and_clipping_is_explicit(registry, monkeypatch, retriever):
    source = SOURCE + "\n-- " + "x" * 13000
    client = retriever(search_hit(source_text=source), include_context=False)
    monkeypatch.setattr(execution, "get_cached_retriever", lambda: client)
    result = asyncio.run(registry.call("lean_retrieve", {"lean_name": "sample"}))
    assert "source_code" not in result["results"][0]
    assert "_source_text" not in result["results"][0]
    result = asyncio.run(registry.call("lean_retrieve", {
        "lean_name": "sample", "include_theorem_proof": True,
    }))
    assert result["results"][0]["source_code"] == source[:12000]
    assert result["results"][0]["source_code_truncated"] is True


def test_context_reads_versioned_source_once_per_file(monkeypatch, retriever, sources):
    _, directory = sources
    second_source = SOURCE.replace("theorem sample", "theorem alias")
    (directory / "Mathlib/Sample.lean").write_text(
        FILE.replace("end Example", second_source + "\nend Example"), encoding="utf-8",
    )
    client = retriever(search_hit(), search_hit(
        id=43, name="Example.alias", source_text=second_source,
        source_link=LINK.replace("#L4-L4", "#L5-L5"),
    ))
    read_source = client._read_local_source
    reads = []

    def counted_read(item):
        reads.append(item["source_link"])
        return read_source(item)

    monkeypatch.setattr(client, "_read_local_source", counted_read)
    results = asyncio.run(client.search(lean_name="sample", limit=2))
    assert reads == [LINK]
    assert len(results) == 2
    for result in results:
        assert result["context_status"] == "available"
        assert "Group G" in json.dumps(result["context"], ensure_ascii=False)
        assert "context_error" not in result


@pytest.mark.parametrize("link", [
    "https://[malformed/Sample.lean#L4-L4",
    "https://example.com/owner/repo/blob/" + REVISION + "/Sample.lean#L4-L4",
    "https://github.com/owner/repo/blob/main/Sample.lean#L4-L4",
    "https://github.com/owner/repo/blob/" + REVISION + "/%2e%2e/Sample.lean#L4-L4",
])
def test_unversioned_or_untrusted_source_locations_are_not_read(monkeypatch, retriever, link):
    def unexpected(_item):
        pytest.fail("No local source file should be read for an invalid source location")

    client = retriever(search_hit(source_link=link))
    monkeypatch.setattr(client, "_read_local_source", unexpected)
    result = asyncio.run(client.search(lean_name="sample"))[0]
    assert result["context_status"] == "unavailable"
    assert "supported versioned source" in result["context_error"]


def test_generated_declaration_does_not_claim_its_generator_signature(registry, monkeypatch, retriever):
    source = "@[to_additive]\nlemma prod_attach : (∏ x ∈ s.attach, f x) = ∏ x ∈ s, f x := by simp"
    client = retriever(search_hit(name="Finset.sum_attach", source_text=source))
    monkeypatch.setattr(execution, "get_cached_retriever", lambda: client)

    def unexpected(_item):
        pytest.fail("A generator's context must not be read as the requested declaration's context")

    monkeypatch.setattr(client, "_read_local_source", unexpected)
    result = asyncio.run(registry.call("lean_retrieve", {
        "lean_name": "Finset.sum_attach", "include_theorem_proof": True,
    }))
    hit = result["results"][0]
    assert hit["fullname"] == "Finset.sum_attach"
    assert hit["signature"] is None
    assert hit["signature_status"] == "name_mismatch"
    assert hit["source_declaration"] == "prod_attach"
    assert "#check Finset.sum_attach" in hit["signature_error"]
    assert hit["source_code"] == source
    assert hit["source_code_role"] == "generator"
    assert hit["context_status"] == "unavailable"
    assert "context" not in hit


def test_library_note_is_not_a_giant_declaration_signature(retriever):
    hit = search_hit(name="LibraryNote.the_algebraic_hierarchy", source_text=(
        'library_note "the algebraic hierarchy" /-- ' + 'Documentation. ' * 1000 + ' -/'
    ))
    result = asyncio.run(retriever(hit, include_context=False).search(lean_name=hit.name))[0]
    assert result["signature"] is None
    assert result["signature_status"] == "unavailable"
    assert result["semantic"] == hit.informalization


def test_conflicting_context_does_not_reclassify_matching_source_header(retriever, sources):
    _, directory = sources
    (directory / "Mathlib/Sample.lean").write_text(
        FILE.replace("namespace Example", "namespace Other"), encoding="utf-8",
    )
    hit = asyncio.run(retriever(search_hit()).search(lean_name="Example.sample"))[0]
    assert hit["signature_status"] == "source_header"
    assert hit["signature"].startswith("theorem sample")
    assert "source_code_role" not in hit
    assert "context extraction may be incomplete" in hit["context_error"]
    assert "#check Example.sample" in hit["context_error"]
    assert hit["context_status"] == "unavailable"
    assert "context" not in hit


def test_unreliable_context_keeps_header_but_omits_conflicting_scope(monkeypatch, retriever):
    context = {"namespace": "Other", "commands": [], "complete": False,
               "warnings": ["Unmatched end; context may be incomplete."]}
    monkeypatch.setattr(explore, "declaration_context", lambda *_: context)
    hit = asyncio.run(retriever(search_hit()).search(lean_name="Example.sample"))[0]
    assert hit["signature_status"] == "source_header"
    assert hit["signature"].startswith("theorem sample")
    assert hit["context_status"] == "unavailable"
    assert "context" not in hit
    assert "source_code_role" not in hit


def test_unreadable_namespace_does_not_become_verified_context(monkeypatch, retriever):
    monkeypatch.setattr(explore, "declaration_context", lambda *_: {
        "namespace": "(unknown)", "commands": [], "complete": False, "warnings": [],
    })
    hit = asyncio.run(retriever(search_hit()).search(lean_name="Example.sample"))[0]
    assert hit["signature_status"] == "source_header"
    assert hit["context_status"] == "unavailable"
    assert "context" not in hit
