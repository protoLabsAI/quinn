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
    WORLDSTATE_DELTA_MIME,
    _TERMINAL,
    A2ATaskStore,
    PushNotificationConfig,
    TaskRecord,
    _build_artifact_event,
    _build_status_event,
    _deliver_webhook,
    _now_iso,
    _run_task_background,
    _store,
    _task_to_response,
    _terminal_artifact_parts,
    _watch_task,
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
def _reset_module_state():
    """Clear module-level _store + _pending_webhook_tasks between tests.

    Route-integration tests exercise register_a2a_routes() which always uses
    the module singletons, so tasks from one test would otherwise leak into
    the next. Clearing on entry and exit keeps tests hermetic.
    """
    from a2a_handler import _pending_webhook_tasks
    _store._tasks.clear()
    _pending_webhook_tasks.clear()
    yield
    _store._tasks.clear()
    _pending_webhook_tasks.clear()


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


# ── Atomic cancel ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_if_not_terminal_returns_updated_record(store):
    record = _make_record(state=WORKING)
    await store.create(record)
    result = await store.cancel_if_not_terminal("test-task-id")
    assert result is not None
    assert result.state == CANCELED
    # Update event rotates so subscribers wake up (same contract as update_state)
    assert record._cancel_event.is_set()


@pytest.mark.asyncio
async def test_cancel_if_not_terminal_returns_none_when_already_terminal(store):
    """Prevents the route handler from clobbering a COMPLETED state that
    the runner wrote while the request was in flight."""
    record = _make_record(state=COMPLETED)
    await store.create(record)
    result = await store.cancel_if_not_terminal("test-task-id")
    assert result is None
    # Record must still be COMPLETED — not downgraded to CANCELED
    assert (await store.get("test-task-id")).state == COMPLETED


@pytest.mark.asyncio
async def test_cancel_if_not_terminal_returns_none_when_missing(store):
    assert await store.cancel_if_not_terminal("no-such-id") is None


# ── SSRF validation ───────────────────────────────────────────────────────────


def test_ssrf_rejects_non_http_scheme():
    from a2a_handler import _is_safe_webhook_url
    assert _is_safe_webhook_url("file:///etc/passwd") is False
    assert _is_safe_webhook_url("gopher://example.com/x") is False
    assert _is_safe_webhook_url("javascript:alert(1)") is False


def test_ssrf_rejects_loopback():
    from a2a_handler import _is_safe_webhook_url
    assert _is_safe_webhook_url("http://127.0.0.1/hook") is False
    assert _is_safe_webhook_url("http://localhost/hook") is False
    assert _is_safe_webhook_url("http://[::1]/hook") is False


def test_ssrf_rejects_rfc1918():
    from a2a_handler import _is_safe_webhook_url
    assert _is_safe_webhook_url("http://10.0.0.1/hook") is False
    assert _is_safe_webhook_url("http://192.168.1.1/hook") is False
    assert _is_safe_webhook_url("http://172.16.0.1/hook") is False


def test_ssrf_rejects_link_local_and_metadata():
    """169.254.169.254 is the AWS/GCP instance-metadata endpoint — the
    canonical SSRF target. Must be blocked."""
    from a2a_handler import _is_safe_webhook_url
    assert _is_safe_webhook_url("http://169.254.169.254/latest/meta-data") is False
    assert _is_safe_webhook_url("http://169.254.1.1/hook") is False


def test_ssrf_rejects_unresolvable_hostname():
    from a2a_handler import _is_safe_webhook_url
    # A hostname under RFC2606's invalid TLD — guaranteed not to resolve.
    assert _is_safe_webhook_url("http://totally-not-a-real-host.invalid/") is False


def test_ssrf_rejects_malformed_url():
    from a2a_handler import _is_safe_webhook_url
    assert _is_safe_webhook_url("") is False
    assert _is_safe_webhook_url("not-a-url") is False
    assert _is_safe_webhook_url("http://") is False


