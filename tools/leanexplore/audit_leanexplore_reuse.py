#!/usr/bin/env python3
"""Audit LeanExplore cross-version reuse without calling any model or service.

By default both databases are opened read-only. --apply-cache fills only NULL
informalizations/embeddings in the new database, in one transaction. The old
database is always read-only. Model names are supplied provenance: LeanExplore's
declarations table does not itself record the embedding model.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any


DEFAULT_EMBEDDING_MODEL = "Qwen/Qwen3-Embedding-0.6B"
REQUIRED_COLUMNS = {
    "id", "name", "module", "docstring", "source_text", "source_link",
    "dependencies", "informalization", "informalization_embedding",
}


def normalized_dependencies(value: str | None) -> str:
    if value is None:
        return "null"
    try:
        decoded = json.loads(value)
    except (ValueError, TypeError):
        return value
    if isinstance(decoded, list):
        decoded = sorted(decoded, key=lambda item: json.dumps(item, sort_keys=True))
    return json.dumps(decoded, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: str | None) -> str | None:
    return hashlib.sha256(value.encode("utf-8")).hexdigest() if value is not None else None


def module_root(module: str) -> str:
    return module.split(".", 1)[0]


def write_jsonl(connection: sqlite3.Connection, query: str, path: Path) -> None:
    with path.open("w", encoding="utf-8") as stream:
        for row in connection.execute(query):
            stream.write(json.dumps(dict(row), ensure_ascii=False) + "\n")


def build_matches(connection: sqlite3.Connection, same_embedding_model: bool) -> None:
    # Only names and IDs are indexed; neither source text nor vector blobs are
    # copied into Python. The extra tables/indexes live in SQLite's temp database.
    connection.executescript("""
        CREATE TEMP TABLE old_by_name AS SELECT id, name FROM old.declarations;
        CREATE INDEX temp.old_by_name_name ON old_by_name(name);
        CREATE TEMP TABLE new_names AS SELECT DISTINCT name FROM scoped_new;
        CREATE UNIQUE INDEX temp.new_names_name ON new_names(name);
        CREATE TEMP TABLE matches AS
        SELECT n.id, n.name, module_root(n.module) AS package,
               MIN(CASE WHEN scope.root IS NOT NULL THEN o.id END) AS same_name_id,
               MIN(CASE WHEN scope.root IS NOT NULL AND o.source_text = n.source_text
                        THEN o.id END) AS same_source_id,
               MIN(CASE WHEN o.source_text = n.source_text
                             AND o.informalization IS NOT NULL THEN o.id END) AS description_id,
               MAX(CASE WHEN scope.root IS NOT NULL AND o.source_text = n.source_text
                             AND o.docstring IS n.docstring
                             AND normalized_dependencies(o.dependencies)
                                 = normalized_dependencies(n.dependencies)
                        THEN 1 ELSE 0 END) AS strict_context_match,
               n.informalization IS NOT NULL AS description_present,
               n.informalization_embedding IS NOT NULL AS embedding_present
        FROM scoped_new n
        LEFT JOIN old_by_name k ON k.name = n.name
        LEFT JOIN old.declarations o ON o.id = k.id
        LEFT JOIN comparison_roots scope ON scope.root = module_root(o.module)
        GROUP BY n.id;
        CREATE UNIQUE INDEX temp.matches_id ON matches(id);
        ALTER TABLE matches ADD COLUMN embedding_id INTEGER;
        ALTER TABLE matches ADD COLUMN description_cache_outside_scope INTEGER DEFAULT 0;
        ALTER TABLE matches ADD COLUMN description_cache_other_module_root INTEGER DEFAULT 0;
        UPDATE matches SET
            description_cache_outside_scope = description_id IS NOT NULL AND (
                SELECT module_root(o.module) NOT IN (SELECT root FROM comparison_roots)
                FROM old.declarations o WHERE o.id = matches.description_id
            ),
            description_cache_other_module_root = description_id IS NOT NULL AND (
                SELECT module_root(o.module) <> matches.package
                FROM old.declarations o WHERE o.id = matches.description_id
            );
    """)
    if not same_embedding_model:
        return
    # Upstream embedding cache is keyed by the entire description. Hashes make
    # the lookup small; exact equality below also checks against hash collisions.
    connection.executescript("""
        CREATE TEMP TABLE old_embeddings AS
            SELECT id, text_sha256(informalization) AS description_hash
            FROM old.declarations
            WHERE informalization IS NOT NULL
              AND informalization_embedding IS NOT NULL;
        CREATE INDEX temp.old_embeddings_hash ON old_embeddings(description_hash);
        UPDATE matches SET embedding_id = (
            SELECT MIN(e.id)
            FROM main.declarations n
            LEFT JOIN old.declarations d ON d.id = matches.description_id
            JOIN old_embeddings e
              ON e.description_hash = text_sha256(COALESCE(n.informalization, d.informalization))
            JOIN old.declarations o ON o.id = e.id
            WHERE n.id = matches.id
              AND COALESCE(n.informalization, d.informalization) <> ''
              AND o.informalization = COALESCE(n.informalization, d.informalization)
        );
    """)


COUNT_FIELDS = """
    COUNT(*) AS new_total,
    COUNT(DISTINCT name) AS new_unique_names,
    SUM(same_name_id IS NULL) AS new_name_rows,
    COUNT(DISTINCT CASE WHEN same_name_id IS NULL THEN name END) AS new_names,
    SUM(same_source_id IS NOT NULL) AS same_name_and_source,
    SUM(same_name_id IS NOT NULL AND same_source_id IS NULL) AS same_name_changed_source,
    SUM(same_source_id IS NOT NULL AND description_id IS NULL) AS missing_old_descriptions,
    SUM(description_id IS NOT NULL) AS description_cache_matches,
    SUM(description_cache_outside_scope) AS description_cache_outside_scope,
    SUM(description_cache_other_module_root) AS description_cache_other_module_root,
    SUM(NOT description_present AND description_id IS NOT NULL) AS reusable_descriptions,
    SUM(description_present) AS existing_descriptions,
    SUM(NOT description_present AND description_id IS NULL) AS upstream_llm_misses,
    SUM(strict_context_match) AS strict_context_matches,
    SUM(same_source_id IS NOT NULL AND NOT strict_context_match) AS same_source_context_changed,
    SUM(embedding_present) AS existing_embeddings,
    SUM(NOT embedding_present AND embedding_id IS NOT NULL) AS reusable_embeddings
