"""Local LeanExplore uses local models and matching local source, without HTTP."""

import asyncio
import json
import sys
import threading
import time
from types import ModuleType, SimpleNamespace

import httpx
import pytest

from dvp.toolkits.retrieval import backend, explore
from dvp.toolkits.retrieval.explore import LeanExploreRetriever
from dvp.toolkits.retrieval.explore_local import LocalRetrievalBusyError, LocalSearchRuntime


REVISION = "a" * 40
REPOSITORY = "https://github.com/leanprover-community/mathlib4"
SOURCE = "theorem sample (a : G) : a * a⁻¹ = 1 := by simp"
FILE = "namespace Example\nvariable {G : Type*} [Group G]\n\n" + SOURCE + "\nend Example\n"


def hit(**overrides):
    values = dict(
        id=42, name="Example.sample", module="Mathlib.Sample", source_text=SOURCE,
        source_link=f"{REPOSITORY}/blob/{REVISION}/Mathlib/Sample.lean#L4-L4",
        informalization="An element times its inverse is one.", docstring="Group inverse law.",
        dependencies='["mul_inv_cancel"]',
    )
    return SimpleNamespace(**(values | overrides))


@pytest.fixture
def sdk(monkeypatch):
    state = SimpleNamespace(options=[], calls=[], hits=[hit()], active=0, max_active=0, delay=0,
                            runtimes=[], owner_loops=[], dispose_loops=[])

    class Runtime(LocalSearchRuntime):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            state.runtimes.append(self)

    monkeypatch.setattr(explore, "LocalSearchRuntime", Runtime)

    class SearchEngine:
        def __init__(self, **options):
            state.options.append(options)
            state.owner_loops.append(asyncio.get_running_loop())

            async def dispose():
                state.dispose_loops.append(asyncio.get_running_loop())

            self.engine = SimpleNamespace(dispose=dispose)

        @property
        def embedding_client(self):
            return state.embedding_client

        @property
        def reranker_client(self):
            return state.reranker_client

    class Service:
        def __init__(self, engine):
            self.engine = engine

        async def search(self, **kwargs):
            state.calls.append(kwargs)
            state.active += 1
            state.max_active = max(state.max_active, state.active)
            try:
                await asyncio.sleep(state.delay)
                return SimpleNamespace(results=state.hits)
            finally:
                state.active -= 1

    module = ModuleType("lean_explore.search")
    module.SearchEngine = SearchEngine
    module.Service = Service
    monkeypatch.setitem(sys.modules, "lean_explore.search", module)

    def unexpected_http(**_kwargs):
        pytest.fail("Local retrieval must never create an HTTP client for source context")

    monkeypatch.setattr(httpx, "AsyncClient", unexpected_http)
    yield state
    for runtime in state.runtimes:
        asyncio.run(runtime.aclose())


@pytest.fixture
def sources(tmp_path):
    directory = tmp_path / "sources" / "leanprover-community" / "mathlib4" / REVISION
    directory.mkdir(parents=True)
    (directory / "Mathlib").mkdir()
    (directory / "Mathlib/Sample.lean").write_text(FILE, encoding="utf-8")
    (directory / ".dvp-source.json").write_text(json.dumps({
        "repository": REPOSITORY, "revision": REVISION,
    }), encoding="utf-8")
    return tmp_path / "sources", directory


def test_local_models_pass_existing_resolved_directories_to_sdk(sdk, tmp_path):
    embedding = tmp_path / "embedding"
    reranker = tmp_path / "reranker"
    embedding.mkdir()
    reranker.mkdir()
    LeanExploreRetriever(embedding_model=str(embedding), reranker_model=str(reranker))
    assert sdk.options == [{
        "use_local_data": False,
        "embedding_model_name": str(embedding), "reranker_model_name": str(reranker),
    }]


