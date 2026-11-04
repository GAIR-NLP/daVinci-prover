"""Exercise local retrieval without a proof-generation model or Lean calls.

Run after configuring the local backend with examples/leanexplore-local.env.sh
or the equivalent environment variables documented in docs/leanexplore.md.
Internet sockets and DNS are denied
for this process; Unix sockets used by asyncio remain available.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import json
import os
from pathlib import Path
import socket
import time
from types import SimpleNamespace


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", action="append", help="Natural-language query; may be repeated")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--output", type=Path, help="Write full raw and model-visible evidence to JSON")
    args = parser.parse_args()
    if os.getenv("DAVINCI_RETRIEVE_BACKEND", "lean_explore").strip().lower() not in {
        "lean_explore", "lean-explore", "leanexplore",
    }:
        parser.error("This check requires DAVINCI_RETRIEVE_BACKEND=lean_explore; "
                     "see examples/leanexplore-local.env.sh")

    network_attempts: list[str] = []
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def denied(operation):
        network_attempts.append(operation)
        raise RuntimeError("Network access is disabled in this local retrieval check")

    def connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            denied("connect")
        return original_connect(sock, address)

    def connect_ex(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            denied("connect_ex")
        return original_connect_ex(sock, address)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.getaddrinfo = lambda *a, **kw: denied("DNS")
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1")

    from dvp.agent.result_projection import _compact
    from dvp.toolkits.retrieval.backend import close_cached_retrievers
    from dvp.toolkits.retrieval.execution import lean_retrieve

    queries = [{"natural_language_query": query} for query in args.query] if args.query else [
        {"lean_name": "Nat.Prime.dvd_mul"},
        {"natural_language_query": "A continuous real-valued function on a compact set is bounded"},
    ]

    async def check():
        records = []
        registry = SimpleNamespace(workspace=SimpleNamespace(target=None), retrieve_index=None)
        for query in queries:
            arguments = {**query, "limit": args.limit, "include_theorem_proof": True}
            started = time.monotonic()
            raw = await lean_retrieve(registry, arguments)
            if not raw.get("success") or not raw.get("results"):
                raise RuntimeError(f"Local retrieval failed: {raw.get('error', raw)}")
            model = _compact(raw, tool_name="lean_retrieve")
            summary = {
                "query": query, "seconds": round(time.monotonic() - started, 2),
                "hits": len(raw["results"]), "visible_hits": len(model["results"]),
                "names": [hit["fullname"] for hit in raw["results"]],
                "with_semantic": sum(bool(hit.get("semantic")) for hit in model["results"]),
                "context_statuses": dict(Counter(hit["context_status"] for hit in raw["results"])),
                "source_revisions": sorted({hit.get("source_revision", "") for hit in raw["results"]}),
            }
            records.append({"summary": summary, "raw": raw, "model_visible": model})
            print(json.dumps(summary, ensure_ascii=False), flush=True)
        return records

    async def checked_and_closed():
        try:
            return await check()
        finally:
            await close_cached_retrievers()

    records = asyncio.run(checked_and_closed())
    report = {"network_attempts": network_attempts,
              "snapshot": os.environ.get("LEAN_EXPLORE_VERSION"), "queries": records}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    if network_attempts:
        raise RuntimeError(f"Unexpected network attempts: {network_attempts}")
    print("Local retrieval passed; network attempts: 0", flush=True)


if __name__ == "__main__":
    main()
