"""Shared gh CLI subprocess runner for Quinn QA tools.

Provides a thin async wrapper around the GitHub CLI, used by
pr_inspector, github_issues, and release_notes tools.
"""

import asyncio
import os

_DEFAULT_REPO = "protoLabsAI/protoMaker"
_COMMAND_TIMEOUT = 30


def get_repo() -> str:
    """Get the configured GitHub repository (owner/name format)."""
    return os.environ.get("GITHUB_REPO", _DEFAULT_REPO)


async def run_gh(
    args: list[str], timeout: int = _COMMAND_TIMEOUT
) -> tuple[int, str, str]:
    """Execute a gh CLI command and return (returncode, stdout, stderr).

    Handles timeouts gracefully and reports missing gh CLI.
    """
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


def check_gh_error(returncode: int, stderr: str) -> str | None:
    """Return a formatted error string if the command failed, else None."""
    if returncode != 0:
        return f"Error (exit {returncode}): {stderr[:500]}"
    return None
