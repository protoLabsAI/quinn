"""Knowledge store for protoResearcher — SQLite + sqlite-vec backed.

Stores papers, findings, digests, model releases with semantic search
via Ollama embeddings and sqlite-vec.
"""

import json
import sqlite3
import struct
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

_OLLAMA_URL = "http://host.docker.internal:11434"
_EMBED_MODEL = "nomic-embed-text"
_EMBED_DIM = 768
_DB_PATH = Path("/sandbox/knowledge/research.db")
_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


class KnowledgeStore:
    """Research knowledge store with semantic vector search."""

    def __init__(
        self,
        db_path: Path = _DB_PATH,
        ollama_url: str = _OLLAMA_URL,
        model: str = _EMBED_MODEL,
    ):
        self.db_path = db_path
        self.ollama_url = ollama_url
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

            # Create vector tables
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

    def _embed(self, text: str) -> list[float] | None:
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
            "INSERT INTO knowledge_vec (embedding) VALUES (?)", (vec_bytes,)
        )
        db.execute(
            "INSERT INTO knowledge_vec_map (rowid, source_table, source_id, content_preview) VALUES (?, ?, ?, ?)",
            (cursor.lastrowid, table, str(source_id), text[:200]),
        )
        return True

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    # --- Papers ---

    def add_paper(
        self,
        arxiv_id: str,
        title: str,
        authors: list[str] | None = None,
        abstract: str = "",
        summary: str = "",
        significance: str = "unknown",
        categories: list[str] | None = None,
        tags: list[str] | None = None,
        pdf_path: str = "",
        source_url: str = "",
        published_at: str = "",
        notes: str = "",
    ) -> bool:
        db = self._get_db()
        if db is None:
            return False

        now = self._now_iso()
        read_at = now if summary else ""

        db.execute(
            """INSERT OR REPLACE INTO papers
               (id, title, authors, abstract, summary, significance, categories, tags,
                pdf_path, source_url, published_at, discovered_at, read_at, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                arxiv_id, title, json.dumps(authors or []), abstract, summary,
                significance, json.dumps(categories or []), json.dumps(tags or []),
                pdf_path, source_url, published_at, now, read_at, notes,
            ),
        )
        # Embed abstract + summary for search
        embed_text = f"{title}\n{abstract}\n{summary}".strip()
        self._store_vector(db, embed_text, "papers", arxiv_id)
        db.commit()
        return True

    def get_paper(self, arxiv_id: str) -> dict | None:
        db = self._get_db()
        if db is None:
            return None
        row = db.execute("SELECT * FROM papers WHERE id = ?", (arxiv_id,)).fetchone()
        if not row:
            return None
        cols = [d[0] for d in db.execute("SELECT * FROM papers LIMIT 0").description]
        return dict(zip(cols, row))

    def get_papers(
        self, topic: str | None = None, since: str | None = None,
        significance: str | None = None, limit: int = 20
    ) -> list[dict]:
        db = self._get_db()
        if db is None:
            return []
        query = "SELECT * FROM papers WHERE 1=1"
        params: list[Any] = []
        if significance:
            query += " AND significance = ?"
            params.append(significance)
        if since:
            query += " AND discovered_at >= ?"
            params.append(since)
        if topic:
            query += " AND (tags LIKE ? OR categories LIKE ?)"
            params.extend([f'%"{topic}"%', f'%"{topic}"%'])
        query += " ORDER BY discovered_at DESC LIMIT ?"
        params.append(limit)
        rows = db.execute(query, params).fetchall()
        cols = [d[0] for d in db.execute("SELECT * FROM papers LIMIT 0").description]
        return [dict(zip(cols, row)) for row in rows]

    # --- Findings ---

    def add_finding(
        self, content: str, source: str = "", source_type: str = "",
        topic: str = "", finding_type: str = "insight", significance: str = "",
    ) -> bool:
        db = self._get_db()
        if db is None:
            return False
        now = self._now_iso()
        cursor = db.execute(
            """INSERT INTO findings (content, source, source_type, topic, finding_type, significance, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (content, source, source_type, topic, finding_type, significance, now),
        )
        self._store_vector(db, content, "findings", str(cursor.lastrowid))
        db.commit()
        return True

    # --- Topics ---

    def add_topic(
        self, name: str, description: str = "", keywords: list[str] | None = None,
        priority: int = 2,
    ) -> bool:
        db = self._get_db()
        if db is None:
            return False
        now = self._now_iso()
        db.execute(
            """INSERT OR REPLACE INTO topics (name, description, keywords, priority, active, created_at)
               VALUES (?, ?, ?, ?, 1, ?)""",
            (name, description, json.dumps(keywords or []), priority, now),
        )
        db.commit()
        return True

    def get_topics(self, active_only: bool = True) -> list[dict]:
        db = self._get_db()
        if db is None:
            return []
        query = "SELECT * FROM topics"
        if active_only:
            query += " WHERE active = 1"
        query += " ORDER BY priority, name"
        rows = db.execute(query).fetchall()
        cols = [d[0] for d in db.execute("SELECT * FROM topics LIMIT 0").description]
        return [dict(zip(cols, row)) for row in rows]

    # --- Digests ---

    def add_digest(
        self, title: str, content: str, digest_type: str = "weekly",
        topic: str = "", papers_referenced: list[str] | None = None,
    ) -> bool:
        db = self._get_db()
        if db is None:
            return False
        now = self._now_iso()
        cursor = db.execute(
            """INSERT INTO digests (title, content, digest_type, topic, papers_referenced, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (title, content, digest_type, topic, json.dumps(papers_referenced or []), now),
        )
        self._store_vector(db, f"{title}\n{content[:500]}", "digests", str(cursor.lastrowid))
        db.commit()
        return True

    def get_digests(self, topic: str | None = None, limit: int = 10) -> list[dict]:
        db = self._get_db()
        if db is None:
            return []
        query = "SELECT * FROM digests"
        params: list[Any] = []
        if topic:
            query += " WHERE topic = ?"
            params.append(topic)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = db.execute(query, params).fetchall()
        cols = [d[0] for d in db.execute("SELECT * FROM digests LIMIT 0").description]
        return [dict(zip(cols, row)) for row in rows]

    # --- Model Releases ---

    def add_model_release(
        self, model_id: str, name: str = "", organization: str = "",
        description: str = "", parameters: str = "", architecture: str = "",
        license_: str = "", downloads: int = 0, likes: int = 0,
        source: str = "huggingface", released_at: str = "", notes: str = "",
    ) -> bool:
        db = self._get_db()
        if db is None:
            return False
        now = self._now_iso()
        cursor = db.execute(
            """INSERT INTO model_releases
               (model_id, name, organization, description, parameters, architecture,
                license, downloads, likes, source, released_at, discovered_at, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (model_id, name, organization, description, parameters, architecture,
             license_, downloads, likes, source, released_at, now, notes),
        )
        embed_text = f"{model_id} {name} {description}".strip()
        self._store_vector(db, embed_text, "model_releases", str(cursor.lastrowid))
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
        for table in ("papers", "findings", "topics", "digests", "model_releases"):
            count = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            stats[table] = count
        return stats
