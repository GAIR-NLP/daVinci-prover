#!/usr/bin/env python3
"""Run pinned upstream informalization with a bounded pool of API endpoints.

The upstream prompt, dependency scheduling, cache keys, generation parameters,
client retries, result acceptance, and commit batches are used unchanged.
Only endpoint configuration, concurrency, observation, and whole-run recovery
are supplied here. A failed/incomplete layer stops before its dependents run.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import fcntl
import getpass
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
import traceback

import leanexplore_upstream as upstream
from leanexplore_upstream import PIN, digest, load_upstream, readonly


def now():
    return datetime.now(timezone.utc).isoformat()


def save(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def append(path, value):
    with path.open("a") as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + "\n")


def database_state(path):
    source = hashlib.sha256()
    descriptions = hashlib.sha256()
    vectors = hashlib.sha256()
    counts = Counter()
    existing_ids = []
    with readonly(path) as db:
        for row in db.execute("SELECT * FROM declarations ORDER BY id"):
            counts["declarations"] += 1
            raw = {k: row[k] for k in (
                "id", "name", "module", "docstring", "source_text",
                "source_link", "dependencies",
            )}
            source.update((json.dumps(raw, ensure_ascii=False, sort_keys=True) + "\n").encode())
            if row["informalization"] is None:
                counts["missing_descriptions"] += 1
            else:
                counts["descriptions"] += 1
                existing_ids.append(row["id"])
                if not row["informalization"].strip():
                    counts["empty_descriptions"] += 1
                descriptions.update(json.dumps(
                    [row["id"], row["informalization"]], ensure_ascii=False
                ).encode())
            blob = row["informalization_embedding"]
            if blob is None:
                counts["missing_vectors"] += 1
            else:
                counts["vectors"] += 1
                vectors.update(str(row["id"]).encode() + b"\0" + blob)
    return {
        "counts": dict(counts), "source_fields_sha256": source.hexdigest(),
        "existing_descriptions_sha256": descriptions.hexdigest(),
        "existing_vectors_sha256": vectors.hexdigest(),
        "existing_description_ids": existing_ids,
    }


def validate_existing(path, initial):
    descriptions = hashlib.sha256()
    ids = set(initial["existing_description_ids"])
    with readonly(path) as db:
        for identifier, text in db.execute(
            "SELECT id,informalization FROM declarations ORDER BY id"
        ):
            if identifier in ids:
                descriptions.update(json.dumps([identifier, text], ensure_ascii=False).encode())
    if descriptions.hexdigest() != initial["existing_descriptions_sha256"]:
        raise RuntimeError("A description that existed before this run was modified")


async def generate(args, key, modules, initial):
    from openai import AsyncOpenAI, DefaultAsyncHttpxClient
    from sqlalchemy.ext.asyncio import create_async_engine

    output = args.run_directory
    started = time.perf_counter()
    counters = Counter()
    endpoint_counts = [Counter() for _ in args.endpoint]
    request_starts = {}
    clients = []
    original_layer = modules["generation"].process_layer
    original_prompt = modules["generation"].build_prompt
    original_commit = modules["generation"].InformalizationRun.commit
    current_layer = {"pass": 0, "number": 0, "size": 0}
    committed = 0
    with readonly(args.database) as db:
        id_by_name = dict(db.execute("SELECT name,id FROM declarations"))

    def clean(value):
        return str(value).replace(key, "[REDACTED]")

    def status(state="running", **extra):
        elapsed = time.perf_counter() - started
        item = {
            "at": now(), "state": state, "pid": os.getpid(),
            "elapsed_seconds": elapsed,
            "initial_pending": initial["counts"].get("missing_descriptions", 0),
            "new_descriptions_committed": committed,
            "committed_per_second": committed / elapsed if elapsed else 0,
            "current_layer": current_layer.copy(),
            "http": dict(counters),
            "endpoints": [
                {"url": url, **dict(stats)}
                for url, stats in zip(args.endpoint, endpoint_counts)
            ],
            **extra,
        }
        save(output / "status.json", item)
        return item

    async def heartbeat():
        while True:
            status()
            await asyncio.sleep(30)

    def observe_prompt(data, template, context):
        prompt = original_prompt(data, template, context)
        append(output / "prompts.jsonl", {
            "id": data.id, "name": data.name, "at": now(),
            "pass": current_layer["pass"], "layer": current_layer["number"],
            "sha256": digest(prompt), "prompt": prompt,
        })
        return prompt

    async def observe_commit(run, updates):
        nonlocal committed
        await original_commit(run, updates)
        committed += len(updates)
        append(output / "commits.jsonl", {
            "at": now(), "count": len(updates), "ids": [u["id"] for u in updates],
            "total_this_process": committed,
        })
        status()

    async def observe_layer(run, layer, progress):
        current_layer["number"] += 1
        current_layer["size"] = len(layer)
        event = {**current_layer, "at": now(), "event": "start"}
        append(output / "layers.jsonl", event)
        status()
        start = time.perf_counter()
        count = await original_layer(run, layer, progress)
        append(output / "layers.jsonl", {
            **current_layer, "at": now(), "event": "end",
            "generated": count, "seconds": time.perf_counter() - start,
        })
        if count != len(layer):
            raise RuntimeError(
                f"Incomplete dependency layer: {count}/{len(layer)} generated; "
                "stop before dependents and resume upstream pipeline on remaining NULL rows"
            )
        return count

    def hooks(endpoint):
        async def request_hook(request):
            body = json.loads(request.content)
            messages = body.get("messages", [])
            prompt = messages[0].get("content", "") if messages else ""
            match = re.search(r"\*\*Declaration Name:\*\* ([^\n]+)", prompt)
            name = match.group(1) if match else None
            counters["requests"] += 1
            endpoint_counts[endpoint]["requests"] += 1
            metadata = {
                "sequence": counters["requests"], "endpoint": endpoint,
                "at": now(), "name": name, "id": id_by_name.get(name),
                "prompt_sha256": digest(prompt),
                "body_without_messages": {k: v for k, v in body.items() if k != "messages"},
                "message_roles": [m.get("role") for m in messages],
            }
            append(output / "requests.jsonl", metadata)
            request_starts[id(request)] = (time.perf_counter(), metadata)

        async def response_hook(response):
            await response.aread()
            start, metadata = request_starts.pop(id(response.request))
            counters["responses"] += 1
            endpoint_counts[endpoint]["responses"] += 1
            status_key = f"http_{response.status_code}"
            counters[status_key] += 1
            endpoint_counts[endpoint][status_key] += 1
            record = {
                **metadata, "finished_at": now(),
                "seconds": time.perf_counter() - start,
                "http_status": response.status_code,
            }
            try:
                data = response.json()
                choice = data["choices"][0]
                message = choice["message"]
                content = message.get("content")
                reason = choice.get("finish_reason")
                record.update(
                    model=data.get("model"), content=content,
                    finish_reason=reason, usage=data.get("usage"),
                    reasoning_chars=len(message.get("reasoning_content") or message.get("reasoning") or ""),
                )
                counters[f"finish_{reason}"] += 1
                endpoint_counts[endpoint][f"finish_{reason}"] += 1
                if not (content or "").strip():
                    counters["empty_content"] += 1
                    endpoint_counts[endpoint]["empty_content"] += 1
            except (ValueError, KeyError, IndexError, TypeError, AttributeError):
                record["error_excerpt"] = clean(response.text[:1000])
            append(output / "responses.jsonl", record)
        return {"request": [request_hook], "response": [response_hook]}

    class EndpointClient(modules["client"].OpenRouterClient):
        def __init__(self, endpoint):
            self.client = AsyncOpenAI(
                base_url=args.endpoint[endpoint], api_key=key,
                http_client=DefaultAsyncHttpxClient(event_hooks=hooks(endpoint)),
            )
            clients.append(self.client)
        # generate (including the upstream retry decorator) is inherited.

    class ClientPool:
        def __init__(self):
            self.members = [EndpointClient(i) for i in range(len(args.endpoint))]
            self.available = asyncio.Queue()
            for _ in range(args.concurrency_per_endpoint):
                for i in range(len(args.endpoint)):
                    self.available.put_nowait(i)

        async def generate(self, **kwargs):
            endpoint = await self.available.get()
            try:
                return await self.members[endpoint].generate(**kwargs)
            finally:
                self.available.put_nowait(endpoint)

    modules["pipeline"].OpenRouterClient = ClientPool
    modules["pipeline"].discover_database_files = lambda: [args.cache_database]
    modules["generation"].build_prompt = observe_prompt
    modules["generation"].process_layer = observe_layer
    modules["generation"].InformalizationRun.commit = observe_commit
    beat = asyncio.create_task(heartbeat())
    failure = None
    passes = []
    try:
        for number in range(1, args.max_resume_passes + 1):
            current_layer.update({"pass": number, "number": 0, "size": 0})
            engine = create_async_engine(f"sqlite+aiosqlite:///{args.database}")
            tasks_before = asyncio.all_tasks()
            attempt = {"pass": number, "started_at": now()}
            try:
                await modules["pipeline"].informalize_declarations(
                    engine, model=args.model,
                    max_concurrent=len(args.endpoint) * args.concurrency_per_endpoint,
                )
                with readonly(args.database) as db:
                    remaining = db.execute(
                        "SELECT COUNT(*) FROM declarations WHERE informalization IS NULL OR trim(informalization)=''"
                    ).fetchone()[0]
                if remaining:
                    raise RuntimeError(f"{remaining} descriptions remain incomplete")
                attempt["state"] = "completed"
                passes.append(attempt)
                break
            except Exception as exc:
                attempt.update(state="failed", error_type=type(exc).__name__, error=clean(exc)[:2000])
                passes.append(attempt)
                append(output / "recovery.jsonl", attempt)
                if number == args.max_resume_passes:
                    raise
            finally:
                remaining_tasks = asyncio.all_tasks() - tasks_before
                for task in remaining_tasks:
                    task.cancel()
                await asyncio.gather(*remaining_tasks, return_exceptions=True)
                await engine.dispose()
                for client in clients:
                    await client.close()
                clients.clear()
            if attempt["state"] == "failed":
                await asyncio.sleep(10)
        final = database_state(args.database)
        validate_existing(args.database, initial)
        if initial["source_fields_sha256"] != final["source_fields_sha256"]:
            raise RuntimeError("Declaration source fields changed during generation")
        if initial["existing_vectors_sha256"] != final["existing_vectors_sha256"]:
            raise RuntimeError("Existing vectors changed during description generation")
        if final["counts"].get("missing_descriptions") or final["counts"].get("empty_descriptions"):
            raise RuntimeError("Description coverage is incomplete")
        final.pop("existing_description_ids")
        report = status("completed", final_database=final, passes=passes)
        save(output / "report.json", report)
    except BaseException as exc:
        failure = {"error_type": type(exc).__name__, "error": clean(exc)[:2000], "passes": passes}
        save(output / "report.json", status("failed", **failure))
        raise
    finally:
        beat.cancel()
        await asyncio.gather(beat, return_exceptions=True)
        for client in clients:
            await client.close()
    return failure


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--cache-database", type=Path, required=True)
    parser.add_argument("--run-directory", type=Path, required=True)
    parser.add_argument("--endpoint", action="append", required=True)
    parser.add_argument("--concurrency-per-endpoint", type=int, default=64)
    parser.add_argument("--model", required=True)
    parser.add_argument("--extractor-source", type=Path, required=True)
    parser.add_argument("--max-resume-passes", type=int, default=3)
    parser.add_argument("--detach", action="store_true")
    args = parser.parse_args()
    if args.concurrency_per_endpoint < 1 or args.max_resume_passes < 1:
        parser.error("Concurrency and recovery pass count must be positive")
    args.database = args.database.resolve(strict=True)
    args.cache_database = args.cache_database.resolve(strict=True)
    args.run_directory = args.run_directory.resolve()
    args.endpoint = [url.rstrip("/") for url in args.endpoint]
    if len(set(args.endpoint)) != len(args.endpoint):
        parser.error("Duplicate endpoints")
    args.run_directory.mkdir(parents=True, exist_ok=False)
    key = os.environ.get("INF_API_KEY") or getpass.getpass("INF_API_KEY (hidden): ")
    if not key:
        raise SystemExit("Missing key")

    def redacted_exception(kind, value, trace):
        message = "".join(traceback.format_exception(kind, value, trace))
        sys.stderr.write(message.replace(key, "[REDACTED]"))

    sys.excepthook = redacted_exception
    if args.detach:
        child = os.fork()
        if child:
            save(args.run_directory / "launcher.json", {"pid": child, "started_at": now()})
            print(json.dumps({"pid": child, "run_directory": str(args.run_directory)}, ensure_ascii=False), flush=True)
            return
        os.setsid()
        descriptor = os.open(args.run_directory / "console.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.dup2(descriptor, 1)
        os.dup2(descriptor, 2)
        os.close(descriptor)
        stdin = os.open(os.devnull, os.O_RDONLY)
        os.dup2(stdin, 0)
        os.close(stdin)
    lock = args.database.with_suffix(".generation.lock").open("w")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    lock.write(str(os.getpid()))
    lock.flush()

    class Redact(logging.Filter):
        def filter(self, record):
            record.msg = record.getMessage().replace(key, "[REDACTED]")
            record.args = ()
            return True

    handler = logging.FileHandler(args.run_directory / "pipeline.log")
    handler.addFilter(Redact())

    class RedactedFormatter(logging.Formatter):
        def format(self, record):
            return super().format(record).replace(key, "[REDACTED]")

    handler.setFormatter(RedactedFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    modules = load_upstream(args.extractor_source.resolve())
    initial = database_state(args.database)
    save(args.run_directory / "initial-database.json", initial)
    save(args.run_directory / "manifest.json", {
        "created_at": now(), "pid": os.getpid(), "database": str(args.database),
        "cache_database": str(args.cache_database), "endpoints": args.endpoint,
        "concurrency_per_endpoint": args.concurrency_per_endpoint,
        "total_concurrency": len(args.endpoint) * args.concurrency_per_endpoint,
        "model": args.model, "upstream_commit": PIN,
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "helper_sha256": hashlib.sha256(Path(upstream.__file__).read_bytes()).hexdigest(),
        "prompt_sha256": hashlib.sha256(modules["pipeline"].PROMPT_PATH.read_bytes()).hexdigest(),
        "generation_parameters": {"temperature": 0.3, "max_tokens": None, "thinking": "not specified"},
        "upstream_generate_unmodified": True, "commit_batch_size": 1000,
        "cache_policy": "original (name,source_text), official baseline only",
        "recovery": "Stop before dependents of an incomplete layer; rerun the original pipeline on NULL rows, at most max_resume_passes times.",
        "max_resume_passes": args.max_resume_passes,
        "credentials_persisted": False,
    })
    asyncio.run(generate(args, key, modules, initial))


if __name__ == "__main__":
    main()
