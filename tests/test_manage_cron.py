"""Tests for the manage_cron tool — HTTP surface against Workstacean's
/api/ceremonies/* endpoints.

Mocks httpx so tests don't require a live Workstacean.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from tools.manage_cron import ManageCronTool


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _fake_response(status: int = 200, body: dict | None = None) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.json.return_value = body or {"success": True, "data": []}
    resp.raise_for_status = MagicMock()
    if status >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            f"HTTP {status}", request=MagicMock(), response=resp,
        )
    return resp


class _FakeClient:
    """Minimal async context-manager drop-in for httpx.AsyncClient."""

    def __init__(self, response: MagicMock) -> None:
        self.response = response
        self.last_get = None
        self.last_post = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def get(self, url, headers=None):
        self.last_get = {"url": url, "headers": headers}
        return self.response

    async def post(self, url, json=None, headers=None):
        self.last_post = {"url": url, "json": json, "headers": headers}
        return self.response


def test_schema_lists_all_actions() -> None:
    tool = ManageCronTool()
    params = tool.parameters
    assert params["required"] == ["action"]
    assert set(params["properties"]["action"]["enum"]) == {
        "list", "create", "update", "delete", "run",
    }


def test_list_returns_empty_message_when_no_ceremonies(monkeypatch) -> None:
    fake = _FakeClient(_fake_response(body={"success": True, "data": []}))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_: fake)
    tool = ManageCronTool()
    result = _run(tool.execute(action="list"))
    assert "No ceremonies registered" in result


def test_list_formats_entries(monkeypatch) -> None:
    fake = _FakeClient(_fake_response(body={"success": True, "data": [
        {"id": "quinn.daily-digest", "name": "Daily Digest",
         "schedule": "0 14 * * *", "skill": "qa_report",
         "targets": ["quinn"], "enabled": True},
        {"id": "board.health", "name": "Board Health",
         "schedule": "*/30 * * * *", "skill": "board_audit",
         "targets": ["all"], "enabled": False},
    ]}))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_: fake)
    tool = ManageCronTool()
    result = _run(tool.execute(action="list"))
    assert "quinn.daily-digest" in result
    assert "board.health" in result
    assert "0 14 * * *" in result
    # disabled ceremony marked pause
    assert "⏸️" in result
    assert "✅" in result


def test_create_requires_all_four_fields() -> None:
    tool = ManageCronTool()
    result = _run(tool.execute(action="create", id="x", name="x", schedule="x"))
    assert "Error" in result and "skill" in result.lower()


def test_create_rejects_bad_id() -> None:
    tool = ManageCronTool()
    result = _run(tool.execute(
        action="create", id="has spaces!", name="n",
        schedule="0 14 * * *", skill="qa_report",
    ))
    assert "Error" in result
    assert "alphanumeric" in result


def test_create_posts_correct_body(monkeypatch) -> None:
    fake = _FakeClient(_fake_response(body={"success": True, "data": {"id": "q.d"}}))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_: fake)
    monkeypatch.setenv("WORKSTACEAN_URL", "http://ws:3000")
    monkeypatch.setenv("WORKSTACEAN_API_KEY", "secret")

    tool = ManageCronTool()
    result = _run(tool.execute(
        action="create", id="quinn.daily", name="Quinn Daily",
        schedule="0 14 * * *", skill="qa_report",
        targets="quinn,ava", enabled=True, notifyChannel="1234",
    ))

    assert fake.last_post["url"] == "http://ws:3000/api/ceremonies/create"
    assert fake.last_post["headers"]["X-API-Key"] == "secret"
    body = fake.last_post["json"]
    assert body["id"] == "quinn.daily"
    assert body["schedule"] == "0 14 * * *"
    assert body["skill"] == "qa_report"
    assert body["targets"] == ["quinn", "ava"]
    assert body["notifyChannel"] == "1234"
    assert body["enabled"] is True
    assert "Ceremony created" in result


def test_create_targets_defaults_to_all(monkeypatch) -> None:
    fake = _FakeClient(_fake_response(body={"success": True}))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_: fake)
    tool = ManageCronTool()
    _run(tool.execute(
        action="create", id="q.d", name="Q",
        schedule="0 0 * * *", skill="s",
    ))
    assert fake.last_post["json"]["targets"] == ["all"]


def test_update_requires_id() -> None:
    tool = ManageCronTool()
    result = _run(tool.execute(action="update", schedule="0 0 * * *"))
    assert "Error" in result and "id" in result.lower()


def test_update_errors_with_empty_body() -> None:
    tool = ManageCronTool()
    result = _run(tool.execute(action="update", id="q.d"))
    assert "Error" in result
    assert "at least one" in result.lower()


def test_update_hits_expected_endpoint(monkeypatch) -> None:
    fake = _FakeClient(_fake_response(body={"success": True}))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_: fake)
    monkeypatch.setenv("WORKSTACEAN_URL", "http://ws:3000")
    tool = ManageCronTool()
    result = _run(tool.execute(
        action="update", id="quinn.daily", schedule="0 9 * * *",
    ))
    assert fake.last_post["url"] == "http://ws:3000/api/ceremonies/quinn.daily/update"
    assert fake.last_post["json"] == {"schedule": "0 9 * * *"}
    assert "updated" in result.lower()


def test_delete_hits_expected_endpoint(monkeypatch) -> None:
    fake = _FakeClient(_fake_response(body={"success": True}))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_: fake)
    monkeypatch.setenv("WORKSTACEAN_URL", "http://ws:3000")
    tool = ManageCronTool()
    result = _run(tool.execute(action="delete", id="quinn.daily"))
    assert fake.last_post["url"] == "http://ws:3000/api/ceremonies/quinn.daily/delete"
    assert "deleted" in result.lower()


def test_run_hits_expected_endpoint(monkeypatch) -> None:
    fake = _FakeClient(_fake_response(body={"success": True}))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_: fake)
    monkeypatch.setenv("WORKSTACEAN_URL", "http://ws:3000")
    tool = ManageCronTool()
    result = _run(tool.execute(action="run", id="quinn.daily"))
    assert fake.last_post["url"] == "http://ws:3000/api/ceremonies/quinn.daily/run"
    assert "triggered" in result.lower()


def test_http_error_surfaces_cleanly(monkeypatch) -> None:
    fake = _FakeClient(_fake_response(status=401, body={"success": False, "error": "Unauthorized"}))
    fake.response.text = '{"success":false,"error":"Unauthorized"}'
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_: fake)
    tool = ManageCronTool()
    result = _run(tool.execute(action="list"))
    assert "Error" in result
    assert "401" in result


def test_connection_error_surfaces_cleanly(monkeypatch) -> None:
    class _BoomClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): return None
        async def get(self, *a, **k):
            raise httpx.ConnectError("Connection refused")
        async def post(self, *a, **k):
            raise httpx.ConnectError("Connection refused")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **_: _BoomClient())
    tool = ManageCronTool()
    result = _run(tool.execute(action="list"))
    assert "Cannot connect" in result


def test_registered_in_lg_tools() -> None:
    """Lock the wire-up so the tool is exposed to the LangGraph agent.
    Source-text check — avoids importing langchain_core in the test env."""
    from pathlib import Path
    src = Path(__file__).parents[1] / "tools" / "lg_tools.py"
    text = src.read_text()
    assert "from tools.manage_cron import ManageCronTool" in text
    assert "_manage_cron = ManageCronTool()" in text
    # manage_cron must appear in the get_all_tools registry list, not just
    # as a function definition
    registry = text.split("def get_all_tools")[1]
    assert "manage_cron," in registry


def test_daily_digest_scheduler_removed_from_discord_bot() -> None:
    """Structural guard: ensure the old scheduler pipeline stays gone so
    we don't end up with two competing cron paths. Source-text check —
    avoids side effects from importing discord_bot in the test env."""
    from pathlib import Path
    src = Path(__file__).parents[1] / "discord_bot.py"
    text = src.read_text()
    for sym in (
        "def _daily_digest_scheduler",
        "async def post_daily_digest",
        "_DIGEST_HOUR",
        "_DIGEST_CHANNEL_ID",
        "_digest_scheduler_running",
    ):
        assert sym not in text, (
            f"{sym!r} still present in discord_bot.py — the daily-digest "
            "pipeline was intentionally removed in favor of Workstacean "
            "ceremonies. Re-introducing it will resurrect the split-brain "
            "scheduler bug."
        )
