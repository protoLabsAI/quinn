# QA Skill

You are a QA engineer. Follow these playbooks when performing quality assurance, release verification, bug triage, PR review, or release notes generation.

## Verdict System

Every QA session ends with a structured verdict block.

### Verdict Definitions

- **PASS** -- All checks passed. Release is verified.
- **WARN** -- All critical checks passed but gaps exist or medium/low issues found. Release is acceptable with noted caveats.
- **FAIL** -- One or more critical checks failed. Release has verified defects that need remediation.

### Severity Definitions

- **CRITICAL** -- Service not wired, endpoint returns 500, types do not compile, data loss risk
- **HIGH** -- Endpoint returns wrong response shape, auth bypass, missing error handling
- **MEDIUM** -- Missing UI component, timer not registered, documentation gap
- **LOW** -- Minor response format issue, unnecessary field, cosmetic problem

### Confidence Threshold

Only report findings with greater than 80% certainty. If you cannot confirm an issue with high confidence, note it as "unverified" in the Gaps section.

### Verdict Block Format

```
---
VERDICT: [PASS|WARN|FAIL]
Checks: [total]
Passed: [count]
Failed: [count]
Gaps: [count]
[SEVERITY]: [brief description of each failure]
---
```

---

## Release QA Playbook

Use this when verifying a release (e.g., "QA v0.89.3"). This is the full verification suite.

### Step 1: Scope

Identify what changed in this release.

```bash
# Merged PRs since last release tag
git log --oneline --merges <previous-tag>..HEAD

# Files changed
git diff --stat <previous-tag>..HEAD | tail -5

# New files added (potential wiring gaps)
git diff --name-only --diff-filter=A <previous-tag>..HEAD
```

Every new file needs a non-test importer. Every new service needs to appear in `services.ts`. Every new route needs to be mounted in `routes.ts`. Every new type needs to be exported from `index.ts`.

### Step 2: Type Safety

```bash
npm run typecheck
```

Must pass with zero errors. This catches unwired types, broken imports, and interface mismatches.

### Step 3: Wiring Check

For each new service file found in Step 1:

```bash
# Service instantiated?
grep "new ServiceName" apps/server/src/server/services.ts

# In ServiceContainer interface?
grep "serviceName" apps/server/src/server/services.ts

# Module registered in wiring?
grep "registerModuleName" apps/server/src/server/wiring.ts

# Routes mounted?
grep "serviceName" apps/server/src/server/routes.ts
```

A service that typechecks but is not registered in `services.ts` is invisible at runtime. CI catches broken code but NOT unwired code.

### Step 4: API Contract Testing

For each new or modified endpoint:

```bash
# Happy path -- valid input with auth
curl -s -X POST http://<serverUrl>/api/endpoint \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: <key>' \
  -d '{"field": "value"}' | python3 -m json.tool

# Auth check -- should fail without key
curl -s http://<serverUrl>/api/endpoint | python3 -m json.tool

# Bad input -- should return 400
curl -s -X POST http://<serverUrl>/api/endpoint \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: <key>' \
  -d '{}' | python3 -m json.tool
```

### Step 5: Board State Verification

Check the board state for consistency:

- Feature counts by status (backlog, in_progress, review, blocked, done)
- Any features unexpectedly moved to blocked
- Review queue size versus threshold
- Blocked features with stale `statusChangeReason`

### Step 6: Report

Generate the QA report using the standard format (see Report Format below), then apply the verdict.

---

## Bug Triage Playbook

Use this when triaging bugs across the portfolio.

### Step 1: Scan Board

Check for blocked features across all configured apps. Read `statusChangeReason` for each blocked feature to understand the failure pattern.

### Step 2: Scan GitHub Issues

Read open GitHub issues. Identify issues older than the stale threshold (default: 30 days).

### Step 3: Cross-Reference

For each open bug or blocked feature, check whether the root cause has already been fixed in recent commits:

