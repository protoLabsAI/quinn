"""Tests for graph.output_format — <scratch_pad>/<output> protocol.

Covers the three shapes of traffic we see live:

1. Well-behaved model — emits both tags in the documented order.
2. Lazy model — emits raw prose without tags (fallback to passthrough).
3. Mixed — emits `<scratch_pad>` but forgets `<output>` wrapper.

Plus streaming-specific edge cases:
- Tag split across chunks (`<outp` + `ut>`)
- Chunk that ends exactly at a tag boundary
- `</output>` spanning two chunks (don't prematurely close)
- Tiny per-char chunks (worst case)
"""

from __future__ import annotations

from graph.output_format import (
    OUTPUT_FORMAT_INSTRUCTIONS,
    OutputFilter,
    extract_output,
)


# ── extract_output (one-shot) ─────────────────────────────────────────────────


def test_extract_output_happy_path():
    text = "<scratch_pad>reasoning here</scratch_pad>\n<output>the answer</output>"
    assert extract_output(text) == "the answer"


def test_extract_output_strips_scratch_when_output_missing():
    text = "<scratch_pad>reasoning</scratch_pad>\nthe answer without output tag"
    assert extract_output(text) == "the answer without output tag"


def test_extract_output_strips_orphan_scratch_open():
    """MiniMax M2.x sometimes leaves scratch_pad unclosed — treat it as
    'everything from the orphan to EOT is reasoning' and strip it."""
    text = "real prose here <scratch_pad>unfinished reasoning never closed"
    assert extract_output(text) == "real prose here"


def test_extract_output_passthrough_no_tags():
    text = "just a plain response with no tags"
    assert extract_output(text) == "just a plain response with no tags"


def test_extract_output_takes_first_output_block():
    text = "<output>first</output> junk <output>second</output>"
    assert extract_output(text) == "first"


def test_extract_output_is_case_insensitive():
    assert extract_output("<OUTPUT>x</OUTPUT>") == "x"


# ── OutputFilter (streaming) ─────────────────────────────────────────────────


def _drive(filt: OutputFilter, chunks: list[str]) -> str:
    """Feed chunks one by one and join emissions + flush tail."""
    emitted = [filt.feed(c) for c in chunks]
    emitted.append(filt.flush())
    return "".join(emitted)


def test_filter_happy_path_single_chunk():
    f = OutputFilter()
    result = _drive(f, [
        "<scratch_pad>reasoning</scratch_pad><output>clean answer</output>"
    ])
    assert result == "clean answer"


def test_filter_happy_path_multi_chunk():
    f = OutputFilter()
    chunks = [
        "<scratch_pad>think",
        "ing more",
        "</scratch_pad><output>the ",
        "answer is ",
        "42</output>",
    ]
    assert _drive(f, chunks) == "the answer is 42"


def test_filter_discards_scratch_pad_content():
    """Inside scratch_pad we emit nothing — no part of the reasoning
    should leak into the downstream stream."""
    f = OutputFilter()
    result = _drive(f, [
        "<scratch_pad>secret reasoning</scratch_pad><output>visible</output>"
    ])
    assert "secret" not in result
    assert "reasoning" not in result
    assert result == "visible"


def test_filter_chunk_boundary_in_opening_tag():
    """`<outp` + `ut>foo` must resolve to `foo` — the split tag must
    reassemble across the chunk boundary."""
    f = OutputFilter()
    assert _drive(f, ["<scratch_pad>r</scratch_pad><outp", "ut>foo</output>"]) == "foo"


def test_filter_chunk_boundary_in_closing_tag():
    """Partial `</out` at end of a chunk must not be emitted — it's
    still part of the closing tag."""
    f = OutputFilter()
    # "foo</out" then "put>" — total inside-output content is "foo"
    result = _drive(f, [
        "<output>foo</out",
        "put>",
    ])
    assert result == "foo"


def test_filter_per_char_chunks():
    """Worst case: one character per chunk. Tag detection still works."""
    f = OutputFilter()
    full = "<scratch_pad>r</scratch_pad><output>hi</output>"
    chunks = list(full)
    assert _drive(f, chunks) == "hi"


def test_filter_fallback_when_no_tags():
    """Model ignored the protocol entirely — after the fallback window,
    emit everything as-is."""
    f = OutputFilter()
    # Keep the total below fallback threshold → flush emits the remainder
    result = _drive(f, ["plain response, no tags"])
    assert result == "plain response, no tags"


def test_filter_fallback_long_prose_no_tags():
    """If we exceed the fallback threshold mid-stream, we should flip
    to passthrough and still emit everything that arrives after."""
    f = OutputFilter()
    # 500 chars of raw prose — exceeds _FALLBACK_THRESHOLD (400)
    long_prose = "x" * 500
    result = _drive(f, [long_prose])
    assert result == long_prose


def test_filter_strips_inline_scratch_in_passthrough():
    """When in passthrough mode (model ignored top-level protocol) we
    still strip any `<scratch_pad>...</scratch_pad>` regions that do
    appear inline, so reasoning never leaks even under fallback."""
    f = OutputFilter()
    # Force passthrough by exceeding threshold, then include inline scratch
    padding = "a" * 450
    chunks = [padding + "<scratch_pad>secret</scratch_pad>b"]
    result = _drive(f, chunks)
    assert "secret" not in result
    assert padding in result
    assert result.endswith("b")


def test_filter_output_missing_close_tag():
    """Model emits `<output>foo` and never closes — flush() should
    release the buffered content so the consumer still sees the
    answer."""
    f = OutputFilter()
    assert _drive(f, ["<scratch_pad>r</scratch_pad><output>unterminated answer"]) == "unterminated answer"


def test_filter_scratch_missing_close_tag_is_dropped():
    """Unterminated scratch at end of stream → nothing emitted. This is
    the safe failure mode: better to lose an answer than to leak raw
    reasoning to the user."""
    f = OutputFilter()
    assert _drive(f, ["<scratch_pad>unterminated reasoning"]) == ""


def test_filter_text_after_output_close_is_dropped():
    """Anything after `</output>` is dropped — the model sometimes adds
    a trailing comment that shouldn't reach the user."""
    f = OutputFilter()
    assert _drive(f, ["<output>answer</output> trailing junk"]) == "answer"


def test_filter_empty_feed():
    f = OutputFilter()
    assert f.feed("") == ""
    assert f.flush() == ""


# ── Prompt integration ──────────────────────────────────────────────────────


def test_instructions_mention_both_tags():
    """Sanity check — the prompt fragment must teach both tags, or the
    filter has nothing to key on."""
    assert "<scratch_pad>" in OUTPUT_FORMAT_INSTRUCTIONS
    assert "<output>" in OUTPUT_FORMAT_INSTRUCTIONS
