#!/usr/bin/env python3
"""Check a deployed retrieval service through the real DVP tool and projection."""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.getenv("DAVINCI_LEANEXPLORE_URL"))
    parser.add_argument("--lean-version", default=os.getenv("DAVINCI_LEANEXPLORE_EXPECTED_LEAN_VERSION", "4.35.0"))
    parser.add_argument("--mathlib-commit", default=os.getenv(
        "DAVINCI_LEANEXPLORE_EXPECTED_MATHLIB_COMMIT", "56a005855ea17e0b0958f73ec801ae140b0a1679"))
    parser.add_argument("--snapshot", default=os.getenv("DAVINCI_LEANEXPLORE_EXPECTED_SNAPSHOT", "20261020_060431"))
    parser.add_argument("--timeout", type=float, default=float(os.getenv("DAVINCI_LEANEXPLORE_TIMEOUT", "300")))
    parser.add_argument("--query", action="append", help="Natural-language query; repeat for multiple queries")
    parser.add_argument("--limit", type=int, choices=range(1, 51), default=10, metavar="1..50")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.url:
        parser.error("Provide --url http://SERVER:8765 or set DAVINCI_LEANEXPLORE_URL")
    os.environ.update({
        "DAVINCI_RETRIEVE_BACKEND": "lean_explore",
        "DAVINCI_LEANEXPLORE_BACKEND": "remote",
        "DAVINCI_LEANEXPLORE_URL": args.url,
        "DAVINCI_LEANEXPLORE_EXPECTED_LEAN_VERSION": args.lean_version,
        "DAVINCI_LEANEXPLORE_EXPECTED_MATHLIB_COMMIT": args.mathlib_commit,
        "DAVINCI_LEANEXPLORE_EXPECTED_SNAPSHOT": args.snapshot,
        "DAVINCI_LEANEXPLORE_TIMEOUT": str(args.timeout),
    })
    os.environ.pop("DAVINCI_RETRIEVE_INDEX", None)

    from dvp.agent.result_projection import _compact
    from dvp.toolkits.retrieval.backend import get_cached_retriever
    from dvp.toolkits.retrieval.execution import lean_retrieve

    queries = [{"natural_language_query": q} for q in args.query] if args.query else [
        {"lean_name": "Nat.Prime.dvd_mul"},
        {"natural_language_query": "A continuous real-valued function on a compact set is bounded"},
    ]

    async def check():
        info = await get_cached_retriever().info()
        print(json.dumps({"server": args.url, "metadata": info}, ensure_ascii=False), flush=True)
        registry = SimpleNamespace(workspace=SimpleNamespace(target=None), retrieve_index=None)
        records = []
        for query in queries:
            started = time.monotonic()
            raw = await lean_retrieve(registry, {
                **query, "limit": args.limit, "include_theorem_proof": True, "include_def_proof": True,
            })
            hits = raw.get("results") or []
            if not raw.get("success") or not hits:
                raise RuntimeError(f"Remote retrieval failed: {raw.get('error', raw)}")
            if query.get("lean_name") and query["lean_name"] not in [hit["fullname"] for hit in hits]:
                raise RuntimeError("Exact-name retrieval did not find the requested declaration")
            visible = _compact(raw, tool_name="lean_retrieve")
            if not any(hit.get("semantic") for hit in visible.get("results", [])):
                raise RuntimeError("Model-visible retrieval lost semantic descriptions")
            if not any(hit.get("context_status") == "available" for hit in hits):
                raise RuntimeError("Remote source context is unavailable; check server source assets")
            if not any(hit.get("source_code") for hit in hits):
                raise RuntimeError("Requested proof/source text is missing")
            summary = {
                "query": query, "seconds": round(time.monotonic() - started, 2),
                "hits": len(hits), "visible_hits": len(visible["results"]),
                "names": [hit["fullname"] for hit in hits],
                "context_statuses": dict(Counter(hit.get("context_status") for hit in hits)),
                "source_revisions": sorted({hit.get("source_revision", "") for hit in hits}),
            }
            print(json.dumps(summary, ensure_ascii=False), flush=True)
            records.append({"summary": summary, "raw": raw, "model_visible": visible})
        # Remote clients need httpx and DVP only, never local inference dependencies.
        heavy = [name for name in ("lean_explore", "torch", "sentence_transformers", "faiss") if name in sys.modules]
        if heavy:
            raise RuntimeError(f"Remote client unexpectedly loaded inference dependencies: {heavy}")
        return {"status": "passed", "server": args.url, "metadata": info,
                "client_inference_modules": heavy, "queries": records}

    report = asyncio.run(check())
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Remote retrieval passed; versions, descriptions, source context and model-visible results verified.", flush=True)


if __name__ == "__main__":
    main()