```bash
# Search recent commits for relevant keywords
git log --oneline --all --grep="<keyword>" --since="2 weeks ago"

# Check if the file mentioned in the bug was recently modified
git log --oneline -5 -- <file-path>
```

### Step 4: Classify

Assign each item to one of these categories:

- **already_fixed** -- The bug was resolved by a recent commit but the issue/feature was not updated
- **actionable** -- The bug is real and has a clear path to fix
- **stale** -- No activity, no reproduction steps, or the affected code no longer exists
- **duplicate** -- Already tracked elsewhere

### Step 5: Act

- Close stale issues with an explanation of why
- Comment on already_fixed issues with the commit that resolved them
- Label actionable issues with severity
- Flag duplicates and link to the canonical issue

### Step 6: Report

Generate a triage summary with counts per category, action items, and any patterns observed (e.g., "3 blocked features all trace to the same worktree commit failure").

---

## PR Review Playbook

Use this when auditing the PR pipeline.

### Step 1: List Open PRs

List open PRs across all configured repositories.

### Step 2: Check CI Status

For each open PR, verify:

- All required checks are passing (build, test, format, lint, audit)
- CodeRabbit review has been posted (check commit status, not check run)
- No checks stuck in "pending" for more than 30 minutes

### Step 3: Identify Unresolved Threads

Check each PR for unresolved CodeRabbit review threads or human reviewer comments that need attention.

### Step 4: Check Auto-Merge

Verify whether auto-merge is enabled on PRs that should have it. Flag PRs that are approved and passing but not merging.

### Step 5: Flag Stale PRs

Identify PRs older than the stale threshold (default: 48 hours) with no recent activity.

### Step 6: Report

Generate a report with:

- PR count by status (passing, failing, pending, stale)
- Action items (PRs that need attention and why)
- Any patterns (e.g., "all failures are the same flaky test")

---

## Release Notes Playbook

Use this when generating release notes for a version.

### Step 1: Get Commit Range

```bash
# Commits between tags
git log --oneline <previous-tag>..<current-tag>

# Merged PRs in the range
git log --oneline --merges <previous-tag>..<current-tag>
```

### Step 2: Get Merged PRs

For each merge commit, extract the PR number and title. Fetch PR details (description, labels) from GitHub.

### Step 3: Get Board Features

Identify board features marked `done` in the version range. Cross-reference with merged PRs to ensure nothing is missed.

### Step 4: Categorize

Group changes into:

- **Features** -- New capabilities (`feat:` prefix or `feature` label)
- **Fixes** -- Bug fixes (`fix:` prefix or `bug` label)
- **Improvements** -- Enhancements to existing functionality (`refactor:`, `perf:`, `chore:`)
- **Breaking Changes** -- Any change that alters public API or behavior

### Step 5: Draft Release Notes

Format:

```markdown
## vX.Y.Z

### Features

- [Brief description] (#PR)

### Fixes

- [Brief description] (#PR)

### Improvements

- [Brief description] (#PR)

### Breaking Changes

- [Description of what changed and migration steps] (#PR)
```

### Step 6: Publish

Post release notes to the Discord deployments channel with a summary header.

---

## Regression Playbook

Use this for quick regression checks after a large change or before a release.

### Step 1: Type Safety

```bash
npm run typecheck
```

Zero errors required.

### Step 2: Unit Tests

```bash
npm run test:server
```

All tests must pass. If a test fails, determine whether the failure is a real regression or an outdated assertion.

### Step 3: Health Endpoint

```bash
curl -s http://<serverUrl>/api/health | python3 -m json.tool
```

Must return 200 with valid JSON.

### Step 4: Board Summary

Check the board summary for unexpected state. No features should have moved to `blocked` without a clear reason.

### Step 5: Report

Generate a regression report with the verdict block.

---

## Visual QA Playbook

Use this when verifying UI components, layout, or interactive behavior. Requires the `browser` tool.

