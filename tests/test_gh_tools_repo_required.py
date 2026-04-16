"""Regression tests for the #74-class bug in github_actions, github_issues,
and release_notes — each gh-backed tool must require an explicit `repo`.

Background: A qa_report run failed with
    TypeError: expected str, bytes or os.PathLike object, not NoneType
inside asyncio.create_subprocess_exec. Root cause: `repo = kwargs.get("repo")
or get_repo()` returned None (GITHUB_REPO isn't set in the deployed
container), and None leaked into the gh argv as `--repo None`, which crashes
asyncio's subprocess setup with an unreadable error.

pr_inspector already had this guard from incident #74. These tests lock the
same guarantee in for github_actions / github_issues / release_notes.
"""

from __future__ import annotations

import pytest

from tools.github_actions import GitHubActionsTool
from tools.github_issues import GitHubIssuesTool
from tools.release_notes import ReleaseNotesTool


# ── github_actions ────────────────────────────────────────────────────────────


def test_github_actions_schema_requires_repo() -> None:
    required = GitHubActionsTool().parameters.get("required", [])
    assert "repo" in required, (
        f"github_actions must require `repo` in schema; got required={required}"
    )


@pytest.mark.asyncio
async def test_github_actions_errors_when_repo_missing() -> None:
    result = await GitHubActionsTool().execute(action="list_workflows")
    assert "Error" in result and "repo" in result


@pytest.mark.asyncio
async def test_github_actions_errors_when_repo_blank() -> None:
    result = await GitHubActionsTool().execute(action="list_workflows", repo="")
    assert "Error" in result and "repo" in result


@pytest.mark.asyncio
async def test_github_actions_rejects_malformed_repo() -> None:
    result = await GitHubActionsTool().execute(
        action="list_workflows", repo="not-a-slash"
    )
    assert "owner/name" in result


@pytest.mark.asyncio
async def test_github_actions_error_mentions_env_var_when_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_REPO", "protoLabsAI/some-repo")
    result = await GitHubActionsTool().execute(action="list_workflows")
    assert "protoLabsAI/some-repo" in result
    assert "last-resort fallback" in result or "must pass repo explicitly" in result


# ── github_issues ─────────────────────────────────────────────────────────────


def test_github_issues_schema_requires_repo() -> None:
    required = GitHubIssuesTool().parameters.get("required", [])
    assert "repo" in required, (
        f"github_issues must require `repo` in schema; got required={required}"
    )


@pytest.mark.asyncio
async def test_github_issues_errors_when_repo_missing() -> None:
    result = await GitHubIssuesTool().execute(action="list_open")
    assert "Error" in result and "repo" in result


@pytest.mark.asyncio
async def test_github_issues_errors_when_repo_blank() -> None:
    result = await GitHubIssuesTool().execute(action="list_open", repo="")
    assert "Error" in result and "repo" in result


@pytest.mark.asyncio
async def test_github_issues_rejects_malformed_repo() -> None:
    result = await GitHubIssuesTool().execute(action="list_open", repo="not-a-slash")
    assert "owner/name" in result


# ── release_notes ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_release_notes_errors_when_repo_missing_for_gh_action() -> None:
    """changelog hits gh, so it must require repo. post_to_discord doesn't."""
    result = await ReleaseNotesTool().execute(action="changelog")
    assert "Error" in result and "repo" in result


@pytest.mark.asyncio
async def test_release_notes_rejects_malformed_repo() -> None:
    result = await ReleaseNotesTool().execute(
        action="changelog", repo="not-a-slash"
    )
    assert "owner/name" in result


@pytest.mark.asyncio
async def test_release_notes_post_to_discord_does_not_require_repo() -> None:
    """post_to_discord has nothing to do with gh — repo absence mustn't trip
    the guard on this action. It will fail downstream if the webhook isn't
    configured, but not on the repo guard."""
    result = await ReleaseNotesTool().execute(action="post_to_discord", content="hi")
    # The absence of "repo is required" confirms the guard was skipped.
    assert "repo" not in result.lower() or "discord" in result.lower()
