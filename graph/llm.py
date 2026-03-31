"""LLM factory for Quinn LangGraph agent.

All models route through the LiteLLM gateway (OpenAI-compatible),
so we use ChatOpenAI for everything.
"""

import os

from langchain_openai import ChatOpenAI

from graph.config import LangGraphConfig


def create_llm(config: LangGraphConfig) -> ChatOpenAI:
    """Create a LangChain ChatModel from config.

    Routes through the LiteLLM gateway which handles provider
    routing (Anthropic, OpenAI, vLLM, etc.) behind a single
    OpenAI-compatible endpoint.
    """
    api_key = config.api_key or os.environ.get("OPENAI_API_KEY", "")

    return ChatOpenAI(
        base_url=config.api_base,
        api_key=api_key,
        model=config.model_name,
        temperature=config.temperature,
        max_tokens=config.max_tokens,
    )
