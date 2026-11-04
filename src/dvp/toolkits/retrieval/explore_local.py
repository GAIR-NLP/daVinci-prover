"""Bounded local search pipelines with one owner loop and serialized models.

LeanExplore's async search has synchronous index/tokenizer work and executor
work using shared models. Keep its database and index access on one background
loop, and protect the official model clients across their entire async calls.
No query results are cached and no retrieval/ranking algorithms are replaced.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable


class LocalRetrievalBusyError(RuntimeError):
    """The bounded local pipeline has no admission capacity."""


class SearchResults(list):
    """List-compatible results with internal, per-request timing evidence."""

    def __init__(self, values, timings: dict[str, float | int]):
        super().__init__(values)
        self.timings = timings


_TIMING: contextvars.ContextVar[dict[str, float] | None] = contextvars.ContextVar(
    "dvp_local_retrieval_timing", default=None,
)


@dataclass(eq=False)
class _Job:
    submitted: float
    task: asyncio.Task | None = None
    started: bool = False
    cancelled: bool = False


class LocalSearchRuntime:
    """One SDK owner loop, bounded pipelines, and one shared model gate.

    A cancelled caller can remove queued work. Once admitted, work retains its
    slot and model gate until the SDK's executor work has really completed.
    """

    def __init__(self, concurrency: int = 4, max_pending: int = 128):
        for label, value in (("local_concurrency", concurrency), ("local_max_pending", max_pending)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{label} must be a positive integer")
        if max_pending < concurrency:
            raise ValueError("local_max_pending must be at least local_concurrency")
        self.concurrency = concurrency
        self.max_pending = max_pending
        self._state_lock = threading.Lock()
        self._closed = False
        self._jobs: dict[_Job, concurrent.futures.Future] = {}
        self._close_future: concurrent.futures.Future | None = None
        self._ready: concurrent.futures.Future = concurrent.futures.Future()
        self.engine = None
        self._thread = threading.Thread(target=self._serve, name="dvp-leanexplore", daemon=True)
        self._thread.start()
        try:
            self._ready.result()
        except BaseException:
            self._thread.join()
            raise

    def _serve(self) -> None:
        try:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self._slots = asyncio.Semaphore(self.concurrency)
            self._model_gate = asyncio.Lock()
        except BaseException as exc:
            loop = getattr(self, "loop", None)
            if loop is not None:
                loop.close()
            self._ready.set_exception(exc)
            return
        self._ready.set_result(None)
        try:
            self.loop.run_forever()
        finally:
            self.loop.run_until_complete(self.loop.shutdown_asyncgens())
            self.loop.run_until_complete(self.loop.shutdown_default_executor())
            self.loop.close()

    def initialize(self, factory: Callable[[], Any]) -> Any:
        """Create the SDK engine on the same loop that will use/dispose it."""
        async def create():
            self.engine = factory()
            return self.engine

        try:
            return asyncio.run_coroutine_threadsafe(create(), self.loop).result()
        except BaseException:
            self._closed = True
            self.loop.call_soon_threadsafe(self.loop.stop)
            self._thread.join()
            raise

    async def run(self, operation: Callable[[], Any]) -> SearchResults:
        job = _Job(time.perf_counter())
        with self._state_lock:
            if self._closed:
                raise RuntimeError("Local LeanExplore retriever is closed")
            if len(self._jobs) >= self.max_pending:
                raise LocalRetrievalBusyError(
                    f"Local LeanExplore is busy ({self.max_pending} pending searches); retry later"
                )
            future = asyncio.run_coroutine_threadsafe(self._run(job, operation), self.loop)
            self._jobs[job] = future
        future.add_done_callback(lambda done: self._finished(job, done))
        wrapped = asyncio.wrap_future(future)
        # Observe a late exception when the requester has already been cancelled.
        wrapped.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        try:
            return await asyncio.shield(wrapped)
        except asyncio.CancelledError:
            if not future.done():
                try:
                    self.loop.call_soon_threadsafe(self._cancel_queued, job)
                except RuntimeError:
                    # A concurrent close can finish this job and stop its
                    # owner between the done check and callback submission.
                    if not future.done():
                        raise
            raise

    def _finished(self, job: _Job, future: concurrent.futures.Future) -> None:
        with self._state_lock:
            self._jobs.pop(job, None)
        if not future.cancelled():
            future.exception()

    @staticmethod
    def _cancel_queued(job: _Job) -> None:
        if not job.started:
            job.cancelled = True
            if job.task is not None:
                job.task.cancel()

    async def _run(self, job: _Job, operation: Callable[[], Any]) -> SearchResults:
        job.task = asyncio.current_task()
        if job.cancelled:
            raise asyncio.CancelledError
        async with self._slots:
            job.started = True
            started = time.perf_counter()
            timing = {"model_queue_seconds": 0.0, "model_seconds": 0.0, "context_seconds": 0.0}
            token = _TIMING.set(timing)
            try:
                values = await operation()
            finally:
                _TIMING.reset(token)
            finished = time.perf_counter()
            return SearchResults(values, {
                **timing,
                "queue_seconds": started - job.submitted,
                "search_seconds": finished - started - timing["context_seconds"],
                "total_seconds": finished - job.submitted,
                "local_concurrency": self.concurrency,
                "model_concurrency": 1,
            })

    async def model_call(self, factory: Callable[[], Any], method: str, *args, **kwargs):
        queued = time.perf_counter()
        async with self._model_gate:
            started = time.perf_counter()
            timing = _TIMING.get()
            if timing is not None:
                timing["model_queue_seconds"] += started - queued
            try:
                # Official properties lazily initialize models. Initialization
                # and every executor batch share the gate, including cancellation.
                client = factory()
                return await getattr(client, method)(*args, **kwargs)
            finally:
                if timing is not None:
                    timing["model_seconds"] += time.perf_counter() - started

    @staticmethod
    def record_context(seconds: float) -> None:
        timing = _TIMING.get()
        if timing is not None:
            timing["context_seconds"] += seconds

    async def aclose(self) -> None:
        """Stop admission, drain SDK work, dispose SQLite on its owner loop."""
        created = False
        with self._state_lock:
            if self._closed and not self._thread.is_alive():
                return
            if self._close_future is None:
                self._closed = True
                jobs = list(self._jobs.values())
                self._close_future = asyncio.run_coroutine_threadsafe(self._drain(jobs), self.loop)
                created = True
            future = self._close_future
        if created:
            # Closing the caller's loop/cancelling its task must not abandon
            # database/worker cleanup, or stop the owner before active work ends.
            future.add_done_callback(self._stop_after_close)
        wrapped = asyncio.wrap_future(future)
        wrapped.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        try:
            await asyncio.shield(wrapped)
        finally:
            if future.done():
                await asyncio.to_thread(self._thread.join)

    def _stop_after_close(self, future: concurrent.futures.Future) -> None:
        if not future.cancelled():
            future.exception()
        self.loop.call_soon_threadsafe(self.loop.stop)

    async def _drain(self, jobs) -> None:
        if jobs:
            await asyncio.gather(*(asyncio.wrap_future(job) for job in jobs), return_exceptions=True)
        database = getattr(self.engine, "engine", None)
        if database is not None:
            await database.dispose()


class _ModelClient:
    def __init__(self, runtime: LocalSearchRuntime, factory: Callable[[], Any]):
        self.runtime = runtime
        self.factory = factory

    async def embed(self, *args, **kwargs):
        return await self.runtime.model_call(self.factory, "embed", *args, **kwargs)

    async def rerank(self, *args, **kwargs):
        return await self.runtime.model_call(self.factory, "rerank", *args, **kwargs)


def guarded_engine_type(engine_type: type, runtime: LocalSearchRuntime) -> type:
    """Guard official lazy model properties; reject an incompatible SDK."""
    properties = {name: getattr(engine_type, name, None)
                  for name in ("embedding_client", "reranker_client")}
    if any(not isinstance(prop, property) or prop.fget is None for prop in properties.values()):
        raise RuntimeError(
            "Unsupported LeanExplore SearchEngine model API; expected lazy embedding_client "
            "and reranker_client properties. Install a supported lean-explore version."
        )

    class GuardedSearchEngine(engine_type):
        @property
        def embedding_client(self):
            return _ModelClient(runtime, lambda: properties["embedding_client"].fget(self))

        @property
        def reranker_client(self):
            return _ModelClient(runtime, lambda: properties["reranker_client"].fget(self))

    return GuardedSearchEngine
