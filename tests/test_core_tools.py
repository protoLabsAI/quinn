"""Tests for board_monitor, file_bug, and qa_memory tools.

These are Quinn's core QA pipeline tools with zero prior test coverage.
board_monitor and file_bug talk to protoMaker over HTTP (mocked with
httpx_mock). qa_memory wraps KnowledgeStore (mocked at the store level).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from tools.board_monitor import BoardMonitorTool, _format_feature, _resolve_app
from tools.file_bug import FileBugTool
from tools.qa_memory import QAMemoryTool, _split_csv


# ── Helpers ───────────────────────────────────────────────────────────────────


def _mock_httpx_post(status_code=200, json_data=None, text=""):
    """Build a mock httpx.AsyncClient.post response."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.text = text or ""
    resp.json.return_value = json_data or {}
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            f"HTTP {status_code}", request=MagicMock(), response=resp,
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


# ── board_monitor ─────────────────────────────────────────────────────────────


def test_board_monitor_name_and_required_action():
    tool = BoardMonitorTool()
    assert tool.name == "board_monitor"
    assert "action" in tool.parameters.get("required", [])


def test_resolve_app_single_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("QA_APPS_CONFIG", raising=False)
    monkeypatch.setenv("PROTOLABS_SERVER_URL", "http://test:3008")
    monkeypatch.setenv("PROTOLABS_API_KEY", "test-key")
    config = _resolve_app("")
    assert config["server_url"] == "http://test:3008"
    assert config["api_key"] == "test-key"


