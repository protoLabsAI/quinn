"""Graceful fallback helpers for research tools.

Pattern: try primary → catch → fallback with partial result.
Never silently fail — always log the fallback decision.
"""

import functools
from typing import Callable


def with_fallback(fallback_msg: str = ""):
    """Decorator: catches exceptions in tool actions, returns graceful error with context."""
    def decorator(func: Callable):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            try:
                result = await func(*args, **kwargs)
                # Check for empty results and suggest retry
                if result and ("No " in result[:20] or result.strip() == ""):
                    return f"{result}\n\n_Tip: Try rephrasing your query for better results._"
                return result
            except Exception as e:
                error_type = type(e).__name__
                error_msg = str(e)[:200]
                fallback = fallback_msg or f"This search encountered an issue."
                return (
                    f"**Partial result** (fallback): {fallback}\n\n"
                    f"_Error: {error_type}: {error_msg}_\n\n"
                    f"_Tip: Try a different query or check if the service is available._"
                )
        return wrapper
    return decorator
