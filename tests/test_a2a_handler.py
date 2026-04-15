"""Tests for a2a_handler — task store, background runner, webhook delivery, routes."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_asyncio

from a2a_handler import (
    CANCELED,
    COMPLETED,
    FAILED,
    SUBMITTED,
    WORKING,
    A2ATaskStore,
    PushNotificationConfig,
    TaskRecord,
    _build_artifact_event,
    _build_status_event,
    _deliver_webhook,
    _now_iso,
    _run_task_background,
    _store,
    register_a2a_routes,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _make_record(**kwargs) -> TaskRecord:
    now = _now_iso()
    defaults = dict(
        id="test-task-id",
        context_id="test-ctx",
        state=SUBMITTED,
        created_at=now,
        updated_at=now,
        message_text="hello",
    )
    defaults.update(kwargs)
    return TaskRecord(**defaults)


@pytest.fixture
def store() -> A2ATaskStore:
    return A2ATaskStore()


@pytest.fixture(autouse=True)
def _reset_module_store():
    """Clear the module-level _store between tests.

    Route-integration tests exercise register_a2a_routes() which always uses
    the module singleton, so tasks from one test would otherwise leak into
    the next. Clearing on entry and exit keeps tests hermetic.
    """
    _store._tasks.clear()
    yield
    _store._tasks.clear()


# ── Task store ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_store_create_and_get(store):
    record = _make_record()
    await store.create(record)
    fetched = await store.get("test-task-id")
    assert fetched is record


@pytest.mark.asyncio
async def test_store_get_missing(store):
    assert await store.get("no-such-id") is None


@pytest.mark.asyncio
async def test_store_update_state(store):
    record = _make_record()
    await store.create(record)
    updated = await store.update_state("test-task-id", WORKING)
    assert updated.state == WORKING
    # Fetch again to confirm persistence
    assert (await store.get("test-task-id")).state == WORKING


@pytest.mark.asyncio
async def test_store_update_accumulated_text(store):
    record = _make_record()
    await store.create(record)
    await store.update_state("test-task-id", WORKING, accumulated_text="hello world")
    assert (await store.get("test-task-id")).accumulated_text == "hello world"


@pytest.mark.asyncio
async def test_store_update_event_rotated(store):
    """Subscribers waiting on the old event wake up; new event is fresh."""
    record = _make_record()
    await store.create(record)
    old_event = record._update_event
    await store.update_state("test-task-id", WORKING)
    # Old event should be set (woke up subscribers)
    assert old_event.is_set()
    # New event should be a different, unset object
    new_record = await store.get("test-task-id")
    assert new_record._update_event is not old_event
    assert not new_record._update_event.is_set()


@pytest.mark.asyncio
async def test_store_cancel_sets_event(store):
    record = _make_record(state=WORKING)
    record._bg_task = None
    await store.create(record)
    result = await store.cancel("test-task-id")
    assert result is True
    assert record._cancel_event.is_set()


@pytest.mark.asyncio
async def test_store_cancel_missing(store):
    assert await store.cancel("no-such-id") is False


# ── Eviction ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cleanup_removes_old_terminal_tasks(store):
    """Terminal tasks older than the TTL are evicted; recent ones survive."""
    from datetime import datetime, timedelta, timezone

    old_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    recent_ts = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()

    old = _make_record(id="old", state=COMPLETED)
    old.updated_at = old_ts
    recent = _make_record(id="recent", state=COMPLETED)
    recent.updated_at = recent_ts

    await store.create(old)
    await store.create(recent)

    removed = await store.cleanup_expired(ttl_seconds=3600)  # 1 hour
    assert removed == 1
    assert await store.get("old") is None
    assert await store.get("recent") is not None


@pytest.mark.asyncio
async def test_cleanup_spares_working_tasks_regardless_of_age(store):
    """A task that's been WORKING for hours must stay — only terminal tasks age out."""
    from datetime import datetime, timedelta, timezone

    stale_working = _make_record(id="stuck", state=WORKING)
    stale_working.updated_at = (
        datetime.now(timezone.utc) - timedelta(days=7)
    ).isoformat()
    await store.create(stale_working)

    removed = await store.cleanup_expired(ttl_seconds=60)
    assert removed == 0
    assert await store.get("stuck") is not None


