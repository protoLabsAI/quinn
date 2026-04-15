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

# MIME type for worldstate-delta-v1 artifacts. Workstacean's effect-domain
# interceptor extracts any DataPart carrying this type on a terminal Task
# and republishes the deltas as world.state.delta bus events, so the GOAP
# planner can update its cached snapshot without waiting for the next poll.
# Ref: protoWorkstacean/docs/extensions/worldstate-delta-v1.md
WORLDSTATE_DELTA_MIME = "application/vnd.protolabs.worldstate-delta+json"

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
    # Most recent tool_start / tool_end status message, e.g. "🔧 file_bug:…"
    # or "✅ file_bug → …". Surfaced in the status frames that ``_watch_task``
    # emits so consumers (SSE clients, :subscribe reconnects) see tool
    # progress without being coupled to the producer's in-process event
    # stream. Cleared to None on terminal transitions.
    last_status_message: str | None = None
    # Observed world-state mutations to emit on the terminal artifact under
    # the worldstate-delta-v1 MIME type. Populated during the run whenever a
    # tool with known effects succeeds (see _chat_langgraph_stream). Shape:
    # [{"domain": "protomaker_board", "path": "data.backlog_count",
    #   "op": "inc", "value": 1}, ...]
    deltas: list[dict] = field(default_factory=list)
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
        status_message: str | None = None,
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
            if status_message is not None:
                record.last_status_message = status_message
            # Terminal transitions clear the status message so post-run
            # subscribers see the final state cleanly, not a stale tool ping.
            if state in _TERMINAL:
                record.last_status_message = None
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

    async def add_delta(self, task_id: str, delta: dict) -> None:
        """Append a worldstate-delta entry to the task's pending list.

        Called when a tool with known mutations (e.g. file_bug) succeeds
        mid-run. The accumulated deltas are emitted as a DataPart artifact
        on the terminal task so Workstacean's effect-domain interceptor can
        publish them as ``world.state.delta`` events.
        """
        async with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                return
            record.deltas.append(delta)

    async def cancel_if_not_terminal(self, task_id: str) -> TaskRecord | None:
        """Atomically cancel a task iff it's not already terminal.

        Replaces the non-atomic get-state-then-update sequence in
        ``_cancel_task``: a runner could race between the check and the write
        and transition to COMPLETED while the caller assumed it was still
        cancellable. Returns the updated record, or None if the task was
        missing or already terminal (signal: HTTP 409 from the caller).
        """
        async with self._lock:
            record = self._tasks.get(task_id)
            if record is None or record.state in _TERMINAL:
                return None
            record.state = CANCELED
            record.updated_at = _now_iso()
            old_event = record._update_event
            record._update_event = asyncio.Event()
        old_event.set()
        record._cancel_event.set()
        if record._bg_task and not record._bg_task.done():
            record._bg_task.cancel()
        return record

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


def _terminal_artifact_parts(record: TaskRecord) -> list[dict]:
    """Build the terminal artifact's ``parts`` list: the accumulated text plus
    a worldstate-delta DataPart if any deltas were observed during the run.

    Workstacean's A2A executor scans artifact parts for DataParts carrying
    ``metadata.mimeType = WORLDSTATE_DELTA_MIME`` and surfaces the payload
    through its effect-domain interceptor. Emitting an empty delta list
    would be confusing, so the DataPart is added only when non-empty.
    """
    parts: list[dict] = []
    if record.accumulated_text:
        parts.append({"kind": "text", "text": record.accumulated_text})
    if record.deltas:
        parts.append({
            "kind": "data",
            "data": {"deltas": list(record.deltas)},
            "metadata": {"mimeType": WORLDSTATE_DELTA_MIME},
        })
    return parts


def _task_to_response(record: TaskRecord) -> dict:
    resp: dict[str, Any] = {
        "id": record.id,
        "contextId": record.context_id,
        "status": {"state": record.state, "timestamp": record.updated_at},
    }
    parts = _terminal_artifact_parts(record)
    if parts:
        resp["artifacts"] = [{"parts": parts}]
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
    elif record.last_status_message and record.state not in _TERMINAL:
        # Surface tool_start / tool_end messages to SSE subscribers. Cleared
        # on terminal transitions so consumers see the final state cleanly.
        evt["status"]["message"] = {
            "role": "agent",
            "parts": [{"kind": "text", "text": record.last_status_message}],
        }
    return evt


