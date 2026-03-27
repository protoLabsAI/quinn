"""Guardrails, query rewriting, caching, and document grading for protoResearcher.

Patterns adopted from production-agentic-rag-course:
- Scope validation before tool calls (0-100 score, threshold 60)
- Query rewriting on sparse results
- SHA256 response caching with TTL
- Binary document relevance grading
"""

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import httpx

# ---------------------------------------------------------------------------
# Guardrails — validate query is within AI/ML research scope
# ---------------------------------------------------------------------------

_GUARDRAIL_PROMPT = """Score this query's relevance to AI/ML research on a scale of 0-100.

Categories that score HIGH (70-100):
- LLM architectures, training, inference optimization
- Model releases, benchmarks, evaluations
- Machine learning methods (RL, DPO, fine-tuning, etc.)
- AI tools, frameworks, libraries
- Research papers, arxiv discussions
- AI agents, tool use, MCP
- Video/image generation models

Categories that score LOW (0-30):
- Cooking, sports, entertainment, politics
- General coding unrelated to AI/ML
- Personal questions, small talk

Respond with ONLY a JSON object like this example: {{"score": 25, "reason": "brief reason"}}

Query: {query}"""


async def check_guardrail(query: str, llm_url: str = "http://127.0.0.1:8317/v1", threshold: int = 60) -> dict:
    """Check if a query is within AI/ML research scope.

    Returns: {"pass": bool, "score": int, "reason": str}
    """
    if not query.strip():
        return {"pass": True, "score": 100, "reason": "empty query"}

    # Quick heuristic bypass for obvious research queries
    research_keywords = [
        "paper", "arxiv", "model", "llm", "training", "inference", "gpu",
        "transformer", "attention", "lora", "dpo", "rlhf", "benchmark",
        "huggingface", "github", "discord", "scan", "research", "digest",
        "moe", "quantiz", "vllm", "agent", "rag", "embedding",
    ]
    query_lower = query.lower()
    if any(kw in query_lower for kw in research_keywords):
        return {"pass": True, "score": 90, "reason": "keyword match"}

    # LLM-based check
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{llm_url}/chat/completions",
                headers={"Authorization": "Bearer protoresearcher-internal"},
                json={
                    "model": "claude-sonnet-4-6",
                    "messages": [{"role": "user", "content": _GUARDRAIL_PROMPT.format(query=query)}],
                    "max_tokens": 100,
                    "temperature": 0.1,
                },
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            # Parse JSON from response — handle think blocks, markdown fences
            import re
            content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
            content = re.sub(r'```json?\s*', '', content).replace('```', '').strip()
            result = json.loads(content)
            score = int(result["score"])
            reason = result.get("reason", "")
            return {"pass": score >= threshold, "score": score, "reason": reason}
    except Exception as e:
        # Fallback: allow the query (don't block on guardrail failure)
        print(f"[guardrail] Check failed: {e}", flush=True)
        return {"pass": True, "score": 50, "reason": f"guardrail check failed ({e}), allowing"}


# ---------------------------------------------------------------------------
# Response caching — SHA256(query) with TTL
# ---------------------------------------------------------------------------

_CACHE_DB_PATH = Path("/sandbox/knowledge/cache.db")
_CACHE_TTL = 86400  # 24 hours


def _get_cache_db() -> sqlite3.Connection:
    _CACHE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(_CACHE_DB_PATH), check_same_thread=False)
    db.execute("""
        CREATE TABLE IF NOT EXISTS response_cache (
            key TEXT PRIMARY KEY,
            response TEXT NOT NULL,
            created_at REAL NOT NULL
        )
    """)
    db.commit()
    return db


_cache_db = None


def _cache_key(query: str) -> str:
    normalized = query.strip().lower()
    return hashlib.sha256(normalized.encode()).hexdigest()


def cache_get(query: str) -> str | None:
    """Check cache for a previous response. Returns None on miss."""
    global _cache_db
    if _cache_db is None:
        try:
            _cache_db = _get_cache_db()
        except Exception:
            return None

    key = _cache_key(query)
    try:
        row = _cache_db.execute(
            "SELECT response, created_at FROM response_cache WHERE key = ?", (key,)
        ).fetchone()
        if row:
            response, created_at = row
            if time.time() - created_at < _CACHE_TTL:
                return response
            # Expired — delete
            _cache_db.execute("DELETE FROM response_cache WHERE key = ?", (key,))
            _cache_db.commit()
    except Exception:
        pass
    return None


def cache_set(query: str, response: str):
    """Store a response in cache."""
    global _cache_db
    if _cache_db is None:
        try:
            _cache_db = _get_cache_db()
        except Exception:
            return

    key = _cache_key(query)
    try:
        _cache_db.execute(
            "INSERT OR REPLACE INTO response_cache (key, response, created_at) VALUES (?, ?, ?)",
            (key, response, time.time()),
        )
        _cache_db.commit()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Document grading — quick binary relevance check
# ---------------------------------------------------------------------------

_GRADE_PROMPT = """Is this document relevant to the research query? Answer with ONLY "yes" or "no".

Research query: {query}

Document excerpt (first 500 chars):
{excerpt}"""


async def grade_document(query: str, content: str, llm_url: str = "http://127.0.0.1:8317/v1") -> bool:
    """Quick binary relevance check. Returns True if relevant."""
    if not content or len(content.strip()) < 50:
        return False  # Too short to be useful

    excerpt = content[:500]

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{llm_url}/chat/completions",
                headers={"Authorization": "Bearer protoresearcher-internal"},
                json={
                    "model": "claude-sonnet-4-6",
                    "messages": [{"role": "user", "content": _GRADE_PROMPT.format(query=query, excerpt=excerpt)}],
                    "max_tokens": 10,
                    "temperature": 0.1,
                },
            )
            resp.raise_for_status()
            answer = resp.json()["choices"][0]["message"]["content"].strip().lower()
            return answer.startswith("yes")
    except Exception:
        # Fallback heuristic: content length > 50 chars = relevant
        return len(content.strip()) > 50


# ---------------------------------------------------------------------------
# Query rewriting — improve sparse queries
# ---------------------------------------------------------------------------

_REWRITE_PROMPT = """The following research query returned sparse or no results. Rewrite it to be more effective for searching AI/ML research papers, models, and repositories.

Original query: {query}

Respond with ONLY the rewritten query (no explanation)."""


async def rewrite_query(query: str, llm_url: str = "http://127.0.0.1:8317/v1") -> str:
    """Rewrite a query for better search results."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{llm_url}/chat/completions",
                headers={"Authorization": "Bearer protoresearcher-internal"},
                json={
                    "model": "claude-sonnet-4-6",
                    "messages": [{"role": "user", "content": _REWRITE_PROMPT.format(query=query)}],
                    "max_tokens": 200,
                    "temperature": 0.3,
                },
            )
            resp.raise_for_status()
            rewritten = resp.json()["choices"][0]["message"]["content"].strip()
            return rewritten if rewritten else query
    except Exception:
        # Fallback: simple keyword expansion
        expansions = {
            "moe": "mixture of experts MoE",
            "rag": "retrieval augmented generation RAG",
            "rlhf": "reinforcement learning human feedback RLHF",
            "dpo": "direct preference optimization DPO",
            "lora": "low-rank adaptation LoRA fine-tuning",
        }
        result = query
        for short, expanded in expansions.items():
            if short in query.lower():
                result = result + " " + expanded
                break
        return result
