"""Structured output protocol for Quinn — `<scratch_pad>` and `<output>` tags.

The model is instructed to wrap internal deliberation in ``<scratch_pad>``
and the user-facing answer in ``<output>``. Server-side, we parse those
tags and forward only the ``<output>`` content to consumers (A2A
artifacts, Gradio chat, subagent return values).

Two entry points:

- ``OutputFilter`` — stateful streaming filter. Feed chunks from
  ``astream_events`` via ``.feed(chunk)``; it returns only the substring
  that should be emitted downstream. Call ``.flush()`` at stream end.

- ``extract_output(text)`` — one-shot helper for non-streaming consumers
  (``_chat_langgraph`` Gradio path, subagent return).

Both have the same fallback semantics: if the model ignores the protocol
and produces raw prose with no tags, we emit it as-is after a grace
window. Real user content never contains literal ``<scratch_pad>`` or
``<output>`` markers, so stripping them is idempotent.

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

Rules:
- Always emit both tags, in that order, exactly once.
- Never include literal `<scratch_pad>` or `<output>` markers inside the
  user-facing content.
- Keep tool-calling deliberation in `<scratch_pad>`. Keep only the
  finished, customer-ready answer in `<output>`.
- If you must defer or ask for clarification, put the question inside
  `<output>` too — the user never sees `<scratch_pad>`.
""".strip()


_SCRATCH_OPEN = "<scratch_pad>"
_SCRATCH_CLOSE = "</scratch_pad>"
_OUTPUT_OPEN = "<output>"
_OUTPUT_CLOSE = "</output>"

_ALL_TAGS = (_SCRATCH_OPEN, _SCRATCH_CLOSE, _OUTPUT_OPEN, _OUTPUT_CLOSE)
_MAX_TAG_LEN = max(len(t) for t in _ALL_TAGS)

# LiteLLM bug #22392 — MiniMax (and some other providers) leak native
# thinking tokens as raw `<think>...</think>` tags in the content stream.
# Strip them as a final pass so they can never reach the user even if the
# model ignores our <scratch_pad>/<output> protocol or emits <think>
# inside an <output> block.
_THINK_RE = re.compile(r"<think>[\s\S]*?</think>", re.IGNORECASE)
_THINK_ORPHAN_RE = re.compile(r"<think>[\s\S]*$", re.IGNORECASE)
_THINK_CLOSE_RE = re.compile(r"</think>\s*", re.IGNORECASE)


def _strip_think(text: str) -> str:
    """Strip native thinking markers emitted by the model provider.

    Three shapes handled (see LiteLLM #22392 for context):
    - Balanced ``<think>...</think>`` pairs
    - Orphaned ``</think>`` (opening was in an earlier chunk, already dropped)
    - Orphaned ``<think>`` with no close (drop from here to EOT — it's reasoning)
    """
    text = _THINK_RE.sub("", text)
    text = _THINK_ORPHAN_RE.sub("", text)
    text = _THINK_CLOSE_RE.sub("", text)
    return text

# If we've buffered this many characters without seeing an opening tag,
# assume the model ignored the protocol and flip to passthrough mode.
# Small enough to surface answers quickly, large enough to not cut a
# legitimate `<scratch_pad>...</scratch_pad>` prefix short.
_FALLBACK_THRESHOLD = 400


# Regex used by extract_output() for the one-shot path. Non-greedy to
# pick the first `<output>...</output>` region.
_OUTPUT_RE = re.compile(r"<output>([\s\S]*?)</output>", re.IGNORECASE)
_SCRATCH_RE = re.compile(r"<scratch_pad>[\s\S]*?</scratch_pad>", re.IGNORECASE)
_ORPHAN_SCRATCH_OPEN_RE = re.compile(r"<scratch_pad>[\s\S]*$", re.IGNORECASE)


def extract_output(text: str) -> str:
    """Return the user-facing content from a complete model response.

    Order of preference:
    1. Content inside the first ``<output>...</output>`` pair.
    2. Full text with ``<scratch_pad>...</scratch_pad>`` regions (and any
       orphaned opening) stripped — covers the case where the model
       skipped ``<output>`` but still wrapped its reasoning.
    3. Full text as-is — model ignored the protocol entirely.

    In all cases, native ``<think>`` tags (LiteLLM #22392 / MiniMax streaming
    bug) are stripped as a final pass.
    """
    m = _OUTPUT_RE.search(text)
    if m:
        return _strip_think(m.group(1).strip())

    stripped = _SCRATCH_RE.sub("", text)
    stripped = _ORPHAN_SCRATCH_OPEN_RE.sub("", stripped)
    return _strip_think(stripped.strip())


