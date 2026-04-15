"""A2A protocol handler — streaming, async task lifecycle, push notifications.

Implements the A2A spec (https://a2a-protocol.org/latest/) as a FastAPI route
factory.  All route logic lives here; server.py calls register_a2a_routes()
once during startup and otherwise stays out of the way.

Supported operations
────────────────────
  POST /a2a                            JSON-RPC 2.0 (legacy, backwards-compat)
    method: message/send               → async, returns submitted immediately
    method: message/sendStream         → SSE stream

  POST /message:send                   REST alias for message/send  (HTTP 202)
  POST /message:stream                 REST alias for message/sendStream (SSE)
  GET  /tasks/{id}                     Poll task state + artifact
  GET  /tasks/{id}:subscribe           SSE reconnect to in-progress task
  POST /tasks/{id}:cancel              Cancel a running task
  POST /tasks/{id}/pushNotificationConfigs   Register webhook after task creation
  GET  /.well-known/agent.json         Agent card
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Callable
from uuid import uuid4

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

logger = logging.getLogger(__name__)

# ── Task state constants ──────────────────────────────────────────────────────

SUBMITTED = "submitted"
WORKING = "working"
COMPLETED = "completed"
FAILED = "failed"
CANCELED = "canceled"

_TERMINAL = {COMPLETED, FAILED, CANCELED}

# ── Data types ────────────────────────────────────────────────────────────────


@dataclass
class PushNotificationConfig:
    url: str
    token: str | None = None
    id: str = field(default_factory=lambda: str(uuid4()))


@dataclass
class TaskRecord:
    """In-memory record for a single A2A task.

    The asyncio primitives (_cancel_event, _update_event, _bg_task) are never
    serialised — _task_to_response() reads only primitive fields.
    """

    id: str
    context_id: str
    state: str
    created_at: str
    updated_at: str
    message_text: str
    accumulated_text: str = ""
    error_message: str | None = None
    push_config: PushNotificationConfig | None = None
    # ── asyncio primitives (not serialised) ──
    _cancel_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _update_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _bg_task: asyncio.Task | None = field(default=None, repr=False)


# ── Task store ────────────────────────────────────────────────────────────────


_DEFAULT_TERMINAL_TTL_S = 3600     # evict terminal tasks older than 1h
_DEFAULT_CLEANUP_INTERVAL_S = 300  # sweep every 5 min


class A2ATaskStore:
    """Asyncio-safe in-memory task store.

    Uses a rotate-event pattern: each call to update_state() replaces
    _update_event with a new asyncio.Event and sets the old one so all current
    subscribers wake up in lock-step.  The new event is ready for the next
    batch of waiters.

    Retains tasks in-memory for ``_DEFAULT_TERMINAL_TTL_S`` after they hit a
    terminal state so pollers/webhook delivery still see them, then evicts.
    Without this, a long-lived process would leak memory proportional to total
    lifetime traffic.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._tasks: dict[str, TaskRecord] = {}
        self._cleanup_task: asyncio.Task | None = None

    async def create(self, record: TaskRecord) -> TaskRecord:
        async with self._lock:
            self._tasks[record.id] = record
        return record

    async def get(self, task_id: str) -> TaskRecord | None:
        return self._tasks.get(task_id)

    async def update_state(
        self,
        task_id: str,
        state: str,
        accumulated_text: str | None = None,
        error: str | None = None,
    ) -> TaskRecord | None:
        async with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                return None
            record.state = state
            record.updated_at = _now_iso()
            if accumulated_text is not None:
                record.accumulated_text = accumulated_text
            if error is not None:
                record.error_message = error
            old_event = record._update_event
            record._update_event = asyncio.Event()
        # Wake subscribers outside the lock so they can re-acquire it
        old_event.set()
        return record

    async def cancel(self, task_id: str) -> bool:
        # Acquire the lock to match every other store mutation. Event.set()
        # and Task.cancel() are themselves thread-safe so we drop the lock
        # before calling them to avoid holding it across cooperative yields.
        async with self._lock:
            record = self._tasks.get(task_id)
        if record is None:
            return False
        record._cancel_event.set()
        if record._bg_task and not record._bg_task.done():
            record._bg_task.cancel()
        return True

    async def cleanup_expired(self, ttl_seconds: int = _DEFAULT_TERMINAL_TTL_S) -> int:
        """Remove terminal tasks whose ``updated_at`` is older than ttl_seconds.

        Returns the count removed. Working / submitted tasks are never evicted —
        they stay until they reach a terminal state, then age out normally.
        """
        cutoff = datetime.now(timezone.utc).timestamp() - ttl_seconds
        removed = 0
        async with self._lock:
            for tid in list(self._tasks.keys()):
                r = self._tasks[tid]
                if r.state not in _TERMINAL:
                    continue
                try:
                    ts = datetime.fromisoformat(r.updated_at).timestamp()
                except ValueError:
                    continue
                if ts < cutoff:
                    del self._tasks[tid]
                    removed += 1
        if removed:
            logger.debug("[a2a] evicted %d expired terminal task(s)", removed)
        return removed

    def start_cleanup(
        self,
        interval_s: int = _DEFAULT_CLEANUP_INTERVAL_S,
        ttl_s: int = _DEFAULT_TERMINAL_TTL_S,
    ) -> None:
        """Start the background eviction loop. Idempotent — safe to call from
        every request handler. No-op if already running.

        Lazy rather than eager because __init__ runs at module import time,
        before an asyncio event loop exists.
        """
        if self._cleanup_task and not self._cleanup_task.done():
            return
        self._cleanup_task = asyncio.create_task(self._cleanup_loop(interval_s, ttl_s))

    async def _cleanup_loop(self, interval_s: int, ttl_s: int) -> None:
        while True:
            await asyncio.sleep(interval_s)
            try:
                await self.cleanup_expired(ttl_s)
            except Exception as exc:
                logger.warning("[a2a] cleanup loop error: %s", exc)


