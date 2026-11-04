"""The tool requests a small default result set without overriding explicit limits."""

import asyncio
from types import SimpleNamespace

import pytest

from dvp.toolkits.retrieval import execution


@pytest.mark.parametrize("backend", ["local", "configured"])
@pytest.mark.parametrize("requested,expected", [(None, 5), (1, 1), (20, 20)])
def test_retrieval_default_and_explicit_limits(monkeypatch, tmp_path, backend, requested, expected):
    calls = []

    async def search(*args, **kwargs):
        calls.append(kwargs)
        return []

    registry = SimpleNamespace(
        retrieve_index=tmp_path / "index.jsonl" if backend == "local" else None,
        _retrieval_search=search,
    )
    monkeypatch.setattr(execution, "get_cached_retriever", lambda: SimpleNamespace(search=search))
    monkeypatch.setattr(execution, "resolved_retrieval_backend", lambda _: "lean_explore")
    args = {"lean_name": "Nat.add_zero"}
    if requested is not None:
        args["limit"] = requested
    result = asyncio.run(execution.lean_retrieve(registry, args))
    assert result["success"] is True
    assert calls == [{"natural_language_query": None, "lean_name": "Nat.add_zero",
                      "keywords": None, "limit": expected}]
