"""LLM factory for protoResearcher LangGraph agent.

Both vLLM and Claude route through OpenAI-compatible endpoints,
so we use ChatOpenAI for everything.
"""

import httpx
from langchain_openai import ChatOpenAI

from graph.config import LangGraphConfig


def _detect_vllm_model(api_base: str) -> str | None:
    """Query vLLM /v1/models to get the loaded model."""
    try:
        resp = httpx.get(f"{api_base}/models", timeout=5)
        data = resp.json().get("data", [])
        if data:
            return data[0]["id"]
    except Exception:
        pass
    return None


def create_llm(config: LangGraphConfig) -> ChatOpenAI:
    """Create a LangChain ChatModel from config.

    Both providers (vLLM and Claude via CLIProxyAPI) expose
    OpenAI-compatible endpoints, so ChatOpenAI handles both.
    """
    model_name = config.model_name
    api_base = config.api_base

    if config.model_provider == "vllm":
        api_base = "http://host.docker.internal:8000/v1"
        detected = _detect_vllm_model(api_base)
        if detected:
            model_name = detected
        api_key = "none"
    elif config.model_provider == "cliproxy":
        api_base = config.api_base or "http://127.0.0.1:8317/v1"
        api_key = config.api_key or "protoresearcher-internal"
    else:
        api_base = config.api_base
        api_key = config.api_key

    return ChatOpenAI(
        base_url=api_base,
        api_key=api_key,
        model=model_name,
        temperature=config.temperature,
        max_tokens=config.max_tokens,
    )