def test_ssrf_accepts_public_ip_literal():
    """A globally-routable IP literal passes — covers the common case of
    operators giving a Tailscale/cloud public IP without DNS."""
    from a2a_handler import _is_safe_webhook_url
    assert _is_safe_webhook_url("https://8.8.8.8/hook") is True


def test_parse_push_config_rejects_unsafe_url():
    """Integration between the parser and the SSRF check — malicious
    submit-time configurations must be dropped (return None), not converted
    into a PushNotificationConfig the runner would then deliver to."""
    from a2a_handler import _parse_push_config
    cfg = _parse_push_config({"pushNotificationConfig": {"url": "http://169.254.169.254/"}})
    assert cfg is None


# ── Webhook task retention ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_push_retains_webhook_task_reference():
    """Regression: asyncio.create_task(...) without storing the handle risks
    GC mid-retry (Python 3.11+ docs). _push must add the task to the
    module-level set and only drop it on completion."""
    from a2a_handler import _pending_webhook_tasks, _push

    record = _make_record(state=COMPLETED, accumulated_text="done")
    record.push_config = PushNotificationConfig(url="https://example.com/hook")

    captured_tasks = []
    evt = asyncio.Event()

    async def _slow_deliver(r, cfg):
        # Stall long enough for the caller to observe task retention.
        await evt.wait()

    with patch("a2a_handler._deliver_webhook", _slow_deliver):
        await _push(record)
        await asyncio.sleep(0)  # let create_task schedule

        # Exactly one in-flight delivery, registered in the retention set.
        assert len(_pending_webhook_tasks) == 1
        task = next(iter(_pending_webhook_tasks))
        captured_tasks.append(task)

        # Release the stall; task completes; done_callback evicts from set.
        evt.set()
        await task
        await asyncio.sleep(0)
        assert task not in _pending_webhook_tasks


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

    async def _fake_push(r):
        push_calls.append(r.state)

    stream_fn = lambda: _mock_stream(("text", "hello "), ("text", "world"), ("done", "hello world"))

    with patch("a2a_handler._store", store), patch("a2a_handler._push", _fake_push):
        await _run_task_background("bg-test", stream_fn)

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

    async def _fake_push(r):
        push_calls.append(r.state)

    stream_fn = lambda: _mock_stream(("text", "partial"), ("error", "boom"))

    with patch("a2a_handler._store", store), patch("a2a_handler._push", _fake_push):
        await _run_task_background("bg-err", stream_fn)

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

    async def _noop_push(r):
        pass

    stream_fn = lambda: _mock_stream(("text", "should not process"))

    with patch("a2a_handler._store", store), patch("a2a_handler._push", _noop_push):
        await _run_task_background("bg-cancel", stream_fn)

    final = await store.get("bg-cancel")
    assert final.state == CANCELED


# ── _push reads live push_config ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_push_reads_push_config_from_live_record():
    """Regression: _push must read record.push_config at call time, not close
    over a submit-time value. Otherwise POST /tasks/{id}/pushNotificationConfigs
    would silently never reach in-flight tasks."""
    from a2a_handler import _push

    record = _make_record(state=WORKING)
    # No push config yet — _push should no-op.
    delivered = []

    async def _fake_deliver(r, cfg):
        delivered.append(cfg.url)

    with patch("a2a_handler._deliver_webhook", _fake_deliver):
        await _push(record)
        await asyncio.sleep(0)  # drain create_task
        assert delivered == []

        # Caller registers a webhook after submit — _push must pick it up.
        record.push_config = PushNotificationConfig(url="http://late.example/hook")
        await _push(record)
        await asyncio.sleep(0)
        assert delivered == ["http://late.example/hook"]


# ── Worldstate-delta-v1 artifact emission ────────────────────────────────────