@pytest.mark.asyncio
async def test_start_cleanup_is_idempotent(store):
    """Calling start_cleanup repeatedly must not spawn multiple loops — the
    route handlers call it on every request, so this property matters."""
    store.start_cleanup(interval_s=60, ttl_s=60)
    first = store._cleanup_task
    store.start_cleanup(interval_s=60, ttl_s=60)
    second = store._cleanup_task
    assert first is second
    first.cancel()
    try:
        await first
    except asyncio.CancelledError:
        pass


# ── Background task runner ────────────────────────────────────────────────────


async def _mock_stream(*events):
    """Helper: yields (event_type, payload) tuples."""
    for event in events:
        yield event
        await asyncio.sleep(0)  # yield control


@pytest.mark.asyncio
async def test_background_runner_success():
    store = A2ATaskStore()
    record = _make_record(id="bg-test")
    await store.create(record)

    push_calls = []

    async def _push(r):
        push_calls.append(r.state)

    stream_fn = lambda: _mock_stream(("text", "hello "), ("text", "world"), ("done", "hello world"))

    with patch("a2a_handler._store", store):
        await _run_task_background("bg-test", stream_fn, _push)

    final = await store.get("bg-test")
    assert final.state == COMPLETED
    assert final.accumulated_text == "hello world"
    assert WORKING in push_calls
    assert COMPLETED in push_calls


@pytest.mark.asyncio
async def test_background_runner_error():
    store = A2ATaskStore()
    record = _make_record(id="bg-err")
    await store.create(record)

    push_calls = []

    async def _push(r):
        push_calls.append(r.state)

    stream_fn = lambda: _mock_stream(("text", "partial"), ("error", "boom"))

    with patch("a2a_handler._store", store):
        await _run_task_background("bg-err", stream_fn, _push)

    final = await store.get("bg-err")
    assert final.state == FAILED
    assert final.error_message == "boom"
    assert FAILED in push_calls


@pytest.mark.asyncio
async def test_background_runner_cancel():
    store = A2ATaskStore()
    record = _make_record(id="bg-cancel")
    await store.create(record)

    # Pre-set the cancel event so the runner exits immediately
    record._cancel_event.set()

    async def _push(r):
        pass

    stream_fn = lambda: _mock_stream(("text", "should not process"))

    with patch("a2a_handler._store", store):
        await _run_task_background("bg-cancel", stream_fn, _push)

    final = await store.get("bg-cancel")
    assert final.state == CANCELED


