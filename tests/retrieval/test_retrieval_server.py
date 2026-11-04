"""HTTP protocol, backpressure, and snapshot checks without loading models."""

import asyncio
import hashlib
import json
import threading

import httpx
import pytest

pytest.importorskip("fastapi", reason="Install dvp[lean-explore-server] to test the optional HTTP service")

from dvp import retrieval_server as server


METADATA = {
    "protocol": server.PROTOCOL, "snapshot": "20261020_060431", "lean_version": "4.35.0",
    "mathlib_commit": "a" * 40, "declarations": 42,
    "search": {"packages": ["Mathlib"], "rerank_top": 50, "bm25_only": False},
    "models": {"embedding": "Qwen/Qwen3-Embedding-0.6B", "reranker": "Qwen/Qwen3-Reranker-0.6B"},
}
HIT = {
    "id": 1, "fullname": "Nat.Prime.dvd_mul", "signature": "theorem Nat.Prime.dvd_mul ...",
    "semantic": "A prime divides a product iff it divides one factor.",
    "dependencies": ["Nat.Prime"], "_source_text": "theorem ... := by exact ...",
    "source_origin": "lean_explore", "source_revision": "a" * 40,
    "context": {"namespace": "Nat", "commands": [], "complete": False}, "context_status": "available",
}


class Retriever:
    def __init__(self):
        self.calls = []

    async def search(self, **arguments):
        self.calls.append(arguments)
        return [HIT.copy()]


def test_worker_loop_creation_failure_reaches_start_caller(monkeypatch):
    def fail():
        raise OSError("No event-loop resources")

    worker = server._SearchWorker(lambda: (Retriever(), METADATA), max_pending=1, warmup=False)
    with monkeypatch.context() as patch:
        patch.setattr(asyncio, "new_event_loop", fail)
        with pytest.raises(OSError, match="No event-loop resources"):
            worker.start()
    assert not worker._thread.is_alive()


def request(app, method, path, **kwargs):
    async def run():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                return await client.request(method, path, **kwargs)
    return asyncio.run(run())


def test_search_preserves_full_result_and_reports_version_metadata():
    retriever = Retriever()
    app = server.create_app(retriever=retriever, metadata=METADATA, token="")
    response = request(app, "POST", "/v1/search", json={
        "lean_name": "Nat.Prime.dvd_mul", "expected_mathlib_commit": "a" * 40,
        "expected_lean_version": "4.35.0", "expected_snapshot": "20261020_060431",
    })
    assert response.status_code == 200
    body = response.json()
    assert body["protocol"] == server.PROTOCOL
    assert body["metadata"] == METADATA
    assert body["results"] == [HIT]
    assert body["count"] == 1
    assert body["processing_time_ms"] >= 0
    assert body["timings"]["server_queue_seconds"] >= 0
    assert body["timings"]["server_search_seconds"] >= 0
    assert retriever.calls == [{"lean_name": "Nat.Prime.dvd_mul", "natural_language_query": None,
                               "keywords": None, "limit": 10}]


@pytest.mark.parametrize("path", ["/v1/info", "/v1/search"])
def test_authentication_is_required_for_info_and_search(path):
    async def run():
        app = server.create_app(retriever=Retriever(), metadata=METADATA, token="secret-token")
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                method = "GET" if path.endswith("info") else "POST"
                options = {} if method == "GET" else {"json": {"keywords": "prime"}}
                for authorization in (None, "Bearer incorrect", "Basic secret-token"):
                    headers = {"Authorization": authorization} if authorization else {}
                    response = await client.request(method, path, headers=headers, **options)
                    assert response.status_code == 401
                    assert response.headers["www-authenticate"] == "Bearer"
                    assert "secret-token" not in response.text
                response = await client.request(method, path, headers={"Authorization": "Bearer secret-token"}, **options)
                assert response.status_code == 200
                assert (await client.get("/healthz")).json() == {"status": "ready"}
    asyncio.run(run())


def test_authentication_reads_environment_token(monkeypatch):
    monkeypatch.setenv("DAVINCI_LEANEXPLORE_TOKEN", "from-environment")
    app = server.create_app(retriever=Retriever(), metadata=METADATA)
    assert request(app, "GET", "/v1/info").status_code == 401


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_timeout_must_be_positive_and_finite(timeout):
    with pytest.raises(ValueError, match="positive"):
        server.create_app(retriever=Retriever(), metadata=METADATA, token="", request_timeout=timeout)


