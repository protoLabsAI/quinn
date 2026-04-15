"""Regression test for #7 — bug_patterns schema consolidation + migration.

Background: Quinn had two classes writing to the same ``bug_patterns``
table with incompatible column sets. Whoever created the table first won,
and the other side's INSERTs raised ``no such column`` — silently killing
the triage skill on every GitHub issue event.

The fix consolidates both writers onto ``knowledge.store.KnowledgeStore``,
makes ``schema.sql`` the single source of truth, adds ``related_features``
as an additive column, and runs an idempotent ALTER TABLE migration on
startup so pre-existing DBs catch up.

These tests lock the schema shape, the migration behaviour, and the
end-to-end INSERT path that the triage skill depends on.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from knowledge.store import KnowledgeStore

_SCHEMA_PATH = Path(__file__).parents[1] / "knowledge" / "schema.sql"


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}


def test_canonical_schema_has_related_features() -> None:
    """A freshly-initialised DB must have related_features from the start."""
    db = sqlite3.connect(":memory:")
    db.executescript(_SCHEMA_PATH.read_text())
    assert "related_features" in _columns(db, "bug_patterns")


def test_canonical_schema_has_regression_tests_table() -> None:
    """regression_tests was previously defined only in the duplicate store;
    consolidation must bring it into schema.sql or we lose the capability."""
    db = sqlite3.connect(":memory:")
    db.executescript(_SCHEMA_PATH.read_text())
    tables = {
        row[0]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "regression_tests" in tables


def test_migration_adds_related_features_to_legacy_db() -> None:
    """Simulate a DB created before related_features landed. The migration
    must add the column on the next KnowledgeStore._get_db() call without
    touching DBs that already have it."""
    db = sqlite3.connect(":memory:")
    # Legacy bug_patterns: no related_features column.
    db.execute(
        """CREATE TABLE bug_patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            app_name TEXT,
            severity TEXT,
            category TEXT,
            pattern TEXT,
            occurrences INTEGER DEFAULT 1,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            resolved INTEGER DEFAULT 0,
            resolution TEXT
        )"""
    )
    db.commit()
    assert "related_features" not in _columns(db, "bug_patterns")

    KnowledgeStore._migrate_schema(db)
    assert "related_features" in _columns(db, "bug_patterns")


def test_migration_is_idempotent() -> None:
    """Running the migration twice must not raise."""
    db = sqlite3.connect(":memory:")
    db.executescript(_SCHEMA_PATH.read_text())
    KnowledgeStore._migrate_schema(db)
    KnowledgeStore._migrate_schema(db)
    assert "related_features" in _columns(db, "bug_patterns")


def test_insert_bug_pattern_with_related_features_succeeds() -> None:
    """The INSERT shape that previously raised 'no such column' must work
    against both a fresh canonical DB and a migrated legacy DB."""
    import json

    for build_db in (_fresh_canonical_db, _migrated_legacy_db):
        db = build_db()
        db.execute(
            """INSERT INTO bug_patterns
               (title, description, app_name, severity, category, pattern,
                occurrences, first_seen, last_seen, resolved, resolution,
                related_features)
               VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, 0, ?, ?)""",
            (
                "Sample bug", "Desc", "protomaker", "HIGH", "wiring", "grep me",
                "2026-04-15T00:00:00Z", "2026-04-15T00:00:00Z", "",
                json.dumps(["feature-1", "feature-2"]),
            ),
        )
        db.commit()
        row = db.execute(
            "SELECT related_features FROM bug_patterns WHERE title = ?",
            ("Sample bug",),
        ).fetchone()
        assert row is not None
        assert json.loads(row[0]) == ["feature-1", "feature-2"]


def _fresh_canonical_db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.executescript(_SCHEMA_PATH.read_text())
    return db


def _migrated_legacy_db() -> sqlite3.Connection:
    """Simulate a DB that pre-dates the related_features column."""
    db = sqlite3.connect(":memory:")
    db.execute(
        """CREATE TABLE bug_patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            app_name TEXT,
            severity TEXT,
            category TEXT,
            pattern TEXT,
            occurrences INTEGER DEFAULT 1,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            resolved INTEGER DEFAULT 0,
            resolution TEXT
        )"""
    )
    db.commit()
    KnowledgeStore._migrate_schema(db)
    return db


def test_qa_memory_tool_wires_to_knowledge_store() -> None:
    """Lock the consolidation: QAMemoryTool must wrap KnowledgeStore, not
    a duplicate class. Prevents schema drift from re-emerging."""
    from tools.qa_memory import QAMemoryTool

    tool = QAMemoryTool.__init__  # just reference — import smoke
    assert tool is not None

    # The module must not re-introduce a QAKnowledgeStore class.
    import tools.qa_memory as qa_memory_mod
    assert not hasattr(qa_memory_mod, "QAKnowledgeStore"), (
        "QAKnowledgeStore was removed in #7 — re-introducing it will "
        "resurrect the schema-drift bug."
    )