# ── Webhook delivery ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_webhook_delivery_success():
    record = _make_record(state=COMPLETED, accumulated_text="result text")
    cfg = PushNotificationConfig(url="https://example.com/hook", token="tok123")

    with patch("a2a_handler.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        await _deliver_webhook(record, cfg)

        mock_client.post.assert_called_once()
        call_kwargs = mock_client.post.call_args
        assert call_kwargs.kwargs["headers"]["Authorization"] == "Bearer tok123"
        payload = call_kwargs.kwargs["json"]
        assert payload["task_id"] == "test-task-id"
        assert payload["status"]["state"] == COMPLETED
        # Completed tasks include the artifact
        assert "artifact" in payload


@pytest.mark.asyncio
async def test_webhook_delivery_no_token():
    record = _make_record(state=FAILED, error_message="oops")
    cfg = PushNotificationConfig(url="https://example.com/hook")

    with patch("a2a_handler.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        await _deliver_webhook(record, cfg)

        call_kwargs = mock_client.post.call_args
        assert "Authorization" not in call_kwargs.kwargs["headers"]


# ── Event builders ────────────────────────────────────────────────────────────


def test_build_status_event():
    record = _make_record(state=WORKING)
    evt = _build_status_event(record)
    assert evt["task_id"] == "test-task-id"
    assert evt["context_id"] == "test-ctx"
    assert evt["status"]["state"] == WORKING


def test_build_artifact_event():
    record = _make_record(accumulated_text="some output")
    evt = _build_artifact_event(record, last_chunk=True)
    assert evt["artifact"]["parts"][0]["text"] == "some output"
    assert evt["last_chunk"] is True
    assert evt["append"] is True


# ── Route integration (FastAPI ASGI test client) ──────────────────────────────


def _make_test_app():
    """Create a minimal FastAPI app with A2A routes wired in."""
    from fastapi import FastAPI

    app = FastAPI()
    card = {"name": "test", "capabilities": {}}

    async def _fake_stream(text, context_id):
        yield ("text", "hello ")
        yield ("text", "world")
        yield ("done", "hello world")

    async def _fake_chat(text, session_id):
        return [{"role": "assistant", "content": "response"}]

    register_a2a_routes(
        app=app,
        chat_stream_fn_factory=_fake_stream,
        chat_fn=_fake_chat,
        api_key="",
        agent_card=card,
    )
    return app, card


@pytest.mark.asyncio
async def test_agent_card_capabilities():
    app, card = _make_test_app()
    assert card["capabilities"]["streaming"] is True
    assert card["capabilities"]["pushNotifications"] is True


@pytest.mark.asyncio
async def test_message_send_returns_submitted():
    """message/send must return submitted state immediately (not block)."""
    app, _ = _make_test_app()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/a2a",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "message/send",
                "params": {"message": {"parts": [{"kind": "text", "text": "hi"}]}},
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["result"]["status"]["state"] == SUBMITTED
    assert "id" in data["result"]


@pytest.mark.asyncio
async def test_get_task_unknown_returns_404():
    app, _ = _make_test_app()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/tasks/no-such-id")

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_task_after_submit():
    """After message/send, GET /tasks/{id} returns the task record."""
    app, _ = _make_test_app()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        send_resp = await client.post(
            "/a2a",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "message/send",
                "params": {"message": {"parts": [{"kind": "text", "text": "hi"}]}},
            },
        )
        task_id = send_resp.json()["result"]["id"]

        # Poll until completed (fake stream is instant in tests)
        for _ in range(20):
            poll = await client.get(f"/tasks/{task_id}")
            if poll.json()["status"]["state"] == COMPLETED:
                break
            await asyncio.sleep(0.05)

    assert poll.json()["status"]["state"] == COMPLETED
    assert poll.json()["artifacts"][0]["parts"][0]["text"] == "hello world"


@pytest.mark.asyncio
async def test_cancel_task():
    app, _ = _make_test_app()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        send_resp = await client.post(
            "/a2a",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "message/send",
                "params": {"message": {"parts": [{"kind": "text", "text": "hi"}]}},
            },
        )
        task_id = send_resp.json()["result"]["id"]
        cancel_resp = await client.post(f"/tasks/{task_id}:cancel")

    # Either canceled or already completed (fake stream may finish instantly)
    assert cancel_resp.status_code in (200, 409)


@pytest.mark.asyncio
async def test_rest_send_returns_202():
    app, _ = _make_test_app()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/message:send",
            json={
                "message": {"parts": [{"kind": "text", "text": "hello"}]},
            },
        )

    assert resp.status_code == 202
    assert resp.json()["status"]["state"] == SUBMITTED


@pytest.mark.asyncio
async def test_stream_first_event_is_submitted():
    """SSE stream must emit submitted as the very first frame."""
    app, _ = _make_test_app()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        async with client.stream(
            "POST",
            "/message:stream",
            json={
                "message": {"parts": [{"kind": "text", "text": "hello"}]},
            },
        ) as resp:
            assert resp.status_code == 200
            first_line = None
            async for line in resp.aiter_lines():
                if line.startswith("data:"):
                    first_line = line
                    break

    assert first_line is not None
    first_event = json.loads(first_line[len("data:") :].strip())
    # REST stream returns plain result dict (no jsonrpc wrapper for rpc_id=None)
    result = first_event.get("result", first_event)
    assert result["status"]["state"] == SUBMITTED


@pytest.mark.asyncio
async def test_agent_card_route():
    app, card = _make_test_app()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/.well-known/agent.json")

    assert resp.status_code == 200
    assert resp.json()["capabilities"]["streaming"] is True
    assert resp.json()["capabilities"]["pushNotifications"] is True
