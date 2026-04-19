"""Structured output protocol for Quinn — `<scratch_pad>` / `<output>` tags.

The model is instructed to wrap internal deliberation in ``<scratch_pad>``
and the user-facing answer in ``<output>``. Server-side, we parse those
tags and forward only the ``<output>`` content to consumers (A2A
artifacts, Gradio chat, subagent return values).

We deliberately do NOT parse the protocol mid-stream — chunk-boundary
tag splitting turned that into a state-machine rabbit hole and the
per-token text rendering Workstacean was doing didn't add real value.
Instead, ``_chat_langgraph_stream`` accumulates the model's tokens
silently while still emitting tool-start / tool-end status events, then
passes the complete text through ``extract_output`` once on the
terminal ``done`` frame. The consumer sees tool progress during the run
and the clean final artifact at completion.

``_strip_reasoning`` also removes provider-emitted ``<think>...</think>``
regions (LiteLLM bug #22392 leaks these as raw tags from MiniMax) and
any orphaned scratch_pad / think openings.

The prompt fragment that teaches the protocol to the model lives in
``OUTPUT_FORMAT_INSTRUCTIONS`` below; ``graph.prompts`` appends it to
both the lead agent and subagent system prompts.
"""

from __future__ import annotations

import re

OUTPUT_FORMAT_INSTRUCTIONS = """
# Response format

Structure every response as:

    <scratch_pad>
    Internal reasoning — which tools to call, what you're learning from
    each result, how you'll assemble the final answer. This is not shown
    to the user; use it freely to think.
    </scratch_pad>
    <output>
    The user-facing answer. This is what lands in the A2A artifact /
    Discord / Gradio chat. Be clean, scannable, markdown-formatted.
    </output>
    <confidence>0.85</confidence>
    <confidence_explanation>
    One short sentence on why this score — what made you sure or unsure.
    </confidence_explanation>

Rules:
- Always emit `<scratch_pad>` and `<output>`, in that order, exactly once.
- Never include literal `<scratch_pad>` / `<output>` / `<confidence>` /
  `<confidence_explanation>` markers inside the user-facing content.
- Keep tool-calling deliberation in `<scratch_pad>`. Keep only the
  finished, customer-ready answer in `<output>`.
- If you must defer or ask for clarification, put the question inside
  `<output>` too — the user never sees `<scratch_pad>`.

Confidence (required on terminal responses):
- `<confidence>` is a number in [0, 1] — your self-assessed confidence
  that the `<output>` is correct and complete. Calibrate honestly: a
  0.9 should mean you'd bet on it; a 0.5 means roughly a coin flip.
- `<confidence_explanation>` is one short sentence on what drove the
  score — spec clarity, tool-result completeness, edge cases unchecked.
- Omit both tags when you're only calling tools (no final answer yet).
  Include them once, on the turn that contains the final `<output>`.
""".strip()


_OUTPUT_RE = re.compile(r"<output>([\s\S]*?)</output>", re.IGNORECASE)
_SCRATCH_RE = re.compile(r"<scratch_pad>[\s\S]*?</scratch_pad>", re.IGNORECASE)
_ORPHAN_SCRATCH_OPEN_RE = re.compile(r"<scratch_pad>[\s\S]*$", re.IGNORECASE)
_THINK_RE = re.compile(r"<think>[\s\S]*?</think>", re.IGNORECASE)
_ORPHAN_THINK_OPEN_RE = re.compile(r"<think>[\s\S]*$", re.IGNORECASE)
_ORPHAN_THINK_CLOSE_RE = re.compile(r"</think>\s*", re.IGNORECASE)
_CONFIDENCE_RE = re.compile(
    r"<confidence>\s*(-?[\d.]+)\s*</confidence>", re.IGNORECASE,
)
_CONFIDENCE_EXPLANATION_RE = re.compile(
    r"<confidence_explanation>([\s\S]*?)</confidence_explanation>", re.IGNORECASE,
)
_CONFIDENCE_ANY_RE = re.compile(
    r"<confidence(?:_explanation)?>[\s\S]*?</confidence(?:_explanation)?>",
    re.IGNORECASE,
)


def _strip_reasoning(text: str) -> str:
    """Remove all reasoning markers (``<think>``, ``<scratch_pad>``, and
    orphaned variants) from a complete response.

    Idempotent — real user content should never contain literal tag
    markers, so applying this twice is safe.
    """
    text = _THINK_RE.sub("", text)
    text = _ORPHAN_THINK_OPEN_RE.sub("", text)
    text = _ORPHAN_THINK_CLOSE_RE.sub("", text)
    text = _SCRATCH_RE.sub("", text)
    text = _ORPHAN_SCRATCH_OPEN_RE.sub("", text)
    text = _CONFIDENCE_ANY_RE.sub("", text)
    return text


def extract_confidence(text: str) -> tuple[float | None, str | None]:
    """Pull ``(confidence, explanation)`` out of a complete model response.

    Returns ``(None, None)`` if the model didn't emit a `<confidence>` tag.
    Clamps confidence to [0, 1]. Unparseable numbers return ``None`` so
    ``_chat_langgraph_stream`` emits no confidence event — the workstacean
    interceptor no-ops on missing confidence, which is the correct
    fallback for a malformed self-report.
    """
    m = _CONFIDENCE_RE.search(text)
    if not m:
        return None, None
    try:
        value = float(m.group(1))
    except ValueError:
        return None, None
    value = max(0.0, min(1.0, value))
    explanation_m = _CONFIDENCE_EXPLANATION_RE.search(text)
    explanation = None
    if explanation_m:
        cleaned = explanation_m.group(1).strip()
        explanation = cleaned or None
    return value, explanation


def extract_output(text: str) -> str:
    """Return the user-facing content from a complete model response.

    Order of preference:
    1. Content inside the first ``<output>...</output>`` pair (still
       with any nested reasoning markers stripped).
    2. Full text with all reasoning markers stripped — covers the case
       where the model skipped ``<output>`` but still wrapped scratch.
    3. Raw text if none of the above triggers — the model ignored every
       convention. Rare in practice once the prompt is in place.
    """
    m = _OUTPUT_RE.search(text)
    if m:
        return _strip_reasoning(m.group(1)).strip()
    return _strip_reasoning(text).strip()