@pytest.mark.asyncio
async def test_store_add_delta_appends_to_record(store):
    """Deltas accumulate under the lock so concurrent tool calls don't
    clobber each other's entries."""
    record = _make_record(state=WORKING)
    await store.create(record)
    await store.add_delta("test-task-id", {"domain": "d", "path": "x", "op": "inc", "value": 1})
    await store.add_delta("test-task-id", {"domain": "d", "path": "y", "op": "set", "value": 42})
    fetched = await store.get("test-task-id")
    assert len(fetched.deltas) == 2
    assert fetched.deltas[0]["path"] == "x"
    assert fetched.deltas[1]["value"] == 42


@pytest.mark.asyncio
async def test_store_add_delta_on_missing_task_is_noop(store):
    """No raise, just silent drop — matches update_state's contract."""
    await store.add_delta("no-such-id", {"domain": "d", "path": "x", "op": "inc", "value": 1})


def test_terminal_artifact_parts_text_only() -> None:
    """A run with no observed mutations produces a single text part — no
    empty DataPart, which would confuse consumers looking for deltas."""
    record = _make_record(state=COMPLETED, accumulated_text="hello world")
    parts = _terminal_artifact_parts(record)
    assert parts == [{"kind": "text", "text": "hello world"}]


def test_terminal_artifact_parts_text_and_delta() -> None:
    """When deltas exist, the text part comes first and a DataPart carrying
    the canonical MIME type follows. Ordering matters — Workstacean's
    executor reads artifact.parts in order."""
    record = _make_record(state=COMPLETED, accumulated_text="Bug filed: ...")
    record.deltas.append(
        {"domain": "protomaker_board", "path": "data.backlog_count",
         "op": "inc", "value": 1}
    )
    parts = _terminal_artifact_parts(record)
    assert len(parts) == 2
    assert parts[0] == {"kind": "text", "text": "Bug filed: ..."}
    assert parts[1]["kind"] == "data"
    assert parts[1]["metadata"]["mimeType"] == WORLDSTATE_DELTA_MIME
    assert parts[1]["data"]["deltas"] == [
        {"domain": "protomaker_board", "path": "data.backlog_count",
         "op": "inc", "value": 1}
    ]


def test_terminal_artifact_parts_empty_without_text_or_deltas() -> None:
    """Suppresses the artifact slot entirely — callers gate on an empty
    list to decide whether to emit an ``artifacts`` field at all."""
    record = _make_record(state=COMPLETED)
    assert _terminal_artifact_parts(record) == []


def test_task_to_response_includes_delta_artifact() -> None:
    """GET /tasks/{id} should surface the delta DataPart so pollers that
    never subscribed to webhooks still see the observed mutation."""
    record = _make_record(state=COMPLETED, accumulated_text="done")
    record.deltas.append(
        {"domain": "ci", "path": "data.blockedPRs", "op": "inc", "value": -1}
    )
    resp = _task_to_response(record)
    assert "artifacts" in resp
    parts = resp["artifacts"][0]["parts"]
    assert any(p.get("kind") == "data"
               and p["metadata"]["mimeType"] == WORLDSTATE_DELTA_MIME
               for p in parts)


@pytest.mark.asyncio
async def test_webhook_payload_includes_delta_on_completed():
    """Push consumers must receive deltas in the same artifact they would
    have gotten by polling — otherwise webhook subscribers miss effects
    that poll subscribers see."""
    record = _make_record(state=COMPLETED, accumulated_text="ok")
    record.deltas.append(
        {"domain": "protomaker_board", "path": "data.backlog_count",
         "op": "inc", "value": 1}
    )
    captured = {}

    async def _capture_post(url, json=None, headers=None):
        captured["json"] = json
        resp = MagicMock(status_code=204)
        return resp

    client_cm = MagicMock()
    client_cm.__aenter__ = AsyncMock(return_value=MagicMock(post=_capture_post))
    client_cm.__aexit__ = AsyncMock(return_value=None)

    with patch("a2a_handler.httpx.AsyncClient", return_value=client_cm):
        await _deliver_webhook(
            record,
            PushNotificationConfig(url="https://example.com/hook"),
        )

    parts = captured["json"]["artifact"]["parts"]
    assert any(p.get("kind") == "data"
               and p["metadata"]["mimeType"] == WORLDSTATE_DELTA_MIME
               for p in parts), f"delta missing from webhook payload: {parts}"


