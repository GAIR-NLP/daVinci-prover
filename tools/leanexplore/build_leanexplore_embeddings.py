#!/usr/bin/env python3
"""Fill missing embeddings with the pinned official LeanExplore pipeline.

Uses the verified local Qwen3-Embedding-0.6B model without network access.
Only missing vectors are filled; the official cache is keyed by the exact
informalization text. Run after all descriptions have been generated and
before build_leanexplore_indices.py.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
import sys
import time

from build_leanexplore_indices import (
    EXTRACTOR_COMMIT, file_sha256, import_builder, no_network,
)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODEL = ROOT / "data/leanexplore-models/Qwen3-Embedding-0.6B"
DEFAULT_MANIFEST = ROOT / "data/leanexplore-models/manifest.json"
MODEL_ID = "Qwen/Qwen3-Embedding-0.6B"
MODEL_REVISION = "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"


def check_model(model: Path, manifest_path: Path) -> dict:
    manifest = json.loads(manifest_path.read_text())
    entry = next(item for item in manifest["models"] if item["repo_id"] == MODEL_ID)
    if entry["revision"] != MODEL_REVISION:
        raise ValueError(f"Expected {MODEL_ID}@{MODEL_REVISION}.")
    for item in entry["files"]:
        if file_sha256(model / item["path"]) != item["sha256"]:
            raise ValueError(f"Model checksum mismatch: {item['path']}")
    return {
        "model_id": MODEL_ID, "revision": MODEL_REVISION, "path": str(model),
        "all_manifest_checksums_verified": True,
    }


def state(database: Path, original_vector_ids: set[int] | None = None) -> dict:
    """Audit vectors in bounded batches and fingerprint immutable inputs."""
    import numpy as np

    descriptions = hashlib.sha256()
    original_vectors = hashlib.sha256()
    vector_ids = set()
    max_norm_error = 0.0
    total = 0
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        for row in connection.execute(
            "SELECT id, informalization, informalization_embedding "
            "FROM declarations ORDER BY id"
        ):
            total += 1
            identity, text, blob = row
            if not text or not text.strip():
                raise ValueError(f"Declaration {identity} lacks a nonempty description.")
            text_bytes = text.encode("utf-8")
            descriptions.update(identity.to_bytes(8, "big"))
            descriptions.update(len(text_bytes).to_bytes(8, "big"))
            descriptions.update(text_bytes)
            if blob is None:
                continue
            vector_ids.add(identity)
            vector = np.frombuffer(blob, dtype=np.float32)
            if vector.shape != (1024,) or not np.isfinite(vector).all():
                raise ValueError(f"Invalid vector at declaration {identity}.")
            norm_error = abs(float(np.linalg.norm(vector)) - 1)
            max_norm_error = max(max_norm_error, norm_error)
            if norm_error > 0.01:
                raise ValueError(f"Vector at declaration {identity} is not normalized.")
            if original_vector_ids is None or identity in original_vector_ids:
                original_vectors.update(identity.to_bytes(8, "big"))
                original_vectors.update(blob)
    if not total:
        raise ValueError("The database is empty.")
    return {
        "declarations": total,
        "descriptions": total,
        "vectors": len(vector_ids),
        "missing_vectors": total - len(vector_ids),
        "max_abs_unit_norm_difference": max_norm_error,
        "description_fingerprint": descriptions.hexdigest(),
        "original_vectors_fingerprint": original_vectors.hexdigest(),
        "_vector_ids": vector_ids,
    }


async def call_official(
    database: Path, model: Path, caches: list[Path], batch_size: int, extractor_root: Path,
) -> dict:
    from sqlalchemy import URL
    from sqlalchemy.ext.asyncio import create_async_engine
    from lean_explore.extract import embeddings

    if not Path(embeddings.__file__).resolve().is_relative_to(extractor_root / "src"):
        raise RuntimeError("Embedding module did not come from the pinned upstream source.")
    original_discovery = embeddings.discover_database_files
    original_apply = embeddings._apply_cache_to_declarations
    original_process_batch = embeddings._process_batch
    started = time.perf_counter()
    observed = {
        "cache_hits": 0, "generation_required": 0,
        "generated_committed": 0, "batch_count": 0,
    }

    def write_progress():
        elapsed = time.perf_counter() - started
        progress = {
            "at": datetime.now(timezone.utc).isoformat(),
            **observed,
            "elapsed_seconds": elapsed,
            "committed_per_second": observed["generated_committed"] / elapsed if elapsed else 0,
        }
        path = database.parent / "embedding-progress.json"
        temporary = path.with_suffix(path.suffix + ".tmp")
        try:
            temporary.write_text(json.dumps(progress, ensure_ascii=False, indent=2) + "\n")
            temporary.replace(path)
        except OSError:
            logging.getLogger(__name__).warning("Could not update embedding progress", exc_info=True)

    async def record_cache(*args, **kwargs):
        result = await original_apply(*args, **kwargs)
        observed.update(cache_hits=result[0], generation_required=len(result[1]))
        write_progress()
        return result

    async def record_batch(*args, **kwargs):
        # The unmodified upstream function commits before returning its count.
        result = await original_process_batch(*args, **kwargs)
        observed["generated_committed"] += result
        observed["batch_count"] += 1
        write_progress()
        return result

    embeddings.discover_database_files = lambda: caches
    embeddings._apply_cache_to_declarations = record_cache
    embeddings._process_batch = record_batch
    engine = create_async_engine(URL.create("sqlite+aiosqlite", database=str(database)))
    try:
        await embeddings.generate_embeddings(
            engine, model_name=str(model), batch_size=batch_size, max_seq_length=512,
        )
    finally:
        embeddings.discover_database_files = original_discovery
        embeddings._apply_cache_to_declarations = original_apply
        embeddings._process_batch = original_process_batch
        await engine.dispose()
    return observed


def build_embeddings(
    database: Path, *, extractor_root: Path,
    model: Path = DEFAULT_MODEL, model_manifest: Path = DEFAULT_MANIFEST,
    cache_databases: list[Path] | None = None, batch_size: int = 128, threads: int = 16,
    report_path: Path | None = None,
) -> dict:
    if batch_size < 1 or threads < 1:
        raise ValueError("Batch size and threads must be positive.")
    database = database.resolve(strict=True)
    extractor_root = extractor_root.resolve(strict=True)
    model = model.resolve(strict=True)
    caches = [path.resolve(strict=True) for path in (cache_databases or [])]
    if database in caches:
        raise ValueError("Use separate completed databases for the official embedding cache.")
    wal = database.with_name(database.name + "-wal")
    if wal.exists() and wal.stat().st_size:
        raise ValueError("Close/checkpoint the description writer before generating embeddings.")
    os.environ.update(
        OMP_NUM_THREADS=str(threads), MKL_NUM_THREADS=str(threads),
        HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", TOKENIZERS_PARALLELISM="false",
    )
    if "LEAN_EXPLORE_EMBEDDING_BATCH_SIZE" in os.environ:
        raise ValueError("Unset LEAN_EXPLORE_EMBEDDING_BATCH_SIZE to retain official encode batch 8.")
    model_report = check_model(model, model_manifest.resolve(strict=True))
    before = state(database)
    vector_ids = before.pop("_vector_ids")
    with no_network():
        import_builder(extractor_root)
        import torch

        torch.set_num_threads(threads)
        start = time.perf_counter()
        observed = asyncio.run(call_official(database, model, caches, batch_size, extractor_root))
        seconds = time.perf_counter() - start
    after = state(database, vector_ids)
    after.pop("_vector_ids")
    if after["missing_vectors"]:
        raise RuntimeError(f"Official embedding stage left {after['missing_vectors']} missing vectors.")
    for key in ("declarations", "description_fingerprint", "original_vectors_fingerprint"):
        if before[key] != after[key]:
            raise RuntimeError(f"Embedding generation unexpectedly modified {key}.")
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "database": str(database), "extractor_root": str(extractor_root),
        "extractor_commit": EXTRACTOR_COMMIT,
        "model": model_report, "cache_databases": list(map(str, caches)),
        "configuration": {
            "max_seq_length": 512, "pipeline_batch_size": batch_size,
            "encode_batch_size": 8, "threads": threads,
            "device": "cuda" if torch.cuda.is_available() else "cpu",
            "document_prompt": None,
            "normalization": "Official SentenceTransformer Normalize module",
        },
        "before": before, "after": after, "official_cache_application": observed,
        "elapsed_seconds": seconds,
        "newly_filled_vectors": after["vectors"] - before["vectors"],
        "all_existing_vectors_unchanged": True, "all_descriptions_unchanged": True,
        "status": "complete", "profile_activated": False,
    }
    destination = report_path or database.parent / "embedding-build-report.json"
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--extractor-root", required=True, type=Path,
                        help="Clean LeanExplore source checkout at the pinned extractor commit.")
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--model-manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--cache-database", type=Path, action="append", dest="cache_databases",
                        help="Optional completed database for exact-text embedding reuse; repeatable. No cache is required by default.")
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--threads", type=int, default=16)
    parser.add_argument("--report-path", type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    report = build_embeddings(**vars(args))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