def test_default_constructor_and_backend_selection_use_local_sdk(sdk, monkeypatch):
    assert LeanExploreRetriever().client.engine is not None
    monkeypatch.setattr(backend, "_RETRIEVER_CACHE", {})
    monkeypatch.delenv("DAVINCI_RETRIEVE_BACKEND", raising=False)
    for name in ("EMBEDDING_MODEL", "RERANKER_MODEL", "SOURCE_DIR"):
        monkeypatch.delenv("DAVINCI_LEANEXPLORE_" + name, raising=False)
    assert backend.get_cached_retriever().client.engine is not None
    assert len(sdk.options) == 2


@pytest.mark.parametrize("option", ["embedding_model", "reranker_model"])
def test_missing_configured_model_fails_before_sdk_can_download(sdk, tmp_path, option):
    with pytest.raises(FileNotFoundError, match="local model directory"):
        LeanExploreRetriever(**{option: str(tmp_path / "missing")})
    assert not sdk.options


def test_local_context_uses_matching_versioned_source_and_keeps_retrieval_fields(sdk, sources):
    source_dir, _ = sources
    client = LeanExploreRetriever(include_context=True, source_dir=source_dir)
    result = asyncio.run(client.search(lean_name="Example.sample"))[0]
    assert result["context_status"] == "available"
    assert "Group G" in json.dumps(result["context"])
    assert result["source_revision"] == REVISION
    assert result["signature"] == SOURCE.split(" := ")[0]
    assert result["dependencies"] == ["mul_inv_cancel"]
    assert result["semantic"] == sdk.hits[0].informalization


@pytest.mark.parametrize("failure, message", [
    ("not_configured", "SOURCE_DIR is not configured"),
    ("missing_manifest", ".dvp-source.json"),
    ("invalid_manifest", "Local source unavailable"),
    ("wrong_revision", "manifest repository/revision"),
    ("wrong_repository", "manifest repository/revision"),
    ("missing_file", "Sample.lean"),
    ("too_large", "2 MiB"),
    ("wrong_span", "does not match"),
    ("wrong_namespace", "namespace conflicts"),
    ("outside_span", "outside the versioned file"),
])
def test_local_context_failures_keep_hits_without_remote_fallback(sdk, sources, failure, message):
    source_dir, directory = sources
    manifest = directory / ".dvp-source.json"
    path = directory / "Mathlib/Sample.lean"
    if failure == "not_configured":
        source_dir = None
    elif failure == "missing_manifest":
        manifest.unlink()
    elif failure == "invalid_manifest":
        manifest.write_text("[]")
    elif failure in {"wrong_revision", "wrong_repository"}:
        manifest.write_text(json.dumps({
            "repository": "https://github.com/another/repository" if failure == "wrong_repository" else REPOSITORY,
            "revision": "b" * 40 if failure == "wrong_revision" else REVISION,
        }))
    elif failure == "missing_file":
        path.unlink()
    elif failure == "too_large":
        path.write_text("x" * (explore._MAX_SOURCE_BYTES + 1))
    elif failure == "wrong_span":
        path.write_text(FILE.replace(SOURCE, "theorem other : True := by trivial"))
    elif failure == "wrong_namespace":
        path.write_text(FILE.replace("namespace Example", "namespace Other"))
    elif failure == "outside_span":
        path.write_text("short\n")

    client = LeanExploreRetriever(include_context=True, source_dir=source_dir)
    result = asyncio.run(client.search(lean_name="Example.sample"))[0]
    assert result["fullname"] == "Example.sample"
    assert result["context_status"] == "unavailable"
    assert message in result["context_error"]
    assert "context" not in result


@pytest.mark.parametrize("escape", ["source", "manifest", "version_dir"])
def test_local_context_rejects_paths_symlinked_outside_versioned_root(sdk, sources, tmp_path, escape):
    source_dir, directory = sources
    if escape == "version_dir":
        outside = tmp_path / "outside"
        directory.rename(outside)
        directory.symlink_to(outside, target_is_directory=True)
    else:
        path = directory / ("Mathlib/Sample.lean" if escape == "source" else ".dvp-source.json")
        outside = tmp_path / "outside"
        path.rename(outside)
        path.symlink_to(outside)
    client = LeanExploreRetriever(include_context=True, source_dir=source_dir)
    result = asyncio.run(client.search(lean_name="Example.sample"))[0]
    assert result["context_status"] == "unavailable"
    assert "outside" in result["context_error"]