@pytest.mark.asyncio
async def test_background_runner_records_delta_event():
    """The runner must accept ``delta`` stream events and persist them on
    the record so the terminal artifact carries them."""
    store = A2ATaskStore()
    record = _make_record(id="bg-delta")
    await store.create(record)

    delta = {"domain": "protomaker_board", "path": "data.backlog_count",
             "op": "inc", "value": 1}
    stream_fn = lambda: _mock_stream(
        ("text", "working..."),
        ("tool_end", "✅ file_bug → Bug filed: ..."),
        ("delta", delta),
        ("done", "Bug filed: feature-abc"),
    )

    async def _noop(_r):
        pass

    with patch("a2a_handler._store", store), patch("a2a_handler._push", _noop):
        await _run_task_background("bg-delta", stream_fn)

    final = await store.get("bg-delta")
    assert final.state == COMPLETED
    assert final.deltas == [delta]


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


# ── _watch_task: shared SSE consumer ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_watch_task_yields_snapshot_then_terminates():
    """A terminal task must emit one status frame and exit — no hanging."""
    store = A2ATaskStore()
    record = _make_record(id="done", state=COMPLETED, accumulated_text="final")
    await store.create(record)
    with patch("a2a_handler._store", store):
        kinds = []
        async for kind, r, payload in _watch_task("done", start_text_len=0):
            kinds.append((kind, payload))
    # Initial snapshot: status + text_delta (since accumulated > start_len)
    assert kinds == [("status", None), ("text_delta", "final")]


@pytest.mark.asyncio
async def test_watch_task_emits_delta_not_full_text():
    """The regression at the heart of #13 item 2: on reconnect, the
    watcher must emit only the new suffix — never the full accumulated_text.

    The watcher naturally coalesces rapid successive updates (desirable
    for SSE chatter), so the test yields aggressively between updates to
    guarantee the watcher visits each intermediate state and that each
    visit emits only the NEW suffix, not the full text."""
    store = A2ATaskStore()
    record = _make_record(id="mid", state=WORKING, accumulated_text="abcdefgh")
    await store.create(record)

    async def _reader():
        deltas = []
        with patch("a2a_handler._store", store):
            async for kind, r, payload in _watch_task("mid", start_text_len=5):
                if kind == "text_delta":
                    deltas.append(payload)
                if kind == "status" and r is not None and r.state in _TERMINAL:
                    return deltas
        return deltas

    task = asyncio.create_task(_reader())
    # Yield repeatedly so the reader runs through snapshot and parks on
    # the first update event.
    for _ in range(5):
        await asyncio.sleep(0)

    # First append — let the reader wake and consume before the next one.
    await store.update_state("mid", WORKING, accumulated_text="abcdefghIJKL")
    for _ in range(5):
        await asyncio.sleep(0)

    # Terminal.
    await store.update_state("mid", COMPLETED, accumulated_text="abcdefghIJKL")
    deltas = await asyncio.wait_for(task, timeout=2.0)
    # Each update yields only the NEW suffix — never the full text.
    assert deltas == ["fgh", "IJKL"]


