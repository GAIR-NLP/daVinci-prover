"""Cross-version cache audit must not silently change declaration provenance."""

import importlib.util
from pathlib import Path
import sqlite3

import pytest


SCRIPT = Path(__file__).parents[2] / "tools/leanexplore/audit_leanexplore_reuse.py"
SPEC = importlib.util.spec_from_file_location("audit_leanexplore_reuse", SCRIPT)
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)

SCHEMA = """
    CREATE TABLE declarations (
        id INTEGER PRIMARY KEY, name TEXT NOT NULL, module TEXT NOT NULL,
        docstring TEXT, source_text TEXT NOT NULL, source_link TEXT NOT NULL,
        dependencies TEXT, informalization TEXT, informalization_embedding BLOB
    )
"""


def make_database(path, rows):
    with sqlite3.connect(path) as connection:
        connection.execute(SCHEMA)
        connection.executemany("INSERT INTO declarations VALUES (?,?,?,?,?,?,?,?,?)", rows)
    return path


@pytest.fixture
def databases(tmp_path):
    old = make_database(tmp_path / "old.db", [
        (1, "unchanged", "Mathlib.A", "doc", "source1", "old", '["B","A"]', "description1", b"v1"),
        (2, "changed", "Mathlib.B", None, "oldsource", "old", "[]", "description2", b"v2"),
        (3, "context", "Lean.C", "old doc", "source3", "old", "[]", "description3", b"v3"),
        (4, "missing", "Lean.D", None, "source4", "old", None, None, None),
        (5, "removed", "Init.E", None, "source5", "old", "[]", "shared description", b"v5"),
        (6, "existing", "Std.F", None, "source6", "old", "[]", "old description", b"v6"),
        (7, "empty", "Std.G", None, "source7", "old", "[]", "", b"v7"),
    ])
    new = make_database(tmp_path / "new.db", [
        (101, "unchanged", "Mathlib.A", "doc", "source1", "new", '["A","B"]', None, None),
        (102, "changed", "Mathlib.B", None, "newsource", "new", "[]", None, None),
        (103, "context", "Lean.C", "new doc", "source3", "new", "[]", None, None),
        (104, "missing", "Lean.D", None, "source4", "new", None, None, None),
        (105, "new", "Mathlib.New", None, "source8", "new", "[]", None, None),
        (106, "new", "Mathlib.New", None, "source8", "new", "[]", None, None),
        (107, "existing", "Std.F", None, "source6", "new", "[]", "shared description", None),
        (108, "preserved", "Other.H", None, "source9", "new", "[]", "already done", b"keep"),
        (109, "empty", "Std.G", None, "source7", "new", "[]", None, None),
    ])
    return old, new


def read_rows(path):
    with sqlite3.connect(path) as connection:
        return connection.execute("SELECT * FROM declarations ORDER BY id").fetchall()


def test_read_only_audit_counts_exact_and_context_changes(databases, tmp_path):
    old, new = databases
    before = old.read_bytes(), new.read_bytes()
    report = AUDIT.audit(
        old, new, tmp_path / "audit",
        module_roots=["Mathlib", "Lean", "Std", "Init", "Other"],
    )
    counts = report["overall"]
    assert counts["new_total"] == 9
    assert counts["old_total"] == 7
    assert counts["new_names"] == 2
    assert counts["new_name_rows"] == 3
    assert counts["removed_names"] == 1
    assert counts["same_name_and_source"] == 5
    assert counts["same_name_changed_source"] == 1
    assert counts["same_source_context_changed"] == 1
    assert counts["strict_context_matches"] == 4
    assert counts["reusable_descriptions"] == 3  # Includes upstream's empty string.
    assert counts["reusable_embeddings"] == 3  # Empty description is skipped.
    assert counts["upstream_llm_misses"] == 4
    assert counts["unique_missing_name_source_pairs"] == 3
    assert report["by_module_root"]["Mathlib"]["upstream_llm_misses"] == 3
    assert report["by_module_root"]["Init"]["new_total"] == 0
    assert len((tmp_path / "audit" / "llm_pending.jsonl").read_text().splitlines()) == 4
    assert before == (old.read_bytes(), new.read_bytes())


def test_apply_only_fills_nulls_and_retains_new_identity(databases, tmp_path):
    old, new = databases
    old_bytes = old.read_bytes()
    before = read_rows(new)
    report = AUDIT.audit(old, new, tmp_path / "apply", apply_cache=True)
    after = read_rows(new)
    assert [row[:7] for row in before] == [row[:7] for row in after]
    assert report["applied"] == {"descriptions": 3, "embeddings": 3}
    assert after[0][-2:] == ("description1", b"v1")
    assert after[2][-2:] == ("description3", b"v3")
    assert after[6][-2:] == ("shared description", b"v5")
    assert after[7][-2:] == ("already done", b"keep")
    assert after[8][-2:] == ("", None)
    assert old.read_bytes() == old_bytes
    repeated = AUDIT.audit(old, new, tmp_path / "repeat", apply_cache=True)
    assert repeated["applied"] == {"descriptions": 0, "embeddings": 0}
    assert repeated["overall"]["upstream_llm_misses"] == 4


def test_different_embedding_models_do_not_reuse_vectors(databases, tmp_path):
    old, new = databases
    report = AUDIT.audit(
        old, new, tmp_path / "different_model", apply_cache=True,
        new_embedding_model="another-model",
    )
    assert report["overall"]["reusable_embeddings"] == 0
    assert report["applied"] == {"descriptions": 3, "embeddings": 0}


def test_same_database_is_rejected_before_writing(databases, tmp_path):
    old, _ = databases
    with pytest.raises(ValueError, match="different files"):
        AUDIT.audit(old, old, tmp_path / "invalid", apply_cache=True)


def test_default_scope_does_not_count_unbuilt_packages_as_removed(databases, tmp_path):
    old, new = databases
    report = AUDIT.audit(old, new, tmp_path / "scope")
    assert report["overall"]["old_total"] == 6
    assert report["overall"]["removed_names"] == 0
    assert "Init" not in report["by_module_root"]
    assert report["comparison_scope"]["old_excluded_module_roots"] == {"Init": 1}
    assert report["comparison_scope"]["old_excluded_count"] == 1
    assert report["overall"]["reusable_embeddings"] == 3  # Cache still includes Init.


def test_explicit_scope_leaves_other_new_packages_untouched(databases, tmp_path):
    old, new = databases
    report = AUDIT.audit(
        old, new, tmp_path / "mathlib_only", apply_cache=True,
        module_roots=["Mathlib"],
    )
    assert report["overall"]["old_total"] == 2
    assert report["overall"]["new_total"] == 4
    assert report["comparison_scope"]["new_excluded_count"] == 5
    assert report["applied"] == {"descriptions": 1, "embeddings": 1}
    assert read_rows(new)[2][-2:] == (None, None)


def test_description_reused_across_package_move_is_reported(databases, tmp_path):
    old, new = databases
    with sqlite3.connect(old) as connection:
        connection.execute("UPDATE declarations SET module='Physlib.Old' WHERE name='unchanged'")
    report = AUDIT.audit(old, new, tmp_path / "package_move", module_roots=["Mathlib"])
    assert report["overall"]["new_names"] == 2  # The moved declaration plus the new name.
    assert report["overall"]["description_cache_outside_scope"] == 1
    assert report["overall"]["description_cache_other_module_root"] == 1
    assert report["overall"]["reusable_descriptions"] == 1
    assert report["overall"]["upstream_llm_misses"] == 3