@pytest.mark.parametrize("field,value", [
    ("mathlib_commit", "b" * 40), ("lean_version", "0.0.0"), ("snapshot", "old_snapshot"),
])
def test_version_mismatch_rejected_before_search(field, value):
    retriever = Retriever()
    app = server.create_app(retriever=retriever, metadata=METADATA, token="")
    response = request(app, "POST", "/v1/search", json={"keywords": "prime", "expected_" + field: value})
    assert response.status_code == 409
    assert response.json()["detail"]["field"] == field
    assert not retriever.calls


@pytest.mark.parametrize("body", [
    {}, {"keywords": "  "}, {"lean_name": "x", "limit": 0}, {"lean_name": "x", "limit": 51},
    {"lean_name": "x", "limit": True}, {"lean_name": "x", "limit": "10"},
    {"lean_name": 123}, {"keywords": "x" * 8193}, {"lean_name": "x", "packages": ["Lean"]},
])
def test_invalid_queries_rejected_without_search(body):
    retriever = Retriever()
    app = server.create_app(retriever=retriever, metadata=METADATA, token="")
    response = request(app, "POST", "/v1/search", json=body)
    assert response.status_code == 422
    assert not retriever.calls


@pytest.mark.parametrize("ending", ["completion", "timeout", "cancellation"])
def test_blocking_inference_keeps_health_responsive_and_slot_until_done(ending):
    started = threading.Event()
    release = threading.Event()

    class BlockingRetriever(Retriever):
        async def search(self, **arguments):
            self.calls.append(arguments)
            started.set()
            # Deliberately block the SDK loop to reproduce synchronous model work.
            if not release.wait(timeout=5):
                raise AssertionError("Test did not release blocking inference")
            return [HIT.copy()]

    async def run():
        retriever = BlockingRetriever()
        app = server.create_app(retriever=retriever, metadata=METADATA, token="", max_pending=1,
                                request_timeout=0.1 if ending == "timeout" else 2)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                first = asyncio.create_task(client.post("/v1/search", json={"keywords": "prime"}))
                try:
                    assert await asyncio.to_thread(started.wait, 2)
                    assert (await asyncio.wait_for(client.get("/healthz"), 0.2)).json() == {"status": "ready"}
                    if ending == "timeout":
                        assert (await first).status_code == 504
                    elif ending == "cancellation":
                        first.cancel()
                        with pytest.raises(asyncio.CancelledError):
                            await first
                    busy = await client.post("/v1/search", json={"keywords": "another"})
                    assert busy.status_code == 503
                    assert busy.headers["retry-after"] == "1"
                    assert len(retriever.calls) == 1
                finally:
                    release.set()
                if ending == "completion":
                    assert (await first).status_code == 200
                for _ in range(100):
                    if not app.state.worker._futures:
                        break
                    await asyncio.sleep(0.01)
                assert not app.state.worker._futures
                assert (await client.post("/v1/search", json={"keywords": "after completion"})).status_code == 200
                assert len(retriever.calls) == 2
    asyncio.run(run())


def test_failed_inference_releases_slot_and_hides_exception_details():
    class FailingRetriever(Retriever):
        async def search(self, **arguments):
            raise RuntimeError("/private/server/model/path")

    app = server.create_app(retriever=FailingRetriever(), metadata=METADATA, token="", max_pending=1)
    response = request(app, "POST", "/v1/search", json={"keywords": "prime"})
    assert response.status_code == 500
    assert "/private" not in response.text


def test_factory_is_called_once_and_warms_up_before_ready(monkeypatch, tmp_path):
    calls = []
    retriever = Retriever()

    def factory(settings):
        calls.append(settings)
        return retriever, METADATA

    monkeypatch.setattr(server, "_local_factory", factory)
    settings = server.ServerSettings(tmp_path, tmp_path, tmp_path)

    async def run():
        app = server.create_app(settings=settings, token="")
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                assert (await client.get("/healthz")).status_code == 200
                for _ in range(3):
                    assert (await client.post("/v1/search", json={"keywords": "prime"})).status_code == 200
    asyncio.run(run())
    assert calls == [settings]
    assert len(retriever.calls) == 4
    assert retriever.calls[0] == {"lean_name": "Nat.Prime.dvd_mul", "limit": 1}


