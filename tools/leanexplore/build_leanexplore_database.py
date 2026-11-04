#!/usr/bin/env python3
"""Extract a Mathlib 4.35 search database from a completed doc-gen4 workspace.

Uses an explicitly pinned LeanExplore source checkout; never installs packages,
changes the active search profile, or calls an LLM/embedding service.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import re
import sqlite3
import subprocess
import sys


EXTRACTOR_COMMIT = "17b9d6c87a2bf0dc4ea27de05da8a1c9965391b7"
MATHLIB_COMMIT = "56a005855ea17e0b0958f73ec801ae140b0a1679"
DOCGEN_COMMIT = "0e1ffe2e4ba5ecfdfaf15c65b8d3805dbb7134ae"
LEAN_COMMIT = "eb6cafb7290e8bd5f2937522d8d804f58f02bafc"
MODULE_ROOTS = {"Mathlib", "Batteries", "Init", "Lean", "Std"}


def git(path: Path, *arguments: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(path), *arguments], text=True
    ).strip()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class CaptureWarnings(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


async def extract(args: argparse.Namespace) -> None:
    source = args.extractor_source.resolve(strict=True)
    lean_root = args.packages_root.resolve(strict=True)
    workspace = lean_root / "mathlib"
    output = args.output_dir.resolve()
    database = output / "lean_explore.db"
    temporary = output / "extracting.db"
    if database.exists() or temporary.exists():
        raise FileExistsError(f"Refusing to overwrite an extraction: {output}")
    if git(source, "rev-parse", "HEAD") != EXTRACTOR_COMMIT:
        raise ValueError("Unexpected LeanExplore extractor commit")
    if git(source, "status", "--porcelain", "--untracked-files=no"):
        raise ValueError("Extractor checkout has modified tracked files")
    if (workspace / "lean-toolchain").read_text().strip() != "leanprover/lean4:v4.35.0":
        raise ValueError("The extraction workspace must use Lean 4.35.0")
    packages = workspace / ".lake/packages"
    for name, expected in (("mathlib", MATHLIB_COMMIT), ("doc-gen4", DOCGEN_COMMIT)):
        path = packages / name
        if git(path, "rev-parse", "HEAD") != expected:
            raise ValueError(f"Unexpected {name} revision")
        if git(path, "status", "--porcelain", "--untracked-files=no"):
            raise ValueError(f"{name} has modified tracked files")
    if git(packages / "batteries", "status", "--porcelain", "--untracked-files=no"):
        raise ValueError("Batteries has modified tracked files")
    build_status = json.loads(args.build_status.read_text())
    if build_status.get("success") is not True:
        raise ValueError("doc-gen4 has not completed successfully")
    raw_database = workspace / ".lake/build/api-docs.db"
    if not raw_database.is_file():
        raise FileNotFoundError(raw_database)
    raw_wal = raw_database.with_name(raw_database.name + "-wal")
    if raw_wal.exists() and raw_wal.stat().st_size:
        raise ValueError("Checkpoint and close the doc-gen4 database before extraction")
    raw_digest = sha256(raw_database)
    with sqlite3.connect(raw_database.as_uri() + "?mode=ro", uri=True) as connection:
        schema_meta = dict(connection.execute("SELECT key, value FROM schema_meta"))
    output.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(source / "src"))
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from lean_explore.models import Base
    from lean_explore.extract.docgen.parser import _extract_package
    from lean_explore.extract.docgen.lean_source import build_package_cache
    from lean_explore.extract.docgen.projection_filter import filter_auto_generated_projections
    from lean_explore.extract.docgen.writer import insert_declarations_batch

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(output / "extraction.log")],
    )
    warnings = CaptureWarnings()
    logging.getLogger("lean_explore").addHandler(warnings)
    toolchain_source = build_package_cache(lean_root, "mathlib").get("lean4")
    if toolchain_source is None:
        raise ValueError("The matching Lean toolchain source directory is missing")
    lean_executable = toolchain_source.parent.parent / "bin/lean"
    lean_version = subprocess.check_output(
        [str(lean_executable), "--version"], text=True
    ).strip()
    if "version 4.35.0," not in lean_version or LEAN_COMMIT not in lean_version:
        raise ValueError(f"Unexpected source toolchain: {lean_version}")
    declarations = _extract_package(lean_root, "mathlib")
    raw_count = len(declarations)
    declarations, projection_count = filter_auto_generated_projections(declarations)
    if not declarations:
        raise ValueError("No declarations were extracted")
    if any("Could not extract source text" in item for item in warnings.messages):
        raise ValueError("Some source files could not be read; inspect extraction.log")
    link_revisions: Counter[str] = Counter()
    batteries_commit = git(packages / "batteries", "rev-parse", "HEAD")
    for declaration in declarations:
        root = declaration.module.split(".")[0]
        if root not in MODULE_ROOTS or not declaration.source_text.strip():
            raise ValueError(f"Invalid extracted declaration: {declaration.name}")
        match = re.search(r"github\.com/([^/]+/[^/]+)/blob/([^/]+)/", declaration.source_link)
        if not match:
            raise ValueError(f"Unpinned source link: {declaration.source_link}")
        repository, revision = match.groups()
        if root == "Mathlib" and (repository, revision) != (
            "leanprover-community/mathlib4", MATHLIB_COMMIT
        ):
            raise ValueError(f"Mathlib source version mismatch: {declaration.source_link}")
        if root in {"Init", "Lean", "Std"} and (
            repository != "leanprover/lean4" or revision not in {"v4.35.0", LEAN_COMMIT}
        ):
            raise ValueError(f"Lean source version mismatch: {declaration.source_link}")
        if root == "Batteries" and (
            repository != "leanprover-community/batteries" or revision != batteries_commit
        ):
            raise ValueError(f"Batteries source version mismatch: {declaration.source_link}")
        link_revisions[f"{repository}@{revision}"] += 1
    engine = create_async_engine(f"sqlite+aiosqlite:///{temporary}")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine) as session:
            inserted = await insert_declarations_batch(session, declarations)
    finally:
        await engine.dispose()
    with sqlite3.connect(temporary.as_uri() + "?mode=ro", uri=True) as connection:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Extracted database integrity check failed")
    if sha256(raw_database) != raw_digest:
        raise ValueError("doc-gen4 database changed during extraction")
    temporary.rename(database)
    manifest = {
        "status": "declarations_extracted",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "lean_version": "4.35.0",
        "lean_executable": str(lean_executable),
        "lean_executable_version": lean_version,
        "lean_source_directory": str(toolchain_source),
        "mathlib_commit": MATHLIB_COMMIT,
        "docgen_commit": DOCGEN_COMMIT,
        "extractor_commit": EXTRACTOR_COMMIT,
        "workspace": str(workspace),
        "module_roots": sorted(MODULE_ROOTS),
        "parsed_before_projection_filter": raw_count,
        "filtered_projections": projection_count,
        "inserted_declarations": inserted,
        "duplicate_names_skipped": len(declarations) - inserted,
        "source_revisions": dict(link_revisions),
        "raw_docgen_sha256": raw_digest,
        "raw_schema_meta": schema_meta,
        "raw_search_database_sha256": sha256(database),
        "extraction_script_sha256": sha256(Path(__file__)),
        "warnings": warnings.messages,
        "dependency_scope": "Upstream name_info.type references; excludes binder-only and proof-body dependencies.",
        "llm_calls": 0,
        "embedding_model_calls": 0,
        "active_profile_changed": False,
    }
    (output / "extraction-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extractor-source", required=True, type=Path)
    parser.add_argument("--packages-root", required=True, type=Path)
    parser.add_argument("--build-status", required=True, type=Path,
                        help="doc-gen4 completion report: a JSON object containing success: true.")
    parser.add_argument("--output-dir", required=True, type=Path)
    asyncio.run(extract(parser.parse_args()))


if __name__ == "__main__":
    main()
