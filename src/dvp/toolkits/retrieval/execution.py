"""Retrieval tools and process-wide admission for local index searches."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import inspect
import os
from pathlib import Path
import threading
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any
import weakref

from dvp.toolkits.retrieval.backend import get_cached_retriever
from dvp.toolkits.retrieval.local import LeanRetriever

if TYPE_CHECKING:
    from dvp.toolkits.registry import ToolRegistry


_LOCAL_RETRIEVERS: dict[Path, LeanRetriever] = {}
_LOCAL_RETRIEVERS_LOCK = threading.Lock()
_LOCAL_RETRIEVAL_RUNTIME_LOCK = threading.Lock()
_LOCAL_RETRIEVAL_EXECUTORS: dict[int, ThreadPoolExecutor] = {}
_LOCAL_RETRIEVAL_GATES: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop,
    dict[int, asyncio.Semaphore],
] = weakref.WeakKeyDictionary()
_LOCAL_RETRIEVAL_CONCURRENCY_ENV = "DVP_RETRIEVAL_CONCURRENCY"
_DEFAULT_LOCAL_RETRIEVAL_CONCURRENCY = 1

RetrievalSearch = Callable[..., Awaitable[list[dict[str, Any]]]]


class RetrievalInfrastructureError(RuntimeError):
    """A trusted retrieval service failure that must not become reward zero."""

    def __init__(self, reason: str, message: str) -> None:
        self.reason = str(reason)
        super().__init__(message)


def resolved_retrieval_backend(index_path: str | Path | None) -> str:
    """Return the backend DVP will actually use for this runtime."""

    if index_path not in (None, ""):
        return "local"
    return os.getenv("DAVINCI_RETRIEVE_BACKEND", "lean_explore").strip().lower()


def _local_retriever(index_path: Path) -> LeanRetriever:
    path = index_path.expanduser().resolve()
    # Construction reads and indexes the complete JSONL file (about 1.26 GiB
    # resident for the standard mathlib index). Multiple event loops and
    # executor threads share this cache, so the lookup and construction must
    # be one critical section rather than a racy check-then-store.
    with _LOCAL_RETRIEVERS_LOCK:
        retriever = _LOCAL_RETRIEVERS.get(path)
        if retriever is None:
            retriever = LeanRetriever(path)
            _LOCAL_RETRIEVERS[path] = retriever
        return retriever


def _local_retrieval_concurrency() -> int:
    raw = os.getenv(
        _LOCAL_RETRIEVAL_CONCURRENCY_ENV,
        str(_DEFAULT_LOCAL_RETRIEVAL_CONCURRENCY),
    )
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{_LOCAL_RETRIEVAL_CONCURRENCY_ENV} must be a positive integer, "
            f"got {raw!r}"
        ) from exc
    if value <= 0:
        raise ValueError(
            f"{_LOCAL_RETRIEVAL_CONCURRENCY_ENV} must be a positive integer, "
            f"got {raw!r}"
        )
    return value


def _local_retrieval_runtime(
    concurrency: int,
) -> tuple[ThreadPoolExecutor, asyncio.Semaphore]:
    """Return the process executor and current-loop admission gate.

    The executor is the process-wide hard cap, including callers hosted by
    different event loops. The semaphore keeps each event loop from filling
    that executor's unbounded work queue with hundreds of pending rollouts.
    """

    loop = asyncio.get_running_loop()
    with _LOCAL_RETRIEVAL_RUNTIME_LOCK:
        executor = _LOCAL_RETRIEVAL_EXECUTORS.get(concurrency)
        if executor is None:
            executor = ThreadPoolExecutor(
                max_workers=concurrency,
                thread_name_prefix="dvp-lean-retrieval",
            )
            _LOCAL_RETRIEVAL_EXECUTORS[concurrency] = executor
        loop_gates = _LOCAL_RETRIEVAL_GATES.get(loop)
        if loop_gates is None:
            loop_gates = {}
            _LOCAL_RETRIEVAL_GATES[loop] = loop_gates
        gate = loop_gates.get(concurrency)
        if gate is None:
            gate = asyncio.Semaphore(concurrency)
            loop_gates[concurrency] = gate
    return executor, gate


def _run_local_retrieval(
    index_path: Path,
    *,
    natural_language_query: str | None,
    lean_name: str | None,
    keywords: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    retriever = _local_retriever(index_path)
    return retriever.search(
        natural_language_query=natural_language_query,
        lean_name=lean_name,
        keywords=keywords,
        limit=limit,
    )


async def _search_local_retriever(
    index_path: Path,
    *,
    natural_language_query: str | None,
    lean_name: str | None,
    keywords: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Load/query the local index without blocking the rollout event loop."""

    concurrency = _local_retrieval_concurrency()
    executor, gate = _local_retrieval_runtime(concurrency)
    call = partial(
        _run_local_retrieval,
        index_path,
        natural_language_query=natural_language_query,
        lean_name=lean_name,
        keywords=keywords,
        limit=limit,
    )
    async with gate:
        return await asyncio.get_running_loop().run_in_executor(
            executor,
            call,
        )


