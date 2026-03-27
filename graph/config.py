"""LangGraph configuration loader for protoResearcher."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class SubagentDef:
    enabled: bool = True
    tools: list[str] = field(default_factory=list)
    max_turns: int = 30


@dataclass
class LangGraphConfig:
    # Model settings
    model_provider: str = "cliproxy"  # cliproxy or vllm
    model_name: str = "claude-sonnet-4-6"
    api_base: str = "http://127.0.0.1:8317/v1"
    api_key: str = "protoresearcher-internal"
    temperature: float = 0.3
    max_tokens: int = 4096
    max_iterations: int = 75

    # Subagents
    explorer: SubagentDef = field(default_factory=lambda: SubagentDef(
        tools=["discord_feed", "huggingface", "github_trending", "browser"],
        max_turns=30,
    ))
    analyst: SubagentDef = field(default_factory=lambda: SubagentDef(
        tools=["paper_reader", "research_memory", "browser"],
        max_turns=40,
    ))
    writer: SubagentDef = field(default_factory=lambda: SubagentDef(
        tools=["research_memory", "discord_feed"],
        max_turns=20,
    ))

    # Middleware toggles
    knowledge_middleware: bool = True
    audit_middleware: bool = True
    memory_middleware: bool = True

    # Knowledge store
    knowledge_db_path: str = "/sandbox/knowledge/research.db"
    embed_model: str = "nomic-embed-text"
    knowledge_top_k: int = 5

    @classmethod
    def from_yaml(cls, path: str | Path) -> "LangGraphConfig":
        """Load config from YAML file."""
        p = Path(path)
        if not p.exists():
            return cls()

        with open(p) as f:
            data = yaml.safe_load(f) or {}

        model = data.get("model", {})
        subagents = data.get("subagents", {})
        middleware = data.get("middleware", {})
        knowledge = data.get("knowledge", {})

        config = cls(
            model_provider=model.get("provider", cls.model_provider),
            model_name=model.get("name", cls.model_name),
            api_base=model.get("api_base", cls.api_base),
            api_key=model.get("api_key", cls.api_key),
            temperature=model.get("temperature", cls.temperature),
            max_tokens=model.get("max_tokens", cls.max_tokens),
            max_iterations=model.get("max_iterations", cls.max_iterations),
            knowledge_middleware=middleware.get("knowledge", True),
            audit_middleware=middleware.get("audit", True),
            memory_middleware=middleware.get("memory", True),
            knowledge_db_path=knowledge.get("db_path", cls.knowledge_db_path),
            embed_model=knowledge.get("embed_model", cls.embed_model),
            knowledge_top_k=knowledge.get("top_k", cls.knowledge_top_k),
        )

        for name in ("explorer", "analyst", "writer"):
            if name in subagents:
                sub = subagents[name]
                setattr(config, name, SubagentDef(
                    enabled=sub.get("enabled", True),
                    tools=sub.get("tools", getattr(config, name).tools),
                    max_turns=sub.get("max_turns", getattr(config, name).max_turns),
                ))

        return config
