"""GitHub issue triage tool for Quinn QA agent.

Manages GitHub issues — listing, labeling, commenting, and closing —
using the gh CLI for authentication and API access.

Requires: gh CLI authenticated and GITHUB_REPO env var (default: protoLabsAI/protoMaker).
"""

import asyncio
import json
import os
from typing import Any

from nanobot.agent.tools.base import Tool

_DEFAULT_REPO = "protoLabsAI/protoMaker"
_COMMAND_TIMEOUT = 30


def _repo() -> str:
    return os.environ.get("GITHUB_REPO", _DEFAULT_REPO)


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


def _format_issue(issue: dict[str, Any]) -> str:
    """Format a single issue as a readable block."""
    number = issue.get("number", "?")
    title = issue.get("title", "Untitled")
    labels = [lbl.get("name", "?") for lbl in (issue.get("labels") or [])]
    created = issue.get("createdAt", "")[:10]
    updated = issue.get("updatedAt", "")[:10]

    line = f"- **#{number}** {title}"
    if labels:
        line += f" [{', '.join(labels)}]"
    line += f"\n  Created: {created} | Updated: {updated}"

    body = issue.get("body", "")
    if body:
        preview = body[:200].replace("\n", " ").strip()
        line += f"\n  {preview}"
        if len(body) > 200:
            line += "..."
    return line


class GitHubIssuesTool(Tool):
    """Triage GitHub issues — list, label, comment, and close."""

    @property
    def name(self) -> str:
        return "github_issues"

    @property
    def description(self) -> str:
        return (
            "Triage GitHub issues for the protoLabs repository.\n\n"
            "Actions:\n"
            "- list_open: List open issues with title, labels, and body preview\n"
            "- close: Close an issue with a reason comment\n"
            "- comment: Add a comment to an issue\n"
            "- label: Add a label to an issue\n\n"
            f"Default repo: {_DEFAULT_REPO} (override with GITHUB_REPO env var)."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list_open", "close", "comment", "label"],
                    "description": "Action to perform.",
                },
                "number": {
                    "type": "integer",
                    "description": "Issue number (required for close, comment, label).",
                },
                "reason": {
                    "type": "string",
                    "description": "Closing reason comment (for close action).",
                },
                "comment": {
                    "type": "string",
                    "description": "Comment text (for comment action).",
                },
                "label": {
                    "type": "string",
                    "description": "Label to add (for label action).",
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
        number = kwargs.get("number")

        if action == "list_open":
            return await self._list_open(repo)
        elif action == "close":
            if not number:
                return "Error: 'number' is required for close."
            reason = kwargs.get("reason", "Closed by Quinn QA agent.")
            return await self._close(repo, number, reason)
        elif action == "comment":
            if not number:
                return "Error: 'number' is required for comment."
            comment_text = kwargs.get("comment", "")
            if not comment_text:
                return "Error: 'comment' text is required."
            return await self._comment(repo, number, comment_text)
        elif action == "label":
            if not number:
                return "Error: 'number' is required for label."
            label_name = kwargs.get("label", "")
            if not label_name:
                return "Error: 'label' name is required."
            return await self._label(repo, number, label_name)
        else:
            return f"Error: Unknown action '{action}'."

    async def _list_open(self, repo: str) -> str:
        """List open issues."""
        rc, out, err = await _run_gh([
            "issue", "list",
            "--repo", repo,
            "--state", "open",
            "--json", "number,title,body,labels,createdAt,updatedAt",
            "--limit", "50",
        ])
        error = _check_gh_error(rc, err)
        if error:
            return error

        if not out:
            return "No open issues found."

        try:
            issues = json.loads(out)
        except json.JSONDecodeError:
            return f"Error parsing issue list: {out[:500]}"

        if not issues:
            return "No open issues found."

        lines = [f"**{len(issues)} Open Issue(s) in {repo}:**\n"]
        for issue in issues:
            lines.append(_format_issue(issue))
        return "\n".join(lines)

    async def _close(self, repo: str, number: int, reason: str) -> str:
        """Close an issue with a comment."""
        rc, out, err = await _run_gh([
            "issue", "close",
            str(number),
            "--repo", repo,
            "--comment", reason,
        ])
        error = _check_gh_error(rc, err)
        if error:
            return error
        return f"Closed issue #{number} in {repo}."

    async def _comment(self, repo: str, number: int, comment_text: str) -> str:
        """Add a comment to an issue."""
        rc, out, err = await _run_gh([
            "issue", "comment",
            str(number),
            "--repo", repo,
            "--body", comment_text,
        ])
        error = _check_gh_error(rc, err)
        if error:
            return error
        return f"Commented on issue #{number} in {repo}."

    async def _label(self, repo: str, number: int, label_name: str) -> str:
        """Add a label to an issue."""
        rc, out, err = await _run_gh([
            "issue", "edit",
            str(number),
            "--repo", repo,
            "--add-label", label_name,
        ])
        error = _check_gh_error(rc, err)
        if error:
            return error
        return f"Added label '{label_name}' to issue #{number} in {repo}."
