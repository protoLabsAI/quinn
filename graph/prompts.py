"""System prompt composer for Quinn LangGraph agent.

Composes the system prompt from:
1. SOUL.md content (identity, personality, values)
2. QA skills (from skills/qa/SKILL.md)
3. Subagent instructions (available types + delegation rules)
4. Dynamic QA context (from KnowledgeMiddleware)
"""

from pathlib import Path

from graph.subagents.config import SUBAGENT_REGISTRY


def _read_file(path: str | Path) -> str:
    """Read a file if it exists, return empty string otherwise."""
    p = Path(path)
    if p.exists():
        return p.read_text(encoding="utf-8").strip()
    return ""


def build_system_prompt(
    workspace: str = "/sandbox",
    include_subagents: bool = True,
    research_context: str = "",
) -> str:
    """Build the complete system prompt for the lead agent."""
    parts = []

    # 1. Identity from SOUL.md
    soul = _read_file(f"{workspace}/SOUL.md")
    if soul:
        parts.append(soul)
    else:
        parts.append(
            "# Quinn\n\n"
            "You are Quinn, the QA Engineer and Release Manager for protoLabs.\n"
            "You verify shipped code works, triage bugs, generate release notes, "
            "and keep the community informed."
        )

    # 2. QA skills
    skill = _read_file(f"{workspace}/skills/qa/SKILL.md")
    if skill:
        parts.append(f"\n# QA Methodology\n\n{skill}")

    # 3. Subagent instructions
    if include_subagents:
        parts.append(_build_subagent_section())

    # 4. Dynamic QA context (injected by KnowledgeMiddleware)
    if research_context:
        parts.append(f"\n# QA Context\n\n{research_context}")

    # 5. Guidelines
    parts.append("""
# Guidelines

- Verify, don't trust. Hit the endpoint, read the response, check the wiring.
- For full audits, delegate to subagents: Auditor scans, Verifier tests, Reporter publishes.
- Rate every finding by severity: CRITICAL / HIGH / MEDIUM / LOW
- Always end QA sessions with a structured verdict: PASS / WARN / FAIL
- Store important findings in qa_memory for regression tracking.
- When publishing to Discord, use discord_feed action=publish with content and title.
- Reply directly with text for conversations. Use the task tool to delegate parallel work.
""")

    return "\n\n".join(parts)


def _build_subagent_section() -> str:
    """Build the subagent delegation instructions."""
    lines = [
        "# Subagent Delegation",
        "",
        "You can delegate tasks to specialized subagents using the `task` tool.",
        "Each subagent has focused tools and expertise:",
        "",
    ]

    for name, config in SUBAGENT_REGISTRY.items():
        lines.append(f"- **{name}**: {config.description}")
        lines.append(f"  Tools: {', '.join(config.tools)}")
        lines.append("")

    lines.extend([
        "**Rules:**",
        "- Delegate board/PR scanning to Auditor",
        "- Delegate verification/testing to Verifier",
        "- Delegate release notes/community updates to Reporter",
        "- For simple checks, answer directly without delegation",
        "- Max 3 concurrent subagent tasks",
        "- Subagents cannot spawn further subagents",
    ])

    return "\n".join(lines)


def build_subagent_prompt(agent_name: str, workspace: str = "/sandbox") -> str:
    """Build system prompt for a specific subagent."""
    config = SUBAGENT_REGISTRY.get(agent_name)
    if not config:
        return "You are a QA subagent. Complete the delegated task efficiently."
    return config.system_prompt
