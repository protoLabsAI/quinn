"""Data models for Quinn's QA knowledge base."""

from dataclasses import dataclass, field


@dataclass
class QAReport:
    id: int = 0
    app_name: str = ""
    version: str = ""
    scope: str = ""  # release/regression/endpoint/visual
    verdict: str = ""  # PASS/WARN/FAIL
    checks_total: int = 0
    checks_passed: int = 0
    checks_failed: int = 0
    gaps: int = 0
    content: str = ""  # full report markdown
    findings: list[dict] = field(default_factory=list)
    created_at: str = ""


@dataclass
class BugPattern:
    id: int = 0
    title: str = ""
    description: str = ""
    app_name: str = ""
    severity: str = ""  # CRITICAL/HIGH/MEDIUM/LOW
    category: str = ""  # wiring/contract/integration/visual/performance
    pattern: str = ""  # what to look for
    occurrences: int = 1
    first_seen: str = ""
    last_seen: str = ""
    resolved: bool = False
    resolution: str = ""


@dataclass
class ReleaseNote:
    id: int = 0
    app_name: str = ""
    version: str = ""
    title: str = ""
    content: str = ""  # full release notes markdown
    features_count: int = 0
    fixes_count: int = 0
    breaking_changes: list[str] = field(default_factory=list)
    published: bool = False
    published_at: str = ""
    created_at: str = ""


@dataclass
class TriageEntry:
    id: int = 0
    source: str = ""  # github_issue/board_feature/discord_report
    source_id: str = ""  # issue number, feature ID
    app_name: str = ""
    classification: str = ""  # already_fixed/actionable/stale/duplicate
    severity: str = ""  # CRITICAL/HIGH/MEDIUM/LOW
    reason: str = ""
    action_taken: str = ""  # closed/labeled/commented/escalated
    created_at: str = ""
