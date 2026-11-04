"""Remote retrieval preserves local evidence and rejects incompatible servers."""

import asyncio
import copy
import json
from types import SimpleNamespace

import httpx
import pytest

from dvp.toolkits.retrieval import backend, execution, remote
from dvp.toolkits.retrieval.remote import PROTOCOL, RemoteLeanExploreRetriever, RemoteRetrievalError


COMMIT = "a" * 40
INFO = {"protocol": PROTOCOL, "snapshot": "snapshot1", "lean_version": "4.35.0", "mathlib_commit": COMMIT}
HIT = {
    "fullname": "Nat.sample", "kind": "theorem", "module": "Mathlib.Sample",
    "filename": "Mathlib/Sample.lean", "source_origin": "lean_explore",
    "source_revision": COMMIT, "signature": "theorem sample : True",
    "semantic": "A sample true proposition.", "docstring": "Original documentation.",
    "dependencies": ["True.intro"], "_source_text": "theorem sample : True := by trivial",
    "context_status": "available", "context": {"namespace": "Nat", "complete": False},
}


def client(**kwargs):
    return RemoteLeanExploreRetriever(**{
        "url": "http://retrieval.internal:8765", "expected_lean_version": "4.35.0",
        "expected_mathlib_commit": COMMIT, "expected_snapshot": "snapshot1",
        "retries": 0, **kwargs,
    })


def reply(**overrides):
    return {"protocol": PROTOCOL, "metadata": copy.deepcopy(INFO),
            "results": [copy.deepcopy(HIT)], "count": 1, **overrides}


def transport(monkeypatch, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(remote.httpx, "AsyncClient", lambda **kwargs: real(
        transport=httpx.MockTransport(handler), **kwargs))


def test_http_roundtrip_preserves_enriched_content_and_proof_controls(monkeypatch):
    calls = []

    def respond(request):
        calls.append(request)
        assert request.headers["authorization"] == "Bearer test-secret"
        assert request.url.host == "retrieval.internal"
        if request.url.path == "/v1/info":
            return httpx.Response(200, json=INFO)
        body = json.loads(request.content)
        assert body["expected_mathlib_commit"] == COMMIT
        assert body["lean_name"] == "Nat.sample"
        return httpx.Response(200, json=reply())

    transport(monkeypatch, respond)
    retriever = client(token="test-secret")
    monkeypatch.setattr(execution, "get_cached_retriever", lambda: retriever)
    registry = SimpleNamespace(workspace=SimpleNamespace(target=None), retrieve_index=None)

    async def run():
        assert await retriever.info() == INFO
        hidden = await execution.lean_retrieve(registry, {"lean_name": "Nat.sample"})
        shown = await execution.lean_retrieve(registry, {"lean_name": "Nat.sample", "include_theorem_proof": True})
        return hidden["results"][0], shown["results"][0]

    hidden, shown = asyncio.run(run())
    assert "_source_text" not in hidden and "source_code" not in hidden
    assert shown["source_code"] == HIT["_source_text"]
    for key in ("semantic", "docstring", "dependencies", "context", "signature", "source_revision"):
        assert hidden[key] == shown[key] == HIT[key]
    assert shown["filename"] == "target/Mathlib/Sample.lean"
    assert len(calls) == 3  # Repeated queries still reach the server.


@pytest.mark.parametrize("field,value", [("mathlib_commit", "b" * 40), ("lean_version", "0.0.0"), ("snapshot", "other")])
def test_identity_checked_on_every_search(monkeypatch, field, value):
    responses = [reply(), reply(metadata={**INFO, field: value})]
    transport(monkeypatch, lambda _: httpx.Response(200, json=responses.pop(0)))

    async def run():
        retriever = client()
        assert await retriever.search(lean_name="Nat.sample")
        with pytest.raises(RemoteRetrievalError, match="mismatch"):
            await retriever.search(lean_name="Nat.sample")

    asyncio.run(run())


def test_mixed_source_revision_is_rejected(monkeypatch):
    transport(monkeypatch, lambda _: httpx.Response(200, json=reply(results=[{**HIT, "source_revision": "b" * 40}])))
    with pytest.raises(RemoteRetrievalError, match="source differs"):
        asyncio.run(client().search(lean_name="Nat.sample"))


@pytest.mark.parametrize("status", [401, 403, 409, 422])
def test_configuration_errors_not_retried_or_fallen_back(monkeypatch, status):
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(status, json={"detail": "configuration rejected"})
    transport(monkeypatch, respond)
    with pytest.raises(RemoteRetrievalError, match=f"HTTP {status}"):
        asyncio.run(client(retries=2).search(lean_name="Nat.sample"))
    assert len(calls) == 1


def test_transient_failure_retry_uses_only_configured_server(monkeypatch):
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(503 if len(calls) == 1 else 200, json={} if len(calls) == 1 else reply())
    transport(monkeypatch, respond)
    assert asyncio.run(client(retries=1).search(lean_name="Nat.sample")) == [HIT]
    assert len(calls) == 2 and all(r.url.host == "retrieval.internal" for r in calls)


def test_remote_selection_does_not_initialize_local_models(monkeypatch):
    monkeypatch.setattr(backend, "_RETRIEVER_CACHE", {})
    monkeypatch.setattr(backend, "LeanExploreRetriever", lambda **_: pytest.fail("No SDK/model loading for remote clients"))
    for key, value in {
        "DAVINCI_RETRIEVE_BACKEND": "lean_explore", "DAVINCI_LEANEXPLORE_BACKEND": "remote",
        "DAVINCI_LEANEXPLORE_URL": "http://retrieval.internal:8765",
        "DAVINCI_LEANEXPLORE_EXPECTED_LEAN_VERSION": "4.35.0",
        "DAVINCI_LEANEXPLORE_EXPECTED_MATHLIB_COMMIT": COMMIT,
    }.items():
        monkeypatch.setenv(key, value)
    first = backend.get_cached_retriever()
    assert first.backend == "remote" and first is backend.get_cached_retriever()
    monkeypatch.setenv("DAVINCI_LEANEXPLORE_TOKEN", "another-test-token")
    assert backend.get_cached_retriever() is not first


@pytest.mark.parametrize("url", ["", "file:///tmp/data", "http://user:password@example.com", "https://example.com?token=secret"])
def test_invalid_service_addresses_rejected(url):
    with pytest.raises(ValueError, match="service URL"):
        client(url=url)
