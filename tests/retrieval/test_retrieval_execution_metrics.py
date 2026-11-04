"""Execution settings and timings must not alter model-visible search evidence."""

import asyncio
from types import SimpleNamespace

import pytest

from dvp.agent.result_projection import _compact
from dvp.protocol import ToolOutcome
from dvp.toolkits.retrieval import backend, execution
from dvp.toolkits.retrieval.explore_local import LocalSearchRuntime, SearchResults


def test_pipeline_settings_separate_shared_retrievers(monkeypatch):
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(backend, "LeanExploreRetriever", create)
    monkeypatch.setattr(backend, "_RETRIEVER_CACHE", {})
    monkeypatch.setenv("DAVINCI_RETRIEVE_BACKEND", "lean_explore")
    monkeypatch.setenv("DAVINCI_LEANEXPLORE_BACKEND", "local")
    monkeypatch.setenv("DAVINCI_LEANEXPLORE_LOCAL_CONCURRENCY", "2")
    monkeypatch.setenv("DAVINCI_LEANEXPLORE_LOCAL_MAX_PENDING", "30")
    first = backend.get_cached_retriever()
    assert backend.get_cached_retriever() is first
    assert calls[0]["local_concurrency"] == 2
    assert calls[0]["local_max_pending"] == 30
    monkeypatch.setenv("DAVINCI_LEANEXPLORE_LOCAL_CONCURRENCY", "4")
    assert backend.get_cached_retriever() is not first
    assert len(calls) == 2


def test_search_timings_are_recorded_without_entering_model_context(monkeypatch):
    timings = {"queue_seconds": 2.5, "search_seconds": 1.0, "total_seconds": 3.5}

    async def search(**_):
        return SearchResults([{"fullname": "Nat.example", "signature": "theorem Nat.example : True",
                               "source_origin": "lean_explore", "filename": "Mathlib/Example.lean"}], timings)

    monkeypatch.setattr(execution, "get_cached_retriever", lambda: SimpleNamespace(search=search))
    monkeypatch.setenv("DAVINCI_RETRIEVE_BACKEND", "lean_explore")
    registry = SimpleNamespace(retrieve_index=None, workspace=SimpleNamespace(target=None))
    raw = asyncio.run(execution.lean_retrieve(registry, {"lean_name": "Nat.example"}))
    assert raw["_harness_retrieval_timing"] == timings
    assert type(raw["results"]) is list
    outcome = ToolOutcome(call_id="query", name="lean_retrieve", backend_name="lean_retrieve",
                          arguments={}, executed=True, valid=True, verified=None,
                          terminal=False, reward=0.0, raw=raw)
    assert outcome.to_dict()["raw"] == raw
    without_timings = {k: v for k, v in raw.items() if k != "_harness_retrieval_timing"}
    assert _compact(raw, tool_name="lean_retrieve") == _compact(without_timings, tool_name="lean_retrieve")
    assert raw["results"][0]["filename"] == "target/Mathlib/Example.lean"


def test_shared_runtime_cleanup_is_once_per_instance_and_clears_cache(monkeypatch):
    closed = []

    class Runtime:
        async def aclose(self):
            closed.append(self)

    one, two = Runtime(), Runtime()
    monkeypatch.setattr(backend, "_RETRIEVER_CACHE", {("a",): one, ("b",): one, ("c",): two})
    asyncio.run(backend.close_cached_retrievers())
    assert closed == [one, two]
    assert not backend._RETRIEVER_CACHE
    asyncio.run(backend.close_cached_retrievers())
    assert closed == [one, two]


def test_owner_loop_creation_failure_reaches_caller_instead_of_waiting_forever(monkeypatch):
    def fail():
        raise OSError("No event-loop resources")

    with monkeypatch.context() as patch:
        patch.setattr(asyncio, "new_event_loop", fail)
        with pytest.raises(OSError, match="No event-loop resources"):
            LocalSearchRuntime()
