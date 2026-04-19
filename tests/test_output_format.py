"""Tests for graph.output_format — <scratch_pad>/<output> protocol.

Covers the three shapes of traffic we see live:

1. Well-behaved model — emits both tags in the documented order.
2. Mixed — emits `<scratch_pad>` but forgets `<output>` wrapper.
3. Native thinking — provider (MiniMax, DeepSeek, Qwen3) leaks
   `<think>...</think>` regions that the filter must also strip.

We no longer parse mid-stream — ``_chat_langgraph_stream`` accumulates
the model's tokens silently and passes the complete text through
``extract_output`` exactly once on the terminal frame. So only the
one-shot path is tested.
"""

from __future__ import annotations

from graph.output_format import (
    OUTPUT_FORMAT_INSTRUCTIONS,
    _strip_reasoning,
    extract_confidence,
    extract_output,
)


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


def test_extract_output_strips_think_inside_output():
    """LiteLLM #22392: MiniMax leaks `<think>...</think>` blocks inside
    `<output>`. _strip_reasoning runs over the output region too."""
    text = (
        "<output>head <think>inner reasoning</think> tail</output>"
    )
    assert extract_output(text) == "head  tail"


def test_extract_output_strips_orphan_think():
    """Orphaned `<think>` opening with no close — drop to EOT."""
    text = "<output>visible <think>unfinished reasoning"
    # Output is unclosed, falls to passthrough branch which strips orphan think
    result = extract_output(text)
    assert "<think>" not in result
    assert "unfinished" not in result
    assert "visible" in result


def test_extract_output_strips_orphan_think_close():
    """Orphaned `</think>` (opener was somewhere upstream already)."""
    text = "<output>real answer</think></output>"
    assert extract_output(text) == "real answer"


def test_strip_reasoning_idempotent():
    """Real content never contains literal tag markers, so applying
    _strip_reasoning twice is safe and produces the same result."""
    text = "<think>THINK_BODY</think>real<scratch_pad>SCRATCH_BODY</scratch_pad>content"
    once = _strip_reasoning(text)
    twice = _strip_reasoning(once)
    assert once == twice
    assert "THINK_BODY" not in once
    assert "SCRATCH_BODY" not in once
    assert once == "realcontent"


def test_instructions_mention_both_tags():
    """Sanity check — the prompt fragment must teach both tags."""
    assert "<scratch_pad>" in OUTPUT_FORMAT_INSTRUCTIONS
    assert "<output>" in OUTPUT_FORMAT_INSTRUCTIONS


# ── confidence-v1 parsing ────────────────────────────────────────────────────


def test_extract_confidence_happy_path():
    text = (
        "<scratch_pad>think</scratch_pad>"
        "<output>answer</output>"
        "<confidence>0.82</confidence>"
        "<confidence_explanation>Spec clear; tests pass.</confidence_explanation>"
    )
    conf, expl = extract_confidence(text)
    assert conf == 0.82
    assert expl == "Spec clear; tests pass."


def test_extract_confidence_returns_none_when_absent():
    """Model didn't self-report — both values must be None so the stream
    skips the confidence event entirely."""
    assert extract_confidence("<output>answer</output>") == (None, None)


def test_extract_confidence_clamps_out_of_range():
    """Miscalibrated models occasionally emit >1 or <0. Clamp so the
    DataPart on the wire stays in-spec."""
    high, _ = extract_confidence("<confidence>1.3</confidence>")
    low, _ = extract_confidence("<confidence>-0.4</confidence>")
    assert high == 1.0
    assert low == 0.0


def test_extract_confidence_unparseable_returns_none():
    """Garbage like <confidence>probably</confidence> → None, not a crash."""
    conf, expl = extract_confidence("<confidence>probably</confidence>")
    assert conf is None
    assert expl is None


def test_extract_confidence_explanation_optional():
    """A <confidence> tag without an accompanying explanation still
    yields the score (explanation is optional per the spec)."""
    conf, expl = extract_confidence("<confidence>0.5</confidence>")
    assert conf == 0.5
    assert expl is None


def test_extract_output_strips_confidence_tags():
    """Confidence markers must not leak into the user-facing output —
    they're metadata for the interceptor, not prose for the reader."""
    text = (
        "<output>the answer</output>"
        "<confidence>0.9</confidence>"
        "<confidence_explanation>why</confidence_explanation>"
    )
    # extract_output reads the <output> body directly; the extra tags
    # live outside it and simply shouldn't survive the reasoning strip
    # if someone concatenates without wrapping.
    assert extract_output(text) == "the answer"

    # Mixed bag — no <output>, just tags at the end. _strip_reasoning
    # should drop confidence entirely.
    tailing = "plain answer<confidence>0.7</confidence>"
    assert extract_output(tailing) == "plain answer"


def test_instructions_mention_confidence_tags():
    """The prompt must teach <confidence> so the model actually emits
    the score — the whole extension is dead weight without this."""
    assert "<confidence>" in OUTPUT_FORMAT_INSTRUCTIONS
    assert "<confidence_explanation>" in OUTPUT_FORMAT_INSTRUCTIONS
