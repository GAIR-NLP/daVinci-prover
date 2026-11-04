"""The local benchmark detects changed content, not just missing search hits."""

import asyncio
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def benchmark_module():
    path = Path(__file__).resolve().parents[2] / "scripts/benchmark_leanexplore_local.py"
    spec = importlib.util.spec_from_file_location("benchmark_leanexplore_local", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Results(list):
    timings = {"queue_seconds": 0.01, "search_seconds": 0.02}


class Retriever:
    local_concurrency = 4
    local_max_pending = 128

    def __init__(self, changed_field=None, fail=False):
        self.changed_field = changed_field
        self.fail = fail
        self.calls = 0
        self.active = 0
        self.peak = 0
        self.closed = False

    async def search(self, **kwargs):
        self.calls += 1
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0)
            if self.fail:
                raise RuntimeError("Controlled retrieval failure")
            results = Results([
                {"fullname": "Example.first", "source_text": json.dumps(kwargs, sort_keys=True),
                 "dependencies": ["Example.dependency"], "score": 1.0,
                 "context": {"namespace": "Example"}},
                {"fullname": "Example.second", "source_text": "theorem second : True := by trivial",
                 "dependencies": [], "score": 0.5, "context": {"namespace": "Example"}},
            ])
            if self.active > 1:
                if self.changed_field == "source_text":
                    results[0]["source_text"] += " changed"
                elif self.changed_field == "dependencies":
                    results[0]["dependencies"].append("Example.extra")
                elif self.changed_field == "order":
                    results.reverse()
            return results
        finally:
            self.active -= 1

    async def aclose(self):
        self.closed = True


def test_one_instance_warms_queries_and_compares_every_repeated_result(benchmark_module):
    client = Retriever()
    report = asyncio.run(benchmark_module.benchmark(
        client, queries=benchmark_module.QUERIES, levels=[1, 2, 4], repetitions=2, limit=10,
    ))
    assert report["success"]
    assert client.calls == 8 + 3 * 2 * 8
    assert client.peak == 4
    assert len(report["reference_results"]) == 8
    for phase in report["phases"]:
        assert phase["requests"] == 16
        assert phase["errors"] == phase["result_mismatches"] == 0
        assert phase["successful_requests_per_second"] > 0
        assert all(record["matches_reference"] for record in phase["records"])
        assert all(record["backend_timings"] == Results.timings for record in phase["records"])
        assert all(len(record["result_sha256"]) == 64 for record in phase["records"])


@pytest.mark.parametrize("changed_field, expected_path", [
    ("source_text", "$[0].source_text"),
    ("dependencies", "$[0].dependencies: length"),
    ("order", "$[0].fullname"),
])
def test_same_hit_count_with_changed_content_or_order_fails(
    benchmark_module, changed_field, expected_path,
):
    client = Retriever(changed_field=changed_field)
    report = asyncio.run(benchmark_module.benchmark(
        client, queries=benchmark_module.QUERIES, levels=[1, 4], repetitions=1, limit=10,
    ))
    assert not report["success"]
    assert report["phases"][0]["result_mismatches"] == 0
    assert report["phases"][1]["result_mismatches"] > 0
    mismatches = [record for record in report["phases"][1]["records"]
                  if not record["matches_reference"]]
    assert all(len(record["ranked_names"]) == 2 for record in mismatches)
    assert all(expected_path in record["difference_paths"] for record in mismatches)
    assert all(len(record["actual_results"]) == 2 for record in mismatches)


@pytest.mark.parametrize("mode", ["stable", "changed", "failed"])
def test_cli_writes_report_returns_failure_and_closes_one_instance(
    benchmark_module, monkeypatch, tmp_path, mode,
):
    from dvp.toolkits.retrieval import backend

    client = Retriever(changed_field="source_text" if mode == "changed" else None,
                       fail=mode == "failed")
    factory_calls = []

    def factory():
        factory_calls.append(True)
        return client

    monkeypatch.setattr(backend, "get_cached_retriever", factory)
    monkeypatch.setenv("DAVINCI_RETRIEVE_BACKEND", "lean_explore")
    output = tmp_path / "benchmark.json"
    exit_code = benchmark_module.main(["--concurrency", "4", "--output", str(output)])
    report = json.loads(output.read_text())
    assert exit_code == (0 if mode == "stable" else 1)
    assert report["success"] is (mode == "stable")
    assert factory_calls == [True]
    assert client.closed
    if mode == "failed":
        assert report["status"] == "warmup_failed"
        assert client.calls == 1
        assert "Controlled retrieval failure" in report["warmup"][0]["error"]
    else:
        assert [phase["client_concurrency"] for phase in report["phases"]] == [1, 4]
        assert report["configuration"]["local_concurrency"] == 4


def test_cli_rejects_jsonl_backend_before_constructing_retriever(
    benchmark_module, monkeypatch, tmp_path,
):
    from dvp.toolkits.retrieval import backend

    monkeypatch.setenv("DAVINCI_RETRIEVE_BACKEND", "jsonl")
    monkeypatch.setattr(backend, "get_cached_retriever", lambda: pytest.fail("Must stay local"))
    with pytest.raises(SystemExit) as exc:
        benchmark_module.main(["--output", str(tmp_path / "unexpected.json")])
    assert exc.value.code == 2
    assert not (tmp_path / "unexpected.json").exists()
