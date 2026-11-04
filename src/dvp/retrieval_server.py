"""Serve one completed local LeanExplore snapshot over HTTP.

The SDK and its models live on a dedicated event-loop thread. This keeps
health/authentication/queue handling responsive while SDK code does synchronous
tokenization or source reads, and preserves the retriever's inference gate.
"""

from __future__ import annotations

import argparse
import asyncio
from concurrent.futures import Future
from contextlib import asynccontextmanager
from dataclasses import dataclass
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import secrets
import threading
import time
from typing import Any, Callable

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator


PROTOCOL = "dvp.leanexplore.v1"
LOGGER = logging.getLogger(__name__)
MODEL_NAMES = {"embedding": "Qwen3-Embedding-0.6B", "reranker": "Qwen3-Reranker-0.6B"}
INDEX_FILES = (
    "bm25_ids_map.json", "informalization_faiss.index", "informalization_faiss_ids_map.json",
)


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    lean_name: str | None = Field(default=None, max_length=8192)
    natural_language_query: str | None = Field(default=None, max_length=8192)
    keywords: str | None = Field(default=None, max_length=8192)
    limit: int = Field(default=10, ge=1, le=50)
    expected_mathlib_commit: str | None = Field(default=None, max_length=128)
    expected_lean_version: str | None = Field(default=None, max_length=128)
    expected_snapshot: str | None = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def nonempty_query(self) -> SearchRequest:
        if not any(value and value.strip() for value in (
            self.lean_name, self.natural_language_query, self.keywords,
        )):
            raise ValueError("Provide a nonempty lean_name, natural_language_query, or keywords")
        return self


@dataclass(frozen=True)
class ServerSettings:
    snapshot: Path
    models_dir: Path
    source_dir: Path
    threads: int = 16
    packages: tuple[str, ...] = ("Mathlib",)
    rerank_top: int = 50
    local_concurrency: int = 4


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_snapshot(settings: ServerSettings) -> dict[str, Any]:
    """Verify the transferred snapshot before loading any model or accepting work."""
    snapshot = settings.snapshot.expanduser().resolve()
    build = _read_object(snapshot / "build-manifest.json")
    report = _read_object(snapshot / "index-build-report.json")
    lean_version = build.get("lean_version")
    commit = build.get("mathlib_commit")
    if build.get("status") != "complete" or build.get("full_semantic_ready") is not True:
        raise ValueError("Snapshot is not marked complete and full_semantic_ready")
    if not isinstance(lean_version, str) or not re.fullmatch(r"\d+\.\d+\.\d+(?:-[A-Za-z0-9.]+)?", lean_version):
        raise ValueError("Snapshot has no valid Lean version")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Snapshot has no pinned Mathlib commit")
    counts = build.get("counts", {})
    count = counts.get("declarations")
    coverage = report.get("database_coverage", {})
    validation = report.get("validation", {})
    if (type(count) is not int or count <= 0
            or any(counts.get(key) != count for key in ("descriptions", "vectors"))
            or report.get("status") != "complete_semantic_index"
            or coverage.get("full_semantic_coverage") is not True
            or any(coverage.get(key) != count for key in ("declarations", "descriptions", "vectors"))
            or any(coverage.get(key) != 0 for key in ("missing_descriptions", "missing_vectors"))
            or validation.get("all_bm25_ids_match_database") is not True
            or validation.get("all_faiss_ids_match_embedded_rows") is not True
            or validation.get("faiss_vectors") != count
            or any(validation.get("bm25_documents", {}).get(key) != count
                   for key in ("bm25_name_raw", "bm25_name_spaced"))):
        raise ValueError("Snapshot manifest and index report do not describe complete matching coverage")

    database = snapshot / "lean_explore.db"
    expected_sha = build.get("database", {}).get("sha256")
    if (not expected_sha or report.get("database_sha256") != expected_sha
            or _sha256(database) != expected_sha):
        raise ValueError("Snapshot database checksum does not match its manifests")
    artifacts = report.get("artifacts", {})
    for name in INDEX_FILES:
        if name not in artifacts:
            raise ValueError(f"Index report is missing {name}")
    for name in ("bm25_name_raw", "bm25_name_spaced"):
        if not (snapshot / name).is_dir() or not any(key.startswith(name + "/") for key in artifacts):
            raise ValueError(f"Index report or snapshot is missing {name}")
    for name, expected in artifacts.items():
        path = (snapshot / name).resolve()
        if not path.is_relative_to(snapshot) or not path.is_file():
            raise ValueError(f"Index artifact is absent or outside the snapshot: {name}")
        if path.stat().st_size != expected.get("bytes") or _sha256(path) != expected.get("sha256"):
            raise ValueError(f"Index artifact checksum mismatch: {name}")
    for name in MODEL_NAMES.values():
        model = settings.models_dir.expanduser().resolve() / name
        for filename in ("config.json", "modules.json", "tokenizer.json", "tokenizer_config.json"):
            if not (model / filename).is_file():
                raise ValueError(f"Local model is incomplete: {model / filename}")
        if not (model / "model.safetensors").is_file():
            raise ValueError(f"Local model weights are missing: {model}")
    source = settings.source_dir.expanduser().resolve() / "leanprover-community" / "mathlib4" / commit
    source_manifest = _read_object(source / ".dvp-source.json")
    if (source_manifest.get("repository") != "https://github.com/leanprover-community/mathlib4"
            or source_manifest.get("revision") != commit or not (source / "Mathlib").is_dir()):
        raise ValueError("Mathlib source directory does not match the indexed commit")
    return {
        "protocol": PROTOCOL,
        "snapshot": snapshot.name,
        "lean_version": lean_version,
        "mathlib_commit": commit,
        "declarations": count,
        "search": {"packages": list(settings.packages), "rerank_top": settings.rerank_top, "bm25_only": False},
        "models": {key: "Qwen/" + name for key, name in MODEL_NAMES.items()},
    }


