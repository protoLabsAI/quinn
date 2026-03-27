"""Prometheus metrics for protoResearcher.

Exposes /metrics endpoint for scraping by Prometheus.
Falls back silently if prometheus-client is not installed.
"""

from __future__ import annotations

_enabled = False
_llm_calls = None
_llm_latency = None
_llm_tokens = None
_tool_calls = None
_tool_latency = None
_active_sessions = None


def init():
    global _enabled, _llm_calls, _llm_latency, _llm_tokens
    global _tool_calls, _tool_latency, _active_sessions

    try:
        from prometheus_client import Counter, Histogram, Gauge

        _llm_calls = Counter(
            "protoresearcher_llm_calls_total", "Total LLM API calls",
            ["model", "finish_reason"],
        )
        _llm_latency = Histogram(
            "protoresearcher_llm_latency_seconds", "LLM call latency",
            ["model"], buckets=[0.5, 1, 2, 5, 10, 20, 30, 60, 120],
        )
        _llm_tokens = Counter(
            "protoresearcher_llm_tokens_total", "Total LLM tokens consumed",
            ["model", "direction"],
        )
        _tool_calls = Counter(
            "protoresearcher_tool_calls_total", "Total tool executions",
            ["tool_name", "success"],
        )
        _tool_latency = Histogram(
            "protoresearcher_tool_latency_seconds", "Tool execution latency",
            ["tool_name"], buckets=[0.01, 0.05, 0.1, 0.5, 1, 2, 5, 10, 30],
        )
        _active_sessions = Gauge(
            "protoresearcher_active_sessions", "Active chat sessions",
        )
        _enabled = True
        print("[metrics] Prometheus metrics initialized")
    except ImportError:
        print("[metrics] prometheus-client not installed. Metrics disabled.")


def is_enabled() -> bool:
    return _enabled


def record_llm_call(model: str, finish_reason: str, latency_s: float,
                     tokens_input: int = 0, tokens_output: int = 0):
    if not _enabled:
        return
    _llm_calls.labels(model=model, finish_reason=finish_reason).inc()
    _llm_latency.labels(model=model).observe(latency_s)
    if tokens_input:
        _llm_tokens.labels(model=model, direction="input").inc(tokens_input)
    if tokens_output:
        _llm_tokens.labels(model=model, direction="output").inc(tokens_output)


def record_tool_call(tool_name: str, success: bool, latency_s: float):
    if not _enabled:
        return
    _tool_calls.labels(tool_name=tool_name, success=str(success)).inc()
    _tool_latency.labels(tool_name=tool_name).observe(latency_s)


def session_started():
    if _enabled and _active_sessions:
        _active_sessions.inc()


def session_ended():
    if _enabled and _active_sessions:
        _active_sessions.dec()