def _build_artifact_event(
    record: TaskRecord,
    *,
    text: str | None = None,
    append: bool = True,
    last_chunk: bool,
) -> dict:
    """Build an artifact update event frame.

    Defaults match the historical behaviour (full record.accumulated_text,
    append=True) so existing callers remain correct. Consumers that want to
    emit an incremental delta should pass ``text=<delta>`` and
    ``append=True``; callers that are replacing the full artifact (e.g.
    initial snapshot on :subscribe reconnect, terminal frame) should pass
    ``text=<full>`` and ``append=False``.

    On terminal frames with accumulated worldstate deltas, the full terminal
    artifact (text + DataPart) is emitted via ``_terminal_artifact_parts``
    instead — see ``_build_terminal_artifact_event``.
    """
    body_text = text if text is not None else record.accumulated_text
    return {
        "task_id": record.id,
        "context_id": record.context_id,
        "artifact": {"parts": [{"kind": "text", "text": body_text}]},
        "append": append,
        "last_chunk": last_chunk,
    }


def _build_terminal_artifact_event(record: TaskRecord) -> dict:
    """Terminal artifact: full text + worldstate-delta DataPart if any.

    Used on COMPLETED frames for both the streaming and subscribe paths so
    consumers see the authoritative final artifact (``append: false``,
    ``last_chunk: true``) with every accumulated delta attached.
    """
    return {
        "task_id": record.id,
        "context_id": record.context_id,
        "artifact": {
            "parts": _terminal_artifact_parts(record),
            "append": False,
            "last_chunk": True,
        },
        "append": False,
        "last_chunk": True,
    }


def _extract_text_and_context(message: dict, context_id: str = "") -> tuple[str, str]:
    """Pull text + contextId out of an A2A Message dict."""
    parts = message.get("parts", [])
    text = next((p.get("text", "") for p in parts if p.get("kind") == "text"), "")
    if not text:
        text = next((p.get("text", "") for p in parts), "")
    context_id = context_id or f"a2a-{uuid4()}"
    return text, context_id