def test_local_generated_declarations_do_not_use_generator_context(sdk, sources):
    source_dir, _ = sources
    sdk.hits = [hit(name="Example.other")]
    client = LeanExploreRetriever(include_context=True, source_dir=source_dir)
    result = asyncio.run(client.search(lean_name="Example.other"))[0]
    assert result["signature_status"] == "name_mismatch"
    assert result["context_status"] == "unavailable"
    assert "different declaration" in result["context_error"]


def test_local_search_bounds_pipelines_without_deduplicating_calls_across_event_loops(sdk):
    sdk.delay = 0.01
    client = LeanExploreRetriever(include_context=False)

    async def concurrent_searches():
        return await asyncio.gather(*(client.search(lean_name="Example.sample") for _ in range(4)))

    for _ in range(2):
        assert len(asyncio.run(concurrent_searches())) == 4
    assert sdk.max_active == 4
    assert len(sdk.calls) == 8


def test_cancelling_local_search_keeps_gate_until_background_inference_finishes(sdk):
    sdk.delay = 0.05
    client = LeanExploreRetriever(include_context=False, local_concurrency=1)

    async def cancel_then_search():
        first = asyncio.create_task(client.search(lean_name="Example.sample"))
        await asyncio.sleep(0.01)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        sdk.delay = 0
        next_query = asyncio.create_task(client.search(lean_name="Example.sample"))
        await asyncio.sleep(0.01)
        assert len(sdk.calls) == 1
        return await asyncio.wait_for(next_query, timeout=0.2)

    assert asyncio.run(cancel_then_search())[0]["fullname"] == "Example.sample"
    assert sdk.max_active == 1
    assert len(sdk.calls) == 2


def test_cancelling_queued_query_does_not_release_another_queries_gate(sdk):
    client = LeanExploreRetriever(include_context=False, local_concurrency=1)
    sdk.delay = 0.05

    async def run():
        first = asyncio.create_task(client.search(lean_name="Example.sample"))
        await asyncio.sleep(0.01)
        second = asyncio.create_task(client.search(lean_name="Example.sample"))
        await asyncio.sleep(0)
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        await asyncio.sleep(0.01)
        assert len(client._local_runtime._jobs) == 1
        assert len(sdk.calls) == 1
        await first
        assert not client._local_runtime._jobs

    asyncio.run(run())
    assert len(sdk.calls) == 1


def test_cancelled_caller_retains_worker_until_failure_then_releases_gate(sdk):
    client = LeanExploreRetriever(include_context=False, local_concurrency=1)
    original_search = client.client.search
    failures = []

    async def fail(**kwargs):
        await asyncio.sleep(0.05)
        raise ValueError("Model inference failed")

    client.client.search = fail

    async def run():
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda _loop, context: failures.append(context))
        first = asyncio.create_task(client.search(lean_name="Example.sample"))
        await asyncio.sleep(0.01)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert len(client._local_runtime._jobs) == 1
        client.client.search = original_search
        result = await asyncio.wait_for(client.search(lean_name="Example.sample"), timeout=0.2)
        assert not client._local_runtime._jobs
        assert result[0]["fullname"] == "Example.sample"

    asyncio.run(run())
    assert not failures


def test_local_bm25_uses_the_same_bounded_pipeline(sdk):
    client = LeanExploreRetriever(include_context=False, bm25_only=True,
                                  local_concurrency=2)

    async def bm25(**kwargs):
        return (await client.client.search(**kwargs)).results

    client._search_bm25_only = bm25
    sdk.delay = 0.01

    async def concurrent_searches():
        return await asyncio.gather(*(client.search(keywords="sample") for _ in range(4)))

    assert len(asyncio.run(concurrent_searches())) == 4
    assert sdk.max_active == 2
    assert len(sdk.calls) == 4


