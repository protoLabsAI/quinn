"""MemoryMiddleware — queues conversation for async knowledge extraction.

After the agent responds, extracts key topics/findings from the
conversation and stores them in the knowledge base asynchronously.
"""

import threading
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage

from langgraph.prebuilt.chat_agent_executor import AgentState


class MemoryMiddleware(AgentMiddleware):
    """Extract and store research findings after agent responses."""

    def __init__(self, knowledge_store):
        super().__init__()
        self._store = knowledge_store

    def after_agent(self, state, runtime) -> dict | None:
        """Queue conversation for async knowledge extraction."""
        messages = state.get("messages", [])
        if len(messages) < 2:
            return None

        # Extract the last exchange (human + AI)
        last_human = None
        last_ai = None
        for msg in reversed(messages):
            if isinstance(msg, AIMessage) and last_ai is None:
                last_ai = msg.content if isinstance(msg.content, str) else str(msg.content)
            elif isinstance(msg, HumanMessage) and last_human is None:
                last_human = msg.content if isinstance(msg.content, str) else str(msg.content)
            if last_human and last_ai:
                break

        if not last_human or not last_ai:
            return None

        # Only store if the response contains substantial content
        if len(last_ai) < 100:
            return None

        # Async storage — don't block the response
        def _store():
            try:
                self._store.add_finding(
                    content=last_ai[:2000],
                    source="conversation",
                    source_type="chat",
                    finding_type="insight",
                )
            except Exception:
                pass

        threading.Thread(target=_store, daemon=True).start()
        return None

    async def aafter_agent(self, state, runtime) -> dict | None:
        return self.after_agent(state, runtime)