# Module-level singleton — one store per process
_store = A2ATaskStore()


# ── Helpers ───────────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _task_to_response(record: TaskRecord) -> dict:
    resp: dict[str, Any] = {
        "id": record.id,
        "contextId": record.context_id,
        "status": {"state": record.state, "timestamp": record.updated_at},
    }
    if record.accumulated_text:
        resp["artifacts"] = [{"parts": [{"kind": "text", "text": record.accumulated_text}]}]
    if record.error_message:
        resp["status"]["message"] = {
            "role": "agent",
            "parts": [{"kind": "text", "text": record.error_message}],
        }
    return resp


def _build_status_event(record: TaskRecord) -> dict:
    evt: dict[str, Any] = {
        "task_id": record.id,
        "context_id": record.context_id,
        "status": {"state": record.state, "timestamp": record.updated_at},
    }
    if record.error_message:
        evt["status"]["message"] = {
            "role": "agent",
            "parts": [{"kind": "text", "text": record.error_message}],
        }
    return evt


def _build_artifact_event(record: TaskRecord, *, last_chunk: bool) -> dict:
    return {
        "task_id": record.id,
        "context_id": record.context_id,
        "artifact": {"parts": [{"kind": "text", "text": record.accumulated_text}]},
        "append": True,
        "last_chunk": last_chunk,
    }


def _extract_text_and_context(message: dict, context_id: str = "") -> tuple[str, str]:
    """Pull text + contextId out of an A2A Message dict."""
    parts = message.get("parts", [])
    text = next((p.get("text", "") for p in parts if p.get("kind") == "text"), "")
    if not text:
        text = next((p.get("text", "") for p in parts), "")
    context_id = context_id or f"a2a-{uuid4()}"
    return text, context_id


def _parse_push_config(configuration: dict) -> PushNotificationConfig | None:
    cfg = (configuration or {}).get("pushNotificationConfig") or (configuration or {}).get("taskPushNotificationConfig")
    if not cfg or not cfg.get("url"):
        return None
    auth = cfg.get("authentication") or {}
    return PushNotificationConfig(
        url=cfg["url"],
        token=auth.get("credentials"),
        id=cfg.get("id", str(uuid4())),
    )


# ── Webhook delivery ──────────────────────────────────────────────────────────