"""


def collect_counts(connection: sqlite3.Connection) -> dict[str, Any]:
    overall = dict(connection.execute(f"SELECT {COUNT_FIELDS} FROM matches").fetchone())
    overall = {key: value or 0 for key, value in overall.items()}
    groups = {
        row["package"]: {key: value or 0 for key, value in dict(row).items() if key != "package"}
        for row in connection.execute(f"SELECT package, {COUNT_FIELDS} FROM matches GROUP BY package")
    }
    # Unique missing keys describe possible deduplication, not upstream API-call
    # batching: upstream normally processes every missing declaration row.
    for row in connection.execute("""
        SELECT m.package, COUNT(*) AS count FROM (
            SELECT DISTINCT m.package, n.name, n.source_text
            FROM matches m JOIN main.declarations n ON n.id = m.id
            WHERE NOT m.description_present AND m.description_id IS NULL
        ) m GROUP BY m.package
    """):
        groups[row["package"]]["unique_missing_name_source_pairs"] = row["count"]
    overall["unique_missing_name_source_pairs"] = connection.execute("""
        SELECT COUNT(*) FROM (
            SELECT DISTINCT n.name, n.source_text
            FROM matches m JOIN main.declarations n ON n.id = m.id
            WHERE NOT m.description_present AND m.description_id IS NULL
        )
    """).fetchone()[0]
    old_counts = connection.execute("""
        SELECT module_root(o.module) AS package,
               COUNT(*) AS old_total, COUNT(DISTINCT o.name) AS old_unique_names,
               SUM(n.name IS NULL) AS removed_name_rows,
               COUNT(DISTINCT CASE WHEN n.name IS NULL THEN o.name END) AS removed_names
        FROM scoped_old o LEFT JOIN new_names n ON n.name = o.name
        GROUP BY package
    """).fetchall()
    for row in old_counts:
        group = groups.setdefault(row["package"], {key: 0 for key in overall})
        group.update({key: value for key, value in dict(row).items() if key != "package"})
    for group in groups.values():
        for key in ("old_total", "old_unique_names", "removed_name_rows", "removed_names",
                    "unique_missing_name_source_pairs"):
            group.setdefault(key, 0)
    overall["old_total"], overall["old_unique_names"] = connection.execute(
        "SELECT COUNT(*), COUNT(DISTINCT name) FROM scoped_old"
    ).fetchone()
    overall["removed_name_rows"], overall["removed_names"] = connection.execute("""
        SELECT COUNT(*), COUNT(DISTINCT o.name)
        FROM scoped_old o LEFT JOIN new_names n ON n.name = o.name
        WHERE n.name IS NULL
    """).fetchone()
    return {"overall": overall, "by_module_root": dict(sorted(groups.items()))}


def audit(
    old_db: Path,
    new_db: Path,
    output_dir: Path,
    *,
    apply_cache: bool = False,
    old_embedding_model: str = DEFAULT_EMBEDDING_MODEL,
    new_embedding_model: str = DEFAULT_EMBEDDING_MODEL,
    module_roots: list[str] | None = None,
) -> dict[str, Any]:
    old_db, new_db = old_db.resolve(strict=True), new_db.resolve(strict=True)
    if old_db.samefile(new_db):
        raise ValueError("Old and new databases must be different files.")
    output_dir.mkdir(parents=True, exist_ok=True)
    mode = "rw" if apply_cache else "ro"
    connection = sqlite3.connect(new_db.as_uri() + f"?mode={mode}", uri=True)
    connection.row_factory = sqlite3.Row
    connection.create_function("normalized_dependencies", 1, normalized_dependencies, deterministic=True)
    connection.create_function("text_sha256", 1, digest, deterministic=True)
    connection.create_function("module_root", 1, lambda value: module_root(value) if value else None, deterministic=True)
    try:
        connection.execute("ATTACH DATABASE ? AS old", (old_db.as_uri() + "?mode=ro",))
        for schema in ("main", "old"):
            columns = {row["name"] for row in connection.execute(f"PRAGMA {schema}.table_info(declarations)")}
            if missing := REQUIRED_COLUMNS - columns:
                raise ValueError(f"{schema} database is missing columns: {sorted(missing)}")
        roots = sorted(set(module_roots if module_roots is not None else (
            row[0] for row in connection.execute("SELECT DISTINCT module_root(module) FROM main.declarations")
        )))
        if any(not root or "." in root for root in roots):
            raise ValueError("--module-root must be a nonempty module root, e.g. Mathlib.")
        connection.execute("CREATE TEMP TABLE comparison_roots (root TEXT PRIMARY KEY)")
        connection.executemany("INSERT INTO comparison_roots VALUES (?)", ((root,) for root in roots))
        connection.executescript("""
            CREATE TEMP VIEW scoped_old AS
                SELECT * FROM old.declarations WHERE module_root(module) IN (SELECT root FROM comparison_roots);
            CREATE TEMP VIEW scoped_new AS
                SELECT * FROM main.declarations WHERE module_root(module) IN (SELECT root FROM comparison_roots);
        """)
        excluded = {
            row["root"]: row["count"] for row in connection.execute("""
                SELECT module_root(module) AS root, COUNT(*) AS count FROM old.declarations
                WHERE module_root(module) NOT IN (SELECT root FROM comparison_roots) GROUP BY root
            """)
        }
        same_model = old_embedding_model == new_embedding_model
        build_matches(connection, same_model)
        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "old_db": str(old_db), "new_db": str(new_db),
            "apply_cache": apply_cache,
            "comparison_scope": {
                "module_roots": roots,
                "selection": "explicit" if module_roots is not None else "module roots present in new database",
                "old_excluded_module_roots": excluded,
                "old_excluded_count": sum(excluded.values()),
                "new_excluded_count": connection.execute("""
                    SELECT COUNT(*) FROM main.declarations
                    WHERE module_root(module) NOT IN (SELECT root FROM comparison_roots)
                """).fetchone()[0],
                "cache_scope": "All old declarations; cross-root description matches are counted separately.",
            },
            "embedding_models": {
                "old": old_embedding_model, "new": new_embedding_model,
                "equal": same_model,
                "provenance": "Caller-supplied; declaration rows do not record the model.",
            },
            "matching_rules": {
                "informalization": "Exact (name, source_text); first old non-NULL description by id wins.",
                "embedding": "Exact description text and identical declared model; empty descriptions skipped.",
                "strict_diagnostic": "Same name/source plus equal docstring and sorted dependency list.",
                "limitation": "Matching text or dependency names does not prove unchanged dependency semantics.",
            },
            **collect_counts(connection),
        }
        write_jsonl(connection, """
            SELECT n.id, n.name, n.module, n.source_text, n.docstring,
                   n.source_link, n.dependencies,
                   CASE WHEN m.same_name_id IS NULL THEN 'new_name'
                        WHEN m.same_source_id IS NULL THEN 'changed_source'
                        ELSE 'old_description_missing' END AS reason
            FROM matches m JOIN main.declarations n ON n.id = m.id
            WHERE NOT m.description_present AND m.description_id IS NULL ORDER BY n.id
        """, output_dir / "llm_pending.jsonl")
        write_jsonl(connection, """
            SELECT n.id, n.name, n.module, text_sha256(n.source_text) AS source_sha256,
                   CASE WHEN m.same_name_id IS NULL THEN 'new_name'
                        WHEN m.same_source_id IS NULL THEN 'changed_source'
                        ELSE 'docstring_or_dependencies_changed' END AS reason
            FROM matches m JOIN main.declarations n ON n.id = m.id
            WHERE m.same_source_id IS NULL OR NOT m.strict_context_match ORDER BY n.id
        """, output_dir / "changes.jsonl")
        write_jsonl(connection, """
            SELECT o.id, o.name, o.module FROM scoped_old o
            LEFT JOIN new_names n ON n.name = o.name WHERE n.name IS NULL ORDER BY o.id
        """, output_dir / "removed_names.jsonl")
        applied = {"descriptions": 0, "embeddings": 0}
        if apply_cache:
            with connection:
                applied["descriptions"] = connection.execute("""
                    UPDATE main.declarations SET informalization = (
                        SELECT o.informalization FROM matches m
                        JOIN old.declarations o ON o.id = m.description_id
                        WHERE m.id = main.declarations.id
                    ) WHERE informalization IS NULL
                        AND id IN (SELECT id FROM matches WHERE description_id IS NOT NULL)
                """).rowcount
                applied["embeddings"] = connection.execute("""
                    UPDATE main.declarations SET informalization_embedding = (
                        SELECT o.informalization_embedding FROM matches m
                        JOIN old.declarations o ON o.id = m.embedding_id
                        WHERE m.id = main.declarations.id
                    ) WHERE informalization_embedding IS NULL
                        AND id IN (SELECT id FROM matches WHERE embedding_id IS NOT NULL)
                """).rowcount
        report["applied"] = applied
        report["database_after"] = dict(connection.execute("""
            SELECT COUNT(*) AS total, COUNT(informalization) AS descriptions,
                   COUNT(informalization_embedding) AS embeddings,
                   SUM(informalization IS NULL) AS descriptions_missing
            FROM main.declarations
        """).fetchone())
        (output_dir / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return report
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-db", required=True, type=Path)
    parser.add_argument("--new-db", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--apply-cache", action="store_true")
    parser.add_argument("--old-embedding-model", default=DEFAULT_EMBEDDING_MODEL)
    parser.add_argument("--new-embedding-model", default=DEFAULT_EMBEDDING_MODEL)
    parser.add_argument("--module-root", dest="module_roots", action="append",
                        help="Compare this module root; repeat as needed. Default: roots in the new database.")
    args = parser.parse_args()
    report = audit(**vars(args))
    print(json.dumps({
        "report": str(args.output_dir / "report.json"),
        "overall": report["overall"], "applied": report["applied"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
