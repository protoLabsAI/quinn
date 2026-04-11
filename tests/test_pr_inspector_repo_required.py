"""Regression test for #74 — pr_inspector must require explicit `repo`.

Background: Quinn's pr_review on protoWorkstacean#104 pulled CodeRabbit
feedback from protoMaker because the tool silently defaulted to a hardcoded
`protoLabsAI/protoMaker` when an agent omitted the `repo` arg. The fix
removed the default and made `repo` required at the schema boundary —
this test locks the behaviour so the default never sneaks back.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from tools.pr_inspector import PrInspectorTool


def test_repo_is_required_in_schema() -> None:
    tool = PrInspectorTool()
    params = tool.parameters
    required = params.get("required", [])
    assert "repo" in required, (
        f"pr_inspector schema must list `repo` as required; got required={required}"
    )


def test_execute_errors_loudly_when_repo_missing() -> None:
    tool = PrInspectorTool()
    result = asyncio.run(tool.execute(action="coderabbit_threads", pr_number=104))
    assert "Error" in result
    assert "repo" in result


def test_execute_errors_loudly_when_repo_blank_string() -> None:
    tool = PrInspectorTool()
    result = asyncio.run(
        tool.execute(action="coderabbit_threads", pr_number=104, repo="")
    )
    assert "Error" in result
    assert "repo" in result


def test_execute_rejects_malformed_repo() -> None:
    tool = PrInspectorTool()
    result = asyncio.run(
        tool.execute(action="coderabbit_threads", pr_number=104, repo="not-a-slash")
    )
    assert "owner/name" in result


def test_error_message_mentions_env_var_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """When GITHUB_REPO is set, the error message should mention it to help
    operators understand why the call failed — and explicitly reject falling
    back to it, since the whole point of the fix is no silent defaults."""
    monkeypatch.setenv("GITHUB_REPO", "protoLabsAI/some-repo")
    tool = PrInspectorTool()
    result = asyncio.run(tool.execute(action="coderabbit_threads", pr_number=104))
    assert "Error" in result
    assert "protoLabsAI/some-repo" in result
    # Must not have silently used the env var
    assert "last-resort fallback" in result or "must pass repo explicitly" in result


def test_get_repo_returns_none_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """No hardcoded protoLabsAI/protoMaker default — get_repo must return None
    when GITHUB_REPO is unset so callers can treat absence as an error."""
    monkeypatch.delenv("GITHUB_REPO", raising=False)
    from tools.gh_cli import get_repo

    assert get_repo() is None
