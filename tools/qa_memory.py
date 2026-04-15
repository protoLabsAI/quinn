"""QA memory tool for Quinn QA agent.

Thin wrapper around ``knowledge.store.KnowledgeStore`` that exposes the
store's operations as a single ``qa_memory`` tool for the LangGraph agent.
The store itself is the single source of truth for the schema; this module
only adapts parameter shapes into the store's method signatures.
"""

from typing import Any

from nanobot.agent.tools.base import Tool

from knowledge.store import KnowledgeStore

_TYPE_TABLE_MAP = {
    "qa_report": "qa_reports",
    "bug_pattern": "bug_patterns",
    "release_note": "release_notes",
    "regression_test": "regression_tests",
}


def _split_csv(raw: str) -> list[str]:
    """Split a comma-separated string into a cleaned list."""
    return [item.strip() for item in raw.split(",") if item.strip()] if raw else []


class QAMemoryTool(Tool):
    """QA knowledge store with semantic search for reports, bugs, and tests."""

    def __init__(self, store: KnowledgeStore | None = None):
        self._store = store or KnowledgeStore()

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
                "title": {"type": "string", "description": "Title for the entry."},
                "summary": {"type": "string", "description": "Summary text (for qa_report)."},
                "description": {
                    "type": "string",
                    "description": "Description (for bug_pattern, regression_test).",
                },
                "content": {"type": "string", "description": "Content text (for release_note)."},
                "version": {"type": "string", "description": "Version string (for release_note)."},
                "app_name": {
                    "type": "string",
                    "description": "Application name (for qa_report, bug_pattern, release_note, regression_test).",
                },
                "severity": {
                    "type": "string",
                    "enum": ["info", "low", "medium", "high", "critical",
                             "LOW", "MEDIUM", "HIGH", "CRITICAL"],
                    "description": "Severity level (for qa_report, bug_pattern).",
                },
                "category": {"type": "string", "description": "Bug category (for bug_pattern)."},
                "pattern": {
                    "type": "string",
                    "description": "Detection pattern (for bug_pattern) — grep pattern, endpoint, etc.",
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
        if action == "search":
            return self._handle_search(kwargs)
        if action == "recent":
            return self._handle_recent(kwargs)
        if action == "patterns":
            return self._handle_patterns(kwargs)
        if action == "stats":
            return self._handle_stats()
        return f"Error: Unknown action '{action}'."

    # --- Store dispatchers ---

    def _handle_store(self, kwargs: dict[str, Any]) -> str:
        entry_type = kwargs.get("entry_type", "")
        if not entry_type:
            return "Error: 'entry_type' is required for store action."
        if entry_type == "qa_report":
            return self._store_qa_report(kwargs)
        if entry_type == "bug_pattern":
            return self._store_bug_pattern(kwargs)
        if entry_type == "release_note":
            return self._store_release_note(kwargs)
        if entry_type == "regression_test":
            return self._store_regression_test(kwargs)
        types = ", ".join(_TYPE_TABLE_MAP.keys())
        return f"Error: Unknown entry_type '{entry_type}'. Use: {types}"

    def _store_qa_report(self, kwargs: dict[str, Any]) -> str:
        title = kwargs.get("title", "")
        summary = kwargs.get("summary", "")
        if not title or not summary:
            return "Error: 'title' and 'summary' are required for qa_report."
        ok = self._store.add_report(
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
            app_name=kwargs.get("app_name", ""),
            severity=kwargs.get("severity", "MEDIUM"),
            category=kwargs.get("category", ""),
            pattern=kwargs.get("pattern", ""),
            resolution=kwargs.get("resolution", ""),
            related_features=_split_csv(kwargs.get("related_features", "")),
        )
        return f"Stored bug pattern: {title}" if ok else "Error: Failed to store (DB unavailable)."

    def _store_release_note(self, kwargs: dict[str, Any]) -> str:
        version = kwargs.get("version", "")
        content = kwargs.get("content", "")
        if not version or not content:
            return "Error: 'version' and 'content' are required for release_note."
        ok = self._store.add_release_notes(
            app_name=kwargs.get("app_name", ""),
            version=version,
            content=content,
            title=kwargs.get("title", ""),
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
                f"- [{r['table']}] #{r['source_id']} (distance: {round(r['distance'], 4)})\n"
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
        entries = self._get_recent(entry_type, limit)
        if not entries:
            return f"No {entry_type} entries found."

        lines = [f"**Recent {entry_type} entries ({len(entries)}):**\n"]
        for entry in entries:
            entry_id = entry.get("id", "?")
            name = entry.get("title", entry.get("version", "Untitled"))
            lines.append(f"- #{entry_id}: {name}")

            if entry_type == "bug_pattern":
                freq = entry.get("occurrences", 0)
                resolved = "resolved" if entry.get("resolved") else "open"
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

    def _get_recent(self, entry_type: str, limit: int) -> list[dict[str, Any]]:
        """Route recent queries to the right store method by entry type."""
        if entry_type == "qa_report":
            return self._store.get_reports(limit=limit)
        if entry_type == "bug_pattern":
            return self._store.get_bug_patterns(unresolved_only=False, limit=limit)
        if entry_type == "release_note":
            return self._store.get_release_notes(limit=limit)
        if entry_type == "regression_test":
            return self._store.get_regression_tests(limit=limit)
        return []

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
                f"- Bug #{r['source_id']} (distance: {round(r['distance'], 4)})\n"
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
