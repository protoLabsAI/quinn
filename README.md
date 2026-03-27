# Quinn

Autonomous QA Engineer and Release Manager for protoLabs Studio.

Quinn monitors the board across all apps in your portfolio, triages bugs, verifies PRs, generates release notes, and keeps the community informed. Built on LangGraph with three specialized subagents.

## What Quinn Does

- **Board Monitoring** — Scans blocked features, stale PRs, review queue saturation across all configured apps
- **Bug Triage** — Classifies GitHub issues as fixed/actionable/stale/duplicate, closes stale ones with evidence
- **PR Verification** — Checks CI status, CodeRabbit threads, auto-merge readiness
- **Release QA** — Runs verification playbooks: typecheck, wiring, endpoint contracts, visual QA
- **Release Notes** — Generates changelogs from git history, merged PRs, and board state
- **Community Updates** — Posts release announcements and QA reports to Discord

## Architecture

```
User / Cron
    |
    v
Quinn (LangGraph Agent)
  |-- Auditor subagent (board_monitor, pr_inspector, github_issues)
  |-- Verifier subagent (qa_memory, browser)
  |-- Reporter subagent (qa_memory, discord_feed, release_notes)
    |
    v
protoLabs Studio API (HTTP) + GitHub CLI + Discord Webhooks
```

**LLM**: Claude Sonnet 4.6 via CLIProxyAPI (Claude Code OAuth)
**UI**: Gradio chat interface
**Knowledge**: SQLite + sqlite-vec (QA reports, bug patterns, release history)
**Observability**: Langfuse tracing, JSONL audit logs

## Quick Start

### Prerequisites

- Docker and Docker Compose
- [Infisical CLI](https://infisical.com/docs/cli/overview) installed and logged in to `secrets.proto-labs.ai`
- Claude Code authenticated (`claude` CLI logged in)
- protoLabs Studio server running (default: localhost:3008)

### 1. Clone and configure

```bash
git clone https://github.com/protoLabsAI/quinn.git
cd quinn

# Log in to Infisical (one-time)
infisical login --domain https://secrets.proto-labs.ai

# Edit config/qa-config.json with your apps
```

### 2. Run (one-liner)

```bash
INFISICAL_API_URL=https://secrets.proto-labs.ai infisical run --env prod -- docker compose up --build
```

This pulls 24 secrets from Infisical, injects them as env vars, and starts Quinn. No `.env` files needed.

Quinn's UI will be available at **http://localhost:7871**.

### 3. Auto-start on boot (systemd)

```bash
# Install the service
sudo cp quinn.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable quinn
sudo systemctl start quinn

# Check status
sudo systemctl status quinn
journalctl -u quinn -f
```

### Secrets Management

All secrets are managed via [Infisical](https://secrets.proto-labs.ai). The `.infisical.json` in this repo points to the `secret-management` project.

Key secrets used by Quinn:

| Secret (Infisical)    | Maps to               | Purpose                          |
| --------------------- | --------------------- | -------------------------------- |
| `DISCORD_BOT_QUINN`   | `DISCORD_BOT_TOKEN`   | Discord bot for reading channels |
| `ANTHROPIC_API_KEY`   | `ANTHROPIC_API_KEY`   | Claude API access                |
| `GITHUB_TOKEN`        | `GITHUB_TOKEN`        | GitHub issue/PR operations       |
| `DISCORD_WEBHOOK_URL` | `DISCORD_WEBHOOK_URL` | Discord webhook for publishing   |

The `DISCORD_BOT_QUINN` to `DISCORD_BOT_TOKEN` mapping happens automatically in the entrypoint.

### Manual fallback (no Infisical)

If Infisical is not available, pass env vars directly:

```bash
DISCORD_BOT_TOKEN=xxx ANTHROPIC_API_KEY=xxx GITHUB_TOKEN=xxx docker compose up --build
```

## Chat Commands

| Command              | Description                                |
| -------------------- | ------------------------------------------ |
| `/audit`             | Full board scan across all configured apps |
| `/qa [version]`      | Run QA playbook for a specific version     |
| `/triage`            | Triage open GitHub issues                  |
| `/release [version]` | Generate release notes                     |
| `/bugs`              | Show active bug reports across apps        |
| `/status`            | Quick health check                         |
| `/help`              | Show available commands                    |

## QA Playbooks

### Release QA

Scope changes, typecheck, wiring check, API contracts, board state. Ends with PASS/WARN/FAIL verdict.

### Bug Triage

Scan board + GitHub issues, cross-reference with recent commits, classify and close stale issues.

### PR Review

Check CI, CodeRabbit threads, auto-merge status. Flag PRs needing attention.

### Release Notes

Git log between tags + merged PRs + done features. Categorize, format, publish to Discord.

## Multi-App Support

Quinn monitors multiple apps in your protoLabs Studio portfolio. Configure in `config/qa-config.json`:

```json
{
  "apps": [
    {
      "name": "protoMaker",
      "projectPath": "/path/to/project",
      "serverUrl": "http://localhost:3008",
      "githubRepo": "org/repo"
    }
  ]
}
```

## Part of protoLabs

Quinn is part of the [protoLabs](https://protolabs.studio) autonomous development studio.

| Agent               | Role                                              |
| ------------------- | ------------------------------------------------- |
| **Ava**             | Chief of Staff — orchestration and strategy       |
| **Quinn**           | QA Engineer — verification and release management |
| **protoResearcher** | Research — AI/ML paper tracking and analysis      |
