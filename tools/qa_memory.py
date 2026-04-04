"""QA knowledge store for Quinn QA agent.

Persists QA reports, bug patterns, release notes, and regression tests
with semantic search via SQLite + sqlite-vec, using Ollama embeddings.

Maintains its own database separate from the research knowledge store,
with QA-specific tables and query patterns.
"""

import json
import os
import sqlite3
import struct
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool

_DB_PATH = Path(os.environ.get("QA_DB_PATH", "/sandbox/knowledge/qa.db"))

_QA_SCHEMA = """
CREATE TABLE IF NOT EXISTS qa_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    app_name TEXT,
    severity TEXT DEFAULT 'info',
    findings TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bug_patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    category TEXT,
    occurrences INTEGER DEFAULT 1,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    related_features TEXT,
    resolution TEXT,
    is_resolved INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS release_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version TEXT NOT NULL,
    content TEXT NOT NULL,
    commits_included TEXT,
    prs_included TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS regression_tests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    steps TEXT,
    expected_result TEXT,
    related_bug TEXT,
    app_name TEXT,
    last_run TEXT,
    last_result TEXT DEFAULT 'pending',
    created_at TEXT NOT NULL
);
"""

# Valid entry types and their corresponding tables
_TYPE_TABLE_MAP = {
    "qa_report": "qa_reports",
    "bug_pattern": "bug_patterns",
    "release_note": "release_notes",
    "regression_test": "regression_tests",
}


