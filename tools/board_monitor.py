"""protoLabs Studio board monitor for Quinn QA agent.

Connects to the protoLabs Studio API to inspect board state, feature health,
and blocked/review queues across multiple configured applications.

Requires:
- PROTOLABS_SERVER_URL (default: http://localhost:3008)
- PROTOLABS_API_KEY
- QA_APPS_CONFIG (optional JSON string mapping app names to {server_url, api_key, project_path})
"""

import json
import os
from typing import Any

import httpx

from nanobot.agent.tools.base import Tool

_DEFAULT_SERVER_URL = "http://localhost:3008"
_REQUEST_TIMEOUT = 20


def _load_apps_config() -> dict[str, dict[str, str]]:
    """Load multi-app configuration from QA_APPS_CONFIG env var.

    Expected format:
    {
        "ava": {"server_url": "http://localhost:3008", "api_key": "...", "project_path": "/home/josh/dev/ava"},
        "homeMaker": {"server_url": "http://localhost:3009", "api_key": "...", "project_path": "/home/josh/dev/labs/homeMaker"}
    }

    Falls back to a single default app using PROTOLABS_SERVER_URL and PROTOLABS_API_KEY.
    """
    raw = os.environ.get("QA_APPS_CONFIG", "")
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass

    return {
        "default": {
            "server_url": os.environ.get("PROTOLABS_SERVER_URL", _DEFAULT_SERVER_URL),
            "api_key": os.environ.get("PROTOLABS_API_KEY", ""),
            "project_path": os.environ.get("PROTOLABS_PROJECT_PATH", ""),
        }
    }


def _resolve_app(app_name: str) -> dict[str, str]:
    """Resolve an app name to its connection config.

    Returns dict with server_url, api_key, project_path.
    Raises ValueError if the app is not found.
    """
    apps = _load_apps_config()

    if app_name and app_name in apps:
        return apps[app_name]

    if not app_name and len(apps) == 1:
        return next(iter(apps.values()))

    if not app_name:
        raise ValueError(
            f"Multiple apps configured. Specify app_name: {', '.join(sorted(apps.keys()))}"
        )

    raise ValueError(
        f"Unknown app '{app_name}'. Available: {', '.join(sorted(apps.keys()))}"
    )


def _headers(api_key: str) -> dict[str, str]:
    """Build request headers with API key authentication."""
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    return headers


async def _post(
    server_url: str, path: str, api_key: str, body: dict[str, Any] | None = None
) -> dict[str, Any] | list[Any]:
    """POST to a protoLabs Studio API endpoint."""
    async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
        resp = await client.post(
            f"{server_url}{path}",
            json=body or {},
            headers=_headers(api_key),
        )
        resp.raise_for_status()
        return resp.json()


async def _get(server_url: str, path: str, api_key: str) -> dict[str, Any]:
    """GET from a protoLabs Studio API endpoint."""
    async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
        resp = await client.get(
            f"{server_url}{path}",
            headers=_headers(api_key),
        )
        resp.raise_for_status()
        return resp.json()


def _format_feature(f: dict[str, Any]) -> str:
    """Format a single feature as a concise one-liner."""
    status = f.get("status", "unknown")
    title = f.get("title", "Untitled")
    feature_id = f.get("id", "?")
    pr = f.get("prNumber")
    reason = f.get("statusChangeReason", "")

    line = f"- [{status}] {title} ({feature_id})"
    if pr:
        line += f" PR#{pr}"
    if reason and status == "blocked":
        line += f"\n  Reason: {reason[:150]}"
    return line


def _format_features_by_status(features: list[dict[str, Any]]) -> str:
    """Group and format features by status."""
    by_status: dict[str, list[dict[str, Any]]] = {}
    for f in features:
        status = f.get("status", "unknown")
        by_status.setdefault(status, []).append(f)

    lines = [f"**Board: {len(features)} features total**\n"]
    for status in ("in_progress", "review", "blocked", "backlog", "done"):
        group = by_status.pop(status, [])
        if group:
            lines.append(f"### {status} ({len(group)})")
            for f in group:
                lines.append(_format_feature(f))
            lines.append("")

    # Any remaining statuses
    for status, group in sorted(by_status.items()):
        if group:
            lines.append(f"### {status} ({len(group)})")
            for f in group:
                lines.append(_format_feature(f))
            lines.append("")

    return "\n".join(lines)


