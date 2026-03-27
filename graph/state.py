"""QAState — LangGraph state schema for Quinn.

Extends AgentState with QA-specific fields and custom reducers.
"""

import operator
from typing import Annotated, NotRequired

from langchain_core.messages import BaseMessage
from langgraph.prebuilt.chat_agent_executor import AgentState


def merge_findings(
    existing: list[dict] | None, new: list[dict] | None
) -> list[dict]:
    """Reducer: append new QA findings, no deduplication needed."""
    if existing is None:
        return new or []
    if new is None:
        return existing
    return existing + new


def merge_action_items(
    existing: list[str] | None, new: list[str] | None
) -> list[str]:
    """Reducer: accumulate action items to report."""
    if existing is None:
        return new or []
    if new is None:
        return existing
    return existing + new


class QAState(AgentState):
    """State schema for the Quinn LangGraph agent.

    Extends AgentState (which provides `messages` with add_messages reducer).
    Custom fields carry QA context through the graph.
    """

    # Session tracking (Gradio session ID)
    session_id: NotRequired[str]

    # Knowledge context injected by KnowledgeMiddleware before LLM call
    qa_context: NotRequired[str]

    # Accumulated QA findings (append-only via reducer)
    findings: Annotated[list[dict], merge_findings]

    # Action items queued for reporting (Discord, GitHub)
    action_items: Annotated[list[str], merge_action_items]

    # Current QA scope (app name, version, or "all")
    current_scope: NotRequired[str | None]

    # Overall verdict for current QA session
    verdict: NotRequired[str | None]  # PASS, WARN, FAIL

    # Captured message() tool content
    captured_messages: Annotated[list[str], operator.add]
