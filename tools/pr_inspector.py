"""GitHub PR inspection tool for Quinn QA agent.

Analyzes pull requests using the gh CLI -- CI status, unresolved review threads,
diff summaries, and open PR listings.

Requires: gh CLI authenticated and GITHUB_REPO env var (default: protoLabsAI/protoMaker).
"""

import json
from typing import Any

from nanobot.agent.tools.base import Tool

from tools.gh_cli import check_gh_error, get_repo, run_gh


class PrInspectorTool(Tool):
    """Inspect GitHub pull requests -- CI, reviews, diffs."""

    @property
    def name(self) -> str:
        return "pr_inspector"

    @property
    def description(self) -> str:
        return (
            "Inspect GitHub pull requests for CI status, code review threads, and diffs.\n\n"
            "Actions:\n"
            "- list_open: List open PRs with title, branch, and CI status\n"
            "- check_ci: Show CI check results for a specific PR\n"
            "- coderabbit_threads: Show unresolved review threads on a PR\n"
            "- diff_summary: Show first 200 lines of PR diff\n\n"
            "Default repo from GITHUB_REPO env var (override with repo parameter)."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list_open", "check_ci", "coderabbit_threads", "diff_summary"],
                    "description": "Action to perform.",
                },
                "pr_number": {
                    "type": "integer",
                    "description": "PR number (required for check_ci, coderabbit_threads, diff_summary).",
                },
                "repo": {
                    "type": "string",
                    "description": "Repository in owner/name format.",
                },
            },
            "required": ["action"],
        }

    async def execute(self, **kwargs: Any) -> str:
        action = kwargs["action"]
        repo = kwargs.get("repo") or get_repo()
        pr_number = kwargs.get("pr_number")

        if action == "list_open":
            return await self._list_open(repo)
        elif action == "check_ci":
            if not pr_number:
                return "Error: 'pr_number' is required for check_ci."
            return await self._check_ci(repo, pr_number)
        elif action == "coderabbit_threads":
            if not pr_number:
                return "Error: 'pr_number' is required for coderabbit_threads."
            return await self._coderabbit_threads(repo, pr_number)
        elif action == "diff_summary":
            if not pr_number:
                return "Error: 'pr_number' is required for diff_summary."
            return await self._diff_summary(repo, pr_number)
        else:
            return f"Error: Unknown action '{action}'."

    async def _list_open(self, repo: str) -> str:
        """List open PRs with metadata."""
        rc, out, err = await run_gh([
            "pr", "list",
            "--repo", repo,
            "--state", "open",
            "--json", "number,title,headRefName,statusCheckRollup,updatedAt",
            "--limit", "30",
        ])
        error = check_gh_error(rc, err)
        if error:
            return error

        if not out:
            return "No open PRs found."

        try:
            prs = json.loads(out)
        except json.JSONDecodeError:
            return f"Error parsing PR list: {out[:500]}"

        if not prs:
            return "No open PRs found."

        lines = [f"**{len(prs)} Open PR(s) in {repo}:**\n"]
        for pr in prs:
            number = pr.get("number", "?")
            title = pr.get("title", "Untitled")
            branch = pr.get("headRefName", "?")
            updated = pr.get("updatedAt", "")[:10]

            checks = pr.get("statusCheckRollup", []) or []
            ci_summary = _summarize_checks(checks)

            lines.append(f"- **#{number}** {title}")
            lines.append(f"  Branch: `{branch}` | Updated: {updated} | CI: {ci_summary}")
        return "\n".join(lines)

    async def _check_ci(self, repo: str, pr_number: int) -> str:
        """Show CI check results for a PR."""
        rc, out, err = await run_gh([
            "pr", "checks",
            str(pr_number),
            "--repo", repo,
        ])
        error = check_gh_error(rc, err)
        # gh pr checks returns exit 1 when checks fail -- that is valid output
        if error and not out:
            return error

        if not out:
            return f"No CI checks found for PR#{pr_number}."

        return f"**CI Checks for PR#{pr_number}:**\n\n```\n{out[:3000]}\n```"

    async def _coderabbit_threads(self, repo: str, pr_number: int) -> str:
        """Show unresolved review threads."""
        rc, out, err = await run_gh([
            "pr", "view",
            str(pr_number),
            "--repo", repo,
            "--json", "reviewThreads",
        ])
        error = check_gh_error(rc, err)
        if error:
            return error

        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return f"Error parsing review threads: {out[:500]}"

        threads = data.get("reviewThreads", [])
        unresolved = [t for t in threads if not t.get("isResolved", True)]

        if not unresolved:
            return f"No unresolved review threads on PR#{pr_number}."

        lines = [f"**{len(unresolved)} Unresolved Thread(s) on PR#{pr_number}:**\n"]
        for i, thread in enumerate(unresolved, 1):
            path = thread.get("path", "unknown file")
            line_num = thread.get("line", "?")
            comments = thread.get("comments", {}).get("nodes", [])

            lines.append(f"### Thread {i}: {path}:{line_num}")
            for comment in comments[:3]:
                author = comment.get("author", {}).get("login", "?")
                body = comment.get("body", "")[:300]
                lines.append(f"  **{author}**: {body}")
            if len(comments) > 3:
                lines.append(f"  _...and {len(comments) - 3} more comment(s)_")
            lines.append("")
        return "\n".join(lines)

    async def _diff_summary(self, repo: str, pr_number: int) -> str:
        """Show first 200 lines of a PR diff."""
        rc, out, err = await run_gh([
            "pr", "diff",
            str(pr_number),
            "--repo", repo,
        ])
        error = check_gh_error(rc, err)
        if error:
            return error

        if not out:
            return f"No diff found for PR#{pr_number}."

        diff_lines = out.split("\n")
        truncated = len(diff_lines) > 200
        preview = "\n".join(diff_lines[:200])

        suffix = ""
        if truncated:
            suffix = f"\n\n_...truncated ({len(diff_lines)} total lines)_"

        return f"**Diff for PR#{pr_number}:**\n\n```diff\n{preview}\n```{suffix}"


def _summarize_checks(checks: list[dict[str, Any]]) -> str:
    """Summarize a list of CI checks into a compact status string."""
    if not checks:
        return "none"

    statuses: dict[str, int] = {}
    for check in checks:
        conclusion = (
            check.get("conclusion")
            or check.get("status")
            or check.get("state")
            or "pending"
        ).lower()
        statuses[conclusion] = statuses.get(conclusion, 0) + 1

    parts = []
    for status, count in sorted(statuses.items()):
        parts.append(f"{count} {status}")
    return ", ".join(parts)