class BoardMonitorTool(Tool):
    """Inspect protoLabs Studio board state, blocked features, and review queues."""

    @property
    def name(self) -> str:
        return "board_monitor"

    @property
    def description(self) -> str:
        return (
            "Monitor protoLabs Studio boards across multiple applications.\n\n"
            "Actions:\n"
            "- sitrep: Full situation report (board state, agents, queue, recent activity)\n"
            "- board_summary: Feature counts grouped by status\n"
            "- blocked_features: List features with status=blocked and their reasons\n"
            "- review_queue: Features in review status (PRs awaiting merge)\n"
            "- health_check: Server health and connectivity check\n\n"
            "Supports multiple apps via app_name parameter."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "sitrep",
                        "board_summary",
                        "blocked_features",
                        "review_queue",
                        "health_check",
                    ],
                    "description": "Action to perform.",
                },
                "app_name": {
                    "type": "string",
                    "description": (
                        "Application to query. Maps to QA_APPS_CONFIG entries. "
                        "Omit if only one app is configured."
                    ),
                },
            },
            "required": ["action"],
        }

    async def execute(self, **kwargs: Any) -> str:
        action = kwargs["action"]
        app_name = kwargs.get("app_name", "")

        try:
            config = _resolve_app(app_name)
        except ValueError as e:
            return f"Error: {e}"

        server_url = config["server_url"]
        api_key = config["api_key"]
        project_path = config.get("project_path", "")

        try:
            if action == "sitrep":
                return await self._sitrep(server_url, api_key, project_path)
            elif action == "board_summary":
                return await self._board_summary(server_url, api_key, project_path)
            elif action == "blocked_features":
                return await self._blocked_features(server_url, api_key, project_path)
            elif action == "review_queue":
                return await self._review_queue(server_url, api_key, project_path)
            elif action == "health_check":
                return await self._health_check(server_url, api_key)
            else:
                return f"Error: Unknown action '{action}'."
        except httpx.HTTPStatusError as e:
            return f"Error: API returned {e.response.status_code} — {e.response.text[:300]}"
        except httpx.ConnectError:
            return f"Error: Cannot connect to {server_url}. Is the server running?"
        except Exception as e:
            return f"Error: {type(e).__name__}: {e}"

    async def _sitrep(
        self, server_url: str, api_key: str, project_path: str
    ) -> str:
        """Full situation report from the sitrep endpoint."""
        body: dict[str, Any] = {}
        if project_path:
            body["projectPath"] = project_path

        data = await _post(server_url, "/api/sitrep", api_key, body)

        if isinstance(data, dict):
            return f"**Situation Report**\n\n```json\n{json.dumps(data, indent=2)[:4000]}\n```"
        return str(data)[:4000]

    async def _board_summary(
        self, server_url: str, api_key: str, project_path: str
    ) -> str:
        """Feature list grouped by status."""
        body: dict[str, Any] = {}
        if project_path:
            body["projectPath"] = project_path

        data = await _post(server_url, "/api/features/list", api_key, body)
        features = data if isinstance(data, list) else data.get("features", [])
        return _format_features_by_status(features)

    async def _blocked_features(
        self, server_url: str, api_key: str, project_path: str
    ) -> str:
        """Filter board to only blocked features."""
        body: dict[str, Any] = {}
        if project_path:
            body["projectPath"] = project_path

        data = await _post(server_url, "/api/features/list", api_key, body)
        features = data if isinstance(data, list) else data.get("features", [])
        blocked = [f for f in features if f.get("status") == "blocked"]

        if not blocked:
            return "No blocked features found."

        lines = [f"**{len(blocked)} Blocked Feature(s):**\n"]
        for f in blocked:
            lines.append(_format_feature(f))
            # Include extra context for blocked features
            failure_count = f.get("failureCount", 0)
            assignee = f.get("assignee", "")
            if failure_count:
                lines.append(f"  Failures: {failure_count}")
            if assignee:
                lines.append(f"  Assignee: {assignee}")
        return "\n".join(lines)

    async def _review_queue(
        self, server_url: str, api_key: str, project_path: str
    ) -> str:
        """Filter board to features in review status."""
        body: dict[str, Any] = {}
        if project_path:
            body["projectPath"] = project_path

        data = await _post(server_url, "/api/features/list", api_key, body)
        features = data if isinstance(data, list) else data.get("features", [])
        in_review = [f for f in features if f.get("status") == "review"]

        if not in_review:
            return "No features in review."

        lines = [f"**{len(in_review)} Feature(s) in Review:**\n"]
        for f in in_review:
            title = f.get("title", "Untitled")
            pr = f.get("prNumber")
            branch = f.get("branch", "")
            line = f"- {title}"
            if pr:
                line += f" (PR#{pr})"
            if branch:
                line += f" [{branch}]"
            lines.append(line)
        return "\n".join(lines)

    async def _health_check(self, server_url: str, api_key: str) -> str:
        """Server health check."""
        data = await _get(server_url, "/api/health", api_key)
        status = data.get("status", "unknown")
        uptime = data.get("uptime", "?")
        version = data.get("version", "?")
        return (
            f"**Health: {status}**\n"
            f"Version: {version}\n"
            f"Uptime: {uptime}s\n"
            f"Server: {server_url}"
        )