def test_search_pipeline_concurrency_and_pending_capacity_are_independent():
    two_started = threading.Event()
    release = threading.Event()

    class ConcurrentRetriever(Retriever):
        local_concurrency = 2
        active = 0
        peak = 0
        closed = False

        async def search(self, **arguments):
            self.calls.append(arguments)
            self.active += 1
            self.peak = max(self.peak, self.active)
            if self.active == 2:
                two_started.set()
            try:
                assert await asyncio.to_thread(release.wait, 3)
                return [HIT.copy()]
            finally:
                self.active -= 1

        async def aclose(self):
            assert self.active == 0
            self.closed = True

    async def run():
        retriever = ConcurrentRetriever()
        app = server.create_app(retriever=retriever, metadata=METADATA, token="",
                                max_pending=3, request_timeout=3)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                requests = [asyncio.create_task(client.post("/v1/search", json={"keywords": str(i)}))
                            for i in range(3)]
                try:
                    assert await asyncio.to_thread(two_started.wait, 2)
                    assert len(retriever.calls) == 2
                    assert (await client.post("/v1/search", json={"keywords": "overflow"})).status_code == 503
                    assert (await client.get("/healthz")).status_code == 200
                finally:
                    release.set()
                responses = await asyncio.gather(*requests)
                assert all(response.status_code == 200 for response in responses)
                assert all(response.json()["results"] == [HIT] for response in responses)
        assert retriever.peak == 2
        assert len(retriever.calls) == 3
        assert retriever.closed
    asyncio.run(run())


def test_timed_out_queued_request_never_starts_inference():
    started = threading.Event()
    release = threading.Event()

    class SlowRetriever(Retriever):
        async def search(self, **arguments):
            self.calls.append(arguments)
            started.set()
            assert await asyncio.to_thread(release.wait, 3)
            return [HIT.copy()]

    async def run():
        retriever = SlowRetriever()
        app = server.create_app(retriever=retriever, metadata=METADATA, token="",
                                max_pending=2, request_timeout=0.05)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                first = asyncio.create_task(client.post("/v1/search", json={"keywords": "first"}))
                try:
                    assert await asyncio.to_thread(started.wait, 2)
                    second = asyncio.create_task(client.post("/v1/search", json={"keywords": "expired"}))
                    assert (await first).status_code == 504
                    assert (await second).status_code == 504
                finally:
                    release.set()
        assert len(retriever.calls) == 1
    asyncio.run(run())


def test_failed_warmup_closes_retriever_before_start_returns(monkeypatch, tmp_path):
    class FailingWarmup(Retriever):
        closed = False

        async def search(self, **arguments):
            raise RuntimeError("warmup failed")

        async def aclose(self):
            self.closed = True

    retriever = FailingWarmup()
    monkeypatch.setattr(server, "_local_factory", lambda _: (retriever, METADATA))
    settings = server.ServerSettings(tmp_path, tmp_path, tmp_path)
    with pytest.raises(RuntimeError, match="warmup failed"):
        request(server.create_app(settings=settings, token=""), "GET", "/healthz")
    assert retriever.closed


def test_cancelling_startup_drains_warmup_and_closes_worker(monkeypatch, tmp_path):
    started = threading.Event()
    release = threading.Event()
    workers = []
    original_worker = server._SearchWorker

    class Warmup(Retriever):
        closed = False

        async def search(self, **arguments):
            started.set()
            assert await asyncio.to_thread(release.wait, 3)
            return [HIT.copy()]

        async def aclose(self):
            self.closed = True

    def worker_factory(*args, **kwargs):
        worker = original_worker(*args, **kwargs)
        workers.append(worker)
        return worker

    retriever = Warmup()
    monkeypatch.setattr(server, "_local_factory", lambda _: (retriever, METADATA))
    monkeypatch.setattr(server, "_SearchWorker", worker_factory)
    settings = server.ServerSettings(tmp_path, tmp_path, tmp_path)

    async def run():
        app = server.create_app(settings=settings, token="")

        async def lifespan():
            async with app.router.lifespan_context(app):
                pytest.fail("Cancelled startup must not enter the ready lifespan")

        task = asyncio.create_task(lifespan())
        try:
            assert await asyncio.to_thread(started.wait, 2)
            task.cancel()
            await asyncio.sleep(0)
            assert not retriever.closed
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        assert retriever.closed
        assert not workers[0]._thread.is_alive()
    asyncio.run(run())


