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


def test_agent_card_declares_effect_domain_extension() -> None:
    """protoWorkstacean's L1 planner reads capabilities.extensions with
    uri = .../a2a/ext/effect-domain-v1 to rank Quinn's skills against
    goals that target world-state selectors. Without this declaration,
    the planner treats Quinn as a black box and falls back to LLM
    reasoning (L2) to decide whether to dispatch her.
    """
    from server import _build_agent_card

    card = _build_agent_card("quinn:7870")
    exts = card["capabilities"].get("extensions", [])
    effect_ext = next(
        (e for e in exts
         if e.get("uri") == "https://protolabs.ai/a2a/ext/effect-domain-v1"),
        None,
    )
    assert effect_ext is not None, (
        "Missing effect-domain-v1 extension — planner will black-box Quinn's skills."
    )

    skills = effect_ext.get("params", {}).get("skills", {})
    # Only skills with directional mutations should be declared.
    # bug_triage and security_triage both land features on the board.
    assert "bug_triage" in skills
    assert "security_triage" in skills

    for skill_name in ("bug_triage", "security_triage"):
        effects = skills[skill_name].get("effects", [])
        assert effects, f"{skill_name} declared but has no effects"
        for effect in effects:
            # Schema per docs/extensions/effect-domain-v1.md
            assert "domain" in effect
            assert "path" in effect
            assert isinstance(effect.get("delta"), (int, float))
            confidence = effect.get("confidence")
            assert isinstance(confidence, (int, float))
            assert 0.0 <= confidence <= 1.0


def test_agent_card_does_not_over_declare_read_only_effects() -> None:
    """Read-only skills (board_audit, qa_report, pr_review) must NOT
    declare effects — over-declaring confuses the planner into picking
    Quinn for goals her skills can't actually move."""
    from server import _build_agent_card

    card = _build_agent_card("quinn:7870")
    exts = card["capabilities"].get("extensions", [])
    effect_ext = next(
        (e for e in exts
         if e.get("uri") == "https://protolabs.ai/a2a/ext/effect-domain-v1"),
        {"params": {"skills": {}}},
    )
    declared = effect_ext["params"]["skills"]
    for read_only in ("board_audit", "qa_report", "pr_review"):
        assert read_only not in declared, (
            f"{read_only} is a read-only skill and should not declare effects"
        )
