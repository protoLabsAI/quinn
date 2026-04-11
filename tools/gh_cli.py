"""Shared gh CLI subprocess runner for Quinn QA tools.

Provides a thin async wrapper around the GitHub CLI, used by
pr_inspector, github_issues, and release_notes tools.

Prefers a short-lived GitHub App installation token (managed by
github_app_auth.refresh_forever) over a plain PAT so Quinn's reviews
post as @protoquinn[bot]. Falls back to GITHUB_TOKEN env var if the
token file hasn't been written yet.
"""

import asyncio
import os

from tools.github_app_auth import read_cached_token

_COMMAND_TIMEOUT = 30


def get_repo() -> str | None:
    """Get the configured GitHub repository (owner/name format), or None.

    Used as a caller-supplied fallback. Previously this hardcoded
    'protoLabsAI/protoMaker' as the default, which silently misrouted
    queries when an agent forgot to pass an explicit repo — Quinn was
    observed pulling CodeRabbit feedback from protoMaker during a
    protoWorkstacean#104 pr_review because the repo arg was omitted and
    the default kicked in. Now returns None when nothing is configured,
    so tool schemas can enforce repo as required at the boundary.
    """
    return os.environ.get("GITHUB_REPO") or None


def _resolve_token() -> str | None:
    """Pick the best available GitHub token. Prefers the App installation token
    written by github_app_auth (fresh, <45 min old, posts as @protoquinn[bot]),
    falls back to the GITHUB_TOKEN env var otherwise."""
    cached = read_cached_token()
    if cached:
        return cached
    return os.environ.get("GITHUB_TOKEN") or None


async def run_gh(
    args: list[str], timeout: int = _COMMAND_TIMEOUT
) -> tuple[int, str, str]:
    """Execute a gh CLI command and return (returncode, stdout, stderr).

    Handles timeouts gracefully and reports missing gh CLI. Injects the
    currently-cached GitHub App installation token as GITHUB_TOKEN in the
    subprocess env so each gh call uses a fresh token.
    """
    env = os.environ.copy()
    token = _resolve_token()
    if token:
        env["GITHUB_TOKEN"] = token

    try:
        proc = await asyncio.create_subprocess_exec(
            "gh", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
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