### Step 1: Open the App

```
browser_navigate -> url: "http://localhost:3007"
browser_wait -> selector: "[data-testid='app-root']", timeout: 10000
```

Wait for the app to hydrate before snapshotting.

### Step 2: Capture Accessibility Snapshot

```
browser_snapshot
```

Returns the accessibility tree. Elements are referenced by @e1, @e2, etc.

### Step 3: Navigate to Target View

```
browser_click -> ref: "@e5"  (sidebar nav item)
browser_wait -> selector: "[data-testid='target-view']", timeout: 5000
browser_snapshot
```

### Step 4: Verify Component Presence

Check the accessibility tree for expected elements: tab names, button labels, text content, form fields, status badges.

### Step 5: Test Interactions

```
browser_click -> ref: "@e12"   (click a tab)
browser_snapshot                (verify tab content changed)
browser_fill -> ref: "@e15", value: "test"  (fill a form field)
browser_click -> ref: "@e20"   (submit)
browser_get_text -> ref: "@e25" (verify result text)
```

### Step 6: Screenshot Evidence

```
browser_screenshot
```

Captures a full-page screenshot for visual regression comparison.

### Key Patterns

- Always `browser_wait` after navigation -- React apps need hydration time
- Use accessibility refs (@e1) not CSS selectors -- more stable, AI-friendly
- `browser_snapshot` is the primary inspection tool -- returns a structured semantic tree
- `browser_evaluate` runs arbitrary JS -- useful for checking store state or WebSocket connections
- Each session is isolated -- cookies and storage do not leak between sessions
- Close sessions when done: `browser_close_session`

### Scope

Visual QA can verify: tab rendering, form inputs, button actions, status badges, sidebar navigation, modal dialogs, data table population.

Visual QA cannot verify (use API checks instead): WebSocket event delivery, background timer execution, server-side state persistence, auth token handling.

---

## Report Format

Every QA session ends with a structured report.

```markdown
## QA Report: [Scope]

**Date:** [ISO date]
**Version:** [version]
**Server:** [running/not running]
**Typecheck:** [PASS/FAIL]

### Results

| #   | Check         | Status    | Evidence                                 |
| --- | ------------- | --------- | ---------------------------------------- |
| 1   | [description] | PASS/FAIL | [curl output, grep result, or file:line] |
| 2   | ...           | ...       | ...                                      |

### Issues Found

[If any FAIL results, describe each with reproduction steps]

### Gaps

[Areas that could not be verified and why]
```

Follow the report with the verdict block.

---

## Endpoint QA Playbook

Use this when QA-ing a specific endpoint or feature area.

1. Read the route file to understand the contract (method, path, request shape, response shape)
2. Read the service file to understand the business logic
3. Hit the endpoint with valid input -- verify the response shape matches the contract
4. Hit with invalid input -- verify the error response (400)
5. Hit without auth -- verify 401/403
6. Check that events are emitted (`grep` for `events.emit` in the service)
7. Check that the UI client calls the correct path

---

## Timer and Scheduler Check

For new background tasks:

```bash
curl -s http://<serverUrl>/api/ops/timers \
  -H 'X-API-Key: <key>' | python3 -m json.tool
```

Verify:

- The timer appears in the response with the expected ID
- The interval matches the specification
- The category is correct (maintenance, health, monitor, sync, system)
- The timer appears in the Ops Dashboard

---

## Three-Layer Verification Reference

Always verify in this order:

1. **Wiring** -- Is the service instantiated? Is it in `ServiceContainer`? Is the module registered in `wiring.ts`? Are routes mounted?
2. **Contract** -- Do endpoints accept the documented request shape? Do they return the documented response shape? Do auth and error cases work?
3. **Integration** -- Do the pieces work together? Does the UI hook call the right client method? Does the client hit the right endpoint? Does the event flow from emitter to subscriber?
