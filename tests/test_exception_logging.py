"""Regression tests for exception traceback logging in _chat_langgraph(_stream).

Background: a multi-tool skill (board_audit / qa_report / security_triage)
was failing with `expected str, bytes or os.PathLike object, not NoneType`.
The A2A handler surfaced str(e) fine but the frame location was gone —
diagnosis required guess-and-check because the stack never reached stderr.

These tests lock in that every unhandled exception in the LangGraph entry
points logs a full traceback via the module logger, so future runtime bugs
are debuggable from `docker logs quinn` alone.
"""

from __future__ import annotations

import logging
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _stub_langchain_core():
    """Stub the minimal langchain_core surface _chat_langgraph* imports.

    Local dev environments don't always have langchain_core installed;
    CI + the container do. Stubbing keeps the test hermetic — we're
    exercising the exception handler path, not the real graph runtime."""
    if "langchain_core" in sys.modules:
        return
    lc = types.ModuleType("langchain_core")
    messages = types.ModuleType("langchain_core.messages")

    class _Msg:
        def __init__(self, content=""):
            self.content = content

    messages.HumanMessage = _Msg
    messages.AIMessage = _Msg
    lc.messages = messages
    sys.modules["langchain_core"] = lc
    sys.modules["langchain_core.messages"] = messages


def _stub_tracing():
    """Stub the tracing module to avoid langfuse imports + side effects."""
    if "tracing" in sys.modules:
        return
    import contextlib

    tracing = types.ModuleType("tracing")

    @contextlib.asynccontextmanager
    async def _noop_session(*args, **kwargs):
        yield None

    tracing.trace_session = _noop_session
    tracing.flush = lambda: None
    tracing.is_enabled = lambda: False
    tracing.current_trace_id = lambda: ""
    tracing.current_session_id = lambda: ""
    sys.modules["tracing"] = tracing


_stub_langchain_core()
_stub_tracing()


@pytest.mark.asyncio
async def test_chat_langgraph_stream_logs_traceback_on_exception(caplog):
    """_chat_langgraph_stream catches Exception and yields str(e) to the
    A2A handler. It MUST also log the full traceback to stderr first —
    otherwise docker logs shows only the access-log lines and the frame
    location is lost."""
    from server import _chat_langgraph_stream

    caplog.set_level(logging.ERROR, logger="quinn.server")

    async def _exploding_events(*args, **kwargs):
        # Shape the raise so it mirrors the production failure: a path
        # op getting None. Any exception exercises the log path.
        raise TypeError(
            "expected str, bytes or os.PathLike object, not NoneType"
        )
        yield  # make this an async generator even though we raise first

    fake_graph = MagicMock()
    fake_graph.astream_events = _exploding_events

    events = []
    with patch("server._graph", fake_graph):
        async for kind, payload in _chat_langgraph_stream("hi", "s-err"):
            events.append((kind, payload))

    # The error event still reaches the A2A handler so the task transitions
    # to FAILED with a readable message.
    assert ("error", "expected str, bytes or os.PathLike object, not NoneType") in events

    # AND the traceback hit the logger (caplog captures module logger output).
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert error_records, "no ERROR record logged — traceback would be lost"
    rec = error_records[0]
    assert rec.name == "quinn.server"
    assert "quinn-a2a-stream" in rec.getMessage()
    assert "s-err" in rec.getMessage()
    # logger.exception() carries exc_info; formatter would render traceback.
    assert rec.exc_info is not None
    assert rec.exc_info[0] is TypeError


@pytest.mark.asyncio
async def test_chat_langgraph_non_stream_logs_traceback_on_exception(caplog):
    """Same guarantee on the non-streaming path that Gradio chat uses."""
    from server import _chat_langgraph

    caplog.set_level(logging.ERROR, logger="quinn.server")

    fake_graph = MagicMock()
    fake_graph.ainvoke = AsyncMock(
        side_effect=TypeError(
            "expected str, bytes or os.PathLike object, not NoneType"
        )
    )
    with patch("server._graph", fake_graph):
        result = await _chat_langgraph("hi", "s-err")

    # Caller (Gradio) still gets a readable assistant message
    assert "**Error:**" in result[0]["content"]

    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert error_records, "no ERROR record logged"
    rec = error_records[0]
    assert rec.name == "quinn.server"
    assert "quinn-chat-lg" in rec.getMessage()
    assert rec.exc_info is not None
    assert rec.exc_info[0] is TypeError
