"""Minimal Tool base class — replaces nanobot dependency for Quinn (LangGraph-only).

Provides the same interface the tool classes expect without requiring the full nanobot package.
"""

from abc import ABC, abstractmethod
from typing import Any


class Tool(ABC):
    """Base class for Quinn tools. Subclasses implement name, description, parameters, and execute."""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def description(self) -> str: ...

    @property
    def parameters(self) -> dict[str, Any]:
        return {}

    @abstractmethod
    async def execute(self, **kwargs: Any) -> str: ...