def test_configured_local_paths_are_forwarded_and_separate_shared_instances(monkeypatch, tmp_path):
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(backend, "LeanExploreRetriever", create)
    monkeypatch.setattr(backend, "_RETRIEVER_CACHE", {})
    monkeypatch.setenv("DAVINCI_RETRIEVE_BACKEND", "lean_explore")
    for name, folder in [
        ("EMBEDDING_MODEL", "embedding"), ("RERANKER_MODEL", "reranker"), ("SOURCE_DIR", "sources"),
    ]:
        monkeypatch.setenv("DAVINCI_LEANEXPLORE_" + name, str(tmp_path / folder))
    first = backend.get_cached_retriever()
    assert first is backend.get_cached_retriever()
    assert calls[0]["embedding_model"] == str(tmp_path / "embedding")
    assert calls[0]["reranker_model"] == str(tmp_path / "reranker")
    assert calls[0]["source_dir"] == str(tmp_path / "sources")
    monkeypatch.setenv("DAVINCI_LEANEXPLORE_SOURCE_DIR", str(tmp_path / "other_sources"))
    assert backend.get_cached_retriever() is not first
    assert len(calls) == 2


def test_pipeline_overlaps_queries_but_serializes_shared_models(sdk):
    client = LeanExploreRetriever(include_context=False, local_concurrency=3)
    state = SimpleNamespace(active=0, peak=0, calls=[], pipeline_active=0, pipeline_peak=0)

    def inference(kind, query):
        state.active += 1
        state.peak = max(state.peak, state.active)
        state.calls.append((kind, query))
        try:
            time.sleep(0.01)
            return SimpleNamespace()
        finally:
            state.active -= 1

    async def embed(texts, **_kwargs):
        return await asyncio.to_thread(inference, "embed", texts[0])

    async def rerank(query, documents):
        return await asyncio.to_thread(inference, "rerank", query)

    sdk.embedding_client = SimpleNamespace(embed=embed)
    sdk.reranker_client = SimpleNamespace(rerank=rerank)

    async def pipeline(query, **_kwargs):
        state.pipeline_active += 1
        state.pipeline_peak = max(state.pipeline_peak, state.pipeline_active)
        try:
            await client.engine.embedding_client.embed([query], is_query=True)
            await asyncio.sleep(0.01)  # Independent SQL query/CPU stages.
            await client.engine.reranker_client.rerank(query, ["same documents"])
            return SimpleNamespace(results=[hit()])
        finally:
            state.pipeline_active -= 1

    client.client.search = pipeline

    async def run():
        return await asyncio.gather(*(client.search(lean_name=f"query{i}") for i in range(6)))

    results = asyncio.run(run())
    assert all(result == results[0] for result in results)
    assert state.pipeline_peak == 3
    assert state.peak == 1
    assert len(state.calls) == 12
    assert max(result.timings["model_queue_seconds"] for result in results) > 0
    assert max(result.timings["queue_seconds"] for result in results) > 0.01
    assert all(result.timings["model_concurrency"] == 1 for result in results)


def test_local_source_scan_does_not_block_owner_loop_or_hold_model_gate(sdk, sources, monkeypatch):
    source_dir, _ = sources
    client = LeanExploreRetriever(include_context=True, source_dir=source_dir, local_concurrency=2)
    entered = threading.Event()
    release = threading.Event()
    original = client._read_local_source
    readers = []

    def read(result):
        readers.append(threading.current_thread().name)
        if len(readers) == 1:
            entered.set()
            assert release.wait(1)
        return original(result)

    monkeypatch.setattr(client, "_read_local_source", read)

    async def run():
        first = asyncio.create_task(client.search(lean_name="first"))
        assert await asyncio.to_thread(entered.wait, 1)
        try:
            second = await asyncio.wait_for(client.search(lean_name="second"), 0.5)
            assert second[0]["context_status"] == "available"
        finally:
            release.set()
        return await first

    result = asyncio.run(run())
    assert result.timings["context_seconds"] > 0
    assert all(name != "dvp-leanexplore" for name in readers)