def test_resolve_app_multi_requires_name(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("QA_APPS_CONFIG", '{"a":{"server_url":"x","api_key":"y"},"b":{"server_url":"x2","api_key":"y2"}}')
    with pytest.raises(ValueError, match="Specify app_name"):
        _resolve_app("")


def test_resolve_app_unknown_raises(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("QA_APPS_CONFIG", raising=False)
    monkeypatch.setenv("PROTOLABS_SERVER_URL", "http://test:3008")
    monkeypatch.setenv("PROTOLABS_API_KEY", "k")
    with pytest.raises(ValueError, match="Unknown app"):
        _resolve_app("nonexistent")


def test_format_feature_blocked_shows_reason():
    f = {"status": "blocked", "title": "Foo", "id": "f-1", "statusChangeReason": "git commit failed"}
    result = _format_feature(f)
    assert "Reason:" in result
    assert "git commit" in result


@pytest.mark.asyncio
async def test_board_monitor_health_check_success(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("QA_APPS_CONFIG", raising=False)
    monkeypatch.setenv("PROTOLABS_SERVER_URL", "http://test:3008")
    monkeypatch.setenv("PROTOLABS_API_KEY", "k")

    mock_resp = _mock_httpx_post(200, {"status": "ok", "uptime": 42, "version": "1.0"})

    async def _fake_get(self, url, **kwargs):
        return mock_resp

    with patch("httpx.AsyncClient.get", _fake_get):
        result = await BoardMonitorTool().execute(action="health_check")

    assert "ok" in result
    assert "42" in result


@pytest.mark.asyncio
async def test_board_monitor_connection_error(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("QA_APPS_CONFIG", raising=False)
    monkeypatch.setenv("PROTOLABS_SERVER_URL", "http://unreachable:9999")
    monkeypatch.setenv("PROTOLABS_API_KEY", "k")

    async def _raise_connect(self, url, **kwargs):
        raise httpx.ConnectError("connection refused")

    with patch("httpx.AsyncClient.get", _raise_connect):
        result = await BoardMonitorTool().execute(action="health_check")

    assert "Error" in result
    assert "Cannot connect" in result


@pytest.mark.asyncio
async def test_board_monitor_http_error(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("QA_APPS_CONFIG", raising=False)
    monkeypatch.setenv("PROTOLABS_SERVER_URL", "http://test:3008")
    monkeypatch.setenv("PROTOLABS_API_KEY", "bad-key")

    mock_resp = _mock_httpx_post(403, text="Forbidden")

    async def _fake_get(self, url, **kwargs):
        return mock_resp

    with patch("httpx.AsyncClient.get", _fake_get):
        result = await BoardMonitorTool().execute(action="health_check")

    assert "Error" in result
    assert "403" in result


@pytest.mark.asyncio
async def test_board_monitor_unknown_action():
    result = await BoardMonitorTool().execute(action="explode", app_name="default")
    assert "Error" in result
    assert "Unknown action" in result


@pytest.mark.asyncio
async def test_board_monitor_blocked_features_empty(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("QA_APPS_CONFIG", raising=False)
    monkeypatch.setenv("PROTOLABS_SERVER_URL", "http://test:3008")
    monkeypatch.setenv("PROTOLABS_API_KEY", "k")

    mock_resp = _mock_httpx_post(200, [
        {"status": "done", "title": "done-feat", "id": "f-1"},
    ])

    async def _fake_post(self, url, **kwargs):
        return mock_resp

    with patch("httpx.AsyncClient.post", _fake_post):
        result = await BoardMonitorTool().execute(action="blocked_features")

    assert "No blocked" in result


# ── file_bug ──────────────────────────────────────────────────────────────────


def test_file_bug_name_and_required_fields():
    tool = FileBugTool()
    assert tool.name == "file_bug"
    required = tool.parameters.get("required", [])
    assert "title" in required
    assert "description" in required


@pytest.mark.asyncio
async def test_file_bug_success(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PROTOLABS_SERVER_URL", "http://test:3008")
    monkeypatch.setenv("PROTOLABS_API_KEY", "k")
    monkeypatch.setenv("PROTOLABS_PROJECT_PATH", "/dev/ava")

    mock_resp = _mock_httpx_post(200, {
        "feature": {"id": "feat-abc", "title": "Button crash"}
    })

    async def _fake_post(self, url, **kwargs):
        return mock_resp

    with patch("httpx.AsyncClient.post", _fake_post):
        result = await FileBugTool().execute(
            title="Button crash", description="Safari only", severity="high",
        )

    assert "Bug filed:" in result
    assert "feat-abc" in result
    assert "severity=high" in result


@pytest.mark.asyncio
async def test_file_bug_source_appended_to_description(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PROTOLABS_SERVER_URL", "http://test:3008")
    monkeypatch.setenv("PROTOLABS_API_KEY", "k")
    monkeypatch.setenv("PROTOLABS_PROJECT_PATH", "/dev/ava")

    captured_body = {}

    async def _capture_post(self, url, **kwargs):
        captured_body.update(kwargs.get("json", {}))
        return _mock_httpx_post(200, {"feature": {"id": "f-1", "title": "x"}})

    with patch("httpx.AsyncClient.post", _capture_post):
        await FileBugTool().execute(
            title="Bug", description="details",
            source="Discord #bugs by @josh",
        )

    assert "Discord #bugs" in captured_body["feature"]["description"]


@pytest.mark.asyncio
async def test_file_bug_connection_error(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PROTOLABS_SERVER_URL", "http://unreachable:9999")
    monkeypatch.setenv("PROTOLABS_API_KEY", "k")

    async def _raise(self, url, **kwargs):
        raise httpx.ConnectError("refused")

    with patch("httpx.AsyncClient.post", _raise):
        result = await FileBugTool().execute(
            title="Bug", description="details",
        )

    assert "Error" in result
    assert "Cannot connect" in result


@pytest.mark.asyncio
async def test_file_bug_api_error(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PROTOLABS_SERVER_URL", "http://test:3008")
    monkeypatch.setenv("PROTOLABS_API_KEY", "bad")

    async def _fake_post(self, url, **kwargs):
        return _mock_httpx_post(401, text="Unauthorized")

    with patch("httpx.AsyncClient.post", _fake_post):
        result = await FileBugTool().execute(
            title="Bug", description="details",
        )

    assert "Error" in result
    assert "401" in result


# ── qa_memory ─────────────────────────────────────────────────────────────────


def _mock_store():
    store = MagicMock()
    store.add_report.return_value = True
    store.add_bug_pattern.return_value = True
    store.add_release_notes.return_value = True
    store.add_regression_test.return_value = True
    store.search.return_value = []
    store.get_reports.return_value = []
    store.get_bug_patterns.return_value = []
    store.get_release_notes.return_value = []
    store.get_regression_tests.return_value = []
    store.find_similar_bugs.return_value = []
    store.get_stats.return_value = {"qa_reports": 5, "bug_patterns": 3}
    return store


def test_qa_memory_name():
    assert QAMemoryTool().name == "qa_memory"


def test_split_csv():
    assert _split_csv("a, b, c") == ["a", "b", "c"]
    assert _split_csv("") == []
    assert _split_csv("  x  ") == ["x"]


@pytest.mark.asyncio
async def test_qa_memory_store_qa_report():
    store = _mock_store()
    tool = QAMemoryTool(store)
    result = await tool.execute(
        action="store", entry_type="qa_report",
        title="Weekly", summary="All good",
    )
    assert "Stored QA report" in result
    store.add_report.assert_called_once()


@pytest.mark.asyncio
async def test_qa_memory_store_missing_entry_type():
    tool = QAMemoryTool(_mock_store())
    result = await tool.execute(action="store")
    assert "Error" in result
    assert "entry_type" in result


@pytest.mark.asyncio
async def test_qa_memory_store_unknown_entry_type():
    tool = QAMemoryTool(_mock_store())
    result = await tool.execute(action="store", entry_type="garbage")
    assert "Error" in result
    assert "Unknown entry_type" in result


@pytest.mark.asyncio
async def test_qa_memory_store_missing_required_fields():
    tool = QAMemoryTool(_mock_store())
    result = await tool.execute(action="store", entry_type="qa_report", title="X")
    assert "Error" in result
    assert "summary" in result


@pytest.mark.asyncio
async def test_qa_memory_search_requires_query():
    tool = QAMemoryTool(_mock_store())
    result = await tool.execute(action="search")
    assert "Error" in result
    assert "query" in result


@pytest.mark.asyncio
async def test_qa_memory_search_returns_results():
    store = _mock_store()
    store.search.return_value = [
        {"table": "qa_reports", "source_id": 1, "distance": 0.1, "preview": "test report"},
    ]
    tool = QAMemoryTool(store)
    result = await tool.execute(action="search", query="test")
    assert "Search results" in result
    assert "test report" in result


@pytest.mark.asyncio
async def test_qa_memory_recent_requires_entry_type():
    tool = QAMemoryTool(_mock_store())
    result = await tool.execute(action="recent")
    assert "Error" in result
    assert "entry_type" in result


@pytest.mark.asyncio
async def test_qa_memory_recent_rejects_unknown_type():
    tool = QAMemoryTool(_mock_store())
    result = await tool.execute(action="recent", entry_type="invalid")
    assert "Error" in result
    assert "Unknown entry_type" in result


@pytest.mark.asyncio
async def test_qa_memory_stats():
    store = _mock_store()
    tool = QAMemoryTool(store)
    result = await tool.execute(action="stats")
    assert "Stats" in result
    assert "qa_reports: 5" in result
    assert "Total: 8" in result


@pytest.mark.asyncio
async def test_qa_memory_store_db_failure():
    store = _mock_store()
    store.add_report.return_value = False
    tool = QAMemoryTool(store)
    result = await tool.execute(
        action="store", entry_type="qa_report",
        title="X", summary="Y",
    )
    assert "Error" in result
    assert "DB unavailable" in result


@pytest.mark.asyncio
async def test_qa_memory_unknown_action():
    tool = QAMemoryTool(_mock_store())
    result = await tool.execute(action="explode")
    assert "Error" in result
    assert "Unknown action" in result


@pytest.mark.asyncio
async def test_qa_memory_patterns_requires_query():
    tool = QAMemoryTool(_mock_store())
    result = await tool.execute(action="patterns")
    assert "Error" in result
    assert "query" in result
