"""Manage scheduled ceremonies (cron jobs) via protoWorkstacean's operations API.

Workstacean runs the actual scheduler (SchedulerPlugin → router → agent.skill.request).
Quinn uses this tool to CRUD ceremonies that trigger her own (or any agent's) skills
on a cron schedule — e.g. "run my qa digest every morning at 14:00 UTC".

Replaces Quinn's hand-rolled per-feature asyncio schedulers. One place to look when
asking "what runs on a cron in this system?".

Requires: WORKSTACEAN_URL (default http://workstacean:3000), WORKSTACEAN_API_KEY.
"""

from __future__ import annotations

import os
import re
from typing import Any

import httpx

from nanobot.agent.tools.base import Tool

_DEFAULT_SERVER_URL = "http://workstacean:3000"
_REQUEST_TIMEOUT = 15
_ID_PATTERN = re.compile(r"^[\w.\-]+$")


class ManageCronTool(Tool):
    """CRUD scheduled ceremonies on protoWorkstacean."""

    @property
    def name(self) -> str:
        return "manage_cron"

    @property
    def description(self) -> str:
        return (
            "Create, update, delete, list, or manually fire scheduled ceremonies "
            "(cron jobs) on protoWorkstacean. Ceremonies invoke an agent skill on a "
            "cron schedule and hot-reload within ~5s of any change.\n\n"
            "Actions:\n"
            "- list: List all ceremonies\n"
            "- create: Create a new ceremony (id, name, schedule, skill required)\n"
            "- update: Update an existing ceremony by id\n"
            "- delete: Delete a ceremony by id\n"
            "- run: Manually fire a ceremony now (for testing — ignores schedule)\n\n"
            "Schedule is a 5-field cron expression (min hr dom mon dow), UTC. "
            "Targets defaults to ['all']; set ['quinn'] to route to just me."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "create", "update", "delete", "run"],
                    "description": "CRUD operation to perform.",
                },
                "id": {
                    "type": "string",
                    "description": (
                        "Ceremony id (alphanumeric, dots, dashes). Required for "
                        "create, update, delete, run. e.g. 'quinn.daily-digest'."
                    ),
                },
                "name": {
                    "type": "string",
                    "description": "Human-readable name. Required for create.",
                },
                "schedule": {
                    "type": "string",
                    "description": (
                        "Cron expression in UTC, e.g. '0 14 * * *' (14:00 daily). "
                        "Required for create."
                    ),
                },
                "skill": {
                    "type": "string",
                    "description": (
                        "Skill id to invoke when the ceremony fires, e.g. 'qa_report'. "
                        "Must be a skill advertised by the target agent's card. "
                        "Required for create."
                    ),
                },
                "targets": {
                    "type": "string",
                    "description": (
                        "Comma-separated agent names that should receive the skill "
                        "request. Default: 'all'. e.g. 'quinn' or 'quinn,ava'."
                    ),
                },
                "enabled": {
                    "type": "boolean",
                    "description": "Enable or disable the ceremony. Default: true.",
                },
                "notifyChannel": {
                    "type": "string",
                    "description": "Discord channel ID to notify when the ceremony fires.",
                },
            },
            "required": ["action"],
        }

    async def execute(self, **kwargs: Any) -> str:
        action = kwargs["action"]
        server_url = os.environ.get("WORKSTACEAN_URL", _DEFAULT_SERVER_URL)
        api_key = os.environ.get("WORKSTACEAN_API_KEY", "")

        try:
            if action == "list":
                return await self._list(server_url, api_key)
            if action == "create":
                return await self._create(server_url, api_key, kwargs)
            if action == "update":
                return await self._update(server_url, api_key, kwargs)
            if action == "delete":
                return await self._delete(server_url, api_key, kwargs)
            if action == "run":
                return await self._run(server_url, api_key, kwargs)
            return f"Error: Unknown action '{action}'."
        except httpx.HTTPStatusError as e:
            return f"Error: Workstacean returned {e.response.status_code} — {e.response.text[:300]}"
        except httpx.ConnectError:
            return f"Error: Cannot connect to Workstacean at {server_url}"
        except Exception as e:
            return f"Error: {type(e).__name__}: {e}"

    # ── actions ───────────────────────────────────────────────────────────────

    async def _list(self, server_url: str, api_key: str) -> str:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            resp = await client.get(
                f"{server_url}/api/ceremonies",
                headers=_headers(api_key),
            )
            resp.raise_for_status()
        data = resp.json().get("data", [])
        if not data:
            return "No ceremonies registered."

        lines = [f"**Ceremonies ({len(data)}):**"]
        for c in data:
            status = "✅" if c.get("enabled", True) else "⏸️"
            targets = ",".join(c.get("targets", [])) or "all"
            lines.append(
                f"- {status} `{c.get('id','?')}` — {c.get('name','(unnamed)')} "
                f"| `{c.get('schedule','?')}` → `{c.get('skill','?')}` (targets: {targets})"
            )
        return "\n".join(lines)

    async def _create(
        self, server_url: str, api_key: str, kwargs: dict[str, Any],
    ) -> str:
        id_ = kwargs.get("id", "").strip()
        name = kwargs.get("name", "").strip()
        schedule = kwargs.get("schedule", "").strip()
        skill = kwargs.get("skill", "").strip()

        if not all([id_, name, schedule, skill]):
            return "Error: create requires id, name, schedule, and skill."
        if not _ID_PATTERN.match(id_):
            return f"Error: id '{id_}' invalid — alphanumeric, dots, and dashes only."

        body = {
            "id": id_,
            "name": name,
            "schedule": schedule,
            "skill": skill,
            "targets": _split_csv(kwargs.get("targets", "")) or ["all"],
            "enabled": kwargs.get("enabled", True),
        }
        notify = kwargs.get("notifyChannel", "").strip()
        if notify:
            body["notifyChannel"] = notify

        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            resp = await client.post(
                f"{server_url}/api/ceremonies/create",
                json=body,
                headers=_headers(api_key),
            )
            resp.raise_for_status()

        return (
            f"Ceremony created: `{id_}` — {name} | `{schedule}` → `{skill}` "
            f"(targets: {', '.join(body['targets'])})"
        )

    async def _update(
        self, server_url: str, api_key: str, kwargs: dict[str, Any],
    ) -> str:
        id_ = kwargs.get("id", "").strip()
        if not id_:
            return "Error: update requires id."
        if not _ID_PATTERN.match(id_):
            return f"Error: id '{id_}' invalid — alphanumeric, dots, and dashes only."

        body: dict[str, Any] = {}
        for key in ("name", "schedule", "skill", "notifyChannel"):
            value = kwargs.get(key, "")
            if value:
                body[key] = value
        if "enabled" in kwargs:
            body["enabled"] = kwargs["enabled"]
        targets_raw = kwargs.get("targets", "")
        if targets_raw:
            body["targets"] = _split_csv(targets_raw)

        if not body:
            return "Error: update requires at least one of name, schedule, skill, targets, enabled, notifyChannel."

        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            resp = await client.post(
                f"{server_url}/api/ceremonies/{id_}/update",
                json=body,
                headers=_headers(api_key),
            )
            resp.raise_for_status()

        changed = ", ".join(body.keys())
        return f"Ceremony `{id_}` updated ({changed})."

    async def _delete(
        self, server_url: str, api_key: str, kwargs: dict[str, Any],
    ) -> str:
        id_ = kwargs.get("id", "").strip()
        if not id_:
            return "Error: delete requires id."
        if not _ID_PATTERN.match(id_):
            return f"Error: id '{id_}' invalid — alphanumeric, dots, and dashes only."

        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            resp = await client.post(
                f"{server_url}/api/ceremonies/{id_}/delete",
                json={},
                headers=_headers(api_key),
            )
            resp.raise_for_status()
        return f"Ceremony `{id_}` deleted."

    async def _run(
        self, server_url: str, api_key: str, kwargs: dict[str, Any],
    ) -> str:
        id_ = kwargs.get("id", "").strip()
        if not id_:
            return "Error: run requires id."
        if not _ID_PATTERN.match(id_):
            return f"Error: id '{id_}' invalid — alphanumeric, dots, and dashes only."

        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            resp = await client.post(
                f"{server_url}/api/ceremonies/{id_}/run",
                json={},
                headers=_headers(api_key),
            )
            resp.raise_for_status()
        return f"Ceremony `{id_}` triggered manually (ignores schedule)."


def _headers(api_key: str) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    return headers


def _split_csv(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()] if raw else []
