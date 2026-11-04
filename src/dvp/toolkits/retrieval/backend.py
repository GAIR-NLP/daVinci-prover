"""Configured retrieval backend selection and shared instances."""

from __future__ import annotations

import os
import hashlib
import asyncio
from pathlib import Path
from typing import Any

from dvp.toolkits.retrieval.local import LeanRetriever
from dvp.toolkits.retrieval.explore import LeanExploreRetriever


def _env_list(name: str) -> list[str] | None:
    value = os.getenv(name, "").strip()
    if not value:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def _env_optional_int(name: str, default: int | None = None) -> int | None:
    value = os.getenv(name)
    if value in (None, ""):
        return default
    if value.strip().lower() in {"none", "null", "off", "disabled"}:
        return None
    return int(value)


def _env_bool(name: str, *, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"", "0", "false", "no", "off", "disabled"}


_RETRIEVER_CACHE: dict[tuple[Any, ...], Any] = {}


async def close_cached_retrievers() -> None:
    """Drain shared local runtimes after their evaluation workers have stopped."""
    instances = {id(value): value for value in _RETRIEVER_CACHE.values()}
    _RETRIEVER_CACHE.clear()
    pending = [value.aclose() for value in instances.values()
               if callable(getattr(value, "aclose", None))]
    results = await asyncio.gather(*pending, return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result


def get_cached_retriever() -> Any:
    backend = os.getenv("DAVINCI_RETRIEVE_BACKEND", "lean_explore").strip().lower()
    if backend in ("lean_explore", "lean-explore", "leanexplore"):
        lean_explore_backend = os.getenv("DAVINCI_LEANEXPLORE_BACKEND", "local").strip().lower()
        if lean_explore_backend == "remote":
            from dvp.toolkits.retrieval.remote import RemoteLeanExploreRetriever

            url = os.getenv("DAVINCI_LEANEXPLORE_URL", "")
            token = os.getenv("DAVINCI_LEANEXPLORE_TOKEN") or None
            timeout = float(os.getenv("DAVINCI_LEANEXPLORE_TIMEOUT", "300"))
            retries = int(os.getenv("DAVINCI_LEANEXPLORE_RETRIES", "2"))
            lean_version = os.getenv("DAVINCI_LEANEXPLORE_EXPECTED_LEAN_VERSION", "")
            commit = os.getenv("DAVINCI_LEANEXPLORE_EXPECTED_MATHLIB_COMMIT") or os.getenv("DVP_MATHLIB_COMMIT", "")
            snapshot = os.getenv("DAVINCI_LEANEXPLORE_EXPECTED_SNAPSHOT") or None
            key = ("lean_explore_remote", url, hashlib.sha256((token or "").encode()).hexdigest(),
                   timeout, retries, lean_version, commit, snapshot)
            if key not in _RETRIEVER_CACHE:
                _RETRIEVER_CACHE[key] = RemoteLeanExploreRetriever(
                    url=url, token=token, timeout=timeout, retries=retries,
                    expected_lean_version=lean_version, expected_mathlib_commit=commit,
                    expected_snapshot=snapshot,
                )
            return _RETRIEVER_CACHE[key]
        api_key = os.getenv("DAVINCI_LEANEXPLORE_API_KEY") or os.getenv("LEANEXPLORE_API_KEY")
        timeout = float(os.getenv("DAVINCI_LEANEXPLORE_TIMEOUT", "20"))
        retries = int(os.getenv("DAVINCI_LEANEXPLORE_RETRIES", "2"))
        packages = _env_list("DAVINCI_LEANEXPLORE_PACKAGES")
        rerank_top = _env_optional_int("DAVINCI_LEANEXPLORE_RERANK_TOP", 50)
        use_local_data = _env_bool("DAVINCI_LEANEXPLORE_USE_LOCAL_DATA", default=False)
        bm25_only = _env_bool("DAVINCI_LEANEXPLORE_BM25_ONLY", default=False)
        bm25_k = int(os.getenv("DAVINCI_LEANEXPLORE_BM25_K", "1000"))
        include_context = _env_bool("DAVINCI_LEANEXPLORE_CONTEXT", default=lean_explore_backend == "api")
        embedding_model = os.getenv("DAVINCI_LEANEXPLORE_EMBEDDING_MODEL") or None
        reranker_model = os.getenv("DAVINCI_LEANEXPLORE_RERANKER_MODEL") or None
        source_dir = os.getenv("DAVINCI_LEANEXPLORE_SOURCE_DIR") or None
        local_concurrency, local_max_pending = 4, 128
        if lean_explore_backend == "local":
            local_concurrency = int(os.getenv("DAVINCI_LEANEXPLORE_LOCAL_CONCURRENCY", "4"))
            local_max_pending = int(os.getenv("DAVINCI_LEANEXPLORE_LOCAL_MAX_PENDING", "128"))
        key = (
            "lean_explore",
            lean_explore_backend,
            bool(api_key),
            timeout,
            retries,
            tuple(packages or []),
            rerank_top,
            use_local_data,
            bm25_only,
            bm25_k,
            include_context,
            embedding_model,
            reranker_model,
            source_dir,
            local_concurrency,
            local_max_pending,
        )
        if key not in _RETRIEVER_CACHE:
            _RETRIEVER_CACHE[key] = LeanExploreRetriever(
                backend=lean_explore_backend,
                api_key=api_key,
                timeout=timeout,
                retries=retries,
                packages=packages,
                rerank_top=rerank_top,
                use_local_data=use_local_data,
                bm25_only=bm25_only,
                bm25_k=bm25_k,
                include_context=include_context,
                embedding_model=embedding_model,
                reranker_model=reranker_model,
                source_dir=source_dir,
                local_concurrency=local_concurrency,
                local_max_pending=local_max_pending,
            )
        return _RETRIEVER_CACHE[key]

    if backend not in ("local", "jsonl"):
        raise ValueError(
            f"Unknown DAVINCI_RETRIEVE_BACKEND={backend!r}; expected lean_explore, local, or jsonl"
        )

    raw_value = os.getenv("DAVINCI_RETRIEVE_INDEX", "")
    if not raw_value:
        raise FileNotFoundError("DAVINCI_RETRIEVE_INDEX is not configured")
    path = Path(raw_value).expanduser().resolve()
    key = ("local", path)
    if key not in _RETRIEVER_CACHE:
        _RETRIEVER_CACHE[key] = LeanRetriever(path)
    return _RETRIEVER_CACHE[key]