class QAKnowledgeStore:
    """QA-specific knowledge store with semantic vector search.

    Uses the same SQLite + sqlite-vec architecture as the research KnowledgeStore
    but with dedicated QA tables: qa_reports, bug_patterns, release_notes,
    and regression_tests.
    """

    def __init__(
        self,
        db_path: Path = _DB_PATH,
        ollama_url: str = "",
        model: str = "",
    ):
        # Import embedding config from research store to stay consistent
        from knowledge.store import _EMBED_DIM, _EMBED_MODEL, _OLLAMA_URL

        self.db_path = db_path
        self.ollama_url = ollama_url or _OLLAMA_URL
        self.model = model or _EMBED_MODEL
        self.embed_dim = _EMBED_DIM
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

            db.executescript(_QA_SCHEMA)

            db.execute(f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS qa_vec
                USING vec0(embedding float[{self.embed_dim}])
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS qa_vec_map (
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
            print(f"[qa_memory] DB init failed: {e}")
            return None

    def _embed(self, text: str) -> list[float] | None:
        import httpx

        try:
            resp = httpx.post(
                f"{self.ollama_url}/api/embeddings",
                json={"model": self.model, "prompt": text[:2000]},
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json()["embedding"]
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
            "INSERT INTO qa_vec (embedding) VALUES (?)", (vec_bytes,)
        )
        db.execute(
            "INSERT INTO qa_vec_map (rowid, source_table, source_id, content_preview) "
            "VALUES (?, ?, ?, ?)",
            (cursor.lastrowid, table, str(source_id), text[:200]),
        )
        return True

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    # --- QA Reports ---

    def add_qa_report(
        self,
        title: str,
        summary: str,
        app_name: str = "",
        severity: str = "info",
        findings: list[str] | None = None,
    ) -> bool:
        db = self._get_db()
        if db is None:
            return False
        now = self._now_iso()
        cursor = db.execute(
            """INSERT INTO qa_reports (title, summary, app_name, severity, findings, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (title, summary, app_name, severity, json.dumps(findings or []), now),
        )
        self._store_vector(db, f"{title}\n{summary}", "qa_reports", str(cursor.lastrowid))
        db.commit()
        return True

    # --- Bug Patterns ---

    def add_bug_pattern(
        self,
        title: str,
        description: str,
        category: str = "",
        related_features: list[str] | None = None,
        resolution: str = "",
    ) -> bool:
        db = self._get_db()
        if db is None:
            return False
        now = self._now_iso()
        cursor = db.execute(
            """INSERT INTO bug_patterns
               (title, description, category, occurrences, first_seen, last_seen,
                related_features, resolution)
               VALUES (?, ?, ?, 1, ?, ?, ?, ?)""",
            (
                title, description, category, now, now,
                json.dumps(related_features or []), resolution,
            ),
        )
        self._store_vector(
            db, f"{title}\n{description}\n{category}",
            "bug_patterns", str(cursor.lastrowid),
        )
        db.commit()
        return True

    # --- Release Notes ---

    def add_release_note(
        self,
        version: str,
        content: str,
        commits_included: list[str] | None = None,
        prs_included: list[int] | None = None,
    ) -> bool:
        db = self._get_db()
        if db is None:
            return False
        now = self._now_iso()
        cursor = db.execute(
            """INSERT INTO release_notes
               (version, content, commits_included, prs_included, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                version, content,
                json.dumps(commits_included or []),
                json.dumps(prs_included or []),
                now,
            ),
        )
        self._store_vector(
            db, f"Release {version}\n{content[:500]}",
            "release_notes", str(cursor.lastrowid),
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
        self._store_vector(
            db, f"{title}\n{description}",
            "regression_tests", str(cursor.lastrowid),
        )
        db.commit()
        return True

    # --- Search & Query ---

    def search(
        self, query: str, k: int = 10, filter_table: str | None = None
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
               FROM qa_vec v
               JOIN qa_vec_map m ON m.rowid = v.rowid
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
                "distance": round(distance, 4),
            })
        return results

    def get_recent(self, entry_type: str, limit: int = 10) -> list[dict[str, Any]]:
        """Get most recent entries of a given type."""
        table = _TYPE_TABLE_MAP.get(entry_type)
        if not table:
            return []
        db = self._get_db()
        if db is None:
            return []
        rows = db.execute(
            f"SELECT * FROM {table} ORDER BY id DESC LIMIT ?",  # noqa: S608
            (limit,),
        ).fetchall()
        cols = [d[0] for d in db.execute(f"SELECT * FROM {table} LIMIT 0").description]  # noqa: S608
        return [dict(zip(cols, row)) for row in rows]

    def find_similar_bugs(self, description: str, k: int = 5) -> list[dict[str, Any]]:
        """Find bug patterns similar to a description."""
        return self.search(description, k=k, filter_table="bug_patterns")

    def get_stats(self) -> dict[str, int]:
        db = self._get_db()
        if db is None:
            return {}
        stats = {}
        for table in _TYPE_TABLE_MAP.values():
            count = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
            stats[table] = count
        return stats


def _split_csv(raw: str) -> list[str]:
    """Split a comma-separated string into a cleaned list."""
    return [item.strip() for item in raw.split(",") if item.strip()] if raw else []


class QAMemoryTool(Tool):
    """QA knowledge store with semantic search for reports, bugs, and tests."""

    def __init__(self, store: QAKnowledgeStore | None = None):
        self._store = store or QAKnowledgeStore()

    @property
    def name(self) -> str:
        return "qa_memory"

    @property
    def description(self) -> str:
        return (
            "Persistent QA knowledge store with semantic search.\n\n"
            "Actions:\n"
            "- store: Save a QA report, bug pattern, release note, or regression test\n"
            "- search: Semantic search across all stored QA data\n"
            "- recent: Get most recent N entries by type\n"
            "- patterns: Find recurring bug patterns similar to a description\n"
            "- stats: Show knowledge base statistics\n\n"
            "Types: qa_report, bug_pattern, release_note, regression_test"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["store", "search", "recent", "patterns", "stats"],
                    "description": "Action to perform.",
                },
                "entry_type": {
                    "type": "string",
                    "enum": ["qa_report", "bug_pattern", "release_note", "regression_test"],
                    "description": "Type of entry (for store and recent actions).",
                },
                "title": {
                    "type": "string",
                    "description": "Title for the entry.",
                },
                "summary": {
                    "type": "string",
                    "description": "Summary text (for qa_report).",
                },
                "description": {
                    "type": "string",
                    "description": "Description (for bug_pattern, regression_test).",
                },
                "content": {
                    "type": "string",
                    "description": "Content text (for release_note).",
                },
                "version": {
                    "type": "string",
                    "description": "Version string (for release_note).",
                },
                "app_name": {
                    "type": "string",
                    "description": "Application name (for qa_report, regression_test).",
                },
                "severity": {
                    "type": "string",
                    "enum": ["info", "low", "medium", "high", "critical"],
                    "description": "Severity level (for qa_report).",
                },
                "category": {
                    "type": "string",
                    "description": "Bug category (for bug_pattern).",
                },
                "resolution": {
                    "type": "string",
                    "description": "Resolution description (for bug_pattern).",
                },
                "steps": {
                    "type": "string",
                    "description": "Comma-separated test steps (for regression_test).",
                },
                "expected_result": {
                    "type": "string",
                    "description": "Expected result (for regression_test).",
                },
                "related_bug": {
                    "type": "string",
                    "description": "Related bug pattern title or ID (for regression_test).",
                },
                "related_features": {
                    "type": "string",
                    "description": "Comma-separated feature IDs (for bug_pattern).",
                },
                "findings": {
                    "type": "string",
                    "description": "Comma-separated findings (for qa_report).",
                },
                "commits_included": {
                    "type": "string",
                    "description": "Comma-separated commit SHAs (for release_note).",
                },
                "prs_included": {
                    "type": "string",
                    "description": "Comma-separated PR numbers (for release_note).",
                },
                "query": {
                    "type": "string",
                    "description": "Search query (for search and patterns actions).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Number of results to return (default: 10).",
                },
            },
            "required": ["action"],
        }

    async def execute(self, **kwargs: Any) -> str:
        action = kwargs["action"]

        if action == "store":
            return self._handle_store(kwargs)
        elif action == "search":
            return self._handle_search(kwargs)
        elif action == "recent":
            return self._handle_recent(kwargs)
        elif action == "patterns":
            return self._handle_patterns(kwargs)
        elif action == "stats":
            return self._handle_stats()
        else:
            return f"Error: Unknown action '{action}'."

    # --- Store dispatchers ---

    def _handle_store(self, kwargs: dict[str, Any]) -> str:
        entry_type = kwargs.get("entry_type", "")
        if not entry_type:
            return "Error: 'entry_type' is required for store action."

        if entry_type == "qa_report":
            return self._store_qa_report(kwargs)
        elif entry_type == "bug_pattern":
            return self._store_bug_pattern(kwargs)
        elif entry_type == "release_note":
            return self._store_release_note(kwargs)
        elif entry_type == "regression_test":
            return self._store_regression_test(kwargs)
        else:
            types = ", ".join(_TYPE_TABLE_MAP.keys())
            return f"Error: Unknown entry_type '{entry_type}'. Use: {types}"

    def _store_qa_report(self, kwargs: dict[str, Any]) -> str:
        title = kwargs.get("title", "")
        summary = kwargs.get("summary", "")
        if not title or not summary:
            return "Error: 'title' and 'summary' are required for qa_report."
        ok = self._store.add_qa_report(
            title=title,
            summary=summary,
            app_name=kwargs.get("app_name", ""),
            severity=kwargs.get("severity", "info"),
            findings=_split_csv(kwargs.get("findings", "")),
        )
        return f"Stored QA report: {title}" if ok else "Error: Failed to store (DB unavailable)."

    def _store_bug_pattern(self, kwargs: dict[str, Any]) -> str:
        title = kwargs.get("title", "")
        description = kwargs.get("description", "")
        if not title or not description:
            return "Error: 'title' and 'description' are required for bug_pattern."
        ok = self._store.add_bug_pattern(
            title=title,
            description=description,
            category=kwargs.get("category", ""),
            related_features=_split_csv(kwargs.get("related_features", "")),
            resolution=kwargs.get("resolution", ""),
        )
        return f"Stored bug pattern: {title}" if ok else "Error: Failed to store (DB unavailable)."

    def _store_release_note(self, kwargs: dict[str, Any]) -> str:
        version = kwargs.get("version", "")
        content = kwargs.get("content", "")
        if not version or not content:
            return "Error: 'version' and 'content' are required for release_note."
        prs_raw = kwargs.get("prs_included", "")
        prs = [int(p.strip()) for p in prs_raw.split(",") if p.strip().isdigit()] if prs_raw else []
        ok = self._store.add_release_note(
            version=version,
            content=content,
            commits_included=_split_csv(kwargs.get("commits_included", "")),
            prs_included=prs,
        )
        return f"Stored release note: {version}" if ok else "Error: Failed to store (DB unavailable)."

    def _store_regression_test(self, kwargs: dict[str, Any]) -> str:
        title = kwargs.get("title", "")
        description = kwargs.get("description", "")
        if not title or not description:
            return "Error: 'title' and 'description' are required for regression_test."
        ok = self._store.add_regression_test(
            title=title,
            description=description,
            steps=_split_csv(kwargs.get("steps", "")),
            expected_result=kwargs.get("expected_result", ""),
            related_bug=kwargs.get("related_bug", ""),
            app_name=kwargs.get("app_name", ""),
        )
        return f"Stored regression test: {title}" if ok else "Error: Failed to store (DB unavailable)."

    # --- Query handlers ---

    def _handle_search(self, kwargs: dict[str, Any]) -> str:
        query = kwargs.get("query", "")
        if not query:
            return "Error: 'query' is required for search."
        limit = kwargs.get("limit", 10)
        entry_type = kwargs.get("entry_type", "")
        filter_table = _TYPE_TABLE_MAP.get(entry_type) if entry_type else None

        results = self._store.search(query, k=limit, filter_table=filter_table)
        if not results:
            return "No matching results found."

        lines = [f"**Search results for '{query}'** ({len(results)} matches):\n"]
        for r in results:
            lines.append(
                f"- [{r['table']}] #{r['source_id']} (distance: {r['distance']})\n"
                f"  {r['preview']}"
            )
        return "\n".join(lines)

    def _handle_recent(self, kwargs: dict[str, Any]) -> str:
        entry_type = kwargs.get("entry_type", "")
        if not entry_type:
            return "Error: 'entry_type' is required for recent."
        if entry_type not in _TYPE_TABLE_MAP:
            types = ", ".join(_TYPE_TABLE_MAP.keys())
            return f"Error: Unknown entry_type '{entry_type}'. Use: {types}"

        limit = kwargs.get("limit", 10)
        entries = self._store.get_recent(entry_type, limit=limit)
        if not entries:
            return f"No {entry_type} entries found."

        lines = [f"**Recent {entry_type} entries ({len(entries)}):**\n"]
        for entry in entries:
            entry_id = entry.get("id", "?")
            name = entry.get("title", entry.get("version", "Untitled"))
            lines.append(f"- #{entry_id}: {name}")

            if entry_type == "bug_pattern":
                freq = entry.get("occurrences", 0)
                resolved = "resolved" if entry.get("is_resolved") else "open"
                lines.append(f"  Frequency: {freq} | Status: {resolved}")
            elif entry_type == "qa_report":
                sev = entry.get("severity", "info")
                app = entry.get("app_name", "")
                detail = f"  Severity: {sev}"
                if app:
                    detail += f" | App: {app}"
                lines.append(detail)
            elif entry_type == "regression_test":
                result = entry.get("last_result", "pending")
                lines.append(f"  Last result: {result}")
        return "\n".join(lines)

    def _handle_patterns(self, kwargs: dict[str, Any]) -> str:
        query = kwargs.get("query", "")
        if not query:
            return "Error: 'query' is required for patterns."
        limit = kwargs.get("limit", 5)
        results = self._store.find_similar_bugs(query, k=limit)
        if not results:
            return "No similar bug patterns found."

        lines = [f"**Similar bug patterns for '{query}'** ({len(results)} matches):\n"]
        for r in results:
            lines.append(
                f"- Bug #{r['source_id']} (distance: {r['distance']})\n"
                f"  {r['preview']}"
            )
        return "\n".join(lines)

    def _handle_stats(self) -> str:
        stats = self._store.get_stats()
        if not stats:
            return "Error: Could not retrieve stats (DB unavailable)."

        lines = ["**QA Knowledge Base Stats:**\n"]
        for table, count in sorted(stats.items()):
            lines.append(f"- {table}: {count}")
        total = sum(stats.values())
        lines.append(f"\n**Total: {total} entries**")
        return "\n".join(lines)