async def lean_retrieve(registry: ToolRegistry, args: dict[str, Any]) -> dict[str, Any]:
    """Use a supplied JSONL index deterministically.

    An explicit ``retrieve_index`` selects the local JSONL runtime,
    independently of the configured retrieval backend.
    """

    workspace = args.get("workspace")
    if workspace and str(workspace).strip() not in ("target", "target/"):
        return {
            "success": False,
            "error": "lean_retrieve only searches the target workspace.",
            "results": [],
        }
    if not any(
        args.get(name)
        for name in ("natural_language_query", "lean_name", "keywords")
    ):
        return {
            "success": False,
            "error": (
                "Provide at least one of natural_language_query, "
                "lean_name, or keywords."
            ),
            "results": [],
        }

    if registry.retrieve_index is None:
        return await _lean_retrieve_configured_backend(registry, args)

    search = registry._retrieval_search or _search_local_retriever
    try:
        results = await search(
            registry.retrieve_index,
            natural_language_query=args.get("natural_language_query"),
            lean_name=args.get("lean_name"),
            keywords=args.get("keywords"),
            limit=int(args.get("limit", 5)),
        )
    except RetrievalInfrastructureError as exc:
        return {
            "success": False,
            "stop": False,
            "error": str(exc),
            "results": [],
            # These private markers are produced only by the trusted
            # runtime callback.  ProverEpisode converts them into an
            # aborted/loss-masked execution, which fully-async retries.
            "_harness_infrastructure_error": True,
            "_harness_retryable_service_error": True,
            "_harness_reason": exc.reason,
        }
    except Exception as exc:
        return {
            "success": False,
            "error": f"{type(exc).__name__}: {exc}",
            "results": [],
        }

    include_theorem_proof = bool(
        args.get("include_theorem_proof", False)
    )
    include_def_proof = bool(args.get("include_def_proof", False))
    enriched: list[dict[str, Any]] = []
    for value in results:
        result = dict(value)
        filename = str(result.get("filename") or "")
        if filename and not filename.startswith("target/"):
            result["filename"] = f"target/{filename}"
        is_theorem = result.get("kind") in ("theorem", "lemma")
        include_source = (
            (is_theorem and include_theorem_proof)
            or (not is_theorem and include_def_proof)
        )
        if include_source and registry.workspace.target and filename:
            try:
                source = (
                    registry.workspace.target
                    / filename.removeprefix("target/")
                )
                lines = source.read_text(
                    encoding="utf-8",
                    errors="replace",
                ).splitlines()
                start = max(
                    0,
                    int(result.get("line_start") or 1) - 1,
                )
                end = min(
                    len(lines),
                    int(
                        result.get("line_end")
                        or result.get("line_start")
                        or 1
                    ),
                )
                result["source_code"] = "\n".join(
                    lines[start:end]
                )[:12_000]
            except Exception as exc:
                result["source_error"] = (
                    f"{type(exc).__name__}: {exc}"
                )
        enriched.append(result)
    return {
        "success": True,
        "results": enriched,
        "count": len(enriched),
        "retrieval_backend": "local",
    }


async def _lean_retrieve_configured_backend(
    registry: ToolRegistry,
    args: dict[str, Any],
) -> dict[str, Any]:
    """Use the configured LeanExplore or JSONL retrieval backend."""

    try:
        retriever = get_cached_retriever()
    except Exception as exc:
        backend = resolved_retrieval_backend(None)
        return {
            "success": False,
            "error": (
                f"{type(exc).__name__}: {exc}. Retrieval backend is "
                f"{backend!r}. Install and configure LeanExplore local data "
                "(python -m pip install -e '.[lean-explore-local]'). For JSONL search, provide "
                "--retrieve-index or set DAVINCI_RETRIEVE_BACKEND=jsonl and "
                "DAVINCI_RETRIEVE_INDEX."
            ),
            "results": [],
        }
    maybe_results = retriever.search(
        natural_language_query=args.get("natural_language_query"),
        lean_name=args.get("lean_name"),
        keywords=args.get("keywords"),
        limit=int(args.get("limit", 5)),
    )
    results = (
        await maybe_results
        if inspect.isawaitable(maybe_results)
        else maybe_results
    )
    include_theorem_proof = bool(
        args.get("include_theorem_proof", False)
    )
    include_def_proof = bool(args.get("include_def_proof", False))
    for result in results:
        embedded_source = result.pop("_source_text", None)
        filename = str(result.get("filename") or "")
        if filename and not filename.startswith("target/"):
            result["filename"] = f"target/{filename}"
        is_theorem = result.get("kind") in ("theorem", "lemma")
        include_source = (
            (is_theorem and include_theorem_proof)
            or (not is_theorem and include_def_proof)
        )
        if include_source and embedded_source:
            source_text = str(embedded_source)
            result["source_code"] = source_text[:12_000]
            if len(source_text) > 12_000:
                result["source_code_truncated"] = True
            continue
        if result.get("source_origin") == "lean_explore" or "lean_explore" in (result.get("source") or []):
            if include_source:
                result["source_error"] = "LeanExplore did not return source text; local source may be a different revision."
            continue
        if include_source and registry.workspace.target and filename:
            try:
                source = (
                    registry.workspace.target
                    / filename.removeprefix("target/")
                )
                lines = source.read_text(
                    encoding="utf-8",
                    errors="replace",
                ).splitlines()
                start = max(
                    0,
                    int(result.get("line_start") or 1) - 1,
                )
                end = min(
                    len(lines),
                    int(
                        result.get("line_end")
                        or result.get("line_start")
                        or 1
                    ),
                )
                result["source_code"] = "\n".join(
                    lines[start:end]
                )[:12_000]
            except Exception as exc:
                result["source_error"] = f"{type(exc).__name__}: {exc}"
    return {
        "success": True,
        "results": list(results),
        "count": len(results),
        "retrieval_backend": resolved_retrieval_backend(None),
        **({"_harness_retrieval_timing": dict(results.timings)}
           if isinstance(getattr(results, "timings", None), dict) else {}),
    }
