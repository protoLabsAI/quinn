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
    assert {"qa_report", "bug_triage", "pr_review", "board_audit"} <= skill_ids
    assert "security_triage" not in skill_ids, "security_triage removed — no tool backs it"


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
         if e.get("uri") == "https://proto-labs.ai/a2a/ext/effect-domain-v1"),
        None,
    )
    assert effect_ext is not None, (
        "Missing effect-domain-v1 extension — planner will black-box Quinn's skills."
    )

    skills = effect_ext.get("params", {}).get("skills", {})
    # Only skills with directional mutations should be declared.
    assert "bug_triage" in skills
    assert "security_triage" not in skills, "security_triage removed — no tool backs it"

    effects = skills["bug_triage"].get("effects", [])
    assert effects, "bug_triage declared but has no effects"
    for effect in effects:
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
         if e.get("uri") == "https://proto-labs.ai/a2a/ext/effect-domain-v1"),
        {"params": {"skills": {}}},
    )
    declared = effect_ext["params"]["skills"]
    for read_only in ("board_audit", "qa_report", "pr_review"):
        assert read_only not in declared, (
            f"{read_only} is a read-only skill and should not declare effects"
        )


def test_agent_card_declares_cost_v1_extension() -> None:
    """Quinn emits a cost-v1 DataPart on every terminal task that invoked
    an LLM. The extension must be declared on the card so Workstacean's
    A2AExecutor (protoWorkstacean#372) knows to extract it onto
    result.data, where the cost interceptor records per-skill samples."""
    from server import _build_agent_card

    card = _build_agent_card("quinn:7870")
    exts = card["capabilities"].get("extensions", [])
    cost_ext = next(
        (e for e in exts
         if e.get("uri") == "https://proto-labs.ai/a2a/ext/cost-v1"),
        None,
    )
    assert cost_ext is not None, (
        "Missing cost-v1 extension declaration — Workstacean's executor "
        "won't know to extract the cost DataPart."
    )


# ── Worldstate-delta-v1 runtime emission ─────────────────────────────────────


def test_file_bug_success_yields_board_backlog_delta() -> None:
    """When file_bug completes with the success marker in its output, the
    stream must emit a worldstate-delta matching the effect-domain-v1 entry
    declared on the card. If these two diverge, Workstacean sees declared
    priors that never get reconciled against observed mutations."""
    from server import _worldstate_delta_for_tool

    output = (
        "Bug filed: **Button crash in Safari** → `feature-abc` "
        "(severity=medium, status=backlog)"
    )
    delta = _worldstate_delta_for_tool("file_bug", output)
    assert delta == {
        "domain": "protomaker_board",
        "path": "data.backlog_count",
        "op": "inc",
        "value": 1,
    }


def test_file_bug_error_yields_no_delta() -> None:
    """An error response from file_bug must not emit a delta — the state
    mutation didn't happen, we shouldn't claim otherwise."""
    from server import _worldstate_delta_for_tool

    assert _worldstate_delta_for_tool(
        "file_bug",
        "Error filing bug: API returned 503 — upstream unavailable",
    ) is None


def test_other_tools_yield_no_delta() -> None:
    """Tools without declared effects must return None — we do not emit
    speculative deltas for read-only tools (board_monitor, pr_inspector,
    github_issues, etc.)."""
    from server import _worldstate_delta_for_tool

    for tool_name in ("board_monitor", "pr_inspector", "github_issues",
                      "qa_memory", "discord_feed"):
        assert _worldstate_delta_for_tool(tool_name, "some output") is None


def test_delta_matches_effect_domain_declaration() -> None:
    """The runtime-observed delta for file_bug must agree with the
    effect-domain-v1 declaration for bug_triage on the card. Drift
    between declared priors and observed mutations defeats the planner's
    scoring model."""
    from server import _build_agent_card, _worldstate_delta_for_tool

    card = _build_agent_card("quinn:7870")
    exts = card["capabilities"]["extensions"]
    effect_ext = next(e for e in exts
                      if e["uri"].endswith("/effect-domain-v1"))
    declared = effect_ext["params"]["skills"]["bug_triage"]["effects"][0]

    observed = _worldstate_delta_for_tool(
        "file_bug",
        "Bug filed: **x** → `feature-y` (severity=low, status=backlog)",
    )
    assert observed is not None
    assert observed["domain"] == declared["domain"]
    assert observed["path"] == declared["path"]
    # Declared delta is a signed number; observed uses op/value form.
    # +1 delta ⇔ inc by 1, so both representations agree.
    assert observed["op"] == "inc"
    assert observed["value"] == declared["delta"]
