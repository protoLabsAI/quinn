"""Knowledge store for Quinn QA agent -- SQLite + sqlite-vec backed.

Stores QA reports, bug patterns, release notes, triage log entries, and
tracked apps with semantic search via the LiteLLM gateway's OpenAI-compatible
embedding endpoint and sqlite-vec.

Embeddings route through the gateway (``qwen3-embedding`` → Qwen3-Embedding-0.6B)
so there's no separate Ollama connection to manage and the same model serves
every agent in the fleet. The gateway caches identical embedding requests so
repeated search queries pay vector latency once.
"""

import json
import os
import sqlite3
import struct
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

_GATEWAY_URL = os.environ.get("OPENAI_BASE_URL", "http://gateway:4000/v1")
_GATEWAY_KEY = os.environ.get("OPENAI_API_KEY", "")
_EMBED_MODEL = "qwen3-embedding"
_EMBED_DIM = 1024  # Qwen3-Embedding-0.6B native dim (supports Matryoshka truncation)
_DB_PATH = Path("/sandbox/knowledge/qa.db")
_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


class KnowledgeStore:
    """QA knowledge store with semantic vector search."""

    def __init__(
        self,
        db_path: Path = _DB_PATH,
        gateway_url: str = _GATEWAY_URL,
        gateway_key: str = _GATEWAY_KEY,
        model: str = _EMBED_MODEL,
    ):
        self.db_path = db_path
        self.gateway_url = gateway_url.rstrip("/")
        self.gateway_key = gateway_key
        self.model = model
        self._db: sqlite3.Connection | None = None

    def _get_db(self) -> sqlite3.Connection | None:
        if self._db is not None:
            return self._db
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            import sqlite_vec

            db = sqlite3.connect(str(self.db_path), check_same_thread=False)
            db.enable_load_extension(True)
            sqlite_vec.load(db)
            db.enable_load_extension(False)

            # Apply schema
            schema_sql = _SCHEMA_PATH.read_text()
            db.executescript(schema_sql)

            # Idempotent migrations for DBs created before a column landed.
            # CREATE TABLE IF NOT EXISTS is a no-op on existing tables, so added
            # columns need ALTER TABLE to reach already-initialized DBs.
            self._migrate_schema(db)

            db.execute(f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_vec
                USING vec0(embedding float[{_EMBED_DIM}])
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS knowledge_vec_map (
                    rowid INTEGER PRIMARY KEY,
                    source_table TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    content_preview TEXT
                )
            """)
            db.commit()
            self._db = db
            return db
        except Exception as e:
            print(f"[knowledge] DB init failed: {e}")
            return None

    @staticmethod
    def _migrate_schema(db: sqlite3.Connection) -> None:
        """Apply additive schema migrations to an already-initialized DB.

        Only runs ALTER TABLE when the column is missing, so it is safe to call
        on every startup.
        """
        def _columns(table: str) -> set[str]:
            return {row[1] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}  # noqa: S608

        # bug_patterns.related_features — added after the table shipped; used for
        # cross-feature bug clustering. Pre-migration, INSERTs referencing this
        # column raised "no such column" and the triage skill died silently.
        if "related_features" not in _columns("bug_patterns"):
            db.execute("ALTER TABLE bug_patterns ADD COLUMN related_features TEXT")

        db.commit()

    def _embed(self, text: str) -> list[float] | None:
        """Fetch an embedding via the gateway's OpenAI-compatible endpoint.

        Returns None on any failure — callers treat that as "skip the vector
        insert but keep the relational row." The agent never blocks on
        embedding availability.
        """
        try:
            headers = {"Content-Type": "application/json"}
            if self.gateway_key:
                headers["Authorization"] = f"Bearer {self.gateway_key}"
            resp = httpx.post(
                f"{self.gateway_url}/embeddings",
                json={"model": self.model, "input": text[:2000]},
                headers=headers,
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json().get("data") or []
            if not data:
                return None
            return data[0].get("embedding")
        except Exception:
            return None

    def _store_vector(
        self, db: sqlite3.Connection, text: str, table: str, source_id: str
    ) -> bool:
        embedding = self._embed(text)
        if embedding is None:
            return False
        vec_bytes = struct.pack(f"{len(embedding)}f", *embedding)
        cursor = db.execute(
            "INSERT INTO knowledge_vec (embedding) VALUES (?)", (vec_bytes,)
        )
        db.execute(
            "INSERT INTO knowledge_vec_map (rowid, source_table, source_id, content_preview) VALUES (?, ?, ?, ?)",
            (cursor.lastrowid, table, str(source_id), text[:200]),
        )
        return True

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    # --- QA Reports ---

    def add_report(
        self,
        title: str,
        summary: str,
        app_name: str = "",
        severity: str = "info",
        findings: list[str] | None = None,
    ) -> bool:
        """Store a QA verification report."""
        db = self._get_db()
        if db is None:
            return False

        now = self._now_iso()
        cursor = db.execute(
            """INSERT INTO qa_reports (title, summary, app_name, severity, findings, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (title, summary, app_name, severity, json.dumps(findings or []), now),
        )
        embed_text = f"{title}\n{summary}"
        self._store_vector(db, embed_text, "qa_reports", str(cursor.lastrowid))
        db.commit()
        return True

    def get_reports(
        self, app_name: str | None = None, severity: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        """Get QA reports, optionally filtered by app or severity."""
        db = self._get_db()
        if db is None:
            return []
        query = "SELECT * FROM qa_reports WHERE 1=1"
        params: list[Any] = []
        if app_name:
            query += " AND app_name = ?"
            params.append(app_name)
        if severity:
            query += " AND severity = ?"
            params.append(severity)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = db.execute(query, params).fetchall()
        cols = [d[0] for d in db.execute("SELECT * FROM qa_reports LIMIT 0").description]
        return [dict(zip(cols, row)) for row in rows]

    # --- Bug Patterns ---

    def add_bug_pattern(
        self,
        title: str,
        description: str,
        app_name: str = "",
        severity: str = "MEDIUM",
        category: str = "",
        pattern: str = "",
        resolution: str = "",
        related_features: list[str] | None = None,
    ) -> bool:
        """Store a recurring bug pattern for regression detection."""
        db = self._get_db()
        if db is None:
            return False

        now = self._now_iso()

        existing = db.execute(
            "SELECT id, occurrences, related_features FROM bug_patterns WHERE title = ? AND app_name = ?",
            (title, app_name),
        ).fetchone()

        if existing:
            # Union the incoming feature IDs into whatever is already on the
            # row — the whole point of related_features is cross-feature
            # clustering, so losing this list on re-occurrence (the original
            # UPSERT behaviour) defeated the column. Order-preserving dedup
            # so the stored list doesn't churn.
            try:
                prior = json.loads(existing[2]) if existing[2] else []
                if not isinstance(prior, list):
                    prior = []
            except (TypeError, ValueError):
                prior = []
            merged = list(dict.fromkeys(prior + (related_features or [])))
            db.execute(
                """UPDATE bug_patterns
                   SET occurrences = ?, last_seen = ?, resolved = 0,
                       related_features = ?
                   WHERE id = ?""",
                (existing[1] + 1, now, json.dumps(merged), existing[0]),
            )
        else:
            cursor = db.execute(
                """INSERT INTO bug_patterns
                   (title, description, app_name, severity, category, pattern,
                    occurrences, first_seen, last_seen, resolved, resolution,
                    related_features)
                   VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, 0, ?, ?)""",
                (
                    title, description, app_name, severity, category, pattern,
                    now, now, resolution,
                    json.dumps(related_features or []),
                ),
            )
            embed_text = f"{title}\n{description}\n{pattern}"
            self._store_vector(db, embed_text, "bug_patterns", str(cursor.lastrowid))

        db.commit()
        return True

    def get_bug_patterns(
        self, app_name: str | None = None, severity: str | None = None,
        unresolved_only: bool = True, limit: int = 50,
    ) -> list[dict]:
        """Get bug patterns, optionally filtered."""
        db = self._get_db()
        if db is None:
            return []
        query = "SELECT * FROM bug_patterns WHERE 1=1"
        params: list[Any] = []
        if app_name:
            query += " AND app_name = ?"
            params.append(app_name)
        if severity:
            query += " AND severity = ?"
            params.append(severity)
        if unresolved_only:
            query += " AND resolved = 0"
        query += " ORDER BY last_seen DESC LIMIT ?"
        params.append(limit)
        rows = db.execute(query, params).fetchall()
        cols = [d[0] for d in db.execute("SELECT * FROM bug_patterns LIMIT 0").description]
        return [dict(zip(cols, row)) for row in rows]

    def resolve_bug_pattern(self, pattern_id: int, resolution: str = "") -> bool:
        """Mark a bug pattern as resolved."""
        db = self._get_db()
        if db is None:
            return False
        db.execute(
            "UPDATE bug_patterns SET resolved = 1, resolution = ? WHERE id = ?",
            (resolution, pattern_id),
        )
        db.commit()
        return True

    # --- Release Notes ---

    def add_release_notes(
        self,
        app_name: str,
        version: str,
        content: str,
        title: str = "",
        features_count: int = 0,
        fixes_count: int = 0,
        breaking_changes: list[str] | None = None,
    ) -> bool:
        """Store generated release notes."""
        db = self._get_db()
        if db is None:
            return False

        now = self._now_iso()
        cursor = db.execute(
            """INSERT INTO release_notes
               (app_name, version, title, content, features_count, fixes_count,
                breaking_changes, published, published_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL, ?)""",
            (
                app_name, version, title, content, features_count, fixes_count,
                json.dumps(breaking_changes or []), now,
            ),
        )
        embed_text = f"{app_name} {version} {title}\n{content[:500]}"
        self._store_vector(db, embed_text, "release_notes", str(cursor.lastrowid))
        db.commit()
        return True

    def get_release_notes(
        self, app_name: str | None = None, limit: int = 10,
    ) -> list[dict]:
        """Get release notes, optionally filtered by app."""
        db = self._get_db()
        if db is None:
            return []
        query = "SELECT * FROM release_notes"
        params: list[Any] = []
        if app_name:
            query += " WHERE app_name = ?"
            params.append(app_name)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = db.execute(query, params).fetchall()
        cols = [d[0] for d in db.execute("SELECT * FROM release_notes LIMIT 0").description]
        return [dict(zip(cols, row)) for row in rows]

    # --- Triage Log ---

    def add_triage_entry(
        self,
        source: str,
        source_id: str,
        classification: str,
        app_name: str = "",
        severity: str = "",
        reason: str = "",
        action_taken: str = "",
    ) -> bool:
        """Log an issue triage decision."""
        db = self._get_db()
        if db is None:
            return False

        now = self._now_iso()
        cursor = db.execute(
            """INSERT INTO triage_log
               (source, source_id, app_name, classification, severity, reason,
                action_taken, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (source, source_id, app_name, classification, severity, reason, action_taken, now),
        )
        embed_text = f"{source} {source_id} {classification} {reason}"
        self._store_vector(db, embed_text, "triage_log", str(cursor.lastrowid))
        db.commit()
        return True

    def get_triage_log(
        self, app_name: str | None = None, classification: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        """Get triage log entries."""
        db = self._get_db()
        if db is None:
            return []
        query = "SELECT * FROM triage_log WHERE 1=1"
        params: list[Any] = []
        if app_name:
            query += " AND app_name = ?"
            params.append(app_name)
        if classification:
            query += " AND classification = ?"
            params.append(classification)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = db.execute(query, params).fetchall()
        cols = [d[0] for d in db.execute("SELECT * FROM triage_log LIMIT 0").description]
        return [dict(zip(cols, row)) for row in rows]

    # --- Apps ---

    def add_app(
        self,
        name: str,
        github_repo: str = "",
        server_url: str = "",
        config: dict | None = None,
    ) -> bool:
        """Register an app for QA tracking."""
        db = self._get_db()
        if db is None:
            return False

        db.execute(
            """INSERT OR REPLACE INTO apps (name, github_repo, server_url, config)
               VALUES (?, ?, ?, ?)""",
            (name, github_repo, server_url, json.dumps(config or {})),
        )
        db.commit()
        return True

    def get_apps(self) -> list[dict]:
        """Get all tracked apps."""
        db = self._get_db()
        if db is None:
            return []
        rows = db.execute("SELECT * FROM apps ORDER BY name").fetchall()
        cols = [d[0] for d in db.execute("SELECT * FROM apps LIMIT 0").description]
        return [dict(zip(cols, row)) for row in rows]

    def update_app_check(self, name: str) -> bool:
        """Update last_checked_at for an app."""
        db = self._get_db()
        if db is None:
            return False
        now = self._now_iso()
        db.execute(
            "UPDATE apps SET last_checked_at = ? WHERE name = ?",
            (now, name),
        )
        db.commit()
        return True

    # --- Regression Tests ---

    def add_regression_test(
        self,
        title: str,
        description: str,
        steps: list[str] | None = None,
        expected_result: str = "",
        related_bug: str = "",
        app_name: str = "",
    ) -> bool:
        """Store a scripted regression test tied to a known bug pattern."""
        db = self._get_db()
        if db is None:
            return False

        now = self._now_iso()
        cursor = db.execute(
            """INSERT INTO regression_tests
               (title, description, steps, expected_result, related_bug, app_name, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                title, description, json.dumps(steps or []),
                expected_result, related_bug, app_name, now,
            ),
        )
        embed_text = f"{title}\n{description}"
        self._store_vector(db, embed_text, "regression_tests", str(cursor.lastrowid))
        db.commit()
        return True

    def get_regression_tests(
        self, app_name: str | None = None, limit: int = 50,
    ) -> list[dict]:
        db = self._get_db()
        if db is None:
            return []
        query = "SELECT * FROM regression_tests"
        params: list[Any] = []
        if app_name:
            query += " WHERE app_name = ?"
            params.append(app_name)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = db.execute(query, params).fetchall()
        cols = [d[0] for d in db.execute("SELECT * FROM regression_tests LIMIT 0").description]
        return [dict(zip(cols, row)) for row in rows]

    # --- Generic Entry (for flexible storage) ---

    def store_entry(
        self, content: str, source: str = "", source_type: str = "",
        entry_type: str = "finding", metadata: str = "",
    ) -> bool:
        """Store a generic knowledge entry. Falls back to the vector index
        for semantic retrieval without requiring a dedicated table."""
        db = self._get_db()
        if db is None:
            return False
        embed_text = f"{entry_type} {source_type}: {content[:500]}"
        self._store_vector(db, embed_text, entry_type, source or self._now_iso())
        db.commit()
        return True

    # --- Semantic Search ---

    def search(
        self, query: str, k: int = 10,
        filter_table: str | None = None,
    ) -> list[dict[str, Any]]:
        db = self._get_db()
        if db is None:
            return []
        embedding = self._embed(query)
        if embedding is None:
            return []
        vec_bytes = struct.pack(f"{len(embedding)}f", *embedding)
        rows = db.execute(
            """SELECT m.source_table, m.source_id, m.content_preview, v.distance
               FROM knowledge_vec v
               JOIN knowledge_vec_map m ON m.rowid = v.rowid
               WHERE v.embedding MATCH ? AND k = ?
               ORDER BY v.distance""",
            (vec_bytes, k),
        ).fetchall()

        results = []
        for table, source_id, preview, distance in rows:
            if filter_table and table != filter_table:
                continue
            results.append({
                "table": table,
                "source_id": source_id,
                "preview": preview,
                "distance": distance,
            })
        return results

    # --- Stats ---

    def get_stats(self) -> dict[str, int]:
        db = self._get_db()
        if db is None:
            return {}
        stats = {}
        for table in (
            "qa_reports", "bug_patterns", "release_notes",
            "triage_log", "apps", "regression_tests",
        ):
            count = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
            stats[table] = count
        return stats

    def find_similar_bugs(
        self, description: str, k: int = 5,
    ) -> list[dict[str, Any]]:
        """Semantic search scoped to bug_patterns."""
        return self.search(description, k=k, filter_table="bug_patterns")
