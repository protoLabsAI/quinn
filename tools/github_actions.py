"""GitHub Actions workflow management for Quinn QA agent.

Inspects CI workflow runs, triggers pipelines, reruns failed jobs, and
retrieves failure logs -- all via the gh CLI.

Requires: gh CLI authenticated and GITHUB_REPO env var (default: protoLabsAI/protoMaker).
"""

import asyncio
import json
from typing import Any

from nanobot.agent.tools.base import Tool

from tools.gh_cli import check_gh_error, get_repo, run_gh

# Timeout for polling operations (trigger + monitor)
_POLL_INTERVAL = 15
_POLL_TIMEOUT = 600  # 10 minutes max


class GitHubActionsTool(Tool):
    """Manage GitHub Actions workflows -- list, trigger, inspect, rerun."""

    @property
    def name(self) -> str:
        return "github_actions"

    @property
    def description(self) -> str:
        return (
            "Manage GitHub Actions CI workflows.\n\n"
            "Actions:\n"
            "- list_workflows: List available workflows with name and state\n"
            "- trigger_workflow: Trigger a workflow on a branch\n"
            "- list_runs: List recent runs for a workflow\n"
            "- view_run: View status, conclusion, and jobs for a specific run\n"
            "- rerun_failed: Rerun only the failed jobs of a run\n"
            "- run_logs: Get failure logs from a run (truncated to 3000 chars)\n"
            "- run_tests: Trigger checks.yml, poll until complete, return summary\n\n"
            "Default repo from GITHUB_REPO env var (override with repo parameter)."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "list_workflows",
                        "trigger_workflow",
                        "list_runs",
                        "view_run",
                        "rerun_failed",
                        "run_logs",
                        "run_tests",
                    ],
                    "description": "Action to perform.",
                },
                "workflow": {
                    "type": "string",
                    "description": "Workflow filename or name (e.g. 'checks.yml'). Required for trigger_workflow, list_runs.",
                },
                "run_id": {
                    "type": "integer",
                    "description": "Workflow run ID. Required for view_run, rerun_failed, run_logs.",
                },
                "ref": {
                    "type": "string",
                    "description": "Git branch ref (default: 'dev').",
                },
                "repo": {
                    "type": "string",
                    "description": "Repository in owner/name format (e.g. 'protoLabsAI/protoMaker').",
                },
            },
            "required": ["action", "repo"],
        }

    async def execute(self, **kwargs: Any) -> str:
        action = kwargs["action"]
        # Fail loudly on missing repo. get_repo() reads GITHUB_REPO but the
        # deployed container doesn't set it, so a missing arg used to fall
        # through as None and crash asyncio.create_subprocess_exec with
        # "expected str, bytes or os.PathLike object, not NoneType" —
        # unreadable without a traceback. Mirror the pr_inspector guard (#74)
        # so the agent sees a clear error and retries with repo= set.
        repo = kwargs.get("repo")
        if not repo:
            env_repo = get_repo()
            if env_repo:
                return (
                    f"Error: github_actions requires an explicit `repo` argument on every call. "
                    f"The GITHUB_REPO env var is set to '{env_repo}' but that is only used as a "
                    f"last-resort fallback for internal scripts — agent dispatches must pass repo "
                    f"explicitly from the task context they were given."
                )
            return (
                "Error: github_actions requires a `repo` argument (owner/name format). "
                "Extract the repo from the task context in the incoming message."
            )
        if "/" not in repo or repo.count("/") != 1:
            return f"Error: `repo` must be in owner/name format, got '{repo}'."
        ref = kwargs.get("ref") or "dev"
        workflow = kwargs.get("workflow", "")
        run_id = kwargs.get("run_id")

        if action == "list_workflows":
            return await self._list_workflows(repo)

        elif action == "trigger_workflow":
            if not workflow:
                return "Error: 'workflow' is required for trigger_workflow."
            return await self._trigger_workflow(repo, workflow, ref)

        elif action == "list_runs":
            if not workflow:
                return "Error: 'workflow' is required for list_runs."
            return await self._list_runs(repo, workflow)

        elif action == "view_run":
            if not run_id:
                return "Error: 'run_id' is required for view_run."
            return await self._view_run(repo, run_id)

        elif action == "rerun_failed":
            if not run_id:
                return "Error: 'run_id' is required for rerun_failed."
            return await self._rerun_failed(repo, run_id)

        elif action == "run_logs":
            if not run_id:
                return "Error: 'run_id' is required for run_logs."
            return await self._run_logs(repo, run_id)

        elif action == "run_tests":
            return await self._run_tests(repo, ref)

        else:
            return f"Error: Unknown action '{action}'."

    async def _list_workflows(self, repo: str) -> str:
        """List available workflows."""
        rc, out, err = await run_gh([
            "workflow", "list",
            "--repo", repo,
            "--json", "name,state",
            "--limit", "20",
        ])
        error = check_gh_error(rc, err)
        if error:
            return error

        if not out:
            return "No workflows found."

        try:
            workflows = json.loads(out)
        except json.JSONDecodeError:
            return f"Error parsing workflows: {out[:500]}"

        if not workflows:
            return "No workflows found."

        lines = [f"**{len(workflows)} Workflow(s) in {repo}:**\n"]
        for wf in workflows:
            name = wf.get("name", "?")
            state = wf.get("state", "?")
            lines.append(f"- **{name}** ({state})")
        return "\n".join(lines)

    async def _trigger_workflow(self, repo: str, workflow: str, ref: str) -> str:
        """Trigger a workflow run on a branch."""
        rc, out, err = await run_gh([
            "workflow", "run", workflow,
            "--repo", repo,
            "--ref", ref,
        ])
        error = check_gh_error(rc, err)
        if error:
            return error

        return f"Triggered workflow `{workflow}` on branch `{ref}` in {repo}."

    async def _list_runs(self, repo: str, workflow: str) -> str:
        """List recent runs for a workflow."""
        rc, out, err = await run_gh([
            "run", "list",
            "--repo", repo,
            "--workflow", workflow,
            "--json", "databaseId,status,conclusion,headBranch,displayTitle",
            "--limit", "10",
        ])
        error = check_gh_error(rc, err)
        if error:
            return error

        if not out:
            return f"No runs found for workflow `{workflow}`."

        try:
            runs = json.loads(out)
        except json.JSONDecodeError:
            return f"Error parsing runs: {out[:500]}"

        if not runs:
            return f"No runs found for workflow `{workflow}`."

        lines = [f"**Recent runs for `{workflow}`:**\n"]
        for run in runs:
            run_id = run.get("databaseId", "?")
            status = run.get("status", "?")
            conclusion = run.get("conclusion", "-")
            branch = run.get("headBranch", "?")
            title = run.get("displayTitle", "Untitled")
            result = conclusion if conclusion and conclusion != "-" else status
            lines.append(f"- **#{run_id}** `{result}` on `{branch}` -- {title}")
        return "\n".join(lines)

    async def _view_run(self, repo: str, run_id: int) -> str:
        """View status and jobs for a specific run."""
        rc, out, err = await run_gh([
            "run", "view", str(run_id),
            "--repo", repo,
            "--json", "status,conclusion,jobs",
        ])
        error = check_gh_error(rc, err)
        if error:
            return error

        if not out:
            return f"No data found for run #{run_id}."

        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return f"Error parsing run data: {out[:500]}"

        status = data.get("status", "?")
        conclusion = data.get("conclusion", "-")
        jobs = data.get("jobs", [])

        lines = [f"**Run #{run_id}:** {status} / {conclusion}\n"]
        if jobs:
            lines.append("**Jobs:**")
            for job in jobs:
                job_name = job.get("name", "?")
                job_conclusion = job.get("conclusion") or job.get("status", "?")
                lines.append(f"- {job_name}: `{job_conclusion}`")
        else:
            lines.append("_No job data available._")
        return "\n".join(lines)

    async def _rerun_failed(self, repo: str, run_id: int) -> str:
        """Rerun only the failed jobs of a run."""
        rc, out, err = await run_gh([
            "run", "rerun", str(run_id),
            "--repo", repo,
            "--failed",
        ])
        error = check_gh_error(rc, err)
        if error:
            return error

        return f"Rerun triggered for failed jobs in run #{run_id}."

    async def _run_logs(self, repo: str, run_id: int) -> str:
        """Get failure logs from a run, truncated to 3000 chars."""
        rc, out, err = await run_gh([
            "run", "view", str(run_id),
            "--repo", repo,
            "--log-failed",
        ], timeout=60)
        # --log-failed returns exit 1 when there are failed jobs -- that is valid output
        if rc != 0 and not out:
            error = check_gh_error(rc, err)
            if error:
                return error

        if not out:
            return f"No failure logs found for run #{run_id}."

        truncated = len(out) > 3000
        preview = out[:3000]

        suffix = ""
        if truncated:
            suffix = f"\n\n_...truncated ({len(out)} total chars)_"

        return f"**Failure logs for run #{run_id}:**\n\n```\n{preview}\n```{suffix}"

    async def _run_tests(self, repo: str, ref: str) -> str:
        """Trigger checks.yml, poll until complete, return pass/fail summary."""
        workflow = "checks.yml"

        # Trigger the workflow
        rc, _, err = await run_gh([
            "workflow", "run", workflow,
            "--repo", repo,
            "--ref", ref,
        ])
        error = check_gh_error(rc, err)
        if error:
            return f"Failed to trigger {workflow}: {error}"

        # Wait briefly for the run to appear in the API
        await asyncio.sleep(5)

        # Find the run we just triggered
        run_id = await self._find_latest_run(repo, workflow, ref)
        if not run_id:
            return f"Triggered {workflow} on `{ref}` but could not locate the run. Check manually."

        # Poll until complete
        elapsed = 0
        while elapsed < _POLL_TIMEOUT:
            rc, out, err = await run_gh([
                "run", "view", str(run_id),
                "--repo", repo,
                "--json", "status,conclusion,jobs",
            ])
            if rc != 0:
                return f"Error polling run #{run_id}: {err[:300]}"

            try:
                data = json.loads(out)
            except json.JSONDecodeError:
                return f"Error parsing poll data: {out[:300]}"

            status = data.get("status", "")
            if status == "completed":
                return self._format_run_summary(run_id, data)

            await asyncio.sleep(_POLL_INTERVAL)
            elapsed += _POLL_INTERVAL

        return f"Run #{run_id} did not complete within {_POLL_TIMEOUT // 60} minutes. Status: {status}."

    async def _find_latest_run(self, repo: str, workflow: str, ref: str) -> int | None:
        """Find the most recent run for a workflow on a branch."""
        rc, out, err = await run_gh([
            "run", "list",
            "--repo", repo,
            "--workflow", workflow,
            "--branch", ref,
            "--json", "databaseId,status",
            "--limit", "1",
        ])
        if rc != 0 or not out:
            return None

        try:
            runs = json.loads(out)
        except json.JSONDecodeError:
            return None

        if runs:
            return runs[0].get("databaseId")
        return None

    @staticmethod
    def _format_run_summary(run_id: int, data: dict[str, Any]) -> str:
        """Format a completed run into a pass/fail summary."""
        conclusion = data.get("conclusion", "unknown")
        jobs = data.get("jobs", [])

        passed = [j for j in jobs if j.get("conclusion") == "success"]
        failed = [j for j in jobs if j.get("conclusion") == "failure"]
        skipped = [j for j in jobs if j.get("conclusion") == "skipped"]

        verdict = "PASS" if conclusion == "success" else "FAIL"

        lines = [
            f"**{verdict}** -- Run #{run_id} completed: `{conclusion}`\n",
            f"- Passed: {len(passed)}",
            f"- Failed: {len(failed)}",
            f"- Skipped: {len(skipped)}",
        ]

        if failed:
            lines.append("\n**Failed jobs:**")
            for job in failed:
                lines.append(f"- {job.get('name', '?')}")

        return "\n".join(lines)
