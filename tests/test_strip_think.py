"""Tests for server._strip_think — reasoning-tag stripping.

Reasoning models emit their chain-of-thought wrapped in ``<think>...</think>``
tags. Different providers ship it differently:

- Claude + DeepSeek + Qwen3: balanced pairs (normal case)
- MiniMax M2.x: streams ``<think>`` and the closing tag can land in a later
  frame or get dropped entirely, so orphaned openings are common

If any ``<think>`` content leaks to the A2A artifact, downstream consumers
(Workstacean's report renderer, Discord summaries) render the model's
internal reasoning as if it were user-facing text. Lock the contract.
"""

from __future__ import annotations


def test_strips_balanced_pair():
    from server import _strip_think
    assert _strip_think("hello <think>reasoning</think> world") == "hello  world"


def test_strips_orphan_opening_tag():
    """MiniMax M2.x: reasoning streams as <think>... with no closing tag.
    Everything from the orphan to EOT is reasoning — strip it all."""
    from server import _strip_think
    assert _strip_think("<think>\nreasoning\n<think>\nmore") == ""
    assert _strip_think(
        "<think>r1</think>\nanswer\n<think>unclosed reasoning"
    ) == "answer"


def test_strips_orphan_closing_tag():
    """If the opening tag was in an earlier chunk, we still want to
    strip the closing marker when it shows up alone."""
    from server import _strip_think
    assert _strip_think("answer </think> tail") == "answer tail"


def test_passthrough_clean_text():
    from server import _strip_think
    assert _strip_think("clean output") == "clean output"


def test_strips_whitespace_only():
    from server import _strip_think
    assert _strip_think("   \n\t  ") == ""


def test_multiple_balanced_pairs():
    from server import _strip_think
    assert _strip_think(
        "a <think>r1</think> b <think>r2</think> c"
    ) == "a  b  c"
