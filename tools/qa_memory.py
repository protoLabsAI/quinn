"""QA Memory tool — stores and retrieves QA reports, bug patterns, release notes.

Wraps the KnowledgeStore with QA-specific actions for use as a LangGraph tool.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from knowledge.store import KnowledgeStore

_DB_PATH = Path(os.environ.get("QA_DB_PATH", "/sandbox/knowledge/qa.db"))

# Lazy-initialized global store
_store: KnowledgeStore | None = None


def _get_store() -> KnowledgeStore:
    global _store
    if _store is None:
        _store = KnowledgeStore(db_path=_DB_PATH)
    return _store


def qa_memory(
    action: str,
    content: str = "",
    entry_type: str = "qa_report",
    app_name: str = "",
    severity: str = "",
    query: str = "",
    limit: int = 10,
) -> str:
    """Store and search QA knowledge: reports, bug patterns, release notes, triage decisions.

    Args:
        action: One of 'store', 'search', 'recent', 'patterns'
        content: Text content to store (for 'store' action)
        entry_type: Type of entry: qa_report, bug_pattern, release_note, triage (for 'store')
        app_name: App name for scoping (optional)
        severity: Severity rating: CRITICAL, HIGH, MEDIUM, LOW (for bug_pattern)
        query: Search query (for 'search' and 'patterns' actions)
        limit: Max results to return (default: 10)

    Returns:
        JSON string with the result
    """
    store = _get_store()
    now = datetime.now(timezone.utc).isoformat()

    if action == "store":
        if not content:
            return json.dumps({"error": "content is required for store action"})

        try:
            store.store_finding(
                content=content,
                source=app_name or "unknown",
                source_type=entry_type,
                topic=severity or "general",
                finding_type=entry_type,
                significance=severity or "unknown",
            )
            return json.dumps({
                "stored": True,
                "type": entry_type,
                "app": app_name,
                "timestamp": now,
            })
        except Exception as e:
            return json.dumps({"error": f"Failed to store: {e}"})

    elif action == "search":
        if not query:
            return json.dumps({"error": "query is required for search action"})

        try:
            results = store.search(query, limit=limit)
            return json.dumps({
                "results": results,
                "count": len(results),
                "query": query,
            })
        except Exception as e:
            return json.dumps({"error": f"Search failed: {e}", "results": []})

    elif action == "recent":
        try:
            results = store.get_recent(entry_type=entry_type, limit=limit)
            return json.dumps({
                "results": results,
                "count": len(results),
                "type": entry_type,
            })
        except Exception as e:
            return json.dumps({"error": f"Recent query failed: {e}", "results": []})

    elif action == "patterns":
        if not query:
            return json.dumps({"error": "query is required for patterns action"})

        try:
            results = store.search(query, limit=limit)
            bug_results = [r for r in results if r.get("source_type") == "bug_pattern"]
            return json.dumps({
                "patterns": bug_results,
                "count": len(bug_results),
                "query": query,
                "all_results": len(results),
            })
        except Exception as e:
            return json.dumps({"error": f"Pattern search failed: {e}", "patterns": []})

    else:
        return json.dumps({
            "error": f"Unknown action: {action}. Use: store, search, recent, patterns"
        })