async def _deliver_webhook(record: TaskRecord, push_config: PushNotificationConfig) -> None:
    """POST a TaskStatusUpdateEvent to the configured webhook URL.

    Retries 3× with exponential backoff (1s / 3s / 9s).
    Skips retry on 4xx (client error — retrying won't help).
    """
    payload = _build_status_event(record)
    if record.state == COMPLETED and record.accumulated_text:
        payload["artifact"] = {
            "parts": [{"kind": "text", "text": record.accumulated_text}],
            "append": False,
            "last_chunk": True,
        }

    headers = {"Content-Type": "application/json"}
    if push_config.token:
        headers["Authorization"] = f"Bearer {push_config.token}"

    backoff = [1, 3, 9]
    async with httpx.AsyncClient(timeout=10) as client:
        for attempt, delay in enumerate(backoff):
            try:
                resp = await client.post(push_config.url, json=payload, headers=headers)
                if resp.status_code < 500:
                    logger.debug("[a2a] webhook delivered → %s (%s)", push_config.url, resp.status_code)
                    return
                logger.warning("[a2a] webhook 5xx (attempt %d): %s", attempt + 1, resp.status_code)
            except httpx.RequestError as exc:
                logger.warning("[a2a] webhook request error (attempt %d): %s", attempt + 1, exc)
            if attempt < len(backoff) - 1:
                await asyncio.sleep(delay)

    logger.error("[a2a] webhook failed after %d attempts: %s", len(backoff), push_config.url)


async def _push(record: TaskRecord) -> None:
    """Fire webhook delivery for *record* if a push config is currently
    registered on it.

    Reads record.push_config at call time rather than closing over the
    submit-time value — otherwise a caller who registered a webhook via
    POST /tasks/{id}/pushNotificationConfigs *after* submitting would
    never receive any state transitions.
    """
    cfg = record.push_config
    if cfg and record.state in _TERMINAL | {WORKING}:
        asyncio.create_task(_deliver_webhook(record, cfg))


# ── Background task runner ────────────────────────────────────────────────────


async def _run_task_background(
    task_id: str,
    stream_fn: Callable[[], AsyncGenerator],
) -> None:
    """Run LangGraph in the background, writing state updates to the task store."""
    record = await _store.update_state(task_id, WORKING)
    if record is None:
        return
    await _push(record)

    accumulated = ""
    try:
        async for event_type, payload in stream_fn():
            record = await _store.get(task_id)
            if record is None:
                return
            if record._cancel_event.is_set():
                await _store.update_state(task_id, CANCELED)
                return

            if event_type == "text":
                accumulated += payload
                await _store.update_state(task_id, WORKING, accumulated_text=accumulated)

            elif event_type in ("tool_start", "tool_end"):
                # Status update only — no new artifact text
                await _store.update_state(task_id, WORKING, accumulated_text=accumulated)

            elif event_type == "done":
                record = await _store.update_state(
                    task_id,
                    COMPLETED,
                    accumulated_text=payload or accumulated,
                )
                await _push(record)
                return

            elif event_type == "error":
                record = await _store.update_state(task_id, FAILED, error=payload)
                await _push(record)
                return

    except asyncio.CancelledError:
        await _store.update_state(task_id, CANCELED)
        raise
    except Exception as exc:
        logger.exception("[a2a] background task %s crashed", task_id)
        record = await _store.update_state(task_id, FAILED, error=str(exc))
        await _push(record)


# ── SSE helpers ───────────────────────────────────────────────────────────────

_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


def _sse_rpc(rpc_id: Any, result: dict) -> str:
    return _sse({"jsonrpc": "2.0", "id": rpc_id, "result": result})


# ── Auth helper ───────────────────────────────────────────────────────────────


def _check_auth(request: Request, api_key: str) -> None:
    if api_key and request.headers.get("x-api-key") != api_key:
        raise HTTPException(status_code=401, detail="Unauthorized")


# ── Route factory ─────────────────────────────────────────────────────────────


