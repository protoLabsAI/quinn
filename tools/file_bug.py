"""File a bug on the protoLabs Studio board via Ava's API.

Quinn uses this after triaging a Discord/GitHub bug report to create
a tracked feature on the board and optionally notify the reporter.

Requires: PROTOLABS_SERVER_URL, PROTOLABS_API_KEY, PROTOLABS_PROJECT_PATH
"""

import os
from typing import Any

import httpx

from nanobot.agent.tools.base import Tool

_DEFAULT_SERVER_URL = "http://localhost:3008"
_REQUEST_TIMEOUT = 20


class FileBugTool(Tool):
    """Create a bug feature on the protoLabs Studio board via Ava."""

    @property
    def name(self) -> str:
        return "file_bug"

    @property
    def description(self) -> str:
        return (
            "File a bug report on the protoLabs Studio board. "
            "Use this after triaging a Discord or GitHub bug report. "
            "Creates a backlog feature with category=bug and returns the feature ID "
            "so you can link back to the reporter."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Short bug title (max 80 chars).",
                },
                "description": {
                    "type": "string",
                    "description": (
                        "Bug details: what happened, steps to reproduce, "
                        "expected vs actual behaviour, environment. "
                        "Markdown supported."
                    ),
                },
                "severity": {
                    "type": "string",
                    "enum": ["critical", "high", "medium", "low"],
                    "description": "Bug severity. Default: medium.",
                },
                "source": {
                    "type": "string",
                    "description": (
                        "Where the report came from, e.g. "
                        "'Discord #bug-reports by @username' or 'GitHub issue #42'."
                    ),
                },
            },
            "required": ["title", "description"],
        }

    async def execute(self, **kwargs: Any) -> str:
        title: str = kwargs["title"]
        description: str = kwargs["description"]
        severity: str = kwargs.get("severity", "medium")
        source: str = kwargs.get("source", "")

        server_url = os.environ.get("PROTOLABS_SERVER_URL", _DEFAULT_SERVER_URL)
        api_key = os.environ.get("PROTOLABS_API_KEY", "")
        project_path = os.environ.get("PROTOLABS_PROJECT_PATH", "")

        full_description = description
        if source:
            full_description += f"\n\n---\n**Source:** {source}"

        body: dict[str, Any] = {
            "projectPath": project_path,
            "feature": {
                "title": title,
                "description": full_description,
                "status": "backlog",
                "category": "bug",
                "metadata": {"severity": severity},
            },
        }

        try:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                resp = await client.post(
                    f"{server_url}/api/features/create",
                    json=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-API-Key": api_key,
                    },
                )
                resp.raise_for_status()
                data = resp.json()

            feature = data.get("feature", data)
            feature_id = feature.get("id", data.get("featureId", "unknown"))
            feature_title = feature.get("title", title)
            return (
                f"Bug filed: **{feature_title}** → `{feature_id}` "
                f"(severity={severity}, status=backlog)\n"
                f"Board URL: {server_url}/features/{feature_id}"
            )

        except httpx.HTTPStatusError as e:
            return f"Error filing bug: API returned {e.response.status_code} — {e.response.text[:300]}"
        except httpx.ConnectError:
            return f"Error: Cannot connect to {server_url}"
        except Exception as e:
            return f"Error: {type(e).__name__}: {e}"