def test_bounded_admission_rejects_excess_and_cancelled_waiter_never_runs(sdk):
    client = LeanExploreRetriever(include_context=False, local_concurrency=1, local_max_pending=2)
    entered = threading.Event()
    release = threading.Event()

    async def search(query, **_kwargs):
        sdk.calls.append(query)
        if query == "first":
            entered.set()
            await asyncio.to_thread(release.wait, 1)
        return SimpleNamespace(results=[hit()])

    client.client.search = search

    async def run():
        first = asyncio.create_task(client.search(lean_name="first"))
        assert await asyncio.to_thread(entered.wait, 1)
        second = asyncio.create_task(client.search(lean_name="queued"))
        await asyncio.sleep(0)
        with pytest.raises(LocalRetrievalBusyError, match="2 pending"):
            await client.search(lean_name="excess")
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        await asyncio.sleep(0.02)
        release.set()
        await first
        await client.search(lean_name="later")

    asyncio.run(run())
    assert sdk.calls == ["first", "later"]


def test_cancellation_keeps_real_executor_inference_serial_and_close_drains(sdk):
    client = LeanExploreRetriever(include_context=False, local_concurrency=2)
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def blocking_encode(texts):
        calls.append(texts[0])
        if texts[0] == "first":
            entered.set()
            assert release.wait(1)
        return SimpleNamespace()

    async def embed(texts, **kwargs):
        return await asyncio.to_thread(blocking_encode, texts)

    sdk.embedding_client = SimpleNamespace(embed=embed)

    async def search(query, **kwargs):
        await client.engine.embedding_client.embed([query])
        return SimpleNamespace(results=[hit()])

    client.client.search = search

    async def run():
        first = asyncio.create_task(client.search(lean_name="first"))
        assert await asyncio.to_thread(entered.wait, 1)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(client.search(lean_name="second"))
        await asyncio.sleep(0.02)
        assert calls == ["first"]
        closing = asyncio.create_task(client.aclose())
        await asyncio.sleep(0.02)
        assert not closing.done()
        assert not sdk.dispose_loops
        release.set()
        await second
        await closing

    asyncio.run(run())
    assert calls == ["first", "second"]
    assert sdk.dispose_loops == sdk.owner_loops
    assert not client._local_runtime._thread.is_alive()


def test_one_owner_loop_for_concurrent_callers_from_different_event_loops(sdk):
    from concurrent.futures import ThreadPoolExecutor

    client = LeanExploreRetriever(include_context=False, local_concurrency=2)
    owner_loops = []

    async def search(**kwargs):
        owner_loops.append(asyncio.get_running_loop())
        await asyncio.sleep(0.02)
        return SimpleNamespace(results=[hit()])

    client.client.search = search
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(asyncio.run, client.search(lean_name=f"query{i}")) for i in range(2)]
        assert all(f.result()[0]["fullname"] == "Example.sample" for f in futures)
    assert owner_loops == sdk.owner_loops * 2
    asyncio.run(client.aclose())
    assert sdk.dispose_loops == sdk.owner_loops


def test_blocking_sdk_stage_does_not_block_rollout_loop(sdk):
    client = LeanExploreRetriever(include_context=False)
    entered = threading.Event()
    release = threading.Event()

    async def search(**kwargs):
        entered.set()
        assert release.wait(1)  # Like the SDK's synchronous small rerank batch.
        return SimpleNamespace(results=[hit()])

    client.client.search = search

    async def run():
        request = asyncio.create_task(client.search(lean_name="query"))
        assert await asyncio.to_thread(entered.wait, 1)
        await asyncio.sleep(0.01)
        release.set()
        return await request

    assert asyncio.run(run())[0]["fullname"] == "Example.sample"


@pytest.mark.parametrize("settings", [
    {"local_concurrency": 0}, {"local_concurrency": True}, {"local_concurrency": 1.5},
    {"local_max_pending": 0}, {"local_concurrency": 4, "local_max_pending": 2},
])
def test_invalid_local_bounds_fail_before_runtime_start(sdk, settings):
    with pytest.raises(ValueError, match="local_"):
        LeanExploreRetriever(**settings)
    assert not sdk.options
    assert not sdk.runtimes


