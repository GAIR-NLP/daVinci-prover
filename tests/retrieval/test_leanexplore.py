"""LeanExplore evidence must retain its own revision and optional context."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from dvp.runtime.workspace import Workspace
from dvp.toolkits.registry import ToolRegistry
from dvp.toolkits.retrieval import execution, explore
from dvp.toolkits.retrieval.explore import LeanExploreRetriever
from dvp.verification.lean import LeanVerificationConfig, LeanVerifier


REVISION = "a" * 40
LINK = f"https://github.com/leanprover-community/mathlib4/blob/{REVISION}/Mathlib/Sample.lean#L4-L4"
SOURCE = "theorem sample (a : G) : a * a⁻¹ = 1 := by simp"
FILE = "namespace Example\nvariable {G : Type*} [Group G]\n\n" + SOURCE + "\nend Example\n"


def api_hit(**overrides):
    values = dict(id=42, name="Example.sample", module="Mathlib.Sample",
                  source_text=SOURCE, source_link=LINK,
                  informalization="An element times its inverse is one.",
                  docstring="Group inverse law.", dependencies='["mul_inv_cancel", "HMul.hMul"]')
    values.update(overrides)
    return SimpleNamespace(**values)


def retriever(*hits, include_context=True):
    instance = object.__new__(LeanExploreRetriever)
    instance.backend = "api"
    instance.bm25_only = False
    instance.packages = ["Mathlib"]
    instance.rerank_top = 50
    instance.timeout = 1.0
    instance.retries = 2
    instance.include_context = include_context

    async def search(**_kwargs):
        return SimpleNamespace(results=list(hits))

    instance.client = SimpleNamespace(search=search)
    return instance


def mock_source(monkeypatch, handler):
    client_type = httpx.AsyncClient
    monkeypatch.setattr(explore.httpx, "AsyncClient", lambda **kwargs: client_type(
        transport=httpx.MockTransport(handler), **kwargs,
    ))


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


def test_retains_dependency_names_docstring_and_exact_source_revision():
    result = asyncio.run(retriever(api_hit(), include_context=False).search(lean_name="sample"))[0]
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
def test_requested_source_never_uses_local_file_at_remote_line_numbers(
    registry, monkeypatch, source, flag,
):
    client = retriever(api_hit(source_text=source), include_context=False)
    monkeypatch.setattr(execution, "get_cached_retriever", lambda: client)
    result = asyncio.run(registry.call("lean_retrieve", {"lean_name": "sample", flag: True}))
    assert result["success"]
    hit = result["results"][0]
    assert hit["source_code"] == source
    assert "unrelated" not in hit["source_code"]
    assert "_source_text" not in hit
    assert hit["source_revision"] == REVISION


def test_missing_remote_source_is_reported_instead_of_using_local_source(registry, monkeypatch):
    client = retriever(api_hit(source_text=""), include_context=False)
    monkeypatch.setattr(execution, "get_cached_retriever", lambda: client)
    result = asyncio.run(registry.call("lean_retrieve", {
        "lean_name": "sample", "include_def_proof": True, "include_theorem_proof": True,
    }))
    hit = result["results"][0]
    assert "source_code" not in hit
    assert "different revision" in hit["source_error"]


def test_source_is_optional_and_clipping_is_explicit(registry, monkeypatch):
    source = SOURCE + "\n-- " + "x" * 13000
    client = retriever(api_hit(source_text=source), include_context=False)
    monkeypatch.setattr(execution, "get_cached_retriever", lambda: client)
    result = asyncio.run(registry.call("lean_retrieve", {"lean_name": "sample"}))
    assert "source_code" not in result["results"][0]
    assert "_source_text" not in result["results"][0]
    result = asyncio.run(registry.call("lean_retrieve", {
        "lean_name": "sample", "include_theorem_proof": True,
    }))
    assert result["results"][0]["source_code"] == source[:12000]
    assert result["results"][0]["source_code_truncated"] is True


def test_context_comes_from_versioned_source_and_shares_fetch_within_one_call(monkeypatch):
    requested = []
    second_source = SOURCE.replace("theorem sample", "theorem alias")

    def respond(request):
        requested.append(str(request.url))
        return httpx.Response(200, text=FILE.replace("end Example", second_source + "\nend Example"))

    mock_source(monkeypatch, respond)
    client = retriever(api_hit(), api_hit(id=43, name="Example.alias", source_text=second_source,
                                         source_link=LINK.replace("#L4-L4", "#L5-L5")))
    results = asyncio.run(client.search(lean_name="sample", limit=2))
    assert requested == [f"https://raw.githubusercontent.com/leanprover-community/mathlib4/{REVISION}/Mathlib/Sample.lean"]
    assert len(results) == 2
    for result in results:
        assert result["context_status"] == "available"
        assert "Group G" in json.dumps(result["context"], ensure_ascii=False)
        assert "context_error" not in result


@pytest.mark.parametrize("response, message", [
    (httpx.Response(404), "404"),
    (httpx.Response(200, text=FILE.replace(SOURCE, "theorem unrelated : True := by trivial")), "does not match"),
    (httpx.Response(200, text="short\n"), "outside"),
    (httpx.Response(200, content=b"x" * (explore._MAX_SOURCE_BYTES + 1)), "2 MiB"),
])
def test_context_failure_does_not_lose_search_results(monkeypatch, response, message):
    mock_source(monkeypatch, lambda _: response)
    result = asyncio.run(retriever(api_hit()).search(lean_name="sample"))[0]
    assert result["fullname"] == "Example.sample"
    assert result["context_status"] == "unavailable"
    assert message in result["context_error"]
    assert "context" not in result


def test_context_timeout_keeps_hits(monkeypatch):
    async def slow_response(_request):
        await asyncio.sleep(1)
        return httpx.Response(200, text=FILE)

    mock_source(monkeypatch, slow_response)
    client = retriever(api_hit())
    client.timeout = 0.1
    result = asyncio.run(client.search(lean_name="sample"))[0]
    assert result["context_status"] == "unavailable"
    assert "time limit" in result["context_error"]


@pytest.mark.parametrize("link", [
    "https://[malformed/Sample.lean#L4-L4",
    "https://example.com/owner/repo/blob/" + REVISION + "/Sample.lean#L4-L4",
    "https://github.com/owner/repo/blob/main/Sample.lean#L4-L4",
    "https://github.com/owner/repo/blob/" + REVISION + "/%2e%2e/Sample.lean#L4-L4",
])
def test_unversioned_or_untrusted_urls_are_not_fetched(monkeypatch, link):
    def unexpected(_request):
        pytest.fail("No source request should be issued")

    mock_source(monkeypatch, unexpected)
    result = asyncio.run(retriever(api_hit(source_link=link)).search(lean_name="sample"))[0]
    assert result["context_status"] == "unavailable"
    assert "supported versioned source" in result["context_error"]


def test_generated_declaration_does_not_claim_its_generator_signature(registry, monkeypatch):
    source = "@[to_additive]\nlemma prod_attach : (∏ x ∈ s.attach, f x) = ∏ x ∈ s, f x := by simp"
    client = retriever(api_hit(name="Finset.sum_attach", source_text=source))
    monkeypatch.setattr(execution, "get_cached_retriever", lambda: client)

    def unexpected(_request):
        pytest.fail("A generator's context must not be fetched as the requested declaration's context")

    mock_source(monkeypatch, unexpected)
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


def test_library_note_is_not_a_giant_declaration_signature():
    hit = api_hit(name="LibraryNote.the_algebraic_hierarchy", source_text=(
        'library_note "the algebraic hierarchy" /-- ' + 'Documentation. ' * 1000 + ' -/'
    ))
    result = asyncio.run(retriever(hit, include_context=False).search(lean_name=hit.name))[0]
    assert result["signature"] is None
    assert result["signature_status"] == "unavailable"
    assert result["semantic"] == hit.informalization


def test_conflicting_context_does_not_reclassify_matching_source_header(monkeypatch):
    mock_source(monkeypatch, lambda request: httpx.Response(
        200, text=FILE.replace("namespace Example", "namespace Other"),
    ))
    hit = asyncio.run(retriever(api_hit()).search(lean_name="Example.sample"))[0]
    assert hit["signature_status"] == "source_header"
    assert hit["signature"].startswith("theorem sample")
    assert "source_code_role" not in hit
    assert "context extraction may be incomplete" in hit["context_error"]
    assert "#check Example.sample" in hit["context_error"]
    assert hit["context_status"] == "unavailable"
    assert "context" not in hit


def test_unreliable_context_keeps_header_but_omits_conflicting_scope(monkeypatch):
    mock_source(monkeypatch, lambda request: httpx.Response(200, text=FILE))
    context = {"namespace": "Other", "commands": [], "complete": False,
               "warnings": ["Unmatched end; context may be incomplete."]}
    monkeypatch.setattr(explore, "declaration_context", lambda *_: context)
    hit = asyncio.run(retriever(api_hit()).search(lean_name="Example.sample"))[0]
    assert hit["signature_status"] == "source_header"
    assert hit["signature"].startswith("theorem sample")
    assert hit["context_status"] == "unavailable"
    assert "context" not in hit
    assert "source_code_role" not in hit


def test_unreadable_namespace_does_not_become_verified_context(monkeypatch):
    mock_source(monkeypatch, lambda request: httpx.Response(200, text=FILE))
    monkeypatch.setattr(explore, "declaration_context", lambda *_: {
        "namespace": "(unknown)", "commands": [], "complete": False, "warnings": [],
    })
    hit = asyncio.run(retriever(api_hit()).search(lean_name="Example.sample"))[0]
    assert hit["signature_status"] == "source_header"
    assert hit["context_status"] == "unavailable"
    assert "context" not in hit


def test_api_rate_limit_is_retried_without_losing_the_query():
    calls = []
    client = retriever(include_context=False)

    async def search(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            response = httpx.Response(429, headers={"Retry-After": "0"},
                                      request=httpx.Request("GET", "https://www.leanexplore.com/api/v2/search"))
            response.raise_for_status()
        return SimpleNamespace(results=[api_hit()])

    client.client.search = search
    results = asyncio.run(client.search(lean_name="Example.sample", limit=3))
    assert len(calls) == 2 and calls[0] == calls[1]
    assert results[0]["fullname"] == "Example.sample"


def test_multiple_api_queries_share_one_search_time_budget():
    client = retriever(include_context=False)
    client.timeout = 0.1
    calls = []

    async def search(**kwargs):
        calls.append(kwargs["query"])
        await asyncio.sleep(0.06)
        return SimpleNamespace(results=[api_hit()])

    client.client.search = search
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(client.search(lean_name="Example.sample", keywords="inverse", natural_language_query="inverse law"))
    assert calls == ["Example.sample", "inverse"]
