"""LangGraph tool adapters for Quinn QA agent.

Wraps existing nanobot Tool classes as LangChain @tool functions.
All business logic stays in the original classes — these are thin adapters.
"""

from langchain_core.tools import tool

from tools.board_monitor import BoardMonitorTool
from tools.browser import BrowserTool
from tools.discord_feed import DiscordFeedTool
from tools.github_issues import GitHubIssuesTool
from tools.pr_inspector import PrInspectorTool
from tools.release_notes import ReleaseNotesTool


# Instantiate underlying tool classes (stateless singletons)
_board_monitor = BoardMonitorTool()
_browser = BrowserTool()
_discord_feed = DiscordFeedTool()
_github_issues = GitHubIssuesTool()
_pr_inspector = PrInspectorTool()
_release_notes = ReleaseNotesTool()


# ---------------------------------------------------------------------------
# Board Monitor
# ---------------------------------------------------------------------------

@tool
async def board_monitor(
    action: str,
    app_name: str = "",
) -> str:
    """Monitor protoLabs Studio boards across multiple applications.

    Actions:
    - sitrep: Full situation report (board state, agents, queue, recent activity)
    - board_summary: Feature counts grouped by status
    - blocked_features: List features with status=blocked and their reasons
    - review_queue: Features in review status (PRs awaiting merge)
    - health_check: Server health and connectivity check

    Supports multiple apps via app_name parameter.
    """
    return await _board_monitor.execute(action=action, app_name=app_name)


# ---------------------------------------------------------------------------
# Browser
# ---------------------------------------------------------------------------

@tool
async def browser(
    action: str,
    url: str = "",
    selector: str = "",
    text: str = "",
    query: str = "",
) -> str:
    """Automate a web browser. Actions: open, snapshot, screenshot, click, fill, find, type, wait.

    Returns accessibility tree snapshots by default (token-efficient).
    Use 'open' first, then 'snapshot' to read page content.
    """
    return await _browser.execute(
        action=action, url=url, selector=selector, text=text, query=query,
    )


# ---------------------------------------------------------------------------
# Discord Feed
# ---------------------------------------------------------------------------

@tool
async def discord_feed(
    action: str,
    channel_id: str = "",
    guild_id: str = "",
    limit: int = 50,
    after: str = "",
    content: str = "",
    title: str = "",
) -> str:
    """Read Discord channels and publish digests.

    READING (requires channel_id):
    - scan: Read recent messages and extract classified URLs
    - history: Get raw message history
    - channels: List channels in a server (guild_id required)
    - digest: Scan a channel and produce a structured link digest

    PUBLISHING (NO channel_id needed -- uses pre-configured webhook):
    - publish: Post content to Discord via webhook.
      Just provide 'content' and optionally 'title'. The webhook is auto-configured.
    """
    return await _discord_feed.execute(
        action=action, channel_id=channel_id, guild_id=guild_id,
        limit=limit, after=after, content=content, title=title,
    )


# ---------------------------------------------------------------------------
# GitHub Issues
# ---------------------------------------------------------------------------

@tool
async def github_issues(
    action: str,
    number: int = 0,
    reason: str = "",
    comment: str = "",
    label: str = "",
    repo: str = "",
) -> str:
    """Triage GitHub issues for the protoLabs repository.

    Actions:
    - list_open: List open issues with title, labels, and body preview
    - close: Close an issue with a reason comment (requires number, reason)
    - comment: Add a comment to an issue (requires number, comment)
    - label: Add a label to an issue (requires number, label)
    """
    kwargs: dict = {"action": action, "repo": repo}
    if number:
        kwargs["number"] = number
    if reason:
        kwargs["reason"] = reason
    if comment:
        kwargs["comment"] = comment
    if label:
        kwargs["label"] = label
    return await _github_issues.execute(**kwargs)


# ---------------------------------------------------------------------------
# PR Inspector
# ---------------------------------------------------------------------------

@tool
async def pr_inspector(
    action: str,
    pr_number: int = 0,
    repo: str = "",
) -> str:
    """Inspect GitHub pull requests for CI status, code review threads, and diffs.

    Actions:
    - list_open: List open PRs with title, branch, and CI status
    - check_ci: Show CI check results for a specific PR (requires pr_number)
    - coderabbit_threads: Show unresolved review threads on a PR (requires pr_number)
    - diff_summary: Show first 200 lines of PR diff (requires pr_number)
    """
    kwargs: dict = {"action": action, "repo": repo}
    if pr_number:
        kwargs["pr_number"] = pr_number
    return await _pr_inspector.execute(**kwargs)


# ---------------------------------------------------------------------------
# Release Notes
# ---------------------------------------------------------------------------

@tool
async def release_notes(
    action: str,
    from_tag: str = "",
    to_tag: str = "",
    since_date: str = "",
    content: str = "",
    version: str = "",
    repo: str = "",
) -> str:
    """Generate changelogs and draft release notes.

    Actions:
    - changelog: Build a categorized changelog between two releases (from_tag, to_tag)
    - draft_release: Compose full release notes from commits, merged PRs, and board data
    - post_to_discord: Send formatted release notes to Discord via webhook (requires content)
    """
    return await _release_notes.execute(
        action=action, from_tag=from_tag, to_tag=to_tag,
        since_date=since_date, content=content, version=version, repo=repo,
    )


# ---------------------------------------------------------------------------
# QA Memory (factory — requires store injection)
# ---------------------------------------------------------------------------

def create_qa_memory_tool(store=None):
    """Factory: creates qa_memory tool with injected QAKnowledgeStore."""
    from tools.qa_memory import QAKnowledgeStore, QAMemoryTool
    _tool = QAMemoryTool(store or QAKnowledgeStore())

    @tool
    async def qa_memory(
        action: str,
        entry_type: str = "",
        title: str = "",
        summary: str = "",
        description: str = "",
        content: str = "",
        version: str = "",
        app_name: str = "",
        severity: str = "info",
        category: str = "",
        resolution: str = "",
        steps: str = "",
        expected_result: str = "",
        related_bug: str = "",
        related_features: str = "",
        findings: str = "",
        commits_included: str = "",
        prs_included: str = "",
        query: str = "",
        limit: int = 10,
    ) -> str:
        """Persistent QA knowledge store with semantic search.

        - store: Save a QA report, bug pattern, release note, or regression test
        - search: Semantic search across all stored QA data
        - recent: Get most recent N entries by type
        - patterns: Find recurring bug patterns similar to a description
        - stats: Show knowledge base statistics

        Types: qa_report, bug_pattern, release_note, regression_test
        """
        return await _tool.execute(
            action=action, entry_type=entry_type, title=title, summary=summary,
            description=description, content=content, version=version,
            app_name=app_name, severity=severity, category=category,
            resolution=resolution, steps=steps, expected_result=expected_result,
            related_bug=related_bug, related_features=related_features,
            findings=findings, commits_included=commits_included,
            prs_included=prs_included, query=query, limit=limit,
        )

    return qa_memory


# ---------------------------------------------------------------------------
# Tool Registry
# ---------------------------------------------------------------------------

def get_all_tools(qa_store=None):
    """Get all QA tools as LangChain tool objects."""
    return [
        board_monitor,
        browser,
        discord_feed,
        github_issues,
        pr_inspector,
        release_notes,
        create_qa_memory_tool(qa_store),
    ]
