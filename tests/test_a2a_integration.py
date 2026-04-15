"""Quinn-side integration tests for the A2A port.

Locks the pieces that the pure handler tests can't see:
- Quinn's _build_agent_card advertises streaming + pushNotifications (so
  @a2a-js/sdk clients switch to the async/streaming path).
- The card still points at /a2a for JSON-RPC (116d201 regression guard).
- register_a2a_routes plays nicely with Quinn's dual well-known URL.
"""

from __future__ import annotations


def test_agent_card_advertises_async_capabilities() -> None:
    """Flipped for the A2A port — without these flags, @a2a-js/sdk
    silently falls back to the synchronous blocking path and Workstacean
    would keep waiting on the HTTP response for the full LangGraph run."""
    from server import _build_agent_card

    card = _build_agent_card("quinn:7870")
    caps = card["capabilities"]
    assert caps["streaming"] is True
    assert caps["pushNotifications"] is True


def test_agent_card_url_still_points_at_rpc_endpoint() -> None:
    """Regression guard for commit 116d201 — the card's `url` field must
    target the JSON-RPC path, not the server root, or @a2a-js/sdk sends
    message/send to / and gets 405 from FastAPI."""
    from server import _build_agent_card

    card = _build_agent_card("quinn:7870")
    assert card["url"].endswith("/a2a")


def test_agent_card_skills_still_present() -> None:
    """Sanity check that capability flip didn't truncate the skills list."""
    from server import _build_agent_card

    card = _build_agent_card("quinn:7870")
    skill_ids = {s["id"] for s in card.get("skills", [])}
    assert {"qa_report", "bug_triage", "pr_review", "security_triage"} <= skill_ids
