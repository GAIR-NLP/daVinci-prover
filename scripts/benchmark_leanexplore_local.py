"""Measure configured local retrieval throughput and check concurrent results.

One retriever loads the models once. After warming every query, the script runs
the same workload sequentially and at each requested client concurrency. This
compares loads on the configured implementation, not old and new algorithms.
No proof-generation model or Lean process is called. Model downloads are disabled.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import inspect
import json
import math
import os
from pathlib import Path
import platform
import statistics
import sys
import time
from typing import Any


QUERIES = [
    {"lean_name": "Nat.Prime.dvd_mul"},
    {"lean_name": "Finset.card_bij"},
    {"keywords": "continuous compact bounded"},
    {"keywords": "permutation cycle order"},
    {"natural_language_query": "A continuous real-valued function on a compact set is bounded"},
    {"natural_language_query": "Count a finite set by partitioning it into fibers of a function"},
    {"lean_name": "Nat.Coprime", "keywords": "coprime product divisibility"},
    {"keywords": "finite sum bijection", "natural_language_query": "Reindex a finite sum along a bijection"},
]


def digest(value: Any) -> str:
    """Hash all JSON fields and list order; no search-result fields are removed."""
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def difference_paths(expected: Any, actual: Any, path: str = "$", limit: int = 20) -> list[str]:
    """Describe the first differences without duplicating large source bodies."""
    if type(expected) is not type(actual):
        return [path + ": type"]
    if isinstance(expected, dict):
        differences = []
        for key in sorted(expected.keys() | actual.keys()):
            child = f"{path}.{key}"
            if key not in expected or key not in actual:
                differences.append(child + ": missing/extra")
            else:
                differences.extend(difference_paths(expected[key], actual[key], child, limit))
            if len(differences) >= limit:
                break
        return differences[:limit]
    if isinstance(expected, list):
        differences = [path + ": length"] if len(expected) != len(actual) else []
        for index, (left, right) in enumerate(zip(expected, actual)):
            differences.extend(difference_paths(left, right, f"{path}[{index}]", limit))
            if len(differences) >= limit:
                break
        return differences[:limit]
    return [] if expected == actual else [path]


def percentile(values: list[float], proportion: float) -> float:
    return sorted(values)[max(0, math.ceil(len(values) * proportion) - 1)]


async def measure_call(retriever: Any, query: dict[str, str], limit: int) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        response = await retriever.search(**query, limit=limit)
        # Copy through JSON so comparisons cover exactly the serializable result,
        # including source text, dependencies, context, scores, and ranking.
        results = json.loads(json.dumps(list(response), ensure_ascii=False, allow_nan=False))
        record = {
            "results": results,
            "result_sha256": digest(results),
            "ranked_names": [item.get("fullname") for item in results],
            "error": None if results else "Search returned no results",
        }
        timings = getattr(response, "timings", None)
        if timings is not None:
            record["backend_timings"] = dict(timings)
    except Exception as exc:
        record = {"error": f"{type(exc).__name__}: {exc}"}
    record["request_seconds"] = time.perf_counter() - started
    return record


async def benchmark(
    retriever: Any, *, queries: list[dict[str, str]], levels: list[int],
    repetitions: int, limit: int,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "comparison": "One configured retriever: sequential versus concurrent client requests",
        "equality_policy": "Exact equality of every returned JSON field and result order; timings are separate",
        "queries": queries, "repetitions": repetitions, "limit": limit,
        "warmup": [], "reference_results": {}, "phases": [], "success": False,
    }
    for index, query in enumerate(queries):
        record = await measure_call(retriever, query, limit)
        record.pop("results", None)
        report["warmup"].append({"query_index": index, **record})
        if record["error"]:
            report["status"] = "warmup_failed"
            return report

    references: dict[int, list[dict[str, Any]]] = {}
    for concurrency in levels:
        gate = asyncio.Semaphore(concurrency)
        phase_started = time.perf_counter()

        async def run_one(query_index: int, repeat_index: int) -> dict[str, Any]:
            queued = time.perf_counter()
            async with gate:
                client_queue = time.perf_counter() - queued
                record = await measure_call(retriever, queries[query_index], limit)
            record.update(query_index=query_index, repeat_index=repeat_index,
                          client_queue_seconds=client_queue)
            results = record.pop("results", None)
            if record["error"]:
                record["matches_reference"] = False
            elif query_index not in references:
                references[query_index] = results
                record["matches_reference"] = True
            else:
                record["matches_reference"] = digest(references[query_index]) == record["result_sha256"]
                if not record["matches_reference"]:
                    record["difference_paths"] = difference_paths(references[query_index], results)
                    record["actual_results"] = results
            return record

        records = await asyncio.gather(*(
            run_one(index, repeat)
            for repeat in range(repetitions) for index in range(len(queries))
        ))
        elapsed = time.perf_counter() - phase_started
        latencies = [record["request_seconds"] for record in records]
        errors = sum(bool(record["error"]) for record in records)
        mismatches = sum(not record["matches_reference"] and not record["error"] for record in records)
        phase = {
            "client_concurrency": concurrency, "requests": len(records),
            "wall_seconds": elapsed, "successful_requests_per_second": (len(records) - errors) / elapsed,
            "request_latency_seconds": {
                "mean": statistics.mean(latencies), "p50": percentile(latencies, 0.5),
                "p95": percentile(latencies, 0.95), "max": max(latencies),
            },
            "errors": errors, "result_mismatches": mismatches, "records": records,
        }
        report["phases"].append(phase)
        print(json.dumps({key: value for key, value in phase.items() if key != "records"}),
              file=sys.stderr, flush=True)
        # Never promote later concurrent results to a missing sequential reference.
        if concurrency == 1 and errors:
            report["status"] = "sequential_reference_failed"
            break

    report["reference_results"] = references
    report["success"] = all(not phase["errors"] and not phase["result_mismatches"]
                            for phase in report["phases"])
    report.setdefault("status", "completed")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--concurrency", type=int, nargs="+", default=[1, 2, 4],
                        help="Client concurrency levels; sequential baseline 1 is always run first")
    parser.add_argument("--repetitions", type=int, default=1,
                        help="Repeat the eight-query workload per level; use 4 or more to exercise concurrency 30")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--output", type=Path, required=True, help="JSON report path")
    args = parser.parse_args(argv)
    if args.repetitions < 1 or not 1 <= args.limit <= 50 or any(n < 1 for n in args.concurrency):
        parser.error("Repetitions and concurrency must be positive; limit must be between 1 and 50")
    backend = os.getenv("DAVINCI_RETRIEVE_BACKEND", "lean_explore").lower()
    if backend not in {"lean_explore", "lean-explore", "leanexplore"} or os.getenv(
        "DAVINCI_LEANEXPLORE_BACKEND", "local"
    ).lower() != "local":
        parser.error("This benchmark requires DAVINCI_RETRIEVE_BACKEND=lean_explore and DAVINCI_LEANEXPLORE_BACKEND=local")

    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1")
    from dvp.toolkits.retrieval.backend import get_cached_retriever

    levels = list(dict.fromkeys([1, *args.concurrency]))

    async def run() -> dict[str, Any]:
        retriever = get_cached_retriever()
        try:
            report = await benchmark(retriever, queries=QUERIES, levels=levels,
                                     repetitions=args.repetitions, limit=args.limit)
            report["configuration"] = {
                name: getattr(retriever, name, None) for name in (
                    "backend", "bm25_only", "bm25_k", "rerank_top", "packages",
                    "include_context", "local_concurrency", "local_max_pending",
                )
            }
            report["configuration"]["snapshot"] = os.getenv("LEAN_EXPLORE_VERSION")
            return report
        finally:
            close = getattr(retriever, "aclose", None)
            if close is not None:
                result = close()
                if inspect.isawaitable(result):
                    await result

    report = asyncio.run(run())
    report["environment"] = {"python": platform.python_version(), "executable": sys.executable}
    report["package_versions"] = {}
    for package in ("dvp", "lean-explore", "torch", "sentence-transformers", "transformers", "bm25s", "faiss-cpu"):
        try:
            report["package_versions"][package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            pass
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"success": report["success"], "report": str(args.output)}, ensure_ascii=False))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