def register_a2a_routes(
    app: FastAPI,
    chat_stream_fn_factory: Callable[[str, str], AsyncGenerator],
    chat_fn: Callable,  # kept for potential future use / testing
    api_key: str,
    agent_card: dict,
    register_card_route: bool = True,
) -> None:
    """Register all A2A routes on *app* and update *agent_card* capabilities.

    Host apps that already serve the agent card themselves (e.g. at multiple
    well-known paths for sdk compat) should pass ``register_card_route=False``
    so FastAPI does not raise on a duplicate route registration.
    """

    # Update agent card capabilities
    agent_card.setdefault("capabilities", {})
    agent_card["capabilities"]["streaming"] = True
    agent_card["capabilities"]["pushNotifications"] = True

    # ── Agent card ────────────────────────────────────────────────────────────

    if register_card_route:
        @app.get("/.well-known/agent.json", include_in_schema=False)
        async def _agent_card_route():
            return agent_card

    # ── Shared submit helper ──────────────────────────────────────────────────

    async def _submit_task(
        text: str,
        context_id: str,
        push_config: PushNotificationConfig | None,
    ) -> TaskRecord:
        """Create a TaskRecord, fire the background runner, return immediately."""
        # Lazy-start the cleanup loop the first time we're inside a running
        # event loop. Idempotent, cheap.
        _store.start_cleanup()

        task_id = str(uuid4())
        now = _now_iso()
        record = TaskRecord(
            id=task_id,
            context_id=context_id,
            state=SUBMITTED,
            created_at=now,
            updated_at=now,
            message_text=text,
            push_config=push_config,
        )
        await _store.create(record)

        bg = asyncio.create_task(
            _run_task_background(
                task_id,
                lambda: chat_stream_fn_factory(text, context_id),
            )
        )
        record._bg_task = bg
        logger.info("[a2a] task %s submitted (context=%s)", task_id, context_id)
        return record

    # ── Streaming SSE generator ───────────────────────────────────────────────

    async def _stream_new_task(
        text: str,
        context_id: str,
        push_config: PushNotificationConfig | None,
        rpc_id: Any = None,
    ):
        """Async generator: creates a task and streams its events as SSE."""
        _store.start_cleanup()
        task_id = str(uuid4())
        now = _now_iso()
        record = TaskRecord(
            id=task_id,
            context_id=context_id,
            state=SUBMITTED,
            created_at=now,
            updated_at=now,
            message_text=text,
            push_config=push_config,
        )
        await _store.create(record)

        # Frame 0: submitted — client gets task_id before LangGraph starts
        yield _sse_rpc(
            rpc_id,
            {
                "id": task_id,
                "contextId": context_id,
                "status": {"state": SUBMITTED, "timestamp": now},
            },
        )

        accumulated = ""
        last_emitted_len = 0

        try:
            await _store.update_state(task_id, WORKING)
            working_record = await _store.get(task_id)
            if working_record is not None:
                await _push(working_record)

            yield _sse_rpc(
                rpc_id,
                {
                    "id": task_id,
                    "contextId": context_id,
                    "status": {"state": WORKING, "timestamp": _now_iso()},
                },
            )

            async for event_type, payload in chat_stream_fn_factory(text, context_id):
                r = await _store.get(task_id)
                if r and r._cancel_event.is_set():
                    await _store.update_state(task_id, CANCELED)
                    yield _sse_rpc(
                        rpc_id,
                        {
                            "id": task_id,
                            "contextId": context_id,
                            "status": {"state": CANCELED, "timestamp": _now_iso()},
                        },
                    )
                    return

                if event_type == "text":
                    accumulated += payload
                    await _store.update_state(task_id, WORKING, accumulated_text=accumulated)
                    # Only emit artifact frame when text actually grew
                    if len(accumulated) > last_emitted_len:
                        last_emitted_len = len(accumulated)
                        yield _sse_rpc(
                            rpc_id,
                            {
                                "id": task_id,
                                "contextId": context_id,
                                "status": {"state": WORKING},
                                "artifacts": [
                                    {"parts": [{"kind": "text", "text": payload}], "append": True, "last_chunk": False}
                                ],
                            },
                        )

                elif event_type in ("tool_start", "tool_end"):
                    await _store.update_state(task_id, WORKING, accumulated_text=accumulated)
                    yield _sse_rpc(
                        rpc_id,
                        {
                            "id": task_id,
                            "contextId": context_id,
                            "status": {
                                "state": WORKING,
                                "timestamp": _now_iso(),
                                "message": {"role": "agent", "parts": [{"kind": "text", "text": payload}]},
                            },
                        },
                    )

                elif event_type == "done":
                    final_text = payload or accumulated
                    r = await _store.update_state(task_id, COMPLETED, accumulated_text=final_text)
                    if r is not None:
                        await _push(r)
                    yield _sse_rpc(
                        rpc_id,
                        {
                            "id": task_id,
                            "contextId": context_id,
                            "status": {"state": COMPLETED, "timestamp": _now_iso()},
                            "artifacts": [
                                {"parts": [{"kind": "text", "text": final_text}], "append": False, "last_chunk": True}
                            ],
                        },
                    )
                    return

                elif event_type == "error":
                    r = await _store.update_state(task_id, FAILED, error=payload)
                    if r is not None:
                        await _push(r)
                    yield _sse_rpc(
                        rpc_id,
                        {
                            "id": task_id,
                            "contextId": context_id,
                            "status": {
                                "state": FAILED,
                                "timestamp": _now_iso(),
                                "message": {"role": "agent", "parts": [{"kind": "text", "text": payload}]},
                            },
                        },
                    )
                    return

        except Exception as exc:
            logger.exception("[a2a] stream task %s crashed", task_id)
            r = await _store.update_state(task_id, FAILED, error=str(exc))
            if r is not None:
                await _push(r)
            yield _sse_rpc(
                rpc_id,
                {
                    "id": task_id,
                    "contextId": context_id,
                    "status": {
                        "state": FAILED,
                        "timestamp": _now_iso(),
                        "message": {"role": "agent", "parts": [{"kind": "text", "text": str(exc)}]},
                    },
                },
            )

    # ── POST /a2a  (JSON-RPC 2.0 — legacy, backwards-compat) ─────────────────

    @app.post("/a2a", include_in_schema=False)
    async def _a2a_rpc(request: Request, req: dict):
        if api_key and request.headers.get("x-api-key") != api_key:
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)

        rpc_id = req.get("id")
        method = req.get("method", "")
        params = req.get("params", {})

        message = params.get("message", {})
        context_id = params.get("contextId", "")
        configuration = params.get("configuration", {})

        parts = message.get("parts", [])
        text = next((p.get("text", "") for p in parts if p.get("kind") == "text"), "")
        if not text:
            text = next((p.get("text", "") for p in parts), "")

        if not text:
            return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32600, "message": "No text content in message"}}

        context_id = context_id or f"a2a-{uuid4()}"
        push_config = _parse_push_config(configuration)

        # ── message/sendStream → SSE ──────────────────────────────────────────
        if method == "message/sendStream":
            return StreamingResponse(
                _stream_new_task(text, context_id, push_config, rpc_id=rpc_id),
                media_type="text/event-stream",
                headers=_SSE_HEADERS,
            )

        # ── message/send → async, returns submitted immediately ───────────────
        if method == "message/send":
            record = await _submit_task(text, context_id, push_config)
            return {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "result": {
                    "id": record.id,
                    "contextId": record.context_id,
                    "status": {"state": SUBMITTED, "timestamp": record.created_at},
                },
            }

        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }

    # ── POST /message:send  (REST) ────────────────────────────────────────────

    @app.post("/message:send", include_in_schema=False)
    async def _rest_send(request: Request, body: dict):
        _check_auth(request, api_key)
        message = body.get("message", {})
        configuration = body.get("configuration", {})
        context_id = body.get("contextId", "")
        text, context_id = _extract_text_and_context(message, context_id)
        if not text:
            raise HTTPException(400, "No text content in message")
        push_config = _parse_push_config(configuration)
        record = await _submit_task(text, context_id, push_config)
        return JSONResponse(_task_to_response(record), status_code=202)

    # ── POST /message:stream  (REST SSE) ─────────────────────────────────────

    @app.post("/message:stream", include_in_schema=False)
    async def _rest_stream(request: Request, body: dict):
        _check_auth(request, api_key)
        message = body.get("message", {})
        configuration = body.get("configuration", {})
        context_id = body.get("contextId", "")
        text, context_id = _extract_text_and_context(message, context_id)
        if not text:
            raise HTTPException(400, "No text content in message")
        push_config = _parse_push_config(configuration)
        return StreamingResponse(
            _stream_new_task(text, context_id, push_config),
            media_type="text/event-stream",
            headers=_SSE_HEADERS,
        )

    # ── GET /tasks/{task_id} ──────────────────────────────────────────────────

    @app.get("/tasks/{task_id}", include_in_schema=False)
    async def _get_task(task_id: str, request: Request):
        _check_auth(request, api_key)
        record = await _store.get(task_id)
        if record is None:
            raise HTTPException(404, f"Task not found: {task_id}")
        return _task_to_response(record)

    # ── GET /tasks/{task_id}:subscribe  (SSE reconnect) ──────────────────────

    @app.get("/tasks/{task_id}:subscribe", include_in_schema=False)
    async def _subscribe_task(task_id: str, request: Request):
        _check_auth(request, api_key)
        record = await _store.get(task_id)
        if record is None:
            raise HTTPException(404, f"Task not found: {task_id}")

        async def _sse_gen():
            # Emit current snapshot immediately on (re)connect
            r = await _store.get(task_id)
            if r is None:
                return
            yield _sse(_build_status_event(r))
            if r.accumulated_text:
                yield _sse(_build_artifact_event(r, last_chunk=r.state in _TERMINAL))
            if r.state in _TERMINAL:
                return

            # Wait for updates until terminal
            last_text_len = len(r.accumulated_text)
            while True:
                r = await _store.get(task_id)
                if r is None or r.state in _TERMINAL:
                    if r:
                        yield _sse(_build_status_event(r))
                        if r.accumulated_text and len(r.accumulated_text) > last_text_len:
                            yield _sse(_build_artifact_event(r, last_chunk=True))
                    return

                next_event = r._update_event
                try:
                    await asyncio.wait_for(next_event.wait(), timeout=25)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue

                r = await _store.get(task_id)
                if r is None:
                    return
                yield _sse(_build_status_event(r))
                if r.accumulated_text and len(r.accumulated_text) > last_text_len:
                    last_text_len = len(r.accumulated_text)
                    yield _sse(_build_artifact_event(r, last_chunk=r.state in _TERMINAL))
                if r.state in _TERMINAL:
                    return

        return StreamingResponse(_sse_gen(), media_type="text/event-stream", headers=_SSE_HEADERS)

    # ── POST /tasks/{task_id}:cancel ──────────────────────────────────────────

    @app.post("/tasks/{task_id}:cancel", include_in_schema=False)
    async def _cancel_task(task_id: str, request: Request):
        _check_auth(request, api_key)
        record = await _store.get(task_id)
        if record is None:
            raise HTTPException(404, f"Task not found: {task_id}")
        if record.state in _TERMINAL:
            raise HTTPException(409, f"Task already terminal: {record.state}")
        await _store.cancel(task_id)
        record = await _store.update_state(task_id, CANCELED)
        return _task_to_response(record)

    # ── POST /tasks/{task_id}/pushNotificationConfigs ─────────────────────────

    @app.post("/tasks/{task_id}/pushNotificationConfigs", include_in_schema=False)
    async def _create_push_config(task_id: str, request: Request, body: dict):
        _check_auth(request, api_key)
        record = await _store.get(task_id)
        if record is None:
            raise HTTPException(404, f"Task not found: {task_id}")

        auth = body.get("authentication") or {}
        cfg = PushNotificationConfig(
            url=body.get("url", ""),
            token=auth.get("credentials"),
            id=body.get("id", str(uuid4())),
        )
        if not cfg.url:
            raise HTTPException(400, "url is required")

        async with _store._lock:
            record.push_config = cfg

        # If task already terminal, fire webhook immediately
        if record.state in _TERMINAL:
            asyncio.create_task(_deliver_webhook(record, cfg))

        logger.info("[a2a] push config registered for task %s → %s", task_id, cfg.url)
        return {"id": cfg.id, "task_id": task_id, "url": cfg.url}

    logger.info("[a2a] routes registered (streaming=True, pushNotifications=True)")
