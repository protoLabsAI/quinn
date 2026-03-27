"""KnowledgeMiddleware — injects relevant knowledge context before LLM calls.

Queries the KnowledgeStore with the last user message and adds
top-k results to the state's research_context field.
"""

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage

from langgraph.prebuilt.chat_agent_executor import AgentState


class KnowledgeMiddleware(AgentMiddleware):
    """Inject knowledge store context before each LLM call."""

    def __init__(self, knowledge_store, top_k: int = 5):
        super().__init__()
        self._store = knowledge_store
        self._top_k = top_k

    def before_model(self, state, runtime) -> dict | None:
        """Query knowledge store with last user message, inject context."""
        messages = state.get("messages", [])
        if not messages:
            return None

        # Find the last human message
        last_human = None
        for msg in reversed(messages):
            if isinstance(msg, HumanMessage):
                last_human = msg.content if isinstance(msg.content, str) else str(msg.content)
                break

        if not last_human:
            return None

        # Search knowledge store
        results = self._store.search(last_human, k=self._top_k)
        if not results:
            return None

        # Format context
        context_parts = ["[Relevant knowledge from previous research:]"]
        for r in results:
            context_parts.append(f"- [{r['table']}] {r['preview']}")

        return {"research_context": "\n".join(context_parts)}

    async def abefore_model(self, state, runtime) -> dict | None:
        """Async version — same logic."""
        return self.before_model(state, runtime)
