#!/usr/bin/env python3
"""Run the pinned, unchanged LeanExplore pipeline on an isolated dependency closure.

The authorized model/endpoint, cache discovery, and optional concurrency are configured.
Observation hooks record requests, responses, and actual dependency contexts
without changing prompts, scheduling, acceptance, or retry behavior.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import getpass
import hashlib
import inspect
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import sys
import time


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "leanexplore"))
import leanexplore_upstream as upstream
from leanexplore_upstream import PIN, digest, load_upstream, readonly


COLUMNS = (
    "id", "name", "module", "docstring", "source_text", "source_link",
    "dependencies", "informalization", "informalization_embedding",
)


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def append_json(path: Path, data: object) -> None:
    with path.open("a") as handle:
        handle.write(json.dumps(data, ensure_ascii=False) + "\n")


def emit(data: object) -> None:
    print(json.dumps(data, ensure_ascii=False), flush=True)


def prepare(args: argparse.Namespace, modules: dict) -> dict:
    from sqlalchemy import create_engine

    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if (output / "test.db").exists():
        raise FileExistsError("Use a fresh output directory; existing test.db will not be overwritten")
    selection = json.loads(args.selection.read_text())
    ids = sorted(selection["temporary_database_minimum_ids"])
    source = readonly(args.source_database)
    baseline = readonly(args.cache_database)
    rows = []
    cache_rows = {}
    expected_hits = set()
    cache_evidence = []
    for identifier in ids:
        row = source.execute("SELECT * FROM declarations WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise ValueError(f"Missing declaration {identifier}")
        data = dict(row)
        old = baseline.execute("SELECT * FROM declarations WHERE name=?", (row["name"],)).fetchone()
        matches = bool(
            old is not None and old["source_text"] == row["source_text"]
            and old["informalization"] is not None
        )
        if matches:
            expected_hits.add(identifier)
            if data["informalization"] != old["informalization"]:
                raise ValueError(f"Current cached value differs from verified baseline: {row['name']}")
        if old is not None:
            old_data = dict(old)
            old_data["informalization_embedding"] = None
            cache_rows[old["id"]] = old_data
        cache_evidence.append({
            "id": identifier, "name": row["name"], "hit": matches,
            "source_sha256": digest(row["source_text"]),
            "baseline_id": old["id"] if old is not None else None,
            "baseline_source_sha256": digest(old["source_text"]) if old is not None else None,
        })
        data["informalization"] = None
        data["informalization_embedding"] = None
        rows.append(data)

    missing_ids = set(ids) - expected_hits
    if missing_ids != set(selection["generation_closure_ids"]):
        raise ValueError("Actual upstream cache misses differ from the selected generation closure")
    selected_names = {r["name"] for r in rows}
    absent_names = set()
    for row in rows:
        if row["id"] not in missing_ids:
            continue
        for name in modules["dependency_layers"].parse_dependencies(row["dependencies"]):
            dependency = source.execute("SELECT id FROM declarations WHERE name=?", (name,)).fetchone()
            if dependency is not None and name not in selected_names:
                raise ValueError(f"Dependency closure is incomplete: {row['name']} -> {name}")
            if dependency is None:
                absent_names.add(name)
    source.close()
    baseline.close()

    for filename, values in (("test.db", rows), ("cache/lean_explore.db", list(cache_rows.values()))):
        path = output / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(f"sqlite:///{path}")
        modules["models"].Base.metadata.create_all(engine)
        engine.dispose()
        with sqlite3.connect(path) as connection:
            connection.executemany(
                f"INSERT INTO declarations ({','.join(COLUMNS)}) VALUES ({','.join('?' for _ in COLUMNS)})",
                [tuple(row[column] for column in COLUMNS) for row in values],
            )
    for row in rows:
        append_json(output / "input-declarations.jsonl", row)
    write_json(output / "cache-matches.json", cache_evidence)
    signature = inspect.signature(modules["pipeline"].informalize_declarations)
    manifest = {
        "created_at": timestamp(),
        "status": "prepared",
        "upstream_commit": PIN,
        "test_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "helper_sha256": hashlib.sha256(Path(upstream.__file__).read_bytes()).hexdigest(),
        "upstream_sources": {
            name: {"path": str(Path(module.__file__)), "sha256": hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()}
            for name, module in modules.items()
        },
        "prompt_sha256": hashlib.sha256(modules["pipeline"].PROMPT_PATH.read_bytes()).hexdigest(),
        "upstream_defaults": {
            name: signature.parameters[name].default
            for name in ("model", "commit_batch_size", "max_concurrent", "limit")
        },
        "effective_concurrency": (
            args.concurrency if args.concurrency is not None
            else signature.parameters["max_concurrent"].default
        ),
        "concurrency_override": args.concurrency,
        "source_database": str(args.source_database.resolve()),
        "baseline_database": str(args.cache_database.resolve()),
        "selection": str(args.selection.resolve()),
        "base_url": args.base_url.rstrip("/"),
        "model": args.model,
        "proxy_environment_present": {
            name: bool(os.environ.get(name))
            for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
        },
        "input_rows": len(rows),
        "expected_cache_hits": len(expected_hits),
        "expected_generations": len(missing_ids),
        "expected_generation_layers": [len(layer) for layer in selection["generation_layer_ids"]],
        "missing_from_source_database_names": sorted(absent_names),
        "cache_discovery": "Restricted to an exact name-projected subset of the verified official baseline; same (name,source_text) keys and values as that full baseline for every test declaration.",
        "model_parameters": {"temperature": 0.3, "max_tokens": None, "thinking": "not specified"},
        "algorithm_changes": [],
        "observational_hooks": ["build_prompt", "process_layer", "HTTP request/response"],
        "accepted_response_rule": "Unchanged upstream: nonempty message.content, stripped; format/finish reason are only audited afterward.",
        "source_database_mutations": False,
        "embedding_generation": False,
    }
    write_json(output / "manifest.json", manifest)
    emit({"prepared": {"input_rows": len(rows), "cache_hits": len(expected_hits), "generations": len(missing_ids), "layers": manifest["expected_generation_layers"]}})
    return {"manifest": manifest, "selection": selection, "rows": rows, "cache_hit_ids": expected_hits}


async def run(args: argparse.Namespace, modules: dict, prepared: dict, key: str) -> None:
    from openai import AsyncOpenAI, DefaultAsyncHttpxClient
    from sqlalchemy.ext.asyncio import create_async_engine

    output = args.output.resolve()
    selection = prepared["selection"]
    manifest = prepared["manifest"]
    by_name = {row["name"]: row for row in prepared["rows"]}
    generation_ids = set(selection["generation_closure_ids"])
    layer_of = {
        identifier: layer for layer, ids in enumerate(selection["generation_layer_ids"])
        for identifier in ids
    }
    pipeline, generation = modules["pipeline"], modules["generation"]
    client_class = modules["client"].OpenRouterClient
    original_build_prompt = generation.build_prompt
    original_process_layer = generation.process_layer
    prompt_rows = []
    response_rows = []
    request_sequence = 0
    requests_started = {}
    clients = []
    started = time.perf_counter()

    def observe_prompt(data, prompt_template, informalizations_by_name):
        prompt = original_build_prompt(data, prompt_template, informalizations_by_name)
        dependencies = []
        for name in modules["dependency_layers"].parse_dependencies(data.dependencies)[:20]:
            text = informalizations_by_name.get(name)
            row = by_name.get(name)
            dependencies.append({
                "name": name,
                "available": text is not None,
                "origin": (
                    "generated_in_this_run" if row and row["id"] in generation_ids
                    else "official_cache"
                ) if text is not None else "unavailable",
                "full_description_sha256": digest(text) if text is not None else None,
                "included_description": (text[:253] + "..." if len(text) > 256 else text) if text is not None else None,
            })
        recorded = {
            "id": data.id, "name": data.name, "layer": layer_of[data.id],
            "created_at": timestamp(), "prompt": prompt,
            "prompt_sha256": digest(prompt), "dependencies": dependencies,
        }
        prompt_rows.append(recorded)
        append_json(output / "prompts.jsonl", recorded)
        return prompt

    async def observe_layer(run_state, layer, layer_progress):
        layer_index = layer_of[layer[0].id]
        append_json(output / "layers.jsonl", {
            "event": "start", "layer": layer_index, "at": timestamp(),
            "ids": [d.id for d in layer],
            "context_size": len(run_state.informalizations_by_name),
        })
        start = time.perf_counter()
        emit({"layer_start": layer_index + 1, "declarations": len(layer)})
        count = await original_process_layer(run_state, layer, layer_progress)
        result = {
            "event": "end", "layer": layer_index, "at": timestamp(),
            "generated": count, "seconds": time.perf_counter() - start,
            "context_size": len(run_state.informalizations_by_name),
        }
        append_json(output / "layers.jsonl", result)
        emit(result)
        return count

    async def observe_request(request):
        nonlocal request_sequence
        request_sequence += 1
        payload = json.loads(request.content)
        messages = payload.get("messages", [])
        prompt = messages[0].get("content", "") if messages else ""
        match = re.search(r"\*\*Declaration Name:\*\* ([^\n]+)", prompt)
        name = match.group(1) if match else None
        recorded = {
            "sequence": request_sequence, "name": name,
            "id": by_name[name]["id"] if name in by_name else None,
            "started_at": timestamp(), "url": str(request.url),
            "body_without_messages": {k: v for k, v in payload.items() if k != "messages"},
            "message_roles": [m.get("role") for m in messages],
            "prompt_sha256": digest(prompt),
        }
        append_json(output / "http-requests.jsonl", recorded)
        requests_started[id(request)] = (time.perf_counter(), recorded)

    async def observe_response(response):
        await response.aread()
        start, request = requests_started.pop(id(response.request))
        row = {
            **request, "finished_at": timestamp(), "seconds": time.perf_counter() - start,
            "http_status": response.status_code,
        }
        try:
            data = response.json()
            row["response_model"] = data.get("model")
            row["usage"] = data.get("usage")
            choice = data["choices"][0]
            message = choice["message"]
            row["content"] = message.get("content")
            row["reasoning_chars"] = len(message.get("reasoning_content") or message.get("reasoning") or "")
            row["finish_reason"] = choice.get("finish_reason")
            row["response_has_content_before_strip"] = bool(message.get("content"))
        except (ValueError, KeyError, IndexError, AttributeError, TypeError):
            row["error_excerpt"] = response.text[:1000].replace(key, "[REDACTED]")
        response_rows.append(row)
        append_json(output / "responses.jsonl", row)
        if len(response_rows) % 10 == 0 or not row.get("response_has_content_before_strip", False):
            emit({"http_responses": len(response_rows), "last_name": row["name"],
                  "last_finish": row.get("finish_reason"), "last_seconds": row["seconds"]})

    class RoutedOfficialClient(client_class):
        def __init__(self):
            # SDK defaults are preserved. Hooks only observe already-needed I/O.
            self.client = AsyncOpenAI(
                base_url=args.base_url.rstrip("/"), api_key=key,
                http_client=DefaultAsyncHttpxClient(
                    event_hooks={"request": [observe_request], "response": [observe_response]}
                ),
            )
            clients.append(self.client)

    # These replace configuration/observation boundaries, not the algorithms.
    pipeline.OpenRouterClient = RoutedOfficialClient
    pipeline.discover_database_files = lambda: [output / "cache/lean_explore.db"]
    generation.build_prompt = observe_prompt
    generation.process_layer = observe_layer
    engine = create_async_engine(f"sqlite+aiosqlite:///{output / 'test.db'}")
    manifest["status"] = "running"
    manifest["started_at"] = timestamp()
    write_json(output / "manifest.json", manifest)
    failure = None
    try:
        options = {"model": args.model}
        if args.concurrency is not None:
            options["max_concurrent"] = args.concurrency
        await pipeline.informalize_declarations(engine, **options)
    except Exception as exc:
        failure = {"type": type(exc).__name__, "message": str(exc)[:1500].replace(key, "[REDACTED]")}
        raise
    finally:
        await engine.dispose()
        for client in clients:
            await client.close()
        elapsed = time.perf_counter() - started
        with readonly(output / "test.db") as connection:
            results = [dict(row) for row in connection.execute(
                "SELECT id,name,module,informalization FROM declarations ORDER BY id"
            )]
        for row in results:
            row["origin"] = "official_cache" if row["id"] in prepared["cache_hit_ids"] else "generation"
            row["root_sample"] = row["id"] in selection["root_ids"]
            append_json(output / "results.jsonl", row)
        generated = [row for row in results if row["id"] in generation_ids and row["informalization"] is not None]
        by_id = {row["id"]: row for row in results}
        by_result_name = {row["name"]: row for row in results}
        context_checks = []
        for prompt in prompt_rows:
            for dep in prompt["dependencies"]:
                if dep["origin"] != "generated_in_this_run":
                    continue
                predecessor = by_result_name[dep["name"]]
                text = predecessor["informalization"]
                snippet = (text[:253] + "..." if len(text) > 256 else text) if text is not None else None
                context_checks.append({
                    "dependent_id": prompt["id"], "dependency_id": predecessor["id"],
                    "dependency_name": dep["name"],
                    "persisted": text is not None,
                    "earlier_layer": layer_of[predecessor["id"]] < prompt["layer"],
                    "full_description_matches": text is not None and digest(text) == dep["full_description_sha256"],
                    "exact_snippet_in_prompt": snippet is not None and f"- {dep['name']}: {snippet}" in prompt["prompt"],
                })
        write_json(output / "dependency-context-checks.json", context_checks)
        report = {
            "status": "completed" if failure is None else "failed",
            "error": failure, "elapsed_seconds": elapsed,
            "concurrency": manifest["effective_concurrency"],
            "input_rows": len(results), "official_cache_hits": len(prepared["cache_hit_ids"]),
            "expected_generations": len(generation_ids), "generated_nonnull": len(generated),
            "pending_ids": sorted(generation_ids - {r["id"] for r in generated}),
            "root_count": len(selection["root_ids"]),
            "root_cache_hits": len(selection["root_cache_hit_ids"]),
            "root_generated_nonnull": sum(by_id[i]["informalization"] is not None for i in selection["root_generation_required_ids"]),
            "http_requests": request_sequence, "http_responses": len(response_rows),
            "http_statuses": dict(Counter(str(r["http_status"]) for r in response_rows)),
            "finish_reasons": dict(Counter(str(r.get("finish_reason")) for r in response_rows)),
            "responses_with_reasoning_content": sum(r.get("reasoning_chars", 0) > 0 for r in response_rows),
            "total_prompt_tokens": sum((r.get("usage") or {}).get("prompt_tokens", 0) or 0 for r in response_rows),
            "total_completion_tokens": sum((r.get("usage") or {}).get("completion_tokens", 0) or 0 for r in response_rows),
            "new_dependency_context_edges_checked": len(context_checks),
            "dependency_context_checks_pass": all(c["earlier_layer"] and c["full_description_matches"] and c["exact_snippet_in_prompt"] for c in context_checks),
            "source_database_modified": False,
            "semantic_quality_audit": "Separate; unchanged official acceptance is not semantic verification.",
        }
        write_json(output / "run-report.json", report)
        manifest["status"] = report["status"]
        manifest["finished_at"] = timestamp()
        write_json(output / "manifest.json", manifest)
        emit(report)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extractor-source", type=Path, required=True)
    parser.add_argument("--source-database", type=Path, required=True)
    parser.add_argument("--cache-database", type=Path, required=True)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--concurrency", type=int, help="Override only the upstream max_concurrent option; omitted preserves the official default.")
    args = parser.parse_args()
    if args.concurrency is not None and args.concurrency < 1:
        parser.error("--concurrency must be positive")
    args.extractor_source = args.extractor_source.resolve()
    modules = load_upstream(args.extractor_source)
    prepared = prepare(args, modules)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(args.output / "pipeline.log")],
    )
    key = os.environ.get("INF_API_KEY") or getpass.getpass("INF_API_KEY (hidden): ")
    if not key:
        raise SystemExit("Missing API key")
    asyncio.run(run(args, modules, prepared, key))


if __name__ == "__main__":
    main()