def test_incompatible_sdk_model_api_fails_closed_and_stops_owner_thread(sdk, monkeypatch):
    module = sys.modules["lean_explore.search"]
    monkeypatch.setattr(module, "SearchEngine", type("UnsupportedEngine", (), {}))
    with pytest.raises(RuntimeError, match="Unsupported LeanExplore"):
        LeanExploreRetriever()
    assert len(sdk.runtimes) == 1
    assert not sdk.runtimes[0]._thread.is_alive()


def test_failed_sdk_initialization_stops_owner_and_close_is_idempotent(sdk, monkeypatch):
    module = sys.modules["lean_explore.search"]

    class BrokenEngine(module.SearchEngine):
        def __init__(self, **kwargs):
            raise FileNotFoundError("missing index")

    monkeypatch.setattr(module, "SearchEngine", BrokenEngine)
    with pytest.raises(FileNotFoundError, match="missing index"):
        LeanExploreRetriever()
    assert not sdk.runtimes[0]._thread.is_alive()


def test_concurrent_close_drains_once_rejects_new_work_and_is_idempotent(sdk):
    client = LeanExploreRetriever(include_context=False)
    sdk.delay = 0.03

    async def run():
        request = asyncio.create_task(client.search(lean_name="first"))
        await asyncio.sleep(0.01)
        closers = [asyncio.create_task(client.aclose()) for _ in range(3)]
        await asyncio.sleep(0)
        with pytest.raises(RuntimeError, match="closed"):
            await client.search(lean_name="too late")
        await request
        await asyncio.gather(*closers)
        await client.aclose()

    asyncio.run(run())
    assert len(sdk.calls) == 1
    assert sdk.dispose_loops == sdk.owner_loops
    assert not client._local_runtime._thread.is_alive()


def test_cancelling_close_still_drains_running_work_and_stops_owner(sdk):
    client = LeanExploreRetriever(include_context=False)
    entered = threading.Event()
    release = threading.Event()

    async def search(**kwargs):
        entered.set()
        assert await asyncio.to_thread(release.wait, 1)
        return SimpleNamespace(results=[hit()])

    client.client.search = search

    async def run():
        request = asyncio.create_task(client.search(lean_name="first"))
        assert await asyncio.to_thread(entered.wait, 1)
        closing = asyncio.create_task(client.aclose())
        await asyncio.sleep(0.01)
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert not sdk.dispose_loops
        assert client._local_runtime._thread.is_alive()
        release.set()
        await request
        await asyncio.to_thread(client._local_runtime._thread.join, 1)

    asyncio.run(run())
    assert sdk.dispose_loops == sdk.owner_loops
    assert not client._local_runtime._thread.is_alive()


def test_failed_database_dispose_does_not_leak_owner_thread(sdk):
    client = LeanExploreRetriever(include_context=False)

    async def dispose():
        raise OSError("database cleanup failed")

    client.engine.engine.dispose = dispose
    with pytest.raises(OSError, match="database cleanup failed"):
        asyncio.run(client.aclose())
    assert not client._local_runtime._thread.is_alive()


def test_failed_lazy_model_initialization_releases_gate_for_next_query(sdk, monkeypatch):
    module = sys.modules["lean_explore.search"]
    initializations = []

    async def embed(*args, **kwargs):
        return SimpleNamespace()

    class Engine(module.SearchEngine):
        @property
        def embedding_client(self):
            initializations.append(asyncio.get_running_loop())
            if len(initializations) == 1:
                raise RuntimeError("model load failed")
            return SimpleNamespace(embed=embed)

    monkeypatch.setattr(module, "SearchEngine", Engine)
    client = LeanExploreRetriever(include_context=False)

    async def search(query, **kwargs):
        await client.engine.embedding_client.embed([query])
        return SimpleNamespace(results=[hit()])

    client.client.search = search

    async def run():
        with pytest.raises(RuntimeError, match="model load failed"):
            await client.search(lean_name="first")
        return await client.search(lean_name="next")

    assert asyncio.run(run())[0]["fullname"] == "Example.sample"
    assert initializations == sdk.owner_loops * 2