def _is_safe_webhook_url(url: str) -> bool:
    """Reject unsafe webhook targets before we accept a push config.

    Defends against SSRF: a client supplying http://169.254.169.254/... or
    http://10.0.0.1/... as a webhook would have Quinn POST task payloads to
    internal cloud metadata, adjacent private services, or the loopback
    device. One-time resolution is not a full defence against DNS rebinding,
    but it closes the trivial "just give it a RFC1918 literal" vector.

    Accepts:  http/https URLs to globally-routable IPs.
    Rejects:  non-http(s) schemes, loopback, link-local, private (RFC1918),
              multicast, reserved, and unresolvable hostnames.
    """
    import ipaddress
    import socket
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.hostname
    if not host:
        return False

    # If the hostname is already a literal IP, check it directly; otherwise
    # resolve once and check every returned address (multi-A / AAAA).
    candidates: list[str] = []
    try:
        ipaddress.ip_address(host)
        candidates = [host]
    except ValueError:
        try:
            # getaddrinfo returns (family, type, proto, canonname, sockaddr);
            # sockaddr[0] is the IP for both AF_INET and AF_INET6.
            candidates = [info[4][0] for info in socket.getaddrinfo(host, None)]
        except socket.gaierror:
            return False

    for addr in candidates:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return False
        if (
            ip.is_loopback
            or ip.is_link_local
            or ip.is_private
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return False
    return True


def _parse_push_config(configuration: dict) -> PushNotificationConfig | None:
    cfg = (configuration or {}).get("pushNotificationConfig") or (configuration or {}).get("taskPushNotificationConfig")
    if not cfg or not cfg.get("url"):
        return None
    url = cfg["url"]
    if not _is_safe_webhook_url(url):
        logger.warning("[a2a] rejected unsafe webhook url: %s", url)
        return None
    auth = cfg.get("authentication") or {}
    return PushNotificationConfig(
        url=url,
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
    if record.state == COMPLETED:
        parts = _terminal_artifact_parts(record)
        if parts:
            payload["artifact"] = {
                "parts": parts,
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


# Strong references to in-flight webhook delivery tasks. Without this the
# asyncio loop holds only weak references (Python 3.11+ docs warn about this
# explicitly) and a pending delivery can be garbage-collected mid-retry,
# silently dropping the status transition a caller registered a webhook to
# receive.
_pending_webhook_tasks: set[asyncio.Task] = set()


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
        task = asyncio.create_task(_deliver_webhook(record, cfg))
        _pending_webhook_tasks.add(task)
        task.add_done_callback(_pending_webhook_tasks.discard)


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
                canceled = await _store.update_state(task_id, CANCELED)
                if canceled is not None:
                    await _push(canceled)
                return

            if event_type == "text":
                accumulated += payload
                await _store.update_state(task_id, WORKING, accumulated_text=accumulated)

            elif event_type in ("tool_start", "tool_end"):
                # Status update only — preserve the tool message on the record
                # so SSE subscribers see it (both the initial message/sendStream
                # consumer and any :subscribe reconnect).
                await _store.update_state(
                    task_id, WORKING,
                    accumulated_text=accumulated,
                    status_message=payload,
                )

            elif event_type == "delta":
                # Worldstate-delta emitted by a tool that mutated shared state.
                # Stored on the record and emitted on the terminal artifact.
                if isinstance(payload, dict):
                    await _store.add_delta(task_id, payload)

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
        canceled = await _store.update_state(task_id, CANCELED)
        if canceled is not None:
            await _push(canceled)
        raise
    except Exception as exc:
        logger.exception("[a2a] background task %s crashed", task_id)
        record = await _store.update_state(task_id, FAILED, error=str(exc))
        if record is not None:
            await _push(record)


# ── SSE helpers ───────────────────────────────────────────────────────────────

_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

# How long a subscriber blocks on the rotating _update_event before yielding
# a keepalive comment. Tuned to stay comfortably below typical reverse-proxy
# idle timeouts (nginx default: 60s) while minimising chatter.
_SSE_KEEPALIVE_TIMEOUT_S = 25


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


def _sse_rpc(rpc_id: Any, result: dict) -> str:
    return _sse({"jsonrpc": "2.0", "id": rpc_id, "result": result})


# ── Shared SSE consumer ───────────────────────────────────────────────────────


async def _watch_task(
    task_id: str,
    start_text_len: int = 0,
) -> AsyncGenerator[tuple[str, TaskRecord | None, str | None], None]:
    """Yield change notifications for a running task until it terminates.

    This is the shared consumer behind both ``message/sendStream`` and
    ``:subscribe``. The producer (``_run_task_background``) runs independently
    as ``record._bg_task``; the consumer only reads the store and awaits the
    rotating ``_update_event``. Dropping the SSE connection no longer stops
    the producer — a reconnect via ``:subscribe`` resumes where the previous
    connection left off.

    Yield tuples are ``(kind, record, payload)`` where kind is one of:
      - ``"status"``: state transition or tool message. payload is None;
        consumers format via ``_build_status_event(record)``.
      - ``"text_delta"``: ``accumulated_text`` grew. payload is the new
        suffix only — never the full accumulated text — so reconnects do
        not duplicate content on the wire.
      - ``"keepalive"``: timed out waiting for an update; record is None.
        Consumers should emit ``": keepalive\\n\\n"`` to keep the proxy happy.

    ``start_text_len`` is the length of ``accumulated_text`` the client has
    already seen. First-connect callers pass 0. :subscribe reconnects pass
    ``len(record.accumulated_text)`` so only genuinely-new suffix text is
    emitted. Callers that want to replay the full artifact on reconnect
    (initial snapshot UX) emit that frame themselves and then start the
    watcher at ``start_text_len = len(record.accumulated_text)``.

    Terminates when the task is deleted or reaches a terminal state. The
    final status frame is always yielded before return.
    """
    record = await _store.get(task_id)
    if record is None:
        return

    last_sent_len = start_text_len

    # Emit the current snapshot first so (re)connecting clients see the
    # state of the world before blocking on the next update.
    yield ("status", record, None)
    if record.accumulated_text and len(record.accumulated_text) > last_sent_len:
        delta = record.accumulated_text[last_sent_len:]
        last_sent_len = len(record.accumulated_text)
        yield ("text_delta", record, delta)

    if record.state in _TERMINAL:
        return

    while True:
        r = await _store.get(task_id)
        if r is None:
            return

        next_event = r._update_event
        try:
            await asyncio.wait_for(next_event.wait(), timeout=_SSE_KEEPALIVE_TIMEOUT_S)
        except asyncio.TimeoutError:
            yield ("keepalive", None, None)
            continue

        r = await _store.get(task_id)
        if r is None:
            return

        yield ("status", r, None)
        if r.accumulated_text and len(r.accumulated_text) > last_sent_len:
            delta = r.accumulated_text[last_sent_len:]
            last_sent_len = len(r.accumulated_text)
            yield ("text_delta", r, delta)

        if r.state in _TERMINAL:
            return


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
        """Submit a new task and stream its lifecycle as JSON-RPC SSE frames.

        The producer (``_run_task_background``) runs as ``record._bg_task``
        independently of this generator — if the SSE connection drops, work
        continues and the client can reattach via ``:subscribe``.

        Emits incremental text deltas only (``append: true``) for the
        streaming window, and the authoritative terminal artifact (full
        text + worldstate-delta DataPart, ``append: false``) on the terminal
        frame. Reconnects see the pre-disconnect text via ``:subscribe``'s
        snapshot, then continue from there.
        """
        record = await _submit_task(text, context_id, push_config)
        task_id = record.id

        # Frame 0: submitted — client gets task_id before the producer starts.
        # Watcher will emit the next status frame (WORKING) once the bg task
        # transitions, so there's no duplication with the watcher's first
        # snapshot.
        yield _sse_rpc(
            rpc_id,
            {
                "id": task_id,
                "contextId": context_id,
                "status": {"state": SUBMITTED, "timestamp": record.created_at},
            },
        )

        try:
            async for kind, r, payload in _watch_task(task_id, start_text_len=0):
                if kind == "keepalive":
                    yield ": keepalive\n\n"
                    continue
                if r is None:
                    return

                if kind == "status":
                    base = {"state": r.state, "timestamp": r.updated_at}
                    if r.error_message:
                        base["message"] = {
                            "role": "agent",
                            "parts": [{"kind": "text", "text": r.error_message}],
                        }
                    elif r.last_status_message and r.state not in _TERMINAL:
                        base["message"] = {
                            "role": "agent",
                            "parts": [{"kind": "text", "text": r.last_status_message}],
                        }
                    frame: dict[str, Any] = {
                        "id": task_id,
                        "contextId": context_id,
                        "status": base,
                    }
                    if r.state == COMPLETED:
                        # Terminal frame carries the full artifact (text +
                        # worldstate-delta DataPart) as append=false so
                        # clients replace whatever incremental text they
                        # already assembled.
                        frame["artifacts"] = [{
                            "parts": _terminal_artifact_parts(r),
                            "append": False,
                            "last_chunk": True,
                        }]
                    yield _sse_rpc(rpc_id, frame)

                elif kind == "text_delta":
                    # Mid-run delta: just the new suffix, append=true. Only
                    # emitted when the task is still WORKING — terminal
                    # deltas roll into the status frame's artifacts list.
                    if r.state not in _TERMINAL and payload:
                        yield _sse_rpc(
                            rpc_id,
                            {
                                "id": task_id,
                                "contextId": context_id,
                                "status": {"state": WORKING},
                                "artifacts": [{
                                    "parts": [{"kind": "text", "text": payload}],
                                    "append": True,
                                    "last_chunk": False,
                                }],
                            },
                        )
        except asyncio.CancelledError:
            # The HTTP connection closed (client disconnect). DO NOT cancel
            # the background task — it continues running, and :subscribe
            # can reattach. Just stop emitting.
            logger.info("[a2a] stream consumer for %s disconnected; bg task continues", task_id)
            raise

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
            # Initial snapshot: emit whatever text is already on the record as
            # an append=False replacement frame, then let _watch_task continue
            # from there with append=True deltas only. This gives reconnecting
            # clients one full payload + future incrementals — no duplication.
            snapshot = await _store.get(task_id)
            if snapshot is None:
                return
            snapshot_len = len(snapshot.accumulated_text)
            if snapshot.accumulated_text:
                yield _sse(_build_artifact_event(
                    snapshot,
                    text=snapshot.accumulated_text,
                    append=False,
                    last_chunk=snapshot.state in _TERMINAL,
                ))

            try:
                async for kind, r, payload in _watch_task(
                    task_id, start_text_len=snapshot_len,
                ):
                    if kind == "keepalive":
                        yield ": keepalive\n\n"
                        continue
                    if r is None:
                        return

                    if kind == "status":
                        if r.state == COMPLETED:
                            # Terminal: authoritative full artifact (text +
                            # worldstate-delta DataPart) as append=false.
                            yield _sse(_build_status_event(r))
                            yield _sse(_build_terminal_artifact_event(r))
                        else:
                            yield _sse(_build_status_event(r))

                    elif kind == "text_delta":
                        if r.state not in _TERMINAL and payload:
                            yield _sse(_build_artifact_event(
                                r, text=payload, append=True, last_chunk=False,
                            ))
            except asyncio.CancelledError:
                logger.info(
                    "[a2a] subscribe consumer for %s disconnected; bg task continues",
                    task_id,
                )
                raise

        return StreamingResponse(_sse_gen(), media_type="text/event-stream", headers=_SSE_HEADERS)

    # ── POST /tasks/{task_id}:cancel ──────────────────────────────────────────

    @app.post("/tasks/{task_id}:cancel", include_in_schema=False)
    async def _cancel_task(task_id: str, request: Request):
        _check_auth(request, api_key)
        # Single atomic read+write under the store lock. The previous
        # get → sleep → cancel → update sequence could race with the
        # background runner and clobber a legitimate COMPLETED state.
        if await _store.get(task_id) is None:
            raise HTTPException(404, f"Task not found: {task_id}")
        record = await _store.cancel_if_not_terminal(task_id)
        if record is None:
            # Either disappeared under us (very unlikely) or already terminal.
            existing = await _store.get(task_id)
            if existing is None:
                raise HTTPException(404, f"Task not found: {task_id}")
            raise HTTPException(409, f"Task already terminal: {existing.state}")
        # Webhook consumers should hear about the cancel transition, same as
        # any other terminal state.
        await _push(record)
        return _task_to_response(record)

    # ── POST /tasks/{task_id}/pushNotificationConfigs ─────────────────────────

    @app.post("/tasks/{task_id}/pushNotificationConfigs", include_in_schema=False)
    async def _create_push_config(task_id: str, request: Request, body: dict):
        _check_auth(request, api_key)
        record = await _store.get(task_id)
        if record is None:
            raise HTTPException(404, f"Task not found: {task_id}")

        url = body.get("url", "")
        if not url:
            raise HTTPException(400, "url is required")
        if not _is_safe_webhook_url(url):
            raise HTTPException(
                400,
                "webhook url rejected: must be http/https, public IP, "
                "not loopback/private/link-local/multicast/reserved",
            )

        auth = body.get("authentication") or {}
        cfg = PushNotificationConfig(
            url=url,
            token=auth.get("credentials"),
            id=body.get("id", str(uuid4())),
        )

        async with _store._lock:
            record.push_config = cfg

        # If task already terminal, fire webhook immediately via the tracked
        # _push path so the delivery task isn't GC'd mid-retry.
        if record.state in _TERMINAL:
            await _push(record)

        logger.info("[a2a] push config registered for task %s → %s", task_id, cfg.url)
        return {"id": cfg.id, "task_id": task_id, "url": cfg.url}

    logger.info("[a2a] routes registered (streaming=True, pushNotifications=True)")