def _local_factory(settings: ServerSettings) -> tuple[Any, dict[str, Any]]:
    metadata = validate_snapshot(settings)
    snapshot = settings.snapshot.expanduser().resolve()
    for key, value in {
        "LEAN_EXPLORE_CACHE_DIR": str(snapshot.parent), "LEAN_EXPLORE_VERSION": snapshot.name,
        "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1",
        "HF_HUB_DISABLE_PROGRESS_BARS": "1", "DISABLE_TQDM": "1",
        "OMP_NUM_THREADS": str(settings.threads), "MKL_NUM_THREADS": str(settings.threads),
        "OPENBLAS_NUM_THREADS": str(settings.threads), "NUMEXPR_NUM_THREADS": str(settings.threads),
    }.items():
        os.environ[key] = value

    # Import after setting SDK paths: Config computes these once at import time.
    from lean_explore.config import Config
    if Config.ACTIVE_CACHE_PATH.resolve() != snapshot:
        raise ValueError("LeanExplore was imported with another snapshot; launch the server in a fresh process")
    import torch
    torch.set_num_threads(settings.threads)
    from dvp.toolkits.retrieval.explore import LeanExploreRetriever
    retriever = LeanExploreRetriever(
        backend="local", packages=list(settings.packages), rerank_top=settings.rerank_top,
        use_local_data=False, bm25_only=False, include_context=True,
        embedding_model=str(settings.models_dir.expanduser().resolve() / MODEL_NAMES["embedding"]),
        reranker_model=str(settings.models_dir.expanduser().resolve() / MODEL_NAMES["reranker"]),
        source_dir=settings.source_dir.expanduser().resolve(),
        local_concurrency=settings.local_concurrency,
        # The HTTP worker owns admission and its larger pending queue.
        local_max_pending=settings.local_concurrency,
    )
    metadata["execution"] = {
        "local_concurrency": settings.local_concurrency,
        "model_concurrency": 1,
    }
    return retriever, metadata


class _QueueFull(Exception):
    pass


