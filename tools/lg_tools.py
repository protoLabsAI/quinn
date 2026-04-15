"""LangGraph tool adapters for Quinn QA agent.

Wraps existing Tool classes as LangChain @tool functions.
All business logic stays in the original classes — these are thin adapters.
"""

from langchain_core.tools import tool

from tools.board_monitor import BoardMonitorTool
from tools.browser import BrowserTool
from tools.discord_admin import DiscordAdminTool
from tools.discord_feed import DiscordFeedTool
from tools.file_bug import FileBugTool
from tools.github_actions import GitHubActionsTool
from tools.github_issues import GitHubIssuesTool
from tools.pr_inspector import PrInspectorTool
from tools.release_notes import ReleaseNotesTool


# Instantiate underlying tool classes (stateless singletons)
_board_monitor = BoardMonitorTool()
_browser = BrowserTool()
_discord_admin = DiscordAdminTool()
_discord_feed = DiscordFeedTool()
_file_bug = FileBugTool()
_github_actions = GitHubActionsTool()
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
# Discord Admin (full server management)
# ---------------------------------------------------------------------------

@tool
async def discord_admin(
    action: str,
    channel_id: str = "",
    name: str = "",
    content: str = "",
    message_id: str = "",
    emoji: str = "",
    category_id: str = "",
    webhook_url: str = "",
    topic: str = "",
    guild_id: str = "",
    limit: int = 20,
) -> str:
    """Manage the Discord server. Quinn has admin permissions.

    Actions:
    - server_info: Full server overview with channels, categories, members
    - list_channels: List all channels with IDs
    - create_channel: Create a text channel (name required, category_id optional)
    - edit_channel: Edit channel name or topic (channel_id required)
    - set_channel_topic: Set a channel's topic (channel_id, topic required)
    - delete_channel: Delete a channel (channel_id required)
    - create_category: Create a channel category (name required)
    - edit_category: Rename a category (channel_id, name required)
    - delete_category: Delete a category (channel_id required)
    - send_message: Send a message to a channel (channel_id, content required)
    - read_messages: Read recent messages (channel_id required, limit optional)
    - delete_message: Delete a message (channel_id, message_id required)
    - add_reaction: React to a message (channel_id, message_id, emoji required)
    - remove_reaction: Remove a reaction (channel_id, message_id, emoji required)
    - list_webhooks: List webhooks (channel_id optional, shows all if omitted)
    - create_webhook: Create a webhook (channel_id required, name optional)
    - send_webhook: Send via webhook (webhook_url, content required)
    - delete_webhook: Delete a webhook (webhook_url required)
    - list_forums: List forum channels
    - create_forum_post: Create a forum post (channel_id, name, content required)
    - reply_to_forum: Reply to a forum thread (channel_id=thread_id, content required)
    """
    return await _discord_admin.execute(
        action=action, channel_id=channel_id, name=name, content=content,
        message_id=message_id, emoji=emoji, category_id=category_id,
        webhook_url=webhook_url, topic=topic, guild_id=guild_id, limit=limit,
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
# GitHub Actions
# ---------------------------------------------------------------------------

@tool
async def github_actions(
    action: str,
    workflow: str = "",
    run_id: int = 0,
    ref: str = "",
    repo: str = "",
) -> str:
    """Manage GitHub Actions CI workflows.

    Actions:
    - list_workflows: List available workflows with name and state
    - trigger_workflow: Trigger a workflow on a branch (requires workflow)
    - list_runs: List recent runs for a workflow (requires workflow)
    - view_run: View status, conclusion, and jobs for a run (requires run_id)
    - rerun_failed: Rerun only the failed jobs of a run (requires run_id)
    - run_logs: Get failure logs from a run, truncated to 3000 chars (requires run_id)
    - run_tests: Trigger checks.yml, poll until complete, return pass/fail summary
    """
    kwargs: dict = {"action": action, "repo": repo}
    if workflow:
        kwargs["workflow"] = workflow
    if run_id:
        kwargs["run_id"] = run_id
    if ref:
        kwargs["ref"] = ref
    return await _github_actions.execute(**kwargs)


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
    body: str = "",
    repo: str = "",
) -> str:
    """Inspect and review GitHub pull requests.

    Actions:
    - list_open: List open PRs with title, branch, and CI status
    - check_ci: Show CI check results for a specific PR (requires pr_number)
    - coderabbit_threads: Show unresolved review threads on a PR (requires pr_number)
    - diff_summary: Show first 200 lines of PR diff (requires pr_number)
    - review_comment: Leave a review comment on a PR (requires pr_number, body)
    - review_approve: Approve a PR with an optional comment (requires pr_number)
    - review_request_changes: Request changes on a PR (requires pr_number, body)
    """
    kwargs: dict = {"action": action, "repo": repo}
    if pr_number:
        kwargs["pr_number"] = pr_number
    if body:
        kwargs["body"] = body
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
    tag: str = "",
    title: str = "",
    notes: str = "",
    repo: str = "",
) -> str:
    """Generate changelogs, draft release notes, and manage GitHub releases.

    Actions:
    - changelog: Build a categorized changelog between two releases (from_tag, to_tag)
    - draft_release: Compose full release notes from commits, merged PRs, and board data
    - post_to_discord: Send formatted release notes to Discord via webhook (requires content)
    - create_release: Create a GitHub release (requires tag, notes; title optional)
    - edit_release: Edit the notes of an existing release (requires tag, notes)
    """
    return await _release_notes.execute(
        action=action, from_tag=from_tag, to_tag=to_tag,
        since_date=since_date, content=content, version=version,
        tag=tag, title=title, notes=notes, repo=repo,
    )


# ---------------------------------------------------------------------------
# QA Memory (factory — requires store injection)
# ---------------------------------------------------------------------------

def create_qa_memory_tool(store=None):
    """Factory: creates qa_memory tool backed by the canonical KnowledgeStore.

    If ``store`` is provided (e.g. from middleware), the tool shares that
    store; otherwise it instantiates its own. Both code paths now hit the
    same schema — the divergent QAKnowledgeStore has been removed (see #7).
    """
    from knowledge.store import KnowledgeStore
    from tools.qa_memory import QAMemoryTool
    _tool = QAMemoryTool(store or KnowledgeStore())

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
        pattern: str = "",
        resolution: str = "",
        steps: str = "",
        expected_result: str = "",
        related_bug: str = "",
        related_features: str = "",
        findings: str = "",
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
            pattern=pattern, resolution=resolution, steps=steps,
            expected_result=expected_result, related_bug=related_bug,
            related_features=related_features, findings=findings,
            query=query, limit=limit,
        )

    return qa_memory


# ---------------------------------------------------------------------------
# Tool Registry
# ---------------------------------------------------------------------------

@tool
async def file_bug(
    title: str,
    description: str,
    severity: str = "medium",
    source: str = "",
) -> str:
    """File a bug report on the protoLabs Studio board.

    Use after triaging a Discord or GitHub bug report. Creates a backlog
    feature with category=bug and returns the feature ID to link back to
    the reporter.

    severity: critical | high | medium | low
    source: where it came from, e.g. 'Discord #bug-reports by @user' or 'GitHub issue #42'
    """
    return await _file_bug.execute(
        title=title, description=description, severity=severity, source=source,
    )


def get_all_tools(qa_store=None):
    """Get all QA tools as LangChain tool objects."""
    return [
        board_monitor,
        browser,
        discord_admin,
        discord_feed,
        file_bug,
        github_actions,
        github_issues,
        pr_inspector,
        release_notes,
        create_qa_memory_tool(qa_store),
    ]
