#!/usr/bin/env python3
"""Build indexes for an explicit, already-populated LeanExplore database.

Calls only the pinned upstream BM25/FAISS builders. No model is loaded, no
network is used, and no LeanExplore profile is activated. FAISS coverage is
limited to rows that already have embeddings; missing vectors remain missing.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
from typing import Any


EXTRACTOR_COMMIT = "17b9d6c87a2bf0dc4ea27de05da8a1c9965391b7"
ARTIFACTS = (
    "bm25_name_spaced", "bm25_name_raw", "bm25_ids_map.json",
    "informalization_faiss.index", "informalization_faiss_ids_map.json",
)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@contextmanager
def no_network():
    """Block IP sockets; asyncio's local socket pair remains usable."""
    original_connect, original_connect_ex = socket.socket.connect, socket.socket.connect_ex
    original_lookup = socket.getaddrinfo

    def connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            raise RuntimeError("Network access is disabled during index construction.")
        return original_connect(sock, address)

    def connect_ex(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            raise RuntimeError("Network access is disabled during index construction.")
        return original_connect_ex(sock, address)

    def lookup(*args, **kwargs):
        raise RuntimeError("DNS access is disabled during index construction.")

    socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo = connect, connect_ex, lookup
    try:
        yield
    finally:
        socket.socket.connect, socket.socket.connect_ex = original_connect, original_connect_ex
        socket.getaddrinfo = original_lookup


def import_builder(extractor_root: Path):
    extractor_root = extractor_root.resolve(strict=True)
    revision = subprocess.check_output(
        ["git", "-C", str(extractor_root), "rev-parse", "HEAD"], text=True
    ).strip()
    if revision != EXTRACTOR_COMMIT:
        raise ValueError(f"Expected extractor {EXTRACTOR_COMMIT}, got {revision}.")
    changed = subprocess.check_output(
        ["git", "-C", str(extractor_root), "status", "--porcelain", "--", "src"], text=True
    ).strip()
    if changed:
        raise ValueError("The pinned extractor src directory has local changes.")
    sys.path.insert(0, str(extractor_root / "src"))
    from lean_explore.extract import index

    if not Path(index.__file__).resolve().is_relative_to(extractor_root / "src"):
        raise RuntimeError("A different lean_explore module was already imported; run in a fresh Python process.")
    return index


def database_summary(db: Path, dimension: int) -> dict[str, Any]:
    with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        summary = dict(connection.execute("""
            SELECT COUNT(*) AS declarations,
                   COUNT(informalization) AS descriptions,
                   COUNT(informalization_embedding) AS vectors,
                   SUM(informalization IS NULL) AS missing_descriptions,
                   SUM(informalization_embedding IS NULL) AS missing_vectors,
                   SUM(informalization_embedding IS NOT NULL AND
                       (informalization IS NULL OR informalization = '')) AS vectors_without_description
            FROM declarations
        """).fetchone())
        lengths = {row[0]: row[1] for row in connection.execute("""
            SELECT length(informalization_embedding), COUNT(*) FROM declarations
            WHERE informalization_embedding IS NOT NULL GROUP BY length(informalization_embedding)
        """)}
    if not summary["declarations"]:
        raise ValueError("The database is empty.")
    if summary["vectors_without_description"]:
        raise ValueError("Some vectors lack a nonempty description; repair this database before indexing.")
    if lengths != {dimension * 4: summary["vectors"]}:
        raise ValueError(f"Expected float32 vectors of dimension {dimension}; byte lengths are {lengths}.")
    if summary["vectors"] < 256:
        raise ValueError("The pinned upstream IVF builder needs at least 256 existing vectors.")
    summary["embedding_dimension"] = dimension
    summary["semantic_coverage_fraction"] = summary["vectors"] / summary["declarations"]
    summary["full_semantic_coverage"] = summary["vectors"] == summary["declarations"]
    return summary


def validate_indices(db: Path, directory: Path, dimension: int) -> dict[str, Any]:
    import bm25s
    import faiss
    import numpy as np
    from lean_explore.search.indexes import SearchIndexes

    lexical_ids = json.loads((directory / "bm25_ids_map.json").read_text())
    semantic_ids = json.loads((directory / "informalization_faiss_ids_map.json").read_text())
    with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) as connection:
        all_ids = {row[0] for row in connection.execute("SELECT id FROM declarations")}
        vector_ids = {row[0] for row in connection.execute(
            "SELECT id FROM declarations WHERE informalization_embedding IS NOT NULL"
        )}
        for label, mapping, expected in (
            ("BM25", lexical_ids, all_ids), ("FAISS", semantic_ids, vector_ids),
        ):
            if any(type(value) is not int for value in mapping):
                raise ValueError(f"{label} ID map contains a non-integer.")
            if len(mapping) != len(set(mapping)) or set(mapping) != expected:
                raise ValueError(f"{label} ID map is not a one-to-one mapping of the expected database rows.")
        lexical_counts = {}
        for name in ("bm25_name_spaced", "bm25_name_raw"):
            index = bm25s.BM25.load(str(directory / name), mmap=True)
            count = int(index.scores["num_docs"])
            if count != len(lexical_ids):
                raise ValueError(f"{name} has {count} documents for {len(lexical_ids)} IDs.")
            lexical_counts[name] = count
        semantic = faiss.read_index(str(directory / "informalization_faiss.index"))
        if semantic.ntotal != len(semantic_ids) or semantic.d != dimension or not semantic.is_trained:
            raise ValueError("FAISS dimensions, training state, or vector count does not match the database.")
        semantic.make_direct_map()
        positions = np.unique(np.linspace(0, len(semantic_ids) - 1, min(256, len(semantic_ids)), dtype=int))
        for position in positions:
            blob = connection.execute(
                "SELECT informalization_embedding FROM declarations WHERE id = ?", (semantic_ids[int(position)],)
            ).fetchone()[0]
            expected = np.frombuffer(blob, dtype=np.float32)
            if not np.isfinite(expected).all() or not np.array_equal(semantic.reconstruct(int(position)), expected):
                raise ValueError(f"FAISS position {position} does not contain its mapped declaration's vector.")
        # Exercise the pinned upstream loader and its disk format, without
        # creating SearchEngine (which initializes embedding/reranking models).
        # DVP's installed 1.3 SDK loads these same filenames directly in its
        # older SearchEngine; no installed package or profile is changed here.
        loader = SearchIndexes(data_directory=directory)
        name = connection.execute("SELECT name FROM declarations WHERE id = ?", (lexical_ids[0],)).fetchone()[0]
        name_hits = loader.name_candidates(name, k=min(10, len(lexical_ids)))
        if lexical_ids[0] not in name_hits:
            raise ValueError("The exact-name BM25 smoke check did not return its indexed declaration.")
        first_blob = connection.execute(
            "SELECT informalization_embedding FROM declarations WHERE id = ?", (semantic_ids[0],)
        ).fetchone()[0]
        semantic_hits = loader.semantic_candidates(
            np.frombuffer(first_blob, dtype=np.float32).tolist(), k=min(10, len(semantic_ids))
        )
        if not semantic_hits or not set(semantic_hits).issubset(vector_ids):
            raise ValueError("The cached-vector FAISS smoke check returned no valid mapped declarations.")
    return {
        "all_bm25_ids_match_database": True, "all_faiss_ids_match_embedded_rows": True,
        "bm25_documents": lexical_counts,
        "faiss_vectors": int(semantic.ntotal), "faiss_dimension": int(semantic.d),
        "sampled_vector_id_pairs_verified": len(positions),
        "loader": "lean_explore.search.indexes.SearchIndexes",
        "exact_name_smoke": {"query": name, "expected_id": lexical_ids[0], "returned_ids": list(name_hits)},
        "cached_vector_smoke_returned_ids": list(semantic_hits),
    }


