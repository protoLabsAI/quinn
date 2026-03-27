"""Release notes and changelog generation for Quinn QA agent.

Builds changelogs from git tags, commit logs, and merged PRs. Optionally
posts formatted release notes to Discord via webhook.

Requires:
- gh CLI authenticated
- GITHUB_REPO env var (default: protoLabsAI/protoMaker)
- DISCORD_WEBHOOK_URL env var (for post_to_discord action)
"""

import asyncio
import json
import os
from typing import Any

import httpx

from nanobot.agent.tools.base import Tool

_DEFAULT_REPO = "protoLabsAI/protoMaker"
_COMMAND_TIMEOUT = 30
_DISCORD_EMBED_COLOR = 0x14B8A6


def _repo() -> str:
    return os.environ.get("GITHUB_REPO", _DEFAULT_REPO)


def _webhook_url() -> str:
    return os.environ.get("DISCORD_WEBHOOK_URL", "")


async def _run_gh(args: list[str], timeout: int = _COMMAND_TIMEOUT) -> tuple[int, str, str]:
    """Execute a gh CLI command and return (returncode, stdout, stderr)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "gh", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return (
            proc.returncode or 0,
            stdout.decode(errors="replace").strip(),
            stderr.decode(errors="replace").strip(),
        )
    except asyncio.TimeoutError:
        proc.kill()  # type: ignore[union-attr]
        return 1, "", f"Command timed out after {timeout}s"
    except FileNotFoundError:
        return 1, "", "gh CLI is not installed or not in PATH."


def _check_gh_error(returncode: int, stderr: str) -> str | None:
    """Return a formatted error string if the command failed, else None."""
    if returncode != 0:
        return f"Error (exit {returncode}): {stderr[:500]}"
    return None


async def _get_recent_tags(repo: str, limit: int = 5) -> list[dict[str, str]] | str:
    """Fetch recent releases/tags from GitHub."""
    rc, out, err = await _run_gh([
        "release", "list",
        "--repo", repo,
        "--limit", str(limit),
        "--json", "tagName,name,publishedAt,isPrerelease",
    ])
    error = _check_gh_error(rc, err)
    if error:
        return error

    if not out:
        return "No releases found."

    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return f"Error parsing releases: {out[:500]}"


async def _get_commits_between(repo: str, from_tag: str, to_tag: str) -> str:
    """Get commit log between two tags using gh api."""
    rc, out, err = await _run_gh([
        "api",
        f"repos/{repo}/compare/{from_tag}...{to_tag}",
        "--jq", ".commits[] | .commit.message | split(\"\\n\") | .[0]",
    ])
    error = _check_gh_error(rc, err)
    if error:
        return error
    return out or "No commits found between these tags."


async def _get_merged_prs(repo: str, since_date: str) -> list[dict[str, Any]] | str:
    """Get PRs merged since a date."""
    rc, out, err = await _run_gh([
        "pr", "list",
        "--repo", repo,
        "--state", "merged",
        "--search", f"merged:>{since_date}",
        "--json", "number,title,mergedAt,headRefName,author",
        "--limit", "50",
    ])
    error = _check_gh_error(rc, err)
    if error:
        return error

    if not out:
        return []

    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return f"Error parsing PR list: {out[:500]}"


def _categorize_commit(message: str) -> str:
    """Categorize a commit message by conventional commit prefix."""
    lower = message.lower().strip()
    if lower.startswith("feat"):
        return "Features"
    elif lower.startswith("fix"):
        return "Bug Fixes"
    elif lower.startswith("refactor"):
        return "Refactoring"
    elif lower.startswith("docs"):
        return "Documentation"
    elif lower.startswith("test"):
        return "Tests"
    elif lower.startswith("chore"):
        return "Chores"
    elif lower.startswith("ci"):
        return "CI"
    return "Other"


def _build_changelog(commits_text: str, from_tag: str, to_tag: str) -> str:
    """Build a markdown changelog from commit messages."""
    if not commits_text or commits_text.startswith("Error"):
        return commits_text

    commits = [line.strip() for line in commits_text.split("\n") if line.strip()]

    by_category: dict[str, list[str]] = {}
    for commit in commits:
        category = _categorize_commit(commit)
        by_category.setdefault(category, []).append(commit)

    lines = [f"## Changelog: {from_tag} -> {to_tag}\n"]

    # Ordered categories — features first, chores last
    priority = ["Features", "Bug Fixes", "Refactoring", "Documentation", "Tests", "CI", "Chores", "Other"]
    for category in priority:
        group = by_category.pop(category, [])
        if group:
            lines.append(f"### {category}")
            for commit in group:
                lines.append(f"- {commit}")
            lines.append("")

    # Any remaining categories
    for category, group in sorted(by_category.items()):
        if group:
            lines.append(f"### {category}")
            for commit in group:
                lines.append(f"- {commit}")
            lines.append("")

    lines.append(f"_Total: {len(commits)} commit(s)_")
    return "\n".join(lines)


class ReleaseNotesTool(Tool):
    """Generate changelogs and release notes from git history and PRs."""

    @property
    def name(self) -> str:
        return "release_notes"

    @property
    def description(self) -> str:
        return (
            "Generate changelogs and draft release notes.\n\n"
            "Actions:\n"
            "- changelog: Build a categorized changelog between two releases\n"
            "- draft_release: Compose full release notes from commits, merged PRs, and board data\n"
            "- post_to_discord: Send formatted release notes to Discord via webhook\n\n"
            f"Default repo: {_DEFAULT_REPO} (override with GITHUB_REPO env var)."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["changelog", "draft_release", "post_to_discord"],
                    "description": "Action to perform.",
                },
                "from_tag": {
                    "type": "string",
                    "description": "Starting tag/release for changelog (default: second-most-recent tag).",
                },
                "to_tag": {
                    "type": "string",
                    "description": "Ending tag/release for changelog (default: most-recent tag).",
                },
                "since_date": {
                    "type": "string",
                    "description": "ISO date for draft_release (e.g. '2026-03-20'). Defaults to 7 days ago.",
                },
                "content": {
                    "type": "string",
                    "description": "Pre-composed release notes to post (for post_to_discord).",
                },
                "version": {
                    "type": "string",
                    "description": "Version string for release title (e.g. 'v0.89.3').",
                },
                "repo": {
                    "type": "string",
                    "description": f"Repository in owner/name format (default: {_DEFAULT_REPO}).",
                },
            },
            "required": ["action"],
        }

    async def execute(self, **kwargs: Any) -> str:
        action = kwargs["action"]
        repo = kwargs.get("repo") or _repo()

        try:
            if action == "changelog":
                return await self._changelog(repo, kwargs)
            elif action == "draft_release":
                return await self._draft_release(repo, kwargs)
            elif action == "post_to_discord":
                return await self._post_to_discord(kwargs)
            else:
                return f"Error: Unknown action '{action}'."
        except Exception as e:
            return f"Error: {type(e).__name__}: {e}"

    async def _changelog(self, repo: str, kwargs: dict[str, Any]) -> str:
        """Build changelog between two tags."""
        from_tag = kwargs.get("from_tag", "")
        to_tag = kwargs.get("to_tag", "")

        # Auto-detect tags if not provided
        if not from_tag or not to_tag:
            tags = await _get_recent_tags(repo, limit=5)
            if isinstance(tags, str):
                return tags
            if len(tags) < 2:
                return "Error: Need at least 2 releases to generate a changelog. Provide from_tag and to_tag."
            if not to_tag:
                to_tag = tags[0].get("tagName", "")
            if not from_tag:
                from_tag = tags[1].get("tagName", "")

        if not from_tag or not to_tag:
            return "Error: Could not determine tags. Provide from_tag and to_tag explicitly."

        commits_text = await _get_commits_between(repo, from_tag, to_tag)
        return _build_changelog(commits_text, from_tag, to_tag)

    async def _draft_release(self, repo: str, kwargs: dict[str, Any]) -> str:
        """Compose full release notes from multiple sources."""
        since_date = kwargs.get("since_date", "")
        version = kwargs.get("version", "")

        if not since_date:
            from datetime import datetime, timedelta, timezone
            week_ago = datetime.now(timezone.utc) - timedelta(days=7)
            since_date = week_ago.strftime("%Y-%m-%d")

        # Gather data in parallel
        tags_task = _get_recent_tags(repo, limit=3)
        prs_task = _get_merged_prs(repo, since_date)
        tags_result, prs_result = await asyncio.gather(tags_task, prs_task)

        lines: list[str] = []

        # Header
        title = f"Release {version}" if version else f"Release Notes (since {since_date})"
        lines.append(f"# {title}\n")

        # Latest tag info
        if isinstance(tags_result, list) and tags_result:
            latest = tags_result[0]
            tag_name = latest.get("tagName", "?")
            published = latest.get("publishedAt", "")[:10]
            lines.append(f"**Latest release:** {tag_name} ({published})\n")

            # Changelog between two most recent tags
            if len(tags_result) >= 2:
                from_tag = tags_result[1].get("tagName", "")
                to_tag = tag_name
                if from_tag and to_tag:
                    commits_text = await _get_commits_between(repo, from_tag, to_tag)
                    changelog = _build_changelog(commits_text, from_tag, to_tag)
                    lines.append(changelog)
                    lines.append("")

        # Merged PRs section
        if isinstance(prs_result, list) and prs_result:
            lines.append(f"## Merged Pull Requests ({len(prs_result)})\n")
            for pr in prs_result:
                number = pr.get("number", "?")
                pr_title = pr.get("title", "Untitled")
                author = pr.get("author", {}).get("login", "?")
                merged = pr.get("mergedAt", "")[:10]
                lines.append(f"- **#{number}** {pr_title} (by @{author}, {merged})")
            lines.append("")
        elif isinstance(prs_result, str):
            lines.append(f"_PR data: {prs_result}_\n")

        return "\n".join(lines)

    async def _post_to_discord(self, kwargs: dict[str, Any]) -> str:
        """Post release notes to Discord via webhook."""
        webhook = _webhook_url()
        if not webhook:
            return "Error: DISCORD_WEBHOOK_URL not set. Add it to your environment."

        content = kwargs.get("content", "")
        version = kwargs.get("version", "")

        if not content:
            return "Error: 'content' is required for post_to_discord."

        title = f"Release {version}" if version else "Release Notes"

        # Discord embed description limit is 4096 chars
        chunks: list[str] = []
        remaining = content
        while remaining:
            chunks.append(remaining[:4096])
            remaining = remaining[4096:]

        embeds: list[dict[str, Any]] = []
        for i, chunk in enumerate(chunks):
            embed: dict[str, Any] = {
                "description": chunk,
                "color": _DISCORD_EMBED_COLOR,
            }
            if i == 0:
                embed["title"] = title
            embeds.append(embed)

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                for batch_start in range(0, len(embeds), 10):
                    batch = embeds[batch_start:batch_start + 10]
                    payload = {
                        "username": "Quinn QA",
                        "embeds": batch,
                    }
                    resp = await client.post(webhook, json=payload)
                    if resp.status_code not in (200, 204):
                        return f"Error: Discord returned {resp.status_code}"
        except Exception as e:
            return f"Error posting to Discord: {e}"

        return f"Posted release notes to Discord ({len(embeds)} embed(s))."