@pytest.fixture
def snapshot_settings(tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    database = snapshot / "lean_explore.db"
    database.write_bytes(b"fixture database bytes")
    database_sha = hashlib.sha256(database.read_bytes()).hexdigest()
    artifacts = {}
    names = [*server.INDEX_FILES, "bm25_name_raw/data.npy", "bm25_name_spaced/data.npy"]
    for name in names:
        path = snapshot / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(name.encode())
        artifacts[name] = {"bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    manifest = {"status": "complete", "full_semantic_ready": True, "lean_version": "4.35.0",
                "mathlib_commit": "a" * 40, "database": {"sha256": database_sha},
                "counts": {"declarations": 42, "descriptions": 42, "vectors": 42}}
    report = {"status": "complete_semantic_index", "database_sha256": database_sha,
              "database_coverage": {"declarations": 42, "descriptions": 42, "vectors": 42,
                                    "missing_descriptions": 0, "missing_vectors": 0, "full_semantic_coverage": True},
              "validation": {"all_bm25_ids_match_database": True, "all_faiss_ids_match_embedded_rows": True,
                             "faiss_vectors": 42, "bm25_documents": {"bm25_name_raw": 42, "bm25_name_spaced": 42}},
              "artifacts": artifacts}
    (snapshot / "build-manifest.json").write_text(json.dumps(manifest))
    (snapshot / "index-build-report.json").write_text(json.dumps(report))
    models = tmp_path / "models"
    for name in server.MODEL_NAMES.values():
        directory = models / name
        directory.mkdir(parents=True)
        for filename in ("config.json", "modules.json", "tokenizer.json", "tokenizer_config.json", "model.safetensors"):
            (directory / filename).write_text("fixture")
    sources = tmp_path / "sources"
    source = sources / "leanprover-community/mathlib4" / ("a" * 40)
    (source / "Mathlib").mkdir(parents=True)
    (source / ".dvp-source.json").write_text(json.dumps({"repository": "https://github.com/leanprover-community/mathlib4",
                                                       "revision": "a" * 40}))
    return server.ServerSettings(snapshot=snapshot, models_dir=models, source_dir=sources)


def test_complete_snapshot_metadata_contains_no_local_paths(snapshot_settings):
    metadata = server.validate_snapshot(snapshot_settings)
    assert metadata == {**METADATA, "snapshot": "snapshot"}
    assert str(snapshot_settings.snapshot) not in json.dumps(metadata)


@pytest.mark.parametrize("failure", ["partial", "coverage", "database", "index", "model", "source"])
def test_incomplete_or_changed_transfers_fail_before_model_loading(snapshot_settings, failure):
    settings = snapshot_settings
    if failure == "partial":
        path = settings.snapshot / "build-manifest.json"
        value = json.loads(path.read_text())
        value["full_semantic_ready"] = False
        path.write_text(json.dumps(value))
    elif failure == "coverage":
        path = settings.snapshot / "index-build-report.json"
        value = json.loads(path.read_text())
        value["validation"]["faiss_vectors"] -= 1
        path.write_text(json.dumps(value))
    elif failure == "database":
        (settings.snapshot / "lean_explore.db").write_bytes(b"changed")
    elif failure == "index":
        (settings.snapshot / "informalization_faiss.index").write_bytes(b"changed")
    elif failure == "model":
        (settings.models_dir / server.MODEL_NAMES["embedding"] / "model.safetensors").unlink()
    elif failure == "source":
        path = settings.source_dir / "leanprover-community/mathlib4" / ("a" * 40) / ".dvp-source.json"
        path.write_text(json.dumps({"repository": "https://github.com/leanprover-community/mathlib4", "revision": "b" * 40}))
    with pytest.raises(ValueError):
        server.validate_snapshot(settings)