async def run_builders(builder, db: Path, output_directory: Path) -> None:
    from sqlalchemy import URL
    from sqlalchemy.ext.asyncio import create_async_engine

    url = URL.create("sqlite+aiosqlite", database=db.as_uri(), query={"mode": "ro", "uri": "true"})
    engine = create_async_engine(url)
    try:
        await builder.build_bm25_indices(engine, output_directory=output_directory)
        await builder.build_faiss_indices(engine, output_directory=output_directory)
    finally:
        await engine.dispose()


def build(
    output_dir: Path, *, extractor_root: Path,
    embedding_dimension: int = 1024, threads: int = 16,
) -> dict[str, Any]:
    if threads < 1 or embedding_dimension < 1:
        raise ValueError("Thread count and embedding dimension must be positive.")
    output_dir = output_dir.resolve(strict=True)
    db = (output_dir / "lean_explore.db").resolve(strict=True)
    if db.parent != output_dir:
        raise ValueError("The database must be a regular file inside the explicit output directory.")
    wal = db.with_name(db.name + "-wal")
    if wal.exists() and wal.stat().st_size:
        raise ValueError("Checkpoint/close the database writer before building indexes; a nonempty WAL exists.")
    os.environ.update(OMP_NUM_THREADS=str(threads), HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    summary = database_summary(db, embedding_dimension)
    db_hash = file_sha256(db)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "database": str(db), "database_sha256": db_hash,
        "extractor_root": str(extractor_root.resolve()), "extractor_commit": EXTRACTOR_COMMIT,
        "database_coverage": summary,
        "status": "complete_semantic_index" if summary["full_semantic_coverage"] else "partial_semantic_index",
        "models_called": 0, "profile_activated": False,
        "semantic_scope": "Only existing vectors are indexed. Missing descriptions/vectors remain ungenerated.",
    }
    with no_network():
        builder = import_builder(extractor_root)
        import faiss

        faiss.omp_set_num_threads(threads)
        with tempfile.TemporaryDirectory(prefix=".indices-build-", dir=output_dir) as temporary:
            staging = Path(temporary)
            asyncio.run(run_builders(builder, db, staging))
            report["validation"] = validate_indices(db, staging, embedding_dimension)
            if file_sha256(db) != db_hash:
                raise RuntimeError("The database changed while indexing; indexes were not published.")
            for name in ARTIFACTS:
                target = output_dir / name
                if target.is_symlink() or target.is_file():
                    target.unlink()
                elif target.exists():
                    shutil.rmtree(target)
                os.replace(staging / name, target)
    report["artifacts"] = {
        str(path.relative_to(output_dir)): {"bytes": path.stat().st_size, "sha256": file_sha256(path)}
        for name in ARTIFACTS
        for path in ([output_dir / name] if (output_dir / name).is_file() else sorted((output_dir / name).rglob("*")))
        if path.is_file()
    }
    report_path = output_dir / "index-build-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path,
                        help="Existing directory containing lean_explore.db; indexes are rebuilt here.")
    parser.add_argument("--extractor-root", type=Path, required=True,
                        help="Clean LeanExplore source checkout at the pinned extractor commit.")
    parser.add_argument("--embedding-dimension", type=int, default=1024)
    parser.add_argument("--threads", type=int, default=16)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    report = build(**vars(args))
    print(json.dumps({
        "report": str(args.output_dir / "index-build-report.json"),
        "status": report["status"], "database_coverage": report["database_coverage"],
        "validation": report["validation"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