class _SearchWorker:
    """One SDK loop, with a capacity slot held until real work has finished."""

    def __init__(self, factory: Callable[[], tuple[Any, dict[str, Any]]], *, max_pending: int, warmup: bool):
        self.factory = factory
        self.max_pending = max_pending
        self.warmup = warmup
        self.ready: Future = Future()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.retriever: Any = None
        self.metadata: dict[str, Any] = {}
        self._futures: set[Future] = set()
        self._lock = threading.Lock()
        self._accepting = False
        self._disposed = False
        self._thread = threading.Thread(target=self._run, name="leanexplore-worker", daemon=True)

    async def _initialize(self) -> None:
        self.retriever, self.metadata = self.factory()
        concurrency = getattr(self.retriever, "local_concurrency", 1)
        if type(concurrency) is not int or concurrency < 1:
            raise ValueError("local_concurrency must be a positive integer")
        self._execution_gate = asyncio.Semaphore(concurrency)
        if self.warmup:
            # Force lazy models and indexes to load once before readiness.
            hits = await self.retriever.search(lean_name="Nat.Prime.dvd_mul", limit=1)
            if not hits:
                raise ValueError("Snapshot warmup returned no search results")

    def _run(self) -> None:
        try:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(self._initialize())
            self._accepting = True
            self.loop.call_soon(self.ready.set_result, None)
            self.loop.run_forever()
        except BaseException as exc:
            if not self.ready.done():
                self.ready.set_exception(exc)
            else:
                LOGGER.exception("LeanExplore worker stopped unexpectedly")
        finally:
            if self.loop is not None:
                try:
                    self.loop.run_until_complete(self._dispose())
                finally:
                    self.loop.run_until_complete(self.loop.shutdown_asyncgens())
                    self.loop.run_until_complete(self.loop.shutdown_default_executor())
                    self.loop.close()

    def start(self) -> None:
        self._thread.start()
        try:
            self.ready.result()
        except BaseException:
            self._thread.join()
            raise

    async def _dispose(self) -> None:
        if self._disposed or self.retriever is None:
            return
        try:
            close = getattr(self.retriever, "aclose", None)
            if close is not None:
                await close()
            else:
                database = getattr(getattr(self.retriever, "engine", None), "engine", None)
                if database is not None:
                    await database.dispose()
        finally:
            self._disposed = True

    async def _execute(
        self, arguments: dict[str, Any], deadline: float,
    ) -> tuple[list[dict[str, Any]], dict[str, float]]:
        queued = time.monotonic()
        async with self._execution_gate:
            if time.monotonic() >= deadline:
                raise TimeoutError("Request expired while waiting for inference")
            started = time.monotonic()
            results = await self.retriever.search(**arguments)
            timings = dict(getattr(results, "timings", {}))
            timings.update(
                server_queue_seconds=started - queued,
                server_search_seconds=time.monotonic() - started,
            )
            return results, timings

    def submit(self, arguments: dict[str, Any], *, deadline: float) -> Future:
        with self._lock:
            if not self._accepting or len(self._futures) >= self.max_pending:
                raise _QueueFull
            assert self.loop is not None
            future = asyncio.run_coroutine_threadsafe(self._execute(arguments, deadline), self.loop)
            self._futures.add(future)
        # Register outside the lock: completed futures call their callback inline.
        def finished(done: Future) -> None:
            with self._lock:
                self._futures.discard(done)
            if not done.cancelled():
                exception = done.exception()
                if exception and not isinstance(exception, TimeoutError):
                    LOGGER.error("Local LeanExplore search failed", exc_info=(type(exception), exception, exception.__traceback__))
        future.add_done_callback(finished)
        return future

    def close(self) -> None:
        with self._lock:
            self._accepting = False
            pending = list(self._futures)
        # Never cancel a running SDK task: executor inference may outlive it.
        for future in pending:
            try:
                future.result()
            except Exception:
                pass
        if self.loop and self.loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(self._dispose(), self.loop).result()
            finally:
                self.loop.call_soon_threadsafe(self.loop.stop)
                self._thread.join()