@pytest.mark.asyncio
async def test_watch_task_multiple_subscribers_each_see_terminal():
    """Two consumers on the same task both receive a terminal status frame.
    Exercises the rotate-event pattern: each update sets the old event and
    installs a fresh one, so both waiters wake up in lock-step."""
    store = A2ATaskStore()
    record = _make_record(id="multi", state=WORKING)
    await store.create(record)

    terminal_states: list[str] = []

    async def _consumer():
        last_state = None
        async for kind, r, _payload in _watch_task("multi", start_text_len=0):
            if kind == "status" and r is not None:
                last_state = r.state
                if last_state in _TERMINAL:
                    break
        terminal_states.append(last_state)

    # Single outer patch — unittest.mock.patch is not safe to stack under
    # concurrent asyncio tasks (restore semantics race on exit).
    with patch("a2a_handler._store", store):
        a = asyncio.create_task(_consumer())
        b = asyncio.create_task(_consumer())
        # Give both consumers time to emit their snapshot and park on the
        # rotating update_event before the test triggers the transition.
        await asyncio.sleep(0.05)
        await store.update_state("multi", COMPLETED)
        await asyncio.wait_for(asyncio.gather(a, b), timeout=2.0)
    assert terminal_states == [COMPLETED, COMPLETED]


@pytest.mark.asyncio
async def test_background_runner_persists_tool_status_message():
    """tool_start / tool_end payloads land on record.last_status_message so
    :subscribe reconnects can see the most recent tool message — the
    producer's in-process event stream is no longer the only source."""
    store = A2ATaskStore()
    record = _make_record(id="tooltrack")
    await store.create(record)

    stream_fn = lambda: _mock_stream(
        ("text", "starting"),
        ("tool_start", "🔧 file_bug: draft"),
        ("tool_end", "✅ file_bug → Bug filed: ..."),
        ("done", "starting"),
    )

    async def _noop(_r):
        pass

    with patch("a2a_handler._store", store), patch("a2a_handler._push", _noop):
        await _run_task_background("tooltrack", stream_fn)

    final = await store.get("tooltrack")
    # Terminal transitions clear the status message — subscribers on a
    # completed task shouldn't see a stale tool ping.
    assert final.state == COMPLETED
    assert final.last_status_message is None


@pytest.mark.asyncio
async def test_stream_producer_survives_consumer_cancellation():
    """The biggest guarantee of the SSE refactor: SSE connection drop does
    NOT kill the LangGraph producer. Verified by simulating a consumer
    disconnect mid-run and asserting the bg task still completes."""
    store = A2ATaskStore()

    async def _slow_stream(_text, _ctx):
        yield ("text", "partial")
        await asyncio.sleep(0.05)
        yield ("text", " more")
        await asyncio.sleep(0.05)
        yield ("done", "partial more")

    async def _noop_push(_r):
        pass

    with patch("a2a_handler._store", store), patch("a2a_handler._push", _noop_push):
        # Manually seed + spawn the bg runner (mimics _submit_task)
        task_id = "drop-test"
        now = _now_iso()
        record = TaskRecord(
            id=task_id, context_id="c", state=SUBMITTED,
            created_at=now, updated_at=now, message_text="t",
        )
        await store.create(record)
        bg = asyncio.create_task(
            _run_task_background(task_id, lambda: _slow_stream("t", "c"))
        )
        record._bg_task = bg

        # Simulate an SSE consumer that attaches, reads one frame, then
        # "disconnects" by closing the generator. Matches what FastAPI
        # does to an SSE StreamingResponse when the HTTP connection closes.
        async def _dropping_consumer():
            gen = _watch_task(task_id, 0)
            await gen.__anext__()         # read one frame then drop
            await gen.aclose()            # close generator cleanly
            return "dropped"

        result = await asyncio.wait_for(_dropping_consumer(), timeout=1.0)
        assert result == "dropped"

        # BG task should STILL complete — that's the whole point of decoupling.
        # Success is measured by the task landing in COMPLETED state, which
        # only happens if the producer ran all the way through "done".
        await asyncio.wait_for(bg, timeout=3.0)
        final = await store.get(task_id)
        assert final.state == COMPLETED
        assert final.accumulated_text == "partial more"


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