class OutputFilter:
    """Stateful streaming filter for ``<scratch_pad>`` / ``<output>`` tags.

    One instance per stream. Feed each chunk from the model via
    ``feed(chunk)`` — the return value is what should go out to the A2A
    SSE consumer. Call ``flush()`` when the upstream stream ends to
    release any final buffered content.

    State machine
    -------------
    - ``pending``: no tag seen yet. Buffer everything; don't emit. If we
      buffer more than ``_FALLBACK_THRESHOLD`` bytes with no tag, assume
      the model ignored the protocol and flip to ``passthrough`` (emit
      everything, stripping any inline scratch_pad).
    - ``scratch``: inside ``<scratch_pad>``. Drop everything until the
      close tag, then flip back to ``pending``.
    - ``output``: inside ``<output>``. Emit everything except the last
      ``_MAX_TAG_LEN`` bytes (held back in case a closing tag is
      mid-emission). On ``</output>`` close, flip to ``done``.
    - ``passthrough``: fallback. Emit everything, still stripping
      ``<scratch_pad>...</scratch_pad>`` regions inline.
    - ``done``: seen ``</output>``; emit nothing further.
    """

    __slots__ = ("_buf", "_state", "_seen_total")

    def __init__(self) -> None:
        self._buf = ""
        self._state = "pending"
        self._seen_total = 0

    def feed(self, chunk: str) -> str:
        """Process an incoming chunk. Return the substring to emit."""
        if not chunk:
            return ""
        self._buf += chunk
        self._seen_total += len(chunk)
        out: list[str] = []
        while self._step(out):
            pass
        return "".join(out)

    def flush(self) -> str:
        """Release remaining buffered content at stream end.

        In ``pending`` (no tag ever seen) we emit the whole buffer —
        the model didn't use the protocol at all. In ``output`` or
        ``passthrough`` we emit whatever's left in the buffer. In
        ``scratch`` or ``done`` we emit nothing.

        All emitted content is passed through ``_strip_think`` so that
        provider-level ``<think>`` tags (LiteLLM #22392) can't escape
        even in the fallback path.
        """
        if self._state in ("pending", "output", "passthrough"):
            out = _strip_think(self._buf)
            self._buf = ""
            return out
        self._buf = ""
        return ""

    # ── internals ────────────────────────────────────────────────────────────

    def _step(self, out: list[str]) -> bool:
        """One pass over the buffer. Returns True if state advanced."""
        if self._state == "done":
            self._buf = ""
            return False

        if self._state == "pending":
            return self._step_pending(out)
        if self._state == "scratch":
            return self._step_scratch()
        if self._state == "output":
            return self._step_output(out)
        if self._state == "passthrough":
            return self._step_passthrough(out)
        return False

    def _step_pending(self, out: list[str]) -> bool:
        # `<output>` wins at any position: everything before it is
        # preamble that the model stuffed outside the protocol.
        output_idx = self._buf.find(_OUTPUT_OPEN)
        if output_idx >= 0:
            self._buf = self._buf[output_idx + len(_OUTPUT_OPEN):]
            self._state = "output"
            return True

        # Early `<scratch_pad>` (before we've seen much content) means
        # the model is following the documented protocol — scratch
        # content is reasoning, should be discarded.
        scratch_idx = self._buf.find(_SCRATCH_OPEN)
        if scratch_idx >= 0 and self._seen_total < _FALLBACK_THRESHOLD:
            self._buf = self._buf[scratch_idx + len(_SCRATCH_OPEN):]
            self._state = "scratch"
            return True

        # Past the fallback threshold with no `<output>` — assume the
        # model is replying in passthrough style. Any inline
        # `<scratch_pad>` still gets stripped, so reasoning can't leak
        # even under fallback.
        if self._seen_total >= _FALLBACK_THRESHOLD:
            self._state = "passthrough"
            return True

        return False

    def _step_scratch(self) -> bool:
        close_idx = self._buf.find(_SCRATCH_CLOSE)
        if close_idx >= 0:
            self._buf = self._buf[close_idx + len(_SCRATCH_CLOSE):]
            self._state = "pending"
            return True
        # Hold back the last _MAX_TAG_LEN bytes in case the close tag is
        # split across chunks, drop the rest.
        if len(self._buf) > _MAX_TAG_LEN:
            self._buf = self._buf[-_MAX_TAG_LEN:]
        return False

    def _step_output(self, out: list[str]) -> bool:
        close_idx = self._buf.find(_OUTPUT_CLOSE)
        if close_idx >= 0:
            out.append(_strip_think(self._buf[:close_idx]))
            self._buf = ""
            self._state = "done"
            return False
        # Emit everything except the tail that might contain a partial
        # `</output>` spanning this and the next chunk.
        if len(self._buf) > _MAX_TAG_LEN:
            emit_end = len(self._buf) - _MAX_TAG_LEN
            out.append(_strip_think(self._buf[:emit_end]))
            self._buf = self._buf[emit_end:]
        return False

    def _step_passthrough(self, out: list[str]) -> bool:
        # Strip inline <scratch_pad>...</scratch_pad> regions that appear
        # in passthrough, emit everything else.
        open_idx = self._buf.find(_SCRATCH_OPEN)
        if open_idx >= 0:
            close_idx = self._buf.find(_SCRATCH_CLOSE, open_idx + len(_SCRATCH_OPEN))
            if close_idx >= 0:
                # Full scratch region present — emit prefix, drop region
                if open_idx > 0:
                    out.append(self._buf[:open_idx])
                self._buf = self._buf[close_idx + len(_SCRATCH_CLOSE):]
                return True
            # Open tag but no close yet — emit prefix, buffer the rest
            if open_idx > 0:
                out.append(_strip_think(self._buf[:open_idx]))
                self._buf = self._buf[open_idx:]
            return False

        # No open tag in buffer. Emit all except the tail that might
        # contain a partial `<scratch_pad>` spanning chunks.
        if len(self._buf) > _MAX_TAG_LEN:
            emit_end = len(self._buf) - _MAX_TAG_LEN
            out.append(_strip_think(self._buf[:emit_end]))
            self._buf = self._buf[emit_end:]
        return False