def create_app(
    *, settings: ServerSettings | None = None, retriever: Any = None,
    metadata: dict[str, Any] | None = None, token: str | None = None,
    max_pending: int = 32, request_timeout: float = 300.0,
) -> FastAPI:
    """Build the ASGI app; injected retrievers permit tests without SDK/models."""
    if max_pending < 1 or not math.isfinite(request_timeout) or request_timeout <= 0:
        raise ValueError("max_pending and request_timeout must be positive")
    if retriever is None and settings is None:
        raise ValueError("Provide ServerSettings or an injected retriever and metadata")
    if retriever is not None and metadata is None:
        raise ValueError("An injected retriever requires metadata")
    if metadata is not None and metadata.get("protocol", PROTOCOL) != PROTOCOL:
        raise ValueError("Injected metadata has an incompatible protocol")
    token = os.getenv("DAVINCI_LEANEXPLORE_TOKEN", "") if token is None else token

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if retriever is not None:
            factory = lambda: (retriever, {**metadata, "protocol": PROTOCOL})
        else:
            factory = lambda: _local_factory(settings)
        worker = _SearchWorker(factory, max_pending=max_pending, warmup=retriever is None)
        starting = asyncio.create_task(asyncio.to_thread(worker.start))
        try:
            await asyncio.shield(starting)
            app.state.worker = worker
            yield
        finally:
            async def shutdown() -> None:
                # Startup runs on a thread. Cancelling the HTTP lifespan must
                # wait for it before disposing the SDK or stopping its loop.
                try:
                    await starting
                except Exception:
                    pass  # start() already preserves the initialization error.
                await asyncio.to_thread(worker.close)

            closing = asyncio.create_task(shutdown())
            closing.add_done_callback(lambda done: None if done.cancelled() else done.exception())
            await asyncio.shield(closing)

    app = FastAPI(title="DVP LeanExplore", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    async def authenticate(request: Request) -> None:
        if token:
            authorization = request.headers.get("authorization", "")
            scheme, _, supplied = authorization.partition(" ")
            if scheme.lower() != "bearer" or not secrets.compare_digest(supplied.encode(), token.encode()):
                raise HTTPException(status_code=401, detail="Invalid bearer token", headers={"WWW-Authenticate": "Bearer"})

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ready"}

    @app.get("/v1/info", dependencies=[Depends(authenticate)])
    async def info() -> dict[str, Any]:
        return app.state.worker.metadata

    @app.post("/v1/search", dependencies=[Depends(authenticate)])
    async def search(body: SearchRequest) -> dict[str, Any]:
        worker = app.state.worker
        for key in ("mathlib_commit", "lean_version", "snapshot"):
            expected = getattr(body, "expected_" + key)
            if expected is not None and expected != worker.metadata.get(key):
                raise HTTPException(status_code=409, detail={"error": "snapshot_mismatch", "field": key,
                                                            "expected": expected, "actual": worker.metadata.get(key)})
        started = time.monotonic()
        arguments = {key: getattr(body, key) for key in ("lean_name", "natural_language_query", "keywords", "limit")}
        try:
            future = worker.submit(arguments, deadline=started + request_timeout)
        except _QueueFull:
            raise HTTPException(status_code=503, detail="Retrieval queue is full; retry later", headers={"Retry-After": "1"}) from None
        wrapped = asyncio.wrap_future(future)
        # HTTP timeout/disconnection must not cancel the concurrent future, or
        # free its capacity slot before the SDK's executor threads finish.
        wrapped.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        try:
            results, timings = await asyncio.wait_for(asyncio.shield(wrapped), timeout=request_timeout)
        except (TimeoutError, asyncio.TimeoutError):
            raise HTTPException(status_code=504, detail="Retrieval request timed out") from None
        except Exception:
            raise HTTPException(status_code=500, detail="Local retrieval failed; see server log") from None
        return {"protocol": PROTOCOL, "metadata": worker.metadata, "results": results,
                "count": len(results), "timings": timings,
                "processing_time_ms": round((time.monotonic() - started) * 1000, 3)}

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--models-dir", type=Path, required=True)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--threads", type=int, default=16)
    parser.add_argument("--local-concurrency", type=int, default=4,
                        help="Concurrent search pipelines sharing one serialized model executor")
    parser.add_argument("--max-pending", type=int, default=32)
    parser.add_argument("--request-timeout", type=float, default=300.0)
    parser.add_argument("--packages", default="Mathlib", help="Comma-separated package filter")
    parser.add_argument("--rerank-top", type=int, default=50)
    arguments = parser.parse_args()
    packages = tuple(value.strip() for value in arguments.packages.split(",") if value.strip())
    if (not 1 <= arguments.port <= 65535 or arguments.threads < 1 or arguments.max_pending < 1
            or not math.isfinite(arguments.request_timeout) or arguments.request_timeout <= 0
            or arguments.rerank_top < 1 or not packages):
        parser.error("port must be 1..65535; threads, max-pending, request-timeout, rerank-top and packages must be positive/nonempty")
    if arguments.local_concurrency < 1:
        parser.error("local-concurrency must be positive")
    settings = ServerSettings(snapshot=arguments.snapshot, models_dir=arguments.models_dir,
                              source_dir=arguments.source_dir, threads=arguments.threads,
                              packages=packages, rerank_top=arguments.rerank_top,
                              local_concurrency=arguments.local_concurrency)
    app = create_app(settings=settings, max_pending=arguments.max_pending, request_timeout=arguments.request_timeout)
    import uvicorn
    # Multiple workers would load multiple models and have separate queue limits.
    uvicorn.run(app, host=arguments.host, port=arguments.port, workers=1)


if __name__ == "__main__":
    main()
